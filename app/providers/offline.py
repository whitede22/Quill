"""离线词典 Provider。

数据源：ECDICT 1.0.28（MIT，skywind3000/ECDICT），由
`scripts/build_offline_dict.py` 裁剪成本项目使用的精简 SQLite——
`dict`（词条）/ `forms`（屈折形态 → 原形）/ `examples`（英汉对照例句）三张表。

与 `mock` 的区别：mock 只有 3 个演示词条，作用是让未配置密钥的人看到界面；
本 Provider 是一本真词典——内置约 5 万常用词，完全断网也能查。

## 能力边界（界面会照实说明，不假装）

- **没有同义语**。ECDICT 是「词 → 释义」语料，不含近义词关系。把 `exchange`
  （词形变化）拿来充当同义语会直接误导——`runs` 不是 `run` 的同义词，
  它是同一个词的另一种形式。同义语只有接口模式能给。
- **例句覆盖约三成**。源语料只为焦点词表配了例句，其余词条没有。
  没有就照实提示缺失，而不是拿一条不相干的句子凑数。

## 查词顺序

1. `dict` 精确命中（列已 COLLATE NOCASE）；
2. `forms` 命中 → 取原形 → 回查 `dict`。这一级是词典自带的形态表，
   比规则还原可靠：`news` 若在词库中就直接命中，不会被还原成 `new`。
   注意仍然**精确优先**——`running` 既是 `run` 的 -ing 形式，本身也是词条，
   此时返回 `running` 自己的释义。
3. 都没有才报错，并说明这是离线词库未收录，而非网络问题。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from ..core.models import Entry
from .base import ProviderError

# 离线词库的 schema 版本。**由读取方定义、构建脚本导入**：两边各写一份常量
# 的结局必然是改了其中一边而忘了另一边，然后程序读到不认识的列集才炸。
#
# 2：解码源语料的字面量换行（`\n` 两个字面量 → 真换行）。v1 的库把 `\n` 原样存着，
# 会在界面与导出里显示成这坨字符，还会让词性正则的 `\b` 边界失效。
OFFLINE_SCHEMA_VERSION = 2

# 随包分发的内置词库文件名。用户把自己构建的完整库放到数据目录时用同一个名字。
OFFLINE_DB_NAME = "offline_dict.db"


def bundled_dict_path() -> Path | None:
    """定位随包分发的内置词库；不存在时返回 None。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        # app/providers/offline.py → 上溯到项目根
        base = Path(__file__).resolve().parents[2]
    candidate = base / "assets" / OFFLINE_DB_NAME
    return candidate if candidate.exists() else None


def resolve_offline_dict() -> Path | None:
    """按优先级返回可用的离线词库：用户自建 > 随包内置。

    用户把 `build_offline_dict.py --tier full` 的产物放到数据目录即可覆盖
    内置的精选库（生僻词覆盖更全），无需改配置——目录里有什么就用什么。
    """
    from ..config import APP_DIR

    user_copy = APP_DIR / OFFLINE_DB_NAME
    if user_copy.exists():
        return user_copy
    return bundled_dict_path()


def read_schema_version(conn: sqlite3.Connection) -> int:
    """读词库自述的 schema 版本；读不到或非数字一律返回 0（视为不兼容）。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def open_offline_db(path: Path | str) -> sqlite3.Connection:
    """以只读方式打开离线词库，并校验版本。

    `check_same_thread=False` 是必要的：查词跑在 QThreadPool 的工作线程里，
    而 Provider 实例是共享的。这里始终只读，不存在多线程写竞争。

    **版本不符必须直接拒绝，不能读下去。** 版本号写进 meta 却没人校验，等于没写：
    v1 的库会把换行存成字面量转义（释义里显示 `\\n`，词性只认得第一行），
    静默读下去的后果是用户看到一堆乱码却不知道原因，只会以为「这本词典很烂」。
    拒绝时要说清是版本问题、以及怎么修。
    """
    path = Path(path)
    if not path.exists():
        raise ProviderError(f"离线词库不存在：{path}")
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise ProviderError(f"离线词库无法打开：{exc}") from exc
    conn.row_factory = sqlite3.Row

    found = read_schema_version(conn)
    if found != OFFLINE_SCHEMA_VERSION:
        conn.close()
        raise ProviderError(
            f"离线词库版本不符：文件为 {found or '未知'}，本程序需要 "
            f"{OFFLINE_SCHEMA_VERSION}。旧版库把换行存成字面量转义，"
            "直接使用会在释义里显示 `\\n`。请用 scripts/build_offline_dict.py "
            "重新构建，或删掉数据目录里的旧词库以改用随包内置库。"
        )
    return conn


def offline_dict_status(path: Path | str | None) -> str:
    """描述离线词库的规模，供设置与状态栏展示。"""
    if path is None:
        return "离线词库缺失"
    try:
        conn = sqlite3.connect(
            "file:" + quote(Path(path).as_posix(), safe="/:") + "?mode=ro", uri=True
        )
        try:
            meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
            words = int(meta.get("word_count") or 0)
            tier = meta.get("tier") or "?"
            version = int(meta.get("schema_version") or 0)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return "离线词库不可读"
    # 版本不符要说清是版本问题。笼统报「不可读」，用户修不了也无从判断
    # 该重下还是该重建；说「版本过旧」并给出动作，才算可操作的提示。
    if version != OFFLINE_SCHEMA_VERSION:
        return f"离线词库版本过旧（v{version or '未知'}），需重新构建"
    label = "完整库" if tier == "full" else "精选库"
    return f"离线词库 · {words:,} 词（{label}）"


class OfflineDictProvider:
    """离线词典。数据源为 ECDICT 裁剪出的本地 SQLite。"""

    name = "offline"

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = resolve_offline_dict()
        if db_path is None:
            raise ProviderError(
                "找不到离线词库文件。请确认安装完整，"
                "或用 scripts/build_offline_dict.py 自行构建。"
            )
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # ---------- 内部 ----------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_offline_db(self.db_path)
        return self._conn

    def _row_for(self, surface: str):
        db = self._db()
        row = db.execute("SELECT * FROM dict WHERE word = ?", (surface,)).fetchone()
        if row is not None:
            return row, surface
        mapped = db.execute(
            "SELECT word FROM forms WHERE form = ?", (surface,)
        ).fetchone()
        if mapped is not None:
            row = db.execute(
                "SELECT * FROM dict WHERE word = ?", (mapped["word"],)
            ).fetchone()
            if row is not None:
                return row, mapped["word"]
        return None, None

    def _to_entry(self, row, lemma: str) -> Entry:
        example = self._db().execute(
            "SELECT sentence_en, sentence_zh FROM examples WHERE word = ?", (lemma,)
        ).fetchone()
        return Entry(
            lemma=row["word"],
            phonetic=(row["phonetic"] or "").strip(),
            pos=(row["pos"] or "").strip(),
            definition_en=(row["definition"] or "").strip(),
            definition_zh=(row["translation"] or "").strip(),
            example=(example["sentence_en"] or "").strip() if example else "",
            example_zh=(example["sentence_zh"] or "").strip() if example else "",
            # 离线词库不含近义词关系。留空并由能力表声明，界面才不会
            # 给每一条都挂上「缺少同义语」——那种提示等于没有提示。
            synonyms=[],
        )

    # ---------- 对外 ----------

    def lookup(self, word: str) -> Entry:
        surface = (word or "").strip().lower()
        if not surface:
            raise ProviderError("请输入要查询的单词")

        try:
            row, lemma = self._row_for(surface)
        except sqlite3.Error as exc:
            raise ProviderError(f"离线词库读取失败：{exc}") from exc

        if row is None:
            raise ProviderError(
                f"离线词库未收录「{surface}」。"
                "内置词库覆盖常用词与考试词汇；生僻词可在设置中配置接口，"
                "或用 scripts/build_offline_dict.py 构建完整词库。"
            )
        return self._to_entry(row, lemma or surface)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
