"""生词本仓储层：CRUD 与全文检索。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .db import (
    _CJK_RE,
    FTS_COLUMNS,
    FTS_WEIGHTS,
    connect,
    fts_values,
    has_fts,
    strip_cjk,
    to_bigram,
)
from .models import Entry, Phrase, split_tags

_COLUMNS = (
    "id, lemma, phonetic, pos, definition_en, definition_zh, example, example_zh, "
    "synonyms_json, collocations_json, notes, tags, source, prompt_version, "
    "created_at, updated_at, review_due, review_count, review_interval, review_ease"
)

# JOINT 查询时 entries_fts 与 entries 存在同名列，必须全部加表前缀消歧义
_COLUMNS_E = ", ".join(f"e.{c.strip()}" for c in _COLUMNS.split(","))

ORDER_MAP = {
    "recent": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "alpha": "lemma COLLATE NOCASE ASC",
}

# 词组的排序映射。**单独一份而不是复用 ORDER_MAP**：那个的 alpha 按 lemma
# 排——phrases 表里根本没有这一列，复用会让「按字母排词组」直接抛 SQL 错误。
PHRASE_ORDER_MAP = {
    "recent": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "alpha": "phrase COLLATE NOCASE ASC",
}


def _tokens_lower(text: str) -> list[str]:
    """按词边界切开并小写化。用于「含这个词」的判断。"""
    return [t for t in re.split(r"[^0-9A-Za-z'\-]+", (text or "").lower()) if t]


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """`needle` 是否作为**连续的几个词**出现在 `haystack` 里。

    按词比较而不是按字符，是这一条的全部要点：`run` 不能命中 `trunk`
    与 `runway`。多词词组（`ice cream`）也照这条走——它在 `ice cream cone`
    里算命中，在 `icecream` 里不算。
    """
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))

# BM25 权重必须与 entries_fts 的列数严格一致，否则 bm25() 会直接报错。
# 由 FTS_WEIGHTS 生成，避免改列集时漏改这里。
_BM25 = "bm25(entries_fts, " + ", ".join(f"{w:.1f}" for w in FTS_WEIGHTS) + ")"

_FTS_INSERT = (
    f"INSERT INTO entries_fts(rowid, {', '.join(FTS_COLUMNS)}) "
    f"VALUES (?, {', '.join('?' * len(FTS_COLUMNS))})"
)


def _write_params(entry: Entry, now: str) -> tuple:
    """UPDATE 与 INSERT 共用的参数，顺序与两条语句的前 14 个占位列一致。

    两条语句的列序必须同步改：这里少一项或多一项，写进去的就不是错位而是
    sqlite3 的绑定数量异常，错误信息还落在调用行上，看不出是这里少写了一个。
    """
    return (
        entry.lemma,
        entry.phonetic,
        entry.pos,
        entry.definition_en,
        entry.definition_zh,
        entry.example,
        entry.example_zh,
        entry.synonyms_json(),
        entry.collocations_json(),
        entry.notes,
        entry.tags,
        entry.source,
        entry.prompt_version,
        now,
    )


def build_fts_query(raw: str) -> str:
    """把用户输入翻译为 FTS5 查询表达式。

    - 英文部分：裸词 + 前缀匹配。裸词（不带列名前缀）在 FTS5 中会命中**所有列**，
      因此英文侧天然覆盖例句、备注、标签，无需逐列罗列；
      列名前缀只用于给 lemma / 释义 / 同义语加权重提示。
    - 中文部分：转 bigram 后以 phrase 匹配 zh_bigram 列，实现子串语义。
      zh_bigram 聚合了全部中文内容（释义、例句翻译、备注、标签、同义语简释）。
    - 中英混合时以 AND 连接，纯中文或纯英文则单通道
    """
    q = (raw or "").strip()
    if not q:
        return ""

    ascii_part = strip_cjk(q).strip()
    cjk_segs = _CJK_RE.findall(q)
    clauses: list[str] = []

    if ascii_part:
        words = [w for w in re.split(r"[^0-9A-Za-z'\-]+", ascii_part) if w]
        if words:
            terms = " OR ".join('"{}"*'.format(w.replace('"', "")) for w in words)
            clauses.append(
                "({terms} OR lemma:({terms}) OR definition_en:({terms}) "
                "OR synonyms_text:({terms}))".format(terms=terms)
            )

    if cjk_segs:
        subs = []
        for seg in cjk_segs:
            bg = to_bigram(seg)
            if bg:
                subs.append('zh_bigram:"{}"'.format(bg))
        if subs:
            clauses.append("(" + " OR ".join(subs) + ")")

    return " AND ".join(clauses)


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """从连接本身问出库文件路径。

    不新增构造参数是有意的：`Repository(conn)` 的调用点散在主程序、自检、
    冒烟、消融各处，只要有一处忘了传路径，那条路径上的后台任务就会静默退回
    共用主连接——而这正是要消灭的写法。让 SQLite 自己回答（`database_list`
    的第一行就是主库文件），不存在「调用方记错」的余地。

    内存库的文件名为空串，此处映射成 None，`clone()` 据此拒绝：另开一条连接
    拿到的是另一个空库，给它一条「连得上但什么都没有」的连接，比直接报错更
    难查。
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return Path(row[2]) if row[2] else None
    except sqlite3.Error:
        return None
    return None


class Repository:
    """所有数据库读写都经由本类，UI 层不直接接触 SQL。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.db_path = _conn_db_path(conn)

    def clone(self) -> Repository:
        """为当前线程另开一条连接，返回指向同一个库的新仓储。

        后台任务（查词、重新查询、导入）一律走这个入口。主线程那条连接有线程
        归属，worker 里直接用它会在 CPython 上抛 ProgrammingError；而把
        `check_same_thread` 放宽成 False 换来的是「不报错但事务互相踩」。

        新连接是独立事务边界：调用方写完后必须自己 commit（仓储的写方法都已
        自行提交），用完 `close()`。克隆不复制任何状态，两个实例之间除库文件
        外没有其他联系。
        """
        if self.db_path is None:
            raise RuntimeError("这条连接没有对应的库文件，无法为其他线程另开连接")
        return Repository(connect(self.db_path))

    def close(self) -> None:
        """关闭底层连接。只用于 `clone()` 出来的临时仓储。"""
        self.conn.close()

    def upsert(self, entry: Entry) -> Entry:
        """写入词条；已存在的同 lemma 词条按其 id 覆盖全部内容字段。

        **用户自有字段（notes / tags）的边界**：`entry.id` 为空说明这是「刚从接口
        取回的新内容」，而不是用户编辑过的对象。此时若库中已有同 lemma 的词条，
        必须保留库里的备注与标签——否则点一次「重新查询」就会把用户写的备注静默
        抹掉（全文索引一起失效），且无法恢复。反过来，用户先 `get` 再改再 `upsert`
        （读取-修改-写入）时 `id` 已就绪，走「全部字段生效」分支，因此把备注清空
        这类显式操作依然有效。

        **整体成败一致**：写主表与写索引必须一起成功或一起失败。SQLite 的隐式
        事务在异常时不会自动回滚，于是出错后连接会停在 `in_transaction=True`
        的状态上，而**下一次任意成功操作的 commit() 会把这份半成品一并提交**
        ——后果是「主表里有这个词条、索引里没有」，用户搜索时找不到它，且
        期间被 `_sync_fts` 的 DELETE 波及的**别的词条**索引也会一起丢。
        这里显式回滚，把「写坏了」变成「这次没写成」。
        """
        now = Entry.now()
        # 必须在把 row 里的 id 写回 entry 之前判断，否则这个信息就丢了
        fresh_content = entry.id is None
        cur = self.conn.cursor()
        try:
            row = self._fetch_existing(cur, entry, fresh_content)
            params = _write_params(entry, now)
            if row:
                self._update_row(cur, entry, row, params, now)
            else:
                self._insert_row(cur, entry, params, now)
            self._sync_fts(entry)
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return entry

    def _fetch_existing(self, cur, entry: Entry, fresh_content: bool):
        """取库里同 lemma 的行，必要时把用户自有字段回写到 entry 上。

        `fresh_content` 由调用方在回写 id 之前算好：它表达的是「这次写入带来的
        是新内容」，不是「库里有没有这一行」。两者在读取-修改-写入场景下不同。
        """
        row = cur.execute(
            "SELECT id, created_at, notes, tags FROM entries WHERE lemma = ?",
            (entry.lemma,),
        ).fetchone()
        if row and fresh_content:
            # 接口回填：用户字段以库中现有值为准，并回写到内存对象上，
            # 让界面渲染与索引同步都能看到「保留之后」的内容
            entry.notes = row["notes"] or ""
            entry.tags = row["tags"] or ""
        return row

    def _update_row(
        self, cur, entry: Entry, row, params: tuple, now: str
    ) -> None:
        """覆盖已有行。id 与 created_at 取自库，更新时间取本次。"""
        entry.id = row["id"]
        entry.created_at = row["created_at"]
        entry.updated_at = now
        cur.execute(
            """UPDATE entries SET lemma=?, phonetic=?, pos=?, definition_en=?,
               definition_zh=?, example=?, example_zh=?, synonyms_json=?,
               collocations_json=?, notes=?, tags=?, source=?, prompt_version=?,
               updated_at=? WHERE id=?""",
            params + (entry.id,),
        )

    def _insert_row(self, cur, entry: Entry, params: tuple, now: str) -> None:
        """插入新行，并在这里播种复习计划。"""
        entry.created_at = now
        entry.updated_at = now
        # 复习计划在这里「播种」：新词条第一次进入生词本时，把它排进
        # 「待复习」（review_due = now，立即到期）。这对应产品口径
        # 「查词即收藏」——收藏的下一步就是复习，第一次复习（评分）
        # 之后才由 SM-2 决定下一次间隔。
        #
        # 而撤销删除恢复的旧词条带着**复习历史**（review_count > 0 或
        # review_due 非空），照原样保留——撤销不该重置记忆进度。所以
        # 判据是「进来的 Entry 有没有复习历史」，而不是「这行是不是新建」。
        seed_due = (
            entry.review_due
            if (entry.review_due or entry.review_count > 0)
            else now
        )
        cur.execute(
            """INSERT INTO entries
               (lemma, phonetic, pos, definition_en, definition_zh, example,
                example_zh, synonyms_json, collocations_json, notes, tags, source,
                prompt_version, updated_at, created_at, review_due,
                review_count, review_interval, review_ease)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            params
            + (
                now,
                seed_due,
                entry.review_count,
                entry.review_interval,
                entry.review_ease,
            ),
        )
        entry.id = cur.lastrowid

    def _sync_fts(self, entry: Entry) -> None:
        """FTS 索引手工同步（索引表与 entries 表松耦合，便于整体重建）。

        行的构造交给 db.fts_values()，与旧库重建共用同一份逻辑。
        """
        if not has_fts(self.conn):
            return  # FTS5 不可用，检索走 LIKE 回退
        self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry.id,))
        self.conn.execute(_FTS_INSERT, (entry.id, *fts_values(entry)))

    def get(self, lemma: str) -> Entry | None:
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM entries WHERE lemma = ?", (lemma,)
        ).fetchone()
        return Entry.from_row(row) if row else None

    def get_by_id(self, entry_id: int) -> Entry | None:
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        entry = Entry.from_row(row)
        # 详情视图需要展示收录的变体形式
        entry.aliases = self.aliases_of(entry.id)
        return entry

    def delete(self, entry_id: int) -> None:
        """删除词条及其索引。

        三条 DELETE 必须同生共死：索引表若正好坏着，第二条会抛错，而第一条
        已经把主表那行删掉了——SQLite 的隐式事务不会自动回滚，于是连接停在
        未提交状态，下一次成功操作的 commit() 会让「主表删了、索引还在」或
        反之落盘。显式回滚把这种情况收敛成「这次没删成」。

        第三条 aliases 是冗余的（DDL 里对 entries 有 ON DELETE CASCADE，
        且 `connect()` 开了 foreign_keys），保留它是为了让「删除要清哪几张表」
        在这一个地方看得全，不依赖读者去别处翻外键定义。
        """
        try:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
            self.conn.execute("DELETE FROM aliases WHERE entry_id = ?", (entry_id,))
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    # ---------- 别名（屈折形式）映射 ----------

    def link_alias(self, alias: str, entry_id: int) -> None:
        """记录「某形态 == 某词条」。alias 表主键唯一，重复写入即忽略。"""
        alias = (alias or "").strip().lower()
        if not alias or entry_id is None:
            return
        self.conn.execute(
            """INSERT INTO aliases(alias, entry_id, created_at) VALUES(?,?,?)
               ON CONFLICT(alias) DO UPDATE SET entry_id=excluded.entry_id""",
            (alias, entry_id, Entry.now()),
        )
        self.conn.commit()

    def find_by_alias(self, alias: str) -> Entry | None:
        row = self.conn.execute(
            f"""SELECT {_COLUMNS_E} FROM aliases a
                JOIN entries e ON e.id = a.entry_id
                WHERE a.alias = ?""",
            ((alias or "").strip().lower(),),
        ).fetchone()
        return Entry.from_row(row) if row else None

    def aliases_of(self, entry_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT alias FROM aliases WHERE entry_id = ? ORDER BY alias", (entry_id,)
        ).fetchall()
        return [r["alias"] for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"])

    def all_ids(self) -> set[int]:
        """全库词条 id 的快照。

        给批量导入用：区分「本次新增」与「本来就有」只能靠 id——`lookup()`
        返回的「是否命中缓存」说明不了这件事（别名命中 running → run 也是
        缓存命中，但那条词早就存在了）。导入前取一次快照，比每个词 import
        之前再查一遍库便宜得多，也不会被「同一个词在文件里出现两次」骗到。
        """
        return {int(r["id"]) for r in self.conn.execute("SELECT id FROM entries")}

    def list_entries(
        self, order: str = "recent", limit: int | None = None, offset: int = 0
    ) -> list[Entry]:
        sql = f"SELECT {_COLUMNS} FROM entries ORDER BY {ORDER_MAP.get(order, ORDER_MAP['recent'])}"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (limit, offset)
        return [Entry.from_row(r) for r in self.conn.execute(sql, params)]

    # ---------- 标签 ----------

    def tag_counts(self) -> list[tuple[str, int]]:
        """全库标签及其词条数，按数量降序、同数量按名称升序。

        **标签的解析只有一份实现**（`models.split_tags`），所以这里不能写
        `GROUP BY tags`——那是把「一个字段」当成「一个值」来数，而它实际上是
        一串用空格分开的标签。用 SQL 分组的话，「考研 高频」与「考研」会被算成
        两个不同的标签，而它们在用户眼里是同一个「考研」。

        词条数决定降序：界面上用来筛选的标签，最常用的那个最该先看到。
        """
        counts: dict[str, int] = {}
        for row in self.conn.execute("SELECT tags FROM entries"):
            for tag in split_tags(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def list_by_tag(
        self, tag: str, order: str = "recent", limit: int | None = None
    ) -> list[Entry]:
        """列出带某个标签的词条。

        先用 `LIKE` 在 SQL 侧粗筛，再用 `Entry.tag_list` 在 Python 侧精判：
        精判必须走**同一份解析**，否则筛「考研」会把「考研英语」也带进来
        （`LIKE '%考研%'` 只是子串匹配，它对标签边界一无所知）。

        粗筛的作用只是把候选行缩小——少了它就要把全库读进内存；而少了精判，
        结果就是错的。两者都不能省，且判断对错的那一半只有精判。
        """
        clean = (tag or "").strip()
        if not clean:
            return self.list_entries(order=order, limit=limit)
        sql = f"SELECT {_COLUMNS} FROM entries WHERE tags LIKE ?"
        params: tuple = (f"%{clean}%",)
        entries = [
            entry
            for entry in (
                Entry.from_row(r) for r in self.conn.execute(sql, params)
            )
            if clean in entry.tag_list
        ]
        if order == "alpha":
            entries.sort(key=lambda e: e.lemma.lower())
        else:
            entries.sort(
                key=lambda e: (e.created_at, e.id or 0), reverse=order != "oldest"
            )
        return entries if limit is None else entries[:limit]

    def search(self, query: str, limit: int | None = 300) -> list[Entry]:
        """中英双通道检索，FTS 不可用时回退 LIKE。

        `limit=None` 表示不设上限（导出路径使用）。列表需要上限是为了界面
        性能，但打印稿若沿用同一上限会被静默截断——文件头还自称「共 N 条」，
        而 N 恰好是被砍掉之后的数量，用户无从察觉。
        """
        query = (query or "").strip()
        if not query:
            return self.list_entries(limit=limit)

        expr = build_fts_query(query)
        if expr and has_fts(self.conn):
            try:
                sql = f"""SELECT {_COLUMNS_E} FROM entries_fts f
                        JOIN entries e ON e.id = f.rowid
                        WHERE entries_fts MATCH ?
                        ORDER BY {_BM25}"""
                params: tuple = (expr,)
                if limit is not None:
                    sql += " LIMIT ?"
                    params = (expr, limit)
                rows = self.conn.execute(sql, params).fetchall()
                if rows:
                    return [Entry.from_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass

        # 回退路径的字段覆盖必须与 FTS 一致，否则「索引一坏，检索能力静默退化」，
        # 这种退化极难被察觉，因此这里逐项对齐 notes / example / tags。
        like = f"%{query}%"
        sql = f"""SELECT {_COLUMNS} FROM entries
                WHERE lemma LIKE ? OR definition_en LIKE ? OR definition_zh LIKE ?
                   OR example LIKE ? OR example_zh LIKE ? OR synonyms_json LIKE ?
                   OR notes LIKE ? OR tags LIKE ?
                ORDER BY created_at DESC"""
        params = (like,) * 8 + (() if limit is None else (limit,))
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.conn.execute(sql, params).fetchall()
        return [Entry.from_row(r) for r in rows]

    # ---------- 词组收藏 ----------
    #
    # 与 entries 是**两张独立的表**（理由见 db.DDL 里 phrases 的建表注释）。
    # 这一节的每个方法都只碰 phrases：两张表的 AUTOINCREMENT 各自从 1 发号，
    # 同一个数字在两边指的是两条毫不相干的数据。任何「拿词条的 id 去删词组」
    # 或反过来，都会安静地删掉另一样东西。

    def add_phrase(self, phrase: Phrase) -> Phrase:
        """收藏一条词组；**同 phrase 已存在时返回已有的那条，不覆盖它的内容**。

        去重落在数据库的 `UNIQUE` 约束上（`ON CONFLICT DO NOTHING`），不是
        「先查一次再决定写不写」：那是两次独立操作，中间隔着一次可能失败的
        时间，而且是「同一件事两处判」。

        **不覆盖已有内容**：同一个词组从两个词条里收藏时，两次带的简释与例句
        可能不同。以先收藏的那份为准——与本程序「用户已有的内容不被后来的
        写入盖掉」一致。否则用户手动改过的那条会被下一次收藏静默改写，
        而两次操作在界面上都显示「已收藏」。
        """
        phrase.phrase = (phrase.phrase or "").strip()
        if not phrase.phrase:
            raise ValueError("词组内容不能为空")
        cur = self.conn.execute(
            """INSERT INTO phrases
               (phrase, gloss_zh, example, example_zh, source_lemma, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (
                phrase.phrase,
                phrase.gloss_zh,
                phrase.example,
                phrase.example_zh,
                phrase.source_lemma,
                Entry.now(),
            ),
        )
        self.conn.commit()
        stored = self.get_phrase(phrase.phrase)
        if stored is not None:
            return stored
        # 理论上到不了这里（要么插进去、要么撞上已有那条）。真到了说明
        # `ON CONFLICT` 没有按预期生效，此时把内存对象原样返回并补上本次的
        # 行号，好过返回 None 让调用方炸在别处。
        phrase.id = cur.lastrowid
        return phrase

    def get_phrase(self, phrase: str) -> Phrase | None:
        """按内容取一条（大小写不敏感，与表上的 COLLATE NOCASE 同款）。"""
        row = self.conn.execute(
            "SELECT * FROM phrases WHERE phrase = ?", ((phrase or "").strip(),)
        ).fetchone()
        return Phrase.from_row(row) if row else None

    def get_phrase_by_id(self, phrase_id: int) -> Phrase | None:
        row = self.conn.execute(
            "SELECT * FROM phrases WHERE id = ?", (int(phrase_id),)
        ).fetchone()
        return Phrase.from_row(row) if row else None

    def list_phrases(
        self, order: str = "recent", limit: int | None = None
    ) -> list[Phrase]:
        """列出全部词组。`limit=None` 表示不设上限（导出路径使用）。"""
        sql = (
            "SELECT * FROM phrases ORDER BY "
            f"{PHRASE_ORDER_MAP.get(order, PHRASE_ORDER_MAP['recent'])}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            return [
                Phrase.from_row(r)
                for r in self.conn.execute(sql, (int(limit),))
            ]
        return [Phrase.from_row(r) for r in self.conn.execute(sql)]

    def search_phrases(
        self, query: str, order: str = "recent", limit: int | None = None
    ) -> list[Phrase]:
        """按关键字过滤词组收藏。**刻意不建第二套全文索引。**

        判据是「四个字段里任意一个包含这个子串（忽略大小写）」，在 Python 侧
        比较，不进 SQL：
          1. 词组是用户**一条一条攒**出来的，量级是几十，全读进来比一次子串
             匹配快得多，也没有任何索引能帮上忙（前缀树在这个规模没有意义）；
          2. 少一张 FTS 表就少一套「列集与主表对不上时整表重建」的逻辑——
             那套逻辑在 entries 上必须存在，在 phrases 上纯属自找。
        """
        keyword = (query or "").strip().lower()
        items = self.list_phrases(order=order)
        if keyword:
            items = [
                item
                for item in items
                if keyword
                in " ".join(
                    (item.phrase, item.gloss_zh, item.example, item.example_zh)
                ).lower()
            ]
        return items if limit is None else items[:limit]

    def delete_phrase(self, phrase_id: int) -> bool:
        """删除一条词组，返回是否真的删掉了。"""
        cur = self.conn.execute("DELETE FROM phrases WHERE id = ?", (int(phrase_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def count_phrases(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS c FROM phrases").fetchone()["c"]
        )

    def phrases_containing(self, lemma: str) -> list[str]:
        """列出「含这个词」的已收藏词组，供详情区回显。

        判据是**按词边界逐词比较**（见 `_contains_run`），不是 `LIKE '%词%'`：
        子串匹配一定会误伤——`run` 会命中 `trunk`、`runway`、`brunch`，而用户
        看到的是「这个词底下凭空冒出一串不相干的词组」，且没有任何提示说明
        那是子串匹配的结果。
        """
        needle = _tokens_lower(lemma)
        if not needle:
            return []
        return [
            item.phrase
            for item in self.list_phrases()
            if _contains_run(_tokens_lower(item.phrase), needle)
        ]

    # ---------- 间隔重复复习 ----------

    def due_count(self, now: str) -> int:
        """到期待复习的条数（`review_due` 非空且不晚于 now）。"""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM entries "
                "WHERE review_due != '' AND review_due <= ?",
                (now,),
            ).fetchone()["c"]
        )

    def due_entries(self, now: str, limit: int | None = None) -> list[Entry]:
        """到期待复习的词条，按到期先后排列（最早到期的最先复习）。

        `limit=None` 表示不限条数（复习会话一次过完所有到期的词）；与
        `list_entries` / `search` 同款，默认不设上限。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM entries "
            "WHERE review_due != '' AND review_due <= ? "
            "ORDER BY review_due ASC, id ASC"
        )
        params: tuple = (now,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (now, limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [Entry.from_row(r) for r in rows]

    def apply_review(self, entry_id: int, grade: int, now: datetime) -> None:
        """按一次评分推进该词条的复习状态。

        只更新四列复习字段与 `updated_at`，不碰词条内容——复习是「学习进度」，
        不是「内容变更」。

        `now` 是**`datetime` 对象**（不是字符串）：它要用来算「下一次到期 = now +
        间隔」。与 `due_count` / `due_entries` 的字符串形参刻意不同——那两个只是
        拿字符串与库里存的 `review_due` 做字典序比较，这里要拿它做日期运算。
        由调用方传入，仓储层不读时钟，便于测试。
        """
        from .review import due_timestamp, format_timestamp, sm2

        row = self.conn.execute(
            "SELECT review_ease, review_interval, review_count "
            "FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return
        ease, interval, count = sm2(
            float(row["review_ease"] or 2.5),
            float(row["review_interval"] or 0.0),
            int(row["review_count"] or 0),
            grade,
        )
        self.conn.execute(
            "UPDATE entries SET review_due = ?, review_interval = ?, "
            "review_count = ?, review_ease = ?, updated_at = ? WHERE id = ?",
            (due_timestamp(now, interval), interval, count, ease, format_timestamp(now), entry_id),
        )
        self.conn.commit()
