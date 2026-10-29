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
    QHBoxLayout,
    QLabel,
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

        summary = QLabel("\n".join(report.summary_lines(payload)))
        summary.setWordWrap(True)
        layout.addWidget(summary)

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

        self._note = QLabel()
        self._note.setObjectName("warn")
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        layout.addSpacing(4)
        layout.addLayout(self._buttons(mode))

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
        target = self._target_path()
        if target is None:
            return
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

