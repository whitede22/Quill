"""设置对话框：主题、接入方式、API 地址、模型、密钥、窗口背景图。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import KEY_PATH, check_config, load_config, save_config
from ..core import analytics, report
from ..providers import offline_dict_status, resolve_offline_dict
from ..providers.openai_compat import OpenAICompatProvider
from ..security.keystore import IS_WINDOWS, save_api_key
from .background import BackgroundRenderer
from .consent import ConsentDialog
from .hotkey import HOTKEY_TEXT
from .hotkey import available as hotkey_available
from .report_dialog import ReportDialog
from .theme import (
    BG_VEIL_DEFAULT,
    THEME_DEFAULT,
    palette,
    theme_id_of,
    theme_ids,
    veil_level,
)
from .workers import submit

# 可选的背景图格式。列出来的都是 Qt 自带 imageformats 插件能解的格式——
# 写成「所有文件」会让用户选中一个 .psd 然后得到一张空白背景，而界面上不会
# 有任何地方说明为什么。
_BG_FILTER = "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.gif);;所有文件 (*)"


def probe_config(base_url: str, model: str, api_key: str, timeout: float) -> str:
    """在工作线程里按「界面上的当前值」试探一次连接，返回一句人话。

    模块级函数，因为它**不能碰任何界面对象**：它跑在 QThreadPool 的线程里。

    刻意不用 `build_provider()`：那个函数会按配置决定「挂不挂离线兜底」，
    而兜底一旦挂上，接口挂掉时它会安静地返回离线词条——测试连接就成了
    「离线词典可用了」，恰恰答错了这个问题。
    """
    provider = OpenAICompatProvider(
        base_url=base_url, api_key=api_key, model=model, timeout=timeout
    )
    return provider.probe()

# 背景预览条的尺寸。做成宽扁的一条，像窗口的缩略：它要演示的是「面板后面的图
# 会露出多少」，一块方形的图反而看不出这层意思。
_BG_PREVIEW = QSize(224, 64)


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: dict,
        api_key: str,
        parent=None,
        on_theme_preview=None,
        on_restore=None,
        on_rebuild_index=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        # 双栏之后宽度换成了高度：880 宽在 125% 缩放的 1080p 下仍有余量，
        # 630 高对得上内容的实际需求，也不再让布局去压缩任何一行。
        self.setMinimumSize(760, 560)
        self.resize(880, 630)
        self._config = dict(config)
        self._init_callbacks(on_theme_preview, on_restore, on_rebuild_index, config)
        # 提交在后台线程跑，期间禁用按钮，避免连点发出多封邮件。
        self._report_sending = False
        self._report_btn: QPushButton | None = None
        self._build_provider_fields(config, api_key)
        form = self._build_form(config)
        offline_hint, storage = self._build_storage_notes()
        self._build_switches(config)
        self._build_analytics(config)
        self._build_notice()
        self._build_actions()
        self._assemble(form, offline_hint, storage)

    def _init_callbacks(
        self, on_theme_preview, on_restore, on_rebuild_index, config: dict
    ) -> None:
        """把只有主窗口才有的能力接进来。为 None 时对应的按钮不出现。

        「从备份恢复」需要一个能关闭数据库连接的地方，「重建搜索索引」要拿仓储
        连接并提交事务，主题即时预览要重绘主窗口——这三件事都只有 main 有。
        回调为 None 时（自检、截图脚本）不显示按钮，而不是显示一个点了只报错的
        按钮。
        """
        self._on_restore = on_restore
        # 回调返回重建后的条数，失败则抛异常，由这里就地显示结果。
        self._on_rebuild_index = on_rebuild_index
        # 默认 None 时退化成「保存后才生效」，这样自检 / 截图脚本沿用旧调用
        # 方式也不会坏。
        self._on_theme_preview = on_theme_preview
        self._theme_original = theme_id_of(config.get("theme"))

    def _build_provider_fields(self, config: dict, api_key: str) -> None:
        """接入方式、地址、模型名、密钥与「测试连接」。"""
        self.mode = QComboBox()
        self.mode.addItem("OpenAI 兼容接口", "openai-compat")
        self.mode.addItem("离线词典（不联网，查本地词库）", "offline")
        self.mode.addItem("离线示例（仅 3 个演示词条）", "mock")
        index = self.mode.findData(config.get("provider", "openai-compat"))
        self.mode.setCurrentIndex(index if index >= 0 else 0)

        self.base_url = QLineEdit(config.get("base_url", ""))
        # 占位提示只写「到版本号为止」这一条规则，不写死某家厂商的地址：
        # 填 OpenAI 还是 DeepSeek 都合法，写死一家会让另一家的用户以为不能填
        # 自己那家。
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.model = QLineEdit(config.get("model", ""))
        self.model.setPlaceholderText("deepseek-chat")
        self.api_key = QLineEdit(api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-...")

        # 「测试连接」。语法校验挡不住「格式对但域名写错 / 密钥失效 / 模型名不
        # 存在」，而那三种要等**下一次查词失败**才暴露——那时用户早已忘了自己
        # 刚改过设置。这个按钮把那一刻提前到当场。
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setObjectName("bare")
        self.btn_test.setToolTip(
            "发一个最小请求（只花 1 个 token），确认地址、密钥与模型名真的能用。"
        )
        self.key_row = QWidget()
        key_layout = QHBoxLayout(self.key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.api_key, 1)
        key_layout.addWidget(self.btn_test)

        self._probe_note = QLabel()
        self._probe_note.setObjectName("muted")
        self._probe_note.setWordWrap(True)
        self._probe_note.setVisible(False)

    def _build_form(self, config: dict) -> QFormLayout:
        """左栏的表单。窗口背景不在这里——它自成一块，右栏单独摆。"""
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)
        form.addRow("界面主题", self._build_theme_row(config))
        form.addRow("接入方式", self.mode)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名称", self.model)
        form.addRow("API 密钥", self.key_row)
        self._background = self._build_background_row(config)
        return form

    def _build_switches(self, config: dict) -> None:
        """两个行为开关：接口兜底与托盘常驻。"""
        self.fallback = QCheckBox("接口请求失败时，自动改用离线词库")
        self.fallback.setChecked(bool(config.get("offline_fallback", True)))
        self.fallback.setToolTip(
            "接口挂掉、额度用尽或断网时，仍然能查到词。\n"
            "兜底内容会在详情里标明来自离线词库，不会冒充接口结果。"
        )

        # 托盘自选。热键的价值在「窗口没开着的时候按一下」，所以默认开启；
        # 但「关掉窗口程序还在跑」必须由用户自己同意，设置里能关。
        # 非 Windows 平台没有全局热键，开关置灰并说明原因——留一个点了没反应的
        # 勾选框，比没有这个勾选框更让人困惑。
        self.tray = QCheckBox(f"关闭窗口后留在托盘，按 {HOTKEY_TEXT} 随时唤出")
        self.tray.setChecked(bool(config.get("tray_enabled", True)))
        if hotkey_available():
            self.tray.setToolTip(
                f"开启后关闭窗口只是收进托盘，{HOTKEY_TEXT} 能把窗口叫回来，"
                "输入框里会自动填好剪贴板里的那个词。\n"
                "想彻底退出，用托盘菜单里的「退出」。"
            )
        else:
            self.tray.setChecked(False)
            self.tray.setEnabled(False)
            self.tray.setText("关闭窗口后留在托盘（仅 Windows 支持全局热键）")
            self.tray.setToolTip("当前平台没有系统级热键，关闭窗口即退出程序。")

    def _build_analytics(self, config: dict) -> None:
        """使用统计的开关与四个次要入口。

        开关和「删除统计」摆在一起：用户判断不了「到底记了什么」，但看得懂
        「有多少条、能不能删掉」。摘要与报告把前半句补上。

        「提交报告」不在这排里。它跟这四个不是同一类动作：那四个都在本机发生，
        它是唯一一个把数据送出去的。同级摆放的结果是它被当成第五个可选项，
        所以单独做成一块，见 `_build_notice`。
        """
        self.analytics = QCheckBox("在这台电脑上记录使用情况（不上传）")
        self.analytics.setChecked(bool(config.get("analytics_enabled", False)))
        self.analytics.setToolTip(
            "只记功能使用次数与耗时，不记生词、释义、笔记、剪贴板内容和密钥。\n"
            "数据存在本机，不会自动发送；想提交报告得自己点「提交报告」。"
        )

        self.analytics_row = QWidget()
        stat_layout = QHBoxLayout(self.analytics_row)
        stat_layout.setContentsMargins(0, 0, 0, 0)
        stat_layout.setSpacing(6)
        for text, slot, tip in (
            ("查看摘要", self._show_summary, "这台电脑到目前为止记了多少条、都是哪几类。"),
            ("导出报告", self._export_report, "存成 JSON，位置由你选；文件里有什么会先列出来。"),
            ("删除统计", self._clear_stats, "只删统计数据，词库、笔记、复习进度不受影响。"),
            ("隐私说明", self._show_privacy, "重新看一遍记录范围与用途。"),
        ):
            button = QPushButton(text)
            button.setObjectName("bare")
            button.setToolTip(tip)
            button.clicked.connect(slot)
            stat_layout.addWidget(button)
        stat_layout.addStretch(1)

    def _build_notice(self) -> None:
        """「提交报告」独立成块。

        摆在一排次要按钮里的后果是：用户扫过去只看到五个一样大的词，而不明白
        这一下会把东西发到别人的邮箱。所以整块用强调色铺底描边，抬头直接说
        「需要你的帮助」——这句话是这一块存在的理由，放在最上面比放在说明里
        更容易被读到。正文写清发什么、不发什么，按钮换成主色，
        「先看看要发什么」留在旁边给不肯盲点的人。
        """
        notice = QFrame()
        notice.setObjectName("notice")
        layout = QHBoxLayout(notice)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)

        text = QVBoxLayout()
        text.setSpacing(3)
        ask = QLabel("我们需要你的帮助！")
        ask.setObjectName("noticeTitle")
        title = QLabel("把使用情况发给作者")
        title.setObjectName("sectionTitle")
        body = QLabel(
            "发出统计摘要和一份 JSON 附件，不含生词、释义、笔记、剪贴板内容和密钥。"
            "本机会留一份，不需要时可以删掉。"
        )
        body.setObjectName("muted")
        body.setWordWrap(True)
        text.addWidget(ask)
        text.addWidget(title)
        text.addWidget(body)

        self._report_btn = QPushButton("提交报告")
        self._report_btn.setObjectName("primary")
        self._report_btn.setToolTip(
            "存一份到本机，然后把报告发给作者；发送失败时本地这份还在。"
        )
        self._report_btn.clicked.connect(self._submit_report)

        peek = QPushButton("先看看要发什么")
        peek.setObjectName("bare")
        peek.setToolTip("打开摘要，看看这台电脑记了多少条、都是哪几类。")
        peek.clicked.connect(self._show_summary)

        actions = QVBoxLayout()
        actions.setSpacing(0)
        actions.addWidget(self._report_btn)
        actions.addWidget(peek, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addLayout(text, 1)
        layout.addLayout(actions)
        self._notice = notice

    def _build_storage_notes(self) -> tuple[QLabel, QLabel]:
        """离线词库与密钥存储两条说明，内容都来自运行时探测。"""
        # 离线词库的规模直接摆在设置里：用户要先知道「本地这本词典有多少词」，
        # 才能判断该不该切过去，而不是切过去发现查不到再生疑。
        offline = resolve_offline_dict()
        self._offline_missing = offline is None
        offline_hint = QLabel(
            "本地词库："
            + offline_dict_status(offline)
            + (f"\n位置：{offline}" if offline else "\n未找到词库文件")
        )
        offline_hint.setObjectName("muted")
        offline_hint.setWordWrap(True)

        storage = QLabel(
            "密钥使用系统级加密（Windows DPAPI）保存在本机，"
            "不会以明文写入配置文件。"
            if IS_WINDOWS
            else "当前平台不支持 DPAPI，密钥将以 Base64 形式保存，请注意本机安全。"
        )
        storage.setObjectName("muted")
        storage.setWordWrap(True)
        return offline_hint, storage

    def _build_actions(self) -> None:
        """恢复、重建索引、重新看教程，以及两条就地显示的提示。"""
        # 备份按钮在主工具栏，恢复放这里：恢复会覆盖数据，不该和「备份」并排
        # 摆成一个手快点错的位置。
        self.btn_restore = QPushButton("从备份恢复…")
        self.btn_restore.setObjectName("bare")
        self.btn_restore.setToolTip(
            "用一份 .db 备份覆盖当前生词本。覆盖前会自动把当前数据另存一份。"
        )
        self.btn_restore.clicked.connect(self._restore_backup)
        self.btn_restore.setVisible(self._on_restore is not None)

        # 索引与词条表是两份数据，靠写入时手工同步。同步漏了不会报错，只是搜不到，
        # 所以给一个用户自己就能按的按钮，不必等维护者。词条内容一个都不会变，
        # 只是按现存的词条重新生成一遍索引。
        self.btn_rebuild_index = QPushButton("重建搜索索引")
        self.btn_rebuild_index.setObjectName("bare")
        self.btn_rebuild_index.setToolTip(
            "搜索查不到本来存在的词时点这里。按当前词条重新生成搜索索引，"
            "不改动任何词条内容。"
        )
        self.btn_rebuild_index.clicked.connect(self._rebuild_index)
        self.btn_rebuild_index.setVisible(self._on_rebuild_index is not None)

        # 结果就地显示，与校验提示同一个道理：弹窗会把设置对话框关掉
        self._rebuild_note = QLabel()
        self._rebuild_note.setObjectName("muted")
        self._rebuild_note.setWordWrap(True)
        self._rebuild_note.setVisible(False)

        # 校验提示：就地显示在对话框里，不弹窗——弹窗会把对话框关掉，
        # 用户还得重新填一遍
        self._error = QLabel()
        self._error.setObjectName("warn")
        self._error.setWordWrap(True)
        self._error.setVisible(False)

        # 重新查看教程：欢迎页 / 教程随时能再看一遍，不必等到版本号递增。
        self.btn_tutorial = QPushButton("重新查看教程")
        self.btn_tutorial.setObjectName("bare")
        self.btn_tutorial.setToolTip("重新打开首次启动时的欢迎页与上手教程。")
        self.btn_tutorial.clicked.connect(self._show_tutorial)

    @staticmethod
    def _section_label(text: str) -> QLabel:
        """栏头的分区标题。与详情区的 section 共用同一条样式。"""
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _build_bottom_bar(self) -> QWidget:
        """统计的开关与入口、维护三件事，同一行左右分列。

        这两组都不常用，但性质不同：左边看和清统计，右边修数据。上下堆叠时
        各自只占半行宽，却各要一整行高度，是这次纵向压缩里最划算的一处。
        """
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        stats = QVBoxLayout()
        stats.setSpacing(2)
        stats.addWidget(self.analytics)
        stats.addWidget(self.analytics_row)

        tools = QVBoxLayout()
        tools.setSpacing(2)
        tools.addStretch(1)
        tools_row = QHBoxLayout()
        tools_row.setSpacing(6)
        # 靠右收边：自检与截图脚本没有恢复/重建的回调，只剩「重新查看教程」
        # 一个按钮，靠左会悬在栏中间，看着像漏了东西。
        tools_row.addStretch(1)
        for button in (self.btn_restore, self.btn_rebuild_index, self.btn_tutorial):
            tools_row.addWidget(button)
        tools.addLayout(tools_row)
        tools.addWidget(self._rebuild_note)
        tools.addStretch(1)

        layout.addLayout(stats, 3)
        # 两组的动作性质不同（看和清统计 / 修数据），中间立一条竖线分界，
        # 不然在浅色下只是两串小字并排，读起来像同一组。
        divider = QLabel()
        divider.setObjectName("divider")
        divider.setFixedWidth(1)
        layout.addWidget(divider)
        layout.addLayout(tools, 2)
        return holder

    def _assemble(
        self, form: QFormLayout, offline_hint: QLabel, storage: QLabel
    ) -> None:
        """装配并接上最后三个信号。

        分成两栏是因为这些内容本来就不是一条链：左边是「怎么查到词」，右边是
        「看起来什么样、词库在哪」。单列排下来要 990 高，超过不少笔记本的
        可用高度，而两栏各自都在 320 上下。
        """
        hint = QLabel(
            "任何兼容 OpenAI 协议的服务都能接入：OpenAI、DeepSeek、Moonshot、"
            "通义、智谱，或本机的 Ollama / vLLM 等。\n"
            "地址填到版本号为止（如 https://api.deepseek.com/v1），"
            "程序会自动补全 /chat/completions。\n"
            "具体地址与模型名以你所选服务商的文档为准。"
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self._section_label("模型接入"))
        left.addLayout(form)
        left.addWidget(hint)
        left.addWidget(self.fallback)
        left.addWidget(self.tray)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._section_label("窗口背景"))
        right.addWidget(self._background)
        right.addWidget(offline_hint)
        right.addWidget(storage)
        right.addStretch(1)

        columns = QHBoxLayout()
        columns.setSpacing(24)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)
        layout.addLayout(columns, 1)
        layout.addWidget(self._notice)
        layout.addWidget(self._build_bottom_bar())
        layout.addWidget(self._probe_note)
        layout.addWidget(self._error)
        layout.addWidget(buttons)

        self.btn_test.clicked.connect(self._test_connection)
        self.mode.currentIndexChanged.connect(self._sync_enabled)
        self._sync_enabled()
    # ---------- 使用统计与反馈 ----------

    def _current_theme(self) -> str:
        """对话框里当前选中的主题。统计这几个弹窗跟设置同色，不另起一套。"""
        return self.theme.currentData() or self._theme_original

    def _show_summary(self) -> None:
        payload = report.build_report(30)
        lines = report.summary_lines(payload)
        if not payload.get("event_total"):
            lines.append("")
            lines.append(
                "还没有记录到事件。统计关闭时不会记录；刚打开的话，"
                "用几个功能再回来看就有数字了。"
            )
        QMessageBox.information(self, "使用统计", "\n".join(lines))

    def _export_report(self) -> None:
        payload = report.build_report(30)
        dialog = ReportDialog(self._current_theme(), payload, self, mode="save")
        if dialog.exec() and dialog.saved_path:
            QMessageBox.information(
                self, "使用报告", f"已保存到：\n{dialog.saved_path}"
            )

    def _submit_report(self) -> None:
        # 提交是第二个独立决定：统计开着也不代表愿意发出去，所以这里不默认放行。
        if not self.analytics.isChecked():
            QMessageBox.information(
                self,
                "提交使用报告",
                "提交之前先勾选「在这台电脑上记录使用情况」。\n"
                "记录与提交是两个决定，程序不替你合并。",
            )
            return
        if self._report_sending:
            QMessageBox.information(
                self, "提交使用报告", "上一份报告还在发送，等它结束再点。"
            )
            return
        payload = report.build_report(30)
        if not payload.get("event_total"):
            QMessageBox.information(
                self, "提交使用报告", "还没有记录到事件，暂时没有可提交的内容。"
            )
            return
        if not report.endpoint():
            # 地址没配就没必要让用户填一遍表单再看失败。
            if self._ask_github("这个版本还没有配置提交地址，报告可以改用 GitHub 提交。"):
                QDesktopServices.openUrl(QUrl(report.issue_url(payload)))
            return
        dialog = ReportDialog(
            self._current_theme(),
            payload,
            self,
            mode="submit",
            submitter=str(self._config.get("report_name") or ""),
        )
        if not (dialog.exec() and dialog.saved_path):
            return
        self._remember_name(dialog.submitter)
        self._start_sending(payload, dialog.saved_path)

    def _remember_name(self, name: str) -> None:
        """把署名记进配置，下次预填。

        直接落盘，不等设置页的「保存」：用户提交完通常就关窗口了，等不到他
        再去点一次保存。这份写入只覆盖署名一个键，设置页里尚未保存的其他改动
        仍然按原样留在表单里。
        """
        self._config["report_name"] = name
        cfg = load_config()
        cfg["report_name"] = name
        save_config(cfg)

    def _start_sending(self, payload: dict, path: Path) -> None:
        """提交要过网络，不能占着主线程，否则界面在等待期间完全不响应。"""
        self._report_sending = True
        if self._report_btn is not None:
            self._report_btn.setEnabled(False)
            self._report_btn.setText("提交中…")
        submit(
            report.submit,
            payload,
            on_success=lambda _: self._report_sent(path),
            on_error=lambda message, kind: self._report_failed(message, path, payload),
            on_finished=self._reset_report_button,
        )

    def _reset_report_button(self) -> None:
        self._report_sending = False
        if self._report_btn is not None:
            self._report_btn.setEnabled(True)
            self._report_btn.setText("提交报告")

    def _report_sent(self, path: Path) -> None:
        QMessageBox.information(
            self,
            "提交使用报告",
            f"报告已发出。\n\n本机留了一份：\n{path}\n不需要的话可以删掉。",
        )

    def _report_failed(self, message: str, path: Path, payload: dict) -> None:
        """发不出去时把话说清楚，并留一条备用通道。

        报告已经落盘，所以失败不是白干；把 GitHub 那条路摆在按钮上，
        比让用户自己去项目主页找入口强。
        """
        detail = f"{message}\n\n报告已保存在：\n{path}"
        if self._ask_github("报告没有发出去。", detail):
            QDesktopServices.openUrl(QUrl(report.issue_url(payload)))

    def _ask_github(self, text: str, detail: str = "") -> bool:
        """问一句要不要改用 GitHub 提交，返回用户的选择。"""
        box = QMessageBox(self)
        box.setWindowTitle("提交使用报告")
        box.setIcon(
            QMessageBox.Icon.Warning if detail else QMessageBox.Icon.Information
        )
        box.setText(text)
        if detail:
            box.setInformativeText(detail)
        github = box.addButton("改用 GitHub 提交", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is github

    def _clear_stats(self) -> None:
        if not analytics.total():
            QMessageBox.information(self, "删除统计", "本机还没有统计数据。")
            return
        answer = QMessageBox.question(
            self,
            "删除统计",
            "删除本机记录的全部使用统计？\n\n"
            "词库、笔记、复习进度存在另一个文件里，不会被删。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = analytics.clear()
        QMessageBox.information(self, "删除统计", f"已删除 {removed} 条记录。")

    def _show_privacy(self) -> None:
        ConsentDialog(self._current_theme(), self, review_only=True).exec()

    def _restore_backup(self) -> None:
        if self._on_restore is not None:
            self._on_restore()

    def _rebuild_index(self) -> None:
        """按当前词条重建搜索索引，结果就地显示。

        同步执行，不丢进线程池：重建动的是主连接所在的那个库，交给后台线程
        就变成两个线程写同一条连接，正是 `db.connect` 注释里记着的那类问题。
        几千条的重建在百毫秒量级，等得起；真要等到几秒的规模，那也是用户的
        词库涨到了十几万条，那时该改的是重建策略而不是搬线程。
        """
        if self._on_rebuild_index is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            count = self._on_rebuild_index()
        except Exception as exc:  # 界面层兜住：异常穿进事件循环会直接崩窗口
            message = f"重建失败：{exc}"
        else:
            message = f"已重建，索引里现在有 {count} 条。"
        finally:
            QApplication.restoreOverrideCursor()
        self._rebuild_note.setText(message)
        self._rebuild_note.setVisible(True)

    def _build_theme_row(self, config: dict) -> QWidget:
        """「界面主题」这一行：下拉框（每项带色块）+ 即时生效。

        即时生效而不是「保存后才换」：配色是**看着挑**的，要用户先保存、看一眼、
        再打开设置改，等于让他来回跑三趟。
        """
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        # 下拉 + 说明装在一个 holder 里，QFormLayout 拿到的行高就变成 holder 的
        # 最小尺寸；QComboBox 的最小高度只有十来像素，空间一紧就被压成一条线。
        # 钉住最小值，压缩落到别的行上去。
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        self.theme = QComboBox()
        for theme_id in theme_ids():
            pal = palette(theme_id)
            self.theme.addItem(self._swatch(pal["accent"]), pal["label"], theme_id)
        index = self.theme.findData(self._theme_original)
        self.theme.setCurrentIndex(index if index >= 0 else 0)
        self.theme.setToolTip(
            "换色只改强调色与底色，不动版式；切换后立即生效。\n"
            "深色主题下「背景透出」的三档取值与浅色不同——那边的字是浅色的，"
            "最坏情况正好相反。"
        )
        layout.addWidget(self.theme)

        note = QLabel("共 " + str(len(theme_ids())) + " 套；切换后立即生效，取消则还原。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.theme.currentIndexChanged.connect(self._on_theme_changed)
        return holder

    @staticmethod
    def _swatch(accent: str):
        """主题下拉框里的小色块。用强调色实心块 + 一圈同色描边。"""
        from PySide6.QtGui import QIcon  # noqa: PLC0415 - 只有这一处需要

        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor(accent).darker(120))
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(2, 2, 12, 12, 3, 3)
        painter.end()
        return QIcon(pixmap)

    def _on_theme_changed(self) -> None:
        # 预览条的蒙版底色取自主题，换主题必须跟着重画，否则它演示的还是旧底色。
        self._refresh_bg_preview()
        if self._on_theme_preview is not None:
            self._on_theme_preview(self.theme.currentData())

    def reject(self) -> None:
        """取消时把主题还原。

        主题是即时生效的：用户试了几套之后按取消，若什么都不做，界面会停在他最后
        试的那套上，而配置里存的仍是原来那套——重启又变回去。这种「看到的和存的
        不一致」是这套界面里最不该出现的东西，比多做一次重绘贵得多。
        """
        if self._on_theme_preview is not None:
            self._on_theme_preview(self._theme_original)
        super().reject()

    def _show_tutorial(self) -> None:
        """打开教程（跳过欢迎页，直接进上手步骤）。"""
        from .onboarding import OnboardingDialog

        OnboardingDialog(
            self._theme_original, parent=self, start_at_tutorial=True
        ).exec()

    def _build_background_row(self, config: dict) -> QWidget:
        """「窗口背景」这一行：路径 + 浏览 + 最近使用 + 模糊 + 档位 + 预览 + 说明。"""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._build_bg_picker(layout, config)
        self._build_bg_recent(layout, config)
        self._build_bg_blur(layout, config)
        self._build_bg_veil(layout, config)
        self._build_bg_preview(layout)
        self._wire_background()
        return holder

    def _build_bg_picker(self, layout: QVBoxLayout, config: dict) -> None:
        picker = QHBoxLayout()
        picker.setSpacing(6)
        self.background = QLineEdit(str(config.get("background_image", "") or ""))
        self.background.setPlaceholderText("留空则使用默认底色")
        self.background.setClearButtonEnabled(True)
        self.btn_browse = QPushButton("浏览…")
        self.btn_clear = QPushButton("清除")
        picker.addWidget(self.background, 1)
        picker.addWidget(self.btn_browse)
        picker.addWidget(self.btn_clear)
        layout.addLayout(picker)

    def _build_bg_recent(self, layout: QVBoxLayout, config: dict) -> None:
        """「最近使用」：换图不该每次从文件对话框从头翻。

        只列文件名，完整路径放悬停提示里——不然一个长路径会把这一行撑爆。
        """
        recent = [
            str(p) for p in (config.get("background_recent") or []) if str(p).strip()
        ]
        recent_row = QHBoxLayout()
        recent_row.setSpacing(6)
        recent_row.addWidget(QLabel("最近使用"))
        self.bg_recent = QComboBox()
        self.bg_recent.addItem("（还没有用过背景图）", "")
        for path in recent:
            self.bg_recent.addItem(Path(path).name, path)
            self.bg_recent.setItemData(
                self.bg_recent.count() - 1, path, Qt.ItemDataRole.ToolTipRole
            )
        self.bg_recent.setEnabled(bool(recent))
        self.bg_recent.setToolTip("直接切回用过的图（文件被挪走时会如实提示）。")
        recent_row.addWidget(self.bg_recent, 1)
        layout.addLayout(recent_row)

    def _build_bg_blur(self, layout: QVBoxLayout, config: dict) -> None:
        self.bg_blur = QCheckBox("加模糊蒙版（背景更含蓄，不跟正文抢注意力）")
        self.bg_blur.setChecked(bool(config.get("background_blur", True)))
        self.bg_blur.setToolTip(
            "开启后背景图会被模糊，作为氛围而不是照片；\n"
            "关掉则原图直出，适合本身就很淡的图。\n"
            "图案很碎（照片、截图）时会自动补上模糊，免得花纹跟正文抢字。"
        )
        layout.addWidget(self.bg_blur)

    def _build_bg_veil(self, layout: QVBoxLayout, config: dict) -> None:
        """「透出多少」必须给用户自己挑。

        同一张图放在深色照片上和浅色插画上，合适的浓度差得很远，而程序猜不准。
        默认取中间档——第一版把最含蓄的那一档写死，结果是「图几乎看不见」，
        等于功能没做（见 theme 的档位表）。
        """
        veil_row = QHBoxLayout()
        veil_row.setSpacing(6)
        veil_row.addWidget(QLabel("背景透出"))
        self.bg_veil = QComboBox()
        # 文案说的是**用户的图**会怎样，不是「不透明度 72%」这种实现口径。
        for label, key in (
            ("含蓄　图更淡，几乎不打扰阅读", "soft"),
            ("适中　图看得见，正文也清楚", "medium"),
            ("明显　图更清楚，界面更通透", "bold"),
        ):
            self.bg_veil.addItem(label, key)
        index = self.bg_veil.findData(veil_level(config.get("background_veil")))
        self.bg_veil.setCurrentIndex(index if index >= 0 else 0)
        self.bg_veil.setToolTip(
            "背景图从面板后面透上来多少。三档都保证正文可读，\n"
            "区别只在图片有多显眼。"
        )
        veil_row.addWidget(self.bg_veil, 1)
        layout.addLayout(veil_row)

    def _build_bg_preview(self, layout: QVBoxLayout) -> None:
        """预览条：缩放 / 模糊 / 蒙版三步与真实窗口完全同一条流水线。

        用的是同一个 BackgroundRenderer，所以它演示的不是「大概」，而是窗口里
        那一块的样子。
        """
        self._preview_renderer = BackgroundRenderer()
        self._preview_path: str | None = None
        self._bg_preview = QLabel()
        self._bg_preview.setObjectName("bgPreview")
        self._bg_preview.setFixedSize(_BG_PREVIEW)
        layout.addWidget(self._bg_preview)

        self._bg_note = QLabel()
        self._bg_note.setObjectName("muted")
        self._bg_note.setWordWrap(True)
        layout.addWidget(self._bg_note)

    def _wire_background(self) -> None:
        self.btn_browse.clicked.connect(self._pick_background)
        self.btn_clear.clicked.connect(lambda: self.background.setText(""))
        self.bg_recent.currentIndexChanged.connect(self._on_recent_picked)
        self.background.textChanged.connect(lambda _: self._sync_background())
        self.bg_blur.toggled.connect(lambda _: self._sync_background())
        self.bg_veil.currentIndexChanged.connect(lambda _: self._sync_background())
        self._sync_background()

    def _on_recent_picked(self) -> None:
        path = self.bg_recent.currentData()
        if path:
            self.background.setText(str(path))

    def _refresh_bg_preview(self) -> None:
        """画出「这张图在窗口里长什么样」。

        判据是「看得出这张图」，不是「有张图」。第一版把面板不透明度写死 88%，
        预览条上只剩一层匀色，用户的原话是「这不是只加了个蒙版吗」——若预览条
        在那一刻能如实呈现，这个误判在设置界面里就会当场暴露。
        """
        if not hasattr(self, "_bg_preview"):
            return
        path = self.background.text().strip()
        if path != self._preview_path:
            self._preview_renderer.set_source(path)
            self._preview_path = path
        if not self._preview_renderer.has_image:
            self._bg_preview.setPixmap(QPixmap())
            self._bg_preview.setText("（读不出这张图）" if path else "（没有背景图）")
            self._bg_preview.setToolTip("")
            return
        pixmap = self._preview_renderer.pixmap_for(_BG_PREVIEW, self.bg_blur.isChecked())
        if pixmap is None or pixmap.isNull():
            self._bg_preview.setPixmap(QPixmap())
            self._bg_preview.setText("（读不出这张图）")
            return
        pal = palette(self.theme.currentData())
        levels = pal["veil"]
        floor = pal["veil_floor"]
        veil = veil_level(self.bg_veil.currentData())
        # 与窗口里 `QFrame#panel` 用**同一个**数：预览条比真实窗口透，用户就会
        # 照着预览挑一档、回到窗口发现不是那样。面板有保底，这里也要有。
        percent = max(int(levels[veil]), int(floor["panel"]))
        overlay = QColor(pal["surface"])
        overlay.setAlphaF(percent / 100.0)
        out = QPixmap(pixmap)
        painter = QPainter(out)
        painter.fillRect(out.rect(), overlay)
        painter.end()
        self._bg_preview.setText("")
        self._bg_preview.setPixmap(out)
        self._bg_preview.setToolTip(
            "窗口里这块区域的样子：面板按「" + str(percent) + "%」盖在图上。"
        )

    def _pick_background(self) -> None:
        current = self.background.text().strip()
        start = ""
        try:
            if current and Path(current).parent.is_dir():
                start = str(Path(current).parent)
        except OSError:
            start = ""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择背景图片", start, _BG_FILTER
        )
        if chosen:
            self.background.setText(chosen)

    def _sync_background(self) -> None:
        """就地说明背景图的当前状态。

        文件不存在**不拦保存**：背景是装饰，而装饰的路径失效不该把用户的设置
        界面锁死（他可能只是把图片挪进了另一个文件夹）。所以这里只说明事实，
        保存照常进行；窗口那边在应用时会如实报告「没能加载」，不会假装换上了。
        """
        text = self.background.text().strip()
        try:
            exists = bool(text) and Path(text).is_file()
        except OSError:
            exists = False
        # 没有图时模糊开关与透出档位都无处可施，一起置灰——留着可选等于承诺
        # 一件做不到的事。两个控件都归这一处管：分两处判断迟早会漏一个，
        # 而漏掉的那个会一直可点、点了没反应。
        for widget in (self.bg_blur, self.bg_veil):
            widget.setEnabled(exists)
        # 预览先刷新：下面那句「模糊开/关」要读渲染器的判定结果——图案太碎时
        # 即使用户关掉了模糊，画出来仍然是糊的。文案与画面必须说同一件事。
        self._refresh_bg_preview()
        if not text:
            self._bg_note.setText("留空则窗口使用默认底色。")
        elif not exists:
            self._bg_note.setText(
                "找不到这个文件，保存后窗口不会有背景（其余设置不受影响）。"
            )
        else:
            if self._preview_renderer.auto_blur:
                blur_text = "自动（图案较碎）" if not self.bg_blur.isChecked() else "开"
            else:
                blur_text = "开" if self.bg_blur.isChecked() else "关"
            self._bg_note.setText(
                f"将使用：{Path(text).name}　"
                f"（模糊{blur_text}，"
                f"透出「{self.bg_veil.currentText().split('　')[0]}」）"
            )
        # 预览条在函数开头已经重画过，这里不再重复调用——重画一次要重新缩放
        # 整张图，拖动输入框时每个字符都会触发。

    def _sync_enabled(self) -> None:
        """非接口模式下，地址、密钥与兜底开关都不起作用，直接置灰。

        留着可编辑会让人以为离线模式也会去读这些值——实际上它一个都不用。
        """
        networked = self.mode.currentData() == "openai-compat"
        for widget in (self.base_url, self.model, self.api_key, self.fallback,
                       self.btn_test):
            widget.setEnabled(networked)

        # 词库文件不在时，这个开关勾了也不会生效：`build_provider()` 先确认
        # 词库路径存在，找不到就直接不挂兜底。让它保持可勾选，等于在界面上
        # 撒一个谎——用户以为有兜底，实际上没有，而且界面上没有任何地方会说。
        # 置灰并就地说明，与「本地词库：离线词库缺失」那行正好对上。
        if self._offline_missing:
            self.fallback.setEnabled(False)
            self.fallback.setToolTip(
                "本机没有离线词库文件，兜底无法启用。\n"
                "请确认安装完整，或用 scripts/build_offline_dict.py 自行构建。"
            )

    def _show_probe(self, text: str, ok: bool) -> None:
        """就地显示测试结果。成功与失败用不同的样式类。

        结果**不弹窗**：弹窗会把设置对话框顶掉，用户还得重开一遍——而这条
        提示本来就是要他「就地改一个字段再试一次」的。
        """
        self._probe_note.setText(text)
        # 动态属性/对象名变了必须 repolish，Qt 不会自己重算——否则失败那条
        # 会一直沿用成功的样式，看起来像「明明没通却说通过了」。
        self._probe_note.setObjectName("muted" if ok else "warn")
        self._probe_note.style().unpolish(self._probe_note)
        self._probe_note.style().polish(self._probe_note)
        self._probe_note.setVisible(True)

    def _test_connection(self) -> None:
        """用**界面上当前的**地址 / 模型 / 密钥试一次连接。

        刻意不先保存再读配置：用户填完新地址的第一件事就是试它，若必须先保存
        才能测，测的就不是他刚填的那个值——而填错的地址一旦保存就会在下次
        查词时生效。
        """
        api_key = self.api_key.text().strip()
        if not api_key:
            self._show_probe("请先填写 API 密钥，再测试连接。", ok=False)
            return
        base_url = self.base_url.text().strip()
        if not base_url:
            self._show_probe("请先填写接口地址，再测试连接。", ok=False)
            return

        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        self._show_probe("正在连接…（最多等待超时时间那么久）", ok=True)
        submit(
            probe_config,
            base_url,
            self.model.text().strip(),
            api_key,
            float(self._config.get("timeout") or 30.0),
            on_success=self._on_probe_ok,
            on_error=self._on_probe_fail,
        )

    def _on_probe_ok(self, message: str) -> None:
        try:
            self.btn_test.setText("测试连接")
            self.btn_test.setEnabled(self.mode.currentData() == "openai-compat")
            self._show_probe(str(message), ok=True)
        except RuntimeError:
            # 对话框已经关掉（用户等不及），控件连同 C++ 对象一起没了。
            # 这不是错误，安静收场即可。
            pass

    def _on_probe_fail(self, message: str, kind: str = "") -> None:
        """测试连接失败。类别不进界面——这里只有一种操作，分类对用户没有意义。"""
        try:
            self.btn_test.setText("测试连接")
            self.btn_test.setEnabled(self.mode.currentData() == "openai-compat")
            self._show_probe(str(message), ok=False)
        except RuntimeError:
            pass

    def _on_save(self) -> None:
        self._config["theme"] = self.theme.currentData() or THEME_DEFAULT
        self._config["provider"] = self.mode.currentData()
        self._config["base_url"] = self.base_url.text().strip()
        self._config["model"] = self.model.text().strip()
        self._config["offline_fallback"] = self.fallback.isChecked()
        self._config["tray_enabled"] = self.tray.isChecked()
        # 统计开关被改动过，就算用户已经就这件事表过态。否则「在设置里关掉、
        # 重启后又被问一次要不要参与」——那等于程序没把他的选择当选择。
        _was_enabled = bool(self._config.get("analytics_enabled", False))
        self._config["analytics_enabled"] = self.analytics.isChecked()
        if self.analytics.isChecked() != _was_enabled:
            self._config["analytics_consent_decided"] = True
        self._config["background_image"] = self.background.text().strip()
        self._config["background_blur"] = self.bg_blur.isChecked()
        self._config["background_veil"] = self.bg_veil.currentData() or BG_VEIL_DEFAULT

        # 保存前先校验：否则填错的地址要等到下次查词失败才暴露，
        # 那时用户早已忘了自己改过设置
        problem = check_config(self._config)
        if problem:
            self._error.setText(problem)
            self._error.setVisible(True)
            return  # 不 accept，保持对话框打开让用户就地改

        save_api_key(KEY_PATH, self.api_key.text().strip())
        self.accept()

    def result_config(self) -> dict:
        return self._config
