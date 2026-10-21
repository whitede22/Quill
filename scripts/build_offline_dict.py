"""构建 Quill 使用的离线词库（数据源：ECDICT）。

## 为什么需要「构建」这一步，而不是直接分发 ECDICT 原库

1. **体积**：原库 131 MB（77 万词条 × 13 列）。其中 `sw`（模糊匹配键）、
   `collins` / `oxford` / `bnc` / `frq` 等字段服务于分词与排序，对「查一个词、
   看它的释义」这个场景是纯冗余。直接内置会让安装包从 73 MB 涨到 200 MB 以上。
2. **查词命中率**：原库没有覆盖全量的「屈折形态 → 原形」索引——
   `focus_word_forms` 只覆盖 4.7 万条焦点词。查 `running` 要落到 `run`，
   只靠规则还原会出现同形碰撞（`news` → `new`）。本脚本从 `exchange`
   字段反向构建全量形态索引，把命中率交给词典本身而不是猜测。
3. **例句在另一个库里**，需要按词合并进来。

产物是自包含的 SQLite，只含 `dict` / `forms` / `examples` 三张表与 `meta`。

## 两档规模

| tier   | 词条数   | 用途                                       |
|--------|---------|--------------------------------------------|
| `core` | 约 5 万  | 内置进安装包，开箱即可离线查词                |
| `full` | 约 77 万 | 用户自行构建到数据目录，替换内置库以覆盖生僻词  |

`core` 的收录标准是「考试词表 ∪ 高频词 ∪ 有重要性标记」，覆盖
中考 / 高考 / 四六级 / 考研 / 雅思 / 托福 / GRE / GMAT / 专四 / 专八
以及 BNC / COCA 高频区间——即「学习者在阅读中真正会查的词」。

## 用法

    python scripts/build_offline_dict.py \\
        --source _ecdict_src/unpacked/ecdict.sqlite \\
        --examples _ecdict_src/unpacked/examples.sqlite \\
        --out assets/offline_dict.db --tier core

数据获取方式见 README 的「离线词典」一节（ECDICT 为 MIT 许可）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

# 产物 schema 版本由**读取方**定义（app/providers/offline.py），这里导入使用：
# 两边各写一份常量的结局必然是改了其中一边而忘了另一边，
# 然后程序读到不认识的列集才炸。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.providers.offline import OFFLINE_SCHEMA_VERSION  # noqa: E402

# 词性标记。长的写在前面，否则 "vt." 会被 "v." 抢先匹配掉。
_POS_RE = re.compile(
    r"\b(vt|vi|adj|adv|prep|conj|pron|num|art|interj|aux|abbr|pl|n|v)\.", re.IGNORECASE
)

# 只有小写字母与连字符的词才算「单词」。
# 排除首字符非字母、含撇号/数字/空格的条目：这些要么是词典内部的形态标记
# （`'hood`），要么是词组（`a little bird told me`），对单词查词是噪音。
PLAIN_WORD_COND = "word GLOB '[a-z]*' AND word NOT GLOB '*[^a-z-]*'"

# 至少要有一边释义，否则收进来也只会得到一条空白记录（与 Provider 的
# 空壳拒收规则一致）。
HAS_MEANING_COND = "(translation <> '' OR definition <> '')"

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 释义本体。WITHOUT ROWID 让主键即数据，省掉一整个 rowid 索引。
CREATE TABLE IF NOT EXISTS dict (
    word        TEXT PRIMARY KEY COLLATE NOCASE,
    phonetic    TEXT NOT NULL DEFAULT '',
    pos         TEXT NOT NULL DEFAULT '',
    definition  TEXT NOT NULL DEFAULT '',
    translation TEXT NOT NULL DEFAULT '',
    tag         TEXT NOT NULL DEFAULT '',
    collins     INTEGER NOT NULL DEFAULT 0,
    oxford      INTEGER NOT NULL DEFAULT 0,
    bnc         INTEGER NOT NULL DEFAULT 0,
    frq         INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- 屈折形态 -> 原形。查词时先查 dict，再查这里。
CREATE TABLE IF NOT EXISTS forms (
    form TEXT PRIMARY KEY COLLATE NOCASE,
    word TEXT NOT NULL
) WITHOUT ROWID;

-- 英汉对照例句。数据源覆盖焦点词表，故词条数远小于 dict——
-- 界面对没有例句的词条会如实说明，而不是假装词典本该有例句。
CREATE TABLE IF NOT EXISTS examples (
    word        TEXT PRIMARY KEY COLLATE NOCASE,
    sentence_en TEXT NOT NULL DEFAULT '',
    sentence_zh TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""


def tier_condition(tier: str, bnc_max: int) -> str:
    """返回筛选词条用的 SQL 条件。"""
    if tier == "full":
        return HAS_MEANING_COND
    return (
        f"{PLAIN_WORD_COND} AND {HAS_MEANING_COND} AND ("
        "  id IN (SELECT entry_id FROM focus_word_memberships)"
        f"  OR (bnc > 0 AND bnc <= {int(bnc_max)})"
        f"  OR (frq > 0 AND frq <= {int(bnc_max)})"
        "  OR collins >= 1 OR oxford = 1 OR tag <> ''"
        ")"
    )


def extract_pos(translation: str, fallback: str) -> str:
    """从中文释义里提取词性标记。

    源库的 `pos` 列大面积为空，而 `translation` 逐行以 `n.` / `v.` 开头，
    本身就是最可靠的词性来源。
    """
    found: list[str] = []
    for match in _POS_RE.finditer(translation or ""):
        tag = match.group(1).lower() + "."
        if tag not in found:
            found.append(tag)
    if found:
        return " / ".join(found[:4])
    return (fallback or "").strip()


def clean_text(text: str | None) -> str:
    """规整释义文本：解码源语料的字面量换行、统一换行、去行尾空白、折叠多余空行。

    **源语料把换行存成两个字面量（反斜杠 + n），不是真换行。** 这一层必须在这里
    解开，不能留给读取方：后果不只是界面上多出 `\\n` 这坨字符——词性正则
    `_POS_RE` 要求 `\\b` 边界，而 `\\nvi.` 里 n 与 v 之间不构成边界，于是释义
    第一行之后的词性**全部匹配不到**（`run` 以动词为主，pos 却被记成纯名词）。
    导出侧同样受害：`_flatten` 用 `str.split()` 压行，而反斜杠不是空白字符，
    转义会原样写进打印稿。

    只解码 `\\n` 与 `\\r`。**不做整串反转义**——有的释义本身在讲正则语法，
    里面的 `\\W` / `\\S` 是正当内容，一并吃掉就是把数据改坏。
    """
    if not text:
        return ""
    raw = str(text)
    # ① 字面量转义 → 真换行；② 源里可能混着真换行，一并归一
    raw = raw.replace("\\n", "\n").replace("\\r", "\n")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue  # 丢掉开头与连续的空行，保留段落之间的单个空行
        out.append(line)
    return "\n".join(out).strip()


def parse_exchange(word: str, exchange: str) -> tuple[list[tuple[str, str]], str | None]:
    """拆解 ECDICT 的 `exchange` 字段。

    格式形如 `p:ran/d:run/i:running/3:runs`：

    - `X:form`（X 不是 0/1）表示「本词的 X 型变化是 form」→ form 的原形是本词；
    - `0:base` 表示「本词的原形是 base」→ 本词是 base 的屈折形式；
    - `1:type` 只是「本词属于哪种变化」的标记，不产生任何映射。

    返回 (屈折形态→本词 的映射列表, 本词的原形或 None)。
    """
    forms: list[tuple[str, str]] = []
    base: str | None = None
    for part in (exchange or "").split("/"):
        if ":" not in part:
            continue
        kind, _, value = part.partition(":")
        kind, value = kind.strip(), value.strip()
        if not value or " " in value:
            # 词组形态（"a little bird told me"）对单词查词无用，且会撑大索引
            continue
        if kind == "0":
            base = value.lower()
        elif kind == "1":
            continue
        else:
            forms.append((value.lower(), word))
    return forms, base


# 「算不算中文」按**常用汉字区**（U+4E00–U+9FFF）判定。这个范围有**两个**消费方：
# 本模块的 has_cjk()，以及构建期自校验里的 SQLite GLOB 子句（自检脚本同款）。
# 两者必须**同源**：写死两份必然漂移，而漂移的后果很难看——Python 放行、SQL 判
# 不合格，于是构建当场失败，报的还是「例句中文侧不含汉字」这种看不出所以然的错。
# 实测踩过两次：一次是 `deprecate` 的中文侧 `±䁿`（U+407F，属 CJK 扩展 A）被过宽的
# has_cjk 留了下来；一次是 GLOB 写成 `[一-龥]`，上界 龥 是 U+9FA5 而不是 9FFF，
# U+9FA6–9FFF 的汉字会被 Python 放行、被 SQL 判成不合格。
#
# 此后范围曾在**本文件内**收成一份（CJK_START / CJK_END → CJK_GLOB_CLASS），
# 但那只解决了本模块内部的分裂：`app/core/db.py`、`repository.py`、`importer.py`
# 三处仍各自写着一份同样的字面量。现在进一步收敛到**全项目一份**——
# 范围定义在 `app/core/db.py`（运行时要用的模块，脚本不能反过来被 app 依赖），
# 本文件 import 它。脚本 import 应用代码在本项目有先例：上面就 import 了
# `app.providers.offline.OFFLINE_SCHEMA_VERSION`。
from app.core.db import CJK_CLASS, CJK_END, CJK_START, _CJK_RE  # noqa: E402

CJK_GLOB_CLASS = CJK_CLASS

# 「中文侧是不是假的」这句查询**就是校验契约本身**，所以单独提出来由构建期自校验与
# 自检脚本共用。共用文本的意义在于：测试能拿**真查询**去跑边界字符，验证它与
# has_cjk() 得出同一结论。若把查询内联回各自的调用处，测试只能去验一个跟真实校验
# 无关的复制品——而「测的是替身、本体没人管」正是本项目栽过的坑（见 check_trim
# 的旧版：基线目录压根不存在，校验永远通过）。表名固定为 examples。
FAKE_ZH_SQL = (
    "SELECT COUNT(*) FROM examples "
    f"WHERE sentence_zh <> '' AND NOT (sentence_zh GLOB '*{CJK_GLOB_CLASS}*')"
)


def has_cjk(text: str) -> bool:
    """文本里是否含常用汉字。

    Tatoeba 的中文侧并非都是中文：实测有英文原文重复、法文、以及纯文档编号
    （`A/CONF.189/PC.3/L.1/Add.1`）。**界面把这一侧标成「中文翻译」**，
    把英文/法文/编号摆在那里就是在撒一个小谎——用户会以为自己在读中文。
    没有中文就如实说没有，而不拿别的东西占位。
    """
    return bool(_CJK_RE.search(text or ""))


def sentence_uses(sentence: str, word: str) -> bool:
    """判断句子里是否真的用到了这个词（或它的屈折形式）。

    这一步不能省。源语料约有 9% 的配对是错的——同一个句子被挂到了多个
    毫不相关的词下（`I hope we'll be home for Christmas` 同时是 abiding /
    absorbing / accommodating / running 的例句）。把错的例句当成「该词的用法」
    展示出去，比不给例句更糟：用户会照着它记住一个不存在的搭配。

    匹配用「放宽的前缀」而不是严格整词：`accelerated` 的例句里写的是
    `accelerate`、`accessory` 的例句里写的是 `accessories`，这些是正常用法；
    而 `abiding` 的例句里连 `abid` 这个前缀都没有，那就是配错了。
    """
    if not sentence or not word:
        return False
    low = sentence.lower()
    if re.search(rf"\b{re.escape(word)}\b", low):
        return True
    tokens = re.findall(r"[a-z]+", low)
    if not tokens:
        return False
    # 短词按整词比，长词允许去掉至多 3 个字符的屈折后缀
    cut = min(len(word), max(4, len(word) - 3))
    prefix = word[:cut]
    return any(token.startswith(prefix) for token in tokens)


def build(
    source: Path,
    examples: Path | None,
    out: Path,
    tier: str,
    bnc_max: int,
    force: bool,
) -> int:
    if not source.exists():
        print(f"源数据不存在：{source}", file=sys.stderr)
        return 2
    if out.exists() and not force:
        print(f"输出已存在：{out}（加 --force 覆盖）", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(out))
    dst.executescript(DDL)

    condition = tier_condition(tier, bnc_max)
    rows = src.execute(
        "SELECT id, word, phonetic, definition, translation, pos, collins, oxford,"
        f" tag, bnc, frq, exchange FROM entries WHERE {condition}"
    )

    word_batch: list[tuple] = []
    form_batch: list[tuple] = []
    seen_words: set[str] = set()
    with_pos = with_en = with_phonetic = 0
    batch_limit = 5000

    def flush() -> None:
        nonlocal word_batch, form_batch
        if word_batch:
            dst.executemany(
                "INSERT OR IGNORE INTO dict(word, phonetic, pos, definition,"
                " translation, tag, collins, oxford, bnc, frq)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                word_batch,
            )
            word_batch = []
        if form_batch:
            dst.executemany(
                "INSERT OR IGNORE INTO forms(form, word) VALUES(?,?)", form_batch
            )
            form_batch = []

    for row in rows:
        word = (row["word"] or "").strip().lower()
        if not word or word in seen_words:
            continue
        seen_words.add(word)

        translation = clean_text(row["translation"])
        definition = clean_text(row["definition"])
        phonetic = (row["phonetic"] or "").strip()
        pos = extract_pos(translation, row["pos"] or "")

        word_batch.append(
            (
                word,
                phonetic,
                pos,
                definition,
                translation,
                (row["tag"] or "").strip(),
                int(row["collins"] or 0),
                int(row["oxford"] or 0),
                int(row["bnc"] or 0),
                int(row["frq"] or 0),
            )
        )
        with_en += 1 if definition else 0
        with_pos += 1 if pos else 0
        with_phonetic += 1 if phonetic else 0

        forms, base = parse_exchange(word, row["exchange"] or "")
        form_batch.extend(forms)
        if base and base != word:
            # 「本词是 base 的屈折形式」也要登记，否则原形没收录时这个词查不到
            form_batch.append((word, base))

        if len(word_batch) >= batch_limit:
            flush()
    flush()

    example_count = 0
    sentence_skipped = 0
    zh_cleared = 0
    if examples is not None and examples.exists():
        ex = sqlite3.connect(f"file:{examples.as_posix()}?mode=ro", uri=True)
        ex.row_factory = sqlite3.Row
        # 只保留词库中确实存在的词的例句：例句里还有大量屈折形式
        # （`abandoned`），它们的原形未必在 core 档里。
        batch: list[tuple] = []
        # 按 rank 升序：一个词可能有多条候选例句，源数据用 rank 标注优劣。
        # 不加这个排序会取到表中任意一条（实测曾取到与词无关的句子）。
        for row in ex.execute(
            "SELECT entry_word, normalized_word, sentence_en, sentence_zh, rank"
            " FROM examples ORDER BY normalized_word, rank"
        ):
            key = (row["normalized_word"] or row["entry_word"] or "").strip().lower()
            if not key or key not in seen_words:
                continue
            english = clean_text(row["sentence_en"])
            if not sentence_uses(english, key):
                sentence_skipped += 1
                continue
            chinese = clean_text(row["sentence_zh"])
            if chinese and not has_cjk(chinese):
                # 中文侧其实是英文原文、法文或文档编号。英文例句本身有效，保留；
                # 但不该把别的东西冒充成它的中文翻译——界面那一栏就叫「中」。
                chinese = ""
                zh_cleared += 1
            batch.append((key, english, chinese))
            if len(batch) >= batch_limit:
                dst.executemany(
                    "INSERT OR IGNORE INTO examples(word, sentence_en, sentence_zh)"
                    " VALUES(?,?,?)",
                    batch,
                )
                example_count += len(batch)
                batch = []
        if batch:
            dst.executemany(
                "INSERT OR IGNORE INTO examples(word, sentence_en, sentence_zh) VALUES(?,?,?)",
                batch,
            )
            example_count += len(batch)
        ex.close()

    # 源库自带 focus_word_forms（4.7 万条），比 exchange 更完整地覆盖
    # focus 词的派生形态。补进去，冲突时保留先写入的 exchange 结果。
    try:
        pairs = [
            (str(row["form"]).strip().lower(), str(row["word"]).strip().lower())
            for row in src.execute(
                "SELECT f.form AS form, e.word AS word FROM focus_word_forms f"
                " JOIN entries e ON e.id = f.entry_id"
            )
            if row["form"]
            and " " not in str(row["form"])
            and str(row["word"]).strip().lower() in seen_words
        ]
        dst.executemany("INSERT OR IGNORE INTO forms(form, word) VALUES(?,?)", pairs)
    except sqlite3.Error:
        pass  # 源库没有该表时静默跳过：形态索引的完整性不依赖它

    meta = {
        "schema_version": str(OFFLINE_SCHEMA_VERSION),
        "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tier": tier,
        "source_name": "ECDICT 1.0.28 (skywind3000/ECDICT)",
        "source_license": "MIT",
        "examples_source": "Tatoeba (CC-BY 2.0 FR)",
    }
    word_count = dst.execute("SELECT COUNT(*) FROM dict").fetchone()[0]
    form_count = dst.execute("SELECT COUNT(*) FROM forms").fetchone()[0]
    example_count = dst.execute("SELECT COUNT(*) FROM examples").fetchone()[0]
    meta.update(
        {
            "word_count": str(word_count),
            "form_count": str(form_count),
            "example_count": str(example_count),
        }
    )
    dst.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", meta.items())
    dst.commit()
    dst.execute("VACUUM")
    dst.commit()

    # **构建器必须自己断言产物。** DB 级自检守的是已入库的那份文件：改坏了
    # `clean_text()` 的解码步骤、而没人重建库，自检照样全绿——等真重建时才炸在
    # 用户机器上，且表现为「词典显示一堆 \n 乱码」。这条让改坏构建器当场失败。
    escaped = dst.execute(
        "SELECT COUNT(*) FROM dict WHERE "
        "instr(translation, char(92)||'n') > 0 OR instr(definition, char(92)||'n') > 0 "
        "OR instr(translation, char(92)||'r') > 0 OR instr(definition, char(92)||'r') > 0"
    ).fetchone()[0]
    # 中文侧要么是中文，要么为空。非中文的「中文翻译」会让界面撒谎。
    # 查询走 FAKE_ZH_SQL（与 has_cjk() 同源，见其定义处）。
    zh_fake = dst.execute(FAKE_ZH_SQL).fetchone()[0]
    dst.close()
    src.close()

    if escaped:
        print(
            f"构建失败：{escaped:,} 条释义仍含字面量转义（源语料的 \\n 未解码）。",
            file=sys.stderr,
        )
        print(
            "  后果：界面与导出的释义里会显示 \\n 这坨字符；词性正则的 \\b 边界也会失效，"
            "第一行之后的词性全部识别不到。",
            file=sys.stderr,
        )
        print("  请检查 clean_text() 的解码步骤。", file=sys.stderr)
        return 2

    if zh_fake:
        print(
            f"构建失败：{zh_fake:,} 条例句的「中文侧」不含任何汉字。",
            file=sys.stderr,
        )
        print(
            "  界面把这一侧标成中文翻译，摆着英文/法文/文档编号就是在撒谎。"
            "没有中文应清空，而不是拿别的东西占位（见 has_cjk()）。",
            file=sys.stderr,
        )
        return 2

    size_mb = out.stat().st_size / 1048576
    def pct(part: int) -> str:
        return f"{part / max(word_count, 1) * 100:.1f}%"

    print(f"离线词库已生成：{out}")
    print(f"  档位         {tier}")
    print(f"  词条         {word_count:,}")
    print(f"  屈折形态     {form_count:,}")
    print(f"  例句         {example_count:,}")
    if sentence_skipped:
        print(
            f"  （另有 {sentence_skipped:,} 条例句与词不匹配，已丢弃：源语料约 9% 的配对有误）"
        )
    if zh_cleared:
        print(
            f"  （另有 {zh_cleared:,} 条例句的中文侧其实是英文/法文/编号，已清空：不拿非中文冒充翻译）"
        )
    print(f"  音标覆盖     {pct(with_phonetic)}")
    print(f"  英文释义覆盖 {pct(with_en)}")
    print(f"  词性覆盖     {pct(with_pos)}")
    print(f"  文件体积     {size_mb:.1f} MB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Quill 离线词库")
    parser.add_argument("--source", type=Path, required=True, help="ECDICT 的 ecdict.sqlite")
    parser.add_argument("--examples", type=Path, default=None, help="例句库 examples.sqlite")
    parser.add_argument("--out", type=Path, required=True, help="输出的离线词库路径")
    parser.add_argument("--tier", choices=("core", "full"), default="core")
    parser.add_argument(
        "--bnc-max", type=int, default=50000, help="core 档收录的词频上限（默认 5 万）"
    )
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    args = parser.parse_args()
    return build(args.source, args.examples, args.out, args.tier, args.bnc_max, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
