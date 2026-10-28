"""SQLite 连接与 schema 管理。"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .models import Entry

SCHEMA_VERSION = 5

# 词条由哪一版提示词生成。列名单独命名为常量，是因为它同时被三处引用：
# DDL、补列迁移、以及 selfcheck 里「旧库补列」的取证脚本——各写一份字符串
# 是典型的数据漂移来源（FTS_COLUMNS 当初就是为同样理由抽成常量的）。
PROMPT_VERSION_COLUMN = "prompt_version"

# entries 表后续追加的列（列名 -> 建列声明）。
# 与 DDL 中的同名声明必须保持一致：DDL 负责新库，本表负责既有库的补列。
ENTRIES_ADDED_COLUMNS: dict[str, str] = {
    PROMPT_VERSION_COLUMN: "INTEGER NOT NULL DEFAULT 0",
    # 固定搭配（`Collocation` 的 JSON 序列化）。加列必须登记在这里：
    # `CREATE TABLE IF NOT EXISTS` 对**已存在**的 entries 表什么都不做，
    # 漏登记的后果是老用户的库永远没有这一列，随后任何带它的语句直接失败。
    "collocations_json": "TEXT NOT NULL DEFAULT '[]'",
    # 间隔重复复习（SM-2）的四列。`review_due` 是 ISO 时间戳，空串表示「从未
    # 复习过」——到期判据是 `review_due != '' AND review_due <= now`，而非「是否
    # 复习过」，因为「复习过一次但早已到期」才是真正需要再复习的那类。
    "review_due": "TEXT NOT NULL DEFAULT ''",
    "review_count": "INTEGER NOT NULL DEFAULT 0",
    "review_interval": "REAL NOT NULL DEFAULT 0",
    "review_ease": "REAL NOT NULL DEFAULT 2.5",
}

# 补列之前，所有词条都由「当时唯一存在过的那一版提示词」生成。
# 刻意写成常量而不是引用 providers.prompts.PROMPT_VERSION：本文件只在补列
# 那一次执行时使用该值，把它绑定到「当前版本号」会让历史回填随未来改动而漂移。
LEGACY_PROMPT_VERSION = 1

# 「迁移之前的 entries 长什么样」的**冻结快照**。
#
# 为什么不能让判据从当前 DDL 里把已登记的列剔掉来**倒推**旧表：那样倒推出来的
# 旧表**仍然含有漏登记的列**，于是「给 entries 加了列却忘了登记」这件最要命的
# 事，判据恰好看不见。实测过：把 `collocations_json` 从 `ENTRIES_ADDED_COLUMNS`
# 里删掉、或者把它的名字打错，倒推出来的旧表照样有这一列，缺列报「无」，自检
# 全绿——**又是「判据恰好漏掉被漏掉的那一类」**。
#
# 所以这份快照必须与当前 DDL **分开写死**：它是历史事实，不随今天的 schema 漂移。
# 上面加的列、以及未来要加的任何列，都**不要**动这里——动了它，判据就重新变成
# 一句恒为真的断言（比没有断言更危险）。
LEGACY_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phonetic      TEXT NOT NULL DEFAULT '',
    pos           TEXT NOT NULL DEFAULT '',
    definition_en TEXT NOT NULL DEFAULT '',
    definition_zh TEXT NOT NULL DEFAULT '',
    example       TEXT NOT NULL DEFAULT '',
    example_zh    TEXT NOT NULL DEFAULT '',
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    notes         TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

# 「算不算中文」只能有**一份**定义 —— 这是本项目最容易被写坏的一条约定，
# 历史上已经栽过两次。
#
# 这个范围有三个消费方，散布在三层：
#   1. 本模块的 to_bigram() / strip_cjk()，供 FTS5 中文子串检索
#   2. repository.build_fts_query()，切出中文段
#   3. importer.py，判断「第一个汉字之后都是解释，不是词」
#   4. build_offline_dict.py，构建期判定「例句中文侧算不算中文」+ 校验用的 SQLite GLOB
#
# 曾长期写成四份独立的字面量，而代价是实测过的：
#   - `has_cjk()` 过宽，把 `deprecate` 中文侧的 `±䁿`（U+407F，属 CJK 扩展 A）
#     当成中文留了下来；
#   - 构建期 GLOB 写成 `[一-龥]`，上界 龥 是 U+9FA5 而非 9FFF，于是
#     U+9FA6–9FFF 的汉字被 Python 放行、被 SQL 判成不合格，构建当场失败，
#     报的还是「例句中文侧不含汉字」这种指错方向的错。
#
# 所以范围只在这里定义一次，字符类由这一对常量派生；`scripts/build_offline_dict.py`
# 直接 import 本模块，不再自己写一份（脚本 import 应用代码在本项目有先例：
# 它本来就 import `app.providers.offline.OFFLINE_SCHEMA_VERSION`）。
CJK_START = "\u4e00"
CJK_END = "\u9fff"
# 注意这里是**字符类**（含方括号），与下面 GLOB 用的形式一致，可直接内插进正则。
CJK_CLASS = f"[{CJK_START}-{CJK_END}]"

# 单个汉字，`+` 变体由各调用点按需加——绝不复制字符类本身。
# 三个调用点对「一个还是一个连串」的需求不同，因此只共享字符类、不共享量词：
#   _CJK_RE    要整段（bigram 切分、strip_cjk 一次替换掉整串）
#   _CJK_ONE_RE 只要第一个汉字的位置（importer 在这里截断）
_CJK_RE = re.compile(CJK_CLASS + "+")
_CJK_ONE_RE = re.compile(CJK_CLASS)

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phonetic      TEXT NOT NULL DEFAULT '',
    pos           TEXT NOT NULL DEFAULT '',
    definition_en TEXT NOT NULL DEFAULT '',
    definition_zh TEXT NOT NULL DEFAULT '',
    example       TEXT NOT NULL DEFAULT '',
    example_zh    TEXT NOT NULL DEFAULT '',
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    collocations_json TEXT NOT NULL DEFAULT '[]',
    review_due    TEXT NOT NULL DEFAULT '',
    review_count  INTEGER NOT NULL DEFAULT 0,
    review_interval REAL NOT NULL DEFAULT 0,
    review_ease   REAL NOT NULL DEFAULT 2.5,
    notes         TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    prompt_version INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 屈折形式到词条的映射：running -> run。
-- 没有这张表时，同一个词的不同形态会各存一条，并且每次都重新调用接口。
CREATE TABLE IF NOT EXISTS aliases (
    alias      TEXT PRIMARY KEY COLLATE NOCASE,
    entry_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aliases_entry ON aliases(entry_id);

-- 词组收藏。**与 entries 是两张独立的表**，不是「entries 加上一个 kind 列」：
-- 词组没有标签、没有提示词版本、没有收录变体，也不该出现在按词检索的 FTS 里。
-- 混进 entries 会把 count() / list_entries() / search() / 导出上限判据全部
-- 牵进「这一个集合还是那两个集合」的分支，而那一片正是回归重点守护的地方。
--
-- `source_lemma` 是**文本而非外键**：它记的是「从哪个词的搭配里收藏来的」，
-- 而删掉那个词条不该连带删掉用户攒下来的词组。
--
-- `phrase` 唯一（COLLATE NOCASE，与 aliases 同款）：同一条词组收藏两次没有
-- 任何意义，它只会让列表里出现两行一模一样的卡片。去重放在数据库约束上而不是
-- 靠调用方自觉——那才是「同一件事只有一份定义」。
CREATE TABLE IF NOT EXISTS phrases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    gloss_zh     TEXT NOT NULL DEFAULT '',
    example      TEXT NOT NULL DEFAULT '',
    example_zh   TEXT NOT NULL DEFAULT '',
    source_lemma TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phrases_source ON phrases(source_lemma);
"""

# entries_fts 的列顺序承担三重角色：DDL 的列定义、BM25 权重的下标依据、
# 以及「旧库索引结构是否需要重建」的比对基准。三者必须同源，
# 因此 DDL 由本元组生成，不手写，避免对不齐。
#
# **固定搭配刻意不进这张索引**（`collocations_json` 只存在于 entries 表）。
# 三条理由，按分量排：
#   1. 加列就是**整表重建**（FTS5 虚表不支持 ALTER），而收益只是「搜搭配能命中
#      主词」——那是顺带的好事，不是用户抱怨过的事；
#   2. `fts_values()` 的 `zh_bigram` 聚合了全部中文内容，每词再挂 3–5 条搭配的
#      中文简释与例句翻译，会把「中文检索被噪音淹没」从担心变成常态；
#   3. 搜索命中的判据是 BM25 排序，搭配文本一进来就会给某些词条刷出与正文
#      无关的高分。**「搜不到搭配」是小事，「搜索被噪音淹没」是大事。**
FTS_COLUMNS = (
    "lemma",
    "definition_en",
    "definition_zh",
    "example_en",
    "synonyms_text",
    "notes",
    "tags",
    "zh_bigram",
)

# 与 FTS_COLUMNS 一一对应。词形命中权重最高，同义语次之，释义与备注再次，
# 例句最低——例句只负责兜底召回，不该在排序上压过释义。
FTS_WEIGHTS = (8.0, 2.0, 2.0, 1.0, 4.0, 3.0, 3.0, 1.0)

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
{columns},
    tokenize = 'unicode61 remove_diacritics 2'
);
""".format(columns=",\n".join(f"    {name}" for name in FTS_COLUMNS))


def fts_values(entry: Entry) -> tuple:
    """构造 entries_fts 的一行内容，顺序与 FTS_COLUMNS 严格对应。

    索引写入与旧库重建共用本函数——两边各写一份是典型的数据漂移来源。

    zh_bigram 聚合**所有**中文内容：中文释义、例句翻译、备注、标签、同义语简释。
    刻意不含 Synonym.diff_note：「强调」「侧重」这类词几乎出现在每个词条的差异说明里，
    纳入后搜这些词会命中整个生词本，是噪音而非召回。
    """
    zh_blob = " ".join(
        [entry.definition_zh, entry.example_zh, entry.notes, entry.tags]
        + [s.gloss_zh for s in entry.synonyms]
    )
    return (
        entry.lemma,
        entry.definition_en,
        entry.definition_zh,
        entry.example,
        entry.synonyms_text,
        entry.notes,
        entry.tags,
        to_bigram(zh_blob),
    )


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    return row is not None


def fts_columns_present(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute("PRAGMA table_info(entries_fts)"))


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """按 entries 重建全文索引。

    entries 是唯一事实来源，所以索引可以整体丢弃重建，无需逐版本迁移脚本。
    这也是把 FTS 表与主表松耦合的收益。
    """
    conn.execute("DROP TABLE IF EXISTS entries_fts")
    conn.execute(FTS_DDL)
    placeholders = ", ".join("?" * len(FTS_COLUMNS))
    insert = (
        f"INSERT INTO entries_fts(rowid, {', '.join(FTS_COLUMNS)}) "
        f"VALUES (?, {placeholders})"
    )
    for row in conn.execute("SELECT * FROM entries").fetchall():
        entry = Entry.from_row(row)
        conn.execute(insert, (entry.id, *fts_values(entry)))


def to_bigram(text: str) -> str:
    """把中文串切成 bigram 序列，供 FTS5 做子串级检索。

    FTS5 的 unicode61 分词器会把中文逐字切开，导致「无处不在」无法命中
    「无处不在的」；改为把中文段切成相邻二字组合，查询端做同样处理后
    以 phrase 方式匹配，即可获得正确的子串语义。
    """
    if not text:
        return ""
    out: list[str] = []
    for seg in _CJK_RE.findall(text):
        if len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return " ".join(out)


def strip_cjk(text: str) -> str:
    """返回去掉中文后的剩余部分。"""
    return _CJK_RE.sub(" ", text or "")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_columns(
    conn: sqlite3.Connection, table: str, expected: dict[str, str]
) -> list[str]:
    """按真实表结构补齐缺失的列，返回本次实际新增的列名。

    存在的理由：DDL 里的 `CREATE TABLE IF NOT EXISTS` 对**已存在**的表不做
    任何事。所以「给 entries 加一列」对老用户的库根本不会发生，随后任何带新列
    的 INSERT/SELECT 都会直接失败。此处与 FTS 重建采取同一策略——比对真实
    结构而不是信任 meta 里的版本号，因为版本号只说明「写到过哪一版」，
    不说明「表结构是否真的对」。
    """
    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    added: list[str] = []
    for name, declaration in expected.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            added.append(name)
    return added


def init_db(conn: sqlite3.Connection) -> None:
    """建表、校准表结构、推进 schema 版本。

    表结构校准分两条独立路径，因为两类表的能力不同：

    - entries / aliases：普通表，缺列可用 ALTER TABLE 增量补上，因此新增列
      必须登记进 ENTRIES_ADDED_COLUMNS，否则老用户的库不会真的多出这一列。
      注意「新库有这一列」不等于「老库也有」——CREATE TABLE IF NOT EXISTS
      只对新库生效。
    - entries_fts：FTS5 虚表不支持 ALTER，列集变化只能整表重建。entries 是
      唯一事实来源，重建即重新索引，不会丢数据。
    """
    conn.executescript(DDL)

    added = _ensure_columns(conn, "entries", ENTRIES_ADDED_COLUMNS)
    if PROMPT_VERSION_COLUMN in added:
        # 只在「刚刚补出这一列」时回填。补列是一次性动作：已标注过版本的行
        # 不能被再次改写，否则维护者递增版本号后，老词条会被重新说成新版生成，
        # 陈旧提示永远不出现。
        conn.execute(
            f"UPDATE entries SET {PROMPT_VERSION_COLUMN} = ? "
            f"WHERE {PROMPT_VERSION_COLUMN} = 0",
            (LEGACY_PROMPT_VERSION,),
        )

    # FTS5 缺失时降级为 LIKE 检索：没有索引只影响速度与排序质量，
    # 不该让程序直接起不来。search() 内置了 LIKE 回退路径。
    if not has_fts(conn):
        try:
            conn.execute(FTS_DDL)
        except sqlite3.OperationalError:
            pass
    elif fts_columns_present(conn) != FTS_COLUMNS:
        # 比对真实表结构，而不是信任 meta 里的版本号：版本号只说明「写到过哪一版」，
        # 不说明「表结构是否真的对」。曾出现过版本号未推进导致升级静默失效的问题。
        _rebuild_fts(conn)

    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
    else:
        try:
            current = int(row["value"])
        except (TypeError, ValueError):
            current = 0
        if current != SCHEMA_VERSION:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
            )
    conn.commit()


def backup_to(conn: sqlite3.Connection, dest: Path | str) -> Path:
    """把当前数据库完整备份到 dest，返回落盘路径。

    两个刻意的设计，都是为了避免「备份本身成为数据风险」：

    1. 先写同目录的 .tmp，校验通过后再用 os.replace 原子替换目标文件。
       若直接写目标路径，一旦中途失败，用户原来那份可用的备份会一起被毁掉。
    2. 备份库以 journal_mode=DELETE 建立，数据全部落在单个文件里。
       若沿用源库的 WAL 模式，则 .tmp-wal 里可能还压着未回写的页，
       只搬运主文件就会得到一个「看起来正常但内容缺失」的备份。

    integrity_check 这一步不能省：备份完不校验，等于没备份。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    sidecars = [tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")]
    for stale in sidecars:
        stale.unlink(missing_ok=True)

    conn.commit()  # 落盘未提交的改动，否则备份会漏掉最近一次写入

    target = sqlite3.connect(str(tmp))
    try:
        target.execute("PRAGMA journal_mode=DELETE")
        conn.backup(target)
        target.commit()
    finally:
        target.close()

    try:
        check = sqlite3.connect(str(tmp))
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError(
                f"备份校验未通过：{row[0] if row else '校验无返回'}"
            )
        os.replace(tmp, dest)
    except Exception:
        for stale in sidecars:
            stale.unlink(missing_ok=True)
        raise

    for stale in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        stale.unlink(missing_ok=True)
    return dest


def inspect_backup(path: Path | str) -> str:
    """检查一份备份能不能用，返回空串表示可用，否则返回一句原因。

    三条都要查，少一条都会让用户在用坏文件覆盖之后才发现：
    文件在不在、`integrity_check` 过不过、里面有没有 `entries` 表。
    最后一条专门防「拿别的 sqlite 文件当备份」——那种文件完整性检查是 ok 的。
    """
    target = Path(path)
    if not target.is_file():
        return "找不到这个文件"
    try:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"打不开：{exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            return f"完整性检查未通过：{row[0] if row else '无返回'}"
        tables = {
            item[0]
            for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "entries" not in tables:
            return "这个文件里没有词条表，不像是 Quill 的备份"
    except sqlite3.Error as exc:
        return f"读取失败：{exc}"
    finally:
        conn.close()
    return ""


def stage_restore(backup: Path | str, target: Path | str) -> Path:
    """把备份复制到目标库旁边的临时文件，供调用方在**关闭连接之后**替换。

    先校验再复制。复制而不是直接 `os.replace(backup, target)`：备份文件本身
    要留着，用户可能想保留它，或者这次恢复只是为了拿回其中几条。
    """
    source = Path(backup)
    problem = inspect_backup(source)
    if problem:
        raise sqlite3.DatabaseError(problem)
    dest = Path(target)
    tmp = dest.with_name(dest.name + ".restore")
    tmp.unlink(missing_ok=True)
    # 按字节复制：源文件可能仍是 WAL 模式，但 inspect 时只读打开、不写入，
    # 主文件本身是完整的（backup_to 产出的就是 DELETE 模式的单文件）。
    tmp.write_bytes(source.read_bytes())
    return tmp


def replace_with_staged(staged: Path | str, target: Path | str) -> None:
    """用已校验的临时文件替换目标库。

    调用前必须做两件事，顺序不能反：**先 `PRAGMA wal_checkpoint(TRUNCATE)`
    把日志回写进主文件，再关闭所有连接**。WAL 里压着已提交但还没回写的页，
    直接删 `-wal` 等于把它们扔掉。

    Windows 上打开的 sqlite 连接会锁住文件，`os.replace` 会抛
    PermissionError——那正是「恢复看起来成功、实际没换」的典型来源，所以
    替换失败必须让异常往上抛，由界面如实说。
    """
    target_path = Path(target)
    for suffix in ("-wal", "-shm"):
        target_path.with_name(target_path.name + suffix).unlink(missing_ok=True)
    os.replace(staged, target_path)
