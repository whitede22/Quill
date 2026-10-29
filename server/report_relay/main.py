"""Quill 使用报告的中转函数（腾讯云函数 SCF，Python 运行时）。

客户端只发一个 HTTPS 请求，SMTP 授权码住在这里的环境变量，不进客户端。
打包好的 exe 能被拆开，任何写进客户端的凭据都等于公开。

环境变量：

    SMTP_HOST   默认 smtp.qq.com
    SMTP_PORT   默认 465（SSL）
    SMTP_USER   发件邮箱
    SMTP_PASS   邮箱授权码，不是登录密码
    MAIL_TO     收件邮箱，多个用逗号分隔
    REPORT_KEY  客户端带来的共享密钥，函数与客户端各存一份

部署方式见同目录的 README.md。
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# 报告是几万字节的 JSON，留足余量即可。上限防的是有人拿它当大文件通道。
MAX_BODY = 256 * 1024

# 同一 IP 在窗口内最多提交几次。函数实例之间不共享内存，冷启动会清零，
# 这一层只是把随手刷接口的人挡住，真正的防线是共享密钥。
RATE_LIMIT = 3
WINDOW_SECONDS = 600

_recent: dict[str, list[float]] = {}


def main_handler(event: dict, context: object) -> dict:
    if _method(event) != "POST":
        return _reply(405, {"ok": False, "error": "只接受 POST"})
    if not _authorized(event):
        return _reply(401, {"ok": False, "error": "密钥不符"})

    body = _body(event)
    if body is None:
        return _reply(413, {"ok": False, "error": "报告过大"})
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _reply(400, {"ok": False, "error": "不是合法 JSON"})

    problem = _validate(data)
    if problem:
        return _reply(400, {"ok": False, "error": problem})
    if not _rate_ok(_client_ip(event)):
        return _reply(429, {"ok": False, "error": "提交过于频繁，请稍后再试"})

    try:
        _send(_build_message(data))
    except (smtplib.SMTPException, OSError) as exc:
        # 只记类型，不回给客户端：报错文本里可能带发件账号。
        print(f"发信失败：{type(exc).__name__}")
        return _reply(502, {"ok": False, "error": "发信失败"})
    return _reply(200, {"ok": True})


def _method(event: dict) -> str:
    ctx = event.get("requestContext") or {}
    return str(event.get("httpMethod") or ctx.get("httpMethod") or "").upper()


def _authorized(event: dict) -> bool:
    expected = os.environ.get("REPORT_KEY", "")
    if not expected:
        return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return hmac.compare_digest(headers.get("x-report-key", ""), expected)


def _body(event: dict) -> str | None:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BODY:
        return None
    return raw


def _client_ip(event: dict) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    ctx = event.get("requestContext") or {}
    return str((ctx.get("identity") or {}).get("sourceIp", "unknown"))


def _rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _recent.get(ip, []) if now - t < WINDOW_SECONDS]
    if len(hits) >= RATE_LIMIT:
        _recent[ip] = hits
        return False
    hits.append(now)
    _recent[ip] = hits
    return True


def _validate(data: dict) -> str:
    """只认结构，不信任内容。缺字段就拒，避免把任意 JSON 转进邮箱。"""
    report = data.get("report")
    if not isinstance(report, dict):
        return "缺少 report"
    if not isinstance(report.get("report_schema_version"), int):
        return "缺少版本号"
    total = report.get("event_total")
    if not isinstance(total, int) or total <= 0:
        return "没有事件"
    summary = data.get("summary")
    if not isinstance(summary, list) or not all(isinstance(x, str) for x in summary):
        return "摘要格式不对"
    if len(summary) > 12 or any(len(x) > 200 for x in summary):
        return "摘要过长"
    return ""


def _build_message(data: dict) -> EmailMessage:
    report = data["report"]
    version = str(report.get("quill_version", ""))[:20]
    stamp = str(report.get("generated_at", ""))[:20]
    msg = EmailMessage()
    msg["Subject"] = f"[Quill 使用报告] v{version} {stamp}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = ", ".join(_recipients())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="quill.local")
    msg.set_content(_body_text(data))

    name = f"quill_usage_report_{stamp[:10] or 'unknown'}.json"
    msg.add_attachment(
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        maintype="application",
        subtype="json",
        filename=name,
    )
    return msg


def _body_text(data: dict) -> str:
    lines = ["这是 Quill 提交的一份本地使用报告，完整内容在附件里。", ""]
    lines.extend(f"- {line}" for line in data["summary"])
    return "\n".join(lines) + "\n"


def _recipients() -> list[str]:
    raw = os.environ.get("MAIL_TO", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _send(message: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=20) as client:
        client.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        client.send_message(message)


def _reply(status: int, payload: dict) -> dict:
    return {
        "isBase64Encoded": False,
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }
