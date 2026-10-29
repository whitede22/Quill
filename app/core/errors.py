"""把异常归到一个有限的类别里，供统计记录。

统计只记类别，不记异常原文——原文里常带绝对路径、用户输入的词、接口返回的
片段。归类只在这里做一次：界面层各写一套判断的话，同一个失败会在两处落到
不同类别，报告里就分不清「接口变了」和「归类松了」。

类别集合与 `analytics._ENUMS["error_code"]` 是同一份口径，加减要两边一起改，
`selfcheck` 有一条断言把两边钉住。

顺序即判据：httpx 的超时是传输异常的子类，鉴权错误是状态码错误的子类。
先判宽的那个会把细的吃掉，结果是所有失败都变成同一个类别，而报告看起来
一切正常。
"""

from __future__ import annotations

import json
import sqlite3

# 与 analytics 的错误类别枚举逐项对应。放在这里是因为「哪些类别存在」由
# 本模块决定，analytics 只负责校验值是否在集合里。
CLASSES: tuple[str, ...] = (
    "timeout",
    "network",
    "auth",
    "rate_limit",
    "http_4xx",
    "http_5xx",
    "bad_response",
    "parse",
    "disk",
    "db",
    "unknown",
)


def classify(exc: BaseException | None) -> str:
    """返回 `CLASSES` 里的一个类别。认不出来的落 `unknown`，不猜。"""
    if exc is None:
        return "unknown"

    if isinstance(exc, sqlite3.Error):
        return "db"

    httpx = _httpx()
    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return _from_status(exc.response.status_code)
        if isinstance(exc, httpx.TransportError):
            return "network"

    # TimeoutError 是 OSError 的子类，必须排在 disk 之前，否则超时会被记成磁盘错。
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    if isinstance(exc, ConnectionError):
        return "network"
    if _is_provider_error(exc):
        return "bad_response"
    if isinstance(exc, OSError):
        return "disk"
    return "unknown"


def _is_provider_error(exc: BaseException) -> bool:
    """provider 层自己抛的失败（返回内容为空、解析不了、词库打不开）。

    延迟导入：本模块被统计模块引用，而统计模块在启动路径上。为了判一个只在
    出错时才走到的分支，不该把整个 provider 包拉进冷启动。
    """
    from ..providers.base import ProviderError

    return isinstance(exc, ProviderError)


def _from_status(status: int) -> str:
    """按 HTTP 状态码归类。401/403 是密钥问题，429 是限流，两者处理方式不同。"""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if 500 <= status <= 599:
        return "http_5xx"
    if 400 <= status <= 499:
        return "http_4xx"
    return "bad_response"


def _httpx():
    """httpx 只在失败路径上用，导入失败不该让归类本身崩掉。"""
    try:
        import httpx
    except Exception:  # noqa: BLE001 - 缺依赖时按其余类别继续判
        return None
    return httpx
