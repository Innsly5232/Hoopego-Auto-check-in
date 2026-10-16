"""Exercise the full CLI against a loopback service, never real account APIs."""

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
import urllib.request
import zlib

import checkin


TODAY = date(2026, 10, 12)
REAL_TRANSPORT = checkin.HttpTransport


def synthetic_account(slot):
    """Produce the paired capture bundle accepted by the offline HAR importer."""
    token = f"synthetic-session-{slot:02d}"
    requests = {}
    for kind, url in (("query", checkin.QUERY_URL), ("signin", checkin.SIGNIN_URL)):
        requests[kind] = {
            "url": url,
            "method": "POST",
            "headers": {
                "hopegoo-token": token,
                "hopegoo-encryptkey": f"synthetic-{kind}-key-{slot:02d}",
                "cookie": f"sectoken={token}",
                "content-type": "application/json; charset=utf-8",
                # Exported transport headers must be replaced for the actual body.
                "content-length": "999999",
                "accept-encoding": "br,gzip,deflate",
            },
            "body": f"synthetic-{kind}-opaque-{slot:02d}++==\n",
        }
    return {"schema_version": 1, **requests}


class OfflineEndToEndTests(unittest.TestCase):
    def test_ten_accounts_real_http_report_and_one_aggregate_sms(self):
        bundles = {slot: synthetic_account(slot) for slot in range(1, 11)}
        records = []
        handler_errors = []
        sms_payloads = []
        states = {slot: {"signed": slot == 2, "days": 11 if slot == 2 else slot - 1}
                  for slot in range(1, 11)}
        query_path = "/sea_shell/manta-you/memberCheckin/listCheckin"
        signin_path = "/sea_shell/manta-you/memberCheckin/performCheckin"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, payload, status=200, encoding="identity"):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if encoding == "gzip":
                    raw = gzip.compress(raw)
                elif encoding == "deflate":
                    raw = zlib.compress(raw)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Encoding", encoding)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/send":
                    records.append(("sms", self.path))
                    try:
                        sms_payloads.append(json.loads(raw))
                    except ValueError:
                        handler_errors.append("SMS payload is not JSON")
                    self.respond({"code": 200, "msg": "accepted by loopback fixture"})
                    return
                token = self.headers.get("hopegoo-token", "")
                slot = next((number for number in bundles
                             if bundles[number]["query"]["headers"]["hopegoo-token"] == token), None)
                if slot is None or self.path != signin_path:
                    handler_errors.append("Unexpected account or endpoint")
                    self.respond({"status": 404}, status=404)
                    return
                records.append((slot, self.path))
                expected = bundles[slot]["signin"]
                for key in ("hopegoo-token", "cookie"):
                    if self.headers.get(key) != expected["headers"][key]:
                        handler_errors.append(f"Account {slot:02d} paired header changed")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
                if payload != {"date": TODAY.isoformat()} or self.headers.get("hopegoo-encryptkey"):
                    handler_errors.append(f"Account {slot:02d} did not send today's unencrypted JSON date")
                    self.respond({"status": 80001, "body": None})
                    return
                if self.headers.get("Content-Type") != "application/json; charset=utf-8":
                    handler_errors.append(f"Account {slot:02d} JSON content type missing")
                if self.headers.get("accept-encoding") != "gzip, deflate":
                    handler_errors.append("Transport encoding header was not replaced")
                if slot == 5:
                    # Deliberately echo private fixture text: production logs must omit it.
                    self.respond({"message": token + "-private-rejection"}, status=401)
                    return
                state = states[slot]
                already_signed = state["signed"]
                if not already_signed:
                    state["signed"] = True
                    state["days"] += 1
                # A misleading repeat reward must never appear as newly earned.
                self.respond({"status": 80002 if already_signed else 200,
                              "body": {"coins": 777 if already_signed else 5,
                                       "continuousDays": state["days"]}}, encoding="deflate")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        local_origin = f"http://127.0.0.1:{server.server_port}"
        mappings = {
            checkin.QUERY_URL: local_origin + query_path,
            checkin.SIGNIN_URL: local_origin + signin_path,
            checkin.PUSH_URL: local_origin + "/send",
        }

        class LoopbackTransport(REAL_TRANSPORT):
            def __init__(self):
                super().__init__(timeout=3)
                self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), checkin.NoRedirect())

            def post(self, url, headers, body):
                # Never permit a live or arbitrary destination, even on a test failure.
                if url not in mappings:
                    raise AssertionError("End-to-end test received an unmapped destination")
                return super().post(mappings[url], headers, body)

        environment = {f"HOPEGOO_ACCOUNT_{slot:02d}": json.dumps(bundle)
                       for slot, bundle in bundles.items()}
        environment.update({"PUSHPLUS_TOKEN": "synthetic-pushplus-token",
                            "PUSHPLUS_OPTION": "synthetic-cmcc-option"})
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                summary_path = Path(directory) / "summary.md"
                environment["GITHUB_STEP_SUMMARY"] = str(summary_path)
                with mock.patch.dict("os.environ", environment, clear=True), \
                        mock.patch("checkin.HttpTransport", side_effect=LoopbackTransport), \
                        mock.patch("checkin.current_date", return_value=TODAY), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    checkin_exit = checkin.main(["--mode", "checkin", "--report", str(report_path)])
                    report_text = report_path.read_text(encoding="utf-8")
                    results = checkin.read_report(report_path, TODAY)
                    notify_exit = checkin.main(["--notify", "--report", str(report_path)])
                summary_text = summary_path.read_text(encoding="utf-8")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)

        self.assertEqual(handler_errors, [])
        self.assertEqual((checkin_exit, notify_exit), (1, 0))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(results), 10)
        self.assertEqual(json.loads(report_text)["mode"], "checkin")
        self.assertEqual(json.loads(report_text)["date"], TODAY.isoformat())
        for slot, result in enumerate(results, start=1):
            self.assertEqual(result.label, f"{slot:02d}")
            calls = [path for recorded_slot, path in records if recorded_slot == slot]
            if slot == 5:
                self.assertEqual(calls, [signin_path])
                self.assertEqual((result.status, result.submission_status), ("failed", "rejected"))
                self.assertIn("登录会话失效或被拒绝", result.reason)
            else:
                self.assertEqual(calls, [signin_path])
                self.assertTrue(states[slot]["signed"])
                if slot == 2:
                    self.assertEqual((result.status, result.days, result.coins), ("already", 11, None))
                    self.assertEqual((result.submission_status, result.submission_http, result.submission_code),
                                     ("already", 200, 80002))
                else:
                    self.assertEqual((result.status, result.days, result.coins), ("success", slot, 5))
                    self.assertEqual((result.submission_status, result.submission_http, result.submission_code),
                                     ("accepted", 200, 200))
        self.assertEqual(len(sms_payloads), 1)
        self.assertEqual(records[-1], ("sms", "/send"))
        payload = sms_payloads[0]
        self.assertEqual((payload["channel"], payload["template"], payload["option"]),
                         ("cmcc", "txt", "synthetic-cmcc-option"))
        self.assertEqual(payload["token"], "synthetic-pushplus-token")
        expected_title, expected_content = checkin.format_message(results, TODAY)
        self.assertEqual((payload["title"], payload["content"]), (expected_title, expected_content))
        self.assertEqual(payload["content"].splitlines()[0], "成功8｜已签1｜失败1")
        for slot in range(1, 11):
            self.assertIn(f"{slot:02d}：", payload["content"])
        self.assertIn("10：成功，+5 G-coins，连续10天", payload["content"])
        self.assertNotIn("777", payload["content"])
        self.assertIn("| 10 | 接口已接受 | 200 | 200 |", summary_text)
        self.assertIn("| 05 | 接口返回失败 | 401 | 未知 |", summary_text)
        public_outputs = stdout.getvalue() + stderr.getvalue() + report_text + summary_text
        private_values = ["synthetic-pushplus-token", "synthetic-cmcc-option", "private-rejection"]
        for bundle in bundles.values():
            for kind in ("query", "signin"):
                private_values.append(bundle[kind]["body"].strip())
                private_values.extend(bundle[kind]["headers"][key]
                                      for key in ("hopegoo-token", "hopegoo-encryptkey", "cookie"))
        for private_value in private_values:
            self.assertNotIn(private_value, public_outputs)


if __name__ == "__main__":
    unittest.main()
