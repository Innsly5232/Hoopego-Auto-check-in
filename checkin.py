#!/usr/bin/env python3
"""Replay user-owned Hopegoo captures; keep all credential values out of reports."""

from __future__ import annotations

import argparse
import gzip
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import zlib


SHANGHAI = ZoneInfo("Asia/Shanghai")
QUERY_URL = "https://app.hopegoo.com/sea_shell/manta-you/memberCheckin/listCheckin"
SIGNIN_URL = "https://app.hopegoo.com/sea_shell/manta-you/memberCheckin/performCheckin"
ALLOWED_HOSTS = {"app.hopegoo.com", "app.reshg.com"}
ALLOWED_PATHS = {urlsplit(QUERY_URL).path, urlsplit(SIGNIN_URL).path}
ALREADY_SIGNED_CODE = 80002
PUSH_URL = "https://www.pushplus.plus/send"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 48 * 1024
DROP_HEADERS = {
    "host", "content-length", "accept-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
}


class SafeError(Exception):
    """Only messages created by this program, never raw service responses."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def current_date() -> date:
    return datetime.now(SHANGHAI).date()


def supported_url(url: str, expected_url: str | None = None) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc.lower() in ALLOWED_HOSTS
                and parsed.path in ALLOWED_PATHS and not parsed.query and not parsed.fragment
                and (expected_url is None or parsed.path == urlsplit(expected_url).path))
    except ValueError:
        return False


def cookie_consistency(headers: Mapping[str, str]) -> str:
    token = headers["hopegoo-token"].encode("utf-8")
    values = []
    for item in headers.get("cookie", "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name == "sectoken":
            values.append(value)
        elif separator and name == "cnUser" and value.startswith("sectoken="):
            values.append(value[len("sectoken="):])
    if any(not hmac.compare_digest(token, value.encode("utf-8")) for value in values):
        raise SafeError("Token 与 Cookie 中的 sectoken 不一致")
    return "matched" if values else "not_available"


@dataclass(frozen=True)
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: bytes

    @classmethod
    def from_config(cls, config: Any, expected_url: str) -> CapturedRequest:
        if (not isinstance(config, dict) or not supported_url(config.get("url"), expected_url)
                or config.get("method") != "POST"):
            raise SafeError("请求配置不匹配")
        raw_headers = config.get("headers")
        body = config.get("body")
        if not isinstance(raw_headers, dict) or not isinstance(body, str) or not body:
            raise SafeError("请求配置不完整")
        headers: dict[str, str] = {}
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SafeError("请求头格式无效")
            key = key.lower()
            if key.startswith(":"):
                continue
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", key) or re.search(r"[\r\n\x00]", value):
                raise SafeError("请求头格式无效")
            if key in DROP_HEADERS:
                continue
            if key in headers:
                raise SafeError("请求头重复")
            headers[key] = value
        if not headers.get("hopegoo-token") or not headers.get("hopegoo-encryptkey"):
            raise SafeError("登录或加密配置缺失")
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise SafeError("请求体编码需重新核对")
        cookie_consistency(headers)
        return cls(config["url"], headers, body.encode("utf-8"))


@dataclass(frozen=True)
class Account:
    label: str
    query: CapturedRequest
    signin: CapturedRequest

    @classmethod
    def from_secret(cls, raw: str, slot: int) -> Account:
        if len(raw.encode("utf-8")) > MAX_SECRET_BYTES:
            raise SafeError("账号配置超过大小限制")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise SafeError("账号配置不是有效 JSON") from None
        if not isinstance(value, dict) or not is_int(value.get("schema_version")) or value["schema_version"] != 1:
            raise SafeError("账号配置版本不支持")
        query = CapturedRequest.from_config(value.get("query"), QUERY_URL)
        signin = CapturedRequest.from_config(value.get("signin"), SIGNIN_URL)
        if urlsplit(query.url).netloc.lower() != urlsplit(signin.url).netloc.lower():
            raise SafeError("查询与签到请求来自不同域名")
        if not hmac.compare_digest(query.headers["hopegoo-token"].encode(), signin.headers["hopegoo-token"].encode()):
            raise SafeError("两个请求的登录会话不一致")
        return cls(f"{slot:02d}", query, signin)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """TLS verification stays enabled; no redirects or automatic POST retries."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
        )

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
        actual_headers = dict(headers)
        actual_headers["accept-encoding"] = "gzip, deflate"
        request = urllib.request.Request(url, data=body, headers=actual_headers, method="POST")
        http_status = None
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                http_status = response.status
                if response.status != 200:
                    raise SafeError(f"接口返回 HTTP {response.status}", http_status=response.status)
                expected_length = response.length
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SafeError("接口响应过大", http_status=http_status)
                if expected_length is not None and len(raw) != expected_length:
                    raise SafeError("接口响应不完整", http_status=http_status)
                encoding = response.headers.get("Content-Encoding", "identity").lower()
        except urllib.error.HTTPError as error:
            reason = "登录会话失效或被拒绝" if error.code in (401, 403) else f"接口返回 HTTP {error.code}"
            error.close()
            raise SafeError(reason, http_status=error.code) from None
        except (TimeoutError, socket.timeout):
            raise SafeError("请求超时", http_status=http_status) from None
        except urllib.error.URLError as error:
            reason = "请求超时" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "网络连接失败"
            raise SafeError(reason, http_status=http_status) from None
        except http.client.HTTPException:
            raise SafeError("接口响应不完整", http_status=http_status) from None
        except (OSError, ValueError):
            raise SafeError("网络请求失败", http_status=http_status) from None
        try:
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    raw = stream.read(MAX_RESPONSE_BYTES + 1)
            elif encoding == "deflate":
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(raw, MAX_RESPONSE_BYTES + 1)
                if not decompressor.eof and len(raw) <= MAX_RESPONSE_BYTES:
                    raise SafeError("接口响应格式异常", http_status=http_status)
            elif encoding not in ("", "identity"):
                raise SafeError("接口响应编码不支持", http_status=http_status)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SafeError("接口响应过大", http_status=http_status)
            value = json.loads(raw)
        except (EOFError, ValueError, OSError, zlib.error):
            raise SafeError("接口响应格式异常", http_status=http_status) from None
        if not isinstance(value, dict):
            raise SafeError("接口响应格式异常", http_status=http_status)
        return value


def business_body(value: dict[str, Any]) -> dict[str, Any]:
    if not is_int(value.get("status")) or value["status"] != 200:
        code = value.get("status")
        reason = f"接口返回业务 status={code}" if is_int(code) and 0 <= code <= 1000000 else "接口业务状态异常"
        raise SafeError(reason)
    if not isinstance(value.get("body"), dict):
        raise SafeError("接口业务数据缺失")
    return value["body"]


@dataclass(frozen=True)
class CalendarState:
    signed: bool
    days: int


def calendar_state(response: dict[str, Any], today: date) -> CalendarState:
    body = business_body(response)
    status, days = body.get("todayStatus"), body.get("continuousDays")
    if not is_int(status) or status not in (0, 1) or not is_int(days) or not 0 <= days <= 100000:
        raise SafeError("签到状态无法识别")
    calendar = body.get("checkinInfoList")
    if not isinstance(calendar, list):
        raise SafeError("签到日历缺失")
    entries = [item for item in calendar if isinstance(item, dict) and is_int(item.get("today")) and item["today"] == 1]
    if len(entries) != 1:
        raise SafeError("当天日历无法识别")
    item = entries[0]
    if item.get("dateText2") != today.isoformat():
        raise SafeError("返回日期不是今天")
    if not is_int(item.get("status")) or item["status"] != status:
        raise SafeError("当天签到状态不一致")
    return CalendarState(status == 1, days)


@dataclass
class Result:
    label: str
    status: str
    days: int | None = None
    coins: int | None = None
    reason: str = ""
    submission_status: str = "not_sent"
    submission_http: int | None = None
    submission_code: int | None = None
    submission_reason: str = ""


def query_account(account: Account, transport: HttpTransport, today: date) -> CalendarState:
    request = account.query
    return calendar_state(transport.post(request.url, request.headers, request.body), today)


def check_account(account: Account, transport: HttpTransport, mode: str, today: date,
                  skip_signed: bool = False) -> Result:
    try:
        before = query_account(account, transport, today)
    except SafeError as error:
        return Result(account.label, "failed", reason=str(error))
    if before.signed and (mode == "query" or skip_signed):
        return Result(account.label, "already", days=before.days)
    if mode == "query":
        return Result(account.label, "query_only", days=before.days)

    coins = None
    submission_error = None
    submission_http = None
    submission_code = None
    submission_status = "unknown"
    try:
        request = account.signin
        response = transport.post(request.url, request.headers, request.body)
        submission_http = 200
        code = response.get("status")
        if is_int(code) and 0 <= code <= 1000000:
            submission_code = code
        if submission_code == ALREADY_SIGNED_CODE:
            # Confirmed by the user's live duplicate-submission validation.
            # This does not imply a new reward or replace the post-query.
            submission_status = "already"
        else:
            body = business_body(response)
            reward = body.get("coins")
            if is_int(reward) and 0 <= reward <= 1000000:
                coins = reward
            submission_status = "accepted"
    except SafeError as error:
        submission_error = str(error)
        submission_http = error.http_status or submission_http
        if (submission_http is not None and submission_http != 200) or (submission_code is not None and submission_code != 200):
            submission_status = "rejected"
    submission = {
        "submission_status": submission_status,
        "submission_http": submission_http,
        "submission_code": submission_code,
        "submission_reason": submission_error or "",
    }

    # Exactly one submission. Even a timeout may mean the server processed it.
    try:
        after = query_account(account, transport, today)
    except SafeError as error:
        return Result(account.label, "uncertain", reason="提交后查询未通过，" + str(error), **submission)
    if after.signed:
        if submission_status == "already":
            return Result(account.label, "already", days=after.days,
                          reason="接口返回已签到", **submission)
        if submission_status == "rejected":
            return Result(account.label, "failed", days=after.days, reason="当天已签到，提交接口返回失败，" + submission_error, **submission)
        if submission_status == "unknown":
            return Result(account.label, "uncertain", days=after.days, reason="当天已签到，提交响应未确认，" + submission_error, **submission)
        if before.signed:
            # A replay of an already signed day does not prove a new reward.
            return Result(account.label, "already", days=after.days, reason="重复提交已接受", **submission)
        return Result(account.label, "success", days=after.days, coins=coins, **submission)
    return Result(account.label, "failed", days=after.days, reason=submission_error or "提交后仍未签到", **submission)


def run_accounts(environment: Mapping[str, str], transport: HttpTransport, mode: str, today: date,
                 skip_signed: bool = False) -> list[Result]:
    results = []
    for slot in range(1, 11):
        raw = environment.get(f"HOPEGOO_ACCOUNT_{slot:02d}", "")
        if not raw:
            continue
        try:
            account = Account.from_secret(raw, slot)
            results.append(check_account(account, transport, mode, today, skip_signed))
        except SafeError as error:
            results.append(Result(f"{slot:02d}", "failed", reason=str(error)))
        except Exception:
            results.append(Result(f"{slot:02d}", "failed", reason="脚本执行异常，请查看运行记录"))
    return results or [Result("01", "failed", reason="账号配置缺失，请填写 GitHub Secret")]


def format_message(results: list[Result], today: date) -> tuple[str, str]:
    counts = {key: sum(item.status == key for item in results) for key in ("success", "already", "query_only")}
    failed = sum(item.status in ("failed", "uncertain") for item in results)
    first = f"成功{counts['success']}｜已签{counts['already']}｜失败{failed}"
    if counts["query_only"]:
        first += f"｜待签{counts['query_only']}"
    lines = [first]
    for item in results:
        days = "未知" if item.days is None else f"{item.days}天"
        if item.status == "success":
            reward = "奖励未知" if item.coins is None else f"+{item.coins} G-coins"
            text = f"成功，{reward}，连续{days}"
        elif item.status == "already":
            text = f"今日已签到，连续{days}"
            if item.reason:
                text += "，" + item.reason
        elif item.status == "query_only":
            text = f"尚未签到（仅查询），连续{days}"
        else:
            text = f"失败，{item.reason or '结果未知'}"
        lines.append(f"{item.label}：{text}")
    return f"Hopegoo签到 {today:%m/%d}", "\n".join(lines)


def github_run_identity() -> str | None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        return None
    return run_id + ":" + attempt


def write_report(path: Path, results: list[Result], today: date, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    data = {"schema_version": 1, "date": today.isoformat(), "mode": mode, "results": [asdict(item) for item in results]}
    run_identity = github_run_identity()
    if run_identity is not None:
        data["github_run"] = run_identity
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(path)


def load_report(path: Path, today: date) -> tuple[date, list[Result]]:
    try:
        if path.stat().st_size > 32 * 1024:
            raise ValueError()
        report = json.loads(path.read_text(encoding="utf-8"))
        report_date = date.fromisoformat(report["date"])
        if report.get("schema_version") != 1:
            raise ValueError()
        run_identity = github_run_identity()
        if run_identity is not None and report.get("github_run") != run_identity:
            raise ValueError()
        # A job that starts before midnight may notify just after midnight.
        # Only the same GitHub run and attempt may use yesterday's report.
        if report_date != today and (run_identity is None or report_date != today - timedelta(days=1)):
            raise ValueError()
        entries = report["results"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
            raise ValueError()
        results = []
        for item in entries:
            result = Result(**item)
            if not re.fullmatch(r"0[1-9]|10", result.label) or result.status not in ("success", "already", "query_only", "failed", "uncertain"):
                raise ValueError()
            if result.days is not None and (not is_int(result.days) or not 0 <= result.days <= 100000):
                raise ValueError()
            if result.coins is not None and (not is_int(result.coins) or not 0 <= result.coins <= 1000000):
                raise ValueError()
            if not isinstance(result.reason, str) or len(result.reason) > 100:
                raise ValueError()
            if result.submission_status not in ("not_sent", "accepted", "already", "rejected", "unknown"):
                raise ValueError()
            if result.submission_http is not None and (not is_int(result.submission_http) or not 100 <= result.submission_http <= 599):
                raise ValueError()
            if result.submission_code is not None and (not is_int(result.submission_code) or not 0 <= result.submission_code <= 1000000):
                raise ValueError()
            if not isinstance(result.submission_reason, str) or len(result.submission_reason) > 100:
                raise ValueError()
            results.append(result)
        return report_date, results
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return today, [Result("01", "failed", reason="脚本未完成，请查看运行记录")]


def read_report(path: Path, today: date) -> list[Result]:
    return load_report(path, today)[1]


def push_message(environment: Mapping[str, str], transport: HttpTransport, title: str, content: str) -> None:
    token = environment.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        raise SafeError("PUSHPLUS_TOKEN 未配置，无法提交短信")
    payload = {"token": token, "title": title, "content": content, "template": "txt", "channel": "cmcc"}
    option = environment.get("PUSHPLUS_OPTION", "").strip()
    if option:
        payload["option"] = option
    response = transport.post(PUSH_URL, {"content-type": "application/json; charset=utf-8"}, json.dumps(payload, ensure_ascii=False).encode())
    if not is_int(response.get("code")) or response["code"] != 200:
        raise SafeError("PushPlus 未接受通知，请检查 Token、渠道及额度")


def append_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
        except OSError:
            print("运行摘要写入失败；详细结果仍见步骤日志。", file=sys.stderr)


def summarize_results(results: list[Result], today: date) -> None:
    title, content = format_message(results, today)
    lines = [f"### {title}", "", "```text", content, "```", "",
             "| 账号 | 本次签到提交 | HTTP | 业务 status |",
             "| --- | --- | --- | --- |"]
    labels = {"not_sent": "未提交", "accepted": "接口已接受", "already": "接口返回已签到", "rejected": "接口返回失败", "unknown": "响应未确认"}
    for item in results:
        http = "未知" if item.submission_http is None else str(item.submission_http)
        code = "未知" if item.submission_code is None else str(item.submission_code)
        lines.append(f"| {item.label} | {labels[item.submission_status]} | {http} | {code} |")
    append_step_summary("\n".join(lines))


def mask_credentials(environment: Mapping[str, str]) -> None:
    if environment.get("GITHUB_ACTIONS") != "true":
        return
    masks = set()
    for name in ["PUSHPLUS_TOKEN"] + [f"HOPEGOO_ACCOUNT_{slot:02d}" for slot in range(1, 11)]:
        raw = environment.get(name, "")
        if not raw:
            continue
        masks.add(raw)
        if name.startswith("HOPEGOO_ACCOUNT_"):
            try:
                account = json.loads(raw)
                for field in ("query", "signin"):
                    request = account.get(field, {})
                    if isinstance(request.get("body"), str):
                        masks.add(request["body"])
                    for key, value in request.get("headers", {}).items():
                        if key.lower() in ("hopegoo-token", "hopegoo-encryptkey", "cookie") and isinstance(value, str):
                            masks.add(value)
            except (ValueError, TypeError, AttributeError):
                pass
    for value in sorted(masks):
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + escaped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("query", "checkin"), default="query")
    parser.add_argument("--skip-signed", action="store_true", help="前查确认当天已签时跳过提交")
    parser.add_argument("--report", type=Path, default=Path("hopegoo-report.json"))
    parser.add_argument("--notify", action="store_true", help="Send summary from report; create failure summary if missing")
    args = parser.parse_args(argv)
    mask_credentials(os.environ)
    today = current_date()
    transport = HttpTransport()
    if args.notify:
        report_date, results = load_report(args.report, today)
        title, content = format_message(results, report_date)
        print(title + "\n" + content)
        try:
            push_message(os.environ, transport, title, content)
        except SafeError as error:
            print(str(error), file=sys.stderr)
            append_step_summary("\n**短信提交失败：**" + str(error))
            return 1
        except Exception:
            print("通知步骤执行异常", file=sys.stderr)
            append_step_summary("\n**短信提交失败：**通知步骤执行异常")
            return 1
        print("PushPlus 已接受通知请求；最终送达状态请在 PushPlus 查询。")
        append_step_summary("\n**短信：**PushPlus 已接受 cmcc 通知请求；最终送达以手机和 PushPlus 记录为准。")
        return 0
    descriptions = {
        "query": "联网查询，不提交签到",
        "checkin": ("联网签到，前查已签则跳过，提交后查询确认" if args.skip_signed
                    else "联网签到，当天已签也提交一次，随后查询确认"),
    }
    print(f"运行模式：{args.mode}（{descriptions[args.mode]}）")
    print(f"运行日期（北京时间）：{today.isoformat()}")
    results = run_accounts(os.environ, transport, args.mode, today, args.skip_signed)
    write_report(args.report, results, today, args.mode)
    summarize_results(results, today)
    for item in results:
        if item.submission_status == "accepted":
            print(f"{item.label}：签到提交接口通过（HTTP 200，业务 status={item.submission_code}）")
        elif item.submission_status == "already":
            print(f"{item.label}：接口返回已签到（HTTP 200，业务 status={item.submission_code}）")
        elif item.submission_status in ("rejected", "unknown"):
            http = f"HTTP {item.submission_http}，" if item.submission_http is not None else ""
            detail = "签到提交接口返回失败" if item.submission_status == "rejected" else "签到提交响应未确认"
            print(f"{item.label}：{detail}（{http}{item.submission_reason}）")
        elif args.mode == "checkin":
            if item.status == "already":
                print(f"{item.label}：前查确认今日已签到，跳过提交")
            else:
                print(f"{item.label}：未提交签到请求，前置查询或账号配置未通过")
    title, content = format_message(results, today)
    print(title + "\n" + content)
    return int(any(item.status in ("failed", "uncertain") for item in results))


if __name__ == "__main__":
    sys.exit(main())
