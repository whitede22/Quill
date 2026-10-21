"""复习会话对话框：正面看词、翻面看义、三档评分推进 SM-2。

一次会话把**到期的词**排好队（`repo.due_entries`），逐条「正面看词 → 翻面
看义 → 三档评分」，评分即落库（`repo.apply_review`）。一轮过完当前到期的全部
词，不递归加回——被评「忘了」的词会回到今天，下一轮复习自然再见。

**样式**：与欢迎页同一条纪律——不写任何字面颜色，颜色全部来自
`theme.palette()`，结构样式（按钮 / 输入框 / 卡片底）复用 QSS 里已有的
objectName（`#primary` / `#bare` / `#muted`）。本页自己的字体规则
（`#reviewTitle` / `#reviewLemma` / `#reviewHint` / …）**住在 `theme.py` 的
模板里**：早先它们是在这里内联拼出来的，同一个语义就此有了两处定义。
selfcheck 那条「不许在界面代码里出现字面颜色」的扫描盯着这里，另有一条盯着
「这份文件里不许再出现内联的样式串」。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import review
from ..core.models import Entry
from ..core.repository import Repository
from .theme import stylesheet

# 三档评分按钮：(文案, grade)。顺序即 FORGOT / FUZZY / REMEMBERED。
_GRADE_BUTTONS = (
    ("忘了", review.FORGOT),
    ("模糊", review.FUZZY),
    ("记住了", review.REMEMBERED),
)


class ReviewDialog(QDialog):
    """一轮复习会话。构造时把到期词排好队，逐条翻面评分。"""

    def __init__(
        self, repo: Repository, theme_id: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        # 单一时钟读数：due 判断与评分推进用同一个「现在」，会话内一致。
        # `datetime` 对象给 apply_review 做日期运算，`format_timestamp` 转成
        # 字符串给 due_entries 做字典序比较——两种形态都从这一个读数派生。
        self._now = datetime.now()
        self._queue: list[Entry] = repo.due_entries(review.format_timestamp(self._now))
        self._index = 0
        self._graded = 0
        self._revealed = False
        # 三个状态各自建不同的按钮，统一在 __init__ 置初值，避免测试在「完成态」
        # 去摸「翻面按钮」这种不存在的引用。
        self._btn_reveal: QPushButton | None = None
        self._btn_close: QPushButton | None = None
        self._grade_buttons: list[QPushButton] = []

        self.setWindowTitle("复习")
        self.setModal(True)
        self.setObjectName("reviewDialog")
        self.setMinimumWidth(480)

        # 样式自持（同欢迎页）：本页的字体规则在 `theme.py` 的模板里。
        self.setStyleSheet(stylesheet(theme_id))

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 22)
        root.setSpacing(8)

        self._title = QLabel("复习")
        self._title.setObjectName("reviewTitle")
        root.addWidget(self._title)

        self._progress = QLabel()
        self._progress.setObjectName("reviewProgress")
        root.addWidget(self._progress)

        # 内容区（正面 / 翻面 / 完成）与底栏（评分 / 关闭）都在这里重建。
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 6, 0, 6)
        self._body_layout.setSpacing(10)
        root.addWidget(self._body, 1)

        self._footer = QVBoxLayout()
        self._footer.setSpacing(8)
        root.addLayout(self._footer)

        self._render()

    # ---------- 状态（供测试读取，也供内部渲染分派） ----------

    @property
    def current_entry(self) -> Entry | None:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    @property
    def total(self) -> int:
        return len(self._queue)

    @property
    def graded(self) -> int:
        return self._graded

    @property
    def revealed(self) -> bool:
        return self._revealed

    # ---------- 渲染 ----------

    def _clear_layout(self, layout) -> None:
        """清空一个布局里的全部子项（递归拆嵌套布局）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _render(self) -> None:
        self._clear_layout(self._body_layout)
        self._clear_layout(self._footer)
        # 三个状态各自建不同按钮，渲染前把引用重置，避免「完成态」还持着上一
        # 状态里被 deleteLater 的控件（跨状态读它会是已析构对象）。
        self._btn_reveal = None
        self._btn_close = None
        self._grade_buttons = []
        entry = self.current_entry
        if entry is None:
            self._render_done()
            return
        self._progress.setText(
            f"第 {self._index + 1} / {len(self._queue)} 个　已评 {self._graded} 个"
        )
        if self._revealed:
            self._render_back(entry)
        else:
            self._render_front(entry)

    def _render_front(self, entry: Entry) -> None:
        lemma = QLabel(entry.lemma)
        lemma.setObjectName("reviewLemma")
        lemma.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lemma.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(lemma)

        if entry.phonetic:
            phonetic = QLabel(entry.phonetic)
            phonetic.setObjectName("reviewPhonetic")
            phonetic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addWidget(phonetic)

        hint = QLabel("先回想一下这个词的意思，再翻面看答案")
        hint.setObjectName("reviewHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body_layout.addWidget(hint)
        self._body_layout.addStretch(2)

        reveal = QPushButton("显示答案")
        reveal.setObjectName("primary")
        reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        reveal.clicked.connect(self._reveal)
        self._btn_reveal = reveal
        self._footer.addWidget(reveal)

        close = QPushButton("结束复习")
        close.setObjectName("bare")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.reject)
        self._btn_close = close
        self._footer.addWidget(close)

    def _render_back(self, entry: Entry) -> None:
        lemma = QLabel(entry.lemma)
        lemma.setObjectName("reviewLemma")
        lemma.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_layout.addWidget(lemma)

        if entry.definition_en:
            self._body_layout.addWidget(_section("英文释义"))
            en = QLabel(entry.definition_en)
            en.setObjectName("reviewBody")
            en.setWordWrap(True)
            self._body_layout.addWidget(en)

        if entry.definition_zh:
            self._body_layout.addWidget(_section("中文释义"))
            zh = QLabel(entry.definition_zh)
            zh.setObjectName("reviewBody")
            zh.setWordWrap(True)
            self._body_layout.addWidget(zh)

        if entry.example:
            self._body_layout.addWidget(_section("例句"))
            example = QLabel(entry.example)
            example.setObjectName("reviewBody")
            example.setWordWrap(True)
            self._body_layout.addWidget(example)
            if entry.example_zh:
                example_zh = QLabel(entry.example_zh)
                example_zh.setObjectName("reviewSub")
                example_zh.setWordWrap(True)
                self._body_layout.addWidget(example_zh)

        if entry.collocations:
            self._body_layout.addWidget(_section("固定搭配"))
            for coll in entry.collocations:
                text = coll.phrase
                if coll.gloss_zh:
                    text = f"{text}　{coll.gloss_zh}"
                coll_label = QLabel(text)
                coll_label.setObjectName("reviewSub")
                coll_label.setWordWrap(True)
                self._body_layout.addWidget(coll_label)

        self._body_layout.addStretch(1)

        # 评分按钮三个并排，各占一等份。忘了在左（最该顺手点到的是「记没记住」，
        # 但三档平权更诚实——这里不做主次，让用户按真实记忆状态点）。
        row = QHBoxLayout()
        row.setSpacing(8)
        self._grade_buttons: list[QPushButton] = []
        for text, grade in _GRADE_BUTTONS:
            button = QPushButton(text)
            button.setObjectName("grade")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(34)
            button.clicked.connect(lambda _=False, g=grade: self._grade(g))
            row.addWidget(button, 1)
            self._grade_buttons.append(button)
        self._footer.addLayout(row)

        close = QPushButton("结束复习")
        close.setObjectName("bare")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.reject)
        self._btn_close = close
        self._footer.addWidget(close)

    def _render_done(self) -> None:
        self._progress.setText("")
        if self._queue:
            head = QLabel("本轮复习完成")
            head.setObjectName("reviewLemma")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addStretch(1)
            self._body_layout.addWidget(head)
            note = QLabel(f"共复习 {self._graded} 个词。被评「忘了」的词会尽快再出现。")
        else:
            head = QLabel("今天没有到期的词")
            head.setObjectName("reviewLemma")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addStretch(1)
            self._body_layout.addWidget(head)
            note = QLabel("查词即自动进入复习计划，到期的词会出现在这里。")
        note.setObjectName("reviewHint")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setWordWrap(True)
        self._body_layout.addWidget(note)
        self._body_layout.addStretch(2)

        close = QPushButton("关闭")
        close.setObjectName("primary")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        self._btn_close = close
        self._footer.addWidget(close)

    # ---------- 动作 ----------

    def _reveal(self) -> None:
        self._revealed = True
        self._render()

    def _grade(self, grade: int) -> None:
        entry = self.current_entry
        if entry is None or entry.id is None:
            return
        self._repo.apply_review(entry.id, grade, self._now)
        self._graded += 1
        self._index += 1
        self._revealed = False
        self._render()


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("reviewSection")
    return label
