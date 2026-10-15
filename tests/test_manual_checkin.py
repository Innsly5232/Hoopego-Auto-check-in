"""Manual capture handling and outcomes, using synthetic credentials only."""

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
from urllib.parse import urlsplit

import checkin
import manual_checkin as manual


TODAY = date(2030, 3, 10)
TOKEN = "synthetic-manual-session"
KEY = "synthetic-paired-key"
BODY = b"AQIDBA=="


def pairs(url=checkin.SIGNIN_URL, token=TOKEN, body=BODY):
    return [
        (":method", "POST"), (":scheme", "https"),
        (":authority", urlsplit(url).netloc), (":path", urlsplit(url).path),
        ("hopegoo-token", token), ("hopegoo-encryptkey", KEY),
        ("cookie", "cnUser=sectoken=" + token),
        ("cookie", "sectoken=" + token), ("cookie", "currency=HKD"),
        ("content-type", "application/json; charset=utf-8"),
        ("content-length", str(len(body))), ("accept-encoding", "br,gzip,deflate"),
    ]


def capture(url=checkin.SIGNIN_URL, token=TOKEN, body=BODY):
    return manual.make_capture(pairs(url, token, body), body, url)


def calendar(signed=False, today=TODAY):
    return {"status": 200, "body": {
        "todayStatus": int(signed), "continuousDays": 3,
        "checkinInfoList": [{"today": 1, "dateText2": today.isoformat(), "status": int(signed)}],
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


class ManualCaptureTests(unittest.TestCase):
    def test_http2_headers_merge_all_cookies_and_keep_exact_body(self):
        value = capture(body=BODY + b"\n")
        self.assertEqual(value.request.body, BODY + b"\n")
        self.assertEqual(value.request.headers["cookie"],
                         f"cnUser=sectoken={TOKEN}; sectoken={TOKEN}; currency=HKD")
        self.assertEqual(value.request.headers["hopegoo-encryptkey"], KEY)
        self.assertFalse(any(key.startswith(":") for key in value.request.headers))
        self.assertNotIn("content-length", value.request.headers)
        self.assertNotIn("accept-encoding", value.request.headers)
        self.assertEqual(value.cookie_token_consistency, "matched")

    def test_conflicting_direct_or_nested_cookie_blocks_request(self):
        for cookie in ("sectoken=wrong-session", "cnUser=sectoken=wrong-session"):
            with self.subTest(cookie=cookie):
                headers = pairs() + [("cookie", cookie)]
                with self.assertRaisesRegex(checkin.SafeError, "sectoken 不一致"):
                    manual.make_capture(headers, BODY, checkin.SIGNIN_URL)

    def test_cookie_without_session_token_cannot_claim_matching_identity(self):
        headers = [(key, value) for key, value in pairs() if key != "cookie"]
        value = manual.make_capture(headers, BODY, checkin.SIGNIN_URL)
        self.assertEqual(value.cookie_token_consistency, "not_available")

    def test_length_mismatch_does_not_strip_or_repair_body(self):
        with self.assertRaisesRegex(checkin.SafeError, "Content-Length"):
            manual.make_capture(pairs(), BODY + b"\n", checkin.SIGNIN_URL)

    def test_duplicate_non_cookie_header_and_unsafe_value_are_rejected(self):
        for extra in (("hopegoo-token", TOKEN), ("x-header", "dummy\r\ninjected")):
            with self.subTest(extra=extra), self.assertRaises(checkin.SafeError):
                manual.make_capture(pairs() + [extra], BODY, checkin.SIGNIN_URL)

    def test_wrong_method_path_authority_and_scheme_are_rejected(self):
        for key, value in ((":method", "GET"), (":scheme", "http"),
                           (":authority", "untrusted.invalid"), (":path", "/wrong")):
            headers = [(name, value if name == key else original) for name, original in pairs()]
            with self.subTest(key=key), self.assertRaises(checkin.SafeError):
                manual.make_capture(headers, BODY, checkin.SIGNIN_URL)

    def test_har_is_single_request_and_capture_date_is_only_metadata(self):
        request = {"method": "POST", "url": checkin.SIGNIN_URL,
                   "headers": [{"name": key, "value": value} for key, value in pairs()],
                   "postData": {"text": BODY.decode()}}
        har = {"log": {"entries": [{"startedDateTime": "2020-01-01T23:00:00Z", "request": request}]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.har"
            path.write_text(json.dumps(har))
            value = manual.load_har(path, checkin.SIGNIN_URL)
            self.assertEqual(value.request.body, BODY)
            self.assertEqual(value.captured_date, "2020-01-02")
            har["log"]["entries"] *= 2
            path.write_text(json.dumps(har))
            with self.assertRaises(checkin.SafeError):
                manual.load_har(path, checkin.SIGNIN_URL)

    def test_har_cookie_array_is_used_when_header_is_absent(self):
        har = {"log": {"entries": [{"request": {
            "method": "POST", "url": checkin.SIGNIN_URL,
            "headers": [{"name": key, "value": value} for key, value in pairs() if key != "cookie"],
            "cookies": [{"name": "sectoken", "value": TOKEN}],
            "postData": {"text": BODY.decode()},
        }}]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.har"
            path.write_text(json.dumps(har))
            value = manual.load_har(path, checkin.SIGNIN_URL)
            self.assertEqual(value.request.headers["cookie"], "sectoken=" + TOKEN)

    def test_new_capture_host_is_preserved_in_har_and_raw_headers(self):
        actual_url = checkin.SIGNIN_URL.replace("app.hopegoo.com", "app.reshg.com")
        har = {"log": {"entries": [{"request": {
            "method": "POST", "url": actual_url,
            "headers": [{"name": key, "value": value} for key, value in pairs(actual_url)],
            "postData": {"text": BODY.decode()},
        }}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "synthetic.har").write_text(json.dumps(har))
            (root / "headers.txt").write_text("\n".join(f"{key}: {value}" for key, value in pairs(actual_url)))
            (root / "body.txt").write_bytes(BODY)
            for value in (manual.load_har(root / "synthetic.har", checkin.SIGNIN_URL),
                          manual.load_headers(root / "headers.txt", root / "body.txt", checkin.SIGNIN_URL)):
                self.assertEqual(value.request.url, actual_url)
                self.assertEqual(value.request.body, BODY)

    def test_unrecognized_origins_and_url_credentials_are_rejected(self):
        for url in (checkin.SIGNIN_URL.replace("app.hopegoo.com", "untrusted.invalid"),
                    checkin.SIGNIN_URL.replace("https://", "http://"),
                    checkin.SIGNIN_URL.replace("https://", "https://someone@"),
                    checkin.SIGNIN_URL + "?redirect=untrusted"):
            with self.subTest(url=url), self.assertRaises(checkin.SafeError):
                manual.make_capture(pairs(url), BODY, url)

    def test_cli_defaults_to_offline_and_outputs_no_capture_values(self):
        with tempfile.TemporaryDirectory() as directory:
            header_file, body_file = Path(directory) / "headers.txt", Path(directory) / "body.txt"
            header_file.write_text("\n".join(f"{key}: {value}" for key, value in pairs()))
            body_file.write_bytes(BODY)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch("manual_checkin.checkin.HttpTransport") as network:
                code = manual.main(["--headers-file", str(header_file), "--body-file", str(body_file)])
            network.assert_not_called()
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["result"], "inspected_offline")
        self.assertEqual(value["account_identity"], "unverified")
        self.assertEqual(value["session_check"], "not_performed")
        self.assertFalse(value["request"]["body_date_rewritten"])
        for private in (TOKEN, KEY, BODY.decode()):
            self.assertNotIn(private, output.getvalue())

    def test_bad_input_does_not_echo_private_content(self):
        with tempfile.TemporaryDirectory() as directory:
            header_file, body_file = Path(directory) / "headers.txt", Path(directory) / "body.txt"
            header_file.write_text("private-unparseable-session-material")
            body_file.write_bytes(BODY)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = manual.main(["--headers-file", str(header_file), "--body-file", str(body_file)])
        self.assertEqual(code, 1)
        self.assertNotIn("private-unparseable", output.getvalue())


class ManualRequestTests(unittest.TestCase):
    def setUp(self):
        self.clock = mock.patch("checkin.current_date", return_value=TODAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.signin = capture()
        self.query = capture(checkin.QUERY_URL, body=b"synthetic-query-body")

    def test_query_performs_only_read_request_without_claiming_identity(self):
        transport = FakeTransport(calendar())
        report, code = manual.execute("query", self.query, transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(report["session_check"], "accepted_by_query")
        self.assertEqual(report["account_identity"], "unverified")
        self.assertEqual([item[0] for item in transport.calls], [checkin.QUERY_URL])

    def test_new_host_query_and_submission_are_not_rewritten_to_old_host(self):
        signin_url = checkin.SIGNIN_URL.replace("app.hopegoo.com", "app.reshg.com")
        query_url = checkin.QUERY_URL.replace("app.hopegoo.com", "app.reshg.com")
        transport = FakeTransport(calendar(), {"status": 200, "body": {}}, calendar(True))
        report, code = manual.execute("submit", capture(signin_url), capture(query_url), transport)
        self.assertEqual((code, report["result"]), (0, "verified_signed"))
        self.assertEqual([url for url, _, _ in transport.calls], [query_url, signin_url, query_url])

    def test_mixed_capture_hosts_are_rejected_before_network(self):
        transport = FakeTransport()
        query_url = checkin.QUERY_URL.replace("app.hopegoo.com", "app.reshg.com")
        with self.assertRaisesRegex(checkin.SafeError, "不同域名"):
            manual.execute("submit", self.signin, capture(query_url), transport)
        self.assertEqual(transport.calls, [])

    def test_single_capture_submission_needs_no_invented_query_body(self):
        transport = FakeTransport({"status": 200, "body": {"coins": 5}})
        report, code = manual.execute("submit", self.signin, transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "accepted_unverified")
        self.assertEqual(report["verification"], "not_performed")
        self.assertEqual(transport.calls, [(checkin.SIGNIN_URL, self.signin.request.headers, BODY)])
        self.assertNotIn("coins", json.dumps(report))

    def test_duplicate_code_without_query_reports_unverified_already_state(self):
        transport = FakeTransport({"status": 80002, "body": {"coins": 999}})
        report, code = manual.execute("submit", self.signin, transport=transport)
        self.assertEqual((code, report["result"]), (0, "already_reported_unverified"))
        self.assertEqual(report["submission"]["state"], "already")
        self.assertEqual(report["submission"]["business_status"], 80002)
        self.assertEqual(report["verification"], "not_performed")
        self.assertNotIn("coins", json.dumps(report))
        self.assertEqual(len(transport.calls), 1)

    def test_duplicate_code_is_confirmed_only_by_a_successful_signed_postquery(self):
        for response, expected, code in ((calendar(True), "already_signed", 0),
                                         (calendar(False), "already_verification_failed", 1),
                                         (checkin.SafeError("网络连接失败"), "already_verification_failed", 1)):
            with self.subTest(expected=expected):
                transport = FakeTransport(calendar(False), {"status": 80002}, response)
                report, actual = manual.execute("submit", self.signin, self.query, transport)
                self.assertEqual((actual, report["result"]), (code, expected))
                self.assertEqual(sum(url == checkin.SIGNIN_URL for url, _, _ in transport.calls), 1)

    def test_mismatched_query_token_stops_before_network(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(checkin.SafeError, "Token 不一致"):
            manual.execute("submit", self.signin, capture(checkin.QUERY_URL, token="other-session"), transport)
        self.assertEqual(transport.calls, [])

    def test_query_business_failure_stops_before_submission_and_does_not_guess_expiry(self):
        transport = FakeTransport({"status": 412, "message": "synthetic-private-server-message"})
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "query_failed")
        self.assertIn("status=412", report["reason"])
        self.assertNotIn("synthetic-private-server-message", json.dumps(report))
        self.assertEqual(len(transport.calls), 1)

    def test_old_calendar_prevents_submission(self):
        transport = FakeTransport(calendar(today=date(2020, 1, 1)))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual(code, 1)
        self.assertEqual(report["reason"], "返回日期不是今天")
        self.assertEqual(len(transport.calls), 1)

    def test_already_signed_skips_submission(self):
        transport = FakeTransport(calendar(True))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (0, "already_signed"))
        self.assertEqual(report["submission"]["state"], "not_sent")
        self.assertEqual(len(transport.calls), 1)

    def test_prequery_submission_and_postquery_confirm_daily_state(self):
        transport = FakeTransport(calendar(), {"status": 200, "body": {}}, calendar(True))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (0, "verified_signed"))
        self.assertEqual([item[0] for item in transport.calls],
                         [checkin.QUERY_URL, checkin.SIGNIN_URL, checkin.QUERY_URL])
        self.assertEqual(transport.calls[1][2], BODY)

    def test_response_timeout_is_not_retried_or_hidden_by_signed_state(self):
        transport = FakeTransport(calendar(), checkin.SafeError("请求超时"), calendar(True))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (1, "submission_unconfirmed"))
        self.assertEqual(report["verification"], "confirmed_signed")
        self.assertEqual(sum(url == checkin.SIGNIN_URL for url, _, _ in transport.calls), 1)

    def test_submission_rejection_remains_failure_even_if_postquery_signed(self):
        transport = FakeTransport(calendar(), {"status": 409}, calendar(True))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (1, "submission_rejected"))
        self.assertEqual(report["submission"]["business_status"], 409)

    def test_postquery_failure_does_not_claim_daily_success(self):
        transport = FakeTransport(calendar(), {"status": 200, "body": {}}, checkin.SafeError("网络连接失败"))
        report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (1, "accepted_verification_failed"))
        self.assertEqual(report["submission"]["state"], "accepted")

    def test_dates_are_read_again_after_midnight(self):
        tomorrow = date(2030, 3, 11)
        transport = FakeTransport(calendar(), {"status": 200, "body": {}}, calendar(True, tomorrow))
        with mock.patch("checkin.current_date", side_effect=[TODAY, TODAY, tomorrow]):
            report, code = manual.execute("submit", self.signin, self.query, transport)
        self.assertEqual((code, report["result"]), (0, "verified_signed"))
        self.assertEqual(report["query_after"]["date"], tomorrow.isoformat())

    def test_wrong_mode_or_endpoint_cannot_send_requests(self):
        transport = FakeTransport()
        for mode, request in (("query", self.signin), ("submit", self.query), ("unknown", self.signin)):
            with self.subTest(mode=mode), self.assertRaises(checkin.SafeError):
                manual.execute(mode, request, transport=transport)
        self.assertEqual(transport.calls, [])


class ManualHttpTests(unittest.TestCase):
    def test_cli_replays_exact_capture_over_http_and_confirms_without_notifications(self):
        received = []
        state = {"signed": False}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, dict(self.headers), body))
                if self.path.endswith("/performCheckin"):
                    state["signed"] = True
                    payload = {"status": 200, "body": {"coins": 5}}
                else:
                    payload = calendar(state["signed"])
                raw = gzip.compress(json.dumps(payload).encode())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        network = checkin.HttpTransport(timeout=3)

        class LoopbackTransport:
            def post(self, url, headers, body):
                path = urlsplit(url).path
                return network.post(f"http://127.0.0.1:{server.server_port}" + path, headers, body)

        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for label, url in (("signin", checkin.SIGNIN_URL), ("query", checkin.QUERY_URL)):
                    (root / f"{label}-headers.txt").write_text(
                        "\r\n".join(f"{key}: {value}" for key, value in pairs(url)))
                    (root / f"{label}-body.txt").write_bytes(BODY)
                output = io.StringIO()
                with mock.patch("checkin.current_date", return_value=TODAY), \
                        mock.patch("checkin.HttpTransport", return_value=LoopbackTransport()), \
                        contextlib.redirect_stdout(output):
                    code = manual.main([
                        "submit", "--headers-file", str(root / "signin-headers.txt"),
                        "--body-file", str(root / "signin-body.txt"),
                        "--query-headers-file", str(root / "query-headers.txt"),
                        "--query-body-file", str(root / "query-body.txt"),
                    ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["result"], "verified_signed")
            self.assertEqual([path.rsplit("/", 1)[-1] for path, _, _ in received],
                             ["listCheckin", "performCheckin", "listCheckin"])
            for _, headers, body in received:
                normalized = {key.lower(): value for key, value in headers.items()}
                self.assertEqual(body, BODY)
                self.assertEqual(int(normalized["content-length"]), len(BODY))
                self.assertEqual(normalized["hopegoo-token"], TOKEN)
                self.assertEqual(normalized["hopegoo-encryptkey"], KEY)
                self.assertEqual(normalized["accept-encoding"], "gzip, deflate")
                self.assertIn("cnUser=sectoken=" + TOKEN, normalized["cookie"])
                self.assertIn("; sectoken=" + TOKEN, normalized["cookie"])
            for private in (TOKEN, KEY, BODY.decode()):
                self.assertNotIn(private, output.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
