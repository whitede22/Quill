# -*- coding: utf-8 -*-
"""界面主题：颜色只能有一份定义。

这一版是**重建**的产物——原文件在一次全量变异运行中因文件系统级损坏丢失
（25485 字节、内容全为 `\\x00`）。重建的约束来自三处外部证据，不来自记忆：

  1. `scripts/mutation_check.py` 里 15 条针对本模块的变异，给出了**逐字**的源码片段；
  2. `scripts/selfcheck.py` / `scripts/gui_smoke.py` 里对本模块的全部断言，给出了
     API 契约（符号名、键名、阈值、QSS 必备片段）；
  3. `docs/theming.md` / `docs/window-background.md` / `docs/ui-polish.md` 给出了
     六套强调色、深浅两套档位值、以及关键色值。

设计原则（`docs/theming.md`）：
  - 六套主题 = 两份底座 × 一份强调色族，`THEMES` 只有**一处**组装；
  - 样式表模板里不许出现字面颜色，全部走 `$name` 占位符；
  - 面板半透明规则**由档位表生成**，不手写。
"""

from __future__ import annotations

import re

# 名义表从 `app/theme_meta.py` 取，方向是 ui 依赖它，不是反过来。这些名字仍然
# 在本模块导出，`selfcheck`、`gui_smoke` 与界面代码沿用 `theme.THEME_DEFAULT`
# 这类写法即可，不必改成从 theme_meta 取。
#
# **不要在这里重新定义它们**。重定义会让配置层与界面层各认一份，配置层放行的
# 主题名到这边可能是空，`selfcheck` 里 `set(THEMES) == set(THEME_IDS)` 那条
# 断言就是拦这件事的。
from ..theme_meta import (  # noqa: F401
    BG_VEIL_DEFAULT,
    THEME_DEFAULT,
    THEME_IDS,
    VEIL_NAMES,
    theme_id_of,
    veil_level,
)

# ---------------------------------------------------------------------------
# 一、底座：深浅两份
#
# 前五套浅色主题共用 `_BASE_LIGHT`，只有 `night` 用 `_BASE_DARK`。深色底座不是
# 「把浅色反过来」——正文、次要文字、边框、提示条、滚动条把手都是另挑的一份，
# 因为同一个层级关系在两种亮度下需要的色差不同。
# ---------------------------------------------------------------------------

_BASE_LIGHT = {
    "dark": False,
    "bg": "#FBFBFA",            # 窗口底色
    "surface": "#FFFFFF",       # 面板 / 卡片底色
    "track": "#F2F2EF",         # 列表轨道（比面板暗一档，卡片边界才看得出来）
    "border": "#E6E6E2",        # 面板描边
    "divider": "#EFEFEC",       # 分割线（比描边更淡，只给 #divider 用）
    "text": "#1F1F1E",          # 正文
    # 次要文字。旧值 `#8A8A86` 在白面板上只有 **3.47:1**、在窗口底 `#FBFBFA`
    # 上 3.35:1，双双低于 AA 的 4.5 —— 而它承担的全是**真内容**（音标、词性、
    # 卡片中文简释、同义语差异说明、搭配简释、状态栏）。加深到 `#6E6E6A`：
    # 白底 5.12:1、窗口底 4.94:1，两端都过；且在最透档的合成底上仍只有 1.69
    # （< 3.0），「最透档把次要文字换成加强色」那条规则因此仍然必要、不是冗余。
    "text2": "#6E6E6A",
    # 占位 / 禁用。旧值 `#B6B6B1` 只有 **2.04:1** —— 那个对比度不是「淡」，
    # 是「看着费劲」。提到 `#91918C`：白底 3.13:1、窗口底 3.02:1。
    #
    # 门槛是 **3:1 而不是 4.5:1**，这一条要写明白：`text3` 只允许用于**占位符**
    # 与**禁用态**（`#placeholder`、`QPushButton:disabled`），WCAG 1.4.3 对
    # 禁用控件明确豁免，占位符低于正文对比度是各平台的通行做法。若哪天有人
    # 把它拿去当真内容用，`selfcheck` 里那条「`$text3` 的用途白名单」会当场报红
    # ——**门槛的松紧与用途的白名单是一对**，松的那一半靠紧的那一半兜住。
    "text3": "#91918C",
    "text2_strong": "#484845",  # 最透档下次要文字的加强色
    "text3_strong": "#484845",  # 最透档下占位文字的加强色
    "icon_idle": "#C4C2B9",     # 卡片 × 常驻色（独立常量，不复用 text3）
    "warn_bg": "#FCF7EE",       # 提示块底
    "warn_border": "#EADFC8",   # 提示块描边
    "warn_text": "#8A6224",
    "input_bg": "#FFFFFF",
    "input_border": "#DCDCD6",
    "scroll": "#D8D8D2",        # 滚动条把手
}

_BASE_DARK = {
    "dark": True,
    "bg": "#1B1C1E",
    "surface": "#24262A",
    "track": "#1F2124",
    "border": "#33363B",
    "divider": "#2C2F33",
    "text": "#E8E8E6",
    "text2": "#A8A8A4",
    "text3": "#7B7B78",
    "text2_strong": "#CFCFCB",
    "text3_strong": "#BCBCB8",
    "icon_idle": "#5E6166",
    "warn_bg": "#33291A",
    "warn_border": "#4E3F26",
    "warn_text": "#E3C48A",
    "input_bg": "#2A2C31",
    "input_border": "#3C4046",
    "scroll": "#4A4E54",
}

# ---------------------------------------------------------------------------
# 二、强调色族：每套主题真正不同的那部分 + 显示名
#
# 一条 = 六个派生色。派生而不是逐套手写，是因为「强调色换了、悬停色忘了换」
# 不会有任何报错，只会让某一套的悬停态停在上一套的颜色上。
# ---------------------------------------------------------------------------

_ACCENTS: dict[str, dict[str, str]] = {
    "ink": {
        "label": "靛蓝",
        "accent": "#4A6FA5",
        "accent_soft": "#EEF3FA",
        "accent_hover": "#3F608F",
        "on_accent": "#FFFFFF",
        "accent_disabled": "#C3D0E2",
    },
    "sage": {
        "label": "松绿",
        "accent": "#3F7A63",
        "accent_soft": "#EEF6F3",
        "accent_hover": "#356653",
        "on_accent": "#FFFFFF",
        "accent_disabled": "#BCD6CB",
    },
    "clay": {
        "label": "赭石",
        "accent": "#8C5A33",
        "accent_soft": "#F6EDE5",
        "accent_hover": "#764B29",
        "on_accent": "#FFFFFF",
        "accent_disabled": "#DDC6B2",
    },
    "plum": {
        "label": "绛紫",
        "accent": "#7A4A72",
        "accent_soft": "#F4EBF2",
        "accent_hover": "#663D5F",
        "on_accent": "#FFFFFF",
        "accent_disabled": "#D8C2D3",
    },
    "graphite": {
        "label": "石墨",
        "accent": "#5A5F66",
        "accent_soft": "#EEF0F2",
        "accent_hover": "#4A4E54",
        "on_accent": "#FFFFFF",
        "accent_disabled": "#C6C9CD",
    },
    "night": {
        "label": "夜",
        "accent": "#7FA6DC",
        "accent_soft": "#26313F",
        "accent_hover": "#93B6E6",
        "on_accent": "#12202F",
        "accent_disabled": "#3F4C5C",
    },
}

# 顺序即设置界面下拉框的顺序，第一条是默认。取自名义表，不在这里重抄一份：
# 抄一份的后果是「加了新主题、只在一处加」，而调色板里根本没有它。
_THEME_ORDER = THEME_IDS

# 底座归属：谁用深色那份。
_DARK_THEMES = frozenset({"night"})


# ---------------------------------------------------------------------------
# 二之二、圆角尺度
#
# 原先四个语义（容器 / 控件 / 胶囊 / 滚动条把手）散在模板里写成四个字面数字
# （8 / 6 / 9 / 5），谁也说不清「这个 6 与那个 8 是两回事还是写岔了」。改成
# 带名字的 token 之后，模板里再出现字面数字就会被 `selfcheck` 挡下来（能失败
# 的静态校验），且「同一属性只有一份定义」这条铁律在圆角上也成立。
#
# 值一律带单位 `px`：`stylesheet()` 的 `$name` 替换是逐 token 做的，写
# `border-radius: $radius_barpx;` 会被解析成键名 `radius_barpx`（不存在，
# 原样留下），出来的样式表里就躺着一个 `$radius_barpx`——Qt 解析失败、静默
# 退回直角，**不报任何错**。这正是模板里所有值都走完整 `$name`（含单位）的
# 原因，圆角与颜色同一条规矩。
# ---------------------------------------------------------------------------

_RADIUS: dict[str, str] = {
    "radius_container": "8px",  # 面板 / 卡片 / 列表：一块面的外轮廓
    "radius_control": "6px",    # 按钮 / 输入框 / 下拉框 / 提示块：面上的控件
    "radius_pill": "9px",       # 胶囊（同义语 chip）：等于它自身高度的一半
    "radius_bar": "5px",        # 滚动条把手：等于把手宽度的一半
}


def _build_themes() -> dict[str, dict[str, object]]:
    """唯一的组装点。在别处再拼一次，代价是「加了新键、只在一边加」不会报错。"""
    out: dict[str, dict[str, object]] = {}
    for tid in _THEME_ORDER:
        base = dict(_BASE_DARK if tid in _DARK_THEMES else _BASE_LIGHT)
        base.update(_ACCENTS[tid])
        base["veil"] = dict(_VEIL_DARK if tid in _DARK_THEMES else _VEIL_LIGHT)
        # 圆角与主题无关（六套共用一份），但必须**并进调色板**：`stylesheet()`
        # 的 `$name` 取值表就是调色板，而 `_TEMPLATE_KEYS` 那条「每套主题都补齐
        # 模板需要的全部键」的护栏也是拿它当名单的。放在外面另做一次替换，
        # 就等于同一件事有第二个查找路径——漏一处不报错，只是那处圆角留在原样。
        base.update(_RADIUS)
        out[tid] = base
    return out


# ---------------------------------------------------------------------------
# 三、背景透出档位
#
# 档位 = 「面板铺自身底色的不透明度」。可读性的最坏情况在深浅两边是**相反**的：
#   - 浅色主题：正文是深色，最坏是背景图那块纯黑——面板被压暗；
#   - 深色主题：正文是浅色，最坏是背景图那块纯白——面板被提亮。
# 同一个 58% 在浅色下守住 5.4:1，在深色下只剩 3.2:1（跌破 4.5）。所以档位
# **名**共用（用户选的是一件事），**值**按底座分家（实现上必须不同）。
# ---------------------------------------------------------------------------

_VEIL_LIGHT = {"soft": 88, "medium": 72, "bold": 58}
_VEIL_DARK = {"soft": 96, "medium": 84, "bold": 72}

# 档位名 → 浅色底座的不透明度。这是**默认主题**（浅色）下的档位表，供测试与界面
# 直接引用；换主题时走 `palette(tid)["veil"]`。绑成字典而不是元组，是因为调用方
# 既迭代档位名，也用它按名取值。
BG_VEIL_LEVELS = dict(_VEIL_LIGHT)

# 档位名与默认档来自 `app/theme_meta.py`，见文件顶部导入块。档位**值**留在这里，
# 因为值随底座变化（同一个 58% 在深色底上只剩 3.2:1），属于渲染层的事实。


# ---------------------------------------------------------------------------
# 四、调色板访问
# ---------------------------------------------------------------------------

THEMES = _build_themes()


def theme_ids() -> list[str]:
    """按设置界面的顺序返回全部主题 id。"""
    return list(_THEME_ORDER)


# `theme_id_of()` 来自 `app/theme_meta.py`。它判的是名义表 `THEME_IDS`，不是这里
# 的 `THEMES` 键，所以两者必须一致，由 `selfcheck` 的集合相等断言守着。


def palette(theme_id: object) -> dict[str, object]:
    """取一套主题的完整调色板。不认识的主题名退回默认套。"""
    return THEMES[theme_id_of(theme_id)]


# 便捷别名。这几个是**默认主题**下的值，供测试与界面直接引用；换主题时走
# `palette()`。绑定成常量而不是函数，是因为它们被断言当成「当前基调」的代表。
TEXT = _BASE_LIGHT["text"]
TEXT2 = _BASE_LIGHT["text2"]
TEXT3 = _BASE_LIGHT["text3"]
ICON_IDLE = _BASE_LIGHT["icon_idle"]
WARN_BG = _BASE_LIGHT["warn_bg"]
WARN_BORDER = _BASE_LIGHT["warn_border"]
WARN_TEXT = _BASE_LIGHT["warn_text"]


# ---------------------------------------------------------------------------
# 五、样式表模板
#
# 模板里**一个字面颜色都不许有**，全部走 `$name` 占位符。写死的十六进制在换主题时
# 不报错，只是静静地留着上一套的颜色，看起来像「主题换了一半」。
# `_TEMPLATE_KEYS` 是模板里所有 `$name` 的白名单，`stylesheet()` 用它从调色板取值，
# 也用它当护栏：加了新颜色却忘了补某套主题，会在生成时直接 KeyError。
# ---------------------------------------------------------------------------

class _QssTemplate:
    """样式表模板。包成对象而不是裸字符串，是为了让 `$name` 白名单与模板**绑在一起**：
    selfcheck 可以直接拿 `_QSS.template` 扫「模板里有没有字面颜色」，不必再问一遍
    「这份字符串是不是那个模板」。
    """

    __slots__ = ("template",)

    def __init__(self, template: str) -> None:
        self.template = template

    def __str__(self) -> str:  # 方便日志与调试
        return self.template


_QSS = _QssTemplate("""
QMainWindow { background: $bg; }
QWidget { color: $text; font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; }

/* 容器背景必须**显式**给，不能留给系统调色板。
   正文颜色由上面那条 `QWidget { color: $text; }` 全量接管，而这里的 `$text`
   是**主题色**（浅色主题下是深色字）；可容器若没有背景规则，绘制时用的是
   `QPalette::Window` / `QPalette::Base`——**系统**的，不是主题的。系统一开
   深色模式（本机 Window=#1e1e1e、Base=#2d2d2d），就出现「深字压深底」：
   详情区整块黑、对话框空白区整块黑，而且**不报任何错**——它看起来像主题
   没生效，实则是一条规则都没铺到这些容器上。
   只按需补「确实承载内容的容器」，不要给 `QWidget` 加通配背景：那会把
   卡片上方的透明叠层一起填实，卡片就成了死板的一块。 */
QDialog { background: $bg; }

/* 详情区的滚动容器。它没有 objectName，也没有任何背景规则，于是 viewport
   去取 `QPalette::Base`——就是上面说的**系统**色。两层都要铺透明：第一层
   管 viewport 自己，第二层管 `setWidget` 塞进去的内容控件（它同样会自填）。
   铺完，详情区才透得出父级 `QFrame#panel` 的半透明底，背景图才透得上来。

   这两条是详情区底色的**唯一**来源。曾额外在 `EntryDetail` 里关过一次
   viewport 的 `autoFillBackground`，实测：两条规则在时不产生任何差异，
   单独存在时又不够（仍是系统色）——按「同一属性只能有一份定义」删掉了，
   留下的就是这里。 */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* 左右两栏之间的分裂条手柄（宽 10px）。它是同一个坑的另一处：没有背景规则
   的容器去取 `QPalette::Window`——系统开深色时就是一条 `#1e1e1e` 的黑竖线，
   横在两栏正中，格外显眼。铺透明让窗口底（有背景图时就是图片本身）透上来，
   手柄只作为「可拖拽的空隙」存在，不画实体色块。

   这条规则的效果**只在真实桌面 + 系统深色下看得见**：离屏平台上抽掉它，
   手柄像素仍是窗口底色（实测 `#fbfbfa→#fbfbfa`，深色 palette 到不了那一层），
   所以守它的判据落在「模板里还有没有这条规则」上，见 `selfcheck` 里那条
   「容器底色来自主题而非系统调色板」。别把「离屏测不出差别」当成「这条没用」。 */
QSplitter::handle { background: transparent; }

/* 欢迎页 / 教程页 与 复习会话。这两页的字体规则原先**内联在各自的界面文件里**
   （`onboarding.py` / `review_dialog.py` 各自拼一段 `setStyleSheet`），颜色虽取自
   调色板、没有字面值，但「欢迎页标题多大、次要文字什么色」这件事仍是两处定义。
   收进来的收益不只是少一次重复：模板这一份同时受「不许字面颜色」与「每套主题
   都必须补齐模板全部键」两条断言约束，内联那一份只能靠单条测试盯。
   两个对话框仍然在构造时 `setStyleSheet(stylesheet(tid))`——**样式自持**这条性质
   照旧（不依赖 QApplication 的全局样式表），测试直接构造它们时拿得到正确配色。 */
QDialog#onboarding { background: $surface; }
QLabel#welcomeTitle { font-size: 26px; font-weight: 700; color: $text; }
QLabel#welcomeSub   { font-size: 14px; color: $text2; }
QLabel#featureTitle { font-size: 14px; font-weight: 600; color: $text; }
QLabel#featureBody  { font-size: 13px; color: $text2; }

QDialog#reviewDialog { background: $surface; }
QLabel#reviewTitle    { font-size: 22px; font-weight: 700; color: $text; }
QLabel#reviewProgress { font-size: 12px; color: $text2; }
QLabel#reviewLemma    { font-size: 32px; font-weight: 700; color: $text; }
QLabel#reviewPhonetic { font-size: 14px; color: $text2; }
/* 提示行归到 `$text2`：它是**要读进去的一句话**（「先回想一下这个词的意思，
   再翻面看答案」），不是占位符——占位色只守 3:1，不该拿来承载正文。 */
QLabel#reviewHint     { font-size: 13px; color: $text2; }
QLabel#reviewSection  { font-size: 12px; font-weight: 600; color: $text2; }
QLabel#reviewBody     { font-size: 14px; color: $text; }
QLabel#reviewSub      { font-size: 12px; color: $text2; }

QFrame#panel {
    background: $surface;
    border: 1px solid $border;
    border-radius: $radius_container;
}

QLabel#lemma    { font-size: 22px; font-weight: 600; color: $text; }
QLabel#phonetic { font-size: 13px; color: $text2; }
QLabel#pos      { font-size: 12px; color: $text2; }
QLabel#body     { font-size: 13px; color: $text; }
QLabel#muted    { font-size: 12px; color: $text2; }
QLabel#placeholder { font-size: 13px; color: $text3; }
/* 分区小标题。两个 objectName 分属两个界面（详情区分节 / 导出对话框分节），
   但**样式只有一处定义**——两行一模一样的规则等于同一件事写了两遍，
   改一处忘一处时不会有任何报错，只会让其中一处停在旧值上。
   颜色从 `$text3` 归到 `$text2`：它是**真内容**（「英 / 中 / 例句 / 固定搭配」），
   不该用只守 3:1 的占位色。 */
QLabel#section, QLabel#sectionTitle {
    font-size: 12px; color: $text2; font-weight: 600;
}
QLabel#divider {
    background: $divider; border: none;
}
QLabel#countLabel { font-size: 12px; color: $text2; }
QLabel#formatHint { font-size: 12px; color: $text2; }

/* 同义语两行。**必须拆成两个标签**——一个 QLabel 只有一个 color，要让释义比
   差异说明重一档，就只能在富文本里塞字面颜色，而那正是深色主题下整行消失的
   缺陷来源（浅色 text 压在 night 卡片底只有 1.09:1）。 */
QLabel#synGloss { font-size: 12px; color: $text; }
QLabel#synNote  { font-size: 12px; color: $text2; }

/* 固定搭配区。例句必须跟着搭配走：be in charge of 与 take charge of 的差别
   只有各自的例句说得清。 */
QLabel#collPhrase  { font-size: 13px; color: $text; font-weight: 600; }
QLabel#collGloss   { font-size: 12px; color: $text2; }
QLabel#collExample { font-size: 12px; color: $text; }

QPushButton {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_control;
    padding: 5px 12px;
}
QPushButton:hover { border-color: $accent; color: $accent; }
QPushButton:disabled { color: $text3; border-color: $border; }

/* 按下态。**此前一条都没有**：按下去与不按长得一模一样，用户拿不到「这一下
   落在哪个按钮上」的反馈。只压深底色、不换颜色——按下是一瞬间的事，换色会
   让按钮在那一下「跳」一格。 */
QPushButton:pressed { background: $track; }

QPushButton#primary {
    background: $accent;
    color: $on_accent;
    border: 1px solid $accent;
    font-weight: 600;
}
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
/* 主按钮的按下态必须单独写：`QPushButton#primary` 比 `QPushButton:pressed`
   更具体，通用那条压不到它的底色上（写了不生效、且不报错）。 */
QPushButton#primary:pressed { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:disabled {
    background: $accent_disabled;
    border-color: $accent_disabled;
    color: $on_accent;
}

/* 无边框次要按钮（格式说明 / 备份 / 设置 / 卡片移除）。悬停才显色。 */
QPushButton#bare {
    background: transparent;
    border: none;
    color: $text2;
    padding: 4px 8px;
}
QPushButton#bare:hover { color: $accent; }
QPushButton#bare:pressed { color: $accent_hover; }

/* 破坏性动作只在悬停时报警，常驻不报警。 */
QPushButton#bare[role="destructive"]:hover { color: $warn_text; }
QPushButton#bare[role="destructive"]:pressed { color: $warn_text; }

/* 状态栏里的按钮。**不能挂 #bare**：那条规则带背景色，会在状态栏贴出方块。 */
QPushButton#statusButton {
    background: transparent;
    border: none;
    color: $accent;
    padding: 2px 8px;
}
QPushButton#statusButton:hover { color: $accent_hover; }
QPushButton#statusButton:pressed { color: $accent_hover; }

/* 左栏「生词本 / 词组收藏」。这对按钮 `setCheckable + setAutoExclusive`——互斥
   由 Qt 保证，但**选中态此前没有任何样式**：两个按钮都落回通用按钮规则，实测
   四角像素与未选中**完全相同**，用户在界面上看不出自己在哪个集合里。
   选中 = 强调色底 + 强调色字 + 强调色描边（描边宽度沿用通用的 1px，不跳尺寸）。
   配了背景图时底色还要再走一档半透明，那条由 `_veil_rules` 生成。 */
QPushButton#mode:checked {
    background: $accent_soft;
    color: $accent;
    border-color: $accent;
}

/* 左栏「待复习 N」。此前**一个样式规则都没有**——它落回通用按钮（白底 + 描边），
   在一排淡色元素里比列表本身还抢眼。它是个入口，不是主操作：无边框、强调色字，
   悬停才加深。配背景图时它同样直接压在原图上，垫底规则见 `_veil_rules`。 */
QPushButton#review {
    background: transparent;
    border: none;
    color: $accent;
    padding: 4px 8px;
    text-align: left;
}
QPushButton#review:hover { color: $accent_hover; }
QPushButton#review:pressed { color: $accent_hover; }

/* 左栏「重查全部」，与「待复习」同行、居右。同样是入口而非主操作：无边框。
   刻意**不用强调色**——那一行只该有一个强调色落点，两处都用等于两处都不显眼；
   它用次要文字色，悬停才升一档。配背景图时的垫底见 `_veil_rules`。 */
QPushButton#refresh {
    background: transparent;
    border: none;
    color: $text2;
    padding: 4px 8px;
    text-align: right;
}
QPushButton#refresh:hover { color: $accent_hover; }
QPushButton#refresh:pressed { color: $accent_hover; }

/* 卡片删除 ×。常驻色是**独立常量**，不复用 text3——那个常量还管着占位文字与
   两处禁用态，改它会把那几处可读性一起拖下去。 */
QPushButton#cardDelete {
    background: transparent;
    border: none;
    color: $icon_idle;
    font-size: 15px;
    padding: 0 4px;
}
QPushButton#cardDelete:hover { color: $warn_text; }
QPushButton#cardDelete:pressed { color: $warn_text; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
    background: $input_bg;
    color: $text;
    border: 1px solid $input_border;
    border-radius: $radius_control;
    padding: 4px 7px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color: $accent; }

/* 排序下拉框给箭头留了位置，否则文字与箭头挤在一起。 */
QComboBox { padding: 5px 22px 5px 9px; }
QComboBox { background: $surface; color: $text; border: 1px solid $border; border-radius: $radius_control; }
QComboBox QAbstractItemView {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    selection-background-color: $accent_soft;
    selection-color: $text;
}

QListWidget {
    background: $track;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_container;
    outline: none;
}

/* 设置里「提交报告」的独立区域。它与生词卡片的区别在**没有悬停变化**：这个
   框不是可点区域，整块高亮等于承诺一个不存在的点击。 */
QFrame#notice {
    background: $surface;
    border: 1px solid $border;
    border-radius: $radius_container;
}

/* 卡片刻意保持**不透明**：列表项文字是透明画的、一直靠卡片盖住它。卡片一透
   就成鬼影；消除鬼影要把文字改透明，会毁掉无障碍与测试读「这一行是什么」的
   能力。有反向断言守这条。 */
QFrame#card {
    background: $surface;
    border: 1px solid $border;
    border-radius: $radius_container;
}
QFrame#card[selected="true"] {
    background: $accent_soft;
    border: 1px solid $accent;
}
QFrame#card:hover { border-color: $accent; }
QLabel#cardTitle { font-size: 14px; color: $text; font-weight: 600; }
QLabel#cardPos   { font-size: 12px; color: $text2; }
QLabel#cardGloss { font-size: 12px; color: $text2; }

/* 同义语 chip（左栏同义词 / 搭配区「收藏」按钮）。
   **选择器的控件类必须是 `QPushButton`**：这两处用的都是 `QPushButton`
   （`widgets.py` 的 `_synonym_row` 与搭配区的收藏按钮），写成 `QLabel#chip`
   时样式表照样能构造、能加载、**不报任何警告**，只是那两处永远拿不到这条
   规则——它们静静地落回通用按钮样式（白底 + 描边 + 控件圆角），于是「同义语
   chip 是个淡色胶囊」这件事从来没有发生过。`selfcheck` 里那条「chip 的选择器
   前缀控件类必须与使用处一致」就是为它加的。
   左右内边距 11 + 11，`widgets._equalize_chips` 按这个数算列宽。 */
QPushButton#chip {
    background: $accent_soft;
    color: $accent;
    border: none;
    border-radius: $radius_pill;
    padding: 3px 11px;
    font-size: 12px;
}
QPushButton#chip:hover { color: $accent_hover; }
/* 按下给一个实心的确认：胶囊面积小，只改字色在这么小的块上看不出来。 */
QPushButton#chip:pressed { background: $accent; color: $on_accent; }

QLabel#warn {
    font-size: 12px;
    color: $warn_text;
    background: $warn_bg;
    border: 1px solid $warn_border;
    border-radius: $radius_control;
    padding: 7px 10px;
}

QStatusBar {
    background: $bg;
    color: $text2;
    font-size: 12px;
}

QToolTip {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    padding: 4px 7px;
}

/* 有背景图时窗口让位，否则 QSS 铺的底色会把图片整个盖住。 */
QMainWindow[hasBackground="true"] { background: transparent; }

QScrollBar:vertical {
    background: transparent; width: 10px; margin: 0;
}
QScrollBar::handle:vertical {
    background: $scroll; border-radius: $radius_bar; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: $accent_disabled; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: $scroll; border-radius: $radius_bar; min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""")

# 模板里所有 `$name` 的白名单。`stylesheet()` 用它取值，selfcheck 用它验「每套
# 主题都补齐了模板需要的颜色」。
_TEMPLATE_KEYS = (
    "bg",
    "surface",
    "track",
    "border",
    "divider",
    "text",
    "text2",
    "text3",
    "text2_strong",
    "text3_strong",
    "icon_idle",
    "warn_bg",
    "warn_border",
    "warn_text",
    "input_bg",
    "input_border",
    "scroll",
    "accent",
    "accent_soft",
    "accent_hover",
    "on_accent",
    "accent_disabled",
    # 圆角尺度。它们不是颜色，但和颜色走**同一条**取值路径（模板里的 `$name`），
    # 所以也必须在这里登记——漏登记的表现是替换不生效、样式表里留下一个
    # `$radius_control`，Qt 解析失败后静默退回直角。
    "radius_container",
    "radius_control",
    "radius_pill",
    "radius_bar",
)


# ---------------------------------------------------------------------------
# 六、面板半透明规则：**由档位表生成**，不手写
#
# 手写两遍时「改了常量、忘了改规则」不会有任何报错，只会静默地让某一档看起来
# 与另一档一样。六个目标各自的颜色取自调色板里的现成值：
#   panel → surface、list → track（它本来就比面板暗一档）、status → bg、
#   裸按钮 / 计数标签 / 视图切换 → surface。
# 后三个是「自己没有表面」的控件：它们直接压在**原图**上，而图片的明暗未知——
# 一个颜色救不了两种背景，只能给文字一个受控的表面。
# ---------------------------------------------------------------------------

def _rgb(value: str) -> tuple[int, int, int]:
    """把 `#RRGGBB` 拆成三元组。"""
    text = value.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


# 「最透那一档下要加深的次要文字」的登记表。存的是**完整选择器**，不是 objectName
# 现拼的 `QLabel#{name}`——面板上还有一种「自己不带底色」的组件，把选择器写死成
# QLabel 就永远加不进它。
#
# 漏一个的表现是「只有这一行看不清」，**不报错**，而用户会以为是内容缺失。
#
# `QPushButton#bare` **刻意不在**这张表里：它的解法是垫底（见 `_VEIL_TARGETS`），
# 不是换文字色。`#cardPos` / `#cardGloss` 同样不在——卡片**刻意保持不透明**
# （见 `_QSS` 里 `#card` 那段），压在卡片上的文字本来就有不透明底，不需要加强；
# 把它们写进来还会让「卡片是否半透明」那条反向断言误报。
_STRONG_TEXT: tuple[tuple[str, str], ...] = (
    ("QLabel#muted", "text2_strong"),
    ("QLabel#phonetic", "text2_strong"),
    # `#section` 与 `#sectionTitle` 现在是同一条规则、同一个色（见 `_QSS`），
    # 归到 `text2_strong` 而不是 `text3_strong`——最透档要换的加强色必须**跟着
    # 基础色走**，否则会出现「平时是 text2、最透档反而换成另一个更浅的档」。
    ("QLabel#section", "text2_strong"),
    ("QLabel#sectionTitle", "text2_strong"),
    ("QLabel#collGloss", "text2_strong"),
    ("QLabel#collExample", "text2_strong"),
    ("QLabel#placeholder", "text3_strong"),
)


def _veil_rules(pal: dict[str, object]) -> str:
    """生成「面板按档位铺底色」与「最透档加深次要文字」的规则块。"""
    targets = (
        ("QFrame#panel", _rgb(pal["surface"])),
        ("QListWidget", _rgb(pal["track"])),
        ("QStatusBar", _rgb(pal["bg"])),
        ("QPushButton#bare", _rgb(pal["surface"])),
        ("QLabel#countLabel", _rgb(pal["surface"])),
        # 后两项是「选中态」的特殊底：`#mode` 未选中时垫面板色，选中时垫**强调色
        # 的浅底**——否则最透那一档会把选中态的半透明底统一刷成面板色，选中与
        # 未选中又变得一模一样（这正是加 `:checked` 之前的状态）。
        # 顺序有讲究：`("QPushButton#mode", ...)` 必须**留在最后一条**，
        # 变异脚本有一条锚点就是拿「它 + 收尾括号」定位的。
        ("QPushButton#review", _rgb(pal["surface"])),
        ("QPushButton#refresh", _rgb(pal["surface"])),
        ("QPushButton#mode:checked", _rgb(pal["accent_soft"])),
        ("QPushButton#mode", _rgb(pal["surface"])),
    )
    lines: list[str] = []
    for level, percent in pal["veil"].items():  # type: ignore[union-attr]
        for target, (r, g, b) in targets:
            lines.append(
                f'QMainWindow[hasBackground="true"][bgVeil="{level}"] {target} '
                f"{{ background: rgba({r}, {g}, {b}, {percent}%); }}"
            )
    # 最「明显」那一档：直接压在面板上的次要文字换成加深一档的颜色，否则会跟
    # 透上来的背景融在一起。soft / medium 原样不动——动它们只会无端把
    # 「淡 + 细」的层次压平。
    deepest = min(pal["veil"], key=lambda name: pal["veil"][name])
    for selector, key in _STRONG_TEXT:
        r, g, b = _rgb(pal[key])
        lines.append(
            f'QMainWindow[hasBackground="true"][bgVeil="{deepest}"] {selector} '
            f"{{ color: #{r:02X}{g:02X}{b:02X}; }}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 七、生成样式表
# ---------------------------------------------------------------------------


def stylesheet(theme_id: object = THEME_DEFAULT) -> str:
    """把模板里的 `$name` 全部换成该套主题的颜色，再拼上生成的半透明规则。"""
    pal = palette(theme_id)
    out = _QSS.template
    # 用正则替换，而不是 `str.replace`：`text2` / `text3` / `text2_strong` 这些
    # 键都**以 `text` 开头**，按 `_TEMPLATE_KEYS` 的顺序 `replace("$text", ...)`
    # 会先把 `$text2` 里的 `$text` 换成 `#1F1F1E`，留下一个 `#1F1F1E2` 这样的
    # 非法 7 位色值——Qt 解析失败、静默回退到 `$text` 的颜色，于是 `text2` 的
    # 地方全都显示成正文色，且**不报任何错**。
    out = re.sub(
        r"\$([A-Za-z_][A-Za-z0-9_]*)",
        lambda m: str(pal[m.group(1)]) if m.group(1) in pal else m.group(0),
        out,
    )
    return out + "\n" + _veil_rules(pal) + "\n"
