import contextlib
from datetime import date
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

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

    def test_header_injection_and_compressed_body_are_rejected(self):
        for key, value in [("cookie", "fake\r\nInjected: true"), ("content-encoding", "gzip")]:
            config = json.loads(secret())
            config["signin"]["headers"][key] = value
            with self.assertRaises(checkin.SafeError):
                checkin.Account.from_secret(json.dumps(config), 1)

    def test_already_signed_only_queries_once(self):
        transport = FakeTransport(calendar(True, 2))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual((result.status, result.days, result.coins), ("already", 2, None))
        self.assertEqual([item[0] for item in transport.calls], [checkin.QUERY_URL])

    def test_unsigned_query_mode_never_submits(self):
        transport = FakeTransport(calendar(False, 1))
        result = checkin.check_account(self.account, transport, "query", TODAY)
        self.assertEqual(result.status, "query_only")
        self.assertEqual(len(transport.calls), 1)

    def test_cross_day_checkin_requires_post_query_confirmation(self):
        transport = FakeTransport(calendar(False, 1), {"status": 200, "body": {"coins": 5}}, calendar(True, 2))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual((result.status, result.coins, result.days), ("success", 5, 2))
        self.assertEqual([item[0] for item in transport.calls], [checkin.QUERY_URL, checkin.SIGNIN_URL, checkin.QUERY_URL])
        self.assertEqual(transport.calls[1][2], self.account.signin.body)

    def test_old_calendar_cannot_trigger_new_day_submission(self):
        transport = FakeTransport(calendar(False, 1, date(2026, 10, 11)))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "返回日期不是今天")
        self.assertEqual(len(transport.calls), 1)

    def test_unknown_status_or_inconsistent_calendar_does_not_submit(self):
        for field, value in [("todayStatus", 2), ("todayStatus", True), ("continuousDays", "1")]:
            response = calendar(False, 1)
            response["body"][field] = value
            transport = FakeTransport(response)
            self.assertEqual(checkin.check_account(self.account, transport, "checkin", TODAY).status, "failed")
            self.assertEqual(len(transport.calls), 1)
        response = calendar(False, 1)
        response["body"]["checkinInfoList"][0]["status"] = 1
        transport = FakeTransport(response)
        self.assertEqual(checkin.check_account(self.account, transport, "checkin", TODAY).status, "failed")

    def test_query_failure_never_submits(self):
        transport = FakeTransport(checkin.SafeError("请求超时"))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual(result.reason, "请求超时")
        self.assertEqual(len(transport.calls), 1)

    def test_submission_timeout_is_verified_and_never_retried(self):
        transport = FakeTransport(calendar(False, 1), checkin.SafeError("请求超时"), calendar(True, 2))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual((result.status, result.coins), ("success", None))
        self.assertEqual(sum(item[0] == checkin.SIGNIN_URL for item in transport.calls), 1)

    def test_success_response_without_signed_state_is_failure(self):
        transport = FakeTransport(calendar(False, 1), {"status": 200, "body": {"coins": 5}}, calendar(False, 1))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.coins)
        self.assertEqual(sum(item[0] == checkin.SIGNIN_URL for item in transport.calls), 1)

    def test_failed_verification_reports_unknown_without_claiming_reward(self):
        transport = FakeTransport(calendar(False, 1), {"status": 200, "body": {"coins": 5}}, checkin.SafeError("网络连接失败"))
        result = checkin.check_account(self.account, transport, "checkin", TODAY)
        self.assertEqual(result.status, "uncertain")
        self.assertIsNone(result.coins)

    def test_bad_account_does_not_prevent_other_accounts_running(self):
        environment = {"HOPEGOO_ACCOUNT_01": "not-json-dummy-private-text", "HOPEGOO_ACCOUNT_02": secret()}
        results = checkin.run_accounts(environment, FakeTransport(calendar(True, 2)), "checkin", TODAY)
        self.assertEqual([(item.label, item.status) for item in results], [("01", "failed"), ("02", "already")])
        self.assertNotIn("dummy-private", results[0].reason)

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

    def test_report_contains_only_sanitized_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            result = checkin.Result("01", "already", days=2)
            checkin.write_report(path, [result], TODAY, "checkin")
            self.assertEqual(checkin.read_report(path, TODAY), [result])
            self.assertNotIn("dummy-session", path.read_text())
            self.assertNotIn("dummy-key", path.read_text())
            self.assertEqual(checkin.read_report(path, date(2026, 10, 13))[0].status, "failed")

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
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-credentials")
                    self.end_headers()
                    return
                raw = gzip.compress(json.dumps({"status": 200, "body": {"coins": 5}}).encode())
                self.send_response(200)
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


if __name__ == "__main__":
    unittest.main()
