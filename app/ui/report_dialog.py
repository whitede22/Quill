"""使用报告的预览与保存 / 提交。

**先看后存**是这里唯一值得坚持的交互顺序：报告是用户替我们做的无偿工作，
他有权在文件落地之前知道里面有什么。所以对话框把「包含什么、不包含什么」
摆在按钮上面，而不是塞进帮助文档。

提交模式下对话框只负责落盘，发信由调用方在后台线程完成。网络请求放在这里
会把对话框卡住，而等待二十五秒的界面与死掉没有区别。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import report
from .theme import stylesheet


class ReportDialog(QDialog):
    """`mode="save"` 让用户挑位置；`mode="submit"` 存到默认目录，由调用方发送。"""

    def __init__(
        self,
        theme_id: str,
        payload: dict,
        parent: QWidget | None = None,
        *,
        mode: str = "save",
        submitter: str = "",
    ) -> None:
        super().__init__(parent)
        self._payload = payload
        self._mode = mode
        self.setWindowTitle("提交使用报告" if mode == "submit" else "使用报告")
        self.setModal(True)
        self.setObjectName("report")
        self.setStyleSheet(stylesheet(theme_id))
        self.setMinimumWidth(520)

        # 保存成功后的绝对路径。调用方用它去状态栏或提示里说清楚文件在哪。
        self.saved_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 18)
        layout.setSpacing(10)

        head = QLabel(
            "报告已生成，还没有保存。下面是它的全部内容概览。" if mode == "save"
            else "报告会存一份到本机，然后把内容发给作者。发送失败时本地这份还在。"
        )
        head.setWordWrap(True)
        head.setObjectName("featureBody")
        layout.addWidget(head)

        # 摘要只在这一刻算一次，之后不随输入重算：它跟着署名与备注增减行数，
        # 每敲一个字就让窗口改高矮，比少显示两行更让人分心。表单里的两行去向
        # 写在输入框下面，不靠实时预览来说明。
        self._summary = QLabel("\n".join(report.summary_lines(payload)))
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        privacy = payload.get("privacy") or {}
        includes = "、".join(privacy.get("includes") or [])
        excludes = "、".join(privacy.get("excludes") or [])
        if includes:
            note_in = QLabel(f"报告里有：{includes}。")
            note_in.setObjectName("muted")
            note_in.setWordWrap(True)
            layout.addWidget(note_in)
        if excludes:
            note_out = QLabel(f"报告里没有：{excludes}。")
            note_out.setObjectName("muted")
            note_out.setWordWrap(True)
            layout.addWidget(note_out)

        if mode == "submit":
            how = QLabel(
                "报告经中转服务发出，邮件正文是上面几行摘要，完整 JSON 作为附件。"
                "不经过 GitHub，也不需要注册账号。"
            )
            how.setObjectName("muted")
            how.setWordWrap(True)
            layout.addWidget(how)
            layout.addWidget(self._build_contact(submitter))

        self._note = QLabel()
        self._note.setObjectName("warn")
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        layout.addSpacing(4)
        layout.addLayout(self._buttons(mode))

    def _build_contact(self, submitter: str) -> QWidget:
        """署名与备注的输入区。

        署名必填：附件是匿名的，没有署名时作者想追问一句都不知道问谁。
        备注选填：愿意写就写，不想写也不拦——肯点提交本身已经是帮忙了。
        """
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.setSpacing(4)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit(submitter)
        self.name_edit.setMaxLength(report.MAX_SUBMITTER)
        self.name_edit.setPlaceholderText("昵称就行，作者要靠它认出你是谁")
        self.name_edit.textChanged.connect(self._sync_payload)

        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText("哪里不好用、缺什么功能，写一句也算数")
        self.note_edit.setFixedHeight(68)
        self.note_edit.textChanged.connect(self._trim_note)

        where = QLabel("这两行跟着摘要进邮件正文，也写进附件。")
        where.setObjectName("muted")
        where.setWordWrap(True)

        form.addRow("怎么称呼你", self.name_edit)
        form.addRow("想说的话", self.note_edit)
        outer.addLayout(form)
        outer.addWidget(where)
        return box

    def _trim_note(self) -> None:
        """备注超出上限就地截断。

        `QPlainTextEdit` 没有 `setMaxLength`，只能在变化后截。截完要恢复光标，
        否则光标跳到开头，接着打字会从第一字开始插。
        """
        text = self.note_edit.toPlainText()
        if len(text) <= report.MAX_NOTE:
            self._sync_payload()
            return
        cursor = self.note_edit.textCursor()
        position = cursor.position()
        self.note_edit.blockSignals(True)
        self.note_edit.setPlainText(text[: report.MAX_NOTE])
        self.note_edit.blockSignals(False)
        cursor.setPosition(min(position, report.MAX_NOTE))
        self.note_edit.setTextCursor(cursor)
        self._sync_payload()

    def _sync_payload(self) -> None:
        """把输入框里的两项随时写进报告。

        落盘发生在按按钮那一刻，而发送用的是同一个 dict，所以它必须在输入时
        就保持最新——只在 `_do_save` 里写一次的话，二次打开对话框、改了名字
        再提交，发出去的还是上一次的署名。
        """
        report.attach_context(self._payload, self.submitter, self.note_text)

    @property
    def submitter(self) -> str:
        """当前填的署名。保存模式下没有这个输入框，返回空串。"""
        edit = getattr(self, "name_edit", None)
        return edit.text().strip() if edit is not None else ""

    @property
    def note_text(self) -> str:
        """当前填的备注，同样是保存模式下的空串。"""
        edit = getattr(self, "note_edit", None)
        return edit.toPlainText().strip() if edit is not None else ""

    def _buttons(self, mode: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        primary = QPushButton("保存并提交" if mode == "submit" else "选择位置保存")
        primary.setObjectName("primary")
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        primary.setMinimumWidth(150)
        primary.clicked.connect(self._do_save)

        cancel = QPushButton("取消")
        cancel.setObjectName("bare")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)

        row.addWidget(primary)
        row.addWidget(cancel)
        row.addStretch(1)
        return row

    def _target_path(self) -> Path | None:
        """保存位置。返回 None 表示用户取消了选择。"""
        folder = report.default_dir()
        name = report.default_name()
        if self._mode == "submit":
            return folder / name
        chosen, _ = QFileDialog.getSaveFileName(
            self, "保存使用报告", str(folder / name), "JSON (*.json)"
        )
        return Path(chosen) if chosen else None

    def _do_save(self) -> None:
        if self._mode == "submit" and not self.submitter:
            # 拦在这里而不是拦在发信之后：报告一旦发出就收不回来，而署名是唯一
            # 能让作者回信的东西。提示留在对话框里，填完可以接着提交。
            self._note.setText("先填一个称呼，作者要知道是谁发的。")
            self._note.setVisible(True)
            self.name_edit.setFocus()
            return

        target = self._target_path()
        if target is None:
            return
        self._sync_payload()
        try:
            path = report.write_report(self._payload, target)
        except OSError as exc:
            # 写不进去是磁盘 / 权限问题，用户能处理（换个位置、清空间），
            # 所以如实说出来并留在对话框里，不关窗。
            self._note.setText(f"保存失败：{exc}")
            self._note.setVisible(True)
            return

        self.saved_path = path
        self.accept()

