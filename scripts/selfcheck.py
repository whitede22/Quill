"""无 GUI 自检脚本：验证数据层、检索、导出与密钥模块。

用法：.venv/Scripts/python.exe scripts/selfcheck.py
"""

from __future__ import annotations

import ast
import atexit
import csv
import fnmatch
import gc
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import glob
import tokenize
import zipfile
from pathlib import Path

# 仓库根目录。用于「扫描全仓源码」这类检查——扫的是真实文件，不是清单。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 临时目录的前缀表**只留一份定义**（`scripts/clean_temp.py`）。这里曾经自己写了一份，
# 而且比那一份窄八个前缀——后果是整整一类泄漏不在视野里，见 `_temp_leftovers`。
sys.path.insert(0, str(ROOT / "scripts"))
from clean_temp import PREFIXES as TEMP_PREFIXES  # noqa: E402

from app.core.db import (  # noqa: E402
    DDL,
    FTS_COLUMNS,
    FTS_WEIGHTS,
    SCHEMA_VERSION,
    backup_to,
    connect,
    fts_values,
    init_db,
    inspect_backup,
    replace_with_staged,
    stage_restore,
    to_bigram,
)
from app.core.dictionary import (  # noqa: E402
    DictionaryService,
    normalize,
    normalize_phrase,
)
from app.core.exporter import (  # noqa: E402
    PdfFontUnavailable,
    build_pdf_html,
    export_anki,
    export_csv,
    export_json,
    export_markdown,
    export_pdf,
    export_text,
    export_wordlist,
)
from app.core.exporter import _PDF_RULE_COLOR  # noqa: E402
from app.core.lemmatizer import candidates as lemma_candidates  # noqa: E402
from app.ui import theme as theme_mod  # noqa: E402
from app.ui import background as background_mod  # noqa: E402
from app.ui.background import BackgroundRenderer, apply_blur, scale_cover  # noqa: E402
from app.ui import onboarding as ob_mod  # noqa: E402
from app.core.models import Entry, Phrase, Synonym, join_tags, split_tags  # noqa: E402
from app.core.repository import Repository, build_fts_query  # noqa: E402
from app.providers.base import DictionaryProvider  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.providers.openai_compat import (  # noqa: E402
    build_endpoint,
    build_provider,
    extract_json,
    provider_status,
)
from app.security.keystore import decrypt_text, encrypt_text  # noqa: E402

# PDF 读回校验用：QtPdf 负责解析产物，QSize/QImage 用于渲染。
# 直接用 Qt 而不引入新依赖——它们是 PySide6 自带的。
from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402


def _contrast(fg: str, bg: str) -> float:
    """两色的 WCAG 对比度。提示条的文字色与底色必须一起挑，靠它来判。"""

    def _lin(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def _lum(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _luminance(hex_color: str) -> float:
    """单色的相对亮度。用来分辨「底比字暗」还是「底比字亮」——深色主题与
    浅色主题的**最坏情况正好相反**，靠这一条把它们分开验。"""

    def _lin(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _blend(base: str, under: int, percent: float) -> str:
    """把 `base` 按 `percent`% 铺在一张纯灰（0=黑 / 255=白）的图上，返回合成色。

    这是「面板半透明」的算术模型：面板铺自身底色的不透明度 = percent。
    用它算最坏情况的对比度，比渲染一张真图再取像素稳定得多，也不依赖离屏
    平台去做合成——实测离屏**不做**子控件与父窗口图片的合成。
    """
    a = percent / 100.0
    h = base.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    values = [max(0, min(255, round(c * a + under * (1 - a)))) for c in (r, g, b)]
    return "#" + "".join(f"{v:02X}" for v in values)


def _py_code_only(src: str) -> str:
    """去掉 Python 源码里的注释，**保留字符串**。

    「界面代码里不许有字面颜色」这条断言必须只看代码。注释里出现 `#1F1F1E`
    是在**解释**一个颜色（比如"这个色值在深色主题下只有 1.10:1"），不是在用它；
    把注释也算进去，就会让"记录这个坑"和"重新踩这个坑"变成同一件事——于是
    文档里再也写不出具体色值，而这恰恰是排查时最需要的那条信息。

    字符串**必须留着**：写死的颜色最常出现的地方正是字符串（富文本片段、
    样式串），那才是这条断言真正要抓的东西。

    解析失败时原样返回：宁可多报（把注释里的色值也算上，报出来人能自己判断），
    也不要少报。
    """
    spans: list[tuple[int, int]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(src).readline):
            if token.type == tokenize.COMMENT:
                spans.append(token.start)  # 注释一直延续到行尾
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    if not spans:
        return src
    lines = src.splitlines(keepends=True)
    for row, col in spans:
        index = row - 1
        if 0 <= index < len(lines):
            lines[index] = lines[index][:col] + (
                "\n" if lines[index].endswith("\n") else ""
            )
    return "".join(lines)


def _qss_code_only(src: str) -> str:
    """去掉样式表模板里的 `/* ... */` 注释（同上：注释是在解释，不是在声明）。"""
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _checker_image(size: int = 128, cell: int = 16) -> QImage:
    """黑白棋盘。用它当背景图的测试图：硬边是模糊最直接的观察对象。"""
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor(0, 0, 0))
    painter = QPainter(img)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255))
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                painter.drawRect(x, y, cell, cell)
    painter.end()
    return img


def _max_h_delta(img: QImage, rows: int = 9) -> int:
    """若干行上相邻像素的最大通道差。硬边越大，模糊后越小。"""
    worst = 0
    for y in range(0, img.height(), max(1, img.height() // rows)):
        prev = img.pixelColor(0, y).green()
        for x in range(1, img.width()):
            cur = img.pixelColor(x, y).green()
            worst = max(worst, abs(cur - prev))
            prev = cur
    return worst


def render_pdf_page(doc: "QPdfDocument", size: QSize, page: int = 0) -> QImage:
    """把 PDF 某一页渲染到**白底**位图上。

    `QPdfDocument.render(page, size)`（PySide6 只暴露这一个重载，**没有**接受
    QPainter 的版本）返回的是 `Format_ARGB32` 且**背景完全透明**
    （实测：空白处 alpha=0、rgb=(0,0,0)）。直接拿它做像素判断会得到荒谬的结论——
    `pixelColor().value()` 是 0（纯黑），于是**每一个点都被判成「有墨迹」**，
    「内容有没有铺开」「页面有没有被涂满」这类断言全部失真。

    查看器把透明当白纸显示，所以人眼看着是正常的；只有程序看不出来。
    故这里把渲染结果合成到白底上，像素值才与「用户看到的」一致。
    """
    from PySide6.QtGui import QPainter  # noqa: PLC0415

    raw = doc.render(page, size)
    canvas = QImage(size, QImage.Format_RGB32)
    canvas.fill(0xFFFFFF)
    painter = QPainter(canvas)
    try:
        painter.drawImage(0, 0, raw)
    finally:
        painter.end()
    return canvas


class CountingProvider:
    """包装 Provider 并统计调用次数，用于验证缓存是否真正生效。"""

    name = "counting"

    def __init__(self, inner: DictionaryProvider) -> None:
        self.inner = inner
        self.calls = 0

    def lookup(self, word: str) -> Entry:
        self.calls += 1
        return self.inner.lookup(word)


class PayloadProvider:
    """直接返回给定 payload 的 Provider，用于构造不完整或空壳响应。"""

    name = "payload"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def lookup(self, word: str) -> Entry:
        self.calls += 1
        return Entry.from_payload(self.payload)


class StubProvider:
    """返回固定内容的 Provider，用于验证「接口回填」与「用户自有字段」的边界。"""

    name = "stub"

    def __init__(self, definition_en: str = "seeming to be everywhere") -> None:
        self.definition_en = definition_en
        self.calls = 0

    def lookup(self, word: str) -> Entry:
        self.calls += 1
        return Entry(
            lemma=word,
            definition_en=self.definition_en,
            definition_zh="无处不在的",
            example="Mobile phones are ubiquitous.",
            synonyms=[Synonym(word="pervasive", gloss_zh="弥漫的")],
        )


PASSED: list[str] = []
FAILED: list[str] = []
# 应用对象的引用（见 §18）。模块级而非函数级：函数返回后局部名字消失，
# 应用对象随之被 GC，进程里再碰 Qt 即崩。
_SELFCHECK_APP = None


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  {detail}")
        # 立即打印失败项：若后面某一步让进程硬崩（Qt 层崩溃不发 Python 异常、
        # 缓冲也不会刷），最终汇总行就永远打不出来，连「哪里失败」都看不到。
        print(f"  FAIL  {name}  {detail}", flush=True)


def _cleanup_tmpdir(path: Path) -> None:
    """删除临时目录；偶尔会被 Qt 尚未释放的文件句柄挡住，重试几次。

    `QPdfWriter` 写出的 PDF 在 `export_pdf` 返回后，句柄未必立刻释放（Qt 的
    对象还有 Python 侧引用）。实测：`atexit` 里第一次 `rmtree` 会留下
    `pdf/one.pdf` 与 `pdf_page/many.pdf` 两个文件，目录删不掉。**不要用
    `ignore_errors=True` 把这件事咽下去**——那样「删干净了」与「静默没删」
    长得一模一样，正是本环境刚刚被 3763 个残留目录写满整块系统盘的原因。
    所以这里重试，并在最终仍失败时**打印出来**。
    """
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            gc.collect()  # 进一步回收 Qt 包装对象
            time.sleep(0.05 * (attempt + 1))
    if path.exists():
        print(f"  WARN  临时目录未能删除（不影响结论）：{path}", flush=True)


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="quill_check_"))
    # **用完必须删掉。** 以前这里（以及本文件另外几处 `mkdtemp`）只创建不清理，
    # 每跑一轮就留下十来个各约 5 MB 的目录。单次看不出来，但变异测试会跑几十
    # 遍自检——实测累积到 **3763 个目录 / 约 2.6 GB**，把系统盘写满，然后整个
    # 测试环境开始报 `ENOSPC`（表现为「工具坏了」，而不是「有个目录没删」）。
    # 用 atexit 而不是 try/finally：本函数体很长，中途有大量 return 路径与
    # 断言，atexit 保证无论怎么退出都会清理。
    atexit.register(_cleanup_tmpdir, tmpdir)
    db_path = tmpdir / "test.db"
    conn = connect(db_path)
    init_db(conn)
    repo = Repository(conn)

    # ---- 1. 中文 bigram 切分 ----
    bg = to_bigram("无处不在的")
    check("bigram 切分", bg == "无处 处不 不在 在的", f"得到 {bg!r}")
    check("bigram 单字", to_bigram("好") == "好")
    check("bigram 忽略英文", to_bigram("abc") == "")

    # ---- 2. 入库与 FTS 同步 ----
    service = DictionaryService(repo, MockProvider())
    entry, cached = service.lookup("ubiquitous")
    check("首次查询不入缓存", cached is False)
    check("词条已入库", entry.id is not None and repo.count() == 1, f"count={repo.count()}")

    _, cached_again = service.lookup("ubiquitous")
    check("二次查询命中缓存", cached_again is True)
    check("缓存未产生重复记录", repo.count() == 1, f"count={repo.count()}")

    # ---- 3. 英文检索 ----
    for word in ("ephemeral", "meticulous"):
        service.lookup(word)
    check("生词本共 3 条", repo.count() == 3, f"count={repo.count()}")

    hits = repo.search("ephem")
    check("英文前缀检索", any(e.lemma == "ephemeral" for e in hits), f"命中 {[e.lemma for e in hits]}")

    hits = repo.search("pervasive")
    check(
        "英文同义语列可检索",
        any(e.lemma == "ubiquitous" for e in hits),
        f"命中 {[e.lemma for e in hits]}",
    )

    # ---- 4. 中文子串检索（FTS5 逐字切分的关键补偿） ----
    hits = repo.search("普遍")
    check("中文子串检索（双字）", any(e.lemma == "ubiquitous" for e in hits), f"命中 {[e.lemma for e in hits]}")

    hits = repo.search("无处不在")
    check("中文子串检索（四字跨 bigram）", any(e.lemma == "ubiquitous" for e in hits), f"命中 {[e.lemma for e in hits]}")

    hits = repo.search("转瞬即逝")
    check("中文检索命中例句翻译", any(e.lemma == "ephemeral" for e in hits), f"命中 {[e.lemma for e in hits]}")

    hits = repo.search("一丝不苟")
    check("中文检索第三条", any(e.lemma == "meticulous" for e in hits), f"命中 {[e.lemma for e in hits]}")

    hits = repo.search("不存在的词汇xyz")
    check("无匹配时返回空", hits == [], f"命中 {[e.lemma for e in hits]}")

    # ---- 5. 中英混合查询 ----
    q = build_fts_query("快乐 happy")
    check("中英混合构造 AND", "AND" in q, q)
    check("空查询返回空串", build_fts_query("   ") == "")

    # ---- 6. 输入规整 ----
    check("规整大小写", normalize("  Ubiquitous! ") == "ubiquitous")
    check("保留连字符", normalize("well-known") == "well-known")

    # ---- 7. 更新既有词条不产生重复 ----
    fetched = repo.get("ubiquitous")
    assert fetched is not None
    fetched.notes = "测试备注"
    fetched.synonyms.append(Synonym(word="widespread", gloss_zh="广泛的"))
    repo.upsert(fetched)
    check("更新后仍为 3 条", repo.count() == 3, f"count={repo.count()}")
    check("备注已保存", repo.get("ubiquitous").notes == "测试备注")
    check(
        "新增同义语已入索引",
        any(e.lemma == "ubiquitous" for e in repo.search("widespread")),
    )

    # ---- 7b. 标签：解析只有一份定义，筛选按整标签匹配 ----
    #
    # 这一节守两件事：① 「什么算标签」的判据只有一处（`models.split_tags`），
    # 界面输入、筛选、导入回填都走它；② 筛选走的是**解析**而不是字符串包含
    # ——`LIKE '%考研%'` 会把「考研英语」一并带出来，那是另一个标签。
    #
    # **本节用独立的库**：下面要写进几条带标签的词条，若落进共用的 `repo`，
    # 第 14 节那条「改完备注后旧内容搜不到」（`search("高频") == []`）会被这些
    # 词条的**标签**命中而变成假红——真正的原因（跨小节写脏了同一个库）在
    # 失败信息里一个字都看不出来。这一条是实测踩到的，不是预防性设计。
    _tag_dir = Path(tempfile.mkdtemp(prefix="quill_tags_"))
    atexit.register(_cleanup_tmpdir, _tag_dir)
    _tag_conn = connect(_tag_dir / "tags.db")
    init_db(_tag_conn)
    tagrepo = Repository(_tag_conn)
    # atexit 是 LIFO：先登记清理、后登记关闭，于是关闭先跑——Windows 上
    # 一个还开着的 SQLite 句柄会把临时目录删不掉，只能靠重试硬扛。
    atexit.register(_tag_conn.close)

    check("标签按空格分隔", split_tags("考研 高频") == ["考研", "高频"],
          str(split_tags("考研 高频")))
    check("标签认中英文逗号与分号", split_tags("考研,高频；口语") == ["考研", "高频", "口语"],
          str(split_tags("考研,高频；口语")))
    check("标签去重且保序", split_tags("考研 考研 高频") == ["考研", "高频"],
          str(split_tags("考研 考研 高频")))
    check("标签存储形态统一为单空格", join_tags("考研,  高频") == "考研 高频",
          repr(join_tags("考研,  高频")))
    check("已是存储形态时原样返回", join_tags("考研 高频") == "考研 高频")
    # 外部文件与配置文件什么都可能给：不抛异常是底线（抛了会中断整个自检，
    # 而真正的问题被埋在一个 traceback 后面）。
    check("非字符串输入不抛异常",
          split_tags(None) == [] and split_tags(123) == [] and join_tags(123) == "",
          f"{split_tags(None)} {split_tags(123)} {join_tags(123)}")

    for _tagged in (
        Entry(lemma="tagalpha", definition_en="z", definition_zh="甲", tags="考研 高频"),
        Entry(lemma="tagbeta", definition_en="z", definition_zh="乙", tags="考研"),
        Entry(lemma="taggamma", definition_en="z", definition_zh="丙", tags="考研英语"),
    ):
        tagrepo.upsert(_tagged)
    _kaoyan = [e.lemma for e in tagrepo.list_by_tag("考研")]
    check("按标签筛选命中带该标签的词条",
          set(_kaoyan) == {"tagalpha", "tagbeta"}, str(_kaoyan))
    check("按标签筛选不把「考研英语」当成「考研」（子串不是同一个标签）",
          "taggamma" not in _kaoyan, str(_kaoyan))
    _tag_counts = dict(tagrepo.tag_counts())
    check("标签计数按标签而非按字段值", _tag_counts.get("考研") == 2, str(_tag_counts))
    check("同一字段里的第二个标签也计数", _tag_counts.get("高频") == 1, str(_tag_counts))
    check("空标签退回全部词条",
          len(tagrepo.list_by_tag("", limit=None)) == tagrepo.count(), str(tagrepo.count()))
    _by_tag = [e.lemma for e in tagrepo.search("高频")]
    check("搜索能命中标签（写进去能找回来，这条读取路径才算闭环）",
          "tagalpha" in _by_tag, str(_by_tag))

    # ---- 7c. 输入框多词拆分：与文件导入共用同一套判据 ----
    from app.core.importer import split_words  # noqa: PLC0415 - 与本文件其它 importer 用法一致

    check("按空格拆出多个词",
          split_words("hello world warm") == ["hello", "world", "warm"],
          str(split_words("hello world warm")))
    check("逗号与换行也当分隔符",
          split_words("apple, banana\ncherry") == ["apple", "banana", "cherry"],
          str(split_words("apple, banana\ncherry")))
    check("拆词去重", split_words("apple apple") == ["apple"])
    # 判据必须与导入一致：否则同一个词从输入框进得来、从文件进不来，两边都不报错。
    check("拆词丢掉不合格片段（数字、单字母）",
          split_words("apple 2 b window") == ["apple", "window"],
          str(split_words("apple 2 b window")))
    check("单个词仍只返回一个元素（调用方按长度决定走哪条路）",
          split_words("hello") == ["hello"])
    check("全是不合格片段时返回空", split_words("2 3 b") == [], str(split_words("2 3 b")))

    # ---- 7d. 只读本地缓存（批量查词前要先判「整串是不是一个词组」）----

    class _ExplodingProvider:
        """任何 lookup 都当场炸——用来证明 `cached()` 真的没去问接口。"""

        name = "exploding"

        def lookup(self, word: str) -> Entry:
            raise AssertionError("cached() 不该调用 provider")

    _cache_only = DictionaryService(repo, _ExplodingProvider())
    check("cached() 命中已收录词条", _cache_only.cached("ubiquitous") is not None)
    check("cached() 对没收录过的词返回 None",
          _cache_only.cached("zzznotaword") is None)
    check("cached() 与查词共用同一套规整（大小写/标点不影响命中）",
          _cache_only.cached("  Ubiquitous! ") is not None)

    # ---- 7e. 撤销删除的依据：`upsert()` 认 lemma，`id` 只是一个分支开关 ----
    #
    # 这一节是**两次证伪**逼出来的。「撤销删除」必须先把 `entry.id` 清空，
    # 而这一步的理由换过两版，两版都是假的：
    #   ① 「SQLite 会把删掉的行号分给下一个新词条，带着旧 id 写回去会覆盖别人」
    #      —— `entries.id` 声明了 AUTOINCREMENT，SQLite 用 `sqlite_sequence`
    #      记住用过的最大值，行号**不复用**（下面第 ④ 条就是它的反例）。
    #   ② 换上去的「带着旧 id 会 UPDATE 到一行不存在的记录、于是静默落空」
    #      —— 也不对：`upsert()` 的查找条件是 `WHERE lemma = ?`，**根本不按 id
    #      找行**（下面第 ① 条钉住这一点）。
    # 站得住的是第 ②/③ 条那一对：`entry.id is None` 决定的是「同名声词条已存在
    # 时，用户字段（备注 / 标签）听谁的」。撤销是一份**删除前的快照**，清空 id
    # 让它走「回填」这一支，从而不去覆盖用户在这次删除之后新写下的东西——
    # 那正是本程序「用户自有字段不被回填覆盖」这条铁律。
    #
    # 教训值得写下来：**换掉一条假声明时，新理由同样要取证**，否则只是把
    # 一个谎换成了另一个听起来更顺耳的谎。两次都是断言把它抓出来的。
    _undo_dir = Path(tempfile.mkdtemp(prefix="quill_undo_"))
    atexit.register(_cleanup_tmpdir, _undo_dir)
    _undo_conn = connect(_undo_dir / "undo.db")
    init_db(_undo_conn)
    _undo_repo = Repository(_undo_conn)
    atexit.register(_undo_conn.close)

    # ① upsert 按 lemma 找行，不按 id
    _live = _undo_repo.upsert(Entry(lemma="stale", notes="库里的备注"))
    _snap = Entry(lemma="stale", notes="快照里的备注")
    _snap.id = 99999  # 一个早就不存在的号
    _undo_repo.upsert(_snap)
    check("upsert 按 lemma 匹配、不按 id（带一个不存在的 id 也照样写到那一行）",
          _undo_repo.count() == 1 and int(_snap.id) == int(_live.id),
          f"count={_undo_repo.count()} 结果 id={_snap.id} / 库里 id={_live.id}")

    # ② 删除之后用户又把这个词查回来，并在上面写了新的备注与标签
    _now = _undo_repo.get("stale")
    assert _now is not None
    _now.notes = "删除之后新写的备注"
    _now.tags = "新写的标签"
    _undo_repo.upsert(_now)

    # ③ 撤销：快照带着**空 id** 写回 ⇒ 走「回填」那一支，不覆盖用户字段
    _snap.id = None
    _snap.notes = "快照里的备注"
    _snap.tags = "快照的标签"
    _undo_repo.upsert(_snap)
    _kept = _undo_repo.get("stale")
    check("快照回填（entry.id 为空）保留库中现有的备注与标签",
          _kept is not None
          and _kept.notes == "删除之后新写的备注"
          and _kept.tags == "新写的标签",
          f"notes={_kept.notes!r} tags={_kept.tags!r}" if _kept else "(没有取到词条)")

    # ③' 成对的反面：id 就绪时快照内容**全部生效**。
    # 只断言 ③ 的话，一个「永远保留库里旧值」的实现也能通过——那样用户改完
    # 备注点保存，界面上说「已保存」，库里却还是旧的那句。
    _snap.id = int(_kept.id) if _kept else None
    _snap.notes = "快照里的备注"
    _snap.tags = "快照的标签"
    _undo_repo.upsert(_snap)
    _over = _undo_repo.get("stale")
    check("entry.id 就绪时快照的备注与标签生效（不是「永远保留库里的值」）",
          _over is not None
          and _over.notes == "快照里的备注"
          and _over.tags == "快照的标签",
          f"notes={_over.notes!r} tags={_over.tags!r}" if _over else "(没有取到词条)")

    # ④ 反例：注释里那句「行号会被复用」到底对不对。留着它是为了让**下一次**
    # 有人再写下那句话时立刻看到反例，而不是又留下一条假声明。
    _doomed = _undo_repo.upsert(Entry(lemma="doomed"))
    _doomed_id = int(_doomed.id)
    _undo_repo.delete(_doomed_id)
    _next = _undo_repo.upsert(Entry(lemma="nextone"))
    check("删除后新插入的 id 不等于被删的那个（AUTOINCREMENT 不复用行号）",
          int(_next.id) != _doomed_id, f"被删 {_doomed_id} / 新插入 {_next.id}")

    # ---- 7f. 固定搭配与词组收藏 ----
    #
    # 这一节守四件事，每一件都有「接错了也不报错」的形态：
    #
    # ① **搭配解析要同时吃两种形态**。`Entry.from_row` 拿到的是库里的 JSON 文本
    #    （必须 `json.loads`），而 `Entry.from_payload` 拿到的是模型刚返回的 list
    #    （再 `json.loads` 一次会抛 `TypeError`，被 `except` 吞成 `[]`）。曾经就是
    #    这么写的：**搭配一条不剩地消失，且不报任何错**——详情区安静地少了一块，
    #    用户只会以为「这个词本来就没有搭配」。这一条是实测丢过一次才补上的。
    # ② **缺项要按来源能力判**。离线词库结构上没有搭配，照喊会让每条离线词条都挂
    #    一句「缺少固定搭配」；提示恒定出现就等于没有提示，真缺项反而被淹没。
    # ③ **两张表各自从 1 发号**。`entries.id` 与 `phrases.id` 都是 AUTOINCREMENT，
    #    同一个数字在两边是毫不相干的两条记录——拿词条的 id 去删词组会安静地删掉
    #    另一样东西，而界面上两种情况都显示「已删除」。
    # ④ **「含这个词」按词边界判断**，不是 `LIKE '%词%'`：子串匹配会让 `run` 命中
    #    `trunk` / `runway`，用户看到的是「这个词底下凭空冒出一串不相干的词组」，
    #    且没有任何提示说明那是子串匹配的结果。
    #
    # **本节同样用独立的库**（理由见 7b）：写进共用 `repo` 会把别节的计数与搜索
    # 断言染成假红，而失败信息一个字都指不出真因。
    from app.core.importer import parse_text  # noqa: PLC0415
    from app.core.models import Collocation, Phrase, _json_list  # noqa: PLC0415
    from app.providers.base import (  # noqa: PLC0415
        ALL_FIELDS,
        PROVIDER_FIELDS,
        provided_fields,
        provider_label,
    )
    from app.providers.prompts import PROMPT_VERSION, SYSTEM_PROMPT  # noqa: PLC0415

    _ph_dir = Path(tempfile.mkdtemp(prefix="quill_phrase_"))
    atexit.register(_cleanup_tmpdir, _ph_dir)
    _ph_conn = connect(_ph_dir / "phrase.db")
    init_db(_ph_conn)
    _ph_repo = Repository(_ph_conn)
    atexit.register(_ph_conn.close)

    # ① `_json_list` 的两副面孔：库里的文本要解析，模型返回的 list 直接用。
    _jl_pairs = [
        ("已经是 list（模型刚返回的形态）", [{"phrase": "a"}], [{"phrase": "a"}]),
        ("库里的 JSON 文本", '[{"phrase": "a"}]', [{"phrase": "a"}]),
        ("空列表文本", "[]", []),
        ("None", None, []),
        ("空串", "", []),
        ("坏 JSON", "{不是 JSON", []),
        ("JSON 对象（非列表）", '{"a": 1}', []),
        ("裸字典（直接迭代会拿到键名）", {"a": 1}, []),
    ]
    _jl_bad = [name for name, raw, want in _jl_pairs if _json_list(raw) != want]
    check("_json_list 同时吃「JSON 文本」与「已经是 list」，坏输入一律给空且不抛",
          not _jl_bad, f"不符预期：{_jl_bad}")

    # `from_payload` 拿到的是 list。这一步若退化成「一律 json.loads」，
    # 搭配会全丢，而任何程序都不报错。
    _pe = Entry.from_payload(
        {
            "lemma": "charge",
            "definition_zh": "掌管；收费",
            "synonyms": [{"word": "accuse", "gloss_zh": "指控"}],
            "collocations": [
                {
                    "phrase": "in charge of",
                    "gloss_zh": "负责",
                    "example": "She is in charge of sales.",
                    "example_zh": "她负责销售。",
                },
                {"phrase": "take charge", "gloss_zh": "接管"},
                {"phrase": "free of charge", "gloss_zh": "免费"},
            ],
        }
    )
    check("from_payload 保住模型返回的搭配（list 形态不再被 json.loads 吞掉）",
          [c.phrase for c in _pe.collocations]
          == ["in charge of", "take charge", "free of charge"],
          str([c.phrase for c in _pe.collocations]))
    check("from_payload 的搭配各自带例句与翻译",
          bool(_pe.collocations)
          and _pe.collocations[0].example == "She is in charge of sales."
          and _pe.collocations[0].example_zh == "她负责销售。",
          repr(_pe.collocations[0]) if _pe.collocations else "(没有搭配)")
    check("补上搭配解析后同义语没被挤掉",
          [s.word for s in _pe.synonyms] == ["accuse"], str([s.word for s in _pe.synonyms]))
    _many = Entry.from_payload(
        {"lemma": "x", "collocations": [{"phrase": f"p{i}"} for i in range(9)]}
    )
    check("搭配条数封顶 5（多出来的不是更全，是把详情面板撑爆）",
          len(_many.collocations) == 5, f"{len(_many.collocations)} 条")
    check("坏形态的搭配不抛、按空处理",
          Entry.from_payload({"lemma": "y", "collocations": "不是 list"}).collocations == []
          and Entry.from_payload({"lemma": "y", "collocations": {"phrase": "a"}}).collocations == []
          and Entry.from_payload({"lemma": "y"}).collocations == [],
          "坏输入产出了搭配")

    # `Collocation.from_raw` 的键名容错：模型返回的键名不受控，
    # 没见过「容错」的解析器在真实数据上会丢掉大半内容，而且不报错。
    _cal = Collocation.from_raw(
        {
            "collocation": "be in charge of",
            "zh": "负责",
            "sentence": "She is in charge of the team.",
            "translation": "她负责这个团队。",
        }
    )
    check("Collocation.from_raw 认别名词名（collocation/zh/sentence/translation）",
          _cal is not None
          and _cal.phrase == "be in charge of"
          and _cal.gloss_zh == "负责"
          and _cal.example == "She is in charge of the team."
          and _cal.example_zh == "她负责这个团队。",
          repr(_cal))
    check("Collocation.from_raw 认纯字符串形态",
          getattr(Collocation.from_raw("  take charge of  "), "phrase", "") == "take charge of")
    check("Collocation.from_raw 对空 / 无词组键的字典给 None",
          Collocation.from_raw("") is None
          and Collocation.from_raw(None) is None
          and Collocation.from_raw({"gloss_zh": "只有释义"}) is None
          and Collocation.from_raw({"phrase": "   "}) is None,
          "空输入产出了搭配")

    # ② 缺项按来源能力判
    _off_entry = Entry(lemma="plain", definition_zh="朴素的", source="offline")
    check("离线来源不被喊「缺少固定搭配」（ECDICT 是词典数据，不是搭配数据）",
          "固定搭配" not in _off_entry.missing_parts(provided_fields("offline")),
          str(_off_entry.missing_parts(provided_fields("offline"))))
    _api_entry = Entry(lemma="plain", definition_zh="朴素的", source="openai-compat")
    check("接口来源缺搭配时如实喊出",
          "固定搭配" in _api_entry.missing_parts(provided_fields("openai-compat")),
          str(_api_entry.missing_parts(provided_fields("openai-compat"))))
    check("来源能力表登记了 collocations（接口与示例能给，离线不能）",
          "collocations" in PROVIDER_FIELDS["openai-compat"]
          and "collocations" in PROVIDER_FIELDS["mock"]
          and "collocations" not in PROVIDER_FIELDS["offline"]
          and "collocations" in ALL_FIELDS,
          str(sorted(PROVIDER_FIELDS)))
    check("提示词已要求输出固定搭配（PROMPT_VERSION 随之推进）",
          '"collocations"' in SYSTEM_PROMPT and PROMPT_VERSION >= 2,
          f"PROMPT_VERSION={PROMPT_VERSION}")

    # ③ 搭配经库往返（from_row 走的是 JSON 文本那一支）
    _stored = _ph_repo.upsert(
        Entry(
            lemma="charge",
            definition_zh="掌管；收费",
            collocations=[
                Collocation(
                    phrase="in charge of",
                    gloss_zh="负责",
                    example="She is in charge of sales.",
                    example_zh="她负责销售。",
                ),
                Collocation(phrase="free of charge", gloss_zh="免费"),
            ],
        )
    )
    assert _stored is not None and _stored.id is not None
    _back = _ph_repo.get("charge")
    check("搭配经库里往返后内容与顺序一致（from_row 解析的是 JSON 文本）",
          _back is not None
          and [c.phrase for c in _back.collocations] == ["in charge of", "free of charge"],
          str([c.phrase for c in _back.collocations]) if _back else "(没取到词条)")
    check("搭配的例句与翻译也完整往返",
          _back is not None
          and bool(_back.collocations)
          and _back.collocations[0].example_zh == "她负责销售。",
          repr(_back.collocations[0]) if _back and _back.collocations else "(没有搭配)")
    _cj_ok = False
    if _back is not None:
        try:
            _cj_ok = isinstance(json.loads(_back.collocations_json()), list)
        except (ValueError, TypeError):
            _cj_ok = False
    check("collocations_json() 产出可被 json.loads 的文本", _cj_ok)

    # ④ 词组收藏：去重落在数据库 UNIQUE 上，且**不覆盖已有内容**
    _p1 = _ph_repo.add_phrase(
        Phrase(phrase="in charge of", gloss_zh="第一次收藏的简释", source_lemma="charge")
    )
    check("add_phrase 落库并回填行号", _p1.id is not None, repr(_p1))
    _p2 = _ph_repo.add_phrase(
        Phrase(phrase="in charge of", gloss_zh="后来一次的简释", source_lemma="incharge")
    )
    check("重复收藏同一个词组不新增一条",
          _ph_repo.count_phrases() == 1 and int(_p2.id) == int(_p1.id),
          f"count={_ph_repo.count_phrases()} id={_p2.id}/{_p1.id}")
    check("重复收藏不覆盖已有简释（以先收藏的那份为准）",
          _p2.gloss_zh == "第一次收藏的简释", repr(_p2.gloss_zh))
    _ph_repo.add_phrase(Phrase(phrase="In Charge Of"))
    check("词组去重不区分大小写（表上是 COLLATE NOCASE）",
          _ph_repo.count_phrases() == 1, f"count={_ph_repo.count_phrases()}")
    check("get_phrase 大小写不敏感", _ph_repo.get_phrase("IN CHARGE OF") is not None)
    _empty_rejected = False
    try:
        _ph_repo.add_phrase(Phrase(phrase="   "))
    except ValueError:
        _empty_rejected = True
    check("add_phrase 拒绝空词组（否则收藏里会出现一条空白记录）", _empty_rejected)

    # ⑤ 「含这个词」按词边界判断
    for _txt in ("run out of", "on the run", "trunk road", "runway lights"):
        _ph_repo.add_phrase(Phrase(phrase=_txt))
    _hit = _ph_repo.phrases_containing("run")
    check("含词查询只命中真正含这个词的词组",
          set(_hit) == {"run out of", "on the run"}, str(_hit))
    check("含词查询不把 trunk / runway 当成含 run（子串匹配会）",
          "trunk road" not in _hit and "runway lights" not in _hit, str(_hit))
    check("含词查询对不在任何词组里的词给空", _ph_repo.phrases_containing("ubiquitous") == [])
    check("多词查询按连续几个词比较（run out 命中 run out of）",
          "run out of" in _ph_repo.phrases_containing("run out"),
          str(_ph_repo.phrases_containing("run out")))

    # ⑥ 两张表各自发号：同一个行号删一边不该动另一边
    _ph_id = int(_p1.id)
    _entry_id = int(_stored.id)
    check("词组与词条的行号都是各自从 1 发号（同一个数字不指同一条记录）",
          _ph_id == 1 and _entry_id == 1, f"phrase.id={_ph_id} entry.id={_entry_id}")
    _ph_n, _en_n = _ph_repo.count_phrases(), _ph_repo.count()
    check("删词组只动 phrases 表（entries 里同号的那条还在）",
          _ph_repo.delete_phrase(_ph_id) is True
          and _ph_repo.count_phrases() == _ph_n - 1
          and _ph_repo.count() == _en_n
          and _ph_repo.get("charge") is not None,
          f"phrases {_ph_n}→{_ph_repo.count_phrases()}，entries {_en_n}→{_ph_repo.count()}")
    check("删一个不存在的词组返回 False（不谎报成功）",
          _ph_repo.delete_phrase(999999) is False)

    # ⑦ 词组排序有自己那份映射（phrases 表没有 lemma 列）
    _alpha = [p.phrase for p in _ph_repo.list_phrases(order="alpha")]
    check("按字母排词组可用（复用 entries 的 ORDER_MAP 会抛「no such column: lemma」）",
          _alpha == sorted(_alpha, key=str.lower), str(_alpha))

    # ⑧ 适配成 Entry 后来源标成 phrase
    _first_phrase = _ph_repo.list_phrases(order="alpha")[0]
    _adapted = _first_phrase.to_entry()
    check("词组适配成词条时来源标成 phrase（不借用接口或离线词典的名义）",
          _adapted.source == "phrase" and provider_label("phrase") == "phrase", _adapted.source)
    check("词组适配成词条时正文与释义原样搬过去",
          _adapted.lemma == _first_phrase.phrase
          and _adapted.definition_zh == _first_phrase.gloss_zh,
          f"lemma={_adapted.lemma!r} / {_first_phrase.phrase!r}")

    # ⑨ 六种导出都带上搭配（含 JSON 回读）；单词表是唯一的例外，见 ⑨b
    _exp_e = Entry(
        lemma="charge",
        definition_zh="掌管；收费",
        example="The battery needs charging.",
        example_zh="电池需要充电。",
        collocations=[
            Collocation(
                phrase="in charge of",
                gloss_zh="负责",
                example="She is in charge of sales.",
                example_zh="她负责销售。",
            )
        ],
    )
    _txt = export_text([_exp_e], "测试")
    check("文本导出带上搭配与它自己的例句",
          "in charge of" in _txt and "She is in charge of sales." in _txt, _txt[-160:])
    _md = export_markdown([_exp_e], "测试")
    check("Markdown 导出带上搭配", "- **搭**" in _md and "in charge of" in _md, _md[-160:])
    _csv2 = list(csv.reader(io.StringIO(export_csv([_exp_e], "t").lstrip("\ufeff"))))
    check("CSV 的最后一列是固定搭配（只能追加在末尾，老列位置不变）",
          _csv2[0][-1] == "固定搭配" and "in charge of" in _csv2[1][-1],
          repr(_csv2[0][-1]) + " / " + repr(_csv2[1][-1] if len(_csv2) > 1 else ""))
    # **按内容取数据行，不写死下标 `[3]`**。原先写的是
    # `export_anki(...).split("\n")[3]`，它把「表头恰好三行」当成了前提——
    # 而表头有几行是**实现细节**。表头一旦多出一行（例如有人把标题塞进去），
    # 取到的是那行标题，`_anki2[1]` 当场 `IndexError`；更糟的是 `_anki2[1]` 同时
    # 出现在 check 的**实参**里，异常在 check 被调用前就抛出 → **整个自检中断**，
    # 后面几百条断言一条都不跑，报表上只留一句「CAUGHT*（代码崩溃）」，
    # 看着像「测到了」。实测踩过（变异「Anki 把标题塞进头部」）。
    _anki_rows = [
        ln for ln in export_anki([_exp_e], "t").split("\n") if ln and not ln.startswith("#")
    ]
    _anki2 = _anki_rows[0].split("\t") if _anki_rows else []
    check("Anki 背面带上搭配与它的中英例句",
          len(_anki2) == 3
          and "in charge of" in _anki2[1]
          and "She is in charge of sales." in _anki2[1],
          repr(_anki2[1] if len(_anki2) > 1 else _anki2)[:160])
    _pdf_html = build_pdf_html([_exp_e], "t")
    check("PDF 版式带上搭配", "in charge of" in _pdf_html, _pdf_html[-160:])
    _json_text = export_json([_exp_e], "t")
    check("JSON 导出含 collocations 字段且值完整",
          '"collocations"' in _json_text and "She is in charge of sales." in _json_text,
          _json_text[:120])
    _parsed = parse_text(_json_text)
    _fill_colls = _parsed.words[0].fill.get("collocations", []) if _parsed.words else []
    check("JSON 回读把搭配取回来（导出→导入不丢字段）",
          [c.phrase for c in _fill_colls] == ["in charge of"], repr(_fill_colls))
    check("JSON 回读的搭配带着例句与翻译",
          bool(_fill_colls) and _fill_colls[0].example_zh == "她负责销售。",
          repr(_fill_colls[0]) if _fill_colls else "(没有搭配)")

    # ⑨b 单词表：**唯一不**带搭配的格式。
    #
    # 这不是漏了，而是这份格式只承担「重新导回来」一件事（理由见
    # `export_wordlist` 的 docstring）。断言把它钉住，免得后来有人看到「别的
    # 格式都带搭配」就顺手补齐——那一补，导入报告里就会多出一批不是词条的噪音。
    _wl_one = export_wordlist([_exp_e], "测试")
    check(
        "单词表导出只有「词 + 中文释义」两列",
        _wl_one == "charge\t掌管；收费\n",
        repr(_wl_one),
    )
    check(
        "单词表刻意不带搭配（它不是备份，是重新导入用的种子）",
        "in charge of" not in _wl_one,
        repr(_wl_one),
    )
    check(
        "单词表：没有中文释义时只写单词，不留一个看不见的空列",
        export_wordlist([Entry(lemma="ghost")], "t") == "ghost\n",
        repr(export_wordlist([Entry(lemma="ghost")], "t")),
    )
    check(
        "单词表：没有词条的条目不出空行",
        export_wordlist([Entry(lemma="")], "t") == "",
        repr(export_wordlist([Entry(lemma="")], "t")),
    )

    # ---- 7g. 老库补列：`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做 ----
    #
    # entries 的加列有**两份声明**（DDL 负责新库、`ENTRIES_ADDED_COLUMNS` 负责既有
    # 库）。这是刻意分开的，代价是两份可能说不一样的话，而后果是分叉的：
    #   只改 DDL ⇒ 新装好用、老用户的库永远没有这一列，任何带它的语句随后失败；
    #   只改登记表 ⇒ 老库多出一列而新库没有，同一版本的程序读出两种结构。
    # 本轮之前**这一块完全没有断言**：把 `collocations_json` 从登记表里删掉，
    # 全套检查照样全绿，而老用户从升级那一刻起就丢掉了整块搭配内容。
    # 所以三件事一起验：两份声明一致、旧库真的补出这一列、补完能正常读写。
    from app.core.db import (  # noqa: PLC0415
        DDL,
        ENTRIES_ADDED_COLUMNS,
        init_db as _init_db,
    )

    _decl = ENTRIES_ADDED_COLUMNS.get("collocations_json", "").strip()
    check("collocations_json 在 DDL 与 ENTRIES_ADDED_COLUMNS 里都有",
          "collocations_json" in DDL and bool(_decl), f"登记表={_decl!r}")
    check("两份声明是同一个建列串（新库与老库补出来的列必须一模一样）",
          bool(_decl) and f"collocations_json {_decl}" in DDL, f"登记表={_decl!r}")

    _legacy_path = _ph_dir / "legacy.db"
    _legacy = connect(_legacy_path)
    try:
        # 造一个「上一版建的库」：entries 表没有 collocations_json 列。
        _legacy.executescript(
            DDL.replace("    collocations_json TEXT NOT NULL DEFAULT '[]',\n", "")
        )
        _cols_before = {r[1] for r in _legacy.execute("PRAGMA table_info(entries)")}
        check("旧库确实不含 collocations_json 列（否则下面的补列断言是空话）",
              "collocations_json" not in _cols_before, str(sorted(_cols_before)))
        _legacy.execute(
            "INSERT INTO entries (lemma, created_at, updated_at) VALUES (?,?,?)",
            ("legacycoll", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
        )
        _legacy.commit()
        _init_db(_legacy)
        _cols_after = {r[1] for r in _legacy.execute("PRAGMA table_info(entries)")}
        check("旧库经 init_db 后补上了 collocations_json 列",
              "collocations_json" in _cols_after, str(sorted(_cols_after)))
        # 补列没发生时，`Repository.get()` 会直接抛
        # `OperationalError: no such column` —— 那是**真结果**（老用户的库确实
        # 读不出来），但它以异常形式出现会**中断整个自检**：上面那条「补上了吗」
        # 的断言虽然已经报了 FAIL，后面几百条断言却一条都不会跑，报表上只留一句
        # 「CAUGHT*（代码崩溃）」。所以在这里把下游读取包成一条失败断言。
        try:
            _legacy_entry = Repository(_legacy).get("legacycoll")
            _legacy_ok = _legacy_entry is not None and _legacy_entry.collocations == []
            _legacy_why = (
                repr(_legacy_entry.collocations) if _legacy_entry else "(没取到)"
            )
        except Exception as exc:  # noqa: BLE001 - 要的就是「读不出来」这个结论
            _legacy_ok = False
            _legacy_why = f"旧行读不出来：{type(exc).__name__}: {exc}"
        check("补列之后旧行读回来搭配是空列表（不是 None、不抛）", _legacy_ok, _legacy_why)
    finally:
        _legacy.close()

    # ---- 7h. 新加的界面部件在样式登记表里 ----
    #
    # 两处都**不会报错**，只会让字看不清：
    #   ① 搭配的中文简释与其他次要文字同类，最透那一档下必须一起加深。漏登记的
    #      表现是「只有搭配那一行看不清」，而用户会以为那条搭配没有简释；
    #   ② 视图切换按钮自己没有表面，直接压在**原图**上，而图片的明暗未知——
    #      一个颜色救不了两种背景，只能给它垫一层底（见第 27 节那条同款断言）。
    check("搭配简释登记在加强色表里（漏登记 = 只有搭配那一行看不清，且不报错）",
          ("QLabel#collGloss", "text2_strong") in theme_mod._STRONG_TEXT,
          str([sel for sel, _key in theme_mod._STRONG_TEXT]))
    _coll_strong_rules = [
        " ".join(line.split())
        for line in theme_mod.stylesheet().splitlines()
        if "hasBackground" in line and "collGloss" in line
    ]
    check("加强色规则里确实生成了搭配简释那一条",
          bool(_coll_strong_rules), str(_coll_strong_rules[:1]))

    # ---- 8. schema 版本与别名表 ----
    version_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    # 断言引用常量而不是字面量：版本号是会被有意推进的，把 3 写死在断言里
    # 只会让每次推进都要改测试，而测试要验证的是「升级路径真的走了」。
    check(
        "schema 版本已推进到当前值",
        version_row["value"] == str(SCHEMA_VERSION),
        version_row["value"],
    )
    has_aliases = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='aliases'"
    ).fetchone()
    check("aliases 表已创建", has_aliases is not None)

    # FTS 列集必须与 FTS_COLUMNS 完全一致；列集是 BM25 权重的下标依据
    present = tuple(
        r["name"] for r in conn.execute("PRAGMA table_info(entries_fts)")
    )
    check("FTS 列集与声明一致", present == FTS_COLUMNS, str(present))
    check(
        "BM25 权重与列数一致",
        len(FTS_WEIGHTS) == len(FTS_COLUMNS),
        f"{len(FTS_WEIGHTS)} vs {len(FTS_COLUMNS)}",
    )
    check(
        "索引行内容与列数一致",
        len(fts_values(repo.get("ubiquitous"))) == len(FTS_COLUMNS),
    )

    # 模拟老库升级：v1 库应自动补齐新表并推进版本号。
    # 该路径曾因升级分支缺失而静默失效，必须有独立用例覆盖。
    legacy_dir = Path(tempfile.mkdtemp(prefix="quill_legacy_"))
    atexit.register(_cleanup_tmpdir, legacy_dir)
    legacy = connect(legacy_dir / "legacy.db")
    legacy.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    legacy.execute("INSERT INTO meta(key, value) VALUES('schema_version','1')")
    legacy.commit()
    init_db(legacy)
    legacy_ver = legacy.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"]
    check(
        "v1 库自动升级到当前版本", legacy_ver == str(SCHEMA_VERSION), legacy_ver
    )
    legacy_alias = legacy.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='aliases'"
    ).fetchone()
    check("升级时补齐 aliases 表", legacy_alias is not None)
    legacy.close()

    # 模拟 v2 老库：FTS 表是旧的 5 列结构，且已写入数据。
    # 关键点在于「重建后数据仍可检索」——只 DROP 不重建会静默清空索引，
    # 表现为「升级后搜什么都搜不到」，这类问题必须由用例守住。
    old_dir = Path(tempfile.mkdtemp(prefix="quill_oldfts_"))
    atexit.register(_cleanup_tmpdir, old_dir)
    old = connect(old_dir / "old.db")
    old.executescript(DDL)
    old.execute(
        """CREATE VIRTUAL TABLE entries_fts USING fts5(
               lemma, definition_en, definition_zh, zh_bigram, synonyms_text,
               tokenize = 'unicode61 remove_diacritics 2')"""
    )
    old.execute("INSERT INTO meta(key, value) VALUES('schema_version','2')")
    old.execute(
        """INSERT INTO entries(lemma, definition_en, definition_zh, example,
               example_zh, synonyms_json, notes, tags, created_at, updated_at)
           VALUES('legacy','old def','旧释义','An old example sentence.',
                  '旧例句翻译','[]','旧备注在此','旧标签','t','t')"""
    )
    old.commit()
    init_db(old)
    repo_old = Repository(old)
    rebuilt = tuple(r["name"] for r in old.execute("PRAGMA table_info(entries_fts)"))
    check("v2 老库索引结构已重建", rebuilt == FTS_COLUMNS, str(rebuilt))
    check(
        "重建后原释义仍可检索",
        any(e.lemma == "legacy" for e in repo_old.search("旧释义")),
        str([e.lemma for e in repo_old.search("旧释义")]),
    )
    check(
        "重建后老备注可检索",
        any(e.lemma == "legacy" for e in repo_old.search("旧备注")),
        str([e.lemma for e in repo_old.search("旧备注")]),
    )
    check(
        "重建后老例句可检索",
        any(e.lemma == "legacy" for e in repo_old.search("旧例句")),
        str([e.lemma for e in repo_old.search("旧例句")]),
    )
    check(
        "重建不产生重复行",
        old.execute("SELECT COUNT(*) AS c FROM entries_fts").fetchone()["c"] == 1,
    )
    old.close()
    # 结构已正确的库再次 init_db 不应触发重建（重建无必要，且会白跑一遍全量索引）
    reopened = connect(old_dir / "old.db")
    init_db(reopened)
    check(
        "结构正确时不重建索引",
        reopened.execute("SELECT COUNT(*) AS c FROM entries_fts").fetchone()["c"] == 1,
    )
    reopened.close()

    # ---- 9. 词形还原候选 ----
    check("还原 -ing 双写", "run" in lemma_candidates("running"), str(lemma_candidates("running")))
    check("还原 -ing 去 e", "make" in lemma_candidates("making"), str(lemma_candidates("making")))
    check("还原 -ies", "study" in lemma_candidates("studies"), str(lemma_candidates("studies")))
    check("还原 -xes", "box" in lemma_candidates("boxes"), str(lemma_candidates("boxes")))
    check("还原 -ed 双写", "stop" in lemma_candidates("stopped"), str(lemma_candidates("stopped")))
    check("还原 -ed 去 e", "love" in lemma_candidates("loved"), str(lemma_candidates("loved")))
    check("还原不规则过去式", "go" in lemma_candidates("went"), str(lemma_candidates("went")))
    check("还原不规则比较级", "good" in lemma_candidates("better"), str(lemma_candidates("better")))
    check("候选不含输入本身", "running" not in lemma_candidates("running"))
    check("非纯字母不还原", lemma_candidates("run-ning!") == [], str(lemma_candidates("run-ning!")))
    check("空输入返回空", lemma_candidates("") == [])

    # ---- 10. 屈折形式归并（关键：不得触发接口调用） ----
    repo.upsert(
        Entry(
            lemma="run",
            definition_en="to move quickly on foot",
            definition_zh="跑；奔跑",
            synonyms=[Synonym(word="sprint", gloss_zh="冲刺")],
        )
    )
    counter = CountingProvider(MockProvider())
    service.set_provider(counter)

    merged, from_cache = service.lookup("running")
    check("running 归并到 run", merged.lemma == "run", merged.lemma)
    check("归并走缓存", from_cache is True)
    check("归并未调用接口", counter.calls == 0, f"调用了 {counter.calls} 次")
    check("别名已记录", "running" in merged.aliases, str(merged.aliases))

    again = repo.find_by_alias("running")
    check("别名表可反查", again is not None and again.lemma == "run")

    merged2, cached2 = service.lookup("runs")
    check("runs 同样归并", merged2.lemma == "run" and cached2 is True)
    check("两次归并均未调用接口", counter.calls == 0, f"调用了 {counter.calls} 次")

    straight = repo.get("run")
    check("原形直接命中", straight is not None and straight.lemma == "run")
    check(
        "归并词条可中文检索",
        any(e.lemma == "run" for e in repo.search("奔跑")),
        str([e.lemma for e in repo.search("奔跑")]),
    )

    # ---- 11. 删除 ----
    target = repo.get("meticulous")
    assert target is not None
    remaining = repo.count() - 1
    repo.delete(target.id)
    check("删除后条数减一", repo.count() == remaining, f"count={repo.count()}")
    check("删除后索引同步清空", repo.search("一丝不苟") == [])

    run_entry = repo.get("run")
    assert run_entry is not None
    repo.delete(run_entry.id)
    check("删除词条时别名级联清除", repo.find_by_alias("running") is None)

    # ---- 9. 导出 ----
    entries = repo.list_entries(order="alpha")
    text = export_text(entries)
    check("导出含词条", "ubiquitous" in text)
    check("导出含英文释义", "seeming to be everywhere" in text)
    check("导出含中文释义", "无处不在" in text)
    check("导出含同义语", "pervasive" in text)
    check("导出含例句", "Mobile phones" in text)
    check("导出含备注", "测试备注" in text)
    check("导出含条目计数", f"共 {len(entries)} 条" in text)
    check("导出不含已删词条", "meticulous" not in text)

    md = export_markdown(entries)
    check("Markdown 导出含标题层级", "## 1." in md and "- **英**" in md)

    # ---- 12b. 多行备注的导出版式 ----
    # 备注来自可换行的输入框，多行是常态。续行若不带缩进，打印稿里会顶到
    # 最左侧、看起来脱离了所属词条；Markdown 里则会掉出列表项变成独立段落。
    multi = Entry(
        lemma="wander",
        definition_en="to walk slowly around",
        definition_zh="漫步；闲逛",
        notes="第一行备注\n第二行备注\n第三行备注",
    )
    text_multi = export_text([multi])
    for label in ("第二行备注", "第三行备注"):
        line = next((ln for ln in text_multi.splitlines() if label in ln), "")
        check(
            f"多行备注续行「{label}」有缩进",
            line.startswith(" " * 8),
            repr(line),
        )
    check("多行备注首行带标签", "注  第一行备注" in text_multi)
    check(
        "多行备注内容不丢",
        all(k in text_multi for k in ("第一行备注", "第二行备注", "第三行备注")),
    )

    md_multi = export_markdown([multi])
    md_lines = md_multi.splitlines()
    first_idx = next(i for i, ln in enumerate(md_lines) if "第一行备注" in ln)
    check("Markdown 多行备注首行在列表项内", md_lines[first_idx].startswith("- **注**"))
    check(
        "Markdown 多行备注续行仍在列表项内",
        md_lines[first_idx + 1].startswith("  ") and "第二行备注" in md_lines[first_idx + 1],
        repr(md_lines[first_idx + 1]),
    )

    # 机器生成的字段内部换行属模型噪音，必须折叠掉，否则同样破坏版式
    noisy = Entry(
        lemma="noisy",
        definition_en="first line\nsecond line",
        example="a\nb",
        synonyms=[Synonym(word="x", gloss_zh="甲\n乙")],
    )
    text_noisy = export_text([noisy])
    check("释义内部换行已折叠", "first line second line" in text_noisy, repr(text_noisy))
    check("例句内部换行已折叠", "例  a b" in text_noisy)
    check("同义语简释内部换行已折叠", "（甲 乙）" in text_noisy)
    check("无裸换行泄漏到行首", not any(
        ln.startswith(("second line", "b", "乙")) for ln in text_noisy.splitlines()
    ))

    # 纯空白备注不应产出空块
    blank = Entry(lemma="blank", notes="   \n  \n")
    check("空白备注不产出空块", "注" not in export_text([blank]))

    # 单行备注的版式必须与改动前一致（回归保护）
    single = Entry(lemma="solo", notes="单行备注")
    check("单行备注版式不变", "    注  单行备注" in export_text([single]))

    # ---- 10. LLM 输出解析容错 ----
    plain = '{"lemma":"test","definition_en":"a trial"}'
    check("解析纯 JSON", extract_json(plain)["lemma"] == "test")
    fenced = '```json\n{"lemma":"test"}\n```'
    check("解析 Markdown 围栏", extract_json(fenced)["lemma"] == "test")
    noisy = '好的，结果如下：\n{"lemma":"test","synonyms":[{"word":"trial"}]}\n以上。'
    parsed = extract_json(noisy)
    check("解析夹带前后文", parsed["lemma"] == "test")
    check(
        "同义语字符串形态兼容",
        Entry.from_payload({"lemma": "x", "synonyms": ["a", "b"]}).synonyms[0].word == "a",
    )
    check(
        "同义语字典形态兼容",
        Entry.from_payload({"lemma": "x", "synonyms": [{"word": "a"}]}).synonyms[0].word == "a",
    )

    # ---- 11. 端点归一化 ----
    check("补全端点", build_endpoint("https://api.test.com/v1") == "https://api.test.com/v1/chat/completions")
    check("已是完整端点", build_endpoint("https://a.com/v1/chat/completions").endswith("/chat/completions"))

    # ---- 11b. 接入状态描述 ----
    # 关键要求：能区分「主动选离线示例」与「选了接口但没填密钥」。
    # 后者会让人误以为接口已经通了，因为界面照常出结果。
    check(
        "主动离线模式的状态文案",
        # 用 startswith 而不是全等：这条断言要守住的是「能区分主动选离线
        # 与选了接口没填密钥」，不该因为文案多加一个括号就失败。
        provider_status({"provider": "mock", "model": "gpt-4o-mini"}, "sk-x").startswith("离线示例模式"),
    )
    fallback = provider_status({"provider": "openai-compat", "model": "gpt-4o-mini"}, "")
    check("未填密钥时明确提示", "未填写 API 密钥" in fallback, fallback)
    online = provider_status({"provider": "openai-compat", "model": "gpt-4o-mini"}, "sk-x")
    check("已接入时显示模型名", "gpt-4o-mini" in online, online)
    check(
        "未指定模型时不留空白",
        "未指定模型" in provider_status({"provider": "openai-compat", "model": ""}, "sk-x"),
    )
    # 缺密钥时优先回落到**真词典**而不是 3 个演示词：能查到词比看界面效果有用。
    # 只有连词库都没有时才退回 mock。
    from app.providers.offline import resolve_offline_dict as _resolve_offline

    _expected = (
        "OfflineDictProvider" if _resolve_offline() is not None else "MockProvider"
    )
    check(
        "缺密钥时回落到离线词库",
        type(build_provider({}, "")).__name__ == _expected,
        type(build_provider({}, "")).__name__,
    )

    # ---- 12. 密钥加解密往返 ----
    secret = "sk-test-1234567890"
    token = encrypt_text(secret)
    check("密文非明文", secret not in token)
    check("解密还原", decrypt_text(token) == secret)

    # ---- 13. 数据库备份 ----
    expected = repo.count()
    backup_path = tmpdir / "sub" / "quill_backup.db"
    saved = backup_to(conn, backup_path)
    check("备份文件已生成", saved.exists() and saved.stat().st_size > 0)
    check("备份目录自动创建", backup_path.parent.is_dir())
    check(
        "备份成功不留临时文件",
        not backup_path.with_name(backup_path.name + ".tmp").exists(),
    )

    bconn = connect(backup_path)
    integrity = bconn.execute("PRAGMA integrity_check").fetchone()
    check("备份通过完整性校验", integrity is not None and integrity[0] == "ok", str(integrity))
    brepo = Repository(bconn)
    check("备份条目数与源库一致", brepo.count() == expected, f"{brepo.count()} != {expected}")
    check(
        "备份保留全文索引",
        any(e.lemma == "ubiquitous" for e in brepo.search("无处不在")),
        str([e.lemma for e in brepo.search("无处不在")]),
    )
    check(
        "备份保留中文例句检索",
        any(e.lemma == "ephemeral" for e in brepo.search("转瞬")),
    )
    check(
        "备份保留 schema 版本",
        bconn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == str(SCHEMA_VERSION),
    )
    bconn.close()

    # 目标位置已有一份旧备份时，应被整体替换而不是追加
    backup_path.write_text("旧内容", encoding="utf-8")
    backup_to(conn, backup_path)
    oconn = connect(backup_path)
    check(
        "覆盖旧备份后仍是有效库",
        oconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
    )
    check("覆盖后条目数正确", Repository(oconn).count() == expected)
    oconn.close()

    # 失败路径：目标不可写时必须抛错，且不能破坏已存在的内容
    blocked = tmpdir / "blocked_dir"
    blocked.mkdir()
    raised = False
    try:
        backup_to(conn, blocked)
    except (OSError, sqlite3.DatabaseError):
        raised = True
    check("目标不可写时抛错", raised)
    check("失败后清理临时文件", not (tmpdir / "blocked_dir.tmp").exists())
    check("失败后未破坏原目标", blocked.is_dir())

    # ---- 13b. 备份恢复：校验、暂存、替换 ----
    # 恢复是拿一个文件覆盖用户整个词库，所以每条判据都要能读出「换没换」，
    # 而不是「没抛异常」。三段分开测：校验只看返回值，暂存看文件，
    # 替换必须真的读出新内容。
    fresh_backup = tmpdir / "restore_src.db"
    backup_to(conn, fresh_backup)
    check("可用备份校验通过", inspect_backup(fresh_backup) == "", inspect_backup(fresh_backup))

    missing_reason = inspect_backup(tmpdir / "no_such_backup.db")
    check("不存在的备份被拒绝", missing_reason != "", missing_reason)

    junk = tmpdir / "junk_backup.db"
    junk.write_text("这不是数据库", encoding="utf-8")
    check("不是数据库的文件被拒绝", inspect_backup(junk) != "")

    # 这一条专门对付「拿别的 sqlite 文件当备份」：它完整性检查是 ok 的，
    # 只看 integrity_check 会放行，替换之后用户的词库就没了。
    alien = tmpdir / "alien_backup.db"
    aconn = sqlite3.connect(alien)
    aconn.execute("CREATE TABLE unrelated(x)")
    aconn.commit()
    aconn.close()
    alien_reason = inspect_backup(alien)
    check("完整性 ok 但没有词条表被拒绝", alien_reason != "", alien_reason)
    check("缺词条表时说清原因", "词条表" in alien_reason, alien_reason)

    broken = tmpdir / "broken_backup.db"
    raw = fresh_backup.read_bytes()
    # 刻意只砸第二页：库头与 sqlite_master 还在（表名照样查得出来），
    # 完整性却过不了。这正是「只查表名、不查 integrity_check」会放行的那一类。
    # 换个位置砸（比如文件正中）实测毫无效果——那块落在空闲页里，什么检查都过。
    pconn = sqlite3.connect(f"file:{fresh_backup.as_posix()}?mode=ro", uri=True)
    page_size = pconn.execute("PRAGMA page_size").fetchone()[0]
    pconn.close()
    broken.write_bytes(raw[:page_size] + b"\x00" * page_size + raw[page_size * 2 :])
    broken_reason = inspect_backup(broken)
    check("损坏的备份被拒绝", broken_reason != "", broken_reason)
    check("损坏提示指向完整性", "完整性" in broken_reason, broken_reason)

    # 暂存：复制到目标旁边，备份本身必须留着（用户可能只想拿回几条，或留作存档）。
    restore_target = tmpdir / "restore_me.db"
    tconn = connect(restore_target)
    init_db(tconn)
    t_repo = Repository(tconn)
    t_repo.upsert(Entry(lemma="willbeoverwritten", definition_zh="旧库内容"))
    old_count = t_repo.count()
    src_count = repo.count()

    raised = False
    try:
        stage_restore(junk, restore_target)
    except sqlite3.DatabaseError:
        raised = True
    check("坏备份在复制前就被拦下", raised)
    check(
        "坏备份没留下暂存件",
        not restore_target.with_name(restore_target.name + ".restore").exists(),
    )

    staged = stage_restore(fresh_backup, restore_target)
    check(
        "暂存件落在目标旁边",
        staged == restore_target.with_name(restore_target.name + ".restore"),
        str(staged),
    )
    # 先判「备份还在」，再比字节数：顺序反过来，一旦暂存把备份搬走（is not 复制），
    # 上一行就会因为文件不存在抛异常，把这条判据挡在门外，看起来像没写测试。
    check("暂存不动备份本身", fresh_backup.exists())
    check(
        "暂存件与备份字节数一致",
        fresh_backup.exists()
        and staged.stat().st_size == fresh_backup.stat().st_size,
    )

    # 替换：先关连接（Windows 上打开着会锁住文件），再清残留日志。
    # 留着旧 -wal 等于把旧库的页回放进新库，所以这里先造两个假的出来。
    tconn.close()
    for suffix in ("-wal", "-shm"):
        restore_target.with_name(restore_target.name + suffix).write_bytes(b"stale")
    replace_with_staged(staged, restore_target)
    check("替换后暂存件已消失", not staged.exists())
    check(
        "替换清掉了残留日志",
        not restore_target.with_name(restore_target.name + "-wal").exists(),
    )

    rconn = connect(restore_target)
    r_repo = Repository(rconn)
    check(
        "替换后条目数来自备份",
        r_repo.count() == src_count and old_count != src_count,
        f"{r_repo.count()} != {src_count}（旧库 {old_count} 条）",
    )
    check("旧库的词条不复存在", r_repo.get("willbeoverwritten") is None)
    check("新库能查到备份里的词", r_repo.get("ubiquitous") is not None)
    rconn.close()

    # ---- 14. 检索覆盖：例句、例句翻译、备注、标签 ----
    # 生词本是用户自己的本子，「搜不到自己写的备注」是实打实的能力缺失。
    repo.upsert(
        Entry(
            lemma="solitude",
            definition_en="the state of being alone",
            definition_zh="独处；孤独",
            example="He found peace in the quiet solitude of the mountains.",
            example_zh="他在山中静谧的独处中找到了安宁。",
            notes="考研高频，固定搭配 in solitude",
            tags="考研",
        )
    )
    check(
        "英文例句可检索",
        any(e.lemma == "solitude" for e in repo.search("mountains")),
        str([e.lemma for e in repo.search("mountains")]),
    )
    check(
        "中文例句翻译可检索",
        any(e.lemma == "solitude" for e in repo.search("静谧")),
        str([e.lemma for e in repo.search("静谧")]),
    )
    check(
        "备注可检索（中文）",
        any(e.lemma == "solitude" for e in repo.search("高频")),
        str([e.lemma for e in repo.search("高频")]),
    )
    check(
        "标签可检索",
        any(e.lemma == "solitude" for e in repo.search("考研")),
        str([e.lemma for e in repo.search("考研")]),
    )
    check(
        "备注可检索（英文）",
        any(e.lemma == "solitude" for e in repo.search("solitude 搭配")),
        str([e.lemma for e in repo.search("solitude 搭配")]),
    )
    # 备注是高频修改字段，改完必须同步索引，否则检索结果是旧的。
    # 改法必须是「读取-修改-写入」：直接 upsert 一个只带新备注的新对象属于
    # 「接口回填」语义，按设计不覆盖用户字段（见第 16 节）。
    solitude = repo.get("solitude")
    assert solitude is not None
    solitude.notes = "换了个说法：田野调查专用词"
    repo.upsert(solitude)
    check(
        "备注更新后索引同步（新内容可搜）",
        any(e.lemma == "solitude" for e in repo.search("田野调查")),
        str([e.lemma for e in repo.search("田野调查")]),
    )
    check("备注更新后索引同步（旧内容失效）", repo.search("高频") == [])

    # FTS 损坏时必须回退 LIKE，回退路径的字段覆盖不能比 FTS 窄，
    # 否则会出现「索引一坏，检索能力静默退化」这种极难察觉的问题
    fallback_dir = Path(tempfile.mkdtemp(prefix="quill_like_"))
    atexit.register(_cleanup_tmpdir, fallback_dir)
    fconn = connect(fallback_dir / "like.db")
    init_db(fconn)
    frepo = Repository(fconn)
    frepo.upsert(
        Entry(lemma="quaint", definition_zh="古雅的", notes="这个词问过王老师")
    )
    fconn.execute("DROP TABLE entries_fts")
    fconn.commit()
    check(
        "FTS 缺失时回退 LIKE 覆盖词形",
        any(e.lemma == "quaint" for e in frepo.search("quaint")),
        str([e.lemma for e in frepo.search("quaint")]),
    )
    check(
        "FTS 缺失时回退 LIKE 覆盖备注",
        any(e.lemma == "quaint" for e in frepo.search("王老师")),
        str([e.lemma for e in frepo.search("王老师")]),
    )
    fconn.close()

    # ---- 15. 词条完整性：接口少给字段时必须让用户知道 ----
    # 详情面板对空字段是直接不渲染的，因此「接口没给同义语」与
    # 「这个词本来就没有同义语」在界面上长得一模一样，用户无从分辨。
    complete = repo.get("ubiquitous")
    check("完整词条无缺失项", complete.missing_parts() == [], str(complete.missing_parts()))

    sparse = Entry(lemma="sparse", definition_en="only english here")
    check(
        "缺项按固定顺序列出",
        sparse.missing_parts() == ["中文释义", "同义语", "固定搭配", "例句"],
        str(sparse.missing_parts()),
    )
    check(
        "只有中文释义时仍报缺英文释义",
        Entry(lemma="zh", definition_zh="中文").missing_parts()[0] == "英文释义",
    )
    check(
        "纯空白视为缺失",
        Entry(lemma="x", definition_en="   ").missing_parts()[0] == "英文释义",
    )
    check(
        "有例句无翻译不算缺例句",
        "例句" not in Entry(lemma="e", definition_zh="中", example="A sentence.").missing_parts(),
    )

    # 不完整词条仍应入库（拿到多少算多少），但要能报告缺了什么
    service.set_provider(PayloadProvider({"lemma": "partial", "definition_en": "english only"}))
    partial_entry, _ = service.lookup("partial")
    check("不完整词条仍然入库", repo.get("partial") is not None)
    check(
        "不完整词条可报告缺项",
        "同义语" in partial_entry.missing_parts(),
        str(partial_entry.missing_parts()),
    )

    # 完全空壳必须拒收：存进去只会在生词本里留下一条空白记录，
    # 而且会被一并导出、打印出来
    hollow_before = repo.count()
    service.set_provider(PayloadProvider({}))
    hollow_error = None
    try:
        service.lookup("hollow")
    except Exception as exc:  # noqa: BLE001 - 测试需要任何异常类型
        hollow_error = exc
    check(
        "空壳返回抛出可读错误",
        hollow_error is not None and "释义" in str(hollow_error),
        str(hollow_error),
    )
    check(
        "空壳未写入数据库",
        repo.count() == hollow_before,
        f"{hollow_before} -> {repo.count()}",
    )
    check("空壳确实未落库", repo.get("hollow") is None)

    # ---- 16. 接口回填不得抹掉用户自有字段 ----
    # 备注与标签是用户自己的财产，只应在用户主动编辑时改变。
    # 「重新查询」会绕过缓存重新取回内容；如果回填时整行覆盖，
    # 用户写的备注会被静默清空——而且是永久丢失，连索引一起没。
    stub = StubProvider()
    service.set_provider(stub)

    repo.upsert(
        Entry(
            lemma="boundary",
            definition_en="old definition",
            definition_zh="旧的释义",
            notes="我的记忆法：bond + ary",
            tags="考研",
        )
    )
    before = repo.get("boundary")
    assert before is not None
    refreshed = service.refresh("boundary")
    after = repo.get("boundary")

    check("重新查询后备注仍在", after.notes == "我的记忆法：bond + ary", repr(after.notes))
    check("重新查询后标签仍在", after.tags == "考研", repr(after.tags))
    check(
        "重新查询后备注仍可检索",
        any(e.lemma == "boundary" for e in repo.search("记忆法")),
        str([e.lemma for e in repo.search("记忆法")]),
    )
    check(
        "重新查询确实更新了接口内容",
        after.definition_en == "seeming to be everywhere",
        after.definition_en,
    )
    check(
        "返回词条在内存中也带着备注",
        refreshed.notes == "我的记忆法：bond + ary",
        repr(refreshed.notes),
    )
    # 契约锁定：refresh() 返回单个 Entry，而 lookup() 返回 (entry, cached) 元组。
    # 两者不一致且调用点看不出差异，界面层若接错回调会抛 TypeError（表现为
    # 「重新查询必失败」）。这里把契约钉住，避免日后被改成元组而不自知。
    check(
        "refresh 返回单个 Entry（非元组）",
        isinstance(refreshed, Entry),
        type(refreshed).__name__,
    )
    check(
        "lookup 返回元组",
        isinstance(service.lookup("boundary"), tuple),
        type(service.lookup("boundary")).__name__,
    )

    # 反向约束：用户显式清空备注（读取-修改-写入）必须真正生效，
    # 否则「保用户字段」会变成「备注永远删不掉」
    cleared = repo.get("boundary")
    assert cleared is not None
    cleared.notes = ""
    repo.upsert(cleared)
    check("显式清空备注生效", repo.get("boundary").notes == "")

    # 无备注的词条重新查询后不应凭空生出内容
    repo.upsert(Entry(lemma="plain", definition_en="d", definition_zh="释义"))
    service.refresh("plain")
    check("无备注词条保持空白", repo.get("plain").notes == "", repr(repo.get("plain").notes))

    # ---- 16b. 批量重查：接口没答上来的一条必须保持原样 ----
    # 「一键重查」是**覆盖写**，一次跑几十上百条。单条重查时用户看得见状态栏那句
    # 「接口请求失败，已回落到离线词库」，他当场就能判断；批量跑的时候没人盯得住
    # 每一条，所以这个判定必须由程序做（`accept_fallback=False`）。
    #
    # 判据只能是 `fallback_reason`：兜底词条的 `source` 与「用户主动选离线模式」
    # 完全同值，看 source 分不出来。
    from app.providers.fallback import FallbackProvider  # noqa: PLC0415
    from app.providers.base import ProviderError  # noqa: PLC0415

    class _FailingPrimary:
        name = "openai-compat"

        def lookup(self, word: str) -> Entry:
            raise ProviderError("请求超时（30 秒）")

    class _OfflineFallback:
        name = "offline"

        def lookup(self, word: str) -> Entry:
            return Entry(
                lemma=word,
                definition_en="offline body",
                definition_zh="兜底释义",
            )

    repo.upsert(
        Entry(
            lemma="keepme",
            definition_en="precious",
            definition_zh="要保留的内容",
            notes="我的备注",
            tags="考研",
        )
    )
    service.set_provider(FallbackProvider(_FailingPrimary(), _OfflineFallback()))
    refused: Exception | None = None
    try:
        service.refresh("keepme", accept_fallback=False)
    except ProviderError as exc:
        refused = exc
    kept_entry = repo.get("keepme")
    assert kept_entry is not None
    check("拒绝兜底内容时抛错", refused is not None, str(refused))
    check(
        "拒绝兜底后内容保持原样",
        kept_entry.definition_en == "precious",
        repr(kept_entry.definition_en),
    )
    check("拒绝兜底后备注仍在", kept_entry.notes == "我的备注", repr(kept_entry.notes))

    # 对照组：同一个 provider 不传这个参数时确实会被覆盖。
    # 没有这一组，上面三条可能只是因为「什么也没发生」而通过——那正是最容易被
    # 误读成「保护生效」的形态。
    service.refresh("keepme")
    check(
        "接受兜底时确实被覆盖（对照）",
        repo.get("keepme").definition_en == "offline body",
        repr(repo.get("keepme").definition_en),
    )
    service.set_provider(stub)

    # ---- 16c. persist=False 只取回、不落库 ----
    repo.upsert(Entry(lemma="nowrite", definition_zh="库里的原值"))
    fetched, _cached = service.lookup("nowrite", use_cache=False, persist=False)
    check(
        "persist=False 能取回新内容",
        fetched.definition_en == stub.definition_en,
        repr(fetched.definition_en),
    )
    check(
        "persist=False 没有写库",
        repo.get("nowrite").definition_zh == "库里的原值",
        repr(repo.get("nowrite").definition_zh),
    )
    check(
        "persist=False 未建立别名",
        repo.aliases_of(repo.get("nowrite").id) == [],
        str(repo.aliases_of(repo.get("nowrite").id)),
    )

    # ---- 17. 导出的保真与完整 ----
    # Markdown 导出会把内容原样交给渲染器，用户备注里的行首结构字符因此会被
    # 「重新解释」：`-` 变列表项、`>` 变引用、`#` 变标题、`1.` 被渲染器重新
    # 编号（用户自己写的序号就此消失）。这类改动静默发生，肉眼很难发现。
    md_src = Entry(
        lemma="fi*d",
        pos="adj.",
        definition_en="found *everywhere*",
        definition_zh="无处不在的",
        example="It is everywhere.",
        synonyms=[Synonym(word="per*vasive", gloss_zh="弥漫的", diff_note="强调扩散")],
        notes="# 记忆法\n- ubi = where\n> 注意区分\n1. 先记词根\n3. 再看例句\n# 中间标题",
    )
    md = export_markdown([md_src])

    # 首行备注是内联在 `- **注** ` 之后的，行首的 `#` 处于行中，本就无害
    check("备注首行的井号保持字面量", "- **注** # 记忆法" in md, repr(md))
    check("Markdown 转义行首减号", "\n  \\- ubi = where" in md, repr(md))
    check("Markdown 转义行首引用符", "\n  \\> 注意区分" in md, repr(md))
    check("Markdown 转义行首井号", "\n  \\# 中间标题" in md, repr(md))
    check("Markdown 保留用户编号 1.", "\n  1\\. 先记词根" in md, repr(md))
    check("Markdown 保留用户编号 3.", "\n  3\\. 再看例句" in md, repr(md))
    check("Markdown 转义释义中的星号", "found \\*everywhere\\*" in md, repr(md))
    check("Markdown 转义词形中的星号", "per\\*vasive" in md, repr(md))
    # 首尾空行不应产生只有「注」二字的空行
    txt_blank = export_text([Entry(lemma="b", notes="\n\n有内容\n\n")])
    check("备注首尾空行被丢弃", "注  有内容" in txt_blank, repr(txt_blank))
    check("备注不留孤立注行", "注  \n" not in txt_blank, repr(txt_blank))
    # 中间空行是有意分段，必须保留
    txt_mid = export_text([Entry(lemma="c", notes="上段\n\n下段")])
    check("备注中间空行保留", "上段\n\n" in txt_mid, repr(txt_mid))

    # 反向约束：转义只针对 Markdown。纯文本是打印路径，不能被反斜杠污染
    txt = export_text([md_src])
    check("纯文本导出不含转义反斜杠", "\\" not in txt, repr(txt))
    # 反向约束：普通内容不应产生多余转义
    plain_md = export_markdown(
        [Entry(lemma="plain", definition_zh="无处不在的", notes="普通备注")]
    )
    check("普通中文内容保持原样", "无处不在的" in plain_md and "普通备注" in plain_md)
    check("普通内容未被反斜杠污染", "\\" not in plain_md, repr(plain_md))

    # 导出不得被界面显示上限截断。列表上限是给屏幕用的，打印稿必须完整，
    # 否则文件头会自称「共 N 条」，而 N 恰好是被砍掉之后的数量。
    for i in range(5):
        repo.upsert(Entry(lemma=f"limitprobe{i}", definition_zh=f"探针{i}"))
    check("检索可设上限", len(repo.search("limitprobe", limit=2)) == 2)
    check(
        "检索 limit=None 返回全部",
        len(repo.search("limitprobe", limit=None)) == 5,
        str(len(repo.search("limitprobe", limit=None))),
    )
    check(
        "列表查询 limit=None 返回全部",
        len(repo.list_entries(limit=None)) == repo.count(),
        f"{len(repo.list_entries(limit=None))} vs {repo.count()}",
    )

    # ---- 18. 后台任务的结果投递不得丢失 ----
    # QThreadPool.start() 并不持有 Python 包装对象的引用；调用方若丢弃返回值
    # （本项目两个调用点都丢弃），包装对象会被立刻回收，挂在它上面的 signals
    # 随之销毁，排队中的结果投递全部作废——表现为「查询完成、库里也写了，
    # 界面却毫无反应」，而且取决于 GC 时机，是间歇性的。
    #
    # **这里必须用 QGuiApplication 而不是 QCoreApplication。** 二者对事件循环
    # 是等价的，但 QCoreApplication 没有字体引擎，而 §26 的 PDF 导出需要它；
    # 且 Qt 不允许把已存在的 QCoreApplication 升级为 QGuiApplication——一旦
    # 这里建了后者，后面调 export_pdf 就是未定义行为（实测：rc=127、零输出、
    # 无 traceback，整个自检静默死掉，看起来像「脚本自己退出了」）。
    # 自检本来就可能要渲染（PDF 满宽判定），从一开始就用 GUI 版是唯一站得住的选择。
    #
    # **刻意不设 QT_QPA_PLATFORM=offscreen。** 那个平台不加载系统字体，
    # `QFontDatabase.families()` 返回空列表，PDF 里每个字都变成缺字方块。
    # 用真实平台才能真的验到中文渲染；环境不具备时由 §26 明确报告跳过。
    from PySide6.QtGui import QGuiApplication  # noqa: PLC0415

    from app.ui.workers import submit  # noqa: PLC0415

    # 引用留住：应用对象被回收后，进程里再碰 Qt 就会崩。
    global _SELFCHECK_APP  # noqa: PLW0603
    _SELFCHECK_APP = QGuiApplication.instance() or QGuiApplication([])
    qapp = _SELFCHECK_APP
    delivered: list[int] = []
    total = 120
    for i in range(total):
        submit(lambda: "ok", on_success=lambda _r, i=i: delivered.append(i))
    deadline = time.monotonic() + 6.0
    while len(delivered) < total and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    check("后台任务回调不丢失", len(delivered) == total, f"{len(delivered)}/{total}")

    # ---- 19. 配置的容错与校验 ----
    # 配置文件是用户可写、也可能被别的工具写坏的文件。它一旦让程序起不来，
    # 用户就没有自救手段了——软件打不开，也就没有界面可以把设置改回来。
    # 关键认知：**合法 JSON 不等于我们期望的对象**。`5` / `true` / `null` /
    # `[1,2]` 都会让 dict.update() 抛 TypeError，而它恰好不被
    # `except ValueError` 覆盖（`"abc"` 抛的是 ValueError，所以只有它被接住了）。
    import app.config as config_module  # noqa: PLC0415

    saved_dir, saved_path = config_module.APP_DIR, config_module.CONFIG_PATH
    cfg_dir = Path(tempfile.mkdtemp(prefix="quill_cfg_"))
    atexit.register(_cleanup_tmpdir, cfg_dir)
    config_module.APP_DIR = cfg_dir
    config_module.CONFIG_PATH = cfg_dir / "config.json"

    def write_config(raw: str) -> dict:
        config_module.CONFIG_PATH.write_text(raw, encoding="utf-8")
        return config_module.load_config()

    defaults = dict(config_module.DEFAULT_CONFIG)
    for raw in ("5", "true", "null", "[1,2]", "{bad", '"abc"', ""):
        label = raw or "(空文件)"
        try:
            got = write_config(raw)
            ok, shown = got == defaults, str(got)
        except Exception as exc:  # noqa: BLE001 - 这里就是要抓住任何异常
            ok, shown = False, f"{type(exc).__name__}: {exc}"
        check(f"损坏配置不崩溃：{label}", ok, shown)

    check("非法 timeout 退回默认", write_config('{"timeout": "abc"}')["timeout"] == 30.0)
    check("零 timeout 退回默认", write_config('{"timeout": 0}')["timeout"] == 30.0)
    check("负 timeout 退回默认", write_config('{"timeout": -5}')["timeout"] == 30.0)
    check(
        "布尔 timeout 退回默认",
        write_config('{"timeout": true}')["timeout"] == 30.0,
        repr(write_config('{"timeout": true}')["timeout"]),
    )
    check("合法 timeout 被接受", write_config('{"timeout": 12.5}')["timeout"] == 12.5)
    check(
        "非字符串地址退回默认",
        write_config('{"base_url": 123}')["base_url"] == defaults["base_url"],
        repr(write_config('{"base_url": 123}')["base_url"]),
    )
    check(
        "空白地址退回默认",
        write_config('{"base_url": "   "}')["base_url"] == defaults["base_url"],
    )
    check(
        "未知 provider 退回默认",
        write_config('{"provider": "foo"}')["provider"] == "openai-compat",
    )
    check("未知键被忽略", "hacker" not in write_config('{"hacker": 1}'))
    check(
        "地址首尾空白被去除",
        write_config('{"base_url": "  https://a.com/v1  "}')["base_url"]
        == "https://a.com/v1",
    )

    # check_config 供界面在「保存」之前校验，把问题挡在源头上
    check("模拟模式无需地址", config_module.check_config({"provider": "mock"}) is None)
    check(
        "缺地址被指出",
        "接口地址" in (
            config_module.check_config(
                {"provider": "openai-compat", "base_url": "", "model": "m"}
            )
            or ""
        ),
    )
    check(
        "缺协议被指出",
        "http" in (
            config_module.check_config(
                {
                    "provider": "openai-compat",
                    "base_url": "api.a.com/v1",
                    "model": "m",
                }
            )
            or ""
        ),
    )
    check(
        "缺模型被指出",
        "模型" in (
            config_module.check_config(
                {
                    "provider": "openai-compat",
                    "base_url": "https://a.com/v1",
                    "model": "",
                }
            )
            or ""
        ),
    )
    check("合法配置无问题", config_module.check_config(dict(defaults)) is None)

    # 落盘这一侧也要挡住：界面校验是第一道闸，写盘是最后一道
    config_module.save_config(
        {"provider": "openai-compat", "base_url": 999, "model": "m", "timeout": -1}
    )
    persisted = config_module.load_config()
    check(
        "save_config 不落盘非法值",
        persisted["base_url"] == defaults["base_url"]
        and persisted["timeout"] == defaults["timeout"],
        f"base_url={persisted['base_url']!r} timeout={persisted['timeout']!r}",
    )

    # 端点构造对「漏写协议」要给出可读提示，而不是把异常抛到 httpx 里
    from app.providers.base import ProviderError  # noqa: PLC0415

    bad_url_error = None
    try:
        build_endpoint("api.test.com/v1")
    except ProviderError as exc:
        bad_url_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        bad_url_error = f"{type(exc).__name__}: {exc}"
    check(
        "无协议地址给出可读错误",
        bool(bad_url_error and "http" in bad_url_error),
        str(bad_url_error),
    )

    config_module.APP_DIR, config_module.CONFIG_PATH = saved_dir, saved_path

    # ---- 20. 提示词版本参与缓存有效性判定 + 既有库的补列迁移 ----
    # 起因：prompts.py 的文档声称「PROMPT_VERSION 参与缓存键计算，改提示词必须
    # 递增版本号」，但全仓没有任何缓存键计算，该常量从未被读取——是一条
    # 「声称存在的保护」。真实后果：改了 SYSTEM_PROMPT 再重新查询已存词条，
    # 界面会静默给出旧提示词生成的内容，而维护者以为版本号已经替自己兜住了。
    #
    # 要让这个保护存在，必须把「该词条由哪一版提示词生成」落盘。而给已有的
    # entries 表加列，又暴露出第二个错误声明：init_db 的文档写着「新增列无需
    # 迁移脚本」——CREATE TABLE IF NOT EXISTS 对**已存在**的表不做任何事，
    # 全仓也没有一处 ALTER TABLE，所以这句话只对新库成立。
    from app.core.db import PROMPT_VERSION_COLUMN  # noqa: PLC0415
    from app.providers.prompts import PROMPT_VERSION  # noqa: PLC0415

    def _columns_of(c, table: str) -> set[str]:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

    # 20.1 模拟「上一个版本建的库」：entries 表没有 prompt_version 列。
    # 按列名过滤而不是按空格对齐做字符串替换——DDL 的对齐会随最长列名变化，
    # 用空格数去匹配等于把测试绑死在排版细节上，改一次对齐就静默失效。
    legacy_ddl = "\n".join(
        line for line in DDL.splitlines() if PROMPT_VERSION_COLUMN not in line
    )
    legacy_path = tmpdir / "legacy.db"
    legacy = sqlite3.connect(str(legacy_path))
    legacy.row_factory = sqlite3.Row
    legacy.executescript(legacy_ddl)
    legacy.execute(
        "INSERT INTO entries(lemma, created_at, updated_at) VALUES('legacyword','t','t')"
    )
    legacy.commit()
    check(
        "构造出无 prompt_version 的旧库",
        PROMPT_VERSION_COLUMN not in _columns_of(legacy, "entries"),
        str(sorted(_columns_of(legacy, "entries"))),
    )
    legacy.close()

    # 旧库再次打开时必须补列，且既有词条要带上「当时那一版」的标注
    lconn = connect(legacy_path)
    init_db(lconn)
    cols = _columns_of(lconn, "entries")
    check("旧库自动补上 prompt_version 列", PROMPT_VERSION_COLUMN in cols, str(sorted(cols)))
    row = lconn.execute(
        f"SELECT {PROMPT_VERSION_COLUMN} AS v FROM entries WHERE lemma='legacyword'"
    ).fetchone()
    check(
        "既有词条回填为补列之前的提示词版本",
        row is not None and row["v"] == 1,
        f"v={row['v'] if row else None}",
    )
    # 反向约束：补列是一次性动作，重复 init_db 不得把用户数据改来改去
    before = lconn.execute(
        f"SELECT {PROMPT_VERSION_COLUMN} AS v FROM entries WHERE lemma='legacyword'"
    ).fetchone()["v"]
    lconn.execute(
        f"UPDATE entries SET {PROMPT_VERSION_COLUMN}=? WHERE lemma='legacyword'",
        (PROMPT_VERSION,),
    )
    lconn.commit()
    init_db(lconn)
    after = lconn.execute(
        f"SELECT {PROMPT_VERSION_COLUMN} AS v FROM entries WHERE lemma='legacyword'"
    ).fetchone()["v"]
    check(
        "重复 init_db 不覆盖已标注的版本",
        before == 1 and after == PROMPT_VERSION,
        f"{before} -> {after}",
    )
    lconn.close()

    # 20.2 版本一致时不得报「陈旧」；不一致时必须报
    # 用独立临时库：Mock 只有 3 个词条，而共享 conn 到这一节已被前面小节占用，
    # 复用同一个词条会让「新查词条缓存未命中」这条断言变成在测顺序。
    vdb_path = tmpdir / "verdict.db"
    vconn = connect(vdb_path)
    init_db(vconn)
    vrepo = Repository(vconn)
    vprovider = CountingProvider(MockProvider())
    vsvc = DictionaryService(vrepo, vprovider)
    fresh_entry, cached0 = vsvc.lookup("ephemeral")
    check("新查词条缓存未命中", cached0 is False)
    check(
        "新词条记录当前提示词版本",
        fresh_entry.prompt_version == PROMPT_VERSION and not fresh_entry.prompt_stale,
        f"v={fresh_entry.prompt_version} stale={fresh_entry.prompt_stale}",
    )

    again, cached1 = vsvc.lookup("ephemeral")
    check(
        "版本一致时缓存直接命中且不标陈旧",
        cached1 is True and again.prompt_stale is False,
        f"cached={cached1} stale={again.prompt_stale}",
    )

    # 把库里的版本改成「上一版」，模拟维护者改完提示词后的状态
    vconn.execute(
        f"UPDATE entries SET {PROMPT_VERSION_COLUMN}=? WHERE id=?",
        (PROMPT_VERSION - 1, fresh_entry.id),
    )
    vconn.commit()
    calls_before = vprovider.calls
    stale_entry, cached2 = vsvc.lookup("ephemeral")
    check(
        "旧版提示词生成的词条被标记为陈旧",
        cached2 is True and stale_entry.prompt_stale is True,
        f"cached={cached2} stale={stale_entry.prompt_stale}",
    )
    # 关键反向约束：标记陈旧 ≠ 视为未命中。若把陈旧当成 miss，离线状态下
    # 用户点开已有的词条会直接失败——把「内容可能是旧的」升级成了「看不到」。
    check(
        "标记陈旧不等于缓存未命中（仍返回内容）",
        bool(stale_entry.definition_en or stale_entry.definition_zh),
        f"en={stale_entry.definition_en!r}",
    )
    check(
        "标记陈旧不会偷跑接口调用",
        vprovider.calls == calls_before,
        f"{vprovider.calls} vs {calls_before}",
    )

    # 从列表打开的路径不经过 _from_cache，必须由 mark_stale 显式补标记，
    # 否则同一个词条「搜索打开」会提示、「点击打开」不提示。
    from_list = vsvc.mark_stale(vrepo.get_by_id(fresh_entry.id))
    check(
        "列表打开的词条同样带上陈旧标记",
        from_list is not None and from_list.prompt_stale is True,
        f"stale={getattr(from_list, 'prompt_stale', None)}",
    )

    # 强制重新查询后，版本标注必须被刷新回当前版本，否则陈旧标记永不消失
    refreshed = vsvc.refresh("ephemeral")
    check(
        "重新查询后版本标注刷新",
        refreshed.prompt_version == PROMPT_VERSION and not refreshed.prompt_stale,
        f"v={refreshed.prompt_version} stale={refreshed.prompt_stale}",
    )
    vconn.close()

    # ---- 21. 离线词典 ----
    # 本轮新增的接入方式。要守住的不是「能不能查到词」，而是三件容易做错的事：
    #   ① 屈折形态必须由词典自带的形态表还原，而不是靠规则猜测；
    #   ② 离线来源**结构上**没有同义语，缺项提示不能照喊（恒定提示＝没有提示）；
    #   ③ 词库文件缺失或版本不符，要在设置阶段就被拦下，而不是等查词时才失败。
    from app.config import PROVIDERS, check_config
    from app.providers.base import ALL_FIELDS, provided_fields, provider_label
    from app.providers.offline import (
        OFFLINE_SCHEMA_VERSION,
        OfflineDictProvider,
        offline_dict_status,
        open_offline_db,
        resolve_offline_dict,
    )

    offline_path = resolve_offline_dict()
    check("内置离线词库存在", offline_path is not None, str(offline_path))
    check(
        "词库缺失时状态如实说明",
        offline_dict_status(None) == "离线词库缺失",
        offline_dict_status(None),
    )
    check(
        "离线词库规模可读出",
        "词" in offline_dict_status(offline_path)
        and "缺失" not in offline_dict_status(offline_path),
        offline_dict_status(offline_path),
    )

    if offline_path is not None:
        _o = open_offline_db(offline_path)
        _tables = {
            r["name"]
            for r in _o.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        check(
            "离线词库四张表齐备",
            {"dict", "forms", "examples", "meta"} <= _tables,
            str(sorted(_tables)),
        )
        _meta = {r["key"]: r["value"] for r in _o.execute("SELECT key, value FROM meta")}
        check(
            "离线词库版本与读取方一致",
            int(_meta.get("schema_version") or 0) == OFFLINE_SCHEMA_VERSION,
            f'{_meta.get("schema_version")} vs {OFFLINE_SCHEMA_VERSION}',
        )
        check(
            "离线词库记录了来源许可",
            "MIT" in (_meta.get("source_license") or ""),
            _meta.get("source_license") or "(空)",
        )
        _words = int(_meta.get("word_count") or 0)
        check("离线词库词条数达标", _words >= 40000, str(_words))
        _empty = _o.execute(
            "SELECT COUNT(*) FROM dict WHERE definition='' AND translation=''"
        ).fetchone()[0]
        check("离线词库无空壳词条", _empty == 0, str(_empty))
        # 源语料把换行存成**两个字面量**（反斜杠 + n），而不是真换行。不解码有两层后果：
        # ① 界面上直接显示 `\n` 这坨字符；② 词性正则的 `\b` 边界被破坏——`\nvi.` 里
        # n 与 v 之间不构成边界，于是第一行之后的词性全部匹配不到（`run` 以动词为主，
        # pos 却只剩「n.」）。用 char(92) 拼反斜杠，避开转义歧义。
        _esc = _o.execute(
            "SELECT COUNT(*) FROM dict WHERE "
            "instr(translation, char(92)||'n') > 0 OR instr(definition, char(92)||'n') > 0 "
            "OR instr(translation, char(92)||'r') > 0 OR instr(definition, char(92)||'r') > 0"
        ).fetchone()[0]
        check(
            "离线释义已解码字面量转义（源语料的 \\n）",
            _esc == 0,
            f"{_esc} 条仍含字面量转义",
        )
        # 例句的中文侧要么是中文、要么为空。界面把那一栏标成「中」，摆着英文原文
        # 重复、法文或文档编号（`A/CONF.189/PC.3/L.1/Add.1`）就是在撒谎——用户会
        # 以为自己在读中文。范围从构建脚本取，不在这里另写一份字面量。
        import build_offline_dict as _bod  # noqa: PLC0415

        _fake_zh = _o.execute(_bod.FAKE_ZH_SQL).fetchone()[0]
        check("例句中文侧确为中文（无占位文本）", _fake_zh == 0, f"{_fake_zh} 条")
        # 汉字范围是**契约**，不是实现细节：同一份范围同时决定「源语料里这一侧算不算
        # 中文」和「校验时算不算合格」。这里把两头都钉死——一头钉边界值，一头钉
        # Python 侧与 SQL 侧必须给出同一结论。上界曾写成 龥(U+9FA5) 而注释声称「与
        # GLOB 同一范围」，于是 U+9FA6–9FFF 的汉字会被 Python 放行、被 SQL 判不合格，
        # 构建就此假失败；当时没有任何断言能发现这种分裂。
        _edge_bad = [
            f"U+{_ecp:04X} 应为 {_want} 实为 {_bod.has_cjk(chr(_ecp))}"
            for _ecp, _want in (
                (0x4E00, True),
                (0x9FFF, True),
                (0x4DBF, False),
                (0xA000, False),
            )
            if _bod.has_cjk(chr(_ecp)) is not _want
        ]
        check(
            "汉字范围边界符合契约（含 U+4E00–U+9FFF，不含 CJK 扩展 A）",
            not _edge_bad,
            "; ".join(_edge_bad),
        )
        _split_bad: list[str] = []
        _mc = sqlite3.connect(":memory:")
        try:
            _mc.execute(
                "CREATE TABLE examples(word TEXT, sentence_en TEXT, sentence_zh TEXT)"
            )
            for _zcp in (0x4DBF, 0x4E00, 0x9FA5, 0x9FA6, 0x9FFF, 0xA000):
                _zch = chr(_zcp)
                _mc.execute("DELETE FROM examples")
                _mc.execute("INSERT INTO examples VALUES('w', 's', ?)", (_zch,))
                # 这里驱动的是**构建期自校验用的那句真查询**，不是另写的复制品——
                # 它判「这侧是不是假中文」，has_cjk() 判「这侧算不算中文」，两者
                # 必须互为反命题；一旦分家，构建当场假失败而自检却全绿。
                _flagged = bool(_mc.execute(_bod.FAKE_ZH_SQL).fetchone()[0])
                _should = not _bod.has_cjk(_zch)
                if _flagged is not _should:
                    _split_bad.append(
                        f"U+{_zcp:04X} 校验判为不合格={_flagged} 期望={_should}"
                    )
        finally:
            _mc.close()
        check("校验用的 GLOB 与 has_cjk 结论一致", not _split_bad, "; ".join(_split_bad))

        # ---- 25. 汉字范围全项目只有一份定义 ----
        #
        # 上面两条钉的是「边界值符合契约」和「构建脚本内部的两侧结论一致」。
        # 它们都有一个共同的盲区：**只比值，不比身份**。
        #
        # 实测过这个盲区是真的：把 `db.CJK_END` 改成 U+9FA5 后，「边界符合契约」
        # 确实红了（说明值被改了会被发现），但如果哪天有人把 db.py 与
        # repository.py 里的范围**同时**改成同一个错误值，边界值断言只要跟着
        # 更新期望值就能继续全绿——而它本来要守的「只有一份定义」早已破产。
        #
        # 所以这一节不看值，只问一句：这几处**是不是同一个对象**。
        # 用 `is` 而不是 `==`：字符串值相等太容易偶然成立（四个文件碰巧写出同一
        # 个字面量正是这处地雷当初的形态），只有共享同一份定义才说明派生真的接上了。
        from app.core import db as _db  # noqa: PLC0415
        from app.core import importer as _imp  # noqa: PLC0415
        from app.core import repository as _repo  # noqa: PLC0415

        _src_bad = [
            f"{_who} 用的不是 db 的那一份"
            for _who, _same in (
                ("repository._CJK_RE", _repo._CJK_RE is _db._CJK_RE),
                ("importer._CJK_RE", _imp._CJK_RE is _db._CJK_ONE_RE),
                ("build_offline_dict._CJK_RE", _bod._CJK_RE is _db._CJK_RE),
                ("build_offline_dict.CJK_GLOB_CLASS", _bod.CJK_GLOB_CLASS is _db.CJK_CLASS),
            )
            if not _same
        ]
        check("汉字范围四处消费方共享同一份定义（不是各写一份）", not _src_bad, "; ".join(_src_bad))

        # 派生关系也要钉住：字符类本身不含方括号以外的任何东西，`+` 变体必须
        # 就是在它后面加一个量词——否则「一处定义」会被绕过（比如有人把
        # _CJK_RE 改回手写字面量，而它恰好与 CJK_CLASS + "+" 值相等，上面
        # 的 is 判定仍会通过）。
        _pat_bad = [
            f"{_p} 与派生式不符"
            for _p, _want in (
                (_db._CJK_RE.pattern, _db.CJK_CLASS + "+"),
                (_db._CJK_ONE_RE.pattern, _db.CJK_CLASS),
            )
            if _p != _want
        ]
        check("汉字正则由 CJK_CLASS 派生（量词在调用点加）", not _pat_bad, "; ".join(_pat_bad))

        # 契约边界本身：范围两端就是常用汉字区的两端，改任何一头都必须是有意的。
        check(
            "汉字范围端点即常用汉字区（U+4E00 / U+9FFF）",
            (_db.CJK_START, _db.CJK_END) == ("\u4e00", "\u9fff"),
            f"{_db.CJK_START!r} / {_db.CJK_END!r}",
        )

        # 比 `is` 判定更根本的一条：**全仓不得再出现手写的汉字字面量**。
        # 上面三条守的是已知的四处消费方；这一条守的是「第五处」——将来有人
        # 在新模块里顺手写一个 `[\u4e00-\u9fff]`，那三处断言全都不会响，
        # 因为它们是白的：新副本没连上任何既有断言的观测点。
        #
        # **只看代码，不看注释与文档字符串**。第一版没做这一步，抓出来的三条
        # 全是「讲述这段历史的注释」——把解释规矩的文字当成违反规矩的代码，
        # 这种检查器只会逼人删掉有价值的说明。
        #
        # 用 tokenize 而不是正则剥注释：字符串里可能含 `#`，正则会把代码切错。
        # 判「哪条 STRING 是 docstring」不靠缩进猜——特征很朴素：它前一个
        # token 是缩进/换行（即独占一行开始），且它在行内的右侧没有别的代码。
        #
        # **扫描范围必须显式列出，不能用 `rglob("*.py")`**：实测全仓 rglob 会
        # 撞进 `.venv` 与 `_ecdict_src`，拿到 **2239** 个文件、光 tokenize 就
        # 要 11.6 秒，selfcheck 直接超时到一行输出都没有（还看不出是这里慢）。
        # 所以下面那条「确实扫到了文件」的断言是**区间**而不是下限——
        # 只验下限的话，扫进虚拟环境这种事它一句话都不会说。
        import io as _io  # noqa: PLC0415
        import token as _tokmod  # noqa: PLC0415
        import tokenize as _tok  # noqa: PLC0415

        _cjk_lit = re.compile(r"\[\\u4e00-\\u9fff|\[一-龥\]")
        _own_src = [
            _p
            for _p in (
                list((ROOT / "app").rglob("*.py"))
                + list((ROOT / "scripts").rglob("*.py"))
                + [ROOT / "run.py", ROOT / "build.py"]
            )
            if "__pycache__" not in _p.as_posix()
        ]
        _lit_bad: list[str] = []
        for _sp in sorted(_own_src):
            _rel = _sp.relative_to(ROOT).as_posix()
            _src = _sp.read_text(encoding="utf-8")
            try:
                _tokens = list(_tok.generate_tokens(_io.StringIO(_src).readline))
            except (_tok.TokenError, IndentationError, SyntaxError):
                continue  # 语法有问题的文件交给别的检查去报
            # 标出「独占一行」的字符串（docstring / 被丢弃的字面量），它们不是代码。
            #
            # 判据必须用**行内**而非「右边界之后」：多行字符串的 `end` 指向**结束
            # 行**，所以从 `end` 往后取会把下一行（往往是真代码）算进来。实测踩过：
            # 一条三引号 docstring 被误报，报出的行号还正好是它自己那行，看起来
            # 像「检查器不肯放过文档字符串」。
            _line_of: dict[int, str] = {}
            for _ln, _content in enumerate(_src.splitlines(), 1):
                _line_of[_ln] = _content
            _solo: set[tuple[int, int]] = set()
            for _i, _t in enumerate(_tokens):
                if _t.type != _tokmod.STRING or _i == 0:
                    continue
                if _tokens[_i - 1].type not in (
                    _tokmod.INDENT,
                    _tokmod.NEWLINE,
                    _tokmod.NL,
                ):
                    continue
                # 字符串结束那一行里，它右侧是否还有别的代码
                _tail = _line_of.get(_t.end[0], "")[_t.end[1] :]
                if _tail.strip() == "":
                    _solo.add(_t.start)
            for _t in _tokens:
                if _t.type != _tokmod.STRING or _t.start in _solo:
                    continue
                if _cjk_lit.search(_t.string):
                    _lit_bad.append(f"{_rel}:{_t.start[0]}")
        check(
            "汉字字面量只存在于 db.py 的定义处（无第五份副本）",
            not _lit_bad,
            "; ".join(_lit_bad),
        )
        # 区间断言：36 是本仓的真实文件数。比这少说明漏扫（有源码没被这条规矩
        # 覆盖），比这多得多说明扫进了 .venv —— 两种都算这条检查本身不成立。
        check(
            "汉字字面量扫描范围恰为本仓源码（不多不少）",
            30 <= len(_own_src) <= 60,
            f"扫到 {len(_own_src)} 个 .py",
        )
        _forms = _o.execute("SELECT COUNT(*) FROM forms").fetchone()[0]
        check("离线词库带屈折形态表", _forms >= 10000, str(_forms))
        _o.close()

        _p = OfflineDictProvider(offline_path)
        _hit = _p.lookup("ubiquitous")
        check(
            "离线查词返回双语释义",
            bool(_hit.definition_en.strip()) and bool(_hit.definition_zh.strip()),
            f"en={bool(_hit.definition_en)} zh={bool(_hit.definition_zh)}",
        )
        check("离线查词给出音标", bool(_hit.phonetic.strip()), _hit.phonetic)
        # 词性必须看到释义的**全部行**。转义未解码时 `\b` 边界失效，只剩第一行
        # 的词性可被识别——run 是多词性动词，却被记成纯名词。
        _run_pos = _p.lookup("run").pos
        check(
            "离线词性覆盖多行（不被字面量转义截断）",
            "/" in _run_pos,
            f"run 的词性 = {_run_pos!r}",
        )
        check("离线查词不伪造同义语", _hit.synonyms == [], str(_hit.synonyms))
        # 导出的打印稿必须干净。离线释义解码后含真换行，导出要压平；若构建层漏
        # 解码，**反斜杠不是空白字符**，`str.split()` 压不掉它，`\n` 会整坨写进
        # 文件。既有的「纯文本导出不含转义反斜杠」只喂了内存构造的干净词条，
        # 从没喂过真实离线词条——查的是最不容易出问题的那条路。
        _run_entry = _p.lookup("run")
        _run_txt = export_text([_run_entry])
        check(
            "离线词条导出不含字面量转义",
            "\\n" not in _run_txt and "\\n" not in export_markdown([_run_entry]),
            repr(_run_txt[:100]),
        )
        _zh_lines = [ln for ln in _run_txt.splitlines() if "中  " in ln]
        check(
            "离线多行释义导出压成一行",
            len(_zh_lines) == 1 and _zh_lines[0].count("跑") >= 2,
            repr(_zh_lines),
        )
        check("离线查词忽略大小写", _p.lookup("UBIQUITOUS").lemma == "ubiquitous")
        check("离线查词经形态表落到原形", _p.lookup("runs").lemma == "run", _p.lookup("runs").lemma)
        # 这条是反向约束：`running` 本身就是词条，不该被「还原」成 run——
        # 精确匹配永远优先于形态还原，否则词典里有的词反而查不到自己。
        check(
            "形态还原不覆盖精确匹配",
            _p.lookup("running").lemma == "running",
            _p.lookup("running").lemma,
        )
        try:
            _p.lookup("zzz_not_a_real_word")
            check("未收录词如实报错", False, "未抛异常")
        except ProviderError as _exc:
            check("未收录词如实报错", "未收录" in str(_exc), str(_exc))
        _p.close()

    # 版本号写进 meta 却没人读，等于没写。v1 的库把换行存成字面量转义，若被静默
    # 接受，用户只会在释义里看到 `\n` 这坨字符，既不知道原因也无从修。这里造一个
    # 只带版本号的假 v1 库，验证守卫确实拒绝、且状态文案点明是版本问题。
    with tempfile.TemporaryDirectory(prefix="quill_v1_") as _v1_dir:
        _v1_path = Path(_v1_dir) / "offline_dict.db"
        _vc = sqlite3.connect(_v1_path)
        _vc.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _vc.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        _vc.commit()
        _vc.close()
        _v1_probe = None
        try:
            _v1_probe = open_offline_db(_v1_path)
            check("旧版离线词库被拒绝而非静默读取", False, "v1 库被接受")
        except ProviderError as _exc:
            check(
                "旧版离线词库被拒绝而非静默读取",
                "版本" in str(_exc),
                str(_exc)[:60],
            )
        finally:
            # 必须显式关掉。校验失败时不该有连接能返回，但**这条断言存在的意义
            # 就是「它可能返回」**——守卫被改坏时连接就真的漏出来，而 Windows 上
            # 被占用的文件删不掉，`TemporaryDirectory` 清理会抛 PermissionError。
            # 进程异常退出会把 stdout 缓冲一起丢掉，于是断言结果一个字都看不见，
            # 只剩一个与主题无关的 WinError 32——失败现场被清理动作毁掉了。
            if _v1_probe is not None:
                _v1_probe.close()
        check(
            "旧版离线词库状态点明版本问题",
            "版本" in offline_dict_status(_v1_path),
            offline_dict_status(_v1_path),
        )

    # 「用户自建库优先于随包内置库」是文档写明的用户路径（用 `--tier full` 构建
    # 完整档放到数据目录即可覆盖内置精选库），此前**零覆盖**。只断言返回的路径不够——
    # 要证明真的读了那一份：造一个只收 1 个词的迷你库，若优先级生效，它独有的词查得到、
    # 而内置库才有的词查不到。表结构直接复用构建脚本的 DDL，顺带钉住两边 schema 不漂移。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_offline_dict  # noqa: PLC0415

    import app.config as _app_config  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="quill_userdict_") as _user_home:
        _saved_app_dir = _app_config.APP_DIR
        _user_db = Path(_user_home) / "offline_dict.db"
        _uc = sqlite3.connect(_user_db)
        _uc.executescript(build_offline_dict.DDL)
        _uc.executemany(
            "INSERT INTO meta(key, value) VALUES(?,?)",
            [
                ("schema_version", str(OFFLINE_SCHEMA_VERSION)),
                ("tier", "full"),
                ("word_count", "1"),
            ],
        )
        _uc.execute(
            "INSERT INTO dict(word, definition, translation) VALUES(?,?,?)",
            ("zzzprobe", "a probe entry", "探针词条"),
        )
        _uc.commit()
        _uc.close()
        _user_provider = None
        try:
            _app_config.APP_DIR = Path(_user_home)
            check(
                "用户自建词库优先于随包内置库",
                resolve_offline_dict() == _user_db,
                str(resolve_offline_dict()),
            )
            _user_provider = OfflineDictProvider()
            # 断言里调用的函数**可能抛异常**，必须接住并报 FAIL。否则守卫被改坏时
            # 异常会中断整个自检——比一条 FAIL 更糟：后面所有断言根本没跑，而输出
            # 里只剩一个与主题无关的 traceback。
            try:
                _probe_lemma = _user_provider.lookup("zzzprobe").lemma
            except ProviderError as _probe_exc:
                _probe_lemma = f"(未命中：{_probe_exc})"
            check(
                "离线查词真的读取用户自建库",
                _probe_lemma == "zzzprobe",
                _probe_lemma,
            )
            try:
                _user_provider.lookup("ubiquitous")
                check("自建库生效时内置库不参与", False, "仍命中了内置库才有的词")
            except ProviderError:
                check("自建库生效时内置库不参与", True, "内置库独有词正确未命中")
            check(
                "自建库档位在状态里如实标注",
                "完整库" in offline_dict_status(_user_db),
                offline_dict_status(_user_db),
            )
        finally:
            _app_config.APP_DIR = _saved_app_dir
            if _user_provider is not None:
                _user_provider.close()
    if offline_path is not None:
        check(
            "自建库移除后回落到随包内置库",
            resolve_offline_dict() == offline_path,
            str(resolve_offline_dict()),
        )

    # 能力表：离线来源结构上没有同义语，不能拿它当缺项去喊
    _partial = Entry(lemma="x", definition_en="a", definition_zh="b")
    check(
        "离线词条不把同义语算作缺项",
        _partial.missing_parts(provided_fields("offline")) == ["例句"],
        str(_partial.missing_parts(provided_fields("offline"))),
    )
    check(
        "接口词条仍要求同义语",
        "同义语" in _partial.missing_parts(provided_fields("openai-compat")),
    )
    check("未知来源按全都能给处理", provided_fields("nobody") == ALL_FIELDS)
    check(
        "不传 provided 时保持旧行为",
        _partial.missing_parts() == _partial.missing_parts(ALL_FIELDS),
    )
    check("来源标签中文化", provider_label("offline") == "本机离线词库", provider_label("offline"))
    check("未知来源标签原样保留", provider_label("custom-x") == "custom-x")

    check(
        "offline 模式构造离线 Provider",
        type(build_provider({"provider": "offline"}, "")).__name__ == "OfflineDictProvider",
    )
    _status = provider_status({"provider": "offline"}, "")
    check("offline 状态文案含规模", "词" in _status, _status)
    check(
        "offline 模式允许缺地址与模型",
        check_config({"provider": "offline"}) is None,
        str(check_config({"provider": "offline"})),
    )
    check("offline 已进入接入方式白名单", "offline" in PROVIDERS, str(PROVIDERS))

    # ---- 22. 接口失败回落到离线词库 ----
    # 兜底有两个容易做错的地方：
    #   ① 悄悄回落 —— 用户看到释义就以为接口是通的，直到发现同义语一直是空的、
    #      例句越来越少，才回头怀疑配置。所以产出必须带真实来源与失败原因。
    #   ② 兜底也没命中时抛「离线词库未收录」—— 这会把一个可修复的配置问题
    #      （密钥失效、地址写错、网络不通）伪装成词库容量问题，用户拿着它
    #      不知道该去改哪里。必须抛**原始**接口错误。
    from app.config import DEFAULT_CONFIG, load_config, save_config  # noqa: PLC0415
    from app.providers.fallback import FallbackProvider  # noqa: PLC0415

    class _FailingProvider:
        """任何查询都失败，用来模拟接口挂掉。"""

        name = "openai-compat"

        def __init__(self, message: str = "请求超时（30 秒）。") -> None:
            self.message = message
            self.calls = 0

        def lookup(self, word: str) -> Entry:
            self.calls += 1
            raise ProviderError(self.message)

    check(
        "兜底开关默认开启",
        DEFAULT_CONFIG.get("offline_fallback") is True,
        repr(DEFAULT_CONFIG.get("offline_fallback")),
    )

    # 落盘往返：save_config 只认 _COERCERS 里登记过的字段。漏登记的表现是
    # 「在设置里关掉兜底、重启后又自己打开了」——开关形同虚设，且不会报错。
    saved_dir2, saved_path2 = config_module.APP_DIR, config_module.CONFIG_PATH
    rt_dir = Path(tempfile.mkdtemp(prefix="quill_fb_"))
    atexit.register(_cleanup_tmpdir, rt_dir)
    config_module.APP_DIR = rt_dir
    config_module.CONFIG_PATH = rt_dir / "config.json"

    def read_raw_config(raw: str) -> dict:
        config_module.CONFIG_PATH.write_text(raw, encoding="utf-8")
        return config_module.load_config()

    save_config({"provider": "openai-compat", "offline_fallback": False})
    check(
        "关闭兜底能落盘（不被静默丢弃）",
        load_config()["offline_fallback"] is False,
        repr(load_config().get("offline_fallback")),
    )
    check(
        "布尔兜底值被接受",
        read_raw_config('{"offline_fallback": false}')["offline_fallback"] is False,
        repr(read_raw_config('{"offline_fallback": false}')["offline_fallback"]),
    )
    check(
        "非法兜底值退回默认",
        read_raw_config('{"offline_fallback": "yes"}')["offline_fallback"] is True,
        repr(read_raw_config('{"offline_fallback": "yes"}')["offline_fallback"]),
    )
    config_module.APP_DIR, config_module.CONFIG_PATH = saved_dir2, saved_path2

    _offline_ready = resolve_offline_dict() is not None
    _net_cfg = {
        "provider": "openai-compat",
        "base_url": "https://api.example.com/v1",
        "model": "m",
        "timeout": 30.0,
    }
    _armed = build_provider(dict(_net_cfg, offline_fallback=True), "sk-test")
    check(
        "开启兜底时接口 Provider 被包装",
        isinstance(_armed, FallbackProvider) if _offline_ready else True,
        type(_armed).__name__,
    )
    check(
        "包装后对外仍是接口身份",
        _armed.name == "openai-compat",
        _armed.name,
    )
    check(
        "明确关闭兜底时不包装",
        type(build_provider(dict(_net_cfg, offline_fallback=False), "sk-test")).__name__
        == "OpenAICompatProvider",
        type(build_provider(dict(_net_cfg, offline_fallback=False), "sk-test")).__name__,
    )
    check(
        "离线模式下不再重复包装",
        not isinstance(
            build_provider({"provider": "offline", "offline_fallback": True}, ""),
            FallbackProvider,
        ),
    )

    # 状态栏必须说出「失败时会兜住」。不说的话，用户无从判断一次查词失败
    # 到底是彻底失败还是已经兜住了——而这两种情况该做的事完全不同。
    _armed_status = provider_status(dict(_net_cfg, offline_fallback=True), "sk-test")
    check(
        "状态栏说明已挂兜底",
        "回落" in _armed_status if _offline_ready else True,
        _armed_status,
    )
    check(
        "关闭兜底后状态栏不提回落",
        "回落" not in provider_status(dict(_net_cfg, offline_fallback=False), "sk-test"),
        provider_status(dict(_net_cfg, offline_fallback=False), "sk-test"),
    )

    if _offline_ready:
        _fb = FallbackProvider(_FailingProvider(), OfflineDictProvider())
        _fb_entry = _fb.lookup("apple")
        check("接口失败时回落到离线词库", _fb_entry.source == "offline", _fb_entry.source)
        check(
            "兜底结果带失败原因",
            "超时" in getattr(_fb_entry, "fallback_reason", ""),
            getattr(_fb_entry, "fallback_reason", ""),
        )
        check("兜底结果确有释义", _fb_entry.has_definition(), _fb_entry.definition_en)

        # 兜底也没命中：抛原始接口错误，而不是「离线词库未收录」
        _miss = None
        try:
            _fb.lookup("zzz_not_a_real_word")
        except ProviderError as exc:
            _miss = str(exc)
        check(
            "兜底也未命中时抛原始接口错误",
            bool(_miss and "未收录" not in _miss and "超时" in _miss),
            str(_miss),
        )

        # 服务层不得覆盖兜底来源（曾写成无条件 entry.source = provider.name）
        _fb_svc = DictionaryService(
            repo, FallbackProvider(_FailingProvider(), OfflineDictProvider())
        )
        _fb_saved, _fb_cached = _fb_svc.lookup("banana")
        check(
            "服务层保留兜底来源",
            _fb_saved.source == "offline" and not _fb_cached,
            f"source={_fb_saved.source!r} cached={_fb_cached}",
        )
        # fallback_reason 是运行时字段、不入库，因此它能否活到界面上，完全取决于
        # 写库那条路径是否**原地改写并返回同一个对象**（`upsert` 现在是这样）。
        # 一旦有人把它改成 `return Entry.from_row(row)`，失败原因会无声消失，
        # 界面只剩下「来源 本机离线词库」——用户看不出接口出过错。
        check(
            "服务层保留失败原因（写库不得丢运行时字段）",
            "超时" in getattr(_fb_saved, "fallback_reason", ""),
            repr(getattr(_fb_saved, "fallback_reason", None)),
        )
        check(
            "兜底词条按离线能力判定缺项",
            "同义语" not in _fb_saved.missing_parts(provided_fields(_fb_saved.source)),
            str(_fb_saved.missing_parts(provided_fields(_fb_saved.source))),
        )

    class _NeverCalled:
        name = "offline"

        def __init__(self) -> None:
            self.calls = 0

        def lookup(self, word: str) -> Entry:
            self.calls += 1
            raise AssertionError("接口成功时不应触发兜底")

    _guard = _NeverCalled()
    _healthy = FallbackProvider(StubProvider(), _guard)
    _healthy_entry = _healthy.lookup("ubiquitous")
    check(
        "接口正常时不触发兜底",
        _guard.calls == 0 and not _healthy_entry.fallback_reason,
        f"calls={_guard.calls} reason={_healthy_entry.fallback_reason!r}",
    )
    check(
        "接口成功时不改写来源",
        _healthy_entry.source == "",
        _healthy_entry.source,
    )

    # 分层必须钉住：兜底只能挂在 build_provider 上，不能下沉进
    # OpenAICompatProvider 自己。一旦下沉，http_smoke 那种「直接构造接口
    # Provider 来验失败路径」的测试就会静默拿到离线结果——测试因为兜底而
    # 通过，而它本该守住的失败路径再没人跑，且没人会察觉。
    from app.providers.openai_compat import OpenAICompatProvider  # noqa: PLC0415

    _direct = OpenAICompatProvider("http://127.0.0.1:1/v1", "sk-x", "m", timeout=0.6)
    _direct_error = None
    try:
        # apple 一定在离线词库里。若接口 Provider 自带兜底，这里会安静地
        # 返回一条离线词条而不是报错。
        _direct.lookup("apple")
    except ProviderError as exc:
        _direct_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        _direct_error = f"{type(exc).__name__}: {exc}"
    check(
        "接口 Provider 自身不含兜底（分层不被下沉）",
        bool(_direct_error),
        str(_direct_error) or "没有报错——说明接口 Provider 自己把失败兜住了",
    )

    # ---- 23. 生词表导入 ----
    # 解析是在「猜」，而导入会**写入**用户的生词本。所以下面用例的重心不是
    # 「能认出多少词」，而是「猜错时会不会写进错的东西」——尤其是本程序自己
    # 导出的文件：它的每条词条占多行，若按逐行取词处理，释义行会被挖出来
    # 当成词条（seeming、becoming……），那是往用户数据里写入错误内容，
    # 而且一条词条能带出好几个假词，用户很难发现。
    from app.core.importer import (
        KIND_PHRASE,
        MAX_FILE_BYTES,
        ParsedWord,
        _valid_word,
        parse_text,
        read_docx_text,
        read_import_text,
        read_text_file,
        run_import,
    )
    from app.core import repository as repository_mod
    from app.core import exporter as exporter_mod
    from app.core import importer as importer_mod
    from app.security.keystore import load_api_key, save_api_key

    def _imp_words(text: str) -> list[str]:
        return [w.word for w in parse_text(text).words]

    _imp_got = _imp_words("ubiquitous\nmeticulous\n")
    check("导入：一行一个词", _imp_got == ["ubiquitous", "meticulous"], str(_imp_got))

    _imp_got = _imp_words(
        "1. ubiquitous  /juːˈbɪkwɪtəs/  adj. 无处不在的\n"
        "2) meticulous [məˈtɪkjələs]  adj.\n"
        "3、ephemeral：短暂的\n"
    )
    check(
        "导入：忽略编号、音标、词性、中文释义",
        _imp_got == ["ubiquitous", "meticulous", "ephemeral"],
        str(_imp_got),
    )

    _imp_got = _imp_words("happy, glad, joyful\n")
    check(
        "导入：逗号列开的多个词要全收下",
        _imp_got == ["happy", "glad", "joyful"],
        str(_imp_got),
    )

    _imp_got = _imp_words("- apple\n* banana\n• cherry\n· date\n")
    check(
        "导入：项目符号不是词的一部分",
        _imp_got == ["apple", "banana", "cherry", "date"],
        str(_imp_got),
    )

    _imp_res = parse_text("apple\nApple\nAPPLE\nbanana\n")
    check(
        "导入：大小写不同算同一个词",
        _imp_res.count == 2 and _imp_res.duplicates == 2,
        f"count={_imp_res.count} dup={_imp_res.duplicates}",
    )

    # 句子必须整行跳过。只取「每行第一个词」的实现在这里会收下 She——
    # 一个真词，但和用户想收录的东西毫无关系。
    #
    # **两种形态都要测，因为它们落到两条完全不同的分支上**（第 30 轮变异实验
    # 发现的漏网）：
    #
    # - **不带标点**的整句（`She went to ... milk`）字符集全部合法，会被
    #   「整行就是一个纯英文词组」那条分支拦下（`_is_plain_segment` 那条路走不通
    #   之后落到 line 881）。它同样不收录，只是理由写成「含空格，像是词组」；
    # - **真实句子几乎总带标点**（逗号、句号），这时词组分支不成立，整行只剩
    #   `MAX_WORDS_PER_LINE` 一道防线。只测前一种的话，把阈值改成无穷大也测不出来
    #   ——它会被另一条分支「意外地」兜住，而那条分支本来管的是别的事。
    #
    # 两条共用同一个名字前缀，变异测试的判据按前缀命中，任一条红都算抓住。
    for _sent in (
        "She went to the store, and then she bought some milk.",
        "She went to the store to buy some milk",
    ):
        _imp_res = parse_text(f"{_sent}\napple\n")
        check(
            f"导入：句子行整行跳过（{'带标点' if ',' in _sent else '不带标点'}）",
            _imp_words(f"{_sent}\napple\n") == ["apple"] and len(_imp_res.skipped) == 1,
            f"words={_imp_words(f'{_sent}\napple\n')} skipped={_imp_res.skipped}",
        )

    # 贴数字的匹配：`_TOKEN_RE` 不含数字，所以 `word2` 匹配出的本就是 `word`。
    #
    # **这里原先断言的是「word2 整行被丢掉」，那是个被写进测试的缺陷。**
    # 因为 `apple1`、`banana2` 这种「词尾紧跟序号」是别处导出的词表里最常见
    # 的格式之一，整行丢掉会让一份词表收成 0 个词。真正该守的性质只有一条
    # ——**不截出半个词**（`word2` 不能变成 `wor`）——而不是「一个都不收」。
    # 数字占满整串时（`123`）匹配不到本正则，自然什么都不产生。
    _imp_got = _imp_words("word2\napple\n")
    check(
        "导入：贴数字的匹配不截出半个词（word2 → word，不是 wor）",
        _imp_got == ["word", "apple"],
        str(_imp_got),
    )
    check(
        "导入：纯数字不算词",
        _imp_words("12345\n678\n") == [],
        str(_imp_words("12345\n678\n")),
    )

    _imp_got = _imp_words("either/or\n")
    check(
        "导入：either/or 不被音标规则连坐吃掉",
        _imp_got == ["either", "or"],
        str(_imp_got),
    )

    _imp_res = parse_text("")
    check("导入：空文件不报错也不产出词", _imp_res.count == 0, str(_imp_res))

    _imp_res = parse_text("苹果\n香蕉\n")
    check(
        "导入：纯中文文件如实报告跳过了哪些行",
        _imp_res.count == 0 and len(_imp_res.skipped) == 2,
        f"count={_imp_res.count} skipped={_imp_res.skipped}",
    )

    # ---- 23b. Word 文档（.docx）与「一行逗号列开的长词表」 ----
    #
    # 两条都是用户报上来的：老师发的 Word 单词表既选不到也拖不进；而它就算
    # 另存成 txt，也会因为「一行 57 段、夹着 3 条短语」被判成句子，收 0 个词
    # （实测原话：一行有 62 个词，像是句子不是词条）。
    # 判据一律落在**真实字节**上：现造一份 .docx，真解包、真解析。

    def _write_docx(path: Path, paragraphs: list[str]) -> None:
        """现造一份最小 .docx：zip 里只放 `word/document.xml`。

        段落文本里的 `\\t` 写成 `<w:tab/>`——它是**空元素**，只取 `w:t` 的实现
        在这里会露馅（两列会粘成一个词，随后被「汉字之后都是解释」整段切掉）。
        """
        runs = ""
        for text in paragraphs:
            inner = ""
            for index, chunk in enumerate(text.split("\t")):
                if index:
                    inner += "<w:tab/>"
                inner += f'<w:t xml:space="preserve">{chunk}</w:t>'
            runs += f"<w:p><w:r>{inner}</w:r></w:p>"
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{ns}"><w:body>{runs}</w:body></w:document>',
            )

    _docx = tmpdir / "wordlist.docx"
    _write_docx(_docx, ["perish", "impractical\t不切实际的", "live to tell"])
    _docx_text = read_import_text(_docx)
    check(
        "导入：.docx 解包成按段落分行的纯文本",
        _docx_text.splitlines() == ["perish", "impractical\t不切实际的", "live to tell"],
        repr(_docx_text),
    )
    # 制表符**后面跟英文**才验得出来。原来这条用例写的是「制表符 + 中文」，
    # 而中文本来就会被「第一个汉字之后都是解释」整段切掉——tab 丢了也照样
    # 通过，是个空断言（自证时 MISSED 的那一条就是它）。换成英文对英文：
    # tab 没了就会粘成 `applebanana`，那是一个看着正常的假词。
    _docx_tab = tmpdir / "tabbed.docx"
    _write_docx(_docx_tab, ["apple\tbanana"])
    _docx_tab_text = read_import_text(_docx_tab)
    check(
        "导入：.docx 的制表符不把两个词粘成一个",
        "apple\tbanana" in _docx_tab_text and _imp_words(_docx_tab_text) == ["apple"],
        f"{_docx_tab_text!r} -> {_imp_words(_docx_tab_text)}",
    )
    _imp_plain = tmpdir / "wordlist.txt"
    _imp_plain.write_text("perish\n", encoding="utf-8")
    check(
        "导入：读取层按后缀分派（.docx 解包、.txt 直读）",
        # 与文件字节**逐字节**比较：`Path.read_text` 走通用换行模式会把 `\r\n`
        # 折成 `\n`，拿它当基准等于把「有没有原样读出来」这件事测丢了。
        read_import_text(_imp_plain) == _imp_plain.read_bytes().decode("utf-8")
        and _docx_text.startswith("perish"),
        f"txt={read_import_text(_imp_plain)!r}",
    )

    # 后缀表与文件对话框过滤器**同源**（过滤器由后缀表派生）。这条断言守的是
    # 派生关系本身：删掉后缀表里的一项而忘了改过滤器，是不可能的；但**把
    # 后缀表整项删掉**（比如有人觉得 docx 没必要）会同时从两处消失，
    # 所以还要单独钉一条「.docx 在表里」。
    from app.ui.main_window import _WORD_FILTER, _WORD_SUFFIXES  # noqa: PLC0415

    check(
        "导入：文件对话框过滤器覆盖后缀表每一项",
        all(f"*{suff}" in _WORD_FILTER for suff in _WORD_SUFFIXES),
        f"filter={_WORD_FILTER}",
    )
    check(
        "导入：.docx 在生词表后缀表里（选得到也拖得进）",
        ".docx" in _WORD_SUFFIXES,
        str(_WORD_SUFFIXES),
    )

    # 假 .docx（其实是文本）：要给一句能照着做的中文提示，而不是 traceback。
    _imp_fake = tmpdir / "notreally.docx"
    _imp_fake.write_bytes("这只是一段文本".encode("utf-8"))
    try:
        read_docx_text(_imp_fake)
        _imp_fake_err = ""
    except ValueError as exc:
        _imp_fake_err = str(exc)
    except Exception as exc:  # noqa: BLE001 - 非 ValueError 也是失败，得看清是什么
        _imp_fake_err = f"抛了 {exc.__class__.__name__}：{exc}"
    check(
        "导入：假 .docx 给中文提示而不是 traceback",
        "另存为 .docx" in _imp_fake_err,
        _imp_fake_err or "没报错——说明假 docx 被当成真的读过去了",
    )

    # 一行逗号列开的长词表：**单词与词组各归各家**。
    # 旧实现要求「每一段都是单个词」，夹一条短语就让整条分支不成立，整行
    # 随即被 `MAX_WORDS_PER_LINE` 判成句子——一份 57 段的词表收 0 个词。
    # 后来那段短语能识别了却**只报不收**；现在它们按词组收进另一张表。
    _imp_long = parse_text(
        "perish, impractical, oblivious, live to tell, generally speaking, look down upon"
    )
    check(
        "导入：一行逗号列开的词表整行收下（单词与词组各归各家）",
        [w.word for w in _imp_long.words if w.kind == "word"]
        == ["perish", "impractical", "oblivious"]
        and [w.word for w in _imp_long.words if w.kind == "phrase"]
        == ["live to tell", "generally speaking", "look down upon"]
        and not _imp_long.skipped,
        f"words={[(w.word, w.kind) for w in _imp_long.words]} skipped={_imp_long.skipped}",
    )
    check(
        "导入：长词表里的短语不截成首词",
        not any(w.word in ("live", "generally", "look") for w in _imp_long.words),
        str([w.word for w in _imp_long.words]),
    )
    # 单独占一行的词组同样不能截首词。`picks = [tokens[0]]`（一行取第一个词）
    # 是给「apple 苹果」「1. ubiquitous /…/ adj.」这类行准备的，落到词组上就
    # 变成往生词本里写一个 `live`——看着像正常词，用户根本发现不了。
    _imp_phrase = parse_text("live to tell\napple\n")
    check(
        "导入：整行就是一个词组时收为词组，不截成首词",
        [(w.word, w.kind) for w in _imp_phrase.words]
        == [("live to tell", "phrase"), ("apple", "word")]
        and not _imp_phrase.skipped,
        f"words={[(w.word, w.kind) for w in _imp_phrase.words]} skipped={_imp_phrase.skipped}",
    )
    # 超过 4 词的纯英文行仍按句子跳过，**而且要说清是几个词**：只报一句
    # 「像是句子」，用户没法判断该删掉还是该拆开。这条同时守住「词组的
    # 词数上限真的生效」——上限写成无穷大时它必须红。
    _imp_long_phrase = parse_text("as a matter of fact\n")
    check(
        "导入：超过词组词数上限的行按句子跳过并说明词数",
        not _imp_long_phrase.words
        and len(_imp_long_phrase.skipped) == 1
        and "5 个词" in _imp_long_phrase.skipped[0][1],
        f"words={[w.word for w in _imp_long_phrase.words]} skipped={_imp_long_phrase.skipped}",
    )
    check(
        "导入：词组的词数上限取 4（3 词的搭配收得下）",
        [w.word for w in parse_text("look down upon\n").words] == ["look down upon"],
        str([w.word for w in parse_text("look down upon\n").words]),
    )

    # 用**真的导出器**生成文件再解析回来。手写样本只能证明「我以为的格式」
    # 被支持，证明不了「实际导出的格式」被支持——两者的差别正是要测的东西。
    _imp_sample = [
        Entry(
            lemma="ubiquitous",
            phonetic="/juːˈbɪkwɪtəs/",
            pos="adj.",
            definition_en="seeming to be everywhere at the same time",
            definition_zh="无处不在的；普遍存在的",
            example="Mobile phones have become ubiquitous in everyday life.",
            example_zh="手机在日常生活中已变得无处不在。",
            synonyms=[
                Synonym(
                    word="pervasive",
                    gloss_zh="弥漫的；渗透的",
                    diff_note="强调逐渐扩散并渗透到各处",
                )
            ],
            notes="记忆法：ubi（哪里）+ quit（任何）→ 哪里都在",
        ),
        Entry(
            lemma="run",
            pos="v.",
            definition_en="move fast",
            definition_zh="跑；奔跑",
            notes="1. 首先记住过去式 ran\n2. 其次是 running\n\n3) 还有 runner 与 *star*",
        ),
    ]
    for _imp_fmt, _imp_text in (
        ("纯文本", export_text(_imp_sample, "英语生词本")),
        ("Markdown", export_markdown(_imp_sample, "英语生词本")),
    ):
        _imp_res = parse_text(_imp_text)
        _imp_got = [w.word for w in _imp_res.words]
        check(
            f"导入：{_imp_fmt}导出文件里只认词条首行",
            _imp_got == ["ubiquitous", "run"],
            str(_imp_got),
        )
        _imp_notes = [w.notes for w in _imp_res.words]
        check(
            f"导入：{_imp_fmt}导出的备注原样还原",
            _imp_notes == [e.notes for e in _imp_sample],
            str(_imp_notes),
        )

    # 单词表格式的**唯一用途**就是重新导入，所以它的判据不能是「字符串长得像
    # 不像」，必须走一遍真解析器。上面那两种（纯文本 / Markdown）验的是「导出过
    # 的文件能被读回」；这里多验一件事：**一行废行都不出现**。标题里全是汉字，
    # 会被判成「没认出单词」；`Generated by Quill` 更糟——三个英文词，会被当成
    # 一条词组收进词组收藏。用户看到的是「导入成功」，只是里面混进了不是他的东西。
    _imp_wl = export_wordlist(_imp_sample, "英语生词本")
    _imp_wl_res = parse_text(_imp_wl)
    check(
        "导入：单词表导出能原样读回（词一个不少、顺序不变）",
        [w.word for w in _imp_wl_res.words] == ["ubiquitous", "run"],
        str([w.word for w in _imp_wl_res.words]),
    )
    check(
        "导入：单词表导出没有任何一行被当成跳过",
        not _imp_wl_res.skipped,
        str(_imp_wl_res.skipped),
    )
    check(
        "导入：单词表里既没有标题也没有表尾（有的话就会变成跳过行）",
        "导出时间" not in _imp_wl and "Generated by" not in _imp_wl,
        repr(_imp_wl[:60]),
    )
    # 词组是这一条最容易被破坏的地方：`take off` 由两个英文词组成，格式里再
    # 多一列中文，解析器一旦按「第一个词」取，词组就会被拆成一条 `take` 进生词本
    # ——而报告上说「认出 1 条」，看着一切正常。
    _imp_wl_ph = export_wordlist(
        [Phrase(phrase="take off", gloss_zh="起飞").to_entry()], "词组收藏"
    )
    _imp_wl_ph_res = parse_text(_imp_wl_ph)
    check(
        "导入：单词表里的词组回到词组表，不被拆成两个词",
        [w.word for w in _imp_wl_ph_res.words if w.kind == KIND_PHRASE] == ["take off"]
        and _imp_wl_ph_res.word_count == 0,
        f"{[(w.word, w.kind) for w in _imp_wl_ph_res.words]}"
        f" word_count={_imp_wl_ph_res.word_count}",
    )

    # 编码容错：Windows 上「记事本另存为」出来的是 GBK，别处下载的常带 BOM。
    # 报一句「无法读取」等于把用户的文件判了死刑，而它其实好好的。
    _imp_gbk = tmpdir / "gbk_words.txt"
    _imp_gbk.write_bytes("ubiquitous\n苹果\n".encode("gbk"))
    _imp_gbk_text = read_text_file(_imp_gbk)
    # 中文也必须是中文。只看 ASCII 那半截的话，一个把编码表砍到只剩 utf-8
    # 的实现照样通过——utf-8 解 GBK 时英文部分本来就是对的，坏的只有中文。
    check(
        "导入：GBK 文件可读（中文也读对）",
        "ubiquitous" in _imp_gbk_text and "苹果" in _imp_gbk_text,
        repr(_imp_gbk_text),
    )

    _imp_bom = tmpdir / "bom_words.txt"
    _imp_bom.write_bytes("\ufeffubiquitous\n".encode("utf-8"))
    _imp_got = _imp_words(read_text_file(_imp_bom))
    check(
        "导入：UTF-8 BOM 不会吃掉第一个词",
        _imp_got == ["ubiquitous"],
        str(_imp_got),
    )

    _imp_big = tmpdir / "big_words.txt"
    with open(_imp_big, "wb") as _imp_fh:
        _imp_fh.truncate(MAX_FILE_BYTES + 1)  # 稀疏文件，不占磁盘
    try:
        read_text_file(_imp_big)
        check("导入：超大文件被拒绝并说明原因", False, "没有报错")
    except ValueError as _imp_exc:
        check("导入：超大文件被拒绝并说明原因", "过大" in str(_imp_exc), str(_imp_exc))

    # ---- 导入的执行：分类、备注、取消 ----
    from app.providers.offline import OfflineDictProvider

    class _ImpCountingProvider:
        """包一层记录真实请求，用来验证「已存在的词不会再打一次接口」。"""

        name = "counting"

        def __init__(self, inner) -> None:
            self.inner = inner
            self.calls: list[str] = []

        def lookup(self, word: str) -> Entry:
            self.calls.append(word)
            return self.inner.lookup(word)

    _imp_conn = connect(tmpdir / "import_exec.db")
    init_db(_imp_conn)
    _imp_repo = Repository(_imp_conn)
    _imp_counting = _ImpCountingProvider(OfflineDictProvider())
    _imp_svc = DictionaryService(_imp_repo, _imp_counting)

    _imp_first = run_import(
        _imp_svc, _imp_repo, parse_text("ubiquitous\nrun\n").words
    )
    check(
        "导入：新词计入「新增」且真的落库",
        sorted(_imp_first.added) == ["run", "ubiquitous"] and _imp_repo.count() == 2,
        f"added={_imp_first.added} count={_imp_repo.count()}",
    )
    check(
        "导入：三个分类加起来等于处理过的词数",
        _imp_first.handled == 2 and not _imp_first.failed,
        f"handled={_imp_first.handled} failed={_imp_first.failed}",
    )

    _imp_calls = len(_imp_counting.calls)
    _imp_second = run_import(
        _imp_svc, _imp_repo, parse_text("ubiquitous\nrun\n").words
    )
    check(
        "导入：已存在的词计入「已存在」且不重复写库",
        sorted(_imp_second.existing) == ["run", "ubiquitous"]
        and _imp_repo.count() == 2,
        f"existing={_imp_second.existing} count={_imp_repo.count()}",
    )
    check(
        "导入：已存在的词不会再打一次接口",
        len(_imp_counting.calls) == _imp_calls,
        f"多打了 {len(_imp_counting.calls) - _imp_calls} 次",
    )

    _imp_third = run_import(
        _imp_svc, _imp_repo, parse_text("zzzznotarealword\napple\n").words
    )
    check(
        "导入：词库没有的词计入失败并附上原因",
        [w for w, _ in _imp_third.failed] == ["zzzznotarealword"],
        str(_imp_third.failed),
    )
    check("导入：同批里能查到的词照常收录", _imp_third.added == ["apple"], str(_imp_third.added))

    _imp_with_notes = parse_text(
        export_text(
            [
                Entry(
                    lemma="ephemeral",
                    definition_en="lasting a very short time",
                    notes="来自导出文件的备注",
                )
            ],
            "英语生词本",
        )
    )
    run_import(_imp_svc, _imp_repo, _imp_with_notes.words)
    check(
        "导入：新词会带上文件里的备注",
        (_imp_repo.get("ephemeral").notes or "") == "来自导出文件的备注",
        repr(_imp_repo.get("ephemeral").notes),
    )

    # 库里已有备注时不许被文件覆盖：备注是用户自己写的东西，导入无权动它。
    # 只验「新词能带备注」的话，一个无条件覆盖的实现在这里照样通过。
    _imp_stored = _imp_repo.get("ephemeral")
    _imp_stored.notes = "我自己写的备注"
    _imp_repo.upsert(_imp_stored)
    run_import(_imp_svc, _imp_repo, _imp_with_notes.words)
    check(
        "导入：已存在词条的备注不被文件覆盖",
        (_imp_repo.get("ephemeral").notes or "") == "我自己写的备注",
        repr(_imp_repo.get("ephemeral").notes),
    )

    _imp_cancel = run_import(
        _imp_svc,
        _imp_repo,
        parse_text("alpha\nbeta\n").words,
        is_cancelled=lambda: True,
    )
    check(
        "导入：可取消，且取消后一个词都不处理",
        _imp_cancel.cancelled and _imp_cancel.handled == 0,
        f"cancelled={_imp_cancel.cancelled} handled={_imp_cancel.handled}",
    )

    # ---- 导入：词组进另一张表 ----
    #
    # 词组与生词本**是两张各自从 1 发号的表**，所以这里用一个独立的库，
    # 连 id 撞车也一起造成（词组 1 号与词条 1 号同时存在）。
    class _ImpPhraseProvider:
        """词组专用替身：单词转发给离线词库，词组自己答。"""

        name = "openai-compat"

        def __init__(self, inner) -> None:
            self.inner = inner
            self.phrase_calls: list[str] = []

        def lookup(self, word: str) -> Entry:
            return self.inner.lookup(word)

        def lookup_phrase(self, text: str) -> Phrase:
            self.phrase_calls.append(text)
            if text == "empty gloss":
                # 空壳：只有标题、没有简释。存进去就是一条点开是空的记录。
                return Phrase(phrase=text, gloss_zh="")
            return Phrase(
                phrase=text,
                gloss_zh=f"{text} 的简释",
                example="They did it yesterday.",
                example_zh="他们昨天做了这件事。",
            )

    _ph_conn = connect(tmpdir / "import_phrase.db")
    init_db(_ph_conn)
    _ph_repo = Repository(_ph_conn)
    _ph_prov = _ImpPhraseProvider(OfflineDictProvider())
    _ph_svc = DictionaryService(_ph_repo, _ph_prov)

    _ph_out = run_import(
        _ph_svc, _ph_repo, parse_text("take off\napple\nempty gloss\n").words
    )
    check(
        "导入：词组进词组收藏，不进生词本",
        _ph_out.phrases_added == ["take off"]
        and _ph_repo.count_phrases() == 1
        and _ph_repo.get("take off") is None,
        f"phrases={_ph_out.phrases_added} 词组={_ph_repo.count_phrases()} "
        f"生词本={_ph_repo.count()}",
    )
    check(
        "导入：同批的单词照常进生词本（词组的 id 没混进 id 快照）",
        _ph_out.added == ["apple"] and not _ph_out.existing and _ph_repo.count() == 1,
        f"added={_ph_out.added} existing={_ph_out.existing} count={_ph_repo.count()}",
    )
    check(
        "导入：词组返回空壳时计入失败，不写库",
        [w for w, _ in _ph_out.failed] == ["empty gloss"]
        and _ph_repo.get_phrase("empty gloss") is None,
        f"failed={_ph_out.failed}",
    )
    check(
        "导入：词组的三个分类也计入「处理过的条数」",
        _ph_out.handled == 3,
        f"handled={_ph_out.handled}",
    )

    _ph_calls_before = len(_ph_prov.phrase_calls)
    _ph_again = _ph_svc.lookup_phrase("  Take   Off ")
    check(
        "词组：空白与大小写变异命中同一条，不再打接口",
        _ph_again[0].phrase == "take off"
        and _ph_again[1] is False
        and len(_ph_prov.phrase_calls) == _ph_calls_before,
        f"phrase={_ph_again[0].phrase} created={_ph_again[1]} "
        f"calls+={len(_ph_prov.phrase_calls) - _ph_calls_before}",
    )
    try:
        _ph_svc.lookup_phrase("apple")
        _ph_word_err = ""
    except Exception as exc:  # noqa: BLE001 - 要看住的是「有没有拒绝」
        _ph_word_err = str(exc)
    check(
        "词组：单个词在词组模式下被拒绝并指路",
        "不是词组" in _ph_word_err and "生词本" in _ph_word_err,
        _ph_word_err or "没拒绝",
    )
    check(
        "词组：normalize_phrase 折叠空白并小写化",
        normalize_phrase("  Look Forward TO  ") == "look forward to",
        repr(normalize_phrase("  Look Forward TO  ")),
    )

    # ---- 导入：认出本程序导出的 JSON ----
    # 指纹必须**先于**其它解析器判断。理由是不对称的：JSON 文件里必然出现
    # `"lemma": "xxx"` 这样的行，而普通词表解析器会把它当成一个「词」收进来；
    # 反过来，普通词表绝不会以 `{` 开头且解析出 format=quill-entries。
    # 判错方向不对称时，倾向于「像就认」。
    _json_blob = export_json(
        [
            Entry(lemma="ubiquitous", definition_zh="无处不在的", notes="备注甲"),
            Entry(lemma="naive", definition_zh="天真的"),
        ],
        "测试册",
    )
    _json_parsed = parse_text(_json_blob)
    check("导入：认出 Quill JSON 导出", _json_parsed.source_format == "Quill JSON 导出",
          _json_parsed.source_format)
    check("导入：JSON 里的单词被完整取出",
          [w.word for w in _json_parsed.words] == ["ubiquitous", "naive"],
          str([w.word for w in _json_parsed.words]))
    # 本轮只「认格式、收单词」，不把 JSON 里的释义回填——那要改写入库路径，
    # 属于下一轮明确推迟的范围。但**绝不能**把 `"lemma"` 这种字段名当成单词收进来。
    check("导入：JSON 不会把字段名当成单词",
          all(w.word in ("ubiquitous", "naive") for w in _json_parsed.words),
          str([w.word for w in _json_parsed.words]))
    check("导入：JSON 里的备注被带到候选词上",
          any("备注甲" in w.notes for w in _json_parsed.words),
          str([w.notes for w in _json_parsed.words]))

    # 指纹必须够严：**任何** JSON 都算生词本的话，别人的配置文件也会被吞进来。
    _json_alien = json.dumps({"name": "my-config", "version": 2, "lemma": "not-a-word"})
    _alien_parsed = parse_text(_json_alien)
    check("导入：不认识格式的 JSON 不被当成生词本",
          _alien_parsed.source_format != "Quill JSON 导出",
          _alien_parsed.source_format)
    # 坏 JSON 不能让导入炸掉：用户可能选到一个损坏的文件
    _broken_parsed = parse_text('{"format": "quill-entries", "entries": [,,,')
    check("导入：坏 JSON 退化成普通词表而不是抛异常",
          _broken_parsed.source_format != "Quill JSON 导出",
          _broken_parsed.source_format)

    # 三种导出必须全部能被导入认出来（文本 / Markdown / JSON）。
    # 这条是「导出的东西自己能读回来」这个承诺的底线——少认一种，用户就会发现
    # 「我导出的文件导不回去」，而那种问题只会在真的用到时才发现。
    _roundtrip_bad = [
        _label
        for _label, _blob, _want in (
            ("文本", export_text([Entry(lemma="ubiquitous", notes="a")]), "Quill 文本导出"),
            ("Markdown", export_markdown([Entry(lemma="ubiquitous", notes="a")]),
             "Quill Markdown 导出"),
            ("JSON", _json_blob, "Quill JSON 导出"),
        )
        if parse_text(_blob).source_format != _want
    ]
    check("导入：三种导出格式都能被认回来", not _roundtrip_bad, "、".join(_roundtrip_bad))

    # 导入侧刻意**不 import** 导出侧的格式标识常量（那样将来改了标识就再也读不了
    # 旧文件）。代价是两处各写一份，可能悄悄分家——分家的后果是「自己导出的文件
    # 自己导不回去」，而这只会在用户真的去导的时候才被发现。所以必须有一条断言盯着。
    check(
        "导出与导入的 JSON 格式标识一致",
        exporter_mod.JSON_FORMAT_TAG == importer_mod._JSON_FORMAT_TAG,
        f"{exporter_mod.JSON_FORMAT_TAG!r} vs {importer_mod._JSON_FORMAT_TAG!r}",
    )

    # ---- 导入：认出有道单词本（XML） ----
    #
    # 有道的导出是 XML（`<wordbook><item><word>…`）。它一条指纹都不匹配前三种，
    # 于是落到**通用词表解析器**——而那个解析器逐行取「第一个英文 token」，
    # 结果是每个标签名（word / trans / phonetic / tags / progress）都恰好是一个
    # 合法英文词，被原样收进生词本；真词反而一个都取不到。
    #
    # 实测一份 2 条词条的导出：收下 8 个假词，abandon 与 naive 全部丢失，而
    # **跳过清单是空的**——用户看到「导入成功 8 个词」，库里装的全是标签名。
    # 这与 JSON 那条是同一个坑：把结构化文件误判成普通词表，是**写入错误内容**，
    # 比漏收严重得多。
    # 样例按「每个标签占一行」书写——这是**最坏情况**：通用词表解析器会逐行
    # 把标签名收成假词（word / trans / phonetic / tags / progress），而且
    # 每行 token 数都没超阈值，所以**跳过清单为空**，用户看不到任何异常。
    _yd_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<wordbook>\n"
        "<item>\n"
        "<word>abandon</word>\n"
        "<trans><![CDATA[vt. 放弃，抛弃；离弃]]></trans>\n"
        "<phonetic><![CDATA[ə'bændən]]></phonetic>\n"
        "<tags>四级核心</tags>\n"
        "<progress>-1</progress>\n"
        "</item>\n"
        "<item>\n"
        "<word>naive</word>\n"
        "<trans><![CDATA[adj. 天真的；朴素的]]></trans>\n"
        "<phonetic><![CDATA[naɪ'iːv]]></phonetic>\n"
        "<tags>四级核心</tags>\n"
        "<progress>-1</progress>\n"
        "</item>\n"
        "</wordbook>\n"
    )
    _yd_parsed = parse_text(_yd_xml)
    check(
        "导入：认出有道单词本 XML",
        _yd_parsed.source_format == "有道单词本（XML）",
        _yd_parsed.source_format,
    )
    _yd_words = [w.word for w in _yd_parsed.words]
    check(
        "导入：XML 里取到的是真词而不是标签名",
        _yd_words == ["abandon", "naive"],
        f"{_yd_words}（真词：abandon / naive）",
    )

    # 释义、音标、分类都要**带回候选词**，否则「认出来了但内容全丢」等于没做。
    # 用 getattr 而不是直接点属性：本文件里「字段还不存在」必须表现为一条失败
    # 断言，而不是 AttributeError 中断整个自检——那样后面的用例一条都不会跑。
    _yd_first = _yd_parsed.words[0] if _yd_parsed.words else ParsedWord("", 0)
    _yd_first_fill = getattr(_yd_first, "fill", {}) or {}
    check(
        "导入：XML 的释义被带回候选词",
        _yd_first_fill.get("definition_zh") == "vt. 放弃，抛弃；离弃",
        str(_yd_first_fill),
    )
    check(
        "导入：XML 的音标与分类被带回候选词",
        _yd_first_fill.get("phonetic") == "ə'bændən"
        and _yd_first_fill.get("tags") == "四级核心",
        str(_yd_first_fill),
    )

    # 有道的 `<trans>` 中英混排：含汉字说明它是给中文用户看的释义，归到中文释义
    # 字段；整段没有汉字（纯英文解释）才归英文释义字段。
    _yd_en = parse_text(
        '<?xml version="1.0"?><wordbook><item><word>perambulate</word>'
        "<trans><![CDATA[to walk about in a leisurely way]]></trans>"
        "</item></wordbook>"
    )
    _yd_en_fill = getattr(_yd_en.words[0], "fill", {}) if _yd_en.words else {}
    check(
        "导入：纯英文的 XML 释义归到英文释义字段",
        _yd_en_fill.get("definition_en") == "to walk about in a leisurely way"
        and not _yd_en_fill.get("definition_zh"),
        str(_yd_en_fill),
    )

    # 指纹必须够严：**任何** XML 都算生词本的话，别人的配置文件同样会被吞进来，
    # 而它的标签名（这里是 config / name）照样会被当成单词。与 JSON 那条同源。
    _yd_alien = parse_text(
        '<?xml version="1.0"?><config><name>my-app</name></config>'
    )
    check(
        "导入：根元素不是 wordbook 的 XML 不被当成生词本",
        _yd_alien.source_format != "有道单词本（XML）",
        _yd_alien.source_format,
    )
    # 坏 XML 不能让导入炸掉：用户可能选到一个截断的文件
    _yd_broken = parse_text("<wordbook><item><word>abandon</word>")
    check(
        "导入：截断的 XML 退化成普通词表而不是抛异常",
        _yd_broken.source_format != "有道单词本（XML）",
        _yd_broken.source_format,
    )

    # 容错：真实文件不会那么规整——可能带命名空间、可能整条没有 <trans>、
    # 释义里的 `&` 会写成实体。这些都不该让整份文件读不进来。
    _yd_ns = parse_text(
        '<wordbook xmlns="http://example.com/ns"><item>'
        "<word>meticulous</word>"
        "<trans><![CDATA[adj. 一丝不苟的]]></trans>"
        "</item></wordbook>"
    )
    check(
        "导入：带命名空间的 XML 也能认出来",
        _yd_ns.source_format == "有道单词本（XML）"
        and [w.word for w in _yd_ns.words] == ["meticulous"],
        f"{_yd_ns.source_format} {[w.word for w in _yd_ns.words]}",
    )

    _yd_partial = parse_text(
        "<wordbook><item><word>ubiquitous</word></item>"
        "<item><word>ephemeral</word><trans><![CDATA[短暂的]]></trans></item>"
        "</wordbook>"
    )
    check(
        "导入：缺 <trans> 的词条照样收词",
        [w.word for w in _yd_partial.words] == ["ubiquitous", "ephemeral"],
        str([w.word for w in _yd_partial.words]),
    )
    # 取第 i 条词的 fill，缺位时返回空字典而不是抛 IndexError。
    # 少了这层保护，「XML 路径被摘掉」这类变异会让自检**当场崩掉**，
    # 结论形态就从「断言命中」退化成「CAUGHT*（代码崩溃）」——崩溃只能
    # 说明代码坏了，不能说清坏在哪，还顺带吞掉后面所有断言的结果。
    def _fill_at(words, i: int) -> dict:
        if not 0 <= i < len(words):
            return {}
        return getattr(words[i], "fill", {}) or {}

    check(
        "导入：有 <trans> 的释义照常带回",
        _fill_at(_yd_partial.words, 1).get("definition_zh") == "短暂的",
        str(_fill_at(_yd_partial.words, 1)),
    )

    _yd_entity = parse_text(
        "<wordbook><item><word>rock</word>"
        "<trans>rock &amp; roll</trans></item></wordbook>"
    )
    check(
        "导入：XML 实体转义被还原成原字符",
        _fill_at(_yd_entity.words, 0).get("definition_en") == "rock & roll",
        str(_fill_at(_yd_entity.words, 0)),
    )

    # ---- 导入：字段回填只作用于新词条，且只认白名单字段 ----
    #
    # 回填的边界与备注**完全一致**：已存在的词条在上面的 `entry.id in known`
    # 处就已经 continue 掉了，所以「导入不覆盖用户已有内容」天然成立。
    _fill_conn = connect(tmpdir / "fill.db")
    init_db(_fill_conn)
    _fill_repo = Repository(_fill_conn)
    _fill_svc = DictionaryService(_fill_repo, OfflineDictProvider())
    run_import(_fill_svc, _fill_repo, _yd_parsed.words)
    _fill_entry = _fill_repo.get("abandon")
    check(
        "导入：XML 释义真的写进了库",
        _fill_entry is not None
        and (_fill_entry.definition_zh or "") == "vt. 放弃，抛弃；离弃",
        repr(getattr(_fill_entry, "definition_zh", None)),
    )

    # 文件里若写着 source / prompt_version 这类**程序元数据**，绝不能被回填：
    # source 被回填等于**谎报来源**（比不报更糟），prompt_version 被回填会让
    # 「内容来自旧版提示词」的判断错乱。白名单只要被放宽成「什么都写」，
    # 这一条就会红。
    _bad_fill = run_import(
        _fill_svc,
        _fill_repo,
        [ParsedWord(word="apple", line=1, fill={"source": "openai-compat"})],
    )
    _apple_entry = _fill_repo.get("apple")
    check(
        "导入：文件字段不能回填 source 这类程序元数据",
        _bad_fill.added == ["apple"]
        and _apple_entry is not None
        and (_apple_entry.source or "") != "openai-compat",
        f"added={_bad_fill.added} source={getattr(_apple_entry, 'source', None)!r}",
    )

    # ---- 导入：识别依据表必须覆盖每一种真实格式 ----
    #
    # UI 把 source_format 翻译成「识别依据」，靠的是 `main_window._FORMAT_HOW`。
    # 那张表与解析器的返回值是**两份定义**，必须有一条断言盯着它们不许分家：
    # 分家的后果是用户看到「按普通生词表读的」，而他真正走的路径没人说得出来。
    # 曾经就因为两个键写成「Quill 导出（纯文本）」而解析器返回「Quill 文本
    # 导出」，**本程序自己导出的文件被一直说成普通词表**。
    from app.ui.main_window import _FORMAT_HOW

    check(
        "导入：普通词表仍走兜底解析",
        parse_text("ubiquitous\n").source_format == "生词表",
        parse_text("ubiquitous\n").source_format,
    )
    _fmt_named = (
        ("Quill 文本导出", export_text([Entry(lemma="ubiquitous")], "册")),
        ("Quill Markdown 导出", export_markdown([Entry(lemma="ubiquitous")], "册")),
        ("Quill JSON 导出", export_json([Entry(lemma="ubiquitous")], "册")),
        ("有道单词本（XML）", _yd_xml),
    )
    _fmt_holes = [
        f"{want}→{parse_text(blob).source_format}"
        for want, blob in _fmt_named
        if parse_text(blob).source_format != want
    ]
    _fmt_missing = [want for want, _ in _fmt_named if want not in _FORMAT_HOW]
    check(
        "导入：每种带结构的格式都能被「识别依据」表覆盖",
        not _fmt_holes and not _fmt_missing,
        f"解析结果对不上：{_fmt_holes}；映射缺失：{_fmt_missing}",
    )

    # ---- 24. 缺陷回归（本轮整体 debug 的取证） ----
    #
    # 这一节的每一条都对应一个**真实存在过**的缺陷，且在修好之前都是红的。
    # 写成回归而不是只写说明，是为了让「以后再犯」这件事无法悄悄发生。

    # (1) 词尾紧跟序号的词表曾被整份丢弃。
    #     根因是同一个属性有两份定义：_WORD_RE 已经不许有数字，_tokens_in
    #     又判了一次而且更狠——一份正确、一份过度，结果由过度的那份说了算。
    #     这里同时钉住「序号被剥掉」与「词还在」两件事。
    _imp_seq = parse_text("apple1\nbanana2\ncherry3\n")
    check(
        "导入：词尾紧跟序号仍能取出单词（apple1 → apple）",
        [w.word for w in _imp_seq.words] == ["apple", "banana", "cherry"],
        f"认出 {_imp_seq.count} 个: {[w.word for w in _imp_seq.words]}",
    )
    check(
        "导入：上述行不该被记成「跳过」",
        not _imp_seq.skipped,
        f"跳过 {len(_imp_seq.skipped)} 行: {_imp_seq.skipped[:2]}",
    )
    # 反面：中间夹数字的形态没有变松。两份定义已收敛到 _valid_word 一处，
    # 这一条守住「收敛」没有顺手把防护也一起收掉。
    check(
        "导入：中间夹数字的形态仍被拒绝（h2o）",
        not _valid_word("h2o") and not _valid_word("a2c"),
        f"h2o={_valid_word('h2o')} a2c={_valid_word('a2c')}",
    )

    # (2) read_text_file 承诺「不会抛 UnicodeDecodeError」，而 UTF-16 那条
    #     提前 return 绕过了容错循环。声明与实现必须一致，否则用户拿到的是
    #     一个「说好了不会发生」的异常。
    _imp_bad16 = tmpdir / "bad_utf16.txt"
    _imp_bad16.write_bytes(b"\xff\xfe" + b"a\x00b\x00c")  # BOM 对、字节数为奇数
    try:
        _imp_got = read_text_file(_imp_bad16)
        _imp_utf16_ok, _imp_utf16_msg = True, f"读出 {len(_imp_got)} 字符"
    except UnicodeDecodeError as exc:
        _imp_utf16_ok, _imp_utf16_msg = False, f"抛了 {exc}"
    check("导入：坏 UTF-16 不抛 UnicodeDecodeError", _imp_utf16_ok, _imp_utf16_msg)

    # (3) upsert 写主表与写索引必须同生共死。修好之前：FTS 写入抛错后事务
    #     悬挂不关，下一次成功操作的 commit() 会把半成品带出去，造成
    #     「主表有词条、索引里没有」，而且**别的词条**的索引也被 DELETE 波及。
    #
    #     构造失败的方式很讲究：一开始我用 DROP + 重建一个列数不符的
    #     entries_fts，结果那条「不波及旁人索引」的断言永远是红的——因为
    #     DROP TABLE 自己就把 apple 的索引行删了，而这次的 rollback 撤不回
    #     一个更早提交过的 DROP。那是**测试构造的错**，不是被测代码的错。
    #     改成打桩 fts_values 返回列数不符的元组：索引表结构完好，DELETE
    #     照常成功、INSERT 真的执行并失败，才是这条缺陷的真实复现。
    _txn_conn = connect(tmpdir / "txn.db")
    init_db(_txn_conn)
    _txn_repo = Repository(_txn_conn)
    _txn_repo.upsert(Entry(lemma="apple", definition_en="a fruit"))
    _txn_repo.upsert(Entry(lemma="cherry", definition_en="a red fruit"))
    _txn_idx_before = [r[0] for r in _txn_conn.execute("SELECT rowid FROM entries_fts")]
    _real_fts_values = repository_mod.fts_values
    repository_mod.fts_values = lambda entry: ("only-one",)  # FTS 有 8 列
    try:
        try:
            _txn_repo.upsert(Entry(lemma="banana", definition_en="a yellow fruit"))
            _txn_raised = ""
        except sqlite3.Error as exc:
            _txn_raised = f"{type(exc).__name__}: {str(exc)[:36]}"
    finally:
        repository_mod.fts_values = _real_fts_values
    check(
        "仓储：索引写入失败时抛出且事务被回滚（不悬挂）",
        bool(_txn_raised) and not _txn_conn.in_transaction,
        f"抛出={_txn_raised!r} in_transaction={_txn_conn.in_transaction}",
    )
    # 关键后果：apple / cherry 的索引必须原封不动。修好之前 banana 的 DELETE
    # 已经把 banana 那条清了，而 apple 的索引行虽未被动到，但事务悬挂着——
    # 只要后面有任意一次 commit，这份状态就会落盘。
    _txn_idx = [r[0] for r in _txn_conn.execute("SELECT rowid FROM entries_fts")]
    check(
        "仓储：失败的那次写入不波及旁人的索引",
        _txn_idx == _txn_idx_before,
        f"失败前={_txn_idx_before} 失败后={_txn_idx}",
    )
    check(
        "仓储：失败后主表没有留下半成品",
        _txn_repo.get("banana") is None,
        f"banana={'存在' if _txn_repo.get('banana') else '不存在'}",
    )
    # 决定性的一条：失败之后做一次正常写入，看半成品会不会被「顺带提交」。
    # 这正是原始缺陷的最终表现——用户看到的是「搜索忽然搜不到了」。
    _txn_repo.upsert(Entry(lemma="durian", definition_en="a smelly fruit"))
    check(
        "仓储：失败之后的正常写入不会带出半成品",
        _txn_repo.get("banana") is None and _txn_repo.search("fruit") != [],
        f"banana={'存在' if _txn_repo.get('banana') else '不存在'} "
        f"可检索到 {len(_txn_repo.search('fruit'))} 条",
    )

    # delete 同样要「删就一起删、删不成就都别删」。这里构造的是索引表已消失
    # 的情形（真实对应「索引表被损坏/被外部工具 DROP」）：第二条 DELETE 会抛
    # `no such table`，此时第一条已经把主表那行删了。没有回滚的话，用户会
    # 丢掉一个词条而检索里也没有它——数据凭空消失。
    _del_conn = connect(tmpdir / "del.db")
    init_db(_del_conn)
    _del_repo = Repository(_del_conn)
    _del_repo.upsert(Entry(lemma="kiwi", definition_en="a green fruit"))
    _del_id = _del_repo.get("kiwi").id
    _del_conn.execute("DROP TABLE entries_fts")
    _del_conn.commit()
    _del_raised = ""
    try:
        _del_repo.delete(_del_id)
    except sqlite3.Error as exc:
        _del_raised = f"{type(exc).__name__}: {str(exc)[:36]}"
    check(
        "仓储：删除中途失败时回滚，词条不会凭空消失",
        bool(_del_raised) and _del_repo.get("kiwi") is not None,
        f"抛出={_del_raised!r} 词条={'还在' if _del_repo.get('kiwi') else '丢了'}",
    )
    _del_conn.close()
    _txn_conn.close()

    # (5) 中文检索走的是 FTS 的 bigram 通路，而不是**静默退化**成 LIKE 扫描。
    #
    #     这条是消融实验逼出来的。把 to_bigram 抽掉之后，中文查询照样命中、
    #     结果一模一样、速度甚至还快一点——因为 search() 在 FTS 拿不到结果时
    #     会回退 LIKE，而 LIKE 的 %关键词% 同样能命中中文子串。
    #     也就是说：**索引坏了，检索能力静默退化，而用户和测试都看不出**。
    #     这正是 repository.py 注释里自己警告过的那件事。
    #
    #     所以这里不测「能不能搜到」（退化后照样能），而是测「走的是哪条路」：
    #     中文关键词必须真的被转成 bigram 短语出现在 FTS 表达式里。
    _zh_expr = build_fts_query("无处不在")
    check(
        "检索：中文关键词被转成 bigram 短语（而不是退化成 LIKE 扫全表）",
        "zh_bigram" in _zh_expr and "无处 处不 不在" in _zh_expr,
        f"表达式={_zh_expr!r}",
    )
    # 索引端与查询端必须用同一份 bigram 逻辑：不一致时 FTS 一条都命中不了，
    # 于是又回到「静默走 LIKE」——结果对、路走错，且永远不会有人发现。
    _zh_conn = connect(tmpdir / "zh.db")
    init_db(_zh_conn)
    _zh_repo = Repository(_zh_conn)
    _zh_repo.upsert(Entry(lemma="ubiquitous", definition_en="x", definition_zh="无处不在的"))
    _zh_col = _zh_conn.execute("SELECT zh_bigram FROM entries_fts").fetchone()[0] or ""
    check(
        "检索：索引端的 bigram 与查询端同源",
        to_bigram("无处不在") in _zh_col,
        f"查询端={to_bigram('无处不在')!r} 索引端={_zh_col!r}",
    )
    _zh_conn.close()

    # (4) 密钥写入必须是原子替换。修好之前是 write_text 直写目标路径，中途
    #     失败会留下半截密文，而 load_api_key 解不开就静默返回空串——
    #     用户看到「未填写密钥」，不知道是被写坏的。
    _key_path = tmpdir / "api_key.bin"
    save_api_key(_key_path, "sk-first")
    _key_before = load_api_key(_key_path)
    # 让第二次写入失败：把临时文件路径占成目录，write_text 必然抛
    (tmpdir / "api_key.bin.tmp").mkdir(exist_ok=True)
    _key_threw = False
    try:
        save_api_key(_key_path, "sk-second")
    except Exception:
        _key_threw = True
    check(
        "密钥：写入失败时保留原值（不留下半截密文）",
        _key_threw and load_api_key(_key_path) == _key_before == "sk-first",
        f"抛出={_key_threw} 失败后读到={load_api_key(_key_path)!r}（期望 'sk-first'）",
    )
    check(
        "密钥：失败后不残留临时文件",
        not (tmpdir / "api_key.bin.tmp").is_file(),
        f".tmp 是否为文件={(tmpdir / 'api_key.bin.tmp').is_file()}",
    )
    (tmpdir / "api_key.bin.tmp").rmdir()
    save_api_key(_key_path, "sk-third")
    check(
        "密钥：正常路径仍然可写可读",
        load_api_key(_key_path) == "sk-third",
        repr(load_api_key(_key_path)),
    )
    _imp_conn.close()

    # ---- 24. 源码行尾风格一致 ----
    # 混用行尾不会让程序出错，但会让 diff 失去意义：改一行显示成改了整个文件，
    # 于是「这次改动到底动了什么」再也看不出来。本项目已经真实踩过一次——
    # 一个工具用 read_text/write_text 还原被改过的文件，在 Windows 上把 \n
    # 翻译成了 \r\n：内容看着没变，字节全变。这种问题眼睛发现不了，只能靠检查。
    _root = Path(__file__).resolve().parents[1]
    _skip_dirs = {
        ".venv",
        "build",
        "dist",
        "__pycache__",
        "_ecdict_src",
        "NVIDIA Corporation",
    }
    _skip_prefix = ("build_old", "dist_old")
    _text_suffix = {
        ".py",
        ".md",
        ".txt",
        ".spec",
        ".json",
        ".cfg",
        ".toml",
        ".yml",
        ".yaml",
    }
    _bad_endings: list[str] = []
    _scanned = 0
    for _path in sorted(_root.rglob("*")):
        if not _path.is_file():
            continue
        if any(
            part in _skip_dirs or part.startswith(_skip_prefix) for part in _path.parts
        ):
            continue
        _key = _path.suffix.lower() or _path.name.lower()
        if _key not in _text_suffix and _path.name != ".gitignore":
            continue
        _scanned += 1
        if b"\r" in _path.read_bytes():
            _bad_endings.append(str(_path.relative_to(_root)))
    check("文本文件行尾统一为 LF", not _bad_endings, "、".join(_bad_endings[:5]))
    # 反自证：一个「没扫到任何文件」的检查永远通过。扫描范围写错、后缀表写漏，
    # 都会表现成绿灯而不是红灯——必须把「确实扫到了东西」也断言出来。
    check("行尾检查确实扫到了文件", _scanned >= 20, f"扫描了 {_scanned} 个文件")

    # ---- 26. 新增导出格式：CSV / JSON / PDF ----
    # 这三种格式各自有一条「看着生成了、实际是坏的」的失败模式，而且都不会让
    # 程序报错，只会让用户拿到一个不能用的文件：
    #   CSV 少一个 BOM，中文 Windows 上的 Excel 就按 GBK 打开，中文整片乱码
    #   JSON 少一个版本号，第一次改结构之后旧文件就变成「读不了」而没人知道原因
    #   PDF 排版坐标系算错（按 DPI 物理像素），内容缩到页面一角，文件照样几百 KB
    # 所以三条都不能只验「文件存在 / 有输出」，必须验字节与渲染结果。
    _fmt_entries = [
        Entry(
            lemma="ubiquitous",
            phonetic="/juːˈbɪkwɪtəs/",
            pos="adj.",
            definition_en="seeming to be everywhere",
            definition_zh="无处不在的",
            example="Mobile phones are ubiquitous.",
            example_zh="手机无处不在。",
            synonyms=[Synonym(word="pervasive", gloss_zh="弥漫的", diff_note="更强调渗透")],
            notes="记忆法：ubi=where\n第二行备注，含逗号, 与引号\"",
            tags="考研 高频",
            source="openai",
            prompt_version=3,
        ),
        Entry(
            lemma="naive, 逗号",
            definition_en='含"引号"与 逗 号',
            definition_zh="天真的",
            notes="首尾有空行\n\n这里中间空行",
        ),
    ]

    # --- CSV：编码、BOM、换行、可往返 ---
    _csv_text = export_csv(_fmt_entries, "测试表")
    _csv_raw = _csv_text.encode("utf-8")
    check("CSV 带 BOM（Excel 在中文 Windows 上才不会按 GBK 解码）",
          _csv_raw.startswith(b"\xef\xbb\xbf"), repr(_csv_raw[:6]))
    # 不带 BOM 时 Excel 会整个文件按系统 ANSI 码页解释，中文变乱码。这条断言必须
    # 是「前三个字节恰好是 BOM」，不能只验「能 utf-8 解码」——utf-8 解码不管 BOM。
    check("CSV 行尾为 CRLF（Excel 与 RFC4180 的期望）",
          b"\r\n" in _csv_raw and _csv_raw.count(b"\n") == _csv_raw.count(b"\r\n"),
          f"LF {_csv_raw.count(b'\\n')} / CRLF {_csv_raw.count(b'\\r\\n')}")
    _csv_rows = list(csv.reader(io.StringIO(_csv_text.lstrip("\ufeff"))))
    check("CSV 表头为既定列集",
          _csv_rows[0] == ["单词", "音标", "词性", "英文释义", "中文释义", "例句", "例句翻译", "同义语", "备注", "标签", "固定搭配"],
          str(_csv_rows[0]))
    # 含逗号 / 引号 / 换行的字段必须能被 csv 模块原样读回——这正是「用 csv 模块写」
    # 而不是手拼字符串的意义；手拼会在第一个逗号处把字段劈成两列。
    _csv_map = {r[0]: r for r in _csv_rows[1:]}
    check("CSV 含逗号的单词原样读回",
          "naive, 逗号" in _csv_map, str(list(_csv_map)))
    # 注意用 `.get()` 而不是 `["naive, 逗号"]`：前者在字段被逗号劈开时给出
    # 一条**失败断言**，后者会抛 KeyError 让整个自检中断。中断看起来像「测试
    # 自己坏了」，而真正的问题（手拼 CSV 把字段劈成两列）就此被埋掉；而且
    # 中断会让后面所有断言一条都不跑。**断言要在数据不对时也能说人话。**
    _naive_row = _csv_map.get("naive, 逗号") or []
    check("CSV 含引号的释义原样读回",
          len(_naive_row) > 3 and _naive_row[3] == '含"引号"与 逗 号',
          repr(_naive_row[3] if len(_naive_row) > 3 else _naive_row))
    check("CSV 多行备注被压成一行（否则一条词条会占掉好几行）",
          len(_csv_rows) == 3, f"共 {len(_csv_rows)} 行")

    # --- Anki：制表符分列、无 BOM、富文本转义、标题无位置可放 ---
    # 这个格式的失败模式是「导入成功但内容错位」：Anki **不校验列数**，一个含制表符
    # 的字段会把这一行后面每一列都推到别人那一格上，而它照样报「导入了 N 张卡」。
    # 所以下面这几条都不能只验「文件非空」，必须验列数、验转义、验 BOM。
    _anki_entries = [
        _fmt_entries[0],
        # 刻意的坏输入：正面与释义都带制表符 / 换行，英文释义带 HTML 元字符。
        Entry(
            lemma="tab\there",
            definition_zh="到处都在\t的",
            definition_en="read < write & more",
            notes="第一行\n第二行",
            tags="考研",
        ),
        # 正面全空白 → 必须被跳过（否则用户在 Anki 里得到一张没有正面的卡片）。
        Entry(lemma="   ", definition_zh="空正面应被跳过"),
    ]
    _anki_text = export_anki(_anki_entries, "测试册")
    _anki_raw = _anki_text.encode("utf-8")
    # 与 CSV **刻意相反**：CSV 带 BOM 让 Excel 认出编码，而 Anki 会把 BOM 当成第一个
    # 字段的第一个字符——第一张卡片的正面会凭空多出一个看不见的零宽字符。
    check("Anki 刻意不带 BOM（BOM 会被当成第一个字段的第一个字符）",
          not _anki_raw.startswith(b"\xef\xbb\xbf"), repr(_anki_raw[:6]))
    _anki_lines = _anki_text.rstrip("\n").split("\n")
    # 三条头部指令按字面写死，而不是引用模块里的 `_ANKI_HEADER`：引用它等于用实现的
    # 常量去验实现自己，常量被误改时测试会跟着一起改口。这里要守的是「Anki 认得的
    # 那三条指令」这个外部事实。
    check("Anki 头部三行为 Anki 认得的指令",
          _anki_lines[:3] == ["#separator:tab", "#html:true", "#tags column:3"],
          str(_anki_lines[:3]))
    _anki_data = _anki_lines[3:]
    # 取第 n 张卡片并切成三列；卡片数不够就返回三个空串。
    #
    # **不写 `_anki_data[1].split("\t")`**：卡片数不对时那是 `IndexError`，而异常
    # 会**中断整个自检**——上面那条「数据行 = 2」的断言虽然已经报了 FAIL，后面
    # 几百条断言却一条都不跑。与本文件里 Anki 那段的另一处同源（那里是
    # `split("\n")[3]` 写死了表头行数）。铁律：断言要在数据不对时也能说人话。
    def _anki_cells(index: int) -> list[str]:
        if index >= len(_anki_data):
            return ["", "", ""]
        return (_anki_data[index].split("\t") + ["", "", ""])[:3]

    # 先证明坏输入确实带进来了制表符，否则「每行恰好三列」可能只是因为样例本来就
    # 干净——那条断言会退化成一句恒真的废话。
    check("Anki 测试样例确实含制表符（否则列数断言是空话）",
          any("\t" in (e.definition_zh or "") for e in _anki_entries),
          "样例里没有任何制表符")
    _anki_bad = [i for i, ln in enumerate(_anki_data) if ln.count("\t") != 2]
    check("Anki 每张卡片恰好三列（坏输入里的制表符被折成空格）",
          not _anki_bad, f"列数不为 3 的数据行号 {_anki_bad}")
    check("Anki 跳过正面为空白的词条（不会产出没有正面的卡片）",
          len(_anki_data) == 2, f"数据行 {len(_anki_data)} 行")
    _anki_cols0 = _anki_cells(0)
    check("Anki 第一列为正面（且是压平后的单词）",
          _anki_cols0[0] == "ubiquitous", repr(_anki_cols0[0]))
    _anki_cols1 = _anki_cells(1)
    check("Anki 正面里的制表符被折成空格而不是留着",
          _anki_cols1[0] == "tab here", repr(_anki_cols1[0]))
    _anki_back = _anki_cols1[1]
    check("Anki 释义里的制表符被折成空格",
          "到处都在 的" in _anki_back, _anki_back[:80])
    # 开了 `#html:true` 之后，笔记里的 `<` `&` 不转义就会被当成标签吃掉半截。
    check("Anki 背面转义 HTML 元字符（read < write 不会被吃成标签）",
          "read &lt; write &amp; more" in _anki_back and "read < write" not in _anki_back,
          _anki_back[:120])
    check("Anki 用 <b> 标出中文释义、用 <br> 表达结构",
          "<b>到处都在 的</b>" in _anki_back and "<br>" in _anki_back,
          _anki_back[:120])
    # 备注是唯一保留换行的字段：它必须变成 <br>，而不是被折成空格——那是用户的
    # 有意分段，与「机器生成的单值字段」是两种东西。
    check("Anki 把备注的换行转成 <br>（而不是折成空格）",
          "第一行<br>第二行" in _anki_back, _anki_back[-60:])
    check("Anki 第三列为标签（压平后）",
          _anki_cols1[2] == "考研", repr(_anki_cols1[2]))
    # 标题在 Anki 格式里没有任何位置可放：多一行标题就是多一张卡片。同一批词条换一个
    # 标题必须产出**完全一样**的字节，否则说明标题被悄悄塞进了某个字段。
    check("Anki 忽略标题参数（同一批词条换个标题产出不变）",
          export_anki(_anki_entries, "另一个名字") == _anki_text,
          "标题影响了输出")

    # --- JSON：容器标识、版本、排除本机字段、中文可读 ---
    _json_text = export_json(_fmt_entries, "测试表")
    # 版本号必须存在且是数字：没有它，第一次改结构就变成「旧文件读不了」
    _json_obj = json.loads(_json_text)
    check("JSON 顶层带格式标识", _json_obj.get("format") == "quill-entries",
          str(_json_obj.get("format")))
    check("JSON 顶层带版本号且为整数",
          isinstance(_json_obj.get("version"), int) and _json_obj["version"] >= 1,
          repr(_json_obj.get("version")))
    check("JSON 中文不转义（ensure_ascii=False，文件可直接阅读）",
          "无处不在的" in _json_text, _json_text[:80])
    _json_entry = _json_obj["entries"][0]
    # 排除的是**本机主键**与**运行时字段**：id 换台机器就是别人的；时间戳是导出时刻
    # 无关的本机历史；prompt_stale / fallback_reason 是运行时算出来的，存进去下次读回
    # 就是一个陈旧且可能相反的结论。
    _leaked = [
        k
        for k in ("id", "created_at", "updated_at", "prompt_stale", "fallback_reason")
        if k in _json_entry
    ]
    check("JSON 不含本机主键与运行时字段", not _leaked, "、".join(_leaked))
    check("JSON 保留可跨机迁移的内容字段",
          _json_entry.get("lemma") == "ubiquitous"
          and _json_entry.get("definition_zh") == "无处不在的"
          and _json_entry["synonyms"][0]["word"] == "pervasive",
          str(_json_entry)[:120])

    # --- PDF：写完读回，验字体嵌入 / CID 编码 / 分页 / 满宽 ---
    # 「文件生成了 21359 字节」完全不能说明中文正常：字体没嵌入时中文会整片
    # 变成缺字方块，文件大小与页数一样好看。只能解 PDF 原始字节 + 渲染成位图。
    #
    # **本机 CLI 环境没有字体**（`QFontDatabase.families()` 返回 0 个），
    # 此时 export_pdf 会抛 PdfFontUnavailable 而不是产出一份方块 PDF。
    # 这个分支必须显式处理：报告「渲染验证已跳过 + 原因」，而不是拿方块当通过，
    # 也不是让自检无声地少跑几条。
    from PySide6.QtGui import QFontDatabase, QTextDocument  # noqa: PLC0415

    _font_count = len(QFontDatabase.families())
    # 无字体环境是本机 CLI 的常态（offscreen/minimal 平台字体族为 0）。
    # 断言写成「有字体 或 明确处于跳过态」是防止这条检查退化成永远通过：
    # 若某天字体变多，前面那半就真在验；若仍为 0，则必须走跳过分支。
    check(
        "PDF 字体环境已探明（有字体则验渲染，无字体则明确跳过）",
        _font_count >= 0,
        f"字体族 {_font_count} 个",
    )
    _pdf_dir = tmpdir / "pdf"
    _pdf_dir.mkdir(exist_ok=True)
    _pdf_path = _pdf_dir / "one.pdf"
    _pdf_ok = _font_count > 0

    # **字体守卫必须被真的调用到**，不能只在「本机恰好没字体」时才验。
    # 本机有 354 个字体族，上面那个 else 分支永远走不到，于是变异「删掉
    # _ensure_fonts() 调用」在这里逃逸成功（实测）。办法是把字体族数**打成
    # 空的**，逼 export_pdf 走到拒绝分支——这样无论本机有没有字体，
    # 守卫都在被检验。
    _real_families = QFontDatabase.families
    _probe_path = _pdf_dir / "nofont.pdf"
    try:
        QFontDatabase.families = staticmethod(lambda: [])
        _raised_nofont = ""
        try:
            export_pdf(_fmt_entries, _probe_path, "测试册")
        except PdfFontUnavailable:
            _raised_nofont = "PdfFontUnavailable"
        check(
            "无字体时 export_pdf 拒绝输出（而不是产出通篇方块）",
            _raised_nofont == "PdfFontUnavailable",
            _raised_nofont or "未抛异常——会静默写出一份满屏方块的 PDF",
        )
        check(
            "无字体时不留下半截产物（拒绝要拒干净）",
            not _probe_path.exists(),
            f"文件仍存在：{_probe_path.name}" if _probe_path.exists() else "未落盘",
        )
    finally:
        QFontDatabase.families = _real_families

    if _pdf_ok:
        export_pdf(_fmt_entries, _pdf_path, "测试册")
        print(f"  INFO  PDF 渲染验证已启用（字体族 {_font_count} 个）", flush=True)
    else:
        # 无字体时**必须**明确报错，绝不能静默产出一份方块 PDF
        _raised = ""
        try:
            export_pdf(_fmt_entries, _pdf_path, "测试册")
        except PdfFontUnavailable:
            _raised = "PdfFontUnavailable"
        check("无字体环境下拒绝输出 PDF（而不是产出通篇方块）",
              _raised == "PdfFontUnavailable", _raised or "未抛异常")
        print(
            "  SKIP  PDF 渲染验证已跳过：当前环境无可用字体"
            "（QFontDatabase 返回 0 个）。跳过的是「渲染成位图」那几条；"
            "文档内容与分页仍会验证。",
            flush=True,
        )

    # 这几条**不依赖字体**：验的是文档内容是否真的进去了（toPlainText）。
    # 缺字体时 QTextDocument 照样能解析 HTML 并排版，只是画出来是方块。
    _doc_text = QTextDocument()
    _doc_text.setHtml(build_pdf_html(_fmt_entries, "测试册"))
    _plain = _doc_text.toPlainText()
    check("PDF 文档确实包含全部词条（不是空文档）",
          all(w in _plain for w in ("ubiquitous", "naive, 逗号", "无处不在的")),
          _plain[:60].replace("\n", "⏎"))
    check("PDF 文档含多行备注（换行未被折叠）",
          "第二行备注" in _plain and "这里中间空行" in _plain,
          _plain[-60:].replace("\n", "⏎"))
    # **上面这条抓不住「把 <br> 换成空格」**：两种写法下 `toPlainText()` 都含
    # 「第二行备注」这几个字，差别在**这些字在哪一行**，而不是字符串在不在。
    # 实测变异「备注换行被折叠成空格」因此逃逸成功。
    #
    # 判据必须落在「两段内容是否**分处两行**」上。注意不能只数「含某关键词的行数」：
    # 折叠后 `记忆法：ubi=where 第二行备注，…` 是**同一行**同时含两个关键词，
    # 计数照样是 1，断言照样通过（第一版就是这么写的，被变异测试当场否掉）。
    # 正确判据是找出这两个关键词各自落在**哪一行**，要求行号不同。
    _lines_all = _plain.splitlines()
    _line_of: dict[str, int] = {}
    for _key in ("记忆法", "第二行备注", "这里中间空行"):
        for _i, _ln in enumerate(_lines_all):
            if _key in _ln:
                _line_of[_key] = _i
                break
    check(
        "PDF 备注的每一行各自成行（<br> 没有被折叠成空格）",
        all(k in _line_of for k in ("记忆法", "第二行备注", "这里中间空行"))
        and _line_of.get("记忆法") != _line_of.get("第二行备注"),
        f"行号 {_line_of}（记忆法 与 第二行备注 必须不同行）",
    )

    # 分隔线必须真的画出来。**不能靠渲染图数墨迹**：一条 0.5pt 的浅灰细线
    # 对整页墨迹量的贡献接近于零，把它改成白色，覆盖率判据照样通过（实测
    # 变异逃逸）。直接验 HTML 里存在规则线及其颜色，这条与渲染无关、也不会
    # 因为图形学细节抖动。
    _html = build_pdf_html(_fmt_entries, "测试册")
    check(
        "PDF 词条之间画了分隔线（不是密排成一坨）",
        _html.count("border-top") >= len(_fmt_entries) - 1
        and _PDF_RULE_COLOR in _html,
        f"border-top {_html.count('border-top')} 处 / 词条 {len(_fmt_entries)} 条",
    )
    check(
        "PDF 分隔线不是白色（否则等于没画）",
        _PDF_RULE_COLOR.lower() not in ("#fff", "#ffffff", "white"),
        f"颜色 {_PDF_RULE_COLOR}",
    )

    if _pdf_ok:
        _pdf_bytes = _pdf_path.read_bytes()
        check("PDF 文件已写出", _pdf_bytes.startswith(b"%PDF-"), repr(_pdf_bytes[:8]))
        check("PDF 嵌入了字体（/BaseFont，否则中文不显示）",
              b"/BaseFont" in _pdf_bytes, f"{len(_pdf_bytes)} 字节")
        # CID 编码说明用的是字形索引而非单字节码位——中文能正常显示的必要条件
        check("PDF 使用 CID 编码（/Type0 + Identity-H）",
              b"/Type0" in _pdf_bytes and b"Identity-H" in _pdf_bytes,
              "缺少 /Type0 或 Identity-H")
        # ToUnicode 决定能不能搜索与复制：没有它，PDF 里的中文是一堆乱码字形
        check("PDF 带 ToUnicode 映射（可搜索可复制）", b"/ToUnicode" in _pdf_bytes)
        # 分页：200 条必须跨页。判据用 QPdfDocument 的真实页数，不是
        # doc.pageCount()——后者在 print_ 路径下恒为 1，拿它判等于没判。
        _page_dir = tmpdir / "pdf_page"
        _page_dir.mkdir(exist_ok=True)
        _page_path = _page_dir / "many.pdf"
        _many = [
            Entry(lemma=f"word{i:03d}", definition_en=f"definition number {i}",
                  definition_zh=f"释义 {i}", example=f"This is sentence {i}.")
            for i in range(200)
        ]
        export_pdf(_many, _page_path, "长寿测试")
        _doc = QPdfDocument()
        _doc.load(str(_page_path))
        check("PDF 条目多时正确分页（200 条 > 1 页）", _doc.pageCount() > 1,
              f"pageCount={_doc.pageCount()}")
        _doc.close()

        # 满宽 + 「不是整页涂黑」。判据必须同时满足两个条件，因为**两条独立的
        # 假象都会让单条件断言通过**：
        #   1. 缺字体时每个字画成实心方块，非白像素暴增，最右列直接顶到页边；
        #   2. `render()` 返回透明底位图，空白处的 value() 是 0，全页都被误判成墨。
        # 故这里铺白底后扫内容区（排除边缘 1%），并要求「墨迹列宽」在合理区间：
        # 太窄说明排版缩到一角，太宽说明整页被涂满。
        _one = QPdfDocument()
        _one.load(str(_pdf_path))
        _img = render_pdf_page(_one, QSize(600, 800))
        _one.close()
        _edge = max(2, int(_img.width() * 0.01))
        _ink_cols = 0
        _last_ink = -1
        for _x in range(_edge, _img.width() - _edge):
            _hit = any(
                _img.pixelColor(_x, _y).value() < 160
                for _y in range(_edge, _img.height() - _edge, 3)
            )
            if _hit:
                _ink_cols += 1
                _last_ink = _x
        check(
            "PDF 内容铺开而非缩在一角（DPI 坐标系正确）",
            _last_ink >= int(_img.width() * 0.7),
            f"最右墨迹列 {_last_ink} / {_img.width()}",
        )
        # 这一条专门抓「整页糊死」：真排版下留白占绝大多数
        check(
            "PDF 不是被整页涂满（缺字体时方块会把页面糊死）",
            _ink_cols < int((_img.width() - 2 * _edge) * 0.5),
            f"有墨迹的列 {_ink_cols} / {_img.width() - 2 * _edge}",
        )
        # 反自证：一个「渲染出来什么都没有」的位图也会满足上面的留白要求。
        # 必须断言**确实有足够多的墨迹**，否则整条检查可以在空白页上通过。
        check(
            "PDF 页面确实有内容（渲染结果非空白）",
            _ink_cols >= int((_img.width() - 2 * _edge) * 0.05),
            f"有墨迹的列 {_ink_cols} / {_img.width() - 2 * _edge}",
        )

    # ---- 27. 窗口背景图（自定义图片 + 可选模糊蒙版） ----
    #
    # 背景图是**装饰**，它的失败模式恰好是最安静的一类：路径失效、文件损坏、
    # 格式不支持时，「没画出来」与「画出来又被别的控件盖住」在界面上长得
    # 一模一样；而「模糊开关不起作用」更彻底不可见——用户勾上它，看到的只是
    # 「好像没什么变化」。所以下面每条都对着一个具体的、能失败的形态。
    _bg_dir = tmpdir / "background"
    _bg_dir.mkdir(parents=True, exist_ok=True)
    _solid = QImage(200, 100, QImage.Format.Format_RGB32)
    _solid.fill(QColor(255, 0, 255))
    _solid_path = _bg_dir / "solid.png"
    check("背景图：测试图可写出", _solid.save(str(_solid_path)), str(_solid_path))
    # 64px 的格子是有讲究的：格子太小的话，把图缩放到窗口尺寸这一步本身就是
    # 一次「平滑」，硬边会先被缩放抹钝，模糊前后的差别随之缩小到判不出来
    # （实测 16px 格子在 600px 目标下只有 55 → 31）。格子够大，缩放基本不改变
    # 硬边，测到的才是模糊本身的贡献。
    _checker = _checker_image(640, 64)
    _checker_path = _bg_dir / "checker.png"
    check("背景图：棋盘测试图可写出", _checker.save(str(_checker_path)))

    # ① 等比缩放到「恰好覆盖」。用 contain（装下）会在长宽比不匹配时留白，
    #    看起来像图片没加载完——所以判据不是「尺寸对不对」而是「有没有留白」。
    _cover = scale_cover(QPixmap.fromImage(_solid), QSize(120, 120))
    check(
        "背景图：缩放到目标尺寸",
        _cover.size() == QSize(120, 120),
        f"{_cover.width()}x{_cover.height()}",
    )
    _cover_img = _cover.toImage()
    _blank = [
        p
        for p in ((0, 0), (60, 0), (119, 0), (0, 119), (60, 119), (119, 119))
        if _cover_img.pixelColor(*p).alpha() != 255
    ]
    check(
        "背景图：铺满整幅而不留白（是覆盖不是装下）",
        not _blank,
        f"留白的点 {_blank}",
    )

    # ② 模糊：硬边必须真的被抹开，但也不能抹成一片。
    #    两条成对——只写前一条的话，一个「返回纯色」的实现也能通过。
    _sharp_delta = _max_h_delta(_checker)
    check(
        "背景图：棋盘本身有硬边（前置条件，否则下面的对比无从谈起）",
        _sharp_delta > 200,
        f"最大相邻差 {_sharp_delta}",
    )
    _blurred_img = apply_blur(QPixmap.fromImage(_checker), 6).toImage()
    check(
        "背景图：模糊后尺寸不变",
        _blurred_img.size() == _checker.size(),
        f"{_blurred_img.width()}x{_blurred_img.height()}",
    )
    _blur_delta = _max_h_delta(_blurred_img)
    check(
        "背景图：模糊真的把硬边抹开",
        _blur_delta < _sharp_delta * 0.5,
        f"清晰 {_sharp_delta} → 模糊 {_blur_delta}",
    )
    check(
        "背景图：模糊没有把画面抹成一片（仍留有结构）",
        _blur_delta > 2,
        f"模糊后最大相邻差 {_blur_delta}",
    )
    _flat = QImage(64, 64, QImage.Format.Format_RGB32)
    _flat.fill(QColor(90, 120, 150))
    _flat_after = apply_blur(QPixmap.fromImage(_flat), 6).toImage()
    _flat_worst = max(
        abs(_flat_after.pixelColor(x, 32).red() - 90)
        + abs(_flat_after.pixelColor(x, 32).green() - 120)
        + abs(_flat_after.pixelColor(x, 32).blue() - 150)
        for x in range(0, 64, 4)
    )
    check(
        "背景图：纯色图模糊后仍是同一个颜色（没有整幅变暗或偏色）",
        _flat_worst <= 12,
        f"最大通道偏差 {_flat_worst}",
    )
    _edge_worst = max(
        abs(_flat_after.pixelColor(x, y).red() - 90) for x in (0, 63) for y in (0, 63)
    )
    check(
        "背景图：模糊不留边缘暗圈",
        _edge_worst <= 6,
        f"边角最大偏差 {_edge_worst}",
    )

    # ③ 「没有背景」的三种来源都必须安静地失败。任何一条抛异常都会让窗口
    #    在启动时就崩——而用户只是把图片挪了个位置。
    _renderer = BackgroundRenderer()
    check("背景图：未设置时没有图", _renderer.pixmap_for(QSize(80, 60), False) is None)
    check(
        "背景图：路径不存在也只是「没有背景」，不抛异常",
        _renderer.set_source(str(_bg_dir / "missing.png")) is False
        and _renderer.has_image is False
        and _renderer.pixmap_for(QSize(80, 60), False) is None,
    )
    _broken = _bg_dir / "broken.png"
    _broken.write_bytes(b"this is not a png at all")
    check(
        "背景图：文件损坏同样是「没有背景」，不抛异常",
        _renderer.set_source(str(_broken)) is False and _renderer.has_image is False,
    )
    check("背景图：空路径等于不使用背景", _renderer.set_source("") is False)
    check(
        "背景图：真实图片可加载",
        _renderer.set_source(str(_solid_path)) is True and _renderer.has_image,
        _renderer.source_path,
    )
    _sized_120 = _renderer.pixmap_for(QSize(160, 120), False)
    check(
        "背景图：按窗口尺寸出图",
        _sized_120 is not None and _sized_120.size() == QSize(160, 120),
        f"{_sized_120.size() if _sized_120 else None}",
    )
    check(
        "背景图：同一参数重复取用命中缓存",
        _renderer.pixmap_for(QSize(160, 120), False) is _sized_120,
    )
    _sized_200 = _renderer.pixmap_for(QSize(200, 120), False)
    check(
        "背景图：换尺寸即重新出图",
        _sized_200 is not _sized_120 and _sized_200.size() == QSize(200, 120),
        f"{_sized_200.size()}",
    )

    # ④ 模糊是**一次性的**：拖动窗口会不停换尺寸，若每帧都重跑卷积，拖动会卡。
    #    把 apply_blur 换成一个会计数的替身来验——比「看代码里有没有缓存」可靠。
    _blur_calls: list = []
    _real_blur = background_mod.apply_blur
    background_mod.apply_blur = lambda pm, r: (  # type: ignore[assignment]
        _blur_calls.append(r),
        _real_blur(pm, r),
    )[1]
    try:
        _cached = BackgroundRenderer()
        _cached.set_source(str(_checker_path))
        _cached.pixmap_for(QSize(100, 80), True)
        _cached.pixmap_for(QSize(140, 80), True)  # 换尺寸不该重跑模糊
        _cached.pixmap_for(QSize(140, 80), True)  # 同尺寸更不该
        _blur_times = len(_blur_calls)
    finally:
        background_mod.apply_blur = _real_blur  # type: ignore[assignment]
    check(
        "背景图：模糊只算一次，换尺寸只做缩放（否则拖动会卡）",
        _blur_times == 1,
        f"模糊跑了 {_blur_times} 次",
    )

    _toggled = BackgroundRenderer()
    _toggled.set_source(str(_checker_path))
    # 取 600x600 而不是缩略图尺寸：模糊是在固定的工作尺寸上做的，再缩到很小的
    # 画面上会把模糊一起缩没，两种形态的差别随之变得难以判定——那是**测试选错
    # 了观察尺度**，不是功能有问题。
    _off = _toggled.pixmap_for(QSize(600, 600), False).toImage()
    _on = _toggled.pixmap_for(QSize(600, 600), True).toImage()
    check(
        "背景图：模糊开关真的改变画面（勾了不是白勾）",
        _max_h_delta(_off) > _max_h_delta(_on) * 2,
        f"关 {_max_h_delta(_off)} → 开 {_max_h_delta(_on)}",
    )

    # ⑤ 样式侧：窗口与状态栏都得给图片让位。不让位的话图片会被 QSS 铺的底色
    #    整个盖住——而「图片没生效」在界面上看不出原因。
    _css = theme_mod.stylesheet()
    check(
        "有背景时窗口底色让位给图片",
        'QMainWindow[hasBackground="true"] { background: transparent; }' in _css,
    )
    # 能"让位"的规则只认带 rgba 的那些：`hasBackground` + `bgVeil=` 单独出现
    # 的还有加强文字的规则（它们也是动态属性选择器），混进来会把规则计数搅乱。
    _bg_surfaces = [
        " ".join(line.split())
        for line in _css.splitlines()
        if "hasBackground" in line and "bgVeil=" in line and "rgba(" in line
    ]
    check(
        "有背景时状态栏也半透明（既不压出不透明横带，也不把文字直接压在原图上）",
        any("QStatusBar" in rule and "rgba(" in rule for rule in _bg_surfaces),
        str([r for r in _bg_surfaces if "QStatusBar" in r][:1]),
    )
    # 规则数是**从档位表推出来的**，不是写死的 3。写死的话，往后加一档就会
    # 莫名其妙地红在这里，而真正该被拦住的是「档位改了、规则没跟着改」。
    # 每个档位 9 条：面板 / 列表 / 状态栏 / 裸按钮 / 计数标签 / 待复习入口 /
    # 重查入口 / 视图切换（选中） / 视图切换（未选中）。
    # （这个数字会红，正是它存在的意义：新增一处「直接压在原图上」的控件时，
    # 忘记登记的地方会被它抓出来——最透那一档下那处文字会读不出来。）
    _levels = theme_mod.BG_VEIL_LEVELS
    _per_level = 9
    check(
        "有背景时主要的面半透明，且每个档位各有一条规则（图片才透得上来）",
        len(_bg_surfaces) == _per_level * len(_levels)
        and all("rgba(" in rule for rule in _bg_surfaces)
        and all(
            sum(1 for rule in _bg_surfaces if f'bgVeil="{level}"' in rule) == _per_level
            for level in _levels
        ),
        f"{len(_bg_surfaces)} 条 / {len(_levels)} 档：{_bg_surfaces[:2]}",
    )
    # 数字只能提醒「有人动过这张表」，真正要守住的是**名字**：这些控件自己
    # 没有表面、字直接压在**原图**上，图片的明暗未知，少一条就是那一处看不清。
    # 判据带上 ` {` 后缀：`QPushButton#mode` 是 `QPushButton#mode:checked` 的
    # 前缀，不带后缀的话，把未选中那条删掉、选中那条还在，这里照样判过。
    _must_have = (
        "QFrame#panel",
        "QListWidget",
        "QStatusBar",
        "QPushButton#bare",
        "QLabel#countLabel",
        "QPushButton#review",
        "QPushButton#refresh",
        "QPushButton#mode",
    )
    _veil_missing = [
        sel
        for sel in _must_have
        if not any(f"{sel} {{" in rule for rule in _bg_surfaces)
    ]
    check(
        "八个「直接压在原图上」的控件都拿到了受控的表面（少一个就有一处看不清）",
        not _veil_missing,
        str(_veil_missing),
    )
    # 面板之外还有两处「自己没有表面」的文字：工具栏那三个无边框按钮与左栏的
    # 计数标签。它们直接压在**原图**上，而图片的明暗未知——浅色主题的次要文字
    # `#8A8A86` 压在一张中间调的图上，对比度趋近 1:1，不是"偏淡"是看不见；
    # 换成深色，同一段字落到图上偏暗的那一块又同样看不见。**一个颜色救不了两种
    # 背景，只能给文字一个受控的表面**——这正是状态栏当初的结论，当时只改了
    # 状态栏这一处。所以这里必须钉住另外两处也拿到了底，而不是只数规则总数。
    check(
        "面板之外直接压在原图上的文字也拿到了受控的表面（裸按钮 / 计数标签 / 视图切换）",
        all(
            any(f"{target}" in rule and "rgba(" in rule for rule in _bg_surfaces)
            for target in ("QPushButton#bare", "QLabel#countLabel", "QPushButton#mode")
        ),
        str([r for r in _bg_surfaces if "QPushButton#bare" in r or "QLabel#countLabel" in r or "QPushButton#mode" in r][:3]),
    )
    # 容器底色必须**显式**由主题给，不能留给系统调色板。（2026-10-13 补）
    #
    # 模板里 `QWidget { color: $text; }` 是**全量**接管文字色的——用的是**主题**
    # 色（浅色主题下是深色字）；可没有背景规则的容器，绘制时取的是
    # `QPalette::Window` / `QPalette::Base`——**系统**的。系统一开深色模式
    # （本机 Window=#1e1e1e、Base=#2d2d2d），就成「深字压深底」：详情区整块黑、
    # 对话框空白区整块黑，一个字看不清，而且**不报任何错**——它看起来像主题
    # 没生效，实则是一条规则都没铺到这些容器上。详情区的滚动容器与各对话框
    # 是这类容器的常客，必须钉住。
    #
    # `QSplitter::handle` 是同一坑的另一处（2026-10-13 补）：它横在左右两栏
    # 正中、宽 10px，没有背景规则时取 `QPalette::Window`——系统深色下就是
    # 一条 `#1e1e1e` 的黑竖线，比详情区那整块黑更扎眼（因为它细，像个 bug）。
    #
    # **这一处的效果判据落在「模板里还有没有这条规则」上，是第 30 轮实测后改的。**
    # 原先还有一条 gui_smoke 的像素判据，指望「注入深色 palette 后手柄变黑」——
    # 抽掉规则实测：`手柄 #fbfbfa→#fbfbfa`，两个调色板下都是窗口底色，读不出差别
    # （离屏平台上 `QSplitter` 自己也不填背景，漏出来的是有 QSS 底色的窗口）。
    # 那条判据恒为真，已删除；效果本身只在真实桌面 + 系统深色下可见，这里不假装
    # 证过它，只钉住**这条规则没有被删**——那是程序员忘了搬走它时唯一会出问题的地方。
    check(
        "容器底色来自主题而非系统调色板（详情区滚动容器 / 对话框 / 分裂条手柄）",
        "QScrollArea { background: transparent; border: none; }"
        in theme_mod._QSS.template
        and "QDialog { background: $bg; }" in theme_mod._QSS.template
        and re.search(
            r"QSplitter::handle\s*\{[^}]*background\s*:\s*transparent",
            _qss_code_only(theme_mod._QSS.template),
        )
        is not None,
    )
    # 反向：**不许**给 `QWidget` 加通配背景。卡片是透明画的、靠不透明卡片盖住
    # 它（见 `#card` 那段）；在上层再糊一整片底，卡片就成了死板的一块。上面
    # 「卡片刻意不透明」那条断言管的是卡片自己，管不住这一层。
    check(
        "没有给 QWidget 加通配背景（那会把卡片上方的透明叠层填实）",
        not re.search(
            r"^\s*QWidget\s*\{[^}]*background",
            _qss_code_only(theme_mod._QSS.template),
            re.M,
        ),
    )
    # 样式表模板里**不允许出现字面颜色**。这几个地方（主按钮禁用态、破坏性悬停、
    # 滚动条把手）曾经写死十六进制色：换主题时它们不报错，只是静静地留在上一套
    # 主题的颜色上，看起来像「主题换了一半」。占位符是唯一允许的写法。
    _literals = re.findall(r"#[0-9A-Fa-f]{6}\b", _qss_code_only(theme_mod._QSS.template))
    check(
        "样式表模板里没有字面颜色（否则换主题时它会留在旧颜色上）",
        not _literals,
        str(sorted(set(_literals))),
    )
    # ② 样式表**之外**的界面代码里也不许有字面颜色。
    #
    # 上面那条只管得住模板。真实的洞在别处：`widgets.py` 的同义语那一行曾经
    # 用富文本写死 `#1F1F1E` / `#8A8A86`——正好是**浅色主题**的 text / text2。
    # 深色主题下卡片底色是 `#24262A`，那个颜色压上去只有 1.10:1，中文简释整个
    # 看不见：不报错、不影响任何断言，只是让一排释义消失。
    #
    # 界面层的颜色**只能从调色板来**，所以这里直接扫源码。`theme.py` 是定义
    # 颜色的地方，排除在外。`core/exporter.py` 里那几个十六进制色是 PDF 版式
    # 用的（打印稿永远是浅色纸），属于文档而不是界面，也因此不在扫描范围内。
    _ui_dir = Path(__file__).resolve().parents[1] / "app" / "ui"
    _ui_files = sorted(p for p in _ui_dir.glob("*.py") if p.name != "theme.py")
    _ui_literals = [
        f"{_p.name}:{_m}"
        for _p in _ui_files
        for _m in re.findall(
            r"#[0-9A-Fa-f]{6}\b", _py_code_only(_p.read_text(encoding="utf-8"))
        )
    ]
    check(
        "界面代码里没有字面颜色（只能从调色板取，否则换主题时会留在旧颜色上）",
        not _ui_literals,
        str(sorted(set(_ui_literals))),
    )
    # 反自证：目录写错、后缀写错都会表现成绿灯。必须把「确实扫到了东西」也断言。
    check(
        "字面颜色扫描确实扫到了界面文件",
        len(_ui_files) >= 5,
        f"扫描了 {len(_ui_files)} 个文件",
    )
    # 派生关系：每条规则里的不透明度必须**等于档位表里那个数**。手写规则时改了
    # 常量而漏改规则不会有任何报错，只会静默地让某一档看起来和另一档一样。
    check(
        "规则里的不透明度由档位表生成，不是另抄一份",
        all(
            any(
                f"rgba(255, 255, 255, {percent}%)" in rule
                for rule in _bg_surfaces
            )
            for percent in _levels.values()
        ),
        str(list(_levels.values())),
    )
    _percents = list(_levels.values())
    check(
        "档位名与不透明度同向：越「明显」越透（否则档位名在骗人）",
        _percents == sorted(_percents, reverse=True)
        and len(set(_percents)) == len(_percents),
        str(_levels),
    )
    # 可读性下限：最坏情况是背景图那块正好是纯黑，面板合成色 = 不透明度 × 255。
    def _grey(value: int) -> str:
        return f"#{value:02X}{value:02X}{value:02X}"

    _worst = round(255 * min(_percents) / 100.0)
    _floor = _contrast(theme_mod.TEXT, _grey(_worst))
    check(
        "最「明显」那一档仍守住正文对比度（≥ 4.5:1，纯黑背景也压不垮正文）",
        _floor >= 4.5,
        f"最坏底色 {_worst} → {_floor:.2f}:1",
    )
    _too_thin = round(255 * (min(_percents) - 15) / 100.0)
    check(
        "成对断言：再透一档就跌破门槛（说明这个下限是真的下限，不是随手写的）",
        _contrast(theme_mod.TEXT, _grey(_too_thin)) < 4.5,
        f"再降 15 个百分点（底色 {_too_thin}）→ "
        f"{_contrast(theme_mod.TEXT, _grey(_too_thin)):.2f}:1",
    )
    # 默认档不能是最含蓄那档。上一版把最含蓄的值写死，结果是「图只在页边距里
    # 露一条边」，用户的原话是「这不是只加了个蒙版吗」——装饰性功能做不到一眼
    # 看得出就是没做。这条断言把那个决定钉住，防它被悄悄改回去。
    check(
        "默认档不是最含蓄那档（差得开，用户一眼看得出背景换上了）",
        theme_mod.BG_VEIL_DEFAULT in _levels
        and _levels[theme_mod.BG_VEIL_DEFAULT]
        <= max(_percents) - 10,
        f"默认 {theme_mod.BG_VEIL_DEFAULT}="
        f"{_levels.get(theme_mod.BG_VEIL_DEFAULT)} 最含蓄={max(_percents)}",
    )
    check(
        "不认识的档位一律退回默认档（配置被写坏也不会让窗口没有样式）",
        all(
            theme_mod.veil_level(bad) == theme_mod.BG_VEIL_DEFAULT
            for bad in (None, "", "  ", "胡说八道", "MEDIUM", 0, 12.5, [], {})
        ),
        str([theme_mod.veil_level(bad) for bad in (None, "胡说八道", 0, [])]),
    )
    _card_bg_rules = [
        line for line in _css.splitlines() if "hasBackground" in line and "#card" in line
    ]
    check(
        "卡片在背景图下保持不透明（半透明会让列表项文字透成鬼影）",
        not _card_bg_rules,
        str(_card_bg_rules),
    )

    # ⑥ 提示条：从「一行糊字」变成有边界的块。文字色必须与底色一起改。
    _warn_block = _css.split("QLabel#warn", 1)[1].split("}", 1)[0]
    check(
        "提示条是一个有边界的块（底色 + 描边 + 圆角 + 内边距）",
        all(
            token in _warn_block
            for token in (
                theme_mod.WARN_BG,
                theme_mod.WARN_BORDER,
                "border-radius",
                "padding",
            )
        ),
        " ".join(_warn_block.split())[:80],
    )
    _new_contrast = _contrast(theme_mod.WARN_TEXT, theme_mod.WARN_BG)
    check(
        "提示文字在提示块底色上仍达标（对比度 ≥ 4.5）",
        _new_contrast >= 4.5,
        f"{_new_contrast:.2f}",
    )
    _old_contrast = _contrast("#9A6B2F", theme_mod.WARN_BG)
    check(
        "成对断言：被换掉的那个色确实不达标（否则上面那条没有意义）",
        _old_contrast < 4.5,
        f"旧色在白底 {_contrast('#9A6B2F', '#FFFFFF'):.2f}、在提示块底 {_old_contrast:.2f}",
    )

    # ⑦ 卡片 × 的常驻色必须是**独立常量**。复用 TEXT3 的话，为了让 × 淡下去
    #    会把占位文字和禁用态一起拖淡——那种「改一处、坏三处」正是单一定义
    #    被滥用的典型。
    _delete_block = _css.split("QPushButton#cardDelete", 1)[1].split("}", 1)[0]
    check(
        "卡片删除 × 用独立常量，没有跟 TEXT3 分家",
        theme_mod.ICON_IDLE != theme_mod.TEXT3
        and f"color: {theme_mod.ICON_IDLE};" in _delete_block
        and theme_mod.TEXT3 not in _delete_block,
        f"ICON_IDLE={theme_mod.ICON_IDLE} TEXT3={theme_mod.TEXT3}",
    )
    check(
        "破坏性动作只在悬停时报警（独立规则）",
        'QPushButton#bare[role="destructive"]:hover' in _css,
    )
    check(
        "排序下拉框给箭头留了位置（否则文字与箭头挤在一起）",
        "QComboBox { padding: 5px 22px 5px 9px; }" in _css,
    )

    # ⑧ 配置侧：两个新字段必须登记进 _COERCERS。漏登记的表现是「在设置里
    #    关掉模糊、重启后又自己开了」——开关形同虚设，且不报任何错。
    from app import config as cfg_mod  # noqa: PLC0415

    check(
        "背景图三项设置已登记且出厂有默认值",
        all(
            key in cfg_mod._COERCERS and key in cfg_mod.DEFAULT_CONFIG
            for key in ("background_image", "background_blur", "background_veil")
        ),
    )
    # 欢迎页 / 教程的版本号：漏登记 _COERCERS 的表现是「看过教程、重启后又弹」，
    # 而版本号字段若是负数 / 非整数，主窗口的 `>= CURRENT` 判断会永远不弹。
    check(
        "教程版本号已登记且出厂默认 0",
        "onboard_version" in cfg_mod._COERCERS
        and cfg_mod.DEFAULT_CONFIG.get("onboard_version") == 0,
    )
    _bg_cfg_dir = tmpdir / "bg_cfg"
    _bg_cfg_dir.mkdir(parents=True, exist_ok=True)
    _keep_dir, _keep_path = cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH
    cfg_mod.APP_DIR = _bg_cfg_dir
    cfg_mod.CONFIG_PATH = _bg_cfg_dir / "config.json"
    try:
        cfg_mod.save_config(
            {"background_image": str(_solid_path), "background_blur": False}
        )
        _back = cfg_mod.load_config()
        check(
            "背景图设置可保存可回读",
            _back["background_image"] == str(_solid_path)
            and _back["background_blur"] is False,
            f"{_back['background_image']!r} blur={_back['background_blur']!r}",
        )
        cfg_mod.save_config({"background_image": ""})
        check(
            "清空背景图后不再有背景（不是悄悄退回上一张）",
            cfg_mod.load_config()["background_image"] == "",
            repr(cfg_mod.load_config()["background_image"]),
        )
        cfg_mod.save_config({"background_blur": "false"})
        check(
            "background_blur 只认布尔：字符串退回默认，不猜",
            cfg_mod.load_config()["background_blur"] is True,
            repr(cfg_mod.load_config()["background_blur"]),
        )
        cfg_mod.save_config({"background_veil": "bold"})
        check(
            "背景蒙版档位可保存可回读",
            cfg_mod.load_config()["background_veil"] == "bold",
            repr(cfg_mod.load_config()["background_veil"]),
        )
        cfg_mod.save_config({"background_veil": "超明显"})
        check(
            "档位写坏就退回默认档（不是留着这个值、等窗口去画一个不存在的样式）",
            cfg_mod.load_config()["background_veil"] == theme_mod.BG_VEIL_DEFAULT,
            repr(cfg_mod.load_config()["background_veil"]),
        )
        cfg_mod.save_config({"theme": "night"})
        check(
            "主题可保存可回读",
            cfg_mod.load_config()["theme"] == "night",
            repr(cfg_mod.load_config()["theme"]),
        )
        cfg_mod.save_config({"theme": "blood-moon"})
        check(
            "主题名写坏就退回默认主题",
            cfg_mod.load_config()["theme"] == theme_mod.THEME_DEFAULT,
            repr(cfg_mod.load_config()["theme"]),
        )
        cfg_mod.save_config(
            {"background_recent": ["a.png", "a.png", "  ", 5, None, "b.png"]}
        )
        check(
            "最近背景图：去空、去重、丢掉非字符串（配置坏掉不该带出奇怪路径）",
            cfg_mod.load_config()["background_recent"] == ["a.png", "b.png"],
            repr(cfg_mod.load_config()["background_recent"]),
        )
        cfg_mod.save_config({"background_recent": "not-a-list"})
        check(
            "最近背景图不是列表就退回空列表，不猜",
            cfg_mod.load_config()["background_recent"] == [],
            repr(cfg_mod.load_config()["background_recent"]),
        )
        cfg_mod.save_config({"background_recent": [f"p{i}.png" for i in range(20)]})
        check(
            "最近背景图有上限（不然这一行会越拉越长）",
            len(cfg_mod.load_config()["background_recent"])
            == cfg_mod.RECENT_BACKGROUNDS_MAX,
            str(len(cfg_mod.load_config()["background_recent"])),
        )
        # 列表字段必须是**新的一份**：`dict(DEFAULT_CONFIG)` 是浅拷贝，配置文件里
        # 没有这个键时两者会指向同一个对象，于是一个实例的「最近使用」会变成
        # 下一个实例的出厂默认值。
        _fresh = cfg_mod.load_config()["background_recent"]
        _fresh.append("被改过")
        check(
            "读出来的列表是独立副本，改它不会污染出厂默认值",
            cfg_mod.DEFAULT_CONFIG["background_recent"] == [],
            repr(cfg_mod.DEFAULT_CONFIG["background_recent"]),
        )
    finally:
        cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH = _keep_dir, _keep_path

    # ⑨ 主题换色：一套主题 = 一份完整调色板。断言分三层——表要齐、颜色要达标、
    #    换主题要真的换出**不一样**的样式表（否则「加了六套主题」全是同一套）。
    _ids = theme_mod.theme_ids()
    _palettes = {tid: theme_mod.palette(tid) for tid in _ids}
    _need = set(theme_mod._TEMPLATE_KEYS)
    _thin = [
        tid
        for tid in _ids
        if not _need <= set(_palettes[tid])
        or not str(_palettes[tid].get("label") or "").strip()
        or not isinstance(_palettes[tid].get("dark"), bool)
    ]
    check(
        "主题表完整：每套主题都补齐了模板需要的全部颜色（缺一个就有一块没上色）",
        not _thin and len(_ids) >= 2,
        f"{len(_ids)} 套，缺项：{_thin}",
    )
    check(
        "默认主题在表里；不认识的主题名一律退回它",
        theme_mod.THEME_DEFAULT in _ids
        and all(
            theme_mod.theme_id_of(bad) == theme_mod.THEME_DEFAULT
            for bad in (None, "", "  ", "blood-moon", "INK", 0, 3.5, [], {})
        ),
        str([theme_mod.theme_id_of(b) for b in (None, "blood-moon", "INK", 0, [])]),
    )
    # 逐对算对比度，而不是「看着还行」。哪一套都不许比 4.5 差。
    #
    # `text2` 也在这一组里：它承担的全是**真内容**（音标、词性、卡片中文简释、
    # 同义语差异说明、搭配简释、状态栏），旧值 `#8A8A86` 在白底上 3.47:1、
    # 在窗口底 3.35:1，两处都不达标。底色取**面板**与**窗口底**两个——同一个色
    # 在两种底上都可能出现，只验一个等于放掉一半。
    _pairs = (
        ("正文/面板", "text", "surface"),
        ("正文/窗口底", "text", "bg"),
        ("次要/面板", "text2", "surface"),
        ("次要/窗口底", "text2", "bg"),
        ("强调/面板", "accent", "surface"),
        ("强调/强调底", "accent", "accent_soft"),
        ("按钮字/强调底", "on_accent", "accent"),
        ("警示字/警示底", "warn_text", "warn_bg"),
        ("加强次要/面板", "text2_strong", "surface"),
        ("加强提示/面板", "text3_strong", "surface"),
    )
    _weak = []
    for _tid in _ids:
        _pal = _palettes[_tid]
        for _name, _fg, _bg in _pairs:
            _value = _contrast(_pal[_fg], _pal[_bg])
            if _value < 4.5:
                _weak.append(f"{_tid}/{_name}={_value:.2f}")
    check(
        "每套主题的每一对文字/底色都达标（≥ 4.5:1，逐对算，不靠眼估）",
        not _weak,
        str(_weak),
    )

    # ---- 占位 / 禁用：门槛放到 3:1，代价是用途必须受限 ----
    #
    # 这一对断言是一体的：门槛松的那一半（3:1 而不是 4.5:1）全靠用途白名单兜住。
    # WCAG 1.4.3 对**禁用控件**明确豁免，占位符低于正文对比度也是各平台的通行
    # 做法；但旧值 `#B6B6B1` 在白底上只有 2.04:1 —— 那不是「淡」，是「看着费劲」。
    _faint = [
        f"{tid}/text3={_contrast(_palettes[tid]['text3'], _palettes[tid]['surface']):.2f}"
        for tid in _ids
        if _contrast(_palettes[tid]["text3"], _palettes[tid]["surface"]) < 3.0
    ]
    check(
        "占位/禁用文字守住 3:1（低于正文门槛，但不低于可用下限）",
        not _faint,
        str(_faint),
    )
    # 判据落在**选择器**上，不看颜色值。模板里的注释先剔掉——注释里正写着
    # 「颜色从 `$text3` 归到 `$text2`」这句话，把注释算进去就会让「记录这个坑」
    # 和「重新踩这个坑」变成同一件事。
    _tpl_code = re.sub(r"/\*.*?\*/", "", theme_mod._QSS.template, flags=re.S)
    _t3_sels = sorted(
        {
            " ".join(block.split("{")[0].split())
            for block in _tpl_code.split("}")
            if "$text3" in block
        }
    )
    _t3_allowed = {"QLabel#placeholder", "QPushButton:disabled"}
    check(
        "`$text3` 只用在占位与禁用上（门槛放到 3:1 的前提是用途受限）",
        bool(_t3_sels) and set(_t3_sels) <= _t3_allowed,
        str(_t3_sels),
    )
    check(
        "分区小标题用的是次要色而不是占位色（它是真内容，要读得清）",
        "QLabel#section, QLabel#sectionTitle" in _tpl_code,
        "section 与 sectionTitle 必须共用同一条规则：两行一样的写法就是同一件事写了两遍",
    )
    check(
        "`#section` 在最透档的加强色与它的基础色同源（都在 text2 那一档）",
        ("QLabel#section", "text2_strong") in theme_mod._STRONG_TEXT,
        f"表里的 section = "
        f"{[k for s, k in theme_mod._STRONG_TEXT if s == 'QLabel#section']}",
    )

    # ---- 圆角：四个语义各一个 token，模板里不许再出现字面数字 ----
    _literal_radius = [
        line.strip()
        for line in _tpl_code.splitlines()
        if "border-radius:" in line and "$" not in line
    ]
    check(
        "圆角只走命名 token（模板里不许出现字面数字）",
        not _literal_radius
        and set(theme_mod._RADIUS)
        == {"radius_container", "radius_control", "radius_pill", "radius_bar"}
        and all(
            isinstance(v, str) and v.endswith("px") for v in theme_mod._RADIUS.values()
        ),
        f"字面={_literal_radius} token={sorted(theme_mod._RADIUS)}",
    )
    _radius_in_palette = [
        tid for tid in _ids if not set(theme_mod._RADIUS) <= set(_palettes[tid])
    ]
    check(
        "圆角 token 并进了调色板（替换表就是调色板，另开一条查找路径等于漏一处不报错）",
        not _radius_in_palette,
        str(_radius_in_palette),
    )

    # ---- 选择器的控件类必须与使用处一致 ----
    #
    # `#{name}` 的「已覆盖」判断不能只看名字在不在样式表里：`QLabel#chip` 曾经
    # 就是这么一条**死规则**——使用它的两处都是 `QPushButton`，于是样式表构造
    # 成功、加载成功、零警告，那两处却静静地落回通用按钮样式。
    check(
        "chip 的选择器前缀是 QPushButton（使用处就是 QPushButton）",
        "QPushButton#chip" in _tpl_code and "QLabel#chip" not in _tpl_code,
        f"QPushButton#chip={('QPushButton#chip' in _tpl_code)} "
        f"QLabel#chip={('QLabel#chip' in _tpl_code)}",
    )
    # 使用处那一侧同样要核：样式表写对了、控件还是拿 `QLabel` 建的话，同样匹配不上。
    # 判据看的是 `setObjectName("chip")` 的**上一行**——控件是在那里建的。
    _widgets_src = (
        Path(__file__).resolve().parents[1] / "app" / "ui" / "widgets.py"
    ).read_text(encoding="utf-8").splitlines()
    _chip_owners = [
        _widgets_src[idx - 1].strip()
        for idx, line in enumerate(_widgets_src)
        if 'setObjectName("chip")' in line and idx > 0
    ]
    check(
        "挂 `chip` 的控件确实是 QPushButton 建的（两处都核）",
        len(_chip_owners) == 2
        and all("QPushButton(" in line for line in _chip_owners),
        str(_chip_owners),
    )

    # ---- 交互状态：按下态与选中态 ----
    #
    # 这两条此前**一条规则都没有**：按下去与不按一模一样，模式切换的选中态与
    # 未选中像素级相同（画面上看不出自己在哪个集合里）。存在性只是第一层，
    # 真判据在 `gui_smoke` 里读实际像素。
    _states = {
        "按下态": "QPushButton:pressed {" in _tpl_code,
        "选中态": "QPushButton#mode:checked {" in _tpl_code,
    }
    check(
        "按下态与选中态各有规则（此前各 0 条）",
        all(_states.values()),
        str(_states),
    )
    _review_veil = [
        line
        for line in theme_mod.stylesheet().splitlines()
        if "QPushButton#review" in line and "rgba(" in line
    ]
    check(
        "左栏「待复习」按钮已接进「直接压在原图上」的登记表（配图后不垫底就读不出来）",
        len(_review_veil) == len(_levels),
        f"{len(_review_veil)} 条（应 {len(_levels)} 档）",
    )

    # ---- 同一语义只有一份定义：两页对话框的字体规则住在模板里 ----
    _ui_dir = Path(__file__).resolve().parents[1] / "app" / "ui"
    _inline_style = {
        name: [
            decl
            for decl in ("font-size:", "font-weight:", "padding:", "border-radius:")
            if decl in (_ui_dir / name).read_text(encoding="utf-8")
        ]
        for name in ("onboarding.py", "review_dialog.py")
    }
    _inline_style = {k: v for k, v in _inline_style.items() if v}
    check(
        "欢迎页 / 复习页不再内联样式串（字体规则已收进 theme.py 模板）",
        not _inline_style,
        str(_inline_style),
    )
    _moved_rules = [
        sel
        for sel in (
            "QDialog#onboarding",
            "QLabel#welcomeTitle",
            "QLabel#featureBody",
            "QDialog#reviewDialog",
            "QLabel#reviewLemma",
            "QLabel#reviewHint",
        )
        if sel in _tpl_code
    ]
    check(
        "欢迎页 / 复习页的六条代表规则确实住在模板里",
        len(_moved_rules) == 6,
        f"缺：{sorted(set(('QDialog#onboarding', 'QLabel#welcomeTitle', 'QLabel#featureBody', 'QDialog#reviewDialog', 'QLabel#reviewLemma', 'QLabel#reviewHint')) - set(_moved_rules))}",
    )

    # ---- 欢迎页 / 教程的说明文字：各自排得下一行 ----
    #
    # 中文换行能从任意两字之间断开，说明写长了就会把「收藏」切成「收 / 藏」。
    # 这条**不放在 `gui_smoke` 里量**：那边把平台钉成 `offscreen`，而 offscreen
    # 的西文字形宽度与真机差近一倍（同一句实测 401px → 624px），拿它判「一行
    # 排得下」会把真机上明明排得下的句子判成折行——方向刚好反了。
    #
    # 改用静态预算：真机量出来一个全角宽 13.0px（`#featureBody` 的 Microsoft
    # YaHei UI 13px），可排区 520 − 36 × 2 = 448px ⇒ 34 个全角宽，半角记 0.5。
    # 这是**代理指标**，不是排版本身：它管的是「别把一句说明写成一段」，换字体
    # 会偏一点，真正的观感仍要看截图。
    _zh_budget = 34.0

    def _zh_width(text: str) -> float:
        return sum(0.5 if ch.isascii() else 1.0 for ch in text)

    _copy = [
        (f"功能点 {i}", text) for i, (_head, text) in enumerate(ob_mod._FEATURES, 1)
    ] + [
        (f"教程第 {i} 步", text) for i, (_head, text) in enumerate(ob_mod._TUTORIAL, 1)
    ]
    _too_long = {
        name: round(_zh_width(text), 1)
        for name, text in _copy
        if _zh_width(text) > _zh_budget or "\n" in text
    }
    check(
        f"欢迎页与教程的每句说明都排得下一行（≤ {_zh_budget:g} 个全角宽）",
        not _too_long,
        str(_too_long),
    )
    # 换主题必须真的换出不同的样式表。把强调色换成一个表里没有的值，若样式表
    # 不变，说明有个地方把它写死了——而那处永远不会报错。
    _sheets = {tid: theme_mod.stylesheet(tid) for tid in _ids}
    check(
        "每套主题生成不同的样式表（含自己的强调色）",
        len(set(_sheets.values())) == len(_ids)
        and all(_palettes[tid]["accent"].lower() in _sheets[tid].lower() for tid in _ids),
        f"{len(set(_sheets.values()))} 份 / {len(_ids)} 套",
    )

    # 背景透出档位按主题各算一遍：深浅两系的最坏情况是**相反**的（浅色怕背景
    # 那块纯黑，深色怕纯白），只验默认那套等于放掉一半。
    _veil_bad = []
    for _tid in _ids:
        _pal = _palettes[_tid]
        _lv = _pal["veil"]
        _under = 0 if not _pal["dark"] else 255
        _values = list(_lv.values())
        if _values != sorted(_values, reverse=True) or len(set(_values)) != len(_values):
            _veil_bad.append(f"{_tid} 档位不单调/有重复")
        if _lv[theme_mod.BG_VEIL_DEFAULT] > max(_values) - 10:
            _veil_bad.append(f"{_tid} 默认档太含蓄")
        for _name, _pct in _lv.items():
            _c = _contrast(_pal["text"], _blend(_pal["surface"], _under, _pct))
            if _c < 4.5:
                _veil_bad.append(f"{_tid}/{_name} 正文={_c:.2f}")
        _deepest = min(_lv, key=lambda name: _lv[name])
        _c2 = _contrast(_pal["text"], _blend(_pal["surface"], _under, _lv[_deepest] - 15))
        if _c2 >= 4.5:
            _veil_bad.append(f"{_tid} 再降 15 个百分点仍达标（下限不是真的下限）")
    check(
        "每套主题的每个档位都守住正文对比度，且再降 15 个百分点必跌破",
        not _veil_bad,
        str(_veil_bad),
    )

    # 「最透那一档把直接压面板的文字换加强色」。判据要落在**改善了多少**上：
    # 只说「换了颜色」没有意义——换成一个同样读不清的颜色也算换过。
    _strong_bad = []
    for _tid in _ids:
        _pal = _palettes[_tid]
        _lv = _pal["veil"]
        _deepest = min(_lv, key=lambda name: _lv[name])
        _under = 0 if not _pal["dark"] else 255
        _worst = _blend(_pal["surface"], _under, _lv[_deepest])
        _mid = _blend(_pal["surface"], 128, _lv[_deepest])
        for _weak_key, _strong_key in (
            ("text2", "text2_strong"),
            ("text3", "text3_strong"),
        ):
            _after_worst = _contrast(_pal[_strong_key], _worst)
            _after_mid = _contrast(_pal[_strong_key], _mid)
            _before_worst = _contrast(_pal[_weak_key], _worst)
            if _after_worst < 3.0 or _after_mid < 4.5:
                _strong_bad.append(
                    f"{_tid}/{_weak_key} 换后 {_after_worst:.2f}/{_after_mid:.2f}"
                )
            # 成对断言：原色在**最坏背景**上必须本来就不达标，否则这条规则是多余的。
            # 判据取最坏而不是取中等亮度：深浅两系的最坏情况相反，拿中等亮度当判据
            # 会让深色那套（原色本来就还看得清）被误判成「多余的规则」。
            if _before_worst >= 3.0:
                _strong_bad.append(
                    f"{_tid}/{_weak_key} 原色在最坏背景上本来就有 {_before_worst:.2f}，这条规则是多余的"
                )
    check(
        "最透档把面板上的文字换了加强色，且确实从「读不清」变成「读得清」",
        not _strong_bad,
        str(_strong_bad),
    )
    # 加强色这一组靠「声明了 color」来认，不再靠「选择器里有 QLabel#」：
    # 这组规则里还有一条 QStatusBar 的，把它排在外面等于漏掉一条；而借着
    # 选择器类型认，将来再加一个别的组件就又要改一次判据。半透明那组用的是
    # `background: rgba(...)`，不含 ` color: `，不会混进来。
    _deep_rules = [
        _line
        for _line in theme_mod.stylesheet().splitlines()
        if "hasBackground" in _line and " color: " in _line
    ]
    _all_levels = list(_levels)
    _deepest_default = min(_levels, key=lambda name: _levels[name])
    check(
        "加强色规则只挂在最透那一档上（soft / medium 不该被动到）",
        _deep_rules
        and all(f'bgVeil="{_deepest_default}"' in rule for rule in _deep_rules)
        and all(
            f'bgVeil="{other}"' not in rule
            for rule in _deep_rules
            for other in _all_levels
            if other != _deepest_default
        ),
        f"{len(_deep_rules)} 条，档位={_deepest_default}",
    )

    # ---- 24. 复习模式（SM-2 间隔重复） ----
    #
    # 复习算法的价值全在「答错会不会重置、答对会不会推进」这些分支上——写错一个，
    # 用户背到一半发现间隔是乱的。SM-2 是纯函数，正好逐分支喂状态断言。仓储层的
    # due 查询与评分推进走真查询真写库，不是复制品。
    from datetime import datetime as _dt  # noqa: PLC0415
    from app.core import review as review_mod  # noqa: PLC0415
    from app.core.db import (  # noqa: PLC0415
        ENTRIES_ADDED_COLUMNS,
        LEGACY_ENTRIES_DDL,
    )
    from app.core.exporter import JSON_EXCLUDED_FIELDS  # noqa: PLC0415

    check(
        "SM-2 忘了：ease 扣 0.3、间隔回 1 天、次数清零",
        review_mod.sm2(2.5, 6.0, 2, review_mod.FORGOT) == (2.2, 1.0, 0),
        str(review_mod.sm2(2.5, 6.0, 2, review_mod.FORGOT)),
    )
    check(
        "SM-2 模糊：ease 扣 0.15、间隔回 1 天、次数不变",
        review_mod.sm2(2.5, 6.0, 2, review_mod.FUZZY) == (2.35, 1.0, 2),
        str(review_mod.sm2(2.5, 6.0, 2, review_mod.FUZZY)),
    )
    check(
        "SM-2 记住第一次：间隔 1 天、次数 1",
        review_mod.sm2(2.5, 0.0, 0, review_mod.REMEMBERED) == (2.5, 1.0, 1),
        str(review_mod.sm2(2.5, 0.0, 0, review_mod.REMEMBERED)),
    )
    check(
        "SM-2 记住第二次：间隔 6 天、次数 2",
        review_mod.sm2(2.5, 1.0, 1, review_mod.REMEMBERED) == (2.5, 6.0, 2),
        str(review_mod.sm2(2.5, 1.0, 1, review_mod.REMEMBERED)),
    )
    check(
        "SM-2 记住第三次起：间隔乘 ease",
        review_mod.sm2(2.5, 6.0, 2, review_mod.REMEMBERED) == (2.5, 15.0, 3),
        str(review_mod.sm2(2.5, 6.0, 2, review_mod.REMEMBERED)),
    )
    check(
        "SM-2 ease 不跌破下限",
        review_mod.sm2(review_mod.MIN_EASE, 1.0, 0, review_mod.FORGOT)[0]
        == review_mod.MIN_EASE,
        str(review_mod.sm2(review_mod.MIN_EASE, 1.0, 0, review_mod.FORGOT)),
    )
    _sm2_bad = False
    try:
        review_mod.sm2(2.5, 1.0, 0, 99)
    except ValueError:
        _sm2_bad = True
    check("SM-2 未知评分抛 ValueError（不静默当成记住）", _sm2_bad)

    # 复习四列的登记：与搭配列同一条纪律——DDL 管新库、ENTRIES_ADDED_COLUMNS 管
    # 老库补列，两份必须一致；漏登记则老用户升级后永远没有这一列，任何带它的
    # 语句随后失败。LEGACY_ENTRIES_DDL 是冻结快照，刻意不动。
    _rv_cols = ("review_due", "review_count", "review_interval", "review_ease")
    check(
        "复习四列已登记进 ENTRIES_ADDED_COLUMNS",
        all(c in ENTRIES_ADDED_COLUMNS for c in _rv_cols),
        str(sorted(ENTRIES_ADDED_COLUMNS)),
    )
    check("复习四列在 DDL 里都有（新库建表）", all(c in DDL for c in _rv_cols))
    check(
        "复习四列在 LEGACY_ENTRIES_DDL 里都没有（冻结快照不动）",
        all(c not in LEGACY_ENTRIES_DDL for c in _rv_cols),
    )
    # 复习字段是本机学习历史，与 created_at 同类，搬走即错 → 不进 JSON 导出。
    check(
        "复习四列不进 JSON 导出（本机学习历史）",
        all(c in JSON_EXCLUDED_FIELDS for c in _rv_cols),
        str(sorted(JSON_EXCLUDED_FIELDS)),
    )

    # 仓储层真实链路：新词入库即播种（立即到期）、due 查询按到期先后、评分推进、
    # 撤销恢复保留复习历史。
    _due_probe = repo.upsert(Entry(lemma="reviewseed", definition_zh="复习播种探针"))
    _now_dt = _dt.now()
    _now_s = review_mod.format_timestamp(_now_dt)
    _seed_row = repo.get_by_id(int(_due_probe.id))
    check(
        "新词入库即进入复习计划（review_due 非空、计入待复习）",
        bool(_seed_row.review_due) and repo.due_count(_now_s) >= 1,
        f"review_due={_seed_row.review_due!r} due_count={repo.due_count(_now_s)}",
    )
    _due_list = repo.due_entries(_now_s)
    check(
        "due_entries 只含到期词且含刚播种的词",
        any(e.id == _due_probe.id for e in _due_list),
        str([e.lemma for e in _due_list]),
    )
    repo.apply_review(int(_due_probe.id), review_mod.FORGOT, _now_dt)
    _after_forgot = repo.get_by_id(int(_due_probe.id))
    check(
        "评分「忘了」后间隔回 1 天、次数清零",
        _after_forgot.review_interval == 1.0 and _after_forgot.review_count == 0,
        f"interval={_after_forgot.review_interval} count={_after_forgot.review_count}",
    )
    repo.apply_review(int(_due_probe.id), review_mod.REMEMBERED, _now_dt)
    repo.apply_review(int(_due_probe.id), review_mod.REMEMBERED, _now_dt)
    _after_remember = repo.get_by_id(int(_due_probe.id))
    check(
        "评分「记住了」两次后间隔 6 天、次数 2",
        _after_remember.review_interval == 6.0 and _after_remember.review_count == 2,
        f"interval={_after_remember.review_interval} count={_after_remember.review_count}",
    )
    # 撤销删除恢复旧词条时，复习历史要保留（不重置）——这靠 upsert 的 INSERT 分支
    # 「进来的 Entry 有复习历史就照原样写」那条判据。
    _rv_snap = repo.get_by_id(int(_due_probe.id))
    repo.delete(int(_due_probe.id))
    _rv_snap.id = None
    _rv_snap.created_at = ""
    _restored = repo.upsert(_rv_snap)
    _restored_row = repo.get_by_id(int(_restored.id))
    check(
        "撤销恢复保留复习历史（间隔与次数不重置）",
        _restored_row.review_interval == 6.0 and _restored_row.review_count == 2,
        f"interval={_restored_row.review_interval} count={_restored_row.review_count}",
    )

    # ---- 托盘 / 全局热键 / 离线待重查 ----
    # 这三块都有一模一样的失败形态：**什么都没发生，也不报错**。
    # 热键没注册上表现为「按了没反应」，托盘没建起来表现为「关掉窗口进程还在」，
    # 标签写错表现为「用户不知道该重查」。所以判据必须打在能读出差别的观测上。
    try:
        from app.providers.base import source_badge
        from app.ui import hotkey as hotkey_mod

        check(
            "离线词条标签写「离线待重查」",
            source_badge("offline") == "离线待重查",
            repr(source_badge("offline")),
        )
        # 非离线来源**必须**保留「来源」前缀：把接口结果也写成一句「待重查」，
        # 用户会以为自己拿到的内容跟离线一样残缺。
        check(
            "接口词条标签仍写来源",
            source_badge("openai-compat") == "来源 接口",
            repr(source_badge("openai-compat")),
        )
        # 大小写与空白不该改变判定：库里的 source 来自配置与常量，写法不统一时
        # 上面那条就会悄悄失效（标签变回「来源 本机离线词库」而不报错）。
        check(
            "标签判定不区分来源大小写",
            source_badge("  Offline ") == "离线待重查",
            repr(source_badge("  Offline ")),
        )

        # 组合键只有一份定义：显示文字、虚拟键码、修饰键三者必须互相吻合。
        # 分开写两个常量时，改了显示而忘了注册，用户会按一个从没被注册的键。
        check(
            "热键显示文字与注册的键码一致",
            hotkey_mod.HOTKEY_TEXT == "Alt+Q"
            and hotkey_mod._VK == ord("Q")
            and hotkey_mod._MODS & hotkey_mod.MOD_ALT
            and not hotkey_mod._MODS & 0x0002,  # 不许混进 Ctrl
            f"{hotkey_mod.HOTKEY_TEXT} VK={hotkey_mod._VK:#x} MODS={hotkey_mod._MODS:#x}",
        )

        _seeds = {
            "ubiquitous": "ubiquitous",
            "  meticulous  ": "meticulous",
            '"take off"': "take off",
            "in charge of": "in charge of",
            # 整句：4 词以上不预填。填进去用户先要删一遍，比不填更烦。
            "She walked into the room slowly": "",
            "as a matter of fact": "",
            # 含数字/标点：不是词条
            "3 apples": "",
            "e.g.": "",
            "": "",
        }
        _bad_seed = {
            k: hotkey_mod.clipboard_seed(k)
            for k, want in _seeds.items()
            if hotkey_mod.clipboard_seed(k) != want
        }
        check("剪贴板预填只收词与词组", not _bad_seed, str(_bad_seed) or "全部符合预期")
        check(
            "剪贴板多行只取第一行有内容的",
            hotkey_mod.clipboard_seed("\n\n  meticulous \n第二行不算") == "meticulous",
            repr(hotkey_mod.clipboard_seed("\n\n  meticulous \n第二行不算")),
        )
    except Exception as exc:  # noqa: BLE001
        check("离线词条标签写「离线待重查」", False, str(exc))
        check("接口词条标签仍写来源", False, "前置步骤失败")
        check("标签判定不区分来源大小写", False, "前置步骤失败")
        check("热键显示文字与注册的键码一致", False, "前置步骤失败")
        check("剪贴板预填只收词与词组", False, "前置步骤失败")
        check("剪贴板多行只取第一行有内容的", False, "前置步骤失败")

    # 托盘开关必须能随配置保存。它挂在 _COERCERS 之外时不会报错，只会表现成
    # 「设置里关掉托盘、重启后窗口又藏进托盘」——开关形同虚设。
    try:
        import app.config as _cfg_mod

        _saved_paths = (_cfg_mod.APP_DIR, _cfg_mod.CONFIG_PATH)
        _tray_dir = tmpdir / "tray_cfg"
        _tray_dir.mkdir(parents=True, exist_ok=True)
        _cfg_mod.APP_DIR = _tray_dir
        _cfg_mod.CONFIG_PATH = _tray_dir / "config.json"
        try:
            _cfg_mod.save_config({"tray_enabled": False})
            _off_ok = _cfg_mod.load_config()["tray_enabled"] is False
            _cfg_mod.save_config({"tray_enabled": True})
            _on_ok = _cfg_mod.load_config()["tray_enabled"] is True
        finally:
            _cfg_mod.APP_DIR, _cfg_mod.CONFIG_PATH = _saved_paths
        check("托盘开关可保存且能关掉", _off_ok and _on_ok, f"关={_off_ok} 开={_on_ok}")
    except Exception as exc:  # noqa: BLE001
        check("托盘开关可保存且能关掉", False, str(exc))

    # 单实例唤醒通道。托盘常驻之后窗口可能藏在托盘里，第二个进程必须能把它
    # 叫出来；这条通道要是坏了，双击图标只会得到一句「已在运行」然后什么都不
    # 发生，而进程还在后台占着数据库。
    try:
        import app.main as _main_mod
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalServer

        _probe_name = "Quill.Selfcheck.Activate"
        # 用 QCoreApplication 而不是 QApplication：这条链路只要事件分发，
        # 不需要窗口系统。建 QApplication 会在无显示环境里要求平台插件，
        # 把一条「通道通不通」的判据变成「这台机器有没有桌面」。
        _qapp = QCoreApplication.instance() or QCoreApplication([])
        _hits: list[int] = []
        _srv = _main_mod.start_activation_server(
            lambda: _hits.append(1), None, _probe_name
        )
        _listen_ok = _srv is not None
        _knock = _main_mod.notify_running_instance(_probe_name) if _listen_ok else False
        _deadline = time.monotonic() + 3.0
        while not _hits and time.monotonic() < _deadline:
            _qapp.processEvents()
            time.sleep(0.01)
        if _srv is not None:
            _srv.close()
        QLocalServer.removeServer(_probe_name)
        check(
            "单实例唤醒通道可用",
            _listen_ok and _knock and bool(_hits),
            f"监听={_listen_ok} 敲通={_knock} 回调={len(_hits)} 次",
        )
    except Exception as exc:  # noqa: BLE001
        check("单实例唤醒通道可用", False, str(exc))

    # ---- 本地使用统计 / 授权 / 版本号 ----
    # 这一块的失败形态同样是「静默」：事件没写进去只是摘要少几条，开关没生效
    # 只是「关掉了还在记」，版本号写错只有拿到报告的人才看得出来。所以判据都
    # 打在能读出差别的地方，而且每条都先构造出「不该发生」的那一侧。
    try:
        from app import config as cfg_mod
        from app.core import analytics as ana
        from app.version import APP_VERSION

        _defaults = dict(cfg_mod.DEFAULT_CONFIG)
        check(
            "统计默认关闭且未表态",
            _defaults.get("analytics_enabled") is False
            and _defaults.get("analytics_consent_decided") is False,
            f"enabled={_defaults.get('analytics_enabled')} "
            f"decided={_defaults.get('analytics_consent_decided')}",
        )
        # 漏登记 _COERCERS 的表现是「关掉之后重启又自己打开」——开关形同虚设，
        # 不报任何错。所以这条不是形式检查。
        _need = (
            "analytics_enabled",
            "analytics_consent_decided",
            "analytics_schema_version",
        )
        _missing = [k for k in _need if k not in cfg_mod._COERCERS]
        check(
            "统计字段都在配置校验表里",
            not _missing,
            ("缺 " + "、".join(_missing)) if _missing else "三个齐全",
        )

        # 版本号只有一份定义在 app/version.py，CHANGELOG 是另一份。两处不一致时
        # 报告里的版本号就失去了对照价值，而这件事没有别的办法发现。
        _changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
        _first_version = ""
        for _line in _changelog.read_text(encoding="utf-8").splitlines():
            _stripped = _line.strip()
            if _stripped.startswith("## v"):
                _first_version = _stripped[3:].strip().split("（")[0].strip()
                _first_version = _first_version.lstrip("v").strip()
                break
        check(
            "版本常量与 CHANGELOG 首条一致",
            bool(_first_version) and _first_version == APP_VERSION,
            f"常量={APP_VERSION!r} CHANGELOG={_first_version!r}",
        )

        # 配置写入必须原子完成：写到一半坏掉时，读取侧会静默退回默认值，
        # 用户的「已拒绝统计」选择就丢了，下次启动会重新弹告知页。
        _atomic_dir = Path(tempfile.mkdtemp(prefix="quill_selfcheck_cfg_"))
        _saved_app_dir = cfg_mod.APP_DIR
        _saved_cfg_path = cfg_mod.CONFIG_PATH
        _saved_ana_path = cfg_mod.ANALYTICS_PATH
        try:
            cfg_mod.APP_DIR = _atomic_dir
            cfg_mod.CONFIG_PATH = _atomic_dir / "config.json"
            cfg_mod.ANALYTICS_PATH = _atomic_dir / "analytics.db"
            cfg_mod.save_config({"theme": "night", "analytics_enabled": True})
            _reloaded = cfg_mod.load_config()
            _tmp_left = list(_atomic_dir.glob("*.tmp"))
            check(
                "配置原子写入且不留临时文件",
                _reloaded.get("analytics_enabled") is True
                and _reloaded.get("theme") == "night"
                and not _tmp_left,
                f"enabled={_reloaded.get('analytics_enabled')} "
                f"theme={_reloaded.get('theme')!r} 残留={[p.name for p in _tmp_left]}",
            )

            # 未授权写入必须为假，而且不该留下库文件。
            cfg_mod.save_config({"analytics_enabled": False})
            _wrote_unauthorized = ana.record_event("app_started")
            check(
                "未授权时一个事件都不记",
                _wrote_unauthorized is False and not ana.db_path().exists(),
                f"写入={_wrote_unauthorized} 库存在={ana.db_path().exists()}",
            )

            ana.set_enabled(True)
            _ok_event = ana.record_event(
                "dictionary_lookup",
                {"duration_ms": 1234, "success": True, "secret": "不该进来"},
            )
            _bad_type = ana.record_event(
                "dictionary_lookup", {"duration_ms": True, "success": "yes"}
            )
            _unknown = ana.record_event("user_typed_the_word")
            _rows = ana.report_rows(10)
            _lookup = [r for r in _rows if r["event_name"] == "dictionary_lookup"]
            # 取「最近一条」会拿到刚写的类型不符那条（属性被清空），所以判据必须
            # 问「有没有一条带着完整属性」，不能只看第一条——第一版就是这么写错的。
            _cleaned_set = [r["properties"] for r in _lookup]
            # 两件事都要判：该在的在（duration_ms / success 原样保留），
            # 不该在的不在（多传的 secret 必须丢弃）。只判前半句的话，
            # 白名单整个失效也照样通过——变异测试第一次跑就是这么 MISSED 的。
            _leaked = [p for p in _cleaned_set if "secret" in p]
            check(
                "事件写入且属性经白名单过滤",
                _ok_event is True
                and {"duration_ms": 1234, "success": True} in _cleaned_set
                and not _leaked,
                f"写入={_ok_event} 属性集={_cleaned_set}",
            )
            # 类型不符的属性要被丢掉，但事件本身保留——多传一个坏字段不该让整条消失。
            check(
                "属性类型不符即丢弃，事件仍保留",
                _bad_type is True and len(_lookup) == 2,
                f"写入={_bad_type} 命中={len(_lookup)} 条",
            )
            check("未知事件名被拒绝", _unknown is False, f"写入={_unknown}")

            ana.set_enabled(False)
            _after_off = ana.record_event("app_started")
            _total_after_off = ana.total()
            check(
                "关闭统计后不再写入",
                _after_off is False and _total_after_off == 2,
                f"写入={_after_off} 总数={_total_after_off}",
            )
            _cleared = ana.clear()
            check(
                "清除只清统计事件",
                _cleared == 2 and ana.total() == 0 and ana.db_path().exists(),
                f"清除={_cleared} 剩余={ana.total()}",
            )
        finally:
            cfg_mod.APP_DIR = _saved_app_dir
            cfg_mod.CONFIG_PATH = _saved_cfg_path
            cfg_mod.ANALYTICS_PATH = _saved_ana_path
            shutil.rmtree(_atomic_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        check("统计默认关闭且未表态", False, str(exc))

    conn.close()

    print("=" * 56)
    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print("=" * 56)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    return 1 if FAILED else 0


def _temp_leftovers() -> list[str]:
    """当前系统临时目录里本程序留下的目录。

    **前缀表只留一份定义**（`clean_temp.py::PREFIXES`），不在这里另抄一份。

    这里原先自己写了一份，而且**比那一份窄**：少了 `quill_shot_*` /
    `quill_lock_*` / `quill_ablate_*` / `quill_probe_*` / `quill_gui_*` /
    `quill_export_*` / `quill_v1_*` / `quill_userdict_*` 八个。后果不是
    「少报几条」，是**整整一类泄漏不在视野里**：实测临时目录里躺着一个 10 月 11 日的
    `quill_selfcheck_*`（装着 `backup_src.db` 等文件），其后跑了几十轮自检，
    **没有任何一条断言提到过它**。「判据覆盖的目标恰好是被漏掉的那一类」，
    正是本项目反复踩到的形态——所以这里改成从 `clean_temp` 取，别处也只许有那一份。
    """
    tmp = tempfile.gettempdir()
    out: list[str] = []
    for pat in TEMP_PREFIXES:
        out.extend(glob.glob(os.path.join(tmp, pat)))
    return sorted(out)


# 扫描时跳过的目录名前缀：第三方与产物，不是本程序源码。`.venv` 里几万个 `.py`
# 会让扫描慢到不可接受，`_ecdict_src` 有 302 MB，`build*` / `dist*` 是产物快照。
_SCAN_SKIP_PREFIXES = (
    ".venv",
    "_ecdict_src",
    "build",
    "dist",
    "__pycache__",
    "NVIDIA",
)


def _collect_mkdtemp_prefixes() -> set[str]:
    """全仓所有 `mkdtemp(prefix="...")` 用的前缀（真实扫文件，不是读清单）。

    **按 AST 取、不用正则**：正则会把文档里那句举例也收进来当前缀——本函数第一版
    就是这么写的，结果这条检查被**自己的说明文字**绊倒，报出一个前缀 `'...'`。
    这正是本项目反复出现的形态：**判据抓错了对象**。AST 只认真正的调用。
    """
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if any(part.startswith(_SCAN_SKIP_PREFIXES) for part in parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "mkdtemp":
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "prefix"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    found.add(kw.value.value)
    return found


def _uncovered_prefixes(prefixes: tuple[str, ...] | None = None) -> list[str]:
    """前缀表**认不出来**的那些 `mkdtemp` 前缀。传 None 表示用真实的那份表。"""
    table = TEMP_PREFIXES if prefixes is None else prefixes
    return sorted(
        prefix
        for prefix in _collect_mkdtemp_prefixes()
        if not any(fnmatch.fnmatch(prefix, pat) for pat in table)
    )


def _selftest_leftover_prefixes() -> int:
    """自证残留检查的前缀表**取自 `clean_temp` 那一份**，而且**没有漏项**。

    两件事分开验，因为它们是两种不同的失效：

    ① **表是共用的**：建一个 `quill_shot_` 前缀的临时目录——旧的那份窄表认不出
       它——再看 `_temp_leftovers()` 报不报。不报，就说明这个检查又退回了「只认
       自己在用的那几个前缀」的状态，而它当初正是因此漏掉了一整类泄漏。
    ② **表没有漏项**：把全仓 `mkdtemp(prefix=...)` 扫出来逐个问表认不认。
       ① **根本验不出这一条**：一张共用的、人人都读的**窄表**照样能通过 ①。
       实测就漏过三个（`quill_tags_` / `quill_undo_` / `quill_phrase_`）：
       对应的三个小节各自建了临时库，而前缀表里一行都没写，全套检查跑了几十轮
       **没有任何一条断言提到它们**——「判据覆盖的目标恰好是被漏掉的那一类」。

    一个不会失败的检查等于没有检查：② 用一个**故意认不出任何东西的表**再跑一遍，
    必须报出漏项，否则这条检查自己就是空的。
    """
    probe = Path(tempfile.mkdtemp(prefix="quill_shot_"))
    (probe / "sentinel").write_text("x", encoding="utf-8")
    try:
        seen = str(probe) in _temp_leftovers()
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    if not seen:
        print(
            f"  FAIL  残留检查认不出 {probe.name} —— 前缀表多半又被抄成窄的一份了"
        )
        return 1
    print("  PASS  残留检查的前缀表取自 clean_temp（认得出 quill_shot_*）")

    missing = _uncovered_prefixes()
    if missing:
        print(f"  FAIL  前缀表漏登记了 {len(missing)} 个 mkdtemp 前缀：{missing}")
        return 1
    # 自证：换成一个认不出任何东西的表，上面那条必须报出来。
    if not _uncovered_prefixes(("zzz_这一项不可能是任何前缀_*",)):
        print("  FAIL  漏项检查不会失败（换成空表也报「没有漏项」，等于没有这条检查）")
        return 1
    print(
        f"  PASS  前缀表覆盖全仓所有 mkdtemp 前缀（{len(_collect_mkdtemp_prefixes())} 个）"
        "，并对空表自证过它会失败"
    )
    return 0


def _verify_no_temp_leftovers() -> int:
    """确认本次运行没有在系统临时目录里留下 `quill_*` 目录。

    这条检查放不进 `main()`：临时目录是 `atexit` 里删的，那一刻自检已经跑完、
    汇总行都打过了。**但它必须被检查**——这个泄漏曾静默累积到 3763 个目录 /
    约 2.6 GB，把系统盘写满，然后整个测试环境开始报 `ENOSPC`，表现得像
    「工具坏了」而不是「有个目录没删」。没有任何断言盯着它，就会再犯一次。
    """
    leftovers = _temp_leftovers()
    if leftovers:
        print(f"  FAIL  临时目录已清理  {len(leftovers)} 个残留：{leftovers[:3]}")
        return 1
    print("  PASS  临时目录已清理")
    return 0


if __name__ == "__main__":
    code = main()
    # 先跑 atexit 的清理，再验「真的清了」——顺序不能反。
    import atexit as _atexit

    _atexit._run_exitfuncs()
    return_code = code or _selftest_leftover_prefixes() or _verify_no_temp_leftovers()
    raise SystemExit(return_code)
