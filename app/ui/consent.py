"""首次启动的统计告知页，以及设置里「再看一遍说明」复用的同一个对话框。

两条容易被做错的边界，这里各有对策：

1. **「已经问过」与「现在开着」是两件事**。只用一个 bool 的话，用户拒绝之后
   每次启动都会被重新问一遍——因为他「关着统计」。所以配置里是两个字段：
   `analytics_consent_decided` 记问没问过，`analytics_enabled` 记开没开。
2. **看过说明不等于同意**。本对话框只是把事实摆出来，用户点了哪个按钮才写哪个
   值；关掉窗口（Esc / 右上角）视为「还没表态」，下次启动会再问一次。这一条
   是刻意的：把「无视弹窗」当成同意，是这类功能最常见的越界方式。

样式不写字面颜色：全部复用 `theme.stylesheet()` 里已有的 objectName
（`welcomeTitle` / `muted` / `primary` / `bare`），与欢迎页同源。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import stylesheet

_TITLE = "帮助我们改进 Quill"

_PARAGRAPHS: list[str] = [
    "Quill 还在早期测试阶段。我们想知道大家实际在用什么，才好决定下一步改哪里。",
    "启用本地使用统计后，Quill 会在这台电脑上记下经过筛选的功能使用情况："
    "查了几次词、每次耗时多久、复习了几轮、导出用了哪种格式、出错时的编号。",
    "不记录具体生词、释义、笔记、例句正文、剪贴板内容和接口密钥。"
    "数据默认只留在本机，不会自动上传。",
    "随时可以在设置里关掉统计、看摘要、导出或删除记录。"
    "想帮忙的话，可以主动生成一份报告，从 GitHub 仓库提交给我们。",
]

_FOOTNOTE = "启用统计不等于同意上传。报告只在你点「提交」时生成，生成前可以先看内容。"


class ConsentDialog(QDialog):
    """统计告知页。

    `review_only=True` 时只用于「再看一遍说明」，不提供选择按钮，也不写配置。
    """

    def __init__(
        self,
        theme_id: str,
        parent: QWidget | None = None,
        *,
        review_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用统计" if review_only else _TITLE)
        self.setModal(True)
        self.setObjectName("consent")
        self.setStyleSheet(stylesheet(theme_id))
        self.setFixedWidth(520)

        # None 表示「没表态」。调用方据此决定要不要写配置。
        self.choice: bool | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 30, 36, 22)
        layout.setSpacing(0)

        title = QLabel(_TITLE)
        title.setObjectName("welcomeTitle")
        layout.addWidget(title)
        layout.addSpacing(16)

        for text in _PARAGRAPHS:
            body = QLabel(text)
            body.setWordWrap(True)
            body.setObjectName("featureBody")
            layout.addWidget(body)
            layout.addSpacing(10)

        layout.addSpacing(4)
        note = QLabel(_FOOTNOTE)
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addSpacing(18)

        layout.addLayout(self._buttons(review_only))

    def _buttons(self, review_only: bool) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        if review_only:
            close = QPushButton("关闭")
            close.setObjectName("primary")
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setMinimumWidth(110)
            close.clicked.connect(self.reject)
            row.addWidget(close)
            row.addStretch(1)
            return row

        agree = QPushButton("知道了，继续使用")
        agree.setObjectName("primary")
        agree.setCursor(Qt.CursorShape.PointingHandCursor)
        agree.setMinimumWidth(150)
        agree.clicked.connect(lambda: self._pick(True))

        decline = QPushButton("暂不参与")
        decline.setObjectName("bare")
        decline.setCursor(Qt.CursorShape.PointingHandCursor)
        decline.clicked.connect(lambda: self._pick(False))

        row.addWidget(agree)
        row.addWidget(decline)
        row.addStretch(1)
        return row

    def _pick(self, value: bool) -> None:
        self.choice = value
        self.accept()
