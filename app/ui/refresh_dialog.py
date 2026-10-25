"""批量重查的范围选择。

## 为什么不是一句「确定重查全部吗」

一次重查就是一次接口请求。全库几百条时，那是一件要花几分钟、要消耗额度的事，
而其中可能大部分本来就已经是接口内容——重查只会得到同一份结果。所以这里把
「哪些会被重查」摊开：总数、需要更新的条数、当前筛选在起什么作用。

默认选中的是「只重查需要更新的」：那才是接上接口之后真正要做的事。想全量刷
一遍也能选，但那应该是用户自己做的选择，而不是被默认值替他决定的。

## 为什么要把「不会被覆盖」写出来

重查是**覆盖写**。用户愿意按下去，靠的是相信那两件事不会发生：自己写的备注
标签被抹掉、接口失败的词条被离线内容顶掉。这两条在服务层是真的（见
`DictionaryService.lookup` 的 `accept_fallback` 与 `Repository.upsert`），
在对话框里说出来只是让它可预期——不说的话，他得先赌一把才知道。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
)


class RefreshDialog(QDialog):
    """选择重查范围。"""

    def __init__(
        self, *, total: int, outdated: int, scope: str, parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("重新查询")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        head = QLabel(f"按当前接口重新查询{scope}。")
        head.setWordWrap(True)
        layout.addWidget(head)

        self.radio_outdated = QRadioButton(
            f"只重查需要更新的（{outdated} 条）"
        )
        self.radio_outdated.setToolTip(
            "需要更新指：内容是本机离线词库来的，或由旧版提示词生成"
        )
        self.radio_all = QRadioButton(f"全部重查（{total} 条）")

        if outdated > 0:
            self.radio_outdated.setChecked(True)
        else:
            # 没有可更新的词条时把它置灰，而不是留一个勾了也没用的选项——
            # 「勾了不生效」比没有这个选项更难理解。
            self.radio_outdated.setEnabled(False)
            self.radio_outdated.setText("只重查需要更新的（没有符合条件的词条）")
            self.radio_all.setChecked(True)

        layout.addWidget(self.radio_outdated)
        layout.addWidget(self.radio_all)

        note = QLabel(
            "重查会覆盖释义、例句这些内容字段；备注与标签是你自己的，"
            "不会被覆盖。接口这次没答上来的词条保持原样，"
            "不会被本机离线词库的内容顶掉。"
        )
        note.setObjectName("formatHint")
        note.setWordWrap(True)
        layout.addSpacing(4)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始重查")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def only_outdated(self) -> bool:
        return self.radio_outdated.isChecked()
