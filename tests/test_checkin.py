import contextlib
from datetime import date, timedelta
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

import checkin


TODAY = date(2026, 10, 12)


def secret(token="dummy-session"):
    def request(url, body):
        return {"url": url, "method": "POST", "body": body, "headers": {
            "hopegoo-token": token, "hopegoo-encryptkey": "dummy-key",
            "cookie": "sectoken=" + token, "content-type": "application/json",
            "content-length": "999", "accept-encoding": "br,gzip,deflate",
        }}
    return json.dumps({"schema_version": 1, "name": "账号01",
        "query": request(checkin.QUERY_URL, "opaque-query==\n"),
        "signin": request(checkin.SIGNIN_URL, "opaque-signin++=="),
    })


def calendar(signed, days, today=TODAY):
    return {"status": 200, "message": "Request Success", "body": {
        "continuousDays": days, "todayStatus": int(signed),
        "checkinInfoList": [{"today": 1, "status": int(signed), "dateText2": today.isoformat()}],
    }}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers, body):
        self.calls.append((url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.account = checkin.Account.from_secret(secret(), 1)

    def test_direct_success_uses_only_submission_response(self):
        for body, coins, days in [({"coins": 5, "continuousDays": 3}, 5, 3), ({}, None, None), (None, None, None)]:
            with self.subTest(body=body):
                transport = FakeTransport({"status": 200, "body": body})
                result = checkin.check_account(self.account, transport, "checkin", TODAY)
                self.assertEqual((result.status, result.submission_status, result.coins, result.days),
                                 ("success", "accepted", coins, days))
                self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL])

    def test_direct_duplicate_needs_no_query_and_never_claims_reward(self):
        transport = FakeTransport({"status": 80002, "body": {"coins": 999}})
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual((result.status, result.submission_status, result.submission_code),
                         ("already", "already", 80002))
        self.assertIsNone(result.coins)
        self.assertIsNone(result.days)
        self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL])
        content = checkin.format_message([result], TODAY)[1]
        self.assertEqual(content, "成功0｜已签1｜失败0\n01：今日已签到")
        self.assertNotIn("连续", content)
        self.assertNotIn("999", content)

    def test_business_rejections_are_failures_without_queries_or_retries(self):
        for code in (80001, 409, 80003, 500):
            with self.subTest(code=code):
                transport = FakeTransport({"status": code, "message": "dummy-private-backend-text", "body": None})
                result = checkin.check_account(self.account, transport, "checkin", TODAY)
                self.assertEqual((result.status, result.submission_status, result.submission_code),
                                 ("failed", "rejected", code))
                self.assertIn(str(code), result.reason)
                self.assertNotIn("dummy-private", result.reason)
                self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL])

    def test_timeout_and_incomplete_response_are_not_retried_or_claimed_unsigned(self):
        cases = [(checkin.SafeError("请求超时"), "uncertain", "unknown", None),
                 (checkin.SafeError("接口响应格式异常", http_status=200), "uncertain", "unknown", 200),
                 (checkin.SafeError("登录会话失效或被拒绝", http_status=403), "failed", "rejected", 403)]
        for failure, status, submission, http in cases:
            with self.subTest(reason=str(failure)):
                transport = FakeTransport(failure)
                result = checkin.check_account(self.account, transport, "checkin", TODAY)
                self.assertEqual((result.status, result.submission_status, result.submission_http),
                                 (status, submission, http))
                self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL])
                self.assertIsNone(result.coins)
                if status == "uncertain":
                    self.assertIn("签到响应未确认", result.reason)

    def test_old_capture_generates_current_date_and_keeps_original_session(self):
        self.account.signin.headers["traceparent"] = "dummy-captured-trace"
        original_headers = dict(self.account.signin.headers)
        original_body = self.account.signin.body
        for today in (TODAY, TODAY + timedelta(days=1)):
            with self.subTest(today=today):
                transport = FakeTransport({"status": 200, "body": {"coins": 5}})
                result = checkin.check_account(self.account, transport, "checkin", today)
                self.assertEqual(result.status, "success")
                self.assertEqual(len(transport.calls), 1)
                url, headers, body = transport.calls[0]
                self.assertEqual(url, self.account.signin.url)
                self.assertEqual(json.loads(body), {"date": today.isoformat()})
                for key in ("hopegoo-token", "cookie"):
                    self.assertEqual(headers[key], original_headers[key])
                for key in ("hopegoo-encryptkey", "traceparent", "content-length"):
                    self.assertNotIn(key, headers)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(self.account.signin.headers, original_headers)
                self.assertEqual(self.account.signin.body, original_body)

    def test_backup_submits_once_and_accepts_duplicate_response(self):
        transport = FakeTransport({"status": 200, "body": {"coins": 5}}, {"status": 80002})
        primary = checkin.check_account(self.account, transport, "checkin", TODAY)
        backup = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual((primary.status, backup.status), ("success", "already"))
        self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL] * 2)
        self.assertIsNone(backup.coins)

    def test_optional_query_mode_is_single_read_only_request(self):
        for response, expected in [(calendar(True, 2), "already"), (calendar(False, 1), "query_only"),
                                   (calendar(False, 1, TODAY - timedelta(days=1)), "failed"),
                                   ({"status": 80002}, "failed"), (checkin.SafeError("请求超时"), "failed")]:
            with self.subTest(expected=expected):
                transport = FakeTransport(response)
                result = checkin.check_account(self.account, transport, "query", TODAY)
                self.assertEqual(result.status, expected)
                self.assertEqual([url for url, _, _ in transport.calls], [checkin.QUERY_URL])
                self.assertEqual(transport.calls[0][2], self.account.query.body)

    def test_direct_cli_notifies_success_already_and_failure_from_same_report(self):
        cases = [({"status": 200, "body": {"coins": 5}}, 0, "成功1｜已签0｜失败0"),
                 ({"status": 80002, "body": {"coins": 999}}, 0, "成功0｜已签1｜失败0"),
                 ({"status": 80001}, 1, "成功0｜已签0｜失败1"),
                 (checkin.SafeError("请求超时"), 1, "成功0｜已签0｜失败1")]
        for response, expected_exit, expected_summary in cases:
            with self.subTest(expected_summary=expected_summary):
                transport = FakeTransport(response, {"code": 200})
                output = io.StringIO()
                environment = {"HOPEGOO_ACCOUNT_01": secret(), "PUSHPLUS_TOKEN": "dummy-push"}
                with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", environment, clear=True), \
                        mock.patch("checkin.HttpTransport", return_value=transport), \
                        mock.patch("checkin.current_date", return_value=TODAY), contextlib.redirect_stdout(output):
                    report = Path(directory) / "report.json"
                    code = checkin.main(["--mode", "checkin", "--report", str(report)])
                    self.assertEqual(code, expected_exit)
                    self.assertEqual(checkin.main(["--notify", "--report", str(report)]), 0)
                self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL, checkin.PUSH_URL])
                payload = json.loads(transport.calls[-1][2])
                self.assertEqual(payload["channel"], "cmcc")
                self.assertIn(expected_summary, payload["content"])
                self.assertNotIn("999", payload["content"])
                for private_text in ("dummy-session", "dummy-key", "dummy-push"):
                    self.assertNotIn(private_text, output.getvalue())

    def test_bad_account_does_not_block_other_accounts_or_notification(self):
        environment = {"HOPEGOO_ACCOUNT_01": "not-json-dummy-private-text", "HOPEGOO_ACCOUNT_02": secret()}
        transport = FakeTransport({"status": 200, "body": {}}, {"code": 200})
        results = checkin.run_accounts(environment, transport, "checkin", TODAY)
        self.assertEqual([(item.label, item.status) for item in results], [("01", "failed"), ("02", "success")])
        title, content = checkin.format_message(results, TODAY)
        checkin.push_message({"PUSHPLUS_TOKEN": "dummy-push"}, transport, title, content)
        self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL, checkin.PUSH_URL])
        self.assertIn("成功1｜已签0｜失败1", content)
        self.assertNotIn("dummy-private", content)

    def test_rejected_submission_does_not_stop_next_account_or_summary(self):
        environment = {"HOPEGOO_ACCOUNT_01": secret("dummy-first"), "HOPEGOO_ACCOUNT_02": secret("dummy-second")}
        transport = FakeTransport({"status": 80001}, {"status": 200, "body": {"coins": 5}}, {"code": 200})
        results = checkin.run_accounts(environment, transport, "checkin", TODAY)
        self.assertEqual([result.status for result in results], ["failed", "success"])
        title, content = checkin.format_message(results, TODAY)
        checkin.push_message({"PUSHPLUS_TOKEN": "dummy-push"}, transport, title, content)
        self.assertEqual([url for url, _, _ in transport.calls], [checkin.SIGNIN_URL] * 2 + [checkin.PUSH_URL])
        self.assertIn("成功1｜已签0｜失败1", content)

    def test_replay_preserves_opaque_body_and_paired_key(self):
        self.assertEqual(self.account.query.body, b"opaque-query==\n")
        self.assertEqual(self.account.signin.body, b"opaque-signin++==")
        self.assertEqual(self.account.signin.headers["hopegoo-encryptkey"], "dummy-key")
        self.assertNotIn("content-length", self.account.query.headers)
        self.assertNotIn("accept-encoding", self.account.query.headers)

    def test_different_accounts_cannot_be_combined(self):
        value = json.loads(secret())
        value["signin"]["headers"]["hopegoo-token"] = "different-dummy-account"
        with self.assertRaises(checkin.SafeError):
            checkin.Account.from_secret(json.dumps(value), 1)

    def test_other_domain_or_method_cannot_receive_credentials(self):
        for key, replacement in [("url", "https://untrusted.invalid"), ("method", "GET")]:
            value = json.loads(secret())
            value["signin"][key] = replacement
            with self.assertRaises(checkin.SafeError):
                checkin.Account.from_secret(json.dumps(value), 1)

    def test_new_domain_preserves_both_actual_urls(self):
        value = json.loads(secret())
        for kind in ("query", "signin"):
            value[kind]["url"] = value[kind]["url"].replace("app.hopegoo.com", "app.reshg.com")
        account = checkin.Account.from_secret(json.dumps(value), 1)
        self.assertEqual(account.query.url, value["query"]["url"])
        self.assertEqual(account.signin.url, value["signin"]["url"])

    def test_new_secret_cannot_mix_domains_or_conflicting_cookie(self):
        value = json.loads(secret())
        value["query"]["url"] = value["query"]["url"].replace("app.hopegoo.com", "app.reshg.com")
        with self.assertRaisesRegex(checkin.SafeError, "不同域名"):
            checkin.Account.from_secret(json.dumps(value), 1)
        value = json.loads(secret())
        value["query"]["headers"]["cookie"] = "sectoken=different-session"
        with self.assertRaisesRegex(checkin.SafeError, "sectoken 不一致"):
            checkin.Account.from_secret(json.dumps(value), 1)


    def test_duplicate_result_report_roundtrip_and_summary(self):
        result = checkin.Result("01", "already", days=2, submission_status="already",
                               submission_http=200, submission_code=80002, reason="接口返回已签到")
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / "report.json"
            checkin.write_report(path, [result], TODAY, "checkin")
            self.assertEqual(checkin.read_report(path, TODAY), [result])
        _, content = checkin.format_message([result], TODAY)
        self.assertIn("已签1｜失败0", content)
        self.assertNotIn("G-coins", content)


    def test_header_injection_and_compressed_body_are_rejected(self):
        for key, value in [("cookie", "fake\r\nInjected: true"), ("content-encoding", "gzip")]:
            config = json.loads(secret())
            config["signin"]["headers"][key] = value
            with self.assertRaises(checkin.SafeError):
                checkin.Account.from_secret(json.dumps(config), 1)


    def test_no_account_configuration_produces_failure_summary(self):
        results = checkin.run_accounts({}, FakeTransport(), "checkin", TODAY)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "failed")

    def test_approved_success_sms_format(self):
        title, content = checkin.format_message([checkin.Result("01", "success", days=2, coins=5)], TODAY)
        self.assertEqual(title, "Hopegoo签到 10/12")
        self.assertEqual(content, "成功1｜已签0｜失败0\n01：成功，+5 G-coins，连续2天")

    def test_already_signed_sms_has_no_invented_reward(self):
        _, content = checkin.format_message([checkin.Result("01", "already", days=2)], TODAY)
        self.assertEqual(content, "成功0｜已签1｜失败0\n01：今日已签到，连续2天")
        self.assertNotIn("G-coins", content)

    def test_failure_and_unknown_reward_sms(self):
        _, content = checkin.format_message([checkin.Result("01", "failed", reason="请求超时")], TODAY)
        self.assertEqual(content, "成功0｜已签0｜失败1\n01：失败，请求超时")
        _, content = checkin.format_message([checkin.Result("01", "success", days=2)], TODAY)
        self.assertIn("奖励未知", content)

    def test_local_report_contains_sanitized_results_and_rejects_another_date(self):
        # Test the local CLI rule independently of the runner's GitHub identity.
        # The separate midnight test covers the same GitHub run's date exception.
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / "report.json"
            result = checkin.Result("01", "already", days=2)
            checkin.write_report(path, [result], TODAY, "checkin")
            self.assertEqual(checkin.read_report(path, TODAY), [result])
            self.assertNotIn("dummy-session", path.read_text())
            self.assertNotIn("dummy-key", path.read_text())
            self.assertEqual(checkin.read_report(path, date(2026, 10, 13))[0].status, "failed")

    def test_notification_across_midnight_keeps_same_run_results_and_date(self):
        transport = FakeTransport({"code": 200})
        output = io.StringIO()
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                       "PUSHPLUS_TOKEN": "dummy-push"}
        results = [checkin.Result("01", "success", days=2, coins=5), checkin.Result("02", "already", days=3)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch("checkin.HttpTransport", return_value=transport), \
                mock.patch("checkin.current_date", return_value=date(2026, 10, 13)), \
                contextlib.redirect_stdout(output):
            path = Path(directory) / "report.json"
            checkin.write_report(path, results, TODAY, "checkin")
            code = checkin.main(["--notify", "--report", str(path)])
        self.assertEqual(code, 0)
        payload = json.loads(transport.calls[0][2])
        self.assertEqual(payload["title"], "Hopegoo签到 10/12")
        self.assertIn("成功1｜已签1｜失败0", payload["content"])
        self.assertIn("02：今日已签到", payload["content"])

    def test_report_from_other_run_or_attempt_cannot_be_reused(self):
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", environment, clear=True):
            path = Path(directory) / "report.json"
            checkin.write_report(path, [checkin.Result("01", "success", days=2, coins=5)], TODAY, "checkin")
            for replacement in ({"GITHUB_RUN_ID": "124"}, {"GITHUB_RUN_ATTEMPT": "2"}):
                with self.subTest(replacement=replacement), mock.patch.dict("os.environ", replacement):
                    for notification_date in (TODAY, date(2026, 10, 13)):
                        self.assertEqual(checkin.read_report(path, notification_date)[0].status, "failed")
            # Matching run metadata still cannot authorize a future or very old report.
            for notification_date in (date(2026, 10, 11), date(2026, 10, 14)):
                self.assertEqual(checkin.read_report(path, notification_date)[0].status, "failed")

    def test_missing_report_still_creates_failure_notification(self):
        results = checkin.read_report(Path("not-a-real-report.json"), TODAY)
        title, content = checkin.format_message(results, TODAY)
        transport = FakeTransport({"code": 200})
        checkin.push_message({"PUSHPLUS_TOKEN": "dummy-push-secret"}, transport, title, content)
        payload = json.loads(transport.calls[0][2])
        self.assertEqual(payload["channel"], "cmcc")
        self.assertIn("失败1", payload["content"])
        self.assertIn("脚本未完成", payload["content"])

    def test_pushplus_rejection_is_not_reported_as_delivery(self):
        with self.assertRaises(checkin.SafeError):
            checkin.push_message({"PUSHPLUS_TOKEN": "dummy-token"}, FakeTransport({"code": 400}), "title", "summary")
        with self.assertRaises(checkin.SafeError):
            checkin.push_message({}, FakeTransport(), "title", "summary")

    def test_notify_cli_does_not_need_account_credentials(self):
        transport = FakeTransport({"code": 200})
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {"PUSHPLUS_TOKEN": "dummy-push"}, clear=True), \
                mock.patch("checkin.HttpTransport", return_value=transport), mock.patch("checkin.current_date", return_value=TODAY), \
                contextlib.redirect_stdout(output):
            code = checkin.main(["--notify", "--report", str(Path(directory) / "missing.json")])
        self.assertEqual(code, 0)
        self.assertNotIn("dummy-push", output.getvalue())
        self.assertEqual(len(transport.calls), 1)

    def test_notify_summary_records_failure_without_claiming_delivery(self):
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("checkin.current_date", return_value=TODAY), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            summary_path = Path(directory) / "summary.md"
            with mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                code = checkin.main(["--notify", "--report", str(Path(directory) / "missing.json")])
            summary = summary_path.read_text()
        self.assertEqual(code, 1)
        self.assertIn("短信提交失败", summary)
        self.assertIn("PUSHPLUS_TOKEN 未配置", summary)
        self.assertNotIn("已接受", summary)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        seen = cls.seen
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append((self.path, body))
                if self.path.startswith("/status/"):
                    self.send_response(int(self.path.rsplit("/", 1)[1]))
                    self.end_headers()
                    self.wfile.write(b"untrusted-dummy-backend-text")
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-credentials")
                    self.end_headers()
                    return
                if self.path == "/denied":
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"untrusted-dummy-backend-text")
                    return
                raw = gzip.compress(json.dumps({"status": 200, "body": {"coins": 5}}).encode())
                self.send_response(200)
                if self.path == "/truncated-chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"40\r\n{\"status\":200}")
                    return
                if self.path == "/truncated-length":
                    raw = b'{"status":200,"body":{}}'
                    self.send_header("Content-Length", str(len(raw) + 12))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/truncated-gzip":
                    raw = raw[:-8]
                if self.path == "/expanded-too-large":
                    raw = gzip.compress(b"x" * (checkin.MAX_RESPONSE_BYTES + 1))
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_http_body_bytes_and_compressed_json_response(self):
        result = checkin.HttpTransport(timeout=2).post(self.base + "/gzip", {}, b"opaque-raw-body==")
        self.assertEqual(result["body"]["coins"], 5)
        self.assertEqual(self.seen[-1], ("/gzip", b"opaque-raw-body=="))

    def test_redirect_cannot_forward_capture_headers(self):
        before = len(self.seen)
        with self.assertRaises(checkin.SafeError):
            checkin.HttpTransport(timeout=2).post(self.base + "/redirect", {"cookie": "dummy-session"}, b"opaque")
        self.assertEqual(self.seen[before:], [("/redirect", b"opaque")])

    def test_http_rejection_metadata_does_not_expose_raw_response(self):
        with self.assertRaises(checkin.SafeError) as raised:
            checkin.HttpTransport(timeout=2).post(self.base + "/denied", {}, b"opaque")
        self.assertEqual(raised.exception.http_status, 403)
        self.assertEqual(str(raised.exception), "登录会话失效或被拒绝")
        self.assertNotIn("untrusted-dummy-backend-text", str(raised.exception))

    def test_http_errors_are_never_retried(self):
        for status in (408, 429, 500, 502, 503, 504, 400, 401, 403, 404, 501):
            with self.subTest(status=status):
                before = len(self.seen)
                with self.assertRaises(checkin.SafeError) as raised:
                    checkin.HttpTransport(timeout=2).post(self.base + f"/status/{status}", {}, b"opaque")
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(len(self.seen) - before, 1)
                self.assertNotIn("untrusted-dummy-backend-text", str(raised.exception))

    def test_network_failure_metadata_does_not_make_transport_retry_posts(self):
        failures = [TimeoutError("untrusted-dummy-backend-text"),
                    urllib.error.URLError(ConnectionResetError("untrusted-dummy-backend-text")),
                    urllib.error.URLError(ssl.SSLCertVerificationError("untrusted-dummy-backend-text"))]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                transport = checkin.HttpTransport(timeout=2)
                with mock.patch.object(transport.opener, "open", side_effect=failure) as post:
                    with self.assertRaises(checkin.SafeError) as raised:
                        transport.post(checkin.SIGNIN_URL, {}, b"opaque")
                post.assert_called_once()
                self.assertNotIn("untrusted-dummy-backend-text", str(raised.exception))

    def test_incomplete_submission_never_queries_or_retries(self):
        for path in ("/truncated-gzip", "/truncated-length", "/truncated-chunked"):
            with self.subTest(path=path):
                network = checkin.HttpTransport(timeout=2)
                before = len(self.seen)
                calls = []
                base = self.base

                class SubmissionTransport:
                    def post(self, url, headers, body):
                        calls.append(url)
                        if url == checkin.SIGNIN_URL:
                            return network.post(base + path, headers, body)
                        return calendar(len(calls) > 1, 1)

                account = checkin.Account.from_secret(secret(), 1)
                result = checkin.check_account(account, SubmissionTransport(), "checkin", TODAY)
                self.assertEqual(result.status, "uncertain")
                self.assertEqual(result.submission_status, "unknown")
                self.assertEqual(result.submission_http, 200)
                self.assertIsNone(result.days)
                self.assertIsNone(result.coins)
                self.assertIn("签到响应未确认", result.reason)
                self.assertEqual(calls, [checkin.SIGNIN_URL])
                self.assertEqual(self.seen[before:], [(path, b'{"date":"2026-10-12"}')])

    def test_compressed_response_cannot_exceed_expanded_limit(self):
        with self.assertRaises(checkin.SafeError) as raised:
            checkin.HttpTransport(timeout=2).post(self.base + "/expanded-too-large", {}, b"opaque")
        self.assertEqual(str(raised.exception), "接口响应过大")
        self.assertEqual(raised.exception.http_status, 200)


if __name__ == "__main__":
    unittest.main()
