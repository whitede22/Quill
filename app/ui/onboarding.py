"""首次启动的欢迎页 + 可重看的教程。

**触发**：主窗口启动时拿配置里的 `onboard_version` 与 `CURRENT_ONBOARD_VERSION`
比较，配置值更小就弹出一次，并把新版本号写回。版本号递增（而不是 bool 的
「看过没有」），所以教程以后大改，老用户也能看到新版。

**样式**：不写任何字面颜色——颜色全部来自 `theme.palette()` 或复用 QSS 里已有的
objectName（`#primary` / `#bare` / `#muted` / `#sectionTitle`）。这一页自己的字体
规则（`#welcomeTitle` / `#welcomeSub` / `#featureTitle` / `#featureBody`）**住在
`theme.py` 的模板里**，与主界面的规则同源：早先它们是在这里内联拼出来的，
同一个语义就此有了两处定义。selfcheck 那条「不许在界面代码里出现字面颜色」的
扫描仍然盯着这里，另有一条盯着「这份文件里不许再出现内联的样式串」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .theme import stylesheet

# 教程版本号。每次大改教程内容就 +1。文案措辞的小改不算，整页重写才算。
CURRENT_ONBOARD_VERSION = 2

_WELCOME_TITLE = "欢迎来到 Quill"
_WELCOME_SUBTITLE = "回车查词，顺手收藏。"
_MADE_BY = "made by wtd22"

# 欢迎页的功能点：(标题, 说明)。前 4 条是**已经能用**的功能，第 5 条是预告位。
#
# 预告位只写还没做的东西。M2 复习模式上线后，这一条原本写着的「复习模式正在
# 路上」就成了假话——左栏明明摆着「待复习 N」。所以现在只留学习统计。
#
# 文案长度是量过的：可排区 448px、一个全角宽 13.0px，预算 34 个全角宽，最长一句
# 32（功能点 2 与教程第 2 步，各 416px）。中文换行能从任意两字之间断开，
#「点「收 / 藏」进词组」这种断法比多占一行难看得多；`selfcheck` 里那条「每句说明
# 都排得下一行」的断言盯着这件事。
_FEATURES: list[tuple[str, str]] = [
    ("查词即收藏", "按回车就查，查到的自己进生词本，不用点保存"),
    (
        "搭配与例句",
        "be in charge of 和 take charge of 差在哪，看例句；"
        "顺眼的收进词组",
    ),
    ("七种导出格式", "纯文本、表格、Anki 卡片都在里面，换工具不丢词"),
    ("主题与背景", "六套主题挑一套顺眼的，或者铺自己的图"),
    ("未完待续", "学习统计排在后面。这页以后改版，会重新弹给你看"),
]

# 教程的四步，与欢迎页前四个功能点一一对应（预告位不进教程）。
# 标题不带「第一步」——序号由调用处的 `f"{i}　{head}"` 加。
_TUTORIAL: list[tuple[str, str]] = [
    (
        "查词与收藏",
        "按回车查词，查到的词进生词本，不用点保存。左栏词表，右栏详情。",
    ),
    (
        "搭配和例句",
        "查词时右栏给出固定搭配，每条配一句例句；点「收藏」收进词组收藏。",
    ),
    (
        "想带走就导出",
        "左上角「导出」有七种，比如 Markdown、PDF、表格和 Anki 卡片。",
    ),
    (
        "界面随你",
        "设置里挑一套主题，也可以铺自己的背景图，再调背景透出来多少。",
    ),
]


class OnboardingDialog(QDialog):
    """欢迎页 + 教程的合体对话框，用 `QStackedWidget` 切两页。"""

    def __init__(
        self,
        theme_id: str,
        parent: QWidget | None = None,
        start_at_tutorial: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_WELCOME_TITLE)
        self.setModal(True)
        self.setObjectName("onboarding")
        # 样式自持：不依赖 QApplication 的全局样式表，这样 gui_smoke 直接构造
        # MainWindow 时弹出来的欢迎页也能拿到正确配色。本页自己的字体规则在
        # `theme.py` 的模板里（`QDialog#onboarding` / `#welcomeTitle` / …）。
        self.setStyleSheet(stylesheet(theme_id))

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_welcome())
        self._stack.addWidget(self._build_tutorial())
        self._stack.setCurrentIndex(1 if start_at_tutorial else 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 30, 36, 22)
        layout.setSpacing(0)
        layout.addWidget(self._stack)

        self.setFixedWidth(520)

    # ---------- 欢迎页 ----------

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel(_WELCOME_TITLE)
        title.setObjectName("welcomeTitle")
        v.addWidget(title)

        sub = QLabel(_WELCOME_SUBTITLE)
        sub.setObjectName("welcomeSub")
        v.addWidget(sub)
        v.addSpacing(22)

        for head, body in _FEATURES:
            v.addWidget(self._feature_row(head, body))
            v.addSpacing(14)
        v.addStretch(1)

        v.addLayout(self._button_row(primary_text="开始使用", secondary_text="先看看教程"))
        v.addSpacing(14)

        made = QLabel(_MADE_BY)
        made.setObjectName("muted")
        made.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(made)

        return page

    # ---------- 教程页 ----------

    def _build_tutorial(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel("四步看懂 Quill")
        title.setObjectName("welcomeTitle")
        v.addWidget(title)
        v.addSpacing(22)

        for i, (head, body) in enumerate(_TUTORIAL, start=1):
            head_label = QLabel(f"{i}　{head}")
            head_label.setObjectName("featureTitle")
            v.addWidget(head_label)
            v.addSpacing(4)
            body_label = QLabel(body)
            body_label.setObjectName("featureBody")
            body_label.setWordWrap(True)
            v.addWidget(body_label)
            v.addSpacing(16)
        v.addStretch(1)

        v.addLayout(self._button_row(primary_text="开始使用", secondary_text="返回欢迎页"))
        return page

    # ---------- 共用部件 ----------

    def _feature_row(self, head: str, body: str) -> QWidget:
        row = QWidget()
        r = QVBoxLayout(row)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(3)

        head_label = QLabel(head)
        head_label.setObjectName("featureTitle")
        r.addWidget(head_label)

        body_label = QLabel(body)
        body_label.setObjectName("featureBody")
        body_label.setWordWrap(True)
        r.addWidget(body_label)
        return row

    def _button_row(self, primary_text: str, secondary_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        primary = QPushButton(primary_text)
        primary.setObjectName("primary")
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        primary.setMinimumWidth(110)

        secondary = QPushButton(secondary_text)
        secondary.setObjectName("bare")
        secondary.setCursor(Qt.CursorShape.PointingHandCursor)

        row.addWidget(primary)
        row.addWidget(secondary)
        row.addStretch(1)

        primary.clicked.connect(self.accept)
        secondary.clicked.connect(self._on_secondary)
        return row

    def _on_secondary(self) -> None:
        """次按钮在欢迎页是「先看看教程」，在教程页是「返回欢迎页」。"""
        self._stack.setCurrentIndex(1 if self._stack.currentIndex() == 0 else 0)
