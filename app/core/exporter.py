"""导出模块：生成适合打印的纯文本，以及结构化 Markdown。

排版原则：不依赖中英混排的等宽对齐（中文在等宽字体下宽度为 2，
对齐必然错位），改用固定宽度的单字标签加缩进，任何字体下都整齐。

关于换行，两类字段的处理刻意不同：
- **机器生成的单值字段**（释义、例句、同义语）内部换行只可能是模型输出的噪音，
  一律折叠为空格——在打印稿里保留它只会破坏版式。
- **用户手写的备注**可能有意分行（记忆法、联想、搭配），保留换行，
  但续行必须缩进对齐，否则打印出来会顶到最左侧、看起来脱离了所属词条。

六种格式各有明确的用途，不是「多提供几种总有你喜欢的」：

| 格式 | 给谁看 | 关键取舍 |
|---|---|---|
| 纯文本 | 打印 | 保留用户备注的换行与行首字符（不转义） |
| Markdown | 粘进别的工具 | 转义元字符，防止备注被渲染成结构 |
| PDF | 直接交出去 / 打印 | 中文必须嵌入字体，能搜索能复制 |
| CSV | 表格软件 | 带 BOM，否则中文 Windows 的 Excel 按 GBK 打开成乱码 |
| Anki | 导入 Anki 背 | 制表符分三列（正面 / 背面 / 标签），**刻意不带 BOM** |
| JSON | 程序读 / 备份 | 带格式标识与版本号，排除本机主键与运行时字段 |

**六种格式全部带上「固定搭配」**（`collocations`）。这不是「顺手都加上」，而是
一条判据：搭配是用户花接口额度换来的内容，任何一种导出把它丢掉，都属于
**静默丢数据**——而用户要等到打印出来或导进 Anki 之后才会发现。各格式的呈现
按自己的版式走（文本/PDF 缩进成两层、CSV 压进一格、Anki 进背面、JSON 保结构）。

CSV 与 JSON 都**不还原完整词条**：CSV 是扁平的，同义语与固定搭配（都带中文
简释与差异说明/例句）被压成一格，是**有损**的；JSON 为了能安全地在机器之间搬，
刻意不带 `id` 与时间戳。真正无损的还原路径是「本程序的纯文本导出 → 导入」
与「JSON 导出 → 导入」。

Anki 那条同样是有损的，而且**更**有损：它只留「词 / 释义 / 标签」三层，
音标、例句与固定搭配被折进背面那一格，备注的换行改写成 `<br>`。它要解决的
不是保真，而是「把生词接进真的记忆流程」——那条路以前只有手工复制粘贴。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

from .models import Entry

# 模块级持有 QGuiApplication 引用，防止被 GC 回收（见 _ensure_gui_app）。
_GUI_APP = None

RULE = "-" * 72
HEAVY_RULE = "=" * 72
INDENT = "    "
# 续行缩进 = INDENT(4) + 单字标签(中文按 2 列计) + 2 个空格，共 8 列。
# 与「英 / 中 / 例 / 注」各行的正文起始位置对齐。
CONT = INDENT + "    "

# 行内会改变渲染结果的字符。只处理这些，避免把可读内容弄得满是反斜杠。
_MD_INLINE_RE = re.compile(r"([\\`*_])")
# 行首结构字符：`-` `+` `*` 是列表，`>` 是引用，`#` 是标题，
# `=` 可能被当作 setext 下划线（紧随其上的段落会变成标题）。
_MD_LEAD_CHARS = "-+*>=#"
# 行首有序列表标记：`1.` / `2)`，渲染器会按自己的规则重新编号。
_MD_OL_RE = re.compile(r"^(\d{1,9})[.)](?=\s|$)")


def _md_inline(text: str) -> str:
    """转义行内 Markdown 元字符。

    反斜杠一并纳入同一次替换（而非分两步），所以用户原本写的 `\\*` 会被
    转成 `\\\\\\*`，渲染回字面量，不会被二次利用成强调语法。
    """
    return _MD_INLINE_RE.sub(r"\\\1", text or "")


def _md_note_line(line: str) -> str:
    """转义用户备注的一行，阻止它被重新解释为 Markdown 结构。

    备注续行是独立一行（导出时只缩进 2 空格），因此行首的 `-` `>` `#` 会被
    解析成列表 / 引用 / 标题，`1.` 这类编号还会被渲染器改写——用户自己写的
    序号就此消失。这里只把行首那个字符转义，正文交给 `_md_inline()`。
    """
    stripped = line.lstrip()
    if not stripped:
        return line
    indent = line[: len(line) - len(stripped)]
    head = ""
    if stripped[0] in _MD_LEAD_CHARS:
        head, stripped = "\\" + stripped[0], stripped[1:]
    else:
        ordered = _MD_OL_RE.match(stripped)
        if ordered:
            head = ordered.group(1) + "\\"
            stripped = stripped[len(ordered.group(1)) :]
    return indent + head + _md_inline(stripped)


def _flatten(text: str) -> str:
    """把机器生成的单值字段压成一行。"""
    return " ".join((text or "").split())


def _html_escape(text: str) -> str:
    """把文本变成能直接放进富文本单元格的形式。

    名字刻意**不带 `pdf`**：它本就是「HTML 转义」这件事本身，而走富文本的
    出口有两个（PDF 排版、Anki 背面）。各写一份的代价是实测过的那类问题——
    一处记得转义 `&`、另一处忘了，于是「read < write」在某一种产物里被吃掉
    半截，而两边都不会报错。
    """
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _note_lines(text: str) -> list[str]:
    """按行切分用户手写内容；全空白视为无内容，不产出空块。

    首尾的空行会被丢弃（可能是输入时顺手留的），中间的空行保留——那是有意的
    分段。否则纯文本导出会多出只有「注」二字的空行。
    """
    parts = [line.rstrip() for line in (text or "").splitlines()]
    while parts and not parts[0].strip():
        parts.pop(0)
    while parts and not parts[-1].strip():
        parts.pop()
    return parts


def _format_header(title: str, count: int, with_time: bool = True) -> list[str]:
    lines = [title]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M") if with_time else ""
    meta = f"共 {count} 条"
    if stamp:
        meta = f"导出时间 {stamp}    {meta}"
    lines.append(meta)
    lines.append(HEAVY_RULE)
    lines.append("")
    return lines


def _format_entry(index: int, entry: Entry) -> list[str]:
    head = f"{index:>3}. {entry.lemma}"
    if entry.phonetic:
        head += f"  {_flatten(entry.phonetic)}"
    if entry.pos:
        head += f"  {_flatten(entry.pos)}"

    lines = [head]
    if entry.definition_en:
        lines.append(f"{INDENT}英  {_flatten(entry.definition_en)}")
    if entry.definition_zh:
        lines.append(f"{INDENT}中  {_flatten(entry.definition_zh)}")
    if entry.example:
        lines.append(f"{INDENT}例  {_flatten(entry.example)}")
        if entry.example_zh:
            lines.append(f"{CONT}{_flatten(entry.example_zh)}")
    for i, syn in enumerate(entry.synonyms):
        label = "同" if i == 0 else "  "
        text = _flatten(syn.word)
        if syn.gloss_zh:
            text += f"（{_flatten(syn.gloss_zh)}）"
        if syn.diff_note:
            text += f" — {_flatten(syn.diff_note)}"
        lines.append(f"{INDENT}{label}  {text}")

    for i, coll in enumerate(entry.collocations):
        label = "搭" if i == 0 else "  "
        text = _flatten(coll.phrase)
        if coll.gloss_zh:
            text += f"（{_flatten(coll.gloss_zh)}）"
        lines.append(f"{INDENT}{label}  {text}")
        # 例句多缩进两格：它属于上面那条搭配，不缩进就会被读成「这个词的例句」
        # ——而同一个词的多条搭配各有例句，那样读下来会以为例句重复了。
        if coll.example:
            lines.append(f"{CONT}  {_flatten(coll.example)}")
            if coll.example_zh:
                lines.append(f"{CONT}  {_flatten(coll.example_zh)}")

    note = _note_lines(entry.notes)
    if note:
        lines.append(f"{INDENT}注  {note[0]}")
        # 空行保持真正为空：打印稿里不该出现只有缩进空格的「看不见的行」
        lines.extend(f"{CONT}{line}" if line.strip() else "" for line in note[1:])
    return lines


def export_text(entries: list[Entry], title: str = "英语生词本") -> str:
    """生成打印用纯文本。"""
    blocks: list[str] = []
    for i, entry in enumerate(entries, start=1):
        blocks.append("\n".join(_format_entry(i, entry)))
    body = f"\n{RULE}\n".join(blocks)

    lines = _format_header(title, len(entries))
    lines.append(body)
    lines.append("")
    lines.append(RULE)
    lines.append("Generated by Quill")
    return "\n".join(lines)


def export_markdown(entries: list[Entry], title: str = "英语生词本") -> str:
    """生成 Markdown，便于粘贴到其他工具或转 PDF。

    多行备注逐行缩进 2 格，使其留在同一个列表项内。注意 CommonMark 的软换行
    渲染为空格，因此仅在**渲染后**的观感上会并成一段；需要逐行原样呈现的场合
    请用纯文本导出（打印路径即走此路径）。
    """
    lines = [f"# {title}", ""]
    lines.append(f"> 导出时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}　共 {len(entries)} 条")
    lines.append("")

    for i, entry in enumerate(entries, start=1):
        head = f"## {i}. {_md_inline(entry.lemma)}"
        if entry.phonetic:
            # 反引号代码段内部不做转义：反斜杠在代码段里是字面量，转义反而会显示出来
            head += f" `{_flatten(entry.phonetic)}`"
        if entry.pos:
            head += f" *{_md_inline(_flatten(entry.pos))}*"
        lines.append(head)
        lines.append("")
        if entry.definition_en:
            lines.append(f"- **英** {_md_inline(_flatten(entry.definition_en))}")
        if entry.definition_zh:
            lines.append(f"- **中** {_md_inline(_flatten(entry.definition_zh))}")
        if entry.example:
            lines.append(f"- **例** {_md_inline(_flatten(entry.example))}")
            if entry.example_zh:
                lines.append(f"  {_md_inline(_flatten(entry.example_zh))}")
        if entry.synonyms:
            lines.append("- **同**")
            for syn in entry.synonyms:
                text = _md_inline(_flatten(syn.word))
                if syn.gloss_zh:
                    text += f"（{_md_inline(_flatten(syn.gloss_zh))}）"
                if syn.diff_note:
                    text += f" — {_md_inline(_flatten(syn.diff_note))}"
                lines.append(f"  - {text}")
        if entry.collocations:
            lines.append("- **搭**")
            for coll in entry.collocations:
                text = _md_inline(_flatten(coll.phrase))
                if coll.gloss_zh:
                    text += f"（{_md_inline(_flatten(coll.gloss_zh))}）"
                lines.append(f"  - {text}")
                if coll.example:
                    lines.append(f"    {_md_inline(_flatten(coll.example))}")
                    if coll.example_zh:
                        lines.append(f"    {_md_inline(_flatten(coll.example_zh))}")
        note = _note_lines(entry.notes)
        if note:
            lines.append(f"- **注** {_md_inline(note[0])}")
            lines.extend(
                f"  {_md_note_line(line)}" if line.strip() else "" for line in note[1:]
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

# 列顺序一经发布就是**对外接口**：用户的表格模板、脚本都按列位置取值。
# 只能往后追加，不要插入、不要重排。
CSV_COLUMNS = (
    "单词",
    "音标",
    "词性",
    "英文释义",
    "中文释义",
    "例句",
    "例句翻译",
    "同义语",
    "备注",
    "标签",
    # 「固定搭配」是在这一版**追加**到末尾的。它只能加在最后：列顺序是对外
    # 接口，插在中间会让所有按列位置取值的表格模板与脚本静默错位。
    "固定搭配",
)

# 扁平表里装不下嵌套结构，同义语三个字段合成一格。分隔符用 `; ` 而不是逗号：
# 逗号在释义里太常见（`to do sth, to do sth else`），拿它当分隔符会把一格又劈开。
SYN_SEP = "; "

# UTF-8 的 BOM。作为返回字符串的首字符，使调用方用最普通的 `utf-8` 编码
# 写盘就得到 Excel 能正确识别的文件（理由见 export_csv 的说明）。
CSV_BOM = "\ufeff"


def _synonym_cell(entry: Entry) -> str:
    """把一个词条的同义语压成一格。

    形如 `pervasive（弥漫的） — 更强调渗透`。刻意保留中文简释与差异说明——
    只留单词会退化成裸词列表，那是本项目明确不接受的形态（见产品约定）。
    """
    parts: list[str] = []
    for syn in entry.synonyms:
        text = _flatten(syn.word)
        if not text:
            continue
        if syn.gloss_zh:
            text += f"（{_flatten(syn.gloss_zh)}）"
        if syn.diff_note:
            text += f" — {_flatten(syn.diff_note)}"
        parts.append(text)
    return SYN_SEP.join(parts)


def _collocation_cell(entry: Entry) -> str:
    """把一个词条的固定搭配压成一格。

    形如 `be in charge of（负责） — She is in charge of it.／她负责这件事。`。

    例句必须带上：搭配的价值一大半在「它怎么用」上，只导出词组列表等于把这一格
    的价值砍掉一半，而用户看不出少了什么。例句与翻译之间用**全角斜杠**分隔——
    半角 `/` 在英文释义里太常见（`either/or`），拿它当分隔符会把一格又读成两段。
    """
    parts: list[str] = []
    for coll in entry.collocations:
        text = _flatten(coll.phrase)
        if not text:
            continue
        if coll.gloss_zh:
            text += f"（{_flatten(coll.gloss_zh)}）"
        if coll.example:
            text += f" — {_flatten(coll.example)}"
            if coll.example_zh:
                text += f"／{_flatten(coll.example_zh)}"
        parts.append(text)
    return SYN_SEP.join(parts)


def export_csv(entries: list[Entry], title: str = "英语生词本") -> str:
    """生成 CSV 文本，**首字符是 BOM**（U+FEFF）。

    编码是这里唯一真正重要的决定。**必须带 BOM**：不带的话，中文 Windows 上的
    Excel 会按系统 ANSI 码页（GBK）打开，整个文件的中文变成乱码；而 LibreOffice、
    VS Code、pandas 对 BOM 都能正确处理。为「一处兼容」付出的代价只是三个字节。

    为什么把 BOM 放在**返回值里**而不是让调用方编码时用 `utf-8-sig`：
    `utf-8-sig` 是一个容易漏掉的编码名，漏了不会报错、只会在别人的 Excel 里
    表现为乱码——而那一刻没有任何人能追到这个决定。放进返回值，则**只要按
    最普通的 `utf-8` 写盘就是对的**，无从漏起。代价是调用方若把返回值打印到
    终端，开头会有一个零宽字符（不可见，无害）。

    行尾用 CRLF（RFC 4180），Excel 与各类解析器都认；表格类工具对 LF 的容忍度
    不如对 CRLF 统一。

    这条路径是**有损**的：同义语压成一格、用户备注的多行被折成一行。要保留版式
    请用纯文本或 Markdown 导出。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                _flatten(entry.lemma),
                _flatten(entry.phonetic),
                _flatten(entry.pos),
                _flatten(entry.definition_en),
                _flatten(entry.definition_zh),
                _flatten(entry.example),
                _flatten(entry.example_zh),
                _synonym_cell(entry),
                _flatten(entry.notes),
                _flatten(entry.tags),
                _collocation_cell(entry),
            ]
        )
    return CSV_BOM + buf.getvalue()


# ---------------------------------------------------------------------------
# Anki
# ---------------------------------------------------------------------------

# Anki 自己的纯文本导入格式：制表符分列，`#` 开头的行是给它的指令。
#
# 三条指令都不是装饰。**不写分隔符指令时**，Anki 默认按逗号切列，而中文释义
# 里逗号极常见——用户按默认值导入一次，一列释义就被劈成好几列，他多半会
# 以为是文件坏了。写上之后 Anki 自己选对分隔符，这条路才真的「打开就能导」。
#
# `#html:true` 让背面里的 `<br>` 被渲染成换行。没有它，用户会在一张卡上看到
# 一堆字面的 `<br>`。
_ANKI_HEADER = ("#separator:tab", "#html:true", "#tags column:3")

# 正面 / 背面 / 标签。列数在这里定一次，说明文案与导出函数都从它派生。
ANKI_COLUMNS = ("正面", "背面", "标签")


def _anki_cell(text: str) -> str:
    """一格内容。**制表符与换行必须先被消掉**——它们正是分隔符本身。

    漏掉这一步的后果不是「格式不整齐」：一个含制表符的释义会让整行的列数错位，
    它后面的每一个字段都会串到别人那一列上，而 Anki 不会报任何错。
    `_flatten()` 把任意空白（含 tab 与换行）折成单个空格，正好是这个用途。
    """
    return _flatten(text)


def _anki_back(entry: Entry) -> str:
    """卡片背面：一行一事，用 `<br>` 断开。

    刻意**不带任何颜色与字号**：卡片长什么样该由 Anki 的模板决定，写进去的
    样式会在用户换模板时变成一对多余的覆盖。这里只表达结构（`<b>` 与 `<br>`）。

    备注是唯一保留换行的字段（用户有意分段），因此它的换行转成 `<br>` 而不是
    被折成空格——与纯文本导出那条决定同源。因为开了 `#html:true`，笔记里的
    `&` `<` `>` 必须先转义，否则「read < write」会被当成标签吃掉半截。
    """
    rows: list[str] = []
    if entry.phonetic:
        rows.append(f"<i>{_html_escape(_anki_cell(entry.phonetic))}</i>")
    if entry.pos:
        rows.append(_html_escape(_anki_cell(entry.pos)))
    if entry.definition_zh:
        rows.append(f"<b>{_html_escape(_anki_cell(entry.definition_zh))}</b>")
    if entry.definition_en:
        rows.append(_html_escape(_anki_cell(entry.definition_en)))
    if entry.example:
        rows.append(f"<br>{_html_escape(_anki_cell(entry.example))}")
        if entry.example_zh:
            rows.append(_html_escape(_anki_cell(entry.example_zh)))
    for syn in entry.synonyms:
        text = _html_escape(_anki_cell(syn.word))
        if syn.gloss_zh:
            text += f"（{_html_escape(_anki_cell(syn.gloss_zh))}）"
        if syn.diff_note:
            text += f" — {_html_escape(_anki_cell(syn.diff_note))}"
        rows.append(f"<br>{text}")
    # 固定搭配进背面。**这是对「三列契约」之内的一次扩张，不是破坏**：那三列
    # 说的是「正面 / 背面 / 标签」，背面里放什么由这张卡自己决定。搭配又恰恰是
    # 最该背的部分（知道一个词的意思 ≠ 会用它的固定说法），所以宁可让卡片变长。
    # 例句用斜体排一行、翻译紧跟一行，与音标/释义的层次区分开。
    for coll in entry.collocations:
        text = _html_escape(_anki_cell(coll.phrase))
        if coll.gloss_zh:
            text += f"（{_html_escape(_anki_cell(coll.gloss_zh))}）"
        rows.append(f"<br>{text}")
        if coll.example:
            rows.append(f"<br><i>{_html_escape(_anki_cell(coll.example))}</i>")
            if coll.example_zh:
                rows.append(f"<br>{_html_escape(_anki_cell(coll.example_zh))}")
    note = _note_lines(entry.notes)
    if note:
        note_html = "<br>".join(_html_escape(line) for line in note)
        rows.append(f"<br><br>{note_html}")
    return "<br>".join(rows)


def export_anki(entries: list[Entry], title: str = "英语生词本") -> str:
    """生成 Anki 可直接导入的制表符分隔文本。

    **为什么是 txt 而不是 `.apkg`**：Anki 自己就能导入这种文本（选文件即可），
    而 `.apkg` 要维护一份 Anki 的内部结构（sqlite + zip + 模型与模板定义），
    成本高一个数量级，用户得到的却几乎一样。这条路的价值在于「今天就能把生词
    接进记忆流程」，先把便宜的那版做出来。

    **刻意不带 BOM**：CSV 带 BOM 是为了让 Excel 认出编码，而 Anki 会把 BOM
    当成第一个字段的第一个字符——第一张卡片的正面会多出一个看不见的零宽字符，
    用户在卡片上对比半天也找不出为什么这个词「和别的不一样」。

    `title` 参数与其他导出函数对齐，但在 Anki 格式里**没有任何位置可放**：
    多一行标题就多一张卡片。它被刻意忽略，而不是塞进标签列——那会污染用户
    的标签体系。调用方仍按同一套签名调它，因此这条差异不构成特例。
    """
    lines: list[str] = list(_ANKI_HEADER)
    for entry in entries:
        lemma = _anki_cell(entry.lemma)
        if not lemma:
            continue
        lines.append("\t".join((lemma, _anki_back(entry), _anki_cell(entry.tags))))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

# 容器标识。导入侧据此认格式——只验「是 JSON」是不够的：任何 JSON 文件都会通过，
# 于是别人的配置文件也会被当成生词本读。
JSON_FORMAT_TAG = "quill-entries"
# 结构版本。**没有它，第一次改结构就变成「旧文件读不了」而说不出原因。**
# 与 OFFLINE_SCHEMA_VERSION 的教训同源：格式文件必须能自报家门。
#
# **加了 `collocations` 字段但没有递增它**，这是有意的：新增一个**可缺省**的
# 字段不改变「怎么读这份文件」——旧文件没有这个键，读进来就是「这个词条没有
# 搭配」；新文件被旧版本的程序读到时，导入侧的字段白名单也只会忽略它。
# 版本号留给**破坏性**变化（改类型、改容器形状、改已有字段的含义）。
JSON_VERSION = 1

# 词条内容字段：跨机器有意义，参与导出与（将来的）回填。
JSON_ENTRY_FIELDS = (
    "lemma",
    "phonetic",
    "pos",
    "definition_en",
    "definition_zh",
    "example",
    "example_zh",
    "synonyms",
    "collocations",
    "notes",
    "tags",
    "source",
    "prompt_version",
)

# 值是「对象列表」的字段：导出时要逐项 `to_dict()`，读回来时逐项 `from_raw()`。
# 集中成一张表，而不是在导出/导入两处各写一遍 `if name == "synonyms"`——
# 那种写法在加第二个嵌套字段时必然漏掉一处，而漏掉的表现是「这个字段静默不导出」。
JSON_NESTED_FIELDS = ("synonyms", "collocations")
# 刻意**排除**的字段，理由分两类：
#   id / created_at / updated_at —— 本机主键与本地历史。换台机器 id 就是别人的
#     行号，写回去要么撞主键要么覆盖别人；时间戳描述的是「本机何时录入」，
#     搬走之后是错的。
#   prompt_stale / fallback_reason —— 运行时字段，由本次会话的配置与网络状况算出来。
#     存进文件、下次读回来就是一条陈旧且可能与当下相反的结论。
JSON_EXCLUDED_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "prompt_stale",
    "fallback_reason",
    "aliases",
    # 复习进度是本机学习历史（due 是「本机何时该复习」的时间戳，搬走即错），
    # 与 created_at 同一类——导出只搬「词条内容」，不搬「学到哪一步」。
    "review_due",
    "review_count",
    "review_interval",
    "review_ease",
)


def entry_to_json_dict(entry: Entry) -> dict:
    """把一个词条转成可跨机器迁移的字典。

    字段顺序跟 `JSON_ENTRY_FIELDS` 一致（Python dict 保序），这样导出的文件
    在 diff 里是稳定的，不会因为字段顺序随机抖动而整片标红。
    """
    out: dict = {}
    for name in JSON_ENTRY_FIELDS:
        value = getattr(entry, name)
        if name in JSON_NESTED_FIELDS:
            out[name] = [item.to_dict() for item in value]
        else:
            out[name] = value
    return out


def export_json(entries: list[Entry], title: str = "英语生词本") -> str:
    """生成 JSON 文本。

    `ensure_ascii=False`：中文保持可读。转义成 `\\u65e0` 之后，这个文件的
    「人也能打开看一眼」的价值就没了，而字节数还更大。

    顶层是对象而非裸数组：数组塞不下「这是什么格式、什么版本」这两个必须
    自报的信息。
    """
    payload = {
        "format": JSON_FORMAT_TAG,
        "version": JSON_VERSION,
        "title": title,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(entries),
        "entries": [entry_to_json_dict(e) for e in entries],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

# PDF 用 PostScript 点（pt）作单位，1pt = 1/72 英寸。字号直接用 pt 指定，
# 由 `QTextDocument.print_()` 换算到设备坐标——**不要自己 scale**：
# 实测按 DPI 手工缩放会让页面坐标系与文档坐标系各算一套，内容被画到页面之外
# （只剩第一段可见）。`print_()` 接受 QPagedPaintDevice（QPdfWriter 即是），
# 分页、DPI 换算、页面尺寸全由 Qt 处理，实测 200 条正确分成 18 页。
PDF_DPI = 300
PDF_PAGE = QPageSize(QPageSize.A4)
PDF_MARGIN_MM = 15

# 版式用的字号与间距（pt）。
#
# **不能用 `border` / `background` 做卡片框**：`QTextDocument` 在 PDF 输出路径
# 下完全不渲染这两类属性（实测：`border:1pt solid #999` 的 table、`border` 的 div
# 在 PDF 里一律看不见，而同样的 HTML 渲染到 widget 时是有框的）。用户看到的会是
# 「说好是卡片式，结果一个框都没有」。
#
# 改用本项目一贯的版式语言：**细横线分隔 + 单字标签缩进**（纯文本导出即此做法）。
# 这个组合一定渲染得出来，且与其它几种导出格式观感一致。
_PDF_TITLE_PT = 18
_PDF_META_PT = 9
_PDF_LEMMA_PT = 13
_PDF_BODY_PT = 10
# 词条之间的分隔线颜色。比标题浅、比正文淡，起分组作用而不抢视线。
_PDF_RULE_COLOR = "#C9C9C9"
_PDF_TITLE_COLOR = "#1F4E79"
_PDF_META_COLOR = "#888888"


def _pdf_entry_html(index: int, entry: Entry) -> str:
    """一个词条渲染成一段 HTML（QTextDocument 的富文本子集）。

    用 HTML 而非手工画线，是因为 `QTextDocument` 自带换行与中英混排；手工排版
    要在每个字段上算宽度，而中文的宽度规则正是本项目一开始就决定不去碰的东西。

    能用的排版手段只有：字号、字重、颜色、`<br>`、缩进（全角空格）、简单
    `margin`。**不能用**：`border`、`background`、`border-radius`、`padding`
    （理由见上面的常量注释）。
    """
    head = f"{index}. {_html_escape(_flatten(entry.lemma))}"
    if entry.phonetic:
        head += (
            f' <span style="font-size:{_PDF_META_PT}pt;color:{_PDF_META_COLOR};">'
            f"{_html_escape(_flatten(entry.phonetic))}</span>"
        )
    if entry.pos:
        head += (
            f' <span style="font-size:{_PDF_META_PT}pt;color:{_PDF_META_COLOR};">'
            f"{_html_escape(_flatten(entry.pos))}</span>"
        )

    rows: list[str] = []
    if entry.definition_en:
        rows.append(f"英　{_html_escape(_flatten(entry.definition_en))}")
    if entry.definition_zh:
        rows.append(f"中　{_html_escape(_flatten(entry.definition_zh))}")
    if entry.example:
        rows.append(f"例　{_html_escape(_flatten(entry.example))}")
    if entry.example_zh:
        rows.append(f"　　{_html_escape(_flatten(entry.example_zh))}")
    for i, syn in enumerate(entry.synonyms):
        text = _html_escape(_flatten(syn.word))
        if syn.gloss_zh:
            text += f"（{_html_escape(_flatten(syn.gloss_zh))}）"
        if syn.diff_note:
            text += f" — {_html_escape(_flatten(syn.diff_note))}"
        rows.append(f"{'同' if i == 0 else '　'}　{text}")
    for i, coll in enumerate(entry.collocations):
        text = _html_escape(_flatten(coll.phrase))
        if coll.gloss_zh:
            text += f"（{_html_escape(_flatten(coll.gloss_zh))}）"
        rows.append(f"{'搭' if i == 0 else '　'}　{text}")
        # 例句比搭配再缩一格（两个全角空格），层级一眼看得出来
        if coll.example:
            rows.append(f"　　　{_html_escape(_flatten(coll.example))}")
            if coll.example_zh:
                rows.append(f"　　　{_html_escape(_flatten(coll.example_zh))}")
    note = _note_lines(entry.notes)
    if note:
        # 备注是用户手写的，**保留换行**（与文本导出同一决定）；用 <br> 而非
        # 折叠成空格，否则记忆法的分条会糊成一段。
        note_html = "<br>".join(_html_escape(line) for line in note)
        rows.append(f"注　{note_html}")

    body = (
        f'<div style="font-size:{_PDF_BODY_PT}pt;margin-top:4pt;">'
        + "<br>".join(rows)
        + "</div>"
        if rows
        else ""
    )
    return (
        f'<div style="font-size:{_PDF_LEMMA_PT}pt;font-weight:bold;'
        f'color:{_PDF_TITLE_COLOR};">{head}</div>{body}'
    )


def build_pdf_html(entries: list[Entry], title: str = "英语生词本") -> str:
    """拼出整份 PDF 的 HTML。

    单独提出来有两个用途：不起绘图器就能验内容是否真的进了文档；以及让
    「排版手段受限于 QTextDocument 子集」这件事集中在一个函数里。
    """
    head = [
        f'<div style="font-size:{_PDF_TITLE_PT}pt;font-weight:bold;'
        f'color:{_PDF_TITLE_COLOR};">{_html_escape(title)}</div>',
        f'<div style="font-size:{_PDF_META_PT}pt;color:{_PDF_META_COLOR};'
        f'margin-top:2pt;">导出时间 {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        f"　共 {len(entries)} 条</div>",
        f'<div style="font-size:{_PDF_META_PT}pt;color:{_PDF_META_COLOR};'
        f'margin-bottom:10pt;">Generated by Quill</div>',
    ]
    parts: list[str] = list(head)
    for i, entry in enumerate(entries, start=1):
        if i > 1:
            # 词条之间用细横线分隔。`<hr>` 是 QTextDocument 一定能渲染的元素，
            # 这也是本项目纯文本导出用的手法（那用的是字符横线，同意图）。
            parts.append('<hr style="border:none;border-top:0.5pt solid '
                         f'{_PDF_RULE_COLOR};margin-top:8pt;margin-bottom:8pt;">')
        parts.append(_pdf_entry_html(i, entry))
    return "".join(parts)


class PdfAppUnavailable(RuntimeError):
    """当前进程已有一个非 GUI 的 Qt 应用，无法生成 PDF。

    单独定义而不是抛 RuntimeError，是因为调用方需要的处置方式不同：这不是
    「参数写错了」，而是「这个进程从根上就不该走到这里」。
    """


def _ensure_gui_app() -> None:
    """保证存在一个 QGuiApplication 实例，否则抛出明确错误。

    **没有 GUI 应用时，`QTextDocument()` 会让整个进程当场消失**——不是抛异常，
    是 rc=127、零输出、无 traceback 的硬崩。实测踩过两次，两次都不是代码逻辑
    写错，而是「Qt 在缺字体引擎时的失败方式根本不给 Python 层任何机会」：

      1. 自检脚本里没有任何 Qt 应用，`export_pdf` 直接让脚本静默退出；
      2. 自检的另一段为了测后台投递建了 `QCoreApplication`（不是 GUI 版），
         此后再调 `export_pdf` 同样静默崩 —— 而此时 `QGuiApplication.instance()`
         并**不**为 None（它返回的正是那个 QCoreApplication，因为 QGuiApplication
         是 QCoreApplication 的子类），所以「判空再建」的写法在这里完全失效。

    PDF 排版要解析字体，字体引擎挂在 GUI 应用上。纯 `QCoreApplication` 只是
    事件循环，没有字体能力——**Qt 不允许把一个已存在的 QCoreApplication 升级成
    QGuiApplication**，所以此情形下无解，只能明确报错。

    报错而不是静默崩，是本模块对「失败必须可诊断」的贯彻：崩掉的进程连
    「是导出功能出了问题」都传不出去，用户只会看到程序莫名其妙地关闭。

    **刻意不设 `QT_QPA_PLATFORM`。** 曾经为了「在没有显示的环境里也能跑」而
    `setdefault("QT_QPA_PLATFORM", "offscreen")`，代价是实测出来的：offscreen
    与 minimal 平台的字体族数量是 **0**，所有字符真的被画成缺字方块（tofu），
    PDF 里既不嵌字体也没有 CID 编码，而文件本身生成得好好的。宁可要求有字体，
    也不要产出一份通篇方块的 PDF——后者会被当成正常文件发出去。
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication

    existing = QCoreApplication.instance()
    if existing is not None:
        # 注意判据是「不是 QGuiApplication」而非「instance() 不为 None」：
        # QGuiApplication.instance() 在只有 QCoreApplication 时返回后者，
        # 拿它判空会误判成「已经有 GUI 应用了」。
        if not isinstance(existing, QGuiApplication):
            raise PdfAppUnavailable(
                "当前进程已创建一个非 GUI 的 Qt 应用（QCoreApplication），"
                "无法在其中生成 PDF：Qt 不允许把已有的 QCoreApplication "
                "升级为 QGuiApplication，而 PDF 排版需要 GUI 应用提供的字体引擎。"
                "请让创建方改用 QGuiApplication（PDF 不需要窗口系统，"
                "QGuiApplication 足够）。"
            )
        return

    # 引用必须留住：QGuiApplication 一旦被回收，进程再碰 Qt 就会崩。
    global _GUI_APP
    _GUI_APP = QGuiApplication([])


class PdfFontUnavailable(RuntimeError):
    """当前环境没有任何可用字体，生成的 PDF 会通篇是缺字方块。"""


def _ensure_fonts() -> None:
    """确认环境里真的有字体，否则明确报错。

    **缺字体不会报错，只会让每个字变成方块。** 实测：offscreen 与 minimal 平台的
    `QFontDatabase.families()` 返回空列表，生成的 PDF 里 `/BaseFont`、`/Type0`、
    `/ToUnicode` 一律缺席，而文件大小、页数、能否打开全都正常。这种产物最危险——
    它看起来完全成功，只有人眼盯着看才发现通篇是方块。

    所以宁可在这里失败得响亮一点。用户运行本程序时是有桌面的，字体从来都在；
    只有无头的自检/CI 环境会走到这个分支。
    """
    from PySide6.QtGui import QFontDatabase

    families = QFontDatabase.families()
    if not families:
        raise PdfFontUnavailable(
            "当前环境没有任何可用字体（QFontDatabase 返回空），"
            "生成的 PDF 会把每个字都画成缺字方块，因此拒绝输出。"
            "请在有桌面会话的环境下运行，或显式指定一个可用的字体平台"
            "（注意 QT_QPA_PLATFORM=offscreen / minimal 都不加载系统字体）。"
        )


def export_pdf(entries: list[Entry], path, title: str = "英语生词本") -> None:
    """把词条写成带分隔线的 PDF，直接落盘。

    与另外几个导出函数不同，这里**写文件而不是返回字符串**：PDF 是二进制容器，
    没有任何调用方需要拿到它的字节内容去拼接。

    落盘前会拒绝两种「产得出来但没法用」的情况，各自抛明确异常而非静默出产物：
    `PdfAppUnavailable`（进程里已有一个非 GUI 的 Qt 应用）、
    `PdfFontUnavailable`（环境没有任何字体，出来的会是通篇方块）。这两种情况
    共同点是**产物本身完全正常**——能打开、有页数、文件大小也合理，只有人眼
    盯着看才发现不对。

    排版走 `QTextDocument.print_(writer)`，而不是自己开 `QPainter` 手工缩放。
    实测对比：手工 `painter.scale(DPI/72)` + `drawContents()` 只画出文档开头
    一小段就停住（200 条只得到 1 页），而 `print_()` 交给 Qt 处理分页与设备
    坐标换算，同样 200 条正确得到 18 页。
    """
    _ensure_gui_app()
    _ensure_fonts()

    writer = QPdfWriter(str(path))
    writer.setPageSize(PDF_PAGE)
    writer.setResolution(PDF_DPI)
    writer.setPageMargins(
        QMarginsF(PDF_MARGIN_MM, PDF_MARGIN_MM, PDF_MARGIN_MM, PDF_MARGIN_MM),
        QPageLayout.Millimeter,
    )
    writer.setTitle(title)
    writer.setCreator("Quill")

    doc = QTextDocument()
    # 文档自带 4pt 边距；页面边距已经由 writer 设过，再留一份会双重缩进。
    doc.setDocumentMargin(0)
    doc.setHtml(build_pdf_html(entries, title))
    # `print_` 接受 QPagedPaintDevice；QPdfWriter 即是，分页由它自己完成。
    doc.print_(writer)


