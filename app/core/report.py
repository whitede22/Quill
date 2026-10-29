"""使用报告：把白名单事件汇总成一份 JSON，并把它送出去。

设计上只做三件事，全部可在自检里验证：

1. **汇总成可读的计数**，不打包原始业务对象。事件明细本身已经过
   `analytics` 的白名单过滤，报告只是把它们按格式装箱，不再引入新字段。
2. **落盘是原子的**（复用 `atomic.write_text`）。报告是拿去提交给别人的文件，
   半截 JSON 比没有更糟：用户会以为发出去的是完整数据。
3. **提交走中转函数**，客户端只发 HTTPS。发信凭据（邮箱授权码）住在服务端，
   不进程序：打包后的 exe 能被拆开，写进来的凭据等于公开。

`DEFAULT_KEY` 不是秘密，它只用来挡住随手调用接口的人，与发信凭据不是一回事。
真正拦住滥用的是服务端的限流与结构校验。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import atomic, config
from ..version import APP_VERSION, REPO_URL

REPORT_SCHEMA_VERSION = 1

# 部署完中转函数后把地址填在这里，见 server/report_relay/README.md。
# 留空时客户端会明确说「这个版本还没有配置提交地址」，不会静默失败。
DEFAULT_ENDPOINT = ""
DEFAULT_KEY = ""

SUBMIT_TIMEOUT = 25.0

# 明细上限。事件量小时全带上，量大时截断并在报告里写明——报告是给人看的，
# 不是数据库备份，几百 KB 的附件没人会附到 Issue 里。
MAX_EVENTS = 1000

# 报告里明确写出「有什么、没有什么」，用户在看预览时不必翻代码。
_INCLUDES = (
    "功能使用次数（查词、复习、导入、导出、启动）",
    "每次操作的耗时区间与成功与否",
    "出错时的预定义编号",
    "Quill 版本与统计数据结构版本",
)
_EXCLUDES = (
    "具体生词与释义",
    "个人笔记与自建例句",
    "剪贴板内容",
    "接口请求与响应正文",
    "API 密钥、令牌、邮箱",
    "用户目录的绝对路径",
)


def build_report(days: int = 30, *, now: datetime | None = None) -> dict:
    """生成报告内容（不落盘）。

    读的是 `analytics` 的聚合结果与明细，不再额外查库，避免两条口径。
    """
    from ..core import analytics

    summary = analytics.summarize(days)
    events = analytics.report_rows(MAX_EVENTS)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "quill_version": APP_VERSION,
        "analytics_schema_version": int(
            config.load_config().get("analytics_schema_version", 1)
        ),
        "generated_at": stamp,
        "period_days": days,
        "event_total": summary["total"],
        "event_count": len(events),
        "events_truncated": summary["total"] > len(events),
        "by_event": summary["by_event"],
        "by_day": [{"date": d, "count": n} for d, n in summary["by_day"]],
        "first_event_utc": summary["first_ts"],
        "last_event_utc": summary["last_ts"],
        "privacy": {"includes": list(_INCLUDES), "excludes": list(_EXCLUDES)},
        "events": events,
    }


def default_name(now: datetime | None = None) -> str:
    return f"quill_usage_report_{(now or datetime.now()).strftime('%Y-%m-%d')}.json"


def default_dir() -> Path:
    """报告的默认落点：数据目录下的 reports 子目录。

    不写进程序安装目录：打包后的目录可能没有写权限，而报告是用户的文件，
    和数据放一起才找得到。
    """
    return config.APP_DIR / "reports"


def write_report(payload: dict, path: Path | str) -> Path:
    """原子写入报告，返回实际路径。失败会抛 OSError，由界面如实提示。"""
    import json

    target = Path(path)
    atomic.write_text(
        target, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def summary_lines(payload: dict) -> list[str]:
    """把报告的要点压成几行字，给预览与摘要共用。"""
    total = payload.get("event_total", 0)
    lines = [
        f"Quill 版本：{payload.get('quill_version', '')}",
        f"报告生成时间：{payload.get('generated_at', '')}",
        f"统计周期：最近 {payload.get('period_days', 0)} 天",
        f"事件总数：{total}",
    ]
    by_event = payload.get("by_event") or {}
    if by_event:
        detail = "、".join(f"{name} {count}" for name, count in sorted(by_event.items()))
        lines.append(f"分类计数：{detail}")
    if payload.get("events_truncated"):
        lines.append(f"明细已截断：只保留最近 {payload.get('event_count', 0)} 条")
    return lines


def issue_url(payload: dict) -> str:
    """拼一个「新建 Issue」的地址，正文里说明要附上报告文件。

    中转函数不可用时的备用通道，只放汇总数字，明细作为附件上传。
    """
    title = f"[使用报告] v{payload.get('quill_version', '')} {payload.get('generated_at', '')}"
    body_lines = [
        "本地使用报告已生成，请把 JSON 文件拖到下面的输入框作为附件。",
        "",
        *[f"- {line}" for line in summary_lines(payload)],
        "",
        "报告文件：请用「提交使用报告」生成的那份 JSON。",
    ]
    body = "\n".join(body_lines)
    return (
        f"{REPO_URL}/issues/new?title={quote(title)}"
        f"&body={quote(body)}&labels=feedback"
    )


class SubmitError(RuntimeError):
    """提交失败。消息是给用户看的，调用方直接显示，不再改写。"""


def endpoint() -> str:
    """中转函数地址。环境变量优先，便于自检指向本地桩服务。"""
    return os.environ.get("QUILL_REPORT_ENDPOINT", DEFAULT_ENDPOINT).strip()


def report_key() -> str:
    return os.environ.get("QUILL_REPORT_KEY", DEFAULT_KEY).strip()


def submit(payload: dict, *, timeout: float = SUBMIT_TIMEOUT) -> None:
    """把报告交给中转函数。成功返回，失败抛 SubmitError。

    请求体分两段：`report` 是完整 JSON（作为附件），`summary` 是几行摘要
    （作为邮件正文）。摘要由客户端生成，服务端不重写一份，免得两边口径分叉。
    """
    url = endpoint()
    if not url:
        raise SubmitError(
            "这个版本还没有配置提交地址。报告已存到本机，可以改用 GitHub 提交。"
        )
    body = {"report": payload, "summary": summary_lines(payload)}
    headers = {"X-Report-Key": report_key(), "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise SubmitError("提交超时，网络可能不通，稍后再试。") from exc
    except httpx.HTTPError as exc:
        raise SubmitError(
            f"连不上提交服务（{type(exc).__name__}），检查网络后重试。"
        ) from exc
    _read_response(resp)


_HINTS = {
    400: "服务端没认这份报告。",
    401: "提交密钥不符，这个版本的客户端可能已经过期。",
    403: "提交密钥不符，这个版本的客户端可能已经过期。",
    405: "提交方式不对，客户端与服务端版本可能不匹配。",
    413: "报告超过服务端上限。",
    429: "提交太频繁，过十分钟再试。",
    502: "服务端发信失败，稍后再试。",
}


def _read_response(resp: httpx.Response) -> None:
    if resp.status_code == 200:
        return
    hint = _HINTS.get(resp.status_code, f"提交失败（{resp.status_code}）。")
    raise SubmitError(f"{hint}{_error_text(resp)}")


def _error_text(resp: httpx.Response) -> str:
    """取服务端给的说明。它只说「密钥不符」这类短语，不含账号信息。"""
    try:
        data = resp.json()
    except ValueError:
        return ""
    message = data.get("error") if isinstance(data, dict) else ""
    return f"服务端说明：{message}" if message else ""

