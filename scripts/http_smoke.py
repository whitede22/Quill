"""OpenAI 兼容适配器的端到端验证。

启动一个本地假 HTTP 服务模拟网关，覆盖真实网络路径：
请求构造、鉴权头、响应解析、Markdown 围栏容错、错误处理与重试策略。

用法：.venv/Scripts/python.exe scripts/http_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import report  # noqa: E402
from app.providers.base import NonRetryableError, ProviderError  # noqa: E402
from app.providers.openai_compat import (  # noqa: E402
    OpenAICompatProvider,
    describe_network_error,
    extract_error_message,
)

import httpx  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

GOOD_PAYLOAD = {
    "lemma": "run",
    "phonetic": "/rʌn/",
    "pos": "v.",
    "definition_en": "to move quickly on foot",
    "definition_zh": "跑；奔跑",
    "example": "She runs every morning before work.",
    "example_zh": "她每天上班前都去跑步。",
    "synonyms": [{"word": "sprint", "gloss_zh": "冲刺", "diff_note": "强调短距离全速"}],
}

# OpenAI 风格的真实错误信封：message 里才是人话，外面那层是机器结构
BAD_MODEL_BODY = json.dumps(
    {
        "error": {
            "message": "The model `gpt-4o-minii` does not exist",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
).encode("utf-8")
RATE_LIMIT_BODY = json.dumps(
    {
        "error": {
            "message": "Rate limit reached for gpt-4o-mini on requests per min. "
            "Limit: 500, Used: 500.",
            "type": "requests",
        }
    }
).encode("utf-8")
FORBIDDEN_BODY = json.dumps(
    {"error": {"message": "Country, region, or territory not supported"}}
).encode("utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  {detail}")


def capture(provider: OpenAICompatProvider, word: str = "run") -> Exception | None:
    """执行查询并返回异常对象，便于对异常类型与文案做平坦断言。"""
    try:
        provider.lookup(word)
        return None
    except Exception as exc:  # noqa: BLE001 - 测试需要拿到任何异常
        return exc


class Handler(BaseHTTPRequestHandler):
    """由 Handler.mode 控制返回行为。"""

    mode = "ok"
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - 基类要求的命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}

        Handler.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "payload": body,
            }
        )

        if Handler.mode == "unauthorized":
            self._respond(401, b'{"error":{"message":"invalid key"}}')
            return
        if Handler.mode == "server_error":
            self._respond(500, b'{"error":{"message":"internal"}}')
            return
        if Handler.mode == "bad_request":
            self._respond(400, BAD_MODEL_BODY)
            return
        if Handler.mode == "rate_limited":
            self._respond(429, RATE_LIMIT_BODY)
            return
        if Handler.mode == "forbidden":
            self._respond(403, FORBIDDEN_BODY)
            return
        if Handler.mode == "html_error":
            # 网关/反代出错时可能返回 HTML 而不是 JSON 错误信封
            self._respond(502, b"<html><body>502 Bad Gateway</body></html>")
            return
        if Handler.mode == "slow":
            time.sleep(3.0)
            try:
                self._respond(200, json.dumps(GOOD_PAYLOAD, ensure_ascii=False).encode())
            except OSError:
                pass  # 客户端已超时断开，写入失败属预期
            return
        if Handler.mode == "empty":
            content = ""
        elif Handler.mode == "fenced":
            content = "```json\n" + json.dumps(GOOD_PAYLOAD, ensure_ascii=False) + "\n```"
        elif Handler.mode == "chatty":
            content = (
                "好的，这是词条：\n"
                + json.dumps(GOOD_PAYLOAD, ensure_ascii=False)
                + "\n希望对你有帮助。"
            )
        else:
            content = json.dumps(GOOD_PAYLOAD, ensure_ascii=False)

        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self._respond(200, body)

    def _respond(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静默
        pass


class RelayHandler(BaseHTTPRequestHandler):
    """模拟报告中转函数。`mode` 决定返回哪个状态码，与真函数共用同一套文案。"""

    mode = "ok"
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - 基类要求的命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        RelayHandler.requests.append(
            {
                "key": self.headers.get("X-Report-Key"),
                "content_type": self.headers.get("Content-Type"),
                "payload": body,
            }
        )
        status, error = RELAY_REPLIES.get(RelayHandler.mode, (200, ""))
        payload = {"ok": status == 200}
        if error:
            payload["error"] = error
        self._respond(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


RELAY_REPLIES = {
    "ok": (200, ""),
    "unauthorized": (401, "密钥不符"),
    "too_many": (429, "提交过于频繁，请稍后再试"),
    "send_failed": (502, "发信失败"),
}

SAMPLE_REPORT = {
    "report_schema_version": 1,
    "quill_version": "9.9.9",
    "generated_at": "2026-10-29 17:00",
    "period_days": 30,
    "event_total": 12,
    "by_event": {"lookup": 12},
}


def check_report_submit(port: int) -> None:
    """报告提交走真实 HTTP：请求体、密钥头、四类失败文案。

    走本地桩而不是打真的中转函数：真函数会发信，测试不该产生邮件。
    路径、头部与状态码翻译都是真的。
    """
    os.environ["QUILL_REPORT_ENDPOINT"] = f"http://127.0.0.1:{port}/report"
    os.environ["QUILL_REPORT_KEY"] = "smoke-key"
    try:
        RelayHandler.mode = "ok"
        RelayHandler.requests.clear()
        report.submit(SAMPLE_REPORT, timeout=5.0)
        check("报告提交成功不抛错", len(RelayHandler.requests) == 1)

        sent = RelayHandler.requests[0]
        check("提交带密钥头", sent["key"] == "smoke-key", str(sent["key"]))
        check("提交带 JSON 类型", sent["content_type"] == "application/json")
        body = sent["payload"]
        check("提交体含完整报告", body.get("report", {}).get("event_total") == 12)
        check("提交体含摘要行", isinstance(body.get("summary"), list) and bool(body["summary"]))
        check(
            "摘要与预览同源",
            body.get("summary") == report.summary_lines(SAMPLE_REPORT),
        )

        for mode, expect in (
            ("unauthorized", "密钥"),
            ("too_many", "频繁"),
            ("send_failed", "发信"),
        ):
            RelayHandler.mode = mode
            try:
                report.submit(SAMPLE_REPORT, timeout=5.0)
                check(f"{mode} 应抛错", False, "未抛出")
            except report.SubmitError as exc:
                check(f"{mode} 文案指向原因", expect in str(exc), str(exc))

        os.environ.pop("QUILL_REPORT_ENDPOINT")
        try:
            report.submit(SAMPLE_REPORT, timeout=5.0)
            check("未配置地址应抛错", False, "未抛出")
        except report.SubmitError as exc:
            check("未配置地址给出说明", "配置" in str(exc), str(exc))

        os.environ["QUILL_REPORT_ENDPOINT"] = "http://127.0.0.1:9/report"
        try:
            report.submit(SAMPLE_REPORT, timeout=1.5)
            check("端口不通应抛错", False, "未抛出")
        except report.SubmitError as exc:
            message = str(exc)
            check("端口不通给出可读中文", ("连不上" in message) or ("超时" in message), message)
            check("端口不通不泄漏内部异常", "Errno" not in message, message)
    finally:
        os.environ.pop("QUILL_REPORT_ENDPOINT", None)
        os.environ.pop("QUILL_REPORT_KEY", None)


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    relay = HTTPServer(("127.0.0.1", 0), RelayHandler)
    relay_port = relay.server_address[1]
    threading.Thread(target=relay.serve_forever, daemon=True).start()

    base_url = f"http://127.0.0.1:{port}/v1"
    provider = OpenAICompatProvider(base_url, "sk-smoke-test", "test-model", timeout=5.0)

    # ---- 1. 正常路径 ----
    Handler.mode = "ok"
    Handler.requests.clear()
    entry = provider.lookup("run")
    check("解析词条原形", entry.lemma == "run", entry.lemma)
    check("解析音标", entry.phonetic == "/rʌn/")
    check("解析词性", entry.pos == "v.")
    check("解析英文释义", entry.definition_en == "to move quickly on foot")
    check("解析中文释义", entry.definition_zh == "跑；奔跑")
    check("解析例句", entry.example.startswith("She runs"))
    check("解析同义语", len(entry.synonyms) == 1 and entry.synonyms[0].word == "sprint")
    check("解析同义语差异说明", entry.synonyms[0].diff_note == "强调短距离全速")

    req = Handler.requests[0]
    check("请求路径正确", req["path"] == "/v1/chat/completions", req["path"])
    check("携带鉴权头", req["auth"] == "Bearer sk-smoke-test", str(req["auth"]))
    check("Content-Type 为 JSON", req["content_type"] == "application/json")
    check("请求体含模型名", req["payload"].get("model") == "test-model")
    check("请求体关闭流式", req["payload"].get("stream") is False)
    messages = req["payload"].get("messages") or []
    check("请求体含 system 与 user", len(messages) == 2, str(len(messages)))
    check(
        "user 消息包含目标词",
        any("run" in str(m.get("content", "")) for m in messages if m.get("role") == "user"),
    )

    # ---- 2. Markdown 围栏容错 ----
    Handler.mode = "fenced"
    entry = provider.lookup("run")
    check("容忍 Markdown 围栏", entry.definition_en == "to move quickly on foot")

    # ---- 3. 夹带前后文容错 ----
    Handler.mode = "chatty"
    entry = provider.lookup("run")
    check("容忍夹带说明文字", entry.lemma == "run")

    # ---- 4. 鉴权失败不重试 ----
    Handler.mode = "unauthorized"
    Handler.requests.clear()
    try:
        provider.lookup("run")
        check("401 应抛错", False, "未抛出异常")
    except NonRetryableError as exc:
        check("401 抛出不可重试错误", True)
        check("401 提示鉴权", "鉴权" in str(exc), str(exc))
    except ProviderError as exc:
        check("401 抛出不可重试错误", False, f"错误类型不对：{type(exc).__name__}")
    check("401 只请求一次", len(Handler.requests) == 1, f"实际 {len(Handler.requests)} 次")

    # ---- 5. 服务端错误会重试 ----
    Handler.mode = "server_error"
    Handler.requests.clear()
    try:
        provider.lookup("run")
        check("500 应抛错", False, "未抛出异常")
    except ProviderError as exc:
        check("500 抛出 ProviderError", isinstance(exc, ProviderError))
        check("500 保留状态码信息", "500" in str(exc), str(exc))
    check("500 重试两次", len(Handler.requests) == 2, f"实际 {len(Handler.requests)} 次")

    # ---- 6. 空响应 ----
    Handler.mode = "empty"
    try:
        provider.lookup("run")
        check("空响应应抛错", False, "未抛出异常")
    except ProviderError:
        check("空响应抛出 ProviderError", True)

    # ---- 7. 地址未配置 ----
    try:
        OpenAICompatProvider("", "k", "m").lookup("run")
        check("空地址应抛错", False, "未抛出异常")
    except ProviderError as exc:
        check("空地址抛出 ProviderError", "地址" in str(exc), str(exc))

    # ---- 8. 确定性 4xx：不该重试，且文案要能指导用户 ----
    # 文档写明「网络错误与 5xx 重试；确定性失败立即抛出」，但实现对除 401/404 外的
    # 所有 >=400 一律重试——400（模型名错误）这种再试一百次也一样失败。
    Handler.mode = "bad_request"
    Handler.requests.clear()
    exc = capture(provider)
    check("400 抛出不可重试错误", isinstance(exc, NonRetryableError), type(exc).__name__)
    check("400 只请求一次", len(Handler.requests) == 1, f"实际 {len(Handler.requests)} 次")
    msg = str(exc)
    check("400 提示检查模型名", "模型" in msg, msg)
    check("400 带出服务端说明", "gpt-4o-minii" in msg, msg)
    check("400 不直接倾倒 JSON 信封", '{"error"' not in msg, msg)

    Handler.mode = "forbidden"
    Handler.requests.clear()
    exc = capture(provider)
    check("403 抛出不可重试错误", isinstance(exc, NonRetryableError), type(exc).__name__)
    check("403 只请求一次", len(Handler.requests) == 1, f"实际 {len(Handler.requests)} 次")
    check("403 带出服务端说明", "not supported" in str(exc), str(exc))

    # ---- 9. 限流 429：属暂时性，应重试，但文案要可读 ----
    Handler.mode = "rate_limited"
    Handler.requests.clear()
    exc = capture(provider)
    check("429 抛出 ProviderError", isinstance(exc, ProviderError), type(exc).__name__)
    check("429 会重试", len(Handler.requests) == 2, f"实际 {len(Handler.requests)} 次")
    msg = str(exc)
    check("429 提示过于频繁", "频繁" in msg, msg)
    check("429 提示稍后再试", "稍后" in msg, msg)

    # ---- 10. 非 JSON 错误体：取不到 message 时退回可读的通用说明 ----
    Handler.mode = "html_error"
    Handler.requests.clear()
    exc = capture(provider)
    check("HTML 错误体抛出 ProviderError", isinstance(exc, ProviderError))
    msg = str(exc)
    check("HTML 错误体提示服务端异常", "服务端" in msg or "网关" in msg, msg)
    check("HTML 错误体不倾倒 HTML 标签", "<html>" not in msg, msg)

    # ---- 11. 超时：要说明是超时，而不是抛原始英文异常 ----
    slow = OpenAICompatProvider(base_url, "sk-smoke-test", "test-model", timeout=0.6)
    Handler.mode = "slow"
    Handler.requests.clear()
    exc = capture(slow)
    check("超时抛出 ProviderError", isinstance(exc, ProviderError), type(exc).__name__)
    msg = str(exc)
    check("超时提示超时", "超时" in msg, msg)
    check("超时信息不含内部异常细节", "ReadTimeout" not in msg and "_ssl" not in msg, msg)

    # ---- 12. 网络异常翻译：逐一喂合成异常，避免依赖网络环境的不确定性 ----
    check(
        "连接错误文案可读",
        "无法连接" in describe_network_error(httpx.ConnectError("boom"), 5.0),
        describe_network_error(httpx.ConnectError("boom"), 5.0),
    )
    check(
        "超时错误文案可读",
        "超时" in describe_network_error(httpx.ReadTimeout("timed out"), 5.0),
    )
    check(
        "其他网络错误文案可读",
        "网络" in describe_network_error(httpx.HTTPError("weird"), 5.0),
    )

    # ---- 13. 错误体解析：标准信封 / 非 JSON / 空体 ----
    check(
        "取出标准错误信封的 message",
        extract_error_message(BAD_MODEL_BODY.decode()) == "The model `gpt-4o-minii` does not exist",
    )
    check(
        "剥离 HTML 标签",
        extract_error_message("<html><body>502 Bad Gateway</body></html>") == "502 Bad Gateway",
        extract_error_message("<html><body>502 Bad Gateway</body></html>"),
    )
    check("空错误体返回空串", extract_error_message("") == "")
    check("无法识别的结构返回空串", extract_error_message('{"foo": 1}') == "")
    long_msg = extract_error_message(json.dumps({"error": {"message": "x" * 500}}))
    check("超长 message 被截断", len(long_msg) <= 161, str(len(long_msg)))

    # ---- 14. 真实网络不可达：文案仍须是可读中文 ----
    Handler.mode = "ok"
    dead = OpenAICompatProvider(
        "http://127.0.0.1:9/v1", "sk-x", "m", timeout=2.0, max_retries=1
    )
    exc = capture(dead)
    check("不可达地址抛出 ProviderError", isinstance(exc, ProviderError), type(exc).__name__)
    msg = str(exc)
    check("不可达地址给出可读中文", ("超时" in msg) or ("无法连接" in msg) or ("网络" in msg), msg)
    check(
        "不可达地址不泄漏内部异常",
        not any(k in msg for k in ("Traceback", "WinError", "_ssl", "Errno")),
        msg,
    )

    # ---- 15. 使用报告提交：请求体、密钥头、失败文案 ----
    check_report_submit(relay_port)

    server.shutdown()
    relay.shutdown()

    print("=" * 56)
    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print("=" * 56)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
