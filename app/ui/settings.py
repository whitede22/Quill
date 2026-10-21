"""设置对话框：主题、接入方式、API 地址、模型、密钥、窗口背景图。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import KEY_PATH, check_config
from ..providers import offline_dict_status, resolve_offline_dict
from ..providers.openai_compat import OpenAICompatProvider
from ..security.keystore import IS_WINDOWS, save_api_key
from .background import BackgroundRenderer
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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._config = dict(config)
        # 主题即时预览用的回调，由主窗口注入。默认 None 时退化成「保存后才生效」，
        # 这样自检 / 截图脚本沿用旧调用方式也不会坏。
        self._on_theme_preview = on_theme_preview
        self._theme_original = theme_id_of(config.get("theme"))

        self.mode = QComboBox()
        self.mode.addItem("OpenAI 兼容接口", "openai-compat")
        self.mode.addItem("离线词典（不联网，查本地词库）", "offline")
        self.mode.addItem("离线示例（仅 3 个演示词条）", "mock")
        index = self.mode.findData(config.get("provider", "openai-compat"))
        self.mode.setCurrentIndex(index if index >= 0 else 0)

        self.base_url = QLineEdit(config.get("base_url", ""))
        # 占位提示只写「到版本号为止」这一条规则，不写死某家厂商的地址：
        # 填 OpenAI 还是 DeepSeek 都合法，写死一家会让另一家的用户以为不能填自己那家。
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
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.api_key, 1)
        key_layout.addWidget(self.btn_test)

        self._probe_note = QLabel()
        self._probe_note.setObjectName("muted")
        self._probe_note.setWordWrap(True)
        self._probe_note.setVisible(False)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)
        form.addRow("界面主题", self._build_theme_row(config))
        form.addRow("接入方式", self.mode)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名称", self.model)
        form.addRow("API 密钥", key_row)
        form.addRow("窗口背景", self._build_background_row(config))

        hint = QLabel(
            "任何兼容 OpenAI 协议的服务都能接入：OpenAI、DeepSeek、Moonshot、"
            "通义、智谱，或本机的 Ollama / vLLM 等。\n"
            "地址填到版本号为止（如 https://api.deepseek.com/v1），"
            "程序会自动补全 /chat/completions。\n"
            "具体地址与模型名以你所选服务商的文档为准。"
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)

        self.fallback = QCheckBox("接口请求失败时，自动改用离线词库")
        self.fallback.setChecked(bool(config.get("offline_fallback", True)))
        self.fallback.setToolTip(
            "接口挂掉、额度用尽或断网时，仍然能查到词。\n"
            "兜底内容会在详情里标明来自离线词库，不会冒充接口结果。"
        )

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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self.fallback)
        layout.addWidget(hint)
        layout.addWidget(offline_hint)
        layout.addWidget(storage)
        layout.addWidget(self.btn_tutorial)
        layout.addWidget(self._probe_note)
        layout.addWidget(self._error)
        layout.addSpacing(4)
        layout.addWidget(buttons)

        self.btn_test.clicked.connect(self._test_connection)
        self.mode.currentIndexChanged.connect(self._sync_enabled)
        self._sync_enabled()

    def _build_theme_row(self, config: dict) -> QWidget:
        """「界面主题」这一行：下拉框（每项带色块）+ 即时生效。

        即时生效而不是「保存后才换」：配色是**看着挑**的，要用户先保存、看一眼、
        再打开设置改，等于让他来回跑三趟。
        """
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

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

        # 「最近使用」：换图不该每次从文件对话框从头翻。只列文件名，完整路径放
        # 悬停提示里——不然一个长路径会把这一行撑爆。
        recent = [str(p) for p in (config.get("background_recent") or []) if str(p).strip()]
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

        self.bg_blur = QCheckBox("加模糊蒙版（背景更含蓄，不跟正文抢注意力）")
        self.bg_blur.setChecked(bool(config.get("background_blur", True)))
        self.bg_blur.setToolTip(
            "开启后背景图会被模糊，作为氛围而不是照片；\n"
            "关掉则原图直出，适合本身就很淡的图。"
        )
        layout.addWidget(self.bg_blur)

        # 「透出多少」必须给用户自己挑：同一张图放在深色照片上和浅色插画上，
        # 合适的浓度差得很远，而程序猜不准。默认取中间档——第一版把最含蓄的
        # 那一档写死，结果是「图几乎看不见」，等于功能没做（见 theme 的档位表）。
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

        # 预览条：缩放 / 模糊 / 蒙版三步与真实窗口完全同一条流水线（同一个
        # BackgroundRenderer），所以它演示的不是「大概」，而是窗口里那一块的样子。
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

        self.btn_browse.clicked.connect(self._pick_background)
        self.btn_clear.clicked.connect(lambda: self.background.setText(""))
        self.bg_recent.currentIndexChanged.connect(self._on_recent_picked)
        self.background.textChanged.connect(lambda _: self._sync_background())
        self.bg_blur.toggled.connect(lambda _: self._sync_background())
        self.bg_veil.currentIndexChanged.connect(lambda _: self._sync_background())
        self._sync_background()
        return holder

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
        veil = veil_level(self.bg_veil.currentData())
        overlay = QColor(pal["surface"])
        overlay.setAlphaF(levels[veil] / 100.0)
        out = QPixmap(pixmap)
        painter = QPainter(out)
        painter.fillRect(out.rect(), overlay)
        painter.end()
        self._bg_preview.setText("")
        self._bg_preview.setPixmap(out)
        self._bg_preview.setToolTip(
            "窗口里这块区域的样子：面板按「"
            + str(levels[veil])
            + "%」盖在图上。"
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
        if not text:
            self._bg_note.setText("留空则窗口使用默认底色。")
        elif not exists:
            self._bg_note.setText(
                "找不到这个文件，保存后窗口不会有背景（其余设置不受影响）。"
            )
        else:
            self._bg_note.setText(
                f"将使用：{Path(text).name}　"
                f"（模糊{'开' if self.bg_blur.isChecked() else '关'}，"
                f"透出「{self.bg_veil.currentText().split('　')[0]}」）"
            )
        # 预览条跟着一起更新：它是「图 + 模糊 + 蒙版」三者共同的产物，任何一项
        # 变了都得重画——只更新文字说明的话，用户改完档位看到的还是上一档的图。
        self._refresh_bg_preview()

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

    def _on_probe_fail(self, message: str) -> None:
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
