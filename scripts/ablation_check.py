"""消融实验：逐项抽走功能，判断它到底有没有价值。

## 为什么需要这个

变异测试（mutation_check.py）守的是「保护会不会被破坏」，它的变异都是
**把某道防线改坏**。消融实验问的是另一个问题：

    **这个功能如果整个不存在，会有人察觉吗？**

两者的差别很关键。一道防线可以「破坏它有人报警」，但仍然只是
锦上添花；而一个「抽走它测试全绿、界面也看不出差别」的功能，
是**在欺骗用户**——文档里写着它存在，用户以为它在起作用。

## 判据（三项都要看）

    1. 测试是否报红   —— 抽走后 selfcheck / gui_smoke 有没有失败
    2. 用户可见行为   —— 用真实函数算出「用户会看到什么」，对比抽走前后
    3. 是否静默错误   —— 抽走后是「明确失败」还是「悄悄写错数据」

只满足第 2 项（界面变了但测试不红）说明**测试没覆盖到用户可见的东西**，
是测试的缺口，不是功能的胜利。三项全不满足则是**无效功能**。

## 纪律

每项消融后必须逐字节还原。本脚本在结束时核对全部文件的 sha256，
任何一处没回到原样都算实验失败（退出码 2）——否则后续结论都建立在
一个被改坏的工作区上，那比不做实验更危险。

但结尾核对**只在跑到终点时有效**。被强杀（SIGTERM / SIGKILL）时它同样
不执行，抽掉的那段就留在源码里，且**长得完全像一段正常代码**。因此另外
补了三件：启动时先自证残留检测器、再查残留、并注册信号处理器即时还原。
判据只用「锚点还在不在」——详见 `check_no_leftovers` 的注释。
"""

from __future__ import annotations

import ast
import gc
import glob
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# 临时目录前缀表**只留一份定义**（`clean_temp.py`）。这里曾经各写一份：探针的
# 清理只认 `quill_ablate_*`，于是 selfcheck / gui_smoke / lock_check 这三个
# 子套件崩溃时漏下的 `quill_check_*` / `quill_lock_*` 完全不在它的视野里
# ——而「判据覆盖的目标恰好是被漏掉的那一类」正是本项目反复踩到的形态。
sys.path.insert(0, str(ROOT / "scripts"))
from clean_temp import PREFIXES as TEMP_PREFIXES  # noqa: E402

# ---------------------------------------------------------------- 消融项定义
#
# 每项：(标识, 说明, 目标文件, 原文, 替换文, 该功能「用户可见」的探测表达式)
#
# 探测表达式在一段独立的子进程里执行，返回一行文本——代表「用户会看到
# 什么」。用它对比抽走前后，比读代码可靠：读代码只能看出「这个 if 变了」，
# 而用户感知到的是它算出来的结果。

def _read_line(rel: str, needle: str, keep_nl: bool = False, lines: int = 1) -> str:
    """取出真实文件里含 needle 的那一行（含缩进）。用于构造锚点。

    有些锚点含反斜杠与引号（正则、`property("selected")`），手写极易差一
    层转义——实测为此返工两次。从真实文件里取，就不存在转错的余地。

    `lines > 1` 时连后面几行一起取。**这不是为了方便，是为了正确**：删掉一个
    `if` 头却把它的 `return` 体留在原处，文件直接 `IndentationError`——见
    `_A11_OLD` 那段说明。
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    i = src.find(needle)
    if i == -1:
        raise RuntimeError(f"未找到 {needle!r} in {rel}")
    start = src.rfind("\n", 0, i) + 1
    end = src.find("\n", i)
    for _ in range(lines - 1):
        end = src.find("\n", end + 1)
    line = src[start:end]
    return line + "\n" if keep_nl else line


# 这两处的锚点直接从真实文件里取，不手写。
_A8_OLD = _read_line("app/core/exporter.py", "_MD_INLINE_RE.sub")
_A8_NEW = '    return text or ""'
#
# A11 的锚点必须**连下一行一起取**（`lines=2`）。
#
# 消融目的是让 `set_selected` 变成空操作（卡片再也不画选中态）。但只把那行
# `if` 换成 `return` 的话，紧跟其后的 `return`（12 空格）会孤零零留在原处 →
# `IndentationError` → 三个套件在**导入阶段就死**，报表写着「整套崩溃」+「已抓住」，
# 看着像「这项功能承重」，其实是**消融自己把源码改坏了**，它要验的那条断言根本
# 没轮到出场。本环境实际发生过（本轮加了 `ast.parse` 守卫才揪出来）。
_A11_OLD = _read_line("app/ui/widgets.py", 'self.property("selected")', keep_nl=True, lines=2)
_A11_NEW = "        return\n"
#
# 面板半透明规则**由档位表生成**（见 theme._veil_rules），源文件里已经没有那一行
# 字面量了，所以消融点跟着挪到档位表上：把默认档（medium）改成 100%，面板就等于
# 完全不透明，图片只剩页边距能看见——与「把那条规则整行删掉」是同一个用户可见
# 后果。**消融要盯的是用户看到什么，不是哪一行代码**，规则搬到别处时跟着搬锚点，
# 而不是把这条消融删掉。
_A15_OLD = '_VEIL_LIGHT = {"soft": 88, "medium": 72, "bold": 58}\n'
_A15_NEW = '_VEIL_LIGHT = {"soft": 88, "medium": 100, "bold": 58}  # 消融：默认档不透明\n'

# A25 的锚点从真实文件里取。原先手写的是一行 `self.btn_undo.setVisible(True)`，
# 而 Round 28 新增的 `_delete_phrase`（词组删除）**原样复制了这一行**——孤零零一行
# 锚点当场变成「命中 2 次」。消融在锚点不唯一时只是打一句 `[SKIP]`，报表上读起来
# 像「这项不必做」，而不是「检查坏了」。带上它上面那两行注释就唯一了。
_A25_OLD = _read_line(
    "app/ui/main_window.py", "# 「撤销」只在刚刚删过之后才有意义", keep_nl=True, lines=3
)
_A25_NEW = _A25_OLD.replace(
    "        self.btn_undo.setVisible(True)\n",
    "        pass  # 消融：删完不提供撤销\n",
)

# A27 的锚点同样从真实文件里取，理由更直白：那段落说明文字会跟着功能一起改
# （Round 28 就把「例句」改成了「例句与固定搭配」），手抄一遍等于给下一次改动
# 预埋一个失效锚点。取到的是**整段**，所以说明怎么写都不会让锚点落空。
_A27_OLD = "    (\n" + _read_line(
    "app/ui/export_dialog.py", '"anki",', keep_nl=True, lines=5
)


ABLATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "A1 离线兜底",
        "接口失败时回落到本机离线词库。抽走后接口一挂，用户就彻底查不到词",
        "app/providers/fallback.py",
        "class FallbackProvider",
        "class _Disabled_FallbackProvider",
    ),
    (
        "A2 中文分词检索",
        "中文查询转 bigram 后匹配 zh_bigram 列。抽走后中文子串一律搜不到",
        "app/core/repository.py",
        "        for seg in cjk_segs:\n            bg = to_bigram(seg)\n",
        "        for seg in cjk_segs:\n            bg = seg  # 消融：不做 bigram 转换\n",
    ),
    (
        "A3 缺项提示",
        "词条不完整时在详情区点名缺什么。抽走后用户拿到半截词条也没提示",
        "app/core/models.py",
        "    def missing_parts(",
        "    def _disabled_missing_parts(",
    ),
    (
        "A4 提示词陈旧标记",
        "prompts 版本比词条新时标 stale。抽走后用户不知道释义是旧版生成的",
        "app/core/dictionary.py",
        "    def mark_stale(",
        "    def _disabled_mark_stale(",
    ),
    (
        "A5 导入双路径识别",
        "认出本程序导出的文件并按块解析。抽走后释义行会被挖成假词条",
        "app/core/importer.py",
        "    if _looks_like_text_export(text):\n        return _parse_text_export(text)\n",
        "",
    ),
    (
        "A6 导入备注保留",
        "导入时不覆盖用户已有备注。抽走后用户的备注会被文件内容冲掉",
        "app/core/importer.py",
        "        if entry.id in known:\n",
        "        if False:\n",
    ),
    (
        "A7 句子整行跳过",
        "一行超过 6 个词且无分隔符时判为句子。抽走后整句会被收成词条",
        "app/core/importer.py",
        "MAX_WORDS_PER_LINE = 6",
        "MAX_WORDS_PER_LINE = 9999",
    ),
    (
        "A8 导出 Markdown 转义",
        "导出时转义 Markdown 元字符。抽走后备注里的 * 和 _ 会被渲染成强调",
        "app/core/exporter.py",
        _A8_OLD,
        _A8_NEW,
    ),
    (
        "A9 导入前核对框",
        "导入前先摆识别结果让用户确认。抽走后直接写库",
        "app/ui/main_window.py",
        "        if not self._confirm_import(path, parsed):\n            return\n",
        "",
    ),
    (
        "A10 删除确认框",
        "删除前弹确认。抽走后点删除直接销毁词条",
        "app/ui/main_window.py",
        # 锚点必须**整段**拿到「弹窗 + 按回答拦下」两件事。
        #
        # 原先只从 `QMessageBox.StandardButton.No,`（调用参数表里的最后一行）开始拿，
        # 到 `return` 结束——可 `question(...)` 的**闭合括号就在被删的片段里**，
        # 删完 `(` 就再也没人关，文件直接 `'(' was never closed`。报表写着
        # 「整套崩溃」+「已抓住」，看着像「这项功能承重」，其实是**消融自己把源码
        # 改坏了**，而那条「有没有按回答拦下」的断言根本没轮到出场。
        "        answer = QMessageBox.question(\n"
        "            self,\n"
        '            "删除词条",\n'
        '            f"确定要从生词本中删除「{entry.lemma}」吗？",\n'
        "            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,\n"
        "            QMessageBox.StandardButton.No,\n"
        "        )\n"
        "        if answer != QMessageBox.StandardButton.Yes:\n"
        "            return\n",
        "",
    ),
    (
        "A11 卡片选中态",
        "卡片自己画选中底色。抽走后列表选中不可见",
        "app/ui/widgets.py",
        _A11_OLD,
        _A11_NEW,
    ),
    (
        "A12 单实例锁",
        "启动时占锁文件防止开两个实例。抽走后可以开多个、互相覆盖数据",
        "app/main.py",
        "def acquire_instance_lock():",
        "def _disabled_acquire_instance_lock():",
    ),
    (
        "A13 背景图渲染",
        "窗口把图片画在底色之上。抽走后配了图也看不见，只剩页边距那圈底色",
        "app/ui/main_window.py",
        "        if not self._background.has_image:\n            return\n",
        "        return  # 消融：背景图不再绘制\n",
    ),
    (
        "A14 模糊蒙版开关",
        "按用户的勾选决定背景糊不糊。抽走后勾了也白勾，永远出原图",
        "app/ui/main_window.py",
        '        self._bg_blur = bool(self.config.get("background_blur", True))\n',
        "        self._bg_blur = False  # 消融：模糊蒙版永远不生效\n",
    ),
    (
        "A15 面板半透明让位",
        "配了背景图时面板转半透明，让图片透上来。抽走后图片只剩页边距能看见",
        "app/ui/theme.py",
        _A15_OLD,
        _A15_NEW,
    ),
    (
        "A16 子控件样式重算",
        "属性变了之后逐个重算子控件样式。抽走后后代选择器沿用旧缓存",
        "app/ui/main_window.py",
        "        for widget in [self, self.statusBar(), *self.findChildren(QWidget)]:\n",
        "        for widget in [self]:  # 消融：不重算子控件样式\n",
    ),
    (
        "A17 背景透出档位",
        "背景从面板后透出多少由用户选。抽走后三档长得一模一样，"
        "那个下拉框白做了——用户调它，画面纹丝不动",
        "app/ui/theme.py",
        "    return text if text in _VEIL_LIGHT else BG_VEIL_DEFAULT\n",
        "    return BG_VEIL_DEFAULT  # 消融：档位一律退回默认，选了等于没选\n",
    ),
    (
        "A18 主题换色",
        "换主题要把整套样式表换上。抽走后窗口记着新主题名、界面纹丝不动",
        "app/ui/main_window.py",
        "            app.setStyleSheet(stylesheet(resolved))\n",
        '            app.setStyleSheet(stylesheet("ink"))  # 消融：切主题不换样式表\n',
    ),
    (
        "A19 取消设置还原主题",
        "主题是即时生效的，取消时要把界面还原回存着的那套。抽走后"
        "「看到的」与「存下来的」不一致，重启又变回去",
        "app/ui/settings.py",
        "        if self._on_theme_preview is not None:\n"
        "            self._on_theme_preview(self._theme_original)\n"
        "        super().reject()\n",
        "        super().reject()  # 消融：取消时不还原主题\n",
    ),
    (
        "A20 拖入图片先问一句",
        "把图片拖进窗口要经用户同意才改配置。抽走后一拖就写进设置——"
        "用户可能只是想拖个东西进来看一眼",
        "app/ui/main_window.py",
        "            QMessageBox.StandardButton.Yes,\n"
        "        )\n"
        "        if answer != QMessageBox.StandardButton.Yes:\n"
        "            return\n",
        "            QMessageBox.StandardButton.Yes,\n"
        "        )\n"
        "        if answer == QMessageBox.StandardButton.Cancel:  # 消融：不按回答拦下\n"
        "            return\n",
    ),
    (
        "A21 最透档的文字加强色",
        "面板透到最狠时，直接压在它上面的小字要换一档更深的颜色。抽走后"
        "中等亮度的背景上只剩 1.7:1，小字与背景融在一起",
        "app/ui/theme.py",
        '    deepest = min(pal["veil"], key=lambda name: pal["veil"][name])\n',
        '    deepest = "nonexistent"  # 消融：加强色规则匹配不到任何档位\n',
    ),
    (
        "A22 记住最近用过的背景图",
        "用过的图要留个快捷入口。抽走后那个下拉框永远只有一句「还没有用过」",
        "app/ui/main_window.py",
        '        recent.insert(0, text)\n'
        '        self.config["background_recent"] = recent[:RECENT_BACKGROUNDS_MAX]\n',
        '        self.config["background_recent"] = recent  # 消融：不把这次用的图记进去\n',
    ),
    # ---- Round 27：标签入口 / 批量查词 / 撤销删除 / 多文件拖入 / Anki 导出 ----
    #
    # 这五项都不是「防护」而是「能力」，变异测试那一套对它们使不上劲——把一条
    # 能力改坏，多半只是让某个断言变红，而不是让它静默失效。它们真正要回答的
    # 是消融这个问题：**如果这个能力压根不存在，用户会察觉吗、还是白做了一场。**
    (
        "A23 输入框多词批量查词",
        "往输入框里粘一串生词，程序按分隔符拆开逐个收录。抽走后整串被当成"
        "**一个词**去查——得到的是一条 lemma 为整串的记录，不报错、不提示，"
        "用户以为 10 个词都收进去了",
        "app/ui/main_window.py",
        "        words = split_words(text)\n"
        "        if len(words) <= 1:\n"
        "            self._lookup_word(text)\n"
        "            return\n",
        "        self._lookup_word(text)  # 消融：不拆分，整串当一个词去查\n"
        "        return\n",
    ),
    (
        "A24 按标签筛选",
        "左栏下拉里能按标签收窄列表。抽走后下拉框照样列出全部标签，选了却"
        "一条都筛不动——「能点但不起作用」等同虚假选项",
        "app/ui/main_window.py",
        '        self._tag_filter = str(self.tag_filter.currentData() or "")\n',
        '        self._tag_filter = ""  # 消融：选了标签也不筛\n',
    ),
    (
        "A25 删除撤销",
        "删错一条能放回来（含备注与标签）。抽走后状态栏那个按钮永不出现，"
        "手滑一次只能靠重新查询找补——而接口内容回不来，用户自己写的那部分更是",
        "app/ui/main_window.py",
        _A25_OLD,
        _A25_NEW,
    ),
    (
        "A26 一次拖入多个文件",
        "拖进来的文件**一个都不丢**。抽走后只收第一个、其余两个无声消失"
        "（原来的实现就是这样，且没有任何提示）",
        "app/ui/main_window.py",
        "            if path.suffix.lower() in suffixes and path.is_file() and path not in out:\n"
        "                out.append(path)\n"
        "        return out\n",
        "            if path.suffix.lower() in suffixes and path.is_file() and path not in out:\n"
        "                out.append(path)\n"
        "        return out[:1]  # 消融：只留第一个\n",
    ),
    (
        "A27 Anki 制表符导出",
        "Anki 制表符导出",
        "把生词接进 Anki 的记忆流程。抽走后导出对话框少一种，"
        "想要的路由不存在——但其余六种**一切照常**，没有任何地方会提这件事",
        "app/ui/export_dialog.py",
        _A27_OLD,
        "",
    ),
    # ---- Round 28：固定搭配 / 词组收藏 ----
    #
    # 这两项与 A23–A27 一样是「能力」而不是「防护」。但它们各自还带一条更细微的
    # 取舍，那两条也单独列出来：**按词边界判**与**重复收藏不覆盖已有内容**——
    # 二者都不是新功能，而是「这个功能会不会悄悄给出错误答案」。
    (
        "A28 固定搭配与用法例句",
        "查词时右侧列出这个词的固定搭配，每条配它自己的例句。抽走后那一整块"
        "不再渲染，词条看上去「本来就没有搭配」——而不是「这次没取到」"
        "（详情面板对空内容是直接不渲染的，两种空白长得一模一样）",
        "app/core/models.py",
        '            c for c in (Collocation.from_raw(x) for x in _json_list(payload.get("collocations"))) if c\n',
        "            c for c in []  # 消融：不读接口返回里给的搭配\n",
    ),
    (
        "A29 词组收藏",
        "把一条搭配收进独立的「词组收藏」。抽走后按钮照样变成「已收藏」、状态栏"
        "照样报一句成功，而库里一条都没有——**界面说的和实际发生的相反**，"
        "用户不会再去点第二次",
        "app/ui/main_window.py",
        "            saved = self.repo.add_phrase(phrase)\n",
        "            saved = phrase  # 消融：点了「收藏」什么都不写\n",
    ),
    (
        "A30 按词边界判「含这个词」",
        "`run` 不该命中 `trunk` / `runway`。抽走后退回子串匹配，用户在这个词底下"
        "凭空看到一串不相干的词组，而界面上没有任何地方说明那是子串匹配的结果",
        "app/core/repository.py",
        "    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))\n",
        '    return " ".join(needle) in " ".join(haystack)  # 消融：退回子串匹配\n',
    ),
    (
        "A31 重复收藏不覆盖已有内容",
        "同一个词组从两个词条里收藏时，以**先收的那份**为准。抽走后第二次收藏会"
        "把第一次的简释与例句顶掉——用户手动改过的那条被静默改写，而两次操作"
        "在界面上都显示「已收藏」",
        "app/core/repository.py",
        '               ON CONFLICT DO NOTHING""",\n',
        "               ON CONFLICT (phrase) DO UPDATE SET gloss_zh=excluded.gloss_zh, "
        "example=excluded.example, example_zh=excluded.example_zh"
        '""",  # 消融：第二次收藏覆盖第一次\n',
    ),
    (
        "A32 撤销词组时切回词组收藏",
        "撤销一条词组要先让左栏回到「词组收藏」——`_reload_list(select_id=…)` 是"
        "在**当前集合**里找那个行号。抽走后状态栏照样打「已恢复词组「…」」，"
        "左栏却还停在生词本：恢复的那条看不见，而且同一个行号几乎总是真的存在，"
        "于是选中的是**另一张表里的同号数据**",
        "app/ui/main_window.py",
        "            if self._mode != MODE_PHRASES:\n"
        "                self._switch_mode(MODE_PHRASES)\n",
        "            pass  # 消融：不切回词组收藏集合，直接在当前集合里按行号选\n",
    ),
    (
        "A33 新词进入复习计划",
        "查词即收藏的同时，新词要被排进「待复习」——否则复习入口永远显示 0，"
        "复习功能等于白做。抽走后新词不再播种 review_due，待复习计数一直是 0",
        "app/core/repository.py",
        "                    if (entry.review_due or entry.review_count > 0)\n"
        "                    else now\n",
        "                    if (entry.review_due or entry.review_count > 0)\n"
        '                    else ""  # 消融：新词不再进入复习计划\n',
    ),
]


def _sha_tree() -> dict[str, str]:
    """当前工作区全部 .py 的哈希，用于核对还原。"""
    out: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in {".venv", "__pycache__", "_ecdict_src"} for part in rel.parts):
            continue
        # 只按**目录名**排除 build*/dist*。原写法拿 `path.parts` 全体去 startswith，
        # 连文件名一起看，于是 `scripts/build_offline_dict.py` 被静默排除在还原核对
        # 之外——而它正是变异锚点之一（它的换行解码被改过一次）。判据里多加的
        # 「顺带匹配」把整整一类对象挡在视野外，正是本项目反复踩过的那一脚：
        # 那不是更安全，而是另一只瞎眼。实测：改前覆盖 43 个（漏掉 `build.py` 与
        # `scripts/build_offline_dict.py`），改后 45 个。
        if any(part.startswith(("dist", "build")) for part in rel.parts[:-1]):
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ---------- 残留防护 ----------
#
# 本脚本会**就地改写源码**。被强杀（SIGTERM / SIGKILL）时 `finally` 不执行，
# 抽掉的那段就留在文件里——**它长得完全像一段正常代码**，下一次运行会拿这坨
# 污染当基线，得出的每个结论都不可信。这个坑在本项目的变异测试上真实发生过
# 一次（`_COERCERS` 里一行登记被留在删除状态，而残留检测器因为判据写窄了没
# 报出来）。所以这里补齐三件：检测残留、注册信号处理器还原、给检测器写自证。

_IN_FLIGHT: dict[str, bytes] = {}


def _restore_in_flight(*_args) -> None:
    """收到终止信号时，把正在被改写的文件立刻还原，再退出。"""
    for rel, raw in list(_IN_FLIGHT.items()):
        try:
            (ROOT / rel).write_bytes(raw)
        except OSError:
            pass
    _IN_FLIGHT.clear()
    raise SystemExit(143)


def check_no_leftovers() -> list[str]:
    """扫描源文件里是否残留了上一次运行抽走功能后的形态。

    **判据是「锚点不见了」**——干净的源码里每个锚点必然存在（应用消融时已
    校验它恰好命中 1 次），所以锚点缺席就说明文件被改过。不能掺「替换后的
    文本在不在」，更不能因为替换文本是空串就跳过：那会把**整类删除型**消融
    挡在视野之外，正是变异测试那边踩过的坑。
    """
    hits: list[str] = []
    for name, _why, rel, old, _new in ABLATIONS:
        if not old.strip():
            continue
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            src = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if src.count(old) == 0:
            hits.append(f"{rel} 残留了消融「{name}」")
    return hits


def selfcheck_leftover_detector() -> list[str]:
    """自证残留检测器不是瞎的：注入一次真残留，要求它报出来，再还原。

    两种形态都测——纯删除（`new` 为空串）与替换。只测一种，另一种的判据
    坏了就没人知道，而这次吃亏的恰恰是纯删除那一支。
    """
    problems: list[str] = []
    if check_no_leftovers():
        problems.append("干净状态下误报残留")

    def _pick(pred):
        for entry in ABLATIONS:
            if entry[3].strip() and pred(entry[3], entry[4]):
                return entry
        return None

    targets = [
        t
        for t in (
            _pick(lambda old, new: new.strip() == ""),
            _pick(lambda old, new: bool(new.strip())),
        )
        if t is not None
    ]

    for entry in targets:
        name, _why, rel, old, new = entry
        path = ROOT / rel
        raw = path.read_bytes()
        try:
            src = raw.decode("utf-8")
            if src.count(old) != 1:
                continue  # 锚点不唯一，主循环会报 SKIP
            path.write_bytes(src.replace(old, new, 1).encode("utf-8"))
            if not any(rel in hit for hit in check_no_leftovers()):
                problems.append(
                    f"注入残留后检测器没报出来（注入的是「{name}」）——"
                    "这条中止保护形同虚设"
                )
        finally:
            path.write_bytes(raw)

    if check_no_leftovers():
        problems.append("自证后未能还原干净")
    return problems


def _temp_dirs() -> set[str]:
    """系统临时目录里本套工具会创建的子进程目录。"""
    tmp = tempfile.gettempdir()
    found: set[str] = set()
    for pat in TEMP_PREFIXES:
        found.update(glob.glob(os.path.join(tmp, pat)))
    return found


def _sweep(dirs: set[str]) -> None:
    """删掉这些目录。重试后仍删不掉就**出声明**——静默失败正是那 2.6 GB 的成因。"""
    for leftover in sorted(dirs):
        for _ in range(3):
            gc.collect()
            try:
                shutil.rmtree(leftover)
                break
            except OSError:
                pass
        else:
            print(f"  WARN  子进程临时目录未能清理：{leftover}", flush=True)


# 子进程超时：**超时要变成一次可见的失败，而不是无限等待。**
#
# 这里原先没有 `timeout=`。探针一旦在 `offscreen` 下弹出模态欢迎页，进程就永不退出，
# `subprocess.run` 于是永远等下去——整条回归链静默卡死 2 小时 34 分，日志停在基线
# 那几行，看起来像「还在跑」。加上限之后，同类故障会在几分钟内以 `124` / `<探测超时>`
# 的形式浮出来，而不是烂在后台。
SUITE_TIMEOUT = 900     # 单套件（selfcheck / gui_smoke / lock_check）上限，秒
PROBE_TIMEOUT = 180     # 单个探针上限，秒


def _run_test(script: str) -> tuple[int, str]:
    """跑一个套件，并把它**这次新产生的**临时目录收掉。

    **这里原先有两个同名定义**（前一个不做临时目录清理），后一个把前一个盖掉了。
    那是最难查的一类重复：文件里两段都长得像在用，可只有一段会执行——改前一个
    的人会看到自己改的东西毫无效果，而且没有任何提示。已删掉没人用的那份。
    """
    before = _temp_dirs()
    try:
        res = subprocess.run(
            [PY, script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _sweep(_temp_dirs() - before)
        return 124, f"<套件超时：{script} 超过 {SUITE_TIMEOUT}s 未退出>"
    _sweep(_temp_dirs() - before)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def _probe(expr: str) -> str:
    """在独立子进程里跑一段探测代码，返回它打印的内容（代表用户所见）。

    **子进程的临时目录由父进程收尾。** 探针里写的是
    `atexit.register(shutil.rmtree, tmp, True)`，而 `True` 是 `ignore_errors`——
    子进程退出时那个 SQLite 连接还没释放（探针从不 `close()`），Windows 上打开的
    句柄会让 `rmtree` 直接失败，于是**静默**留下一个装着 `t.db` 的目录。
    实测一次消融跑完留下 15 个。这正是本项目真实发生过的那类泄漏：它不报错、
    不影响任何断言，只在累积到临界后让整个环境以毫不相干的方式崩掉
    （曾累积到 3763 个目录 / 2.6 GB，症状是系统盘写满、Bash 报 `ENOSPC`）。

    修法选在父进程而不是逐个改探针体：探针是十来段独立脚本，各自 `mkdtemp`，
    逐个去补 `close()` 既容易漏（新加的探针又会忘），又要动十几处。父进程只需
    在跑探针前后各拍一次快照，把**本次新出现的**目录删掉即可——这是唯一一处，
    而且对将来新增的探针自动生效。只删「新增」的，绝不碰别的运行留下的东西。
    """
    before = _temp_dirs()
    try:
        res = subprocess.run(
            [PY, "-c", expr],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _sweep(_temp_dirs() - before)
        head = expr.strip().splitlines()[0][:70]
        print(f"  WARN  探针超时（{PROBE_TIMEOUT}s 未退出）：{head}", flush=True)
        return f"<探测超时：{PROBE_TIMEOUT}s 未退出>"
    _sweep(_temp_dirs() - before)

    out = (res.stdout or "").strip()
    if res.returncode != 0:
        return f"<探测自身出错: {(res.stderr or '').strip().splitlines()[-1][:80]}>"
    return out


# 探针的公共纪律：**构造主窗口之前，先把欢迎页版本置成「已看过」。**
#
# 探针用全新的 `QUILL_HOME`（临时目录），配置里没有 `onboard_version`，默认 0。
# 主窗口构造时挂了 `QTimer.singleShot(0, _maybe_show_onboarding)`，探针里的
# `app.processEvents()` 一跑就弹出**模态**欢迎页并 `exec()` 等用户点击——而探针
# 跑在 `offscreen` 平台，没有人能点它。于是探针**永久阻塞**：不报错、不超时、
# 不退出，整条回归链就此停住（实测卡了 2 小时 34 分，日志停在基线那几行，只能
# 外部强杀）。欢迎页（M1）加入之后消融没再跑过，所以这个坑一直没暴露。
#
# `gui_smoke` 用的是同一招（构造主窗口前设 `config['onboard_version']`，见其
# `welcome_check`）；欢迎页本身另有专门的场景单独测，不靠这些探针来覆盖。
#
# A15 与 A16 的「用户所见」是同一个事实（面板透不透），所以共用一段探测。
#
# 两处代码不同、症状相同是**正常**的：A15 把那条半透明规则整行删掉，A16 是
# 规则还在但属性变更没广播到子控件、后代选择器沿用旧缓存。前者错在「没写」，
# 后者错在「写了没生效」——正是本项目真实踩过的那个坑，值得各留一条。
#
# 这里读的是**控件的解析后调色板**而不是屏幕像素：离屏平台不会把子控件的
# 半透明底色与父窗口画上去的图片做合成（读像素永远是纯白），调色板里的
# alpha 才是同一个事实的直接证据。
_BG_PANEL_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor, QPalette
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.theme import stylesheet
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
app.setStyleSheet(stylesheet())
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
img = QImage(200, 120, QImage.Format.Format_RGB32); img.fill(QColor(255, 0, 255))
p = tmp/'b.png'; img.save(str(p))
w.config['background_image'] = str(p); w.config['background_blur'] = False
w._apply_background(); w.update(); app.processEvents()
panel = w.detail.parentWidget()
col = panel.palette().color(QPalette.ColorRole.Window)
print(f'配了背景图后右侧面板底色={col.getRgb()}（alpha<255 才算透出图片）')
"""

# A17 的「用户所见」：把档位从最含蓄调到最明显，面板透出来的程度有没有跟着变。
# 抽掉档位后三档读数完全相同——用户看到的是「这个下拉框调了没用」。
_BG_VEIL_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor, QPalette
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.theme import stylesheet
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
app.setStyleSheet(stylesheet())
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
img = QImage(200, 120, QImage.Format.Format_RGB32); img.fill(QColor(255, 0, 255))
p = tmp/'b.png'; img.save(str(p))
w.config['background_image'] = str(p); w.config['background_blur'] = False
panel = w.detail.parentWidget()
seen = {}
for level in ('soft', 'medium', 'bold'):
    w.config['background_veil'] = level
    w._apply_background(); w.update(); app.processEvents()
    seen[level] = panel.palette().color(QPalette.ColorRole.Window).alpha()
print(f'含蓄/适中/明显三档的面板不透明度={seen}（三者相同＝档位没起作用）')
"""

# A18 的「用户所见」：切主题之后，主按钮的底色（也就是强调色）与窗口底色有没有变。
# 抽掉「真的把样式表换上」之后，无论切到哪一套，读出来的都是同一份——
# 用户看到的就是「那个下拉框调了没用」。
_THEME_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
seen = {}
for tid in ('ink', 'clay', 'night'):
    w._apply_theme(tid); w.update(); app.processEvents()
    seen[tid] = (
        w.btn_query.palette().color(QPalette.ColorRole.Button).name(),
        w.grab().toImage().pixelColor(8, 6).getRgb()[:3],
    )
print(f'三套主题下(主按钮底色, 窗口底色)={seen}（三项相同＝换主题没生效）')
"""

# A19 的「用户所见」：在设置里挑一套配色再按取消，界面该不该停在那套上。
# 抽掉还原之后，看到的与存下来的就不一致了——重启又变回去。
_THEME_CANCEL_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.main_window import MainWindow
from app.ui.settings import SettingsDialog
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
w._apply_theme('ink')
d = SettingsDialog(dict(w.config), '', w, on_theme_preview=w._apply_theme)
d.theme.setCurrentIndex(d.theme.findData('clay')); app.processEvents()
picked = w._theme
d.reject(); app.processEvents()
print(f'选中 clay 后={picked}，按取消后={w._theme}（取消后若不是 ink，就是没还原）')
"""

# A20 / A22 的「用户所见」：往窗口里拖一张图片，会发生什么。
_DROP_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor, QDropEvent
from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
import app.ui.main_window as mw
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
img = QImage(200, 120, QImage.Format.Format_RGB32); img.fill(QColor(255, 0, 255))
p = tmp/'drop.png'; img.save(str(p))

def drop():
    mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(p))])
    w.dropEvent(QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

real = mw.QMessageBox
class _No:
    StandardButton = real.StandardButton
    @staticmethod
    def question(*a, **k): return real.StandardButton.No
    warning = staticmethod(lambda *a, **k: None)
    information = staticmethod(lambda *a, **k: None)
class _Yes:
    StandardButton = real.StandardButton
    @staticmethod
    def question(*a, **k): return real.StandardButton.Yes
    warning = staticmethod(lambda *a, **k: None)
    information = staticmethod(lambda *a, **k: None)

w.config['background_image'] = ''; w.config['background_recent'] = []
mw.QMessageBox = _No
try:
    drop()
finally:
    mw.QMessageBox = real
# 这一步的结论必须在**下一次拖拽之前**取走：后面的「是」会把配置改掉，
# 放在最后取值读到的就永远是后一步的结果（实测踩过）。
no_kept = bool(w.config.get('background_image'))
mw.QMessageBox = _Yes
try:
    drop()
finally:
    mw.QMessageBox = real
print(f'点「否」后仍然配着背景图={no_kept}；'
      f'点「是」后最近使用条数={len(w.config.get("background_recent") or [])}')
"""

# A21 的「用户所见」：最透那一档下，直接压在面板上的小节文字读不读得清。
# 判据落在**颜色本身**上——「换了颜色」没有意义，换成同样读不清的颜色也算换过。
_DEEP_TEXT_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QImage, QColor, QPalette
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.core.models import Entry, Synonym
from app.providers.mock import MockProvider
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
img = QImage(200, 120, QImage.Format.Format_RGB32); img.fill(QColor(255, 0, 255))
p = tmp/'b.png'; img.save(str(p))
w.config['background_image'] = str(p); w.config['background_blur'] = False
w.detail.show_entry(Entry(lemma='x', definition_en='probe', definition_zh='探针',
                          synonyms=[Synonym(word='pervasive', gloss_zh='弥漫的')]))
app.processEvents()
seen = {}
for level in ('medium', 'bold'):
    w.config['background_veil'] = level
    w._apply_background(); w.update(); app.processEvents()
    for lab in w.detail.findChildren(QLabel):
        if lab.objectName() == 'section':
            seen[level] = lab.palette().color(QPalette.ColorRole.WindowText).name()
            break
print(f'适中/最透两档下小节文字的颜色={seen}（两档相同＝加强色没生效）')
"""


# 本轮四个探针（A23 / A24 / A25 / A26）都要一个「带窗口的离屏环境」，差别只在
# 最后几行。抽成一段共用，而不是各抄一遍：抄四遍的代价是将来改一处（多一个构造
# 参数、换个 provider）会有三处忘了跟着改，而它们**只在被抽走时才会跑一次**——
# 平时完全看不见，等到真跑消融那天才发现探针自己先炸了。
_MAIN_WINDOW_SETUP = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.core.models import Entry
from app.providers.mock import MockProvider
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
"""

# A23：往输入框粘一串词，看最终**库里有什么**——「收进 3 条」与「收进 1 条
# 毫无意义的记录」是用户唯一能察觉到的差别。
_A23_PROBE = _MAIN_WINDOW_SETUP + """
class _Picky:
    name = 'picky'
    def lookup(self, word):
        return Entry(lemma=word, definition_en='x', definition_zh=word + ' 的释义')
w.service.set_provider(_Picky())
w.input.setText('alpha beta gamma')
w._do_lookup()
import time
# 两条路都是异步的，但**节奏不同**：批量查词走导入那条链路（有 `_import_progress`），
# 而「整串当一个词」走的是普通查词（没有那个标志）。只等 `_import_progress` 的话，
# 被抽走的那一支根本来不及落库，探针报出来的差别会变成「3 条 vs 0 条」——
# 那是个**时序假象**，不是这条消融要说明的事（它要说明的是「整串被当成一个词收下了」）。
# 因此这里固定等一段时间，让两条路都跑完，报出来的才是真正落在库里的东西。
deadline = time.monotonic() + 2.5
while time.monotonic() < deadline:
    app.processEvents(); time.sleep(0.01)
lem = sorted(e.lemma for e in repo.list_entries(limit=None))
print(f'粘 3 个词之后库里有 {len(lem)} 条 {lem}')
"""

# A24：库里有带标签与不带标签的各一条，选中那个标签，看列表剩几行。
_A24_PROBE = _MAIN_WINDOW_SETUP + """
repo.upsert(Entry(lemma='tagged', definition_en='x', definition_zh='甲', tags='考研'))
repo.upsert(Entry(lemma='plain', definition_en='x', definition_zh='乙'))
w._reload_list(); w._refresh_tag_filter(); app.processEvents()
w.tag_filter.setCurrentIndex(w.tag_filter.findData('考研')); app.processEvents()
print(f'选中「考研」标签之后列表里有 {w.list.count()} 行（应有 1 行）')
"""

# A25：删掉一条（确认框桩成「是」），看「撤销」还能不能被用户点到。
_A25_PROBE = _MAIN_WINDOW_SETUP + """
import app.ui.main_window as mw
victim = repo.upsert(Entry(lemma='victim', definition_en='x', definition_zh='甲'))
w._reload_list(); app.processEvents()

class _Yes:
    StandardButton = mw.QMessageBox.StandardButton

    @staticmethod
    def question(*a, **k):
        return mw.QMessageBox.StandardButton.Yes

    warning = staticmethod(lambda *a, **k: None)
    information = staticmethod(lambda *a, **k: None)

_real = mw.QMessageBox
mw.QMessageBox = _Yes
try:
    w._delete_entry(int(victim.id))
finally:
    mw.QMessageBox = _real
app.processEvents()
print(f'删掉一条之后：「撤销」按钮可见={not w.btn_undo.isHidden()}，'
      f'库里还有这个词={repo.get("victim") is not None}')
"""

# A26：一次拖入 3 个词表文件，看程序收下几个。**不需要窗口**，只走那个纯函数
# ——但 `QMimeData` 必须活到事件用完为止：`QDropEvent` 不持有它的 Python 引用，
# 提前回收会让 `event.mimeData()` 给出一个已析构的包装对象。
_A26_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt
from PySide6.QtGui import QDropEvent
from app.ui.main_window import MainWindow
files = []
for name in ('a.txt', 'b.txt', 'c.txt'):
    p = tmp / name
    p.write_text('word\\n', encoding='utf-8')
    files.append(p)
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(p)) for p in files])
ev = QDropEvent(QPointF(1, 1), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
got = [p.name for p in MainWindow._dropped_paths(ev)]
print(f'一次拖入 3 个词表文件，程序收下 {len(got)} 个 {got}')
"""

# A27：导出对话框给出几种格式——这是用户唯一能看到这件事的地方。
_A27_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from app.ui.export_dialog import EXPORT_FORMATS
keys = [k for k, _l, _h, _e in EXPORT_FORMATS]
print(f'导出对话框给出 {len(keys)} 种格式 {keys}')
"""

# A28：**必须走 `Entry.from_payload`**——那是接口返回 JSON 时唯一的解析入口，也是
# 这项消融动的那一行所在。自己 new 一个 Entry 把搭配直接塞进对象里，实验就完全测
# 不到它。后半段再让它经过**真实详情面板**：这一项横跨「收下」与「呈现」两处，
# 只测其中一处都答不了它。
_A28_PROBE = """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.core.models import Entry
from app.providers.mock import MockProvider
from app.ui.theme import stylesheet
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
app.setStyleSheet(stylesheet())
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()

payload = {
    'lemma': 'charge',
    'definition_en': 'to ask for money',
    'definition_zh': '收费；指控',
    'collocations': [
        {'phrase': 'be in charge of', 'zh': '负责',
         'sentence': 'She is in charge of sales.', 'translation': '她负责销售。'},
        {'phrase': 'take charge of', 'zh': '接管'},
    ],
}
entry = Entry.from_payload(payload)
print(f'接口给了 2 条搭配，程序收下 {len(entry.collocations)} 条 '
      f'{[c.phrase for c in entry.collocations]}')
# 落库再读回来：这一步顺带走 `from_row`（库里的 JSON 文本那一支）与界面渲染。
repo.upsert(entry)
w.detail.show_entry(repo.get('charge')); app.processEvents()
shown = [lb.text() for lb in w.detail.findChildren(QLabel, 'collPhrase')]
ex = [lb.text() for lb in w.detail.findChildren(QLabel, 'collExample')]
print(f'右侧搭配区列出 {len(shown)} 条 {shown}，其中带独立例句的 {len(ex)} 条')
"""

# A29：点真按钮、看真库。两组输出必须**对着读**——`mark_collected` 是在「落库成功
# 之后」才调的，所以抽走落库之后按钮照样说「已收藏」，只有库里那一项会说什么都没发生。
_A29_PROBE = _MAIN_WINDOW_SETUP + """
from app.core.models import Collocation
entry = Entry(lemma='charge', definition_en='x', definition_zh='收费；指控',
              collocations=[Collocation(
                  phrase='be in charge of', gloss_zh='负责',
                  example='She is in charge of sales.', example_zh='她负责销售。')])
w.detail.show_entry(entry); app.processEvents()
btn = w.detail._collect_buttons.get('be in charge of')
btn.click(); app.processEvents()
print(f'点「收藏」之后：词组收藏里 {repo.count_phrases()} 条，'
      f'按钮写着「{btn.text()}」、还能量={btn.isEnabled()}，'
      f'状态栏说「{w.statusBar().currentMessage()}」')
"""

# A30：`run` 与 `trunk` / `runway` 是这一条的全部要点。用真实库最省事——「含这个词
# 的已收藏词组」本来就是从库里查出来的。
_A30_PROBE = """
import sys, tempfile; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Phrase
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
for text in ('run out of', 'trunk road', 'runway model', 'a long run'):
    r.add_phrase(Phrase(phrase=text))
got = sorted(r.phrases_containing('run'))
print(f'查「run」时回显的已收藏词组：{got}')
print('（只有含 run 这个词的两条才算——`trunk` / `runway` 是另外两个词）')
"""

# A31：同一个词组收藏两次，两次带的简释不同，看库里最后留下的是哪一份。
_A31_PROBE = """
import sys, tempfile; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Phrase
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
r.add_phrase(Phrase(phrase='be in charge of', gloss_zh='负责（先收的那份）',
                    example='She is in charge of sales.', source_lemma='charge'))
r.add_phrase(Phrase(phrase='be in charge of', gloss_zh='主管（后收的那份）',
                    example='Who is in charge here?', source_lemma='in charge'))
stored = r.get_phrase('be in charge of')
print(f'同一个词组收藏两次之后，库里是「{stored.gloss_zh}」/「{stored.example}」，'
      f'共 {r.count_phrases()} 条')
"""

# A32：删掉一条词组 → 跑回生词本逛一圈 → 点撤销。用户看到的是「左栏现在在看哪个
# 集合、选中的是哪一行」，不是内部变量。
#
# 生词本里**先放两条词**，是为了让「行号撞车」真的发生——这一点最初写错了，
# 取证后改的：`phrases.id` 声明了 AUTOINCREMENT，恢复时**拿不回原来的行号**，
# 而是发一个新的（这里正好是 2，因为 1 已经用过了）。写得巧不如撞得巧：生词本里
# id=2 的那条词就在那儿。于是「按行号在当前集合里找」找到的是**一个完全不同的
# 词**，界面上没有任何地方会报错。
_A32_PROBE = _MAIN_WINDOW_SETUP + """
import app.ui.main_window as mw
from app.core.models import Phrase
repo.upsert(Entry(lemma='charge', definition_en='x', definition_zh='收费；指控'))
repo.upsert(Entry(lemma='runway', definition_en='x', definition_zh='跑道'))
phr = repo.add_phrase(Phrase(phrase='be in charge of', gloss_zh='负责',
                             example='She is in charge of sales.', source_lemma='charge'))
w._switch_mode(mw.MODE_PHRASES); w._reload_list(); app.processEvents()

class _Yes:
    StandardButton = mw.QMessageBox.StandardButton

    @staticmethod
    def question(*a, **k):
        return mw.QMessageBox.StandardButton.Yes

    warning = staticmethod(lambda *a, **k: None)
    information = staticmethod(lambda *a, **k: None)

_real = mw.QMessageBox
mw.QMessageBox = _Yes
try:
    w._delete_phrase(int(phr.id))
finally:
    mw.QMessageBox = _real
app.processEvents()
# 删错之后用户跑回生词本里逛了一圈，才想起要撤销
w._switch_mode(mw.MODE_WORDS); w._reload_list(); app.processEvents()
w._undo_delete(); app.processEvents()
sel = w.list.currentItem()
back = repo.get_phrase('be in charge of')
print(f'生词本两条词的 id 是 1/2；删掉的那条词组 id={phr.id}，恢复后拿到新 id='
      f'{back.id if back else "<没恢复>"}')
print(f'撤销之后：当前集合={w._mode}，左栏 {w.list.count()} 行，'
      f'选中行=「{sel.text() if sel else "（没有选中任何行）"}」')
"""

# A33：查词即收藏的同时，新词要被排进「待复习」。抽走播种后，复习入口的计数
# 恒为 0，复习功能等于白做——用户查了词，却永远等不到「该复习了」。
_A33_PROBE = """
import sys, tempfile; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Entry
from app.core import review
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
r.upsert(Entry(lemma='newword', definition_zh='新词'))
print(f'新词入库后，待复习数 = {r.due_count(review.now_timestamp())}')
"""


PROBES = {
    "A1 离线兜底": """
import sys; sys.path.insert(0,'.')

# 整段都包在 try 里，而不是只包住最后那两行 import。
#
# 这一步的消融是**改名**，而 `app/providers/__init__.py` 在模块顶层就引用了
# 那个名字——于是连 `from app.providers.base import ProviderError` 都会炸。
# 导入写在保护外面时，探针会在自己的 except 生效之前就死掉，报表上「用户可见」
# 那一栏只剩一句「探测自身出错」；而那一栏恰恰是这项实验唯一要回答的问题
# （抽走之后用户会看到什么）。实测就是这样错的：A1 的「后」一直是个错误串。
try:
    from app.providers.base import ProviderError
    from app.providers.fallback import FallbackProvider
    from app.providers.offline import OfflineDictProvider

    class DeadProvider:
        \"\"\"模拟接口彻底连不上（断网 / 挂了）。\"\"\"
        name = "dead"
        def lookup(self, word):
            raise ProviderError("连不上")

    prov = FallbackProvider(DeadProvider(), OfflineDictProvider())
except Exception as e:
    print(f"兜底不可用（{type(e).__name__}: {str(e)[:40]}）")
else:
    try:
        entry = prov.lookup("apple")
        print(f"接口挂了 → 用户拿到 source={entry.source} 有释义={bool(entry.definition_en)}")
    except Exception as ex:
        print(f"接口挂了 → 用户拿到异常 {type(ex).__name__}: {str(ex)[:40]}")
""",
    "A2 中文分词检索": """
import sys, tempfile, time; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Entry
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
r.upsert(Entry(lemma='ubiquitous', definition_en='x', definition_zh='无处不在的'))
# 灌到 20000 条：只有这样「走 FTS」与「退回 LIKE 扫全表」的耗时才分得开。
# 3000 条时两边都是亚毫秒（0.1ms 量级），探针报出来的「快/慢」全是抖动。
for i in range(20000):
    r.upsert(Entry(lemma=f'w{i}', definition_en='x', definition_zh=f'普通词条{i}'))
best = 9e9
hits = 0
for _ in range(3):
    t0 = time.perf_counter(); hits = len(r.search('无处不在')); d = time.perf_counter() - t0
    best = min(best, d)
print(f"中文『无处不在』命中 {hits} 条，20000 条库中最快 {best*1000:.1f} ms")
""",
    "A3 缺项提示": """
import sys; sys.path.insert(0,'.')
from app.core.models import Entry
e = Entry(lemma='x', definition_en='only english')
try:
    miss = e.missing_parts(None)
    print(f'缺项提示: {miss}')
except Exception as ex:
    print(f'缺项提示不可用（{type(ex).__name__}）')
""",
    "A4 提示词陈旧标记": """
import sys, tempfile; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Entry
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
# 造一个「旧版提示词生成」的词条，再看服务层标不标陈旧
e = Entry(lemma='old', definition_en='x', prompt_version=0)
r.upsert(e)
svc = DictionaryService(r, MockProvider())
got = svc.mark_stale(r.get('old'))
print(f"旧版词条 → prompt_stale={getattr(got, 'prompt_stale', '<无此属性>')}")
""",
    "A5 导入双路径识别": """
import sys; sys.path.insert(0,'.')
from app.core.exporter import export_text
from app.core.models import Entry
from app.core.importer import parse_text
src = export_text([Entry(lemma='ubiquitous', definition_en='seeming to be everywhere', notes='我的备注')])
r = parse_text(src)
print(f'识别={r.source_format} 词={[w.word for w in r.words]}')
""",
    "A6 导入备注保留": """
import sys, tempfile; sys.path.insert(0,'.')
from pathlib import Path
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.models import Entry
from app.core.importer import parse_text, run_import
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
tmp = Path(tempfile.mkdtemp(prefix="quill_ablate_"))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
c = connect(tmp/'t.db'); init_db(c); r = Repository(c)
r.upsert(Entry(lemma='alpha', definition_en='a', notes='我自己写的'))
svc = DictionaryService(r, MockProvider())
# 文件里给 alpha 带一条**不同**的备注，看导入会不会把它写进去
import app.core.exporter as ex
src = ex.export_text([Entry(lemma='alpha', definition_en='a', notes='文件里的备注')])
run_import(svc, r, parse_text(src).words)
print(f'导入后备注={r.get("alpha").notes!r}（期望仍是 我自己写的）')
""",
    "A7 句子整行跳过": """
import sys; sys.path.insert(0,'.')
from app.core.importer import parse_text
r = parse_text('She went to the store to buy some milk\\n')
print(f'整句被收成 {r.count} 个词: {[w.word for w in r.words]}')
""",
    "A8 导出 Markdown 转义": """
import sys; sys.path.insert(0,'.')
from app.core.exporter import export_markdown
from app.core.models import Entry
out = export_markdown([Entry(lemma='x', definition_en='y', notes='a *b* and _c_')])
line = [l for l in out.splitlines() if 'a ' in l and 'b' in l]
print(f'备注导出为: {line[0].strip() if line else "未找到"}')
""",
    "A9 导入前核对框": """
import sys, re; sys.path.insert(0,'.')
src = open('app/ui/main_window.py', encoding='utf-8').read()
# 取函数体这件事本身也可能取错：`def _import_path` 是 `def _import_paths` 的
# **前缀**，正则若不锚定那个左括号，非贪婪匹配会从 `_import_paths` 那一行开始
# 截——而那个函数体里没有 `_confirm_import`，于是这一栏会永远说「核对框被抽走」，
# 无论消融有没有生效。本环境实际发生过（多文件导入引入 `_import_paths` 之后）。
m = re.search(r'def _import_path\\(.*?(?=\\n    def )', src, re.S)
body = m.group(0) if m else ''
if not body.startswith('def _import_path('):
    print('探测自身出错：没截到 _import_path 的函数体')
else:
    print('有核对框' if '_confirm_import' in body else '核对框被抽走（直接写库）')
""",
    "A10 删除确认框": """
import sys, re; sys.path.insert(0,'.')
src = open('app/ui/main_window.py', encoding='utf-8').read()
m = re.search(r'def _delete_entry.*?(?=\\n    def )', src, re.S)
body = m.group(0) if m else ''
# 光看「有没有弹 question」太浅——抽走的可能只是弹窗之后的判断，
# 于是弹窗还在、点「否」却照样删。要看的是「有没有按回答拦下」。
asked = 'QMessageBox.question' in body
checked = '!= QMessageBox.StandardButton.Yes' in body or '== QMessageBox.StandardButton.No' in body
print(f'弹确认框={asked} 按回答拦下={checked}')
""",
    "A11 卡片选中态": """
import sys; sys.path.insert(0,'.')
from pathlib import Path
try:
    from app.ui import widgets
    ok = hasattr(widgets.EntryCard, 'set_selected')
except Exception as e:
    ok = False
src = Path('app/ui/widgets.py').read_text(encoding='utf-8')
short_circuit = 'if bool(self.property("selected")) == bool(flag):' in src
print(f'有set_selected={ok} 有短路守卫={short_circuit}')
""",
    "A12 单实例锁": """
import sys; sys.path.insert(0,'.')
import app.main as m
print('有单实例锁' if hasattr(m, 'acquire_instance_lock') else '锁被抽走')
""",
    "A13 背景图渲染": """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.theme import stylesheet
from app.ui.main_window import MainWindow
from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
app.setStyleSheet(stylesheet())
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
img = QImage(200, 120, QImage.Format.Format_RGB32); img.fill(QColor(255, 0, 255))
p = tmp/'b.png'; img.save(str(p))
w.config['background_image'] = str(p); w.config['background_blur'] = False
w._apply_background(); w.update(); app.processEvents()
# 取窗口左上角的留白处：那里没有面板也没有卡片，铺的就是背景图本身。
px = w.grab().toImage().pixelColor(8, 6).getRgb()[:3]
print(f'窗口留白处像素={px}（配的图是纯品红 255,0,255）')
""",
    "A14 模糊蒙版开关": """
import sys, os, tempfile; sys.path.insert(0,'.')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix='quill_ablate_'))
import atexit, shutil; atexit.register(shutil.rmtree, tmp, True)
os.environ['QUILL_HOME'] = str(tmp)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
app = QApplication([])
from app.config import load_config
from app.core.db import connect, init_db
from app.core.repository import Repository
from app.core.dictionary import DictionaryService
from app.providers.mock import MockProvider
from app.ui.theme import stylesheet
from app.ui.main_window import MainWindow

def checker(sz, cell):
    im = QImage(sz, sz, QImage.Format.Format_RGB32)
    for y in range(sz):
        for x in range(sz):
            im.setPixelColor(x, y, QColor(0,0,0) if ((x//cell)+(y//cell))%2 else QColor(255,255,255))
    return im

def strip_delta(image, y, x0, x1):
    best = 0; prev = image.pixelColor(x0, y)
    for x in range(x0+1, x1):
        cur = image.pixelColor(x, y)
        best = max(best, abs(cur.red()-prev.red())+abs(cur.green()-prev.green())+abs(cur.blue()-prev.blue()))
        prev = cur
    return best

from app.ui.onboarding import CURRENT_ONBOARD_VERSION as _CV; config = load_config(); config['provider'] = 'mock'; config['onboard_version'] = _CV; config['analytics_consent_decided'] = True
conn = connect(tmp/'t.db'); init_db(conn)
repo = Repository(conn)
app.setStyleSheet(stylesheet())
w = MainWindow(repo, DictionaryService(repo, MockProvider()), config); w.show()
# 64px 的大格子：格子太小的话缩放到窗口尺寸这一步本身就是一次平滑，
# 硬边先被抹钝，模糊前后的差别随之缩到判不出来。
ck = tmp/'c.png'; checker(640, 64).save(str(ck))
w.config['background_image'] = str(ck); w.config['background_blur'] = True
w._apply_background(); w.update(); app.processEvents()
d = strip_delta(w.grab().toImage(), 6, 20, 520)
print(f'勾了模糊后背景硬边的最大跳变={d}（越大越清晰，越小越糊）')
""",
    "A15 面板半透明让位": _BG_PANEL_PROBE,
    "A16 子控件样式重算": _BG_PANEL_PROBE,
    "A17 背景透出档位": _BG_VEIL_PROBE,
    "A18 主题换色": _THEME_PROBE,
    "A19 取消设置还原主题": _THEME_CANCEL_PROBE,
    "A20 拖入图片先问一句": _DROP_PROBE,
    "A21 最透档的文字加强色": _DEEP_TEXT_PROBE,
    "A22 记住最近用过的背景图": _DROP_PROBE,
    "A23 输入框多词批量查词": _A23_PROBE,
    "A24 按标签筛选": _A24_PROBE,
    "A25 删除撤销": _A25_PROBE,
    "A26 一次拖入多个文件": _A26_PROBE,
    "A27 Anki 制表符导出": _A27_PROBE,
    "A28 固定搭配与用法例句": _A28_PROBE,
    "A29 词组收藏": _A29_PROBE,
    "A30 按词边界判「含这个词」": _A30_PROBE,
    "A31 重复收藏不覆盖已有内容": _A31_PROBE,
    "A32 撤销词组时切回词组收藏": _A32_PROBE,
    "A33 新词进入复习计划": _A33_PROBE,
}


def item_id(name: str) -> str:
    """取消融条目的编号：`"A2 中文分词检索"` → `"A2"`。

    **`name.startswith("A2")` 不能代替它。** 那个写法会把 A20 / A21 / A22 一并
    命中——本环境实际发生过一次：报表的「要点补充」里，A21（最透档的文字加强色）
    与 A22（记住最近用过的背景图）都被套上了一段属于 A2 的结论，说它们
    「命中条数前后一致，变的是**耗时**」。**那是假话**：它们变的是用户看得见的
    东西，与耗时无关。而且那段话读起来完全通顺，没人会去怀疑。

    前缀匹配在这里没有任何好处，只有这类风险——「A2」从来就不是「A20」的前缀
    关系，只是字符串恰好如此。所以判据取编号**相等**。
    """
    return name.split(" ", 1)[0]


# 「要点补充」要说的话，**按编号声明**，不按名字前缀去猜。
#
# 「变了 / 没变」这一列会把两种很不一样的情况压成同一个词，所以有几项要把关键
# 数字再打一遍。A2 尤其典型：抽走 bigram 后**命中结果完全一样**（`search()` 静默
# 回退到 LIKE），变的只有耗时——只印「变了」就丢掉了「它到底靠什么在起作用」
# 这个真正有用的信息。
#
# 每项写成 `(第一句挂在条目名后面, 其余逐行)`，由 `main()` 打印。**没有这一项就
# 一句话都不说**，不会像前缀匹配那样顺手把邻居也说了。
_EXTRA_NOTES: dict[str, tuple[str, ...]] = {
    "A2": (
        "命中条数前后一致，变的是**耗时**",
        "这说明 bigram 的价值是性能而非召回；也说明索引一旦坏掉，",
        "检索会静默退化（结果一样、只是慢几十倍），用户察觉不到。",
    ),
    "A30": (
        "命中条数会从 2 条涨到 4 条，多出来的是 `trunk road` / `runway model`",
        "子串匹配**不会漏掉真命中，只会多给**——所以它看起来很「宽松、更不会错过」，",
        "错误方向恰好是最不容易被当成 bug 的那一边：用户看到的是「凭空多出几条",
        "不相干的词组」，而界面上一句解释都没有。",
    ),
    "A31": (
        "条数前后都是 1 条（去重照常），变的是**留下来的那一份内容**",
        "这说明「不重复写入」与「不覆盖已有内容」是两件事，`ON CONFLICT DO NOTHING`",
        "同时担着这两条：抽掉它换成 DO UPDATE，去重还在、覆盖却回来了。",
        "而两次收藏在界面上都显示「已收藏」，用户不会知道先前那份已经被顶掉。",
    ),
    "A32": (
        "左栏会从「词组收藏 1 行」变成「生词本 2 行」，变的是**这个行号落在哪个集合里**",
        "词组与词条是两张各自从 1 发号的表，而 `phrases.id` 是 AUTOINCREMENT——",
        "恢复时拿不回原来的行号，会发一个新的；那个新行号在生词本里往往真的存在，",
        "于是「已恢复词组「…」」打出来了，用户看到的高亮却落在**一个完全不同的词**上。",
        "这条路径上没有任何地方会报错：不选中与选错行都长得像一次正常恢复。",
    ),
}


def main() -> int:
    print("=" * 64)
    print("消融实验：逐项抽走功能，看用户与测试能不能察觉")
    print("=" * 64)
    print()

    # 子进程临时目录的基线快照。跑完要核对「没有新增残留」——`_run_test` 与
    # `_probe` 里已经逐个删了，但**「删了吗」必须被验一次**：那些子进程各自装着
    # SQLite 连接，删不掉是这台机器上的常态（实测一次留下 15 个），而删不掉时的
    # 表现是「什么都不发生」。这条断言就是让它变成一次可见的失败。
    temp_before = _temp_dirs()

    # 顺序不能颠倒：先自证检测器（一个不会失败的检测器等于没有检测器），
    # 再查残留，最后注册还原。必须在 `_sha_tree()` 之前——那时记下来的
    # 才是「原状」，否则会把已经坏掉的状态当成基线。
    detector_problems = selfcheck_leftover_detector()
    if detector_problems:
        print("残留检测器自证失败：")
        for line in detector_problems:
            print(f"  - {line}")
        return 4
    leftovers = check_no_leftovers()
    if leftovers:
        print("检测到上一次运行残留的消融，已中止（继续跑会拿坏代码当基线）：")
        for line in leftovers:
            print(f"  - {line}")
        print("\n请把上述位置改回原样，或从备份 / 版本库恢复后重跑。")
        return 3
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _restore_in_flight)
        except (ValueError, OSError):
            # 非主线程或平台不支持，退化成「没有中断还原」——
            # 真正兜底的是下次运行时的残留检测。
            pass
    # 被强杀时若 stdout 重定向到文件，缓冲会连输出一起带走——行缓冲能让
    # 已跑到的进度留在文件里，否则事后看不出跑到哪一项。
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    before = _sha_tree()

    # ---------- 静态校验：编号唯一，且「要点补充」不许挂在不存在的编号上 ----------
    #
    # 这两条挡的是同一类失败：**报表把一句话说给了不该说的那一项**。
    # 本环境实际发生过一次——末尾的「要点补充」原先按 `name.startswith("A2")`
    # 挑条目，字符串前缀把 A20 / A21 / A22 一起命中，于是那三项被套上了属于
    # A2 的结论（「命中条数前后一致，变的是**耗时**」）。**那是假话**：它们变的
    # 是用户看得见的东西，跟耗时无关；而那段话读起来通顺，没人会去怀疑。
    #
    # 想过「禁止编号互为前缀」这条更硬的守卫，**没采用**：现用编号是 A1…A22，
    # `A2` 本来就是 `A20` 的前缀——这条守卫会把一份能用的清单直接判死。为了让
    # 守卫成立去重命名一堆在用的标识符，那是为做而做。真正的修法是**按编号
    # 精确匹配**（见 `item_id`），加上这两条能失败的静态校验。
    _ids = [item_id(n) for n in PROBES]
    _dupes = sorted({i for i in _ids if _ids.count(i) > 1})
    _orphan = sorted(k for k in _EXTRA_NOTES if k not in set(_ids))
    if _dupes or _orphan:
        print("编号不合法，报表会说错对象：")
        for dup in _dupes:
            print(f"  {dup} 重复出现（按编号挑会同时命中两条）")
        for key in _orphan:
            print(f"  「要点补充」挂在 {key} 上，但清单里没有这个编号（说了永远没人听）")
        return 4

    # 基线：先确认不抽任何东西时，测试全绿、探测结果记为「正常」
    #
    # 三个套件都要跑。一开始我只跑了 selfcheck 与 gui_smoke，结果
    # 「单实例锁」那项消融显示「测试全绿（没抓住）」——而它其实由
    # lock_check.py 守着，只是那个脚本不在我的清单里。**实验设计漏了一个
    # 套件，就会得出「这个功能没人守」的错误结论。**
    print("--- 基线（不抽任何东西）---")
    SUITES = [
        ("selfcheck", "scripts/selfcheck.py"),
        ("gui_smoke", "scripts/gui_smoke.py"),
        ("lock_check", "scripts/lock_check.py"),
    ]
    base_ok = True
    for label, script in SUITES:
        rc, out = _run_test(script)
        ok = "失败 0 项" in out
        base_ok = base_ok and ok
        # 退出码与「失败 0 项」**可以不一致**，而且真会：`selfcheck` 是先打完汇总行、
        # 再在 `atexit` 清理之后检查临时目录残留的，有残留就 `return 1`。只按文本
        # 判定会打出「退出码 1  全绿」这种自相矛盾的行——看着像个 bug，其实是两件事
        # 被压成了一行。把第二件事的名字写出来，别让人去猜。
        extra = "" if rc == 0 else "（无失败项，但退出码非零）"
        print(f"  {label:<11}: 退出码 {rc}  {'全绿' if ok else '有失败'}{extra}")
    if not base_ok:
        print()
        print("基线就不是全绿，消融结论无从判断。先修好再跑。")
        return 2

    base_probes: dict[str, str] = {}
    for name in PROBES:
        base_probes[name] = _probe(PROBES[name])
    print(f"  探测基线已记录 {len(base_probes)} 项")
    print()

    rows: list[tuple[str, str, str, str, str]] = []

    for name, why, rel, old, new in ABLATIONS:
        path = ROOT / rel
        raw = path.read_bytes()
        text = raw.decode("utf-8")

        if text.count(old) != 1:
            print(f"[SKIP] {name} —— 锚点命中 {text.count(old)} 次，无法定向消融")
            rows.append((name, "SKIP", "-", "-", "锚点不唯一"))
            continue

        _IN_FLIGHT[rel] = raw
        stripped = text.replace(old, new, 1)

        # 抽走之后文件必须**仍然能解析**：锚点唯一不等于缩进对得齐（替换按子串
        # 做，少写几个空格照样「唯一命中」），替换完可能直接 IndentationError。
        # 那时三个套件是在**导入阶段就死**，报表上写「整套崩溃」——看着像「这项
        # 功能承重」，其实是**消融自己把源码改坏了**。挡在跑套件之前。同一坑在
        # 变异脚本上实际踩过一次（见 `mutation_check` 里那段说明）。
        try:
            ast.parse(stripped)
        except SyntaxError as exc:
            print(
                f"[BAD] {name} —— 消融让源码无法解析（{exc.msg}，第 {exc.lineno} 行）。"
                f"这是**消融写错了**，不是功能承重"
            )
            rows.append((name, "BAD", "-", "-", "消融写坏了源码"))
            continue

        path.write_bytes(stripped.encode("utf-8"))
        try:
            results: dict[str, str] = {}
            for label, script in SUITES:
                _rc, out = _run_test(script)
                if "失败 0 项" in out:
                    continue
                # 套件**正常跑完**一定会打印结论行「通过 N 项，失败 M 项」。
                # 没有这一行，说明它根本没跑到结论（多半是导入就炸了），那是
                # 「整套崩溃」，不是「0 项红」。这两件事原先被压成同一个词：
                # 无论跑出什么，一律写 `{out.count('FAIL')} 项红`，于是整套崩溃
                # 显示成「0 项红」——而同一行又被判成「已抓住」，输出里就出现
                # 「0 项红却已抓住」的自相矛盾。A1 离线兜底正是这种：抽掉
                # `FallbackProvider` 这个名字之后，三个套件全部在导入阶段就死，
                # 一个 FAIL 都打不出来。判据本身不受影响（套件连启动都做不到，
                # 恰恰说明这个功能是承重的），但要**如实写出来**，不能让人
                # 把「崩了」误读成「一条都没红」。
                if "项，失败 " in out:
                    results[label] = f"{out.count('FAIL')} 项红"
                else:
                    results[label] = "整套崩溃"
            probe_after = _probe(PROBES[name])
        finally:
            path.write_bytes(raw)
            _IN_FLIGHT.pop(rel, None)

        user_changed = probe_after != base_probes[name]
        test_col = "、".join(f"{k} {v}" for k, v in results.items()) if results else "全绿（没抓住）"
        tests = list(results.values())

        print(f"[{name}]")
        print(f"    抽走的是: {why}")
        print(f"    用户可见  : 前 {base_probes[name]}")
        print(f"               后 {probe_after}")
        print(f"    测试反应  : {test_col}")
        print()

        rows.append(
            (
                name,
                "全绿（没抓住）" if not tests else "已抓住",
                "变了" if user_changed else "没变",
                test_col,
                probe_after,
            )
        )

    after = _sha_tree()
    dirty = [p for p in before if before[p] != after.get(p)]

    print("=" * 64)
    print("结论表")
    print("=" * 64)
    print()
    print(f"{'消融项':<16}{'测试':<12}{'用户可见':<10}")
    print("-" * 64)
    blind: list[str] = []
    for name, t, u, detail, _ in rows:
        print(f"{name:<16}{t:<12}{u:<10}")
        if t == "全绿（没抓住）":
            blind.append(f"{name}（{detail}）")

    print()
    print("=" * 64)
    if blind:
        print(f"以下 {len(blind)} 项消融后测试全绿 —— 它们没有被测试守住：")
        for b in blind:
            print(f"  - {b}")
        print()
        print("注意：这不等于「功能无效」。要看「用户可见」那一列——")
        print("      若界面确实变了（用户可见=变了），则是**测试缺口**：")
        print("      功能真的在起作用，只是没有任何测试盯着它。")
        print("      若界面也没变（用户可见=没变），那它才是**无效功能**。")
    else:
        print(f"全部 {len(rows)} 项消融都被测试抓住了。")

    # 「要点补充」：按编号（不是名字前缀）挑条目。见 `_EXTRA_NOTES` 与 `item_id`。
    print()
    print("要点补充（结果相同但代价不同的那几项）：")
    _said = False
    for index, (name, *_) in enumerate(rows):
        notes = _EXTRA_NOTES.get(item_id(name))
        if not notes:
            continue
        _said = True
        print(f"  - {name}：{notes[0]}——")
        print(f"      前 {base_probes[name]}")
        print(f"      后 {rows[index][4]}")
        for line in notes[1:]:
            print(f"    {line}")
    if not _said:
        print("  （本次没有要补充的项）")

    print()
    if dirty:
        print("还原失败！以下文件未回到原状：")
        for d in dirty:
            print(f"  {d}")
        return 2
    print(f"工作区已逐字节还原（核对 {len(before)} 个 .py 文件的 sha256）")

    # 子进程临时目录的清理自证。放在最后：前面的失败会先返回，不会被这条掩盖；
    # 而这条一旦报红，说明有目录正堆在系统盘上——它不会让任何别的东西失败，
    # 只会一路静默累积（见 `_probe` 的注释）。
    stray = sorted(_temp_dirs() - temp_before)
    if stray:
        print(f"子进程临时目录清理失败：{len(stray)} 个残留")
        for path in stray:
            print(f"  {path}")
        return 5
    print("子进程临时目录已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
