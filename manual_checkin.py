#!/usr/bin/env python3
"""Inspect or replay one user-owned capture without Actions or notifications."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime
import hmac
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import checkin


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
ALLOWED_HOSTS = checkin.ALLOWED_HOSTS
supported_url = checkin.supported_url


@dataclass(frozen=True)
class Capture:
    request: checkin.CapturedRequest
    cookie_token_consistency: str
    captured_date: str | None = None


def read_private_file(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CAPTURE_BYTES + 1)
    except OSError:
        raise checkin.SafeError("无法读取请求资料文件") from None
    if len(raw) > MAX_CAPTURE_BYTES:
        raise checkin.SafeError("请求资料文件超过 2 MiB 限制")
    return raw


def decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise checkin.SafeError("请求资料须为 UTF-8；不会自动转换请求体") from None


def cookie_consistency(headers: dict[str, str]) -> str:
    return checkin.cookie_consistency(headers)


def make_capture(pairs: list[tuple[str, str]], body: bytes, url: str,
                 captured_at: str | None = None) -> Capture:
    if not supported_url(url):
        raise checkin.SafeError("仅允许已确认的 Hopegoo HTTPS 查询或签到接口")
    headers: dict[str, str] = {}
    pseudo: dict[str, str] = {}
    for key, value in pairs:
        if not isinstance(key, str) or not isinstance(value, str):
            raise checkin.SafeError("请求头格式无效")
        key = key.lower()
        if re.search(r"[\r\n\x00]", value):
            raise checkin.SafeError("请求头含无效字符")
        if key.startswith(":"):
            if key not in {":method", ":authority", ":path", ":scheme"} or key in pseudo:
                raise checkin.SafeError("HTTP/2 伪请求头无效或重复")
            pseudo[key] = value
        elif key in headers:
            if key != "cookie":
                raise checkin.SafeError("非 Cookie 请求头重复")
            headers[key] += "; " + value
        else:
            headers[key] = value
    expected = urlsplit(url)
    if (pseudo.get(":method", "POST") != "POST"
            or pseudo.get(":scheme", "https") != "https"
            or pseudo.get(":authority", expected.netloc).lower() != expected.netloc.lower()
            or pseudo.get(":path", expected.path) != expected.path
            or headers.get("host", expected.netloc).lower() != expected.netloc.lower()):
        raise checkin.SafeError("请求头中的方法、域名或路径与所选操作不匹配")
    length = headers.get("content-length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                               or int(length) != len(body)):
        raise checkin.SafeError("请求体长度与 Content-Length 不一致；请核对是否截断或多了换行")
    request = checkin.CapturedRequest.from_config({
        "url": url, "method": "POST", "headers": headers, "body": decode_text(body),
    }, url)
    captured_date = None
    if captured_at:
        try:
            moment = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                captured_date = moment.astimezone(checkin.SHANGHAI).date().isoformat()
        except ValueError:
            pass
    return Capture(request, cookie_consistency(request.headers), captured_date)


def load_headers(headers_path: Path, body_path: Path, url: str) -> Capture:
    pairs = []
    for line in decode_text(read_private_file(headers_path)).splitlines():
        if not line.strip():
            continue
        start = 1 if line.startswith(":") else 0
        key, separator, value = line[start:].partition(":")
        if not separator or not key:
            raise checkin.SafeError("请求头文本格式无效；每行应为名称、冒号和值")
        pairs.append(((":" if start else "") + key.strip(), value.strip()))
    hosts = [value.lower() for key, value in pairs if key.lower() in {":authority", "host"}]
    host = hosts[0] if hosts else urlsplit(url).netloc.lower()
    if host not in ALLOWED_HOSTS:
        raise checkin.SafeError("请求头中的域名不在已确认的接口列表中")
    actual_url = "https://" + host + urlsplit(url).path
    return make_capture(pairs, read_private_file(body_path), actual_url)


def load_har(path: Path, url: str) -> Capture:
    try:
        data = json.loads(decode_text(read_private_file(path)))
    except ValueError:
        raise checkin.SafeError("文件不是有效 HAR JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("log"), dict):
        raise checkin.SafeError("HAR 缺少 log.entries")
    entries = data["log"].get("entries")
    if not isinstance(entries, list):
        raise checkin.SafeError("HAR 缺少 log.entries")
    matches = [entry for entry in entries if isinstance(entry, dict)
               and isinstance(entry.get("request"), dict)
               and supported_url(entry["request"].get("url"), url)]
    if len(matches) != 1:
        raise checkin.SafeError("HAR 必须且只能包含一条所选接口的请求")
    entry = matches[0]
    request = entry["request"]
    post = request.get("postData")
    if request.get("method") != "POST" or not isinstance(post, dict):
        raise checkin.SafeError("HAR 缺少 POST 请求体")
    if (not isinstance(post.get("text"), str) or post.get("encoding")
            or post.get("_encoding")):
        raise checkin.SafeError("HAR 请求体缺失或带额外编码；请核对导出格式")
    raw_headers = request.get("headers")
    if not isinstance(raw_headers, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str) for item in raw_headers):
        raise checkin.SafeError("HAR 请求头格式无效")
    pairs = [(item["name"], item["value"]) for item in raw_headers]
    if not any(key.lower() == "cookie" and value for key, value in pairs):
        cookies = request.get("cookies", [])
        if not isinstance(cookies, list):
            raise checkin.SafeError("HAR Cookie 格式无效")
        values = []
        for item in cookies:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("value"), str)
                    or not item["name"] or re.search(r"[=;\r\n\x00]", item["name"])
                    or re.search(r"[;\r\n\x00]", item["value"])):
                raise checkin.SafeError("HAR Cookie 格式无效")
            values.append(item["name"] + "=" + item["value"])
        if values:
            pairs = [(key, value) for key, value in pairs if key.lower() != "cookie"]
            pairs.append(("cookie", "; ".join(values)))
    captured_at = entry.get("startedDateTime")
    return make_capture(pairs, post["text"].encode("utf-8"), request["url"],
                        captured_at if isinstance(captured_at, str) else None)


def describe(capture: Capture) -> dict[str, Any]:
    try:
        base64.b64decode(capture.request.body, validate=True)
        body_format = "base64_text"
    except ValueError:
        body_format = "opaque_text"
    return {
        "endpoint": capture.request.url,
        "body_bytes": len(capture.request.body),
        "body_format": body_format,
        "body_date": "unknown",
        "body_date_rewritten": False,
        "cookie_token_consistency": capture.cookie_token_consistency,
        "captured_date": capture.captured_date,
    }


def query_state(capture: Capture, transport: checkin.HttpTransport) -> dict[str, Any]:
    response = transport.post(capture.request.url, capture.request.headers, capture.request.body)
    # Re-evaluate the clock for each response, including a post-query across midnight.
    today = checkin.current_date()
    state = checkin.calendar_state(response, today)
    return {"date": today.isoformat(), "signed": state.signed, "continuous_days": state.days}


def execute(mode: str, capture: Capture, query: Capture | None = None,
            transport: checkin.HttpTransport | None = None) -> tuple[dict[str, Any], int]:
    if mode not in {"inspect", "query", "submit"}:
        raise checkin.SafeError("运行模式无效")
    expected_url = checkin.QUERY_URL if mode == "query" else checkin.SIGNIN_URL
    valid_url = supported_url(capture.request.url, None if mode == "inspect" else expected_url)
    if not valid_url or (query and not supported_url(query.request.url, checkin.QUERY_URL)):
        raise checkin.SafeError("请求接口与运行模式不匹配")
    report: dict[str, Any] = {
        "mode": mode, "runtime_date": checkin.current_date().isoformat(),
        "request": describe(capture), "account_identity": "unverified",
        "session_check": "not_performed", "submission": {"state": "not_sent"},
        "verification": "not_performed",
    }
    if query:
        if urlsplit(capture.request.url).netloc.lower() != urlsplit(query.request.url).netloc.lower():
            raise checkin.SafeError("查询与签到请求来自不同域名；请使用同一会话的配套抓包")
        if not hmac.compare_digest(capture.request.headers["hopegoo-token"].encode(),
                                   query.request.headers["hopegoo-token"].encode()):
            raise checkin.SafeError("查询与签到请求的 Token 不一致")
        report["query_request"] = describe(query)
    if mode == "inspect":
        report["result"] = "inspected_offline"
        return report, 0
    transport = transport if transport is not None else checkin.HttpTransport()
    if mode == "query" or query:
        try:
            before = query_state(capture if mode == "query" else query, transport)
        except checkin.SafeError as error:
            report.update(result="query_failed", session_check="not_confirmed", reason=str(error))
            return report, 1
        report.update(session_check="accepted_by_query", query_before=before)
        if mode == "query":
            report["result"] = "query_ok"
            return report, 0
        if before["signed"]:
            report.update(result="already_signed", verification="confirmed_by_query")
            return report, 0

    submission: dict[str, Any] = {"state": "unconfirmed", "http_status": None,
                                  "business_status": None}
    try:
        response = transport.post(capture.request.url, capture.request.headers, capture.request.body)
        submission["http_status"] = 200
        code = response.get("status")
        if checkin.is_int(code) and 0 <= code <= 1000000:
            submission["business_status"] = code
        if submission["business_status"] == checkin.ALREADY_SIGNED_CODE:
            submission["state"] = "already"
        else:
            checkin.business_body(response)
            submission["state"] = "accepted"
    except checkin.SafeError as error:
        submission["reason"] = str(error)
        submission["http_status"] = error.http_status or submission["http_status"]
        if ((submission["http_status"] is not None and submission["http_status"] != 200)
                or (submission["business_status"] is not None and submission["business_status"] != 200)):
            submission["state"] = "rejected"
    report["submission"] = submission
    if query:
        try:
            after = query_state(query, transport)
            report["query_after"] = after
            report["verification"] = "confirmed_signed" if after["signed"] else "not_signed"
        except checkin.SafeError as error:
            report.update(verification="query_failed", verification_reason=str(error))
    if submission["state"] == "already":
        if query:
            verified = report["verification"] == "confirmed_signed"
            report["result"] = "already_signed" if verified else "already_verification_failed"
            return report, int(not verified)
        report["result"] = "already_reported_unverified"
        return report, 0
    if submission["state"] != "accepted":
        report["result"] = "submission_" + submission["state"]
        return report, 1
    if query:
        verified = report["verification"] == "confirmed_signed"
        report["result"] = "verified_signed" if verified else "accepted_verification_failed"
        return report, int(not verified)
    report["result"] = "accepted_unverified"
    return report, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="手动检查或重放本人请求；默认离线检查，不发送通知。")
    parser.add_argument("mode", nargs="?", default="inspect", choices=("inspect", "query", "submit"))
    parser.add_argument("--kind", choices=("query", "signin"), help="仅离线检查时选择接口，默认 signin")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--har", type=Path, help="所选接口的一条 HAR 请求")
    source.add_argument("--headers-file", type=Path, help="每行一个请求头，可包含 HTTP/2 伪请求头")
    parser.add_argument("--body-file", type=Path, help="与请求头配套的原始请求体文件")
    query_source = parser.add_mutually_exclusive_group()
    query_source.add_argument("--query-har", type=Path)
    query_source.add_argument("--query-headers-file", type=Path)
    parser.add_argument("--query-body-file", type=Path)
    args = parser.parse_args(argv)
    if args.mode != "inspect" and args.kind:
        parser.error("--kind 仅用于 inspect；query 和 submit 已确定接口")
    if bool(args.headers_file) != bool(args.body_file):
        parser.error("--headers-file 与 --body-file 必须成对使用")
    if bool(args.query_headers_file) != bool(args.query_body_file):
        parser.error("--query-headers-file 与 --query-body-file 必须成对使用")
    if args.mode == "query" and (args.query_har or args.query_headers_file):
        parser.error("query 模式直接使用 --har 或 --headers-file/--body-file 指定查询请求")
    try:
        url = checkin.QUERY_URL if args.mode == "query" or args.kind == "query" else checkin.SIGNIN_URL
        capture = load_har(args.har, url) if args.har else load_headers(args.headers_file, args.body_file, url)
        query = None
        if args.query_har:
            query = load_har(args.query_har, checkin.QUERY_URL)
        elif args.query_headers_file:
            query = load_headers(args.query_headers_file, args.query_body_file, checkin.QUERY_URL)
        report, code = execute(args.mode, capture, query)
    except checkin.SafeError as error:
        report, code = {"result": "input_error", "reason": str(error)}, 1
    except Exception:
        # Never expose raw service responses, capture contents, or exception values.
        report, code = {"result": "unexpected_error", "reason": "执行异常；未确认的提交请勿直接重试"}, 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
