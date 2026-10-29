"""本地使用统计：一个入口、一张白名单、一个独立文件。

四条规则，每一条都能被 `selfcheck` 单独证伪：

1. **未授权不记**。`analytics_enabled` 为假时 `record_event()` 立刻返回，连库都
   不打开。开关每次现读配置，不缓存——缓存会让「刚在设置里关掉」与「实际还在
   记」之间出现窗口。
2. **只认白名单**。事件名、属性名、属性值三样都要在表里。不在表里的事件整条
   拒绝，不在表里的属性单个丢弃（多传一个字段不该让整条事件消失）。
3. **失败不致命**。本模块对外不抛异常。统计是附加物，它坏掉时用户的核心学习
   流程必须照常。
4. **不记内容**。没有任何自由文本字段：没有生词、释义、备注、例句、剪贴板、
   密钥、异常原文、绝对路径。想加字段必须先改进本文件的白名单，而白名单只收
   枚举与计数。
5. **会话号不是身份**。每次启动生成一个八位随机值（`session_id()`），只用来
   把同一次打开里的操作归到一起。它不来自硬件、不与上一次关联、不写进配置；
   用户删掉统计库它就没了。它随事件一起落在本机库里，因为不落盘就没法分组。

存储刻意放在独立文件 `analytics.db`，而不是往 `quill.db` 加表：统计写失败、
库被锁、用户删掉这个文件，都不该碰到生词本；备份与导出词库时也不会把统计
数据一起带走。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..version import APP_VERSION
from .errors import CLASSES as _ERROR_CLASSES

# 事件表结构版本。加列或改语义时 +1，报告里会带上它，便于旧报告仍可解析。
# v2：加 session_id 列；查词加来源与缓存标记；复习加四档评分计数；
#     启动加耗时与词库规模分档；错误从笼统的「某操作失败」改成有限类别。
SCHEMA_VERSION = 2

_MAX_PROPERTIES = 8
_MAX_NAME_LEN = 32
_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_MAX_COUNT = 1_000_000
_MAX_JSON_LEN = 512

# 词条来源只有这三条路：手输、导入、剪贴板预填后回车。
SOURCE_TYPES: tuple[str, ...] = ("manual", "import", "clipboard")

# 属性种类 → 校验方式。种类是有限的几种，不做「任意类型都收」。
_COUNT = "count"
_DURATION = "duration_ms"
_FLAG = "flag"
_SOURCE = "source_type"
_CODE = "error_code"
_FORMAT = "format"
_PROVIDER = "provider_kind"
_BUCKET = "vocab_bucket"
_OP = "op"

_FORMAT_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")

# 枚举型属性：值必须落在给定集合里，多一个就丢掉。换名或加值时只改这里，
# 否则同一列在报告里会读出第四种写法，分析时得先做一次归一化。
_ENUMS: dict[str, frozenset[str]] = {
    # 这次查词真正用的是哪条路。判「是不是兜底」只能看 fallback_reason，
    # 兜底词条的 source 也是 offline，与用户主动选离线完全同值。
    _PROVIDER: frozenset({"online", "offline", "fallback"}),
    # 生词本规模分档。要分档是因为「有人攒到几百词吗」这个结论只需要量级，
    # 精确条数除了多记一个数没有别的用处。
    _BUCKET: frozenset({"0", "1-49", "50-199", "200+"}),
    _OP: frozenset({"lookup", "phrase", "refresh", "import", "export", "report"}),
    # 错误类别。原先记的是「某操作 failed」，一个操作一个值，读不出失败原因。
    # 取值表在 `core/errors.py`，那边负责归类，这边只负责校验值在那张表里。
    _CODE: frozenset(_ERROR_CLASSES),
}

# 事件白名单。只收**真实存在**的业务动作，不为凑数编造事件。
EVENTS: dict[str, dict[str, str]] = {
    # 启动一次。startup_ms 是进程启动到窗口显示；vocab_bucket 看有没有人
    # 真攒到几百词——这个数决定复习模块还值不值得继续投入。
    "app_started": {"startup_ms": _DURATION, "vocab_bucket": _BUCKET},
    # 查词一次。duration_ms 是从发起到拿到结果（含兜底）的时长。
    # provider_kind 与 cached 是给「四秒卡在哪一环」准备的：没有这两项，
    # 慢只能记成慢，分不出是接口、离线首次加载还是根本没查库。
    "dictionary_lookup": {
        "duration_ms": _DURATION,
        "success": _FLAG,
        "provider_kind": _PROVIDER,
        "cached": _FLAG,
    },
    # 新增一个词条。source_type 区分手输、导入、剪贴板。
    "word_added": {"source_type": _SOURCE},
    # 复习完一轮。四档计数是 SM-2 参数的唯一依据：只知道「评了几条」，
    # 调不了间隔与难度系数。
    "review_completed": {
        "count": _COUNT,
        "duration_ms": _DURATION,
        "forgot": _COUNT,
        "fuzzy": _COUNT,
        "remembered": _COUNT,
        "easy": _COUNT,
    },
    # 导入收尾。count 是实际写库条数，不含跳过。
    "import_completed": {"count": _COUNT, "success": _FLAG},
    # 导出收尾。format 是导出格式标识，取值由界面层传入。
    "export_completed": {"format": _FORMAT, "count": _COUNT},
    # 出错。只记类别与操作名，不写异常原文（里面常含路径与用户输入）。
    "app_error": {"error_code": _CODE, "op": _OP},
}

# 写失败的次数。只给自检与状态栏用，不落盘。
_failures = 0

# 本次进程的会话号。import 时生成一次，之后整个进程不变。
# 放在模块级而不是每次现取：同一次打开里的所有事件必须归到一组，取两次就会
# 得到两个号，分组的结论直接失效。
_SESSION_ID = secrets.token_hex(4)


def session_id() -> str:
    """本次启动的会话号。

    八位十六进制，来自 `secrets`，与硬件、账号、上一次启动都没有关系。
    它只做一件事：把同一次打开里发生的事件归到一起，好回答「这次打开用了多深」。
    """
    return _SESSION_ID


def failures() -> int:
    """累计写失败次数（进程内）。用于自检与「统计不可用」的提示。"""
    return _failures


# ---------- 配置开关 ----------


def enabled() -> bool:
    """当前是否允许记录。每次现读配置，不缓存。

    配置读取失败时 `load_config()` 会退回默认值，而默认值是「关」——这正好是
    任务书要求的保守方向：宁可少记，不可误记。
    """
    try:
        return bool(config.load_config().get("analytics_enabled", False))
    except Exception:  # 配置模块本身已保证不抛，这里再兜一层
        return False


def decided() -> bool:
    """用户是否已经就统计明确表过态。未表态时主窗口才会弹告知页。"""
    try:
        return bool(config.load_config().get("analytics_consent_decided", False))
    except Exception:
        return False


def set_enabled(value: bool, *, mark_decided: bool = True) -> None:
    """写入开关。`mark_decided` 为真时同时记下「用户表过态」。"""
    cfg = config.load_config()
    cfg["analytics_enabled"] = bool(value)
    if mark_decided:
        cfg["analytics_consent_decided"] = True
    config.save_config(cfg)


# ---------- 数据库 ----------


def db_path() -> Path:
    return config.ANALYTICS_PATH


def _connect() -> sqlite3.Connection:
    # 数据目录可能还不存在（全新安装、或用户把目录删了）。sqlite3.connect 不会
    # 替我们建目录，会直接抛 OperationalError，所以先建。
    config.ensure_dir()
    conn = sqlite3.connect(db_path(), timeout=1.0)
    # 按列名取值。少了这一行，下面所有 `row["n"]` 都会抛 TypeError，而异常又被
    # 本模块「失败不致命」的兜底吃掉——表现是「库里明明有 6 条，摘要显示 0 条」。
    # 实测踩过：不报错、不清零，就是静静地说没有数据。
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 统计写入是一条 INSERT，1 秒是「宁可放弃本次记录也不卡界面」的上限。
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_name      TEXT    NOT NULL,
            ts_utc          TEXT    NOT NULL,
            app_version     TEXT    NOT NULL,
            session_id      TEXT    NOT NULL DEFAULT '',
            properties_json TEXT    NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_name_ts ON events(event_name, ts_utc)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧结构的库补到当前版本。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，所以 v1 的库拿到手就
    缺 `session_id`。这里按缺失补列，而不是删表重建：统计数据可以丢，但「丢」
    这件事得由用户决定，不该由一次升级替他做。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "session_id" not in columns:
        conn.execute(
            "ALTER TABLE events ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "UPDATE meta SET value = ?"
        " WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
        (str(SCHEMA_VERSION), SCHEMA_VERSION),
    )


# ---------- 校验 ----------


def _clean_value(kind: str, value: object) -> object | None:
    """按种类校验单个属性值。不合格返回 None（该属性被丢弃）。"""
    allowed = _ENUMS.get(kind)
    if allowed is not None:
        return value if isinstance(value, str) and value in allowed else None
    if kind == _FLAG:
        return value if isinstance(value, bool) else None
    if kind in (_COUNT, _DURATION):
        # bool 是 int 的子类，`True` 会当成 1 —— 必须显式排除，否则
        # 「success: True」写进 count 也能通过类型检查。
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        limit = _MAX_COUNT if kind == _COUNT else _MAX_DURATION_MS
        if 0 <= value <= limit:
            return value
        return None
    if kind == _SOURCE:
        return value if value in SOURCE_TYPES else None
    if kind == _FORMAT:
        return value if isinstance(value, str) and _FORMAT_RE.fullmatch(value) else None
    return None


def _clean_properties(spec: dict[str, str], properties: dict) -> dict:
    """过滤属性：未知键丢弃、类型不符丢弃、超出条数截断。

    注意这里是**丢弃**而不是整条拒绝：业务侧多传一个字段属于开发期疏忽，
    让它连带丢掉整条事件，等于统计悄悄少一块，而且不报错。
    """
    if not isinstance(properties, dict):
        return {}
    clean: dict[str, object] = {}
    for key, value in properties.items():
        if len(clean) >= _MAX_PROPERTIES:
            break
        kind = spec.get(key)
        if kind is None:
            continue
        checked = _clean_value(kind, value)
        if checked is not None:
            clean[key] = checked
    return clean


# ---------- 写入 ----------


def record_event(event_name: str, properties: dict | None = None) -> bool:
    """记录一条事件。返回是否真的写入。**本函数不抛异常。**

    这是唯一入口：业务模块不允许直接连统计库。所有校验都在这里，
    调用方只负责在真实动作发生时喊一声。
    """
    global _failures
    try:
        if not isinstance(event_name, str) or len(event_name) > _MAX_NAME_LEN:
            return False
        spec = EVENTS.get(event_name)
        if spec is None:
            return False
        if not enabled():
            return False

        clean = _clean_properties(spec, properties or {})
        payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        if len(payload) > _MAX_JSON_LEN:
            payload = "{}"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO events(event_name, ts_utc, app_version, session_id,"
                " properties_json) VALUES(?, ?, ?, ?, ?)",
                (event_name, stamp, APP_VERSION, _SESSION_ID, payload),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        _failures += 1
        return False


# ---------- 读取与清理 ----------


def _query(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    _ensure_schema(conn)
    return list(conn.execute(sql, args))


def total() -> int:
    """事件总数。库不存在或读不出来时返回 0。"""
    if not db_path().exists():
        return 0
    try:
        conn = _connect()
        try:
            rows = _query(conn, "SELECT COUNT(*) AS n FROM events")
            return int(rows[0]["n"]) if rows else 0
        finally:
            conn.close()
    except Exception:
        return 0


def summarize(days: int = 30) -> dict:
    """给界面摘要与报告用的聚合结果。

    只做计数与按天分组，不返回任何属性原文以外的东西——属性本身已经是白名单
    里的枚举与数字。
    """
    empty = {
        "total": 0,
        "by_event": {},
        "by_day": [],
        "sessions": 0,
        "first_ts": "",
        "last_ts": "",
        "days": days,
    }
    if not db_path().exists():
        return empty
    try:
        conn = _connect()
        try:
            _ensure_schema(conn)
            by_event = {
                row["event_name"]: int(row["n"])
                for row in conn.execute(
                    "SELECT event_name, COUNT(*) AS n FROM events GROUP BY event_name"
                )
            }
            span = conn.execute(
                "SELECT MIN(ts_utc) AS a, MAX(ts_utc) AS b FROM events"
            ).fetchone()
            # 会话数就是「打开了几次」。旧库里这一列是空串，数出来是 0 而不是
            # 一个看起来合理的假数——空串本来就不该算成一次打开。
            sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM events"
                " WHERE session_id <> ''"
            ).fetchone()
            # 按**本地日期**分组给人看：库里存 UTC，用户看到的是自己那边的「今天」。
            by_day = [
                (row["d"], int(row["n"]))
                for row in conn.execute(
                    "SELECT strftime('%Y-%m-%d', ts_utc, 'localtime') AS d,"
                    " COUNT(*) AS n FROM events GROUP BY d ORDER BY d"
                )
            ]
        finally:
            conn.close()
        if days > 0:
            by_day = by_day[-days:]
        return {
            "total": sum(by_event.values()),
            "by_event": by_event,
            "by_day": by_day,
            "sessions": int(sessions["n"]) if sessions else 0,
            "first_ts": (span["a"] or "") if span else "",
            "last_ts": (span["b"] or "") if span else "",
            "days": days,
        }
    except Exception:
        return empty


def clear() -> int:
    """删除全部统计数据，返回删除条数。

    只碰 `analytics.db` 里的 `events` 表。词库不在这个文件里，删不到它。
    """
    if not db_path().exists():
        return 0
    try:
        conn = _connect()
        try:
            _ensure_schema(conn)
            before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            conn.execute("DELETE FROM events")
            conn.commit()
            return int(before)
        finally:
            conn.close()
    except Exception:
        return 0


def report_rows(limit: int = 2000) -> list[dict]:
    """取最近 `limit` 条事件，供报告导出。

    每条只含白名单字段：事件名、时间、版本、会话号、属性 JSON（已过滤）。
    不返回主键，主键会暴露「本机一共记了多少条」以外的顺序信息，对分析没有
    价值。会话号是随机的，同一次打开里的事件共用它。
    """
    if not db_path().exists():
        return []
    try:
        conn = _connect()
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT event_name, ts_utc, app_version, session_id, properties_json"
                " FROM events ORDER BY id DESC LIMIT ?",
                (max(0, int(limit)),),
            )
            return [
                {
                    "event_name": row["event_name"],
                    "ts_utc": row["ts_utc"],
                    "app_version": row["app_version"],
                    "session_id": row["session_id"],
                    "properties": json.loads(row["properties_json"] or "{}"),
                }
                for row in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []
