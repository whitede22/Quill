"""使用报告：把白名单事件汇总成一份 JSON，并给出提交入口。

设计上只做三件事，全部可在自检里验证：

1. **汇总成可读的计数**，不打包原始业务对象。事件明细本身已经过
   `analytics` 的白名单过滤，报告只是把它们按格式装箱，不再引入新字段。
2. **落盘是原子的**（复用 `atomic.write_text`）。报告是拿去提交给别人的文件，
   半截 JSON 比没有更糟：用户会以为发出去的是完整数据。
3. **提交不等于发送**。本模块只负责拼出一个 GitHub 新建 Issue 的地址，正文里
   写清楚要用户自己附加文件。程序无法知道用户最后有没有点提交，所以任何界面
   文案都不许写「已提交」「已发送」。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .. import atomic, config
from ..version import APP_VERSION, REPO_URL

REPORT_SCHEMA_VERSION = 1

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

    只放汇总数字，不放明细：URL 有长度上限，而明细本来就要作为附件上传。
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
