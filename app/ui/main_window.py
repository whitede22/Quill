"""主窗口。"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    APP_DIR,
    KEY_PATH,
    RECENT_BACKGROUNDS_MAX,
    ensure_dir,
    load_config,
    save_config,
)
from ..core import analytics
from ..core.db import backup_to, rebuild_fts
from ..core.dictionary import DictionaryService
from ..core.exporter import (
    PdfFontUnavailable,
    export_anki,
    export_csv,
    export_json,
    export_markdown,
    export_pdf,
    export_text,
    export_wordlist,
)
from ..core.importer import (
    ImportOutcome,
    ParsedWord,
    ParseResult,
    parse_text,
    read_import_text,
    run_import,
    split_words,
)
from ..core import review
from ..core.models import Entry, Phrase, join_tags
from ..core.repository import Repository
from ..providers import build_provider, provider_status
from ..providers.base import provided_fields, provider_label, supports_phrases
from ..security.keystore import load_api_key
from .background import BackgroundRenderer
from .export_dialog import EXPORT_FORMATS, ExportDialog, FormatHelpDialog
from .hotkey import HOTKEY_TEXT, GlobalHotkey, clipboard_seed
from .hotkey import available as hotkey_available
from .refresh_dialog import RefreshDialog
from .review_dialog import ReviewDialog
from .settings import SettingsDialog
from .theme import BG_VEIL_DEFAULT, stylesheet, theme_id_of, veil_level
from .widgets import EntryCard, EntryDetail
from .workers import submit

_USER_ROLE = Qt.ItemDataRole.UserRole

# 左栏在看哪个集合。**只在这一处定义**：列表、搜索、排序、删除、撤销、导出
# 全都按它分派。各处各写一次字符串判断的话，漏掉的那一处会安静地按另一个
# 集合办（比如在词组模式下删掉一个同号词条），而界面只会显示「已删除」。
MODE_WORDS = "words"
MODE_PHRASES = "phrases"

# 拖拽落点按后缀分派：生词表去导入，图片去当窗口背景。两份表都是**唯一一份
# 定义**——图片那组必须与设置里文件对话框的过滤器一致，各写一份的话会出现
# 「对话框里选得到、拖进来却不认」这种自相矛盾的行为。
# 生词表这组同理：`.docx` 加进这里，就必须同时加进 `_do_import` 的对话框过滤器。
_WORD_SUFFIXES = (".txt", ".md", ".json", ".xml", ".docx", ".docm")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")

# 文件对话框的过滤器**从上面那份后缀表派生**，不再手写一遍字面量：
# 两处各写一份时，改了其中一处就会出现「对话框里选得到、拖进来却不认」。
# 派生之后这种不一致在结构上不可能发生，自检里也不用再盯一个「两边要一样」
# 的约定（那种约定迟早会被忘掉一次）。
_WORD_FILTER = "生词表 (" + " ".join(f"*{s}" for s in _WORD_SUFFIXES) + ");;所有文件 (*)"

# 每种「来源格式」对应的识别依据说明。
#
# **键必须与 `importer.parse_text()` 实际返回的 `source_format` 逐字相同。**
# 写错一个字，用户看到的就永远是兜底那句「按普通生词表读的」，而他真正走的
# 路径没人说得出来——**谎报比不报更糟**。这不是假想的风险：这两个键曾经写成
# 「Quill 导出（纯文本）」与「Quill 导出（Markdown）」，而解析器返回的是
# 「Quill 文本导出」与「Quill Markdown 导出」，于是**本程序自己导出的文件
# 也被说成普通生词表**，一直没被发现。现在自检里有一条断言盯着它们不许分家。
_FORMAT_HOW = {
    "Quill 文本导出": "按导出格式读的，只取「N. 单词」那一行，并还原备注",
    "Quill Markdown 导出": "按导出格式读的，只取「## 单词」那一行，并还原备注",
    "Quill JSON 导出": "按 JSON 结构读的，取每个词条的 lemma、备注与释义",
    "有道单词本（XML）": "按 XML 结构读的，取每个 <word> 的单词与 <trans> 释义",
}
_FORMAT_HOW_DEFAULT = "按普通生词表读的：每行取英文词，用逗号列开的会整行收下"


def _dismiss_progress(progress: QProgressDialog) -> None:
    """收掉进度框，并让它**真的从屏幕上撤掉**再往下走。

    只 `close()` 是不够的。这条收尾路径紧接着要做 `_reload_list()`——重建整表
    卡片，是几百毫秒到几秒的同步重活；主线程被占满的这段时间里，隐藏请求得不到
    处理，用户看到的就是「进度条停在 100% 不动」；等 `_report_import()` 弹出模态
    结果框、事件循环跑起来，它才消失。**「导入完成却卡在 100%，要过一会才能关掉」
    的成因就在这几行之间**，而不是进度本身没跑完。

    所以这里补 `processEvents`，并且**排除用户输入**：

    - 不排除的话，用户在这几毫秒里再点一次「导入」会重入 `_do_import`——
      此刻 `_import_progress` 已置 None，它会开一轮新导入，与正在收尾的这一轮
      抢列表状态；
    - 排除之后，窗口系统照样处理隐藏与重绘，而这正是要解决的那一件事。

    两次而不是一次：一次 `processEvents` 处理的是**隐藏**，紧接着还要处理一次
    重绘——屏幕上那块残留的 100% 只有在重绘之后才真的消失。两次的代价是微秒级。

    `hide()` 与 `close()` 都给：`hide()` 是明确的「从屏幕上撤掉」，`close()` 走的
    是 QDialog 的关闭流程。单靠 `close()` 时，模态关系与嵌套事件循环都可能把
    hide 推迟到下一个循环——而那正是用户抱怨的那一段。

    顺带把「取消」按钮摘掉：`canceled` 已经在上面断开（`setValue(maximum)` 会让
    `QProgressDialog` 自己补发一次 canceled，而此刻我们已经在收尾了），
    留着它只会让用户点了没反应、看起来像卡死。
    """
    try:
        progress.canceled.disconnect()
    except (RuntimeError, TypeError):
        pass
    progress.setCancelButton(None)
    progress.setValue(progress.maximum())
    progress.setEnabled(False)
    progress.hide()
    progress.close()
    app = QApplication.instance()
    if app is not None:
        for _ in range(2):
            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


def _brief(text: str, limit: int = 44) -> str:
    """把可能很长的一句话压成一行短摘要。

    失败原因是写给「单个词为什么没进来」的，往往带着整段出路建议；
    直接塞进清单弹窗会把版面撑散，反而看不清是哪个词出的问题。
    完整原因会随清单文件一起落盘，不在这里丢。
    """
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")

# 单次渲染的条目上限。QListWidget 插入上万行会明显卡顿，
# 超出时提示用户用搜索缩小范围，而不是硬扛。
LIST_LIMIT = 1000

# 导入时一次交给后台的词数。分批是为了让「取消」有落点、进度能走：
# 整批一次提交的话，用户点了取消也要等全部跑完才停得下来。
IMPORT_BATCH = 10


class MainWindow(QMainWindow):
    def __init__(
        self, repo: Repository, service: DictionaryService, config: dict
    ) -> None:
        super().__init__()
        self.repo = repo
        self.service = service
        self.config = config
        self._pending_note: tuple[int, str] | None = None
        # 标签与备注走同一条自动保存通道，但各自记一份待写值：用户完全可能
        # 改了备注又改标签，两者都在防抖窗口里等着落库。
        self._pending_tags: tuple[int, str] | None = None
        # 当前按哪个标签筛选（空串＝不筛）。放在左栏下拉里，与搜索框是**与**
        # 的关系——两个条件同时生效，而不是后一个把前一个顶掉。
        self._tag_filter = ""
        # 最近一次被删的东西。用于状态栏那个「撤销」——只留最近一条：
        # 一个删除栈会让「撤销」变成「撤销到哪一步」，而用户要救的永远是
        # 刚刚手滑的那一下。
        #
        # 存的是 **(类别, 快照)** 而不是裸 `Entry`：撤销要认得出删的是词条还是
        # 词组——两者是两张各自从 1 发号的表，同一个数字在两边是两条毫不相干
        # 的数据（详见 db.DDL 里 phrases 的建表注释）。
        self._last_deleted: tuple[str, object] | None = None
        # 左栏此刻在看哪个集合：生词本还是词组收藏。列表、搜索、排序、导出
        # 全都按它分派——**只有一处判据**，各处各判一次必然有一处会忘。
        self._mode = MODE_WORDS
        # 内容请求序号：只有最新一次请求的结果才允许上屏（见 _next_seq）
        self._content_seq = 0
        # 当前详情区正显示哪条词条（按 id）。用于挡住「程序化选中触发的重复
        # 渲染」——见 _on_item_changed 里那段说明，它会让失败原因凭空消失。
        self._displayed_id: int | None = None
        # 当前高亮的那张卡片。不靠 currentItemChanged 的 previous 参数去反选：
        # 列表重载时 clear() 已把旧项连同旧卡片一起销毁，拿着已删除的对象去访问
        # 会抛 RuntimeError，而这条路径恰好在「删完一条接着重载」时必经。
        self._selected_card: EntryCard | None = None

        # 背景图。状态挂在这里而不是每帧现读配置：paintEvent 会被调很多次，
        # 每次都去读文件等于每次重绘都碰一次磁盘。
        self._background = BackgroundRenderer()
        self._bg_blur = False
        # 背景透出的档位（面板铺自身底色的不透明度）。见 theme 的档位表。
        self._bg_veil = BG_VEIL_DEFAULT
        self._background_ok = False
        # 当前主题。窗口自己也应用一次（而不是只依赖 main.py 启动时设过），
        # 这样自检 / 截图脚本直接构造 MainWindow 时拿到的也是正确的配色。
        self._theme = theme_id_of(config.get("theme"))

        # 导入状态。这些必须挂在实例上而不是藏在某一层的闭包里：整批导入
        # 被拆成多次后台往返（见 IMPORT_BATCH），每批回来都要接着上一批的
        # 账继续记，而每次往返之间事件循环是活的。
        self._import_queue: list[ParsedWord] = []
        self._import_total = 0
        self._import_done = 0
        self._import_outcome = ImportOutcome()
        self._import_known: set[int] = set()
        self._import_cancelled = False
        self._import_error = ""
        self._import_progress: QProgressDialog | None = None
        # 这一批词是从哪来的（文件导入 / 输入框批量查词）。报告文案要用它：
        # 批量查词走的是与导入同一条链路，但把结果说成「导入完成」会让用户
        # 以为程序自己去读了个文件。
        self._import_origin = "导入"

        # 批量重查状态。与导入那套分开记：一次重查每条都要一次接口往返，
        # 没法按批合并；而且两者的「失败」含义完全不同——导入的失败是
        # 「这个词没进来」，重查的失败是「这一条保持原样」。混用一套计数，
        # 报告里就会把这两种事说成同一件。
        self._refresh_queue: list[tuple[int, str]] = []
        self._refresh_total = 0
        self._refresh_done = 0
        self._refresh_updated: list[int] = []
        self._refresh_kept: list[tuple[str, str]] = []
        self._refresh_cancelled = False
        self._refresh_progress: QProgressDialog | None = None

        # 托盘与全局热键。`_quitting` 区分「关掉窗口」与「真的退出」：
        # 有托盘时前者只是藏起来（热键才能随时唤出），后者必须真的结束进程。
        # 没有这个开关，托盘菜单里的「退出」会走同一条 close() 路径，于是
        # 用户点了退出、窗口消失、进程还在——一个再也打不开的常驻程序。
        self._quitting = False
        self._tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._hotkey: GlobalHotkey | None = None
        self._tray_hinted = False

        # 统计用的一次性状态。三类信息都只在「刚发生的那一次操作」里有意义：
        #   起点时间（算耗时）、是否失败（算成功率）、来源（手动还是剪贴板）。
        # 放在窗口上而不是参数里传：回调是异步的，经 submit 绕一圈回来时
        # 已经没有调用栈可以带参数了。
        self._lookup_started = 0.0
        self._lookup_failed = False
        self._op_name = "lookup"
        self._pending_source = "manual"
        # 备份恢复要关掉 main 里那个连接才能替换文件，所以具体流程由 main 注入。
        # 为 None 时（自检、截图脚本直接构造窗口）按钮不出现。
        self.restore_hook = None

        self.setWindowTitle("Quill · 英语生词本")
        self.resize(1080, 700)
        self.setMinimumSize(760, 520)
        # 收下拖进窗口的 txt 生词表——「把文件拖进来」是比翻文件对话框
        # 更省事的一键导入路径
        self.setAcceptDrops(True)

        self._build_ui()
        # 主题先于背景：两者都会 repolish，但背景图那条还要读样式表里生成的
        # 半透明规则，样式表换了就必须在它之后重算一次。
        self._apply_theme(self._theme)
        # 背景图要在界面搭好之后才能挂：它要 repolish 窗口与状态栏，而状态栏
        # 是在 _build_ui 里才建出来的。
        self._apply_background()
        # 恢复窗口状态必须早于 _wire_signals：内部会改动排序下拉框的选中项，
        # 此时信号尚未接上，才不会多触发一次列表重载。
        self._restore_state()
        self._wire_signals()
        self._reload_list()
        # 托盘要在界面搭完之后建：气泡与菜单都要一个活着的窗口做父级。
        self._setup_tray()
        # 首次启动（或教程升级后）弹出欢迎页。用 singleShot 延后到事件循环：
        # 窗口要先 show() 出来，弹窗才有正确的父级与层级。
        QTimer.singleShot(0, self._maybe_show_first_run)

    # ---------- 托盘与全局热键 ----------

    def _setup_tray(self) -> None:
        """建托盘图标并注册全局热键。

        **这里每一道前置检查都是「不满足就干脆不建」，不是「建了再说」。**
        托盘不存在时把窗口藏起来，用户会以为程序已经退出，而进程还占着数据库
        锁——下一次双击 exe 得到的是「Quill 已在运行」，且没有任何窗口能操作。
        所以宁可没有托盘（关闭窗口就真的退出），也不要一个有托盘的半成品。
        """
        if not bool(self.config.get("tray_enabled", True)):
            return
        if not hotkey_available():
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        app = QApplication.instance()
        if app is None:
            return

        tray = QSystemTrayIcon(app.windowIcon(), self)
        tray.setToolTip(f"Quill · 英语生词本（{HOTKEY_TEXT} 唤出）")

        menu = QMenu(self)
        act_show = menu.addAction("显示主窗口")
        act_show.triggered.connect(self._restore_from_tray)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit_from_tray)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()

        self._tray = tray
        self._tray_menu = menu
        # 托盘常驻的前提是「最后一个窗口关掉也不退出应用」，否则隐藏窗口会
        # 连带结束进程，热键随之失效。
        app.setQuitOnLastWindowClosed(False)

        hotkey = GlobalHotkey(self._on_hotkey, self)
        self._hotkey = hotkey
        if not hotkey.register():
            # 注册失败必须当场说。沉默的表现是「按了没反应」，用户会归咎于
            # 程序坏了，而不是「这个键被别的软件占了」。
            self.statusBar().showMessage(
                f"热键 {HOTKEY_TEXT} 被其他程序占用，已改为只在托盘菜单里操作；"
                f"关掉占用它的程序后重启本程序即可恢复",
                10000,
            )

    def _teardown_tray(self) -> None:
        """撤掉托盘与热键。重复调用安全。"""
        if self._hotkey is not None:
            self._hotkey.unregister()
            self._hotkey = None
        if self._tray is not None:
            self._tray.hide()
            self._tray.setContextMenu(None)
            self._tray.deleteLater()
            self._tray = None
        self._tray_menu = None

    def _apply_tray_enabled(self) -> None:
        """设置里改了「托盘常驻」之后就地生效，不用重启程序。"""
        want = bool(self.config.get("tray_enabled", True))
        if want and self._tray is None:
            self._setup_tray()
        elif not want and self._tray is not None:
            self._teardown_tray()
            app = QApplication.instance()
            if app is not None:
                app.setQuitOnLastWindowClosed(True)

    def _should_hide_to_tray(self) -> bool:
        """关窗口时该不该收进托盘，而不是真退出。

        三个条件缺一不可：不是真退出、托盘真的在、配置还开着。
        """
        return (
            not self._quitting
            and self._tray is not None
            and self._tray.isVisible()
            and bool(self.config.get("tray_enabled", True))
        )

    def _hint_hidden_to_tray(self) -> None:
        """第一次收进托盘时说一句，之后不再说。

        不说的话，「关窗口没退出」会被理解成关不掉；每次都说的话，就成了
        每次都要点掉的骚扰。
        """
        if self._tray_hinted or self._tray is None:
            return
        self._tray_hinted = True
        self._tray.showMessage(
            "Quill 还在后台运行",
            f"按 {HOTKEY_TEXT} 随时唤出；不想要托盘的话，在设置里关掉「托盘常驻」。",
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )

    def _restore_from_tray(self) -> None:
        """把窗口从托盘 / 最小化状态提到前台。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        # 单击与双击都还原窗口。只认双击的话，不少用户（尤其是第一次用托盘
        # 图标的人）会点一下就觉得「没反应」。
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _quit_from_tray(self) -> None:
        """托盘菜单里的「退出」：真的结束进程。"""
        self._quitting = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_hotkey(self) -> None:
        """热键触发：窗口提到前台，剪贴板里的词预填进输入框。

        预填的东西决定落到哪个集合：2 个词以上只可能是词组（导入侧同一条判据），
        留在生词本模式的话回车会把它拆成两个词分别去查。
        """
        self._restore_from_tray()
        seed = clipboard_seed(QApplication.clipboard().text())
        if not seed:
            self.input.setFocus()
            self.input.selectAll()
            self.statusBar().showMessage(
                f"剪贴板里没有可查的词。窗口已就绪，直接输入即可（{HOTKEY_TEXT} 随时唤出）",
                4000,
            )
            return
        target = MODE_PHRASES if len(seed.split()) >= 2 else MODE_WORDS
        if self._mode != target:
            self._switch_mode(target)
        self.input.setText(seed)
        self.input.selectAll()
        self.input.setFocus()
        # 这一次查词是从剪贴板来的。统计里区分手输与剪贴板，是为了知道热键
        # 到底有没有人用——这是判断「这个功能值不值得留」的唯一依据。
        self._pending_source = "clipboard"

    def _maybe_show_onboarding(self) -> None:
        """教程版本号落后于当前版本时，弹出欢迎页并写回新版本。

        判据是 `onboard_version < CURRENT_ONBOARD_VERSION`（版本号递增），
        不是「是否看过」的 bool——教程大改后老用户也能看到新版。
        """
        from .onboarding import CURRENT_ONBOARD_VERSION, OnboardingDialog

        seen = self.config.get("onboard_version", 0)
        if not isinstance(seen, int) or seen >= CURRENT_ONBOARD_VERSION:
            return
        dialog = OnboardingDialog(self._theme, parent=self)
        dialog.exec()
        self.config["onboard_version"] = CURRENT_ONBOARD_VERSION
        save_config(self.config)

    def _maybe_show_first_run(self) -> None:
        """首启的两页按固定顺序弹：先欢迎页，后统计告知。

        两个都是模态框，必须串行。同时弹两个的结果是后者压住前者，用户关掉上面
        那个才发现底下还有一个；在没有窗口系统的环境里，则表现为整个进程卡在
        `exec()` 上，且不报错。
        """
        self._maybe_show_onboarding()
        self._maybe_show_consent()

    def _maybe_show_consent(self) -> None:
        """用户还没就本地统计表过态时问一次。

        **关掉窗口不算表态**：`choice` 仍是 None，配置不写，下次启动再问。
        把「无视弹窗」当成同意，是这类功能最容易越界的地方。
        """
        from .consent import ConsentDialog

        if bool(self.config.get("analytics_consent_decided", False)):
            return
        dialog = ConsentDialog(self._theme, parent=self)
        dialog.exec()
        if dialog.choice is None:
            return
        self.config["analytics_enabled"] = bool(dialog.choice)
        self.config["analytics_consent_decided"] = True
        save_config(self.config)

    # ---------- 背景图 ----------

    def _apply_background(self) -> bool:
        """按当前配置加载 / 清除背景图，并让样式跟着切换。

        返回是否真的有一张可用的图。调用方（设置保存）据此决定状态栏要不要
        如实说明——图片没加载上却说「设置已保存」，用户会以为背景换好了。

        `hasBackground` 这个动态属性是给样式表用的：**没有图片时窗口必须铺回
        底色**，否则窗口会把底下没画东西的区域直接透出去（花屏）。属性变了
        必须 unpolish/polish 一次，Qt 不会主动重算样式。

        `bgVeil` 是「背景透出多少」的档位，决定面板铺白的不透明度（规则由
        theme.BG_VEIL_LEVELS 生成）。它**总是要设**，不能只在配了图时才设：
        档位切换发生在没有图的时刻时，属性若还留着上一次的值，等图配上来的
        那一瞬间就会先按旧档显示一次。
        """
        path = str(self.config.get("background_image") or "").strip()
        self._bg_blur = bool(self.config.get("background_blur", True))
        self._bg_veil = veil_level(self.config.get("background_veil"))
        loaded = self._background.set_source(path) if path else False
        if not path:
            self._background.clear()
        self._background_ok = loaded
        self.setProperty("bgVeil", self._bg_veil)
        for widget in (self, self.statusBar()):
            widget.setProperty("hasBackground", loaded)
        # 属性变了必须重算样式，Qt 不会自己做。而且**只 repolish 窗口本身不够**：
        # `QMainWindow[hasBackground="true"] QFrame#panel` 这类后代规则是挂在子控件
        # 的样式缓存上的，祖先的动态属性变了不会让那些缓存失效。实测踩过——面板
        # 一直沿用旧样式，现象是「背景换了，可面板还是不透」，而且不报任何错。
        # 子控件数量有限（百来个），只在这一刻重算一次，代价可以接受。
        self._repolish_all()
        self.update()
        return loaded

    def _repolish_all(self) -> None:
        """重算窗口与全部子控件的样式缓存。

        「祖先的动态属性变了，后代选择器不会失效」是 Qt 的行为，不是版本问题：
        `QMainWindow[hasBackground="true"] QFrame#panel` 挂在**子控件**的样式
        缓存上，光 repolish 窗口本身压不住它。换主题与换背景图都要走这一条，
        所以只有这一份实现——分两处写，迟早漏掉一处，而漏掉的那次不报任何错。
        """
        for widget in [self, self.statusBar(), *self.findChildren(QWidget)]:
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    # ---------- 主题 ----------

    def _apply_theme(self, theme_id: object) -> str:
        """切换界面主题并立即重绘。返回实际生效的主题名。

        换主题等价于换一整份样式表，走应用级 `setStyleSheet`。主题名**先归一化**：
        不认识的输入退回默认主题，因此「画出来的」与「存下来的」永远是同一套
        （配置被手改坏时，界面不该停在一个不存在的主题上）。
        """
        resolved = theme_id_of(theme_id)
        self._theme = resolved
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet(resolved))
        # setStyleSheet 会让 Qt 重算样式，但「重算样式」与「重画像素」是两件事；
        # 背景图是 paintEvent 画的，显式刷一次更稳。顺手把子控件缓存也过一遍，
        # 与换背景图走同一条路。
        self._repolish_all()
        self.update()
        return resolved

    # ---------- 背景图：拖进来直接用 ----------

    def _remember_background(self, path: object) -> None:
        """把这张图记进「最近使用」。最新在前、去重、截断到上限。

        只记路径、不拷贝文件：用户换图只要改路径，不必先往程序目录里导入一份。
        """
        text = str(path or "").strip()
        if not text:
            return
        recent = [p for p in (self.config.get("background_recent") or []) if p != text]
        recent.insert(0, text)
        self.config["background_recent"] = recent[:RECENT_BACKGROUNDS_MAX]

    def _offer_background(self, path: Path, ignored: int = 0) -> None:
        """把拖进窗口的图片设为窗口背景。**先问一句**，不默默改配置。

        拖拽落点同时也是「导入生词表」的落点，同一个手势在两种文件上做两件完全
        不同的事。而「把我刚拖进来的照片写进配置文件」是用户没有要求过的副作用：
        他可能只是想拖个东西进来看一眼。问一句的成本，比让他事后翻设置找补低。

        `ignored` 是这次一起拖进来的**其它**图片数量（只取第一张）。不写出来就
        是静默丢弃——那正是这一轮要修掉的那类行为，只不过换了个地方。
        """
        extra = f"\n（另有 {ignored} 张图未处理，只取第一张）" if ignored > 0 else ""
        answer = QMessageBox.question(
            self,
            "设为窗口背景？",
            f"把这张图片设为窗口背景吗？\n\n{path.name}{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.config["background_image"] = str(path)
        self._remember_background(path)
        save_config(self.config)
        loaded = self._apply_background()
        # 读不出来要如实说。只报「已设为背景」会让用户对着一动不动的窗口反复拖。
        self.statusBar().showMessage(
            f"已把 {path.name} 设为窗口背景"
            if loaded
            else f"没能把 {path.name} 读成图片，背景未变"
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """画窗口背景。

        顺序不能反：先让基类把 QSS 底色铺完（配了背景图时那条规则是 transparent，
        等于什么都不铺），再把自己的图片画上去。子控件在这之后才绘制，所以
        卡片、面板这些不透明控件仍然压在图片之上——也就是说，背景图出现在
        页边距与分割条这些留白处，不会跑到正文下面去干扰阅读。
        """
        super().paintEvent(event)
        if not self._background.has_image:
            return
        pixmap = self._background.pixmap_for(self.size(), self._bg_blur)
        if pixmap is None or pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()

    # ---------- 窗口状态持久化 ----------

    @staticmethod
    def _settings() -> QSettings:
        """界面状态单独存一个 ini，与 config.json 分离。

        路径挂在 APP_DIR 下而非系统注册表，这样更换 QUILL_HOME 或整个拷贝
        数据目录时，窗口尺寸与排序习惯会跟着一起走。
        """
        return QSettings(str(APP_DIR / "ui.ini"), QSettings.Format.IniFormat)

    def _restore_state(self) -> None:
        store = self._settings()
        geometry = store.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = store.value("window/state")
        if state is not None:
            self.restoreState(state)

        sizes = str(store.value("splitter/sizes", "") or "")
        parsed = [int(p) for p in sizes.split(",") if p.strip().isdigit()]
        if len(parsed) == 2 and all(p > 0 for p in parsed):
            self.splitter.setSizes(parsed)

        order = store.value("list/order", "recent")
        index = self.order.findData(str(order))
        if index >= 0:
            self.order.setCurrentIndex(index)

    def _save_state(self) -> None:
        store = self._settings()
        store.setValue("window/geometry", self.saveGeometry())
        store.setValue("window/state", self.saveState())
        store.setValue("splitter/sizes", ",".join(str(s) for s in self.splitter.sizes()))
        store.setValue("list/order", self.order.currentData())
        store.sync()

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 8)
        root.setSpacing(12)

        self.input = QLineEdit()
        self.input.setPlaceholderText("输入英语单词，回车查询并收藏")
        self.input.setMinimumHeight(33)
        self.input.setClearButtonEnabled(True)

        self.btn_query = QPushButton("查询")
        self.btn_query.setObjectName("primary")

        self.btn_import = QPushButton("导入")
        self.btn_import.setToolTip(
            "从 txt / md / json 生词表批量收录（也可以直接把文件拖进窗口）"
        )

        self.btn_export = QPushButton("导出")
        self.btn_export.setToolTip(
            "导出为纯文本 / 单词表 / Markdown / PDF / 表格 / Anki / JSON"
            "（点一下会先让你选）"
        )
        self.btn_format_help = QPushButton("格式说明")
        self.btn_format_help.setToolTip("每种导出格式长什么样、适合用来做什么")
        self.btn_backup = QPushButton("备份")
        self.btn_backup.setToolTip("把整个生词本另存为一份数据库副本")
        self.btn_settings = QPushButton("设置")

        # 工具栏分三级，视线才有落点：主操作（查询，实心）→ 常规（导入 / 导出，
        # 描边）→ 次要（格式说明 / 备份 / 设置，无边框）。挂 `#bare` 是纯样式
        # 改动，不动控件结构——界面测试断言的是文案与链路，样式可以自由换。
        for button in (self.btn_format_help, self.btn_backup, self.btn_settings):
            button.setObjectName("bare")

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.input, 1)
        top.addWidget(self.btn_query)
        top.addSpacing(18)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_export)
        top.addWidget(self.btn_format_help)
        top.addWidget(self.btn_backup)
        top.addWidget(self.btn_settings)
        root.addLayout(top)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("搜索生词本（支持中文）")
        self.filter.setClearButtonEnabled(True)

        self.order = QComboBox()
        self.order.addItem("最近", "recent")
        self.order.addItem("最早", "oldest")
        self.order.addItem("字母", "alpha")
        # 96 而不是 86：86 时「最近」两个字与下拉箭头挤在一起。右侧再多留的
        # 那点空隙由样式表里的 QComboBox padding-right 提供。
        self.order.setFixedWidth(96)
        self.order.setToolTip("列表排序方式")

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(self.filter, 1)
        filter_row.addWidget(self.order)

        # 标签筛选。做成一行的下拉而不是一排 chip：左栏宽度是固定的（拖一下
        # 分割条，详情区就跟着缩），chip 排不下时只能裁剪或横向滚动——裁剪等于
        # 让某些标签**无声消失**，而滚动条在一个 30px 高的行里既难用又难发现。
        # 标签一多，下拉反而是唯一诚实的形态。
        #
        # 没有标签时整行隐藏：一个永远只有「全部标签」一项的下拉是在占位。
        self.tag_row = QWidget()
        tag_layout = QHBoxLayout(self.tag_row)
        tag_layout.setContentsMargins(0, 0, 0, 0)
        tag_layout.setSpacing(6)
        tag_label = QLabel("标签")
        tag_label.setObjectName("muted")
        tag_layout.addWidget(tag_label)
        self.tag_filter = QComboBox()
        self.tag_filter.addItem("全部标签", "")
        self.tag_filter.setToolTip("只看带某个标签的词条；与搜索框同时生效。")
        tag_layout.addWidget(self.tag_filter, 1)
        self.tag_row.setVisible(False)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 卡片之间的间隙交给列表的 spacing，而不是给卡片加外边距：外边距要作用在
        # 卡片上，就得再套一层容器，行高也不再等于卡片高度，uniformItemSizes 的
        # 一致性假设会被打破。
        self.list.setSpacing(6)

        self.count_label = QLabel("共 0 条")
        # 自带一个对象名（而不是复用 #muted）：配了背景图时，它需要单独垫一层
        # 底——它不在面板里、也没有列表那样的轨道衬底，字是直接压在原图上的。
        # 复用 #muted 的话，垫底会把面板里每个次要标签也垫上一块白药丸。
        self.count_label.setObjectName("countLabel")

        # 「生词本 / 词组收藏」切换。两个可勾选按钮，而不是 QTabBar：左栏很窄，
        # 标签页的页眉会白吃一行高度，而这里要表达的只是一个「在哪个集合里」。
        # `setAutoExclusive` 保证永远恰好一个选中——互斥交给 Qt，别在回调里
        # 手动一个个 setChecked（漏一处就会出现「两个都亮着」）。
        #
        # 计数写进按钮文本：词组收藏是用户一条条攒出来的，他在生词本模式下也该
        # 看得见自己攒了多少；不写的话那一栏在他心里等于不存在。
        self.btn_mode_words = QPushButton()
        self.btn_mode_phrases = QPushButton()
        for button in (self.btn_mode_words, self.btn_mode_phrases):
            button.setObjectName("mode")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mode_words.setToolTip("查词收下来的词条")
        self.btn_mode_phrases.setToolTip("从右侧「固定搭配」区收藏下来的词组")

        mode_row = QHBoxLayout()
        mode_row.setSpacing(2)
        mode_row.addWidget(self.btn_mode_words)
        mode_row.addWidget(self.btn_mode_phrases)
        mode_row.addStretch(1)

        # 「待复习」入口。文本随时更新为到期条数（`_refresh_review_count`），点击
        # 打开复习会话。只在生词本模式下有意义——词组没有复习列，切过去就隐藏。
        self.btn_review = QPushButton("待复习 0")
        self.btn_review.setObjectName("review")
        self.btn_review.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_review.setToolTip(
            "开始复习到期词条：正面看词、翻面看义，再按「忘了 / 模糊 / 记住了」"
            "评分推进记忆计划"
        )
        # 「重查全部」与「待复习」同处一行、左右分列：两者都是「拿当前这一栏
        # 的内容做一件事」，放在一起比塞进顶部工具栏更贴近它们的作用范围。
        self.btn_refresh_all = QPushButton("重查全部")
        self.btn_refresh_all.setObjectName("refresh")
        self.btn_refresh_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_all.setToolTip(
            "按当前接口把列表里的词条重新查一遍。已写的备注与标签保留，"
            "接口没答上来的词条保持原样。"
        )

        review_row = QHBoxLayout()
        review_row.setSpacing(6)
        review_row.addWidget(self.btn_review)
        review_row.addStretch(1)
        review_row.addWidget(self.btn_refresh_all)

        left_layout.addLayout(mode_row)
        left_layout.addLayout(review_row)
        left_layout.addLayout(filter_row)
        left_layout.addWidget(self.tag_row)
        left_layout.addWidget(self.list, 1)
        left_layout.addWidget(self.count_label)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(1, 1, 1, 1)
        self.detail = EntryDetail()
        self.detail.set_networked(self._is_networked())
        panel_layout.addWidget(self.detail)

        self.splitter.addWidget(left)
        self.splitter.addWidget(panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setSizes([350, 690])

        root.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        # 接入状态常驻在右下角。用 addPermanentWidget 而不是 showMessage：
        # showMessage 会被每次查询结果覆盖，用户最需要看到它的时候恰好看不到。
        #
        # 「撤销删除」也放在这个永久区，而且**必须**是这里：showMessage 的临时
        # 消息会把普通区（addWidget）的控件整个盖住，于是「已删除「X」」一出现，
        # 撤销按钮就跟着消失——它恰好只在那一刻有用。
        self.btn_undo = QPushButton("撤销删除")
        self.btn_undo.setObjectName("statusButton")
        self.btn_undo.setToolTip("把刚刚删掉的那条放回去（词条含备注与标签，词组含简释与例句）")
        self.btn_undo.setVisible(False)
        self.statusBar().addPermanentWidget(self.btn_undo)

        self.mode_label = QLabel()
        self.mode_label.setObjectName("muted")
        self.statusBar().addPermanentWidget(self.mode_label)
        self._refresh_mode_label()

    def _is_networked(self) -> bool:
        """当前接入方式是否会联网。

        只有接口模式会。离线与示例模式都不出网，而 `build_provider` 也只在
        接口模式下挂兜底——两者用的是同一个判据，不能各判一套。
        """
        return self.config.get("provider") == "openai-compat"

    def _refresh_mode_label(self) -> None:
        self.mode_label.setText(provider_status(self.config, load_api_key(KEY_PATH)))
        self.mode_label.setToolTip(
            "当前查词来源。切换或配置请点击右上角「设置」。"
        )

    def _wire_signals(self) -> None:
        self.input.returnPressed.connect(self._do_lookup)
        self.btn_query.clicked.connect(self._do_lookup)
        self.btn_import.clicked.connect(self._do_import)
        self.btn_export.clicked.connect(self._do_export)
        self.btn_format_help.clicked.connect(self._show_format_help)
        self.btn_backup.clicked.connect(self._do_backup)
        self.btn_settings.clicked.connect(self._open_settings)

        self.list.currentItemChanged.connect(self._on_item_changed)
        self.detail.synonymClicked.connect(self._lookup_word)
        self.detail.refreshRequested.connect(self._refresh_word)
        self.detail.deleteRequested.connect(self._request_delete)
        self.detail.noteChanged.connect(self._on_note_changed)
        self.detail.tagsChanged.connect(self._on_tags_changed)
        self.detail.collocationCollected.connect(self._on_collocation_collected)
        self.detail.phraseDeleteRequested.connect(self._request_phrase_delete)
        self.btn_undo.clicked.connect(self._undo_delete)
        self.tag_filter.currentIndexChanged.connect(self._on_tag_filter_changed)
        self.btn_mode_words.clicked.connect(lambda _=False: self._switch_mode(MODE_WORDS))
        self.btn_mode_phrases.clicked.connect(
            lambda _=False: self._switch_mode(MODE_PHRASES)
        )
        self.btn_review.clicked.connect(self._open_review)
        self.btn_refresh_all.clicked.connect(self._do_refresh_all)

        # 输入即搜，但需要防抖，避免每个字符都打一次数据库
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(280)
        self._filter_timer.timeout.connect(self._reload_list)
        self.filter.textChanged.connect(lambda _: self._filter_timer.start())
        self.order.currentIndexChanged.connect(self._on_order_changed)

        # 备注自动保存：停顿 600ms 后落库，避免逐字写盘
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(600)
        self._note_timer.timeout.connect(self._flush_edits)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.filter.setFocus)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.input.setFocus)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._do_import)
        QShortcut(QKeySequence("Escape"), self, activated=self.input.clear)

    # ---------- 列表 ----------

    def _switch_mode(self, mode: str) -> None:
        """切换左栏在看哪个集合（生词本 / 词组收藏）。

        切之前必须把压在内存里的备注与标签落库：切走之后详情区会被整份重建
        （`show_entry` / `show_phrase` 都会 `_reset()`），那两个待写值若还挂在
        防抖定时器上，就再也没有机会写回去了——用户会看到自己刚敲的字消失，
        而落库侧一声不响。
        """
        if mode == self._mode:
            return
        self._flush_edits()
        self._mode = mode
        # 换了集合等于换了整套数据：当前高亮的卡片与「详情区显示的是哪一条」
        # 都不再属于这一屏。留着它们，下一次选中就会拿旧 id 去比对而跳过渲染。
        self._selected_card = None
        self._displayed_id = None
        if mode == MODE_PHRASES:
            self.detail.show_phrase(None)
        else:
            self.detail.show_entry(None)
        self._reload_list()

    def _sync_mode_buttons(self) -> None:
        """把「现在在哪个集合」与两个集合的条数反映到切换按钮上。

        条数每次重载都重算，不做增量维护：它会因为**好几条互不相干的路径**
        变化（查词、批量查词、导入、删除、撤销、收藏词组），增量维护等于在
        每一处都补一段同步逻辑——漏掉任何一处都只是「数字不对」，不报错，
        而且很久都不会有人发现。
        """
        self.btn_mode_words.setText(f"生词本 {self.repo.count()}")
        self.btn_mode_phrases.setText(f"词组收藏 {self.repo.count_phrases()}")
        words = self._mode == MODE_WORDS
        self.btn_mode_words.setChecked(words)
        self.btn_mode_phrases.setChecked(not words)
        # 重查只对词条有意义：词组没有提示词版本列，也就没有「过时」这个概念
        # （见 `DictionaryService.lookup_phrase` 的说明）。切到词组模式就藏起来，
        # 别留一个点了没反应的按钮。
        self.btn_refresh_all.setVisible(words)
        self.filter.setPlaceholderText(
            "搜索生词本（支持中文）" if words else "搜索词组收藏"
        )
        # 输入框的语义随集合变：同一个框在两个模式下查的是两种东西。占位文字
        # 必须跟着改，否则用户在词组模式里输入 `take off`，看到提示还写着
        # 「输入英语单词」，会以为是程序把词组拆错了。
        self.input.setPlaceholderText(
            "输入英语单词，回车查询并收藏"
            if words
            else "输入英语词组，回车查询并收藏，如 take off"
        )

    def _refresh_review_count(self) -> None:
        """把「待复习」按钮更新为到期条数，并按当前集合显隐。

        复习只对词条有意义——词组没有复习列，切过去就隐藏；生词本模式下显示
        `待复习 N`。条数每次重载都重算，不做增量维护：它会因查词、导入、删除、
        撤销、评分等多条路径变化，增量维护漏一处就只是「数字不对」，不报错。
        """
        if self._mode == MODE_PHRASES:
            self.btn_review.setVisible(False)
            return
        self.btn_review.setVisible(True)
        self.btn_review.setText(f"待复习 {self.repo.due_count(review.now_timestamp())}")

    def _open_review(self) -> None:
        """打开复习会话；关闭后刷新待复习数。

        先把手上的备注 / 标签落库：会话是模态的，虽然它只写复习列、不碰内容，
        但「进一个会写库的模态框之前先把待写值落库」是一以贯之的姿势——否则
        用户可能带着未保存的编辑进会话，出来后那条编辑的落库时机就悬了。
        """
        self._flush_edits()
        started = time.monotonic()
        dialog = ReviewDialog(self.repo, self._theme, parent=self)
        dialog.exec()
        # 时长含「打开但一个都没评」的整段停留时间，所以只有真评过才记：
        # 记了没评的那次，会把「平均一轮复习多久」拉得毫无意义。
        graded = dialog.graded()
        if graded:
            analytics.record_event(
                "review_completed",
                {
                    "count": int(graded),
                    "duration_ms": int(max(0.0, time.monotonic() - started) * 1000),
                },
            )
        # 会话里评过分，到期集合变了，按钮上的数字要跟着刷新。
        self._refresh_review_count()

    def _current_entries(self, *, unlimited: bool = False) -> list[Entry]:
        """列表与导出共用同一数据源，保证「所见即所导」。

        但**上限不共用**：列表设上限是为界面性能（超出时计数标签会明确写出
        「已显示 N 条 · 共 M 条」），导出若沿用同一上限，打印稿会被静默截断，
        而文件头还自称「共 N 条」。因此导出传 unlimited=True。

        标签筛选与搜索框是**与**的关系：两个条件同时生效。它们各自单独占满
        列表的场景很好想，但真正会发生的是「我要在这个标签里找那个词」——
        若后一个条件把前一个顶掉，用户会以为标签被清空了。
        """
        cap = None if unlimited else LIST_LIMIT
        query = self.filter.text().strip()
        if self._mode == MODE_PHRASES:
            # 词组走同一条「所见即所导」的路：这里返回的也是 `Entry`（由
            # `Phrase.to_entry()` 适配），因此列表卡片、导出、格式说明样例
            # 一个分支都不用再写。**排序与筛选也从这里分派**，不另开一条。
            return [
                item.to_entry()
                for item in self.repo.search_phrases(
                    query, order=self.order.currentData(), limit=cap
                )
            ]
        if self._tag_filter:
            tagged = self.repo.list_by_tag(
                self._tag_filter, order=self.order.currentData()
            )
            if query:
                # 交集按 id 取。文字检索走 FTS（含大小写折叠、词形归并），
                # 不能用 `in` 去比字符串——那样「Ubiquitous」与「ubiquitous」
                # 会被判成两个不同的词条。
                hit_ids = {entry.id for entry in self.repo.search(query, limit=None)}
                tagged = [entry for entry in tagged if entry.id in hit_ids]
            return tagged if cap is None else tagged[:cap]
        if query:
            return self.repo.search(query, limit=cap)
        return self.repo.list_entries(order=self.order.currentData(), limit=cap)

    @staticmethod
    def _list_text(entry: Entry) -> str:
        """列表项的**文本表示**。

        界面上看到的是卡片（`EntryCard`），项自身的文字被卡片整个盖住——但它不是
        废物：无障碍接口、`item` 的默认提示、以及测试都靠它读「这一行是什么」。
        丢掉它，卡片就只剩像素，任何不经过渲染的检查手段都只能去猜。
        """
        gloss = (entry.definition_zh or entry.definition_en or "").replace("\n", " ")
        # 左栏宽度有限，中文按双宽计算，截断到 15 字以免横向溢出
        if len(gloss) > 15:
            gloss = gloss[:15] + "…"
        return f"{entry.lemma}  ·  {gloss}" if gloss else entry.lemma

    def _reload_list(self, select_id: int | None = None) -> None:
        query = self.filter.text().strip()
        entries = self._current_entries()
        phrases_mode = self._mode == MODE_PHRASES

        self.list.blockSignals(True)
        self.list.clear()
        # clear() 已把旧卡片一并销毁，这里必须把引用放掉：持着已删除的卡片去
        # set_selected 会抛 RuntimeError，而「删掉一条 → 重载列表」正好必经此处。
        self._selected_card = None
        for entry in entries:
            item = QListWidgetItem(self._list_text(entry))
            item.setData(_USER_ROLE, entry.id)
            item.setToolTip(entry.definition_zh or entry.definition_en)
            # 行高与卡片同高：卡片由 setItemWidget 铺满项矩形，行高给错就会被压扁
            item.setSizeHint(QSize(0, EntryCard.HEIGHT))
            self.list.addItem(item)
            card = EntryCard(entry)
            # 卡片长什么样是共用的，**但删除落到哪张表必须分派**：词条与词组
            # 是两张各自从 1 发号的表，拿词组的 id 去删词条会安静地删掉别人。
            if phrases_mode:
                card.deleteRequested.connect(self._request_phrase_delete)
            else:
                card.deleteRequested.connect(self._request_delete)
            self.list.setItemWidget(item, card)
        self.list.blockSignals(False)

        total = self.repo.count_phrases() if phrases_mode else self.repo.count()
        shown = len(entries)
        if shown >= LIST_LIMIT:
            self.count_label.setText(
                f"已显示 {shown} 条 · 共 {total} 条 · 请用搜索缩小范围"
            )
        elif query or (self._tag_filter and not phrases_mode):
            self.count_label.setText(f"匹配 {shown} 条 · 共 {total} 条")
        else:
            self.count_label.setText(f"共 {total} 条")

        # 切换按钮上的条数要跟着这次重载走（这一次的数据才是刚查出来的）
        self._sync_mode_buttons()
        # 「待复习」条数同理：查词、导入、删除、撤销、评分都会改它，每次重载重算。
        self._refresh_review_count()

        # 标签筛选只对词条有意义——词组没有标签。切到词组模式时必须把它藏掉，
        # 否则那一行会永远显示「全部标签」，一个只有一项的下拉就是在占位。
        if phrases_mode:
            self.tag_row.setVisible(False)
        else:
            # 标签下拉放在最后重建：它会读库（tag_counts），而上面那些步骤才是
            # 「这一屏该显示什么」的判据来源。顺序反过来的话，删掉最后一条带某标签
            # 的词条时，下拉里那个标签会先消失、列表随后才重排，中间那一瞬的界面
            # 自相矛盾（下拉显示「全部标签」而列表还是空的）。
            self._refresh_tag_filter()

        if select_id is not None:
            for i in range(self.list.count()):
                if self.list.item(i).data(_USER_ROLE) == select_id:
                    self.list.setCurrentRow(i)
                    return

    def _refresh_tag_filter(self) -> None:
        """按库里的标签重建筛选下拉。

        每次都整份重建，不做增量维护：标签会因为**三条互不相干的路径**变化
        （详情区编辑标签、导入带回标签、删除词条），增量维护等于在这三处各写
        一份同步逻辑——漏掉任何一处都只是「下拉里少一个标签」，不报错，而且
        很久都不会被发现。标签的数量是用户自己打出来的，量级很小，重建很便宜。

        重建期间必须挡住信号：`setCurrentIndex` 会触发筛选变化，而那会再走一次
        `_reload_list` —— 于是「重建下拉」变成「重建中又重建一遍」，最外那一次
        用的是已经过期的标签清单。
        """
        counts = self.repo.tag_counts()
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("全部标签", "")
        for tag, count in counts:
            self.tag_filter.addItem(f"{tag}（{count}）", tag)
        index = self.tag_filter.findData(self._tag_filter)
        # 正在筛的那个标签没了（最后一条带它的词条被删了或改了标签）时，
        # 筛选必须跟着撤销。留着它，列表会永远空着而下拉显示「全部标签」——
        # 用户看到的是一份「明明是全部却什么都没有」的列表。
        if index < 0:
            self._tag_filter = ""
            index = 0
        self.tag_filter.setCurrentIndex(index)
        self.tag_filter.blockSignals(False)
        self.tag_row.setVisible(bool(counts))

    def _on_tag_filter_changed(self, _index: int) -> None:
        self._flush_edits()
        self._tag_filter = str(self.tag_filter.currentData() or "")
        self._reload_list()

    def _on_item_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        # 切换词条前先落盘未保存的备注，否则会丢
        self._flush_edits()
        # 高亮必须在下面那两处 return **之前**处理。两处 return 都不渲染新词条
        # （一处是无选中，一处是「已经在显示了」），但它们仍然是一次真实的选中
        # 变化：把高亮放在后面，点卡片时背景与边框就不会动，看起来像点不动。
        self._apply_card_selection(current)
        if current is None:
            return
        entry_id = int(current.data(_USER_ROLE))
        # 已经显示的就是这一条：直接返回，不要从库里重读一遍再渲染。
        #
        # 这不是省一次查询的优化，而是修一个会让功能失效的覆盖问题：
        # `_present_entry()` 先渲染刚查到的词条，紧接着 `_reload_list(select_id=…)`
        # 会程序化选中该项并触发本回调；从 `repo.get_by_id()` 读回来的对象是
        # 由 `Entry.from_row` 构造的，**不含任何运行时字段**，于是
        # 「接口请求失败，以下内容来自本机离线词库」这句提示会被立刻抹掉，
        # 而它正是兜底功能存在的意义。
        if entry_id == self._displayed_id:
            return
        if self._mode == MODE_PHRASES:
            # 词组详情走另一条渲染路径（`show_phrase`），**绝不复用 `show_entry`**：
            # 那条路径会建出备注与标签两个输入框，而它们发信号时带的是词条的 id
            # ——词组的 id 落进去会指到另一张表里的同号词条。详见 `show_phrase`。
            phrase = self.repo.get_phrase_by_id(entry_id)
            self._displayed_id = None if phrase is None else entry_id
            self.detail.show_phrase(phrase)
            return
        entry = self.repo.get_by_id(entry_id)
        # 点击列表打开的词条不经过 DictionaryService 的缓存装饰，需在此补上
        # 「提示词版本是否落后」的标记，否则同一个词条搜索打开会提示、点击打开不提示。
        self._displayed_id = None if entry is None else entry_id
        self._show_entry(self.service.mark_stale(entry))

    def _show_entry(self, entry: Entry | None) -> None:
        """把词条渲染到详情区（**唯一入口**，两个调用点共用）。

        「已收藏的含本词的词组」由这里查好传进去，而不是让详情面板自己去访问
        仓储：那样渲染器就有了第二个数据来源，任何一次写库都可能只更新了其中
        一个，而界面上完全看不出来。
        """
        collected = [] if entry is None else self.repo.phrases_containing(entry.lemma)
        self.detail.show_entry(entry, collected)

    def _apply_card_selection(self, current: QListWidgetItem | None) -> None:
        """把「当前选中项」反映到卡片外观上。

        列表项自身的选中底色在这派不上用场——卡片把项矩形整个盖住了，看不见。
        所以选中态由卡片自己画，这里只负责告诉它该不该亮。
        """
        if self._selected_card is not None:
            self._selected_card.set_selected(False)
        card = self.list.itemWidget(current) if current is not None else None
        if isinstance(card, EntryCard):
            card.set_selected(True)
            self._selected_card = card
        else:
            self._selected_card = None

    def _on_order_changed(self, _index: int) -> None:
        self._flush_edits()
        self._reload_list()

    # ---------- 备注与标签（自动保存） ----------

    def _on_note_changed(self, entry_id: int, text: str) -> None:
        self._pending_note = (entry_id, text)
        self._note_timer.start()

    def _on_tags_changed(self, entry_id: int, text: str) -> None:
        self._pending_tags = (entry_id, text)
        self._note_timer.start()

    def _flush_edits(self) -> None:
        """把压在内存里的备注与标签落库。

        两条待写值各自**重新从库里读一次**再写，而不是复用同一份 entry 对象：
        `repo.upsert()` 写的是整行，若两次写入共用一份旧副本，后写的那次会把
        先写的改动覆盖回去——两条都报「已保存」，其中一条静默消失。重新读一次
        的代价是一次 SELECT，而它换来的是「谁先写谁后写都不丢」。

        落库的形态也要在这里统一（`join_tags`）：界面输入允许逗号与空格混用，
        而库里存的必须是单空格分隔，否则按标签筛选只能退化成子串匹配。
        """
        jobs: list[tuple[int, str, str]] = []
        if self._pending_note is not None:
            entry_id, text = self._pending_note
            jobs.append((entry_id, "notes", text))
        if self._pending_tags is not None:
            entry_id, text = self._pending_tags
            jobs.append((entry_id, "tags", text))
        self._pending_note = None
        self._pending_tags = None

        for entry_id, field, value in jobs:
            entry = self.repo.get_by_id(entry_id)
            if entry is None:
                continue  # 写回之前那条已被删掉：没什么可保存的
            if field == "tags":
                value = join_tags(value)
            # 「值没变就不写」：落库会顺带重建 FTS 行，而这条路径由输入防抖
            # 触发，用户在备注里每停 600ms 就写一次库是没必要的。
            if getattr(entry, field) == value:
                continue
            setattr(entry, field, value)
            self.repo.upsert(entry)
            self.statusBar().showMessage(
                f"{'备注' if field == 'notes' else '标签'}已保存 · {entry.lemma}"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._flush_edits()
        self._save_state()
        # 有托盘时，「关窗口」只是收起来。这一步必须排在下面那些取消动作**之前**：
        # 收进托盘之后导入、重查都还在跑，把它们取消掉属于另一个 bug。
        if self._should_hide_to_tray():
            event.ignore()
            self.hide()
            self._hint_hidden_to_tray()
            return
        # 真的退出：先撤托盘与热键。热键是系统级独占资源，进程走了却没撤，
        # 下一次启动会注册失败，而用户看到的只是「热键突然不好使了」。
        self._teardown_tray()
        # 导入 / 重查正跑到一半被关窗口：进度框是 `self` 的 WindowModal 子窗口，
        # 父窗口关掉之后它可能留在屏幕上——而到那时已经没有任何东西能把它的状态
        # 收回来，用户看到的就是一个「关不掉的 100%」。先取消，再把框撤掉。
        #
        # 这里不走 `_dismiss_progress`：那个函数会跑一次 processEvents，而在
        # closeEvent 里再进一次事件循环，等于在关闭过程中触发重绘与回调。
        self._import_cancelled = True
        self._refresh_cancelled = True
        for attr in ("_import_progress", "_refresh_progress"):
            progress = getattr(self, attr, None)
            setattr(self, attr, None)
            if progress is not None:
                progress.setCancelButton(None)
                progress.hide()
                progress.close()
        super().closeEvent(event)

    # ---------- 查询 ----------

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.btn_query.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.btn_query.setText("查询中…" if busy else "查询")
        if busy and message:
            self.statusBar().showMessage(message)

    def _do_lookup(self) -> None:
        """输入框回车：一个词就查一个词，一串词就批量收录；词组模式下查词组。

        **模式决定语义**，不靠猜。生词本模式下这是「用户实际的使用姿势」：
        读文章时攒一串生词一次粘进来。此前这里把整串当作一个词去查——粘 10
        个词得到 1 条 lemma 是「hello world warm …」的记录，既不报错也不提示，
        用户以为 10 个词都收进去了。

        「一串」的判据不是「有没有空格」，而是**整串在本地有没有**：`ice cream`
        与 `New York` 都是带空格的单个词条，盲拆会把它们拆成四个词。所以先拿
        整串查一次本地缓存（只在本地找，不发网络请求），命中就当词组。

        词组模式下**整行当作一条词组**：`take off` 不该被拆成 take 与 off 收进
        生词本——那是写错集合，而且两条词条看着都正常，用户很难发现。
        """
        text = self.input.text().strip()
        if not text:
            return
        if self._import_progress is not None or self._refresh_progress is not None:
            # 批量查词与文件导入共用同一条队列与 id 快照，同时在飞会互相抢；
            # 批量重查虽自成一套，但它也在同一张表上连续写，混进来同样会把
            # 进度与报告搅乱。
            QMessageBox.information(self, "查询", "上一批还在处理，请稍等片刻。")
            return
        if self._mode == MODE_PHRASES:
            self._lookup_phrase(text)
            return
        words = split_words(text)
        if len(words) <= 1:
            self._lookup_word(text)
            return
        if self.service.cached(text) is not None:
            # 整串本身就是一个收过的词条（词组），照词组查——「ice cream」不该
            # 因为中间有空格就被拆成两个词。
            self._lookup_word(text)
            return
        self.input.clear()
        self._start_batch_lookup(words)

    def _start_batch_lookup(self, words: list[str]) -> None:
        """把一串词交给导入链路批量收录。

        **复用导入而不是另写一条批量**：分批、进度、取消、失败清单、「新增 /
        已存在」统计那一整套已经在那里了，重写一遍只会得到一份行为略有出入的
        副本（而两份的差异恰恰会在最不常走的那条路上）。唯一的区别是入口——
        没有文件，也不需要核对：这些词是用户刚刚亲手打进来的。
        """
        parsed = ParseResult(
            words=[ParsedWord(word=word, line=index) for index, word in enumerate(words, 1)],
            total_lines=1,
            source_format="输入框",
        )
        self._import_origin = "批量查词"
        self._start_import(parsed)

    def _lookup_word(self, word: str) -> None:
        # 查询会切换当前词条，压在内存里的备注先落盘，否则会丢
        self._flush_edits()
        self._begin_timing("lookup")
        seq = self._next_seq()
        self._set_busy(True, f"正在查询「{word}」…")
        submit(
            self.service.lookup,
            word,
            on_success=lambda result, s=seq: self._on_lookup_ok(result, s),
            on_error=lambda message, s=seq: self._on_error(message, s),
            on_finished=lambda s=seq: self._finish_content(s),
        )

    def _phrase_blocked_reason(self) -> str:
        """当前来源给不出词组时返回一句说明，否则返回空串。

        判据看**真实生效的 provider**，不看 `config["provider"]`——接口模式下
        没配密钥时 `build_provider()` 会静默退回离线词库，而 config 里仍写着
        "openai-compat"。按 config 判的话，用户会在「接口模式」下点查询词组、
        然后拿到一句「离线词库未收录」，完全对不上自己看到的那行状态。
        """
        name = str(getattr(self.service.provider, "name", "") or "")
        if supports_phrases(name):
            return ""
        return (
            "本机离线词库只收单词，不含词组，没有可以查询的来源。\n\n"
            "先到右上角「设置」接上接口，词组才有内容可查。"
        )

    def _lookup_phrase(self, text: str) -> None:
        """查一条词组：结果落进词组收藏，**不进生词本**。

        与 `_lookup_word` 分开而不是复用：两者的产物进不同的表，回调里要刷新的
        也是不同的集合（词组列表 vs 词条列表），合并只会得到一个到处判断
        「这次是哪种」的函数。
        """
        reason = self._phrase_blocked_reason()
        if reason:
            QMessageBox.information(self, "查询词组", reason)
            return
        self._flush_edits()
        self._begin_timing("phrase")
        seq = self._next_seq()
        self._set_busy(True, f"正在查询词组「{text}」…")
        submit(
            self.service.lookup_phrase,
            text,
            on_success=lambda result, s=seq: self._on_phrase_ok(result, s),
            on_error=lambda message, s=seq: self._on_error(message, s),
            on_finished=lambda s=seq: self._finish_content(s),
        )

    def _on_phrase_ok(self, result, seq: int) -> None:
        if not self._is_current(seq):
            return
        phrase, created = result
        self._present_phrase(phrase, created)

    def _present_phrase(self, phrase: Phrase, created: bool) -> None:
        """把查到的词组呈现到界面。

        顺序与 `_present_entry` 同一套理由：先切集合（切过去会清空
        `_displayed_id` 并重载一次列表），再声明「详情区显示的是哪一条」，
        最后带 select_id 重载。反过来的话，重载触发的选中回调会拿这个 id 去
        比对，发现「已在显示」而跳过——它跳过的是**整份重渲染**，而此刻详情区
        里还是切换模式时那个空占位。
        """
        self.input.clear()
        if self._mode != MODE_PHRASES:
            self._switch_mode(MODE_PHRASES)
        self._displayed_id = phrase.id
        self.detail.show_phrase(phrase)
        self._reload_list(select_id=phrase.id)
        if created:
            self.statusBar().showMessage(f"{phrase.phrase} — 已收藏到词组收藏")
        else:
            self.statusBar().showMessage(
                f"{phrase.phrase} — 已在词组收藏里，内容以先前那条为准"
            )

    def _refresh_word(self, word: str) -> None:
        if self._refresh_progress is not None:
            # 批量正在跑：同一条词条被两条路径同时写，最终留下谁的内容取决于
            # 谁后到，而界面上完全看不出差别。等这一轮结束再单独重查。
            self.statusBar().showMessage("正在批量重查，请等这一轮结束再单独重查")
            return
        # 重新查询会用接口内容整条覆盖，压在内存里的备注必须先落盘，
        # 否则回填时库里根本没有值可保留
        self._flush_edits()
        seq = self._next_seq()
        self._set_busy(True, f"正在重新查询「{word}」…")
        # 注意：refresh() 返回的是单个 Entry，不是 (entry, cached) 元组，
        # 因此必须走专用回调。两者契约不同，接错会抛 TypeError，
        # 界面表现为「重新查询必失败」（尽管库里其实已经更新成功）。
        submit(
            self.service.refresh,
            word,
            on_success=lambda entry, s=seq: self._on_refresh_ok(entry, s),
            on_error=lambda message, s=seq: self._on_error(message, s),
            on_finished=lambda s=seq: self._finish_content(s),
        )

    # ---------- 请求时序 ----------
    # 异步请求的返回顺序不保证与发起顺序一致：先发的慢请求可能后到。
    # 没有这道闸门时，旧结果会把界面「抢」回上一个词条——而输入框已经清空，
    # 用户根本察觉不到自己看的是别的词。因此每次内容请求都领一个序号，
    # 只有序号仍是最新的结果才允许上屏（成功 / 失败 / 收尾一律适用）。

    def _next_seq(self) -> int:
        self._content_seq += 1
        return self._content_seq

    def _is_current(self, seq: int) -> bool:
        return seq == self._content_seq

    def _finish_content(self, seq: int) -> None:
        if self._is_current(seq):
            self._set_busy(False)
            self._record_lookup()
        # 否则说明还有更新的请求在飞，忙碌态由它负责解除

    def _begin_timing(self, op: str) -> None:
        """记下这次操作的起点与名字。

        只留耗时与成败：统计不关心用户查的是哪个词，那是他最私人的东西之一。
        """
        self._lookup_started = time.monotonic()
        self._lookup_failed = False
        self._op_name = op

    def _record_lookup(self) -> None:
        """收尾时补一条查词事件。

        没有起点就不记：批量重查走的是 `service.refresh`，它不经过
        `_begin_timing`，而「重查」和「查词」在报告里是两件事。
        """
        started = self._lookup_started
        if not started:
            return
        self._lookup_started = 0.0
        duration = int(max(0.0, time.monotonic() - started) * 1000)
        analytics.record_event(
            "dictionary_lookup",
            {"duration_ms": duration, "success": not self._lookup_failed},
        )

    def _on_lookup_ok(self, result, seq: int) -> None:
        if not self._is_current(seq):
            return
        entry, cached = result
        # 没走缓存说明这次真的写了库。来源区分手输与剪贴板，用来判断热键有没有人用。
        # 词是什么不进统计——报告里只需要「新增了几个」。
        if not cached:
            analytics.record_event(
                "word_added", {"source_type": self._pending_source}
            )
        self._pending_source = "manual"
        self._present_entry(entry, self._origin_of(entry, cached))

    @staticmethod
    def _origin_of(entry, cached: bool) -> str:
        """把「这条内容是怎么来的」说成一句人话。

        不能只按「是否命中缓存」二分：非缓存的来源至少有三种——接口、离线
        词库（用户主动选）、以及接口失败后的离线兜底。把后两种一律说成
        「已从接口获取并保存」，等于在常驻可见的状态栏上把离线内容冒充成接口
        结果——正是兜底功能专门要避免的那件事，而且状态栏比详情区更难不看见。

        **判「是不是兜底」只能看 `fallback_reason`，不能看 `source`**：
        兜底词条的 source 也是 `"offline"`，与「用户主动选离线模式」完全同值。
        （同样的混同在详情区的缺项提示里也犯过——那里错用了 source 分支，
        于是接口模式下拿到兜底内容的用户被指引去「切到接口模式」。）
        """
        if cached:
            return "来自本地缓存"
        reason = (getattr(entry, "fallback_reason", "") or "").strip()
        if reason:
            # 兜底必须把「接口这次失败了」一并说出来。只报来源（离线词库）
            # 会让用户以为一切正常，直到某天发现内容一直是旧的。
            label = provider_label(entry.source) or "离线词库"
            return f"接口请求失败，已回落到{label}并保存"
        return f"已从{provider_label(entry.source) or '接口'}获取并保存"

    def _on_refresh_ok(self, entry, seq: int) -> None:
        """refresh() 只返回 Entry，与 lookup() 的元组契约不同，故单独处理。"""
        if not self._is_current(seq):
            return
        self._present_entry(entry, "已重新查询并更新")

    def _present_entry(self, entry, origin: str) -> None:
        """把查到的词条呈现到界面（两个入口共用，避免呈现逻辑各自漂移）。"""
        self.input.clear()
        # 查词的结果属于生词本。若用户此刻正在看词组收藏，先切回去：否则会出现
        # 「右侧显示刚查到的词、左栏列的全是词组」这种自相矛盾的一屏，而下面那句
        # `_reload_list(select_id=…)` 在词组集合里根本找不到这个词条的 id——
        # 它不会报错，只是什么都不选中。
        if self._mode != MODE_WORDS:
            self._switch_mode(MODE_WORDS)
        # 先声明「详情区显示的是哪一条」，再重载列表。顺序不能反：
        # 重载会用 select_id 程序化选中该项并触发 _on_item_changed，那里靠
        # 这个 id 判断「已显示、不必重读渲染」——晚一步就会把刚渲染好的
        # 运行时字段（兜底失败原因）用库里的副本覆盖掉。
        self._displayed_id = entry.id
        self._show_entry(entry)
        self._reload_list(select_id=entry.id)
        # 缺项必须按**来源能力**过滤，与详情区用同一个判据。离线词库结构上
        # 不含同义语，若在这里照喊，每查一个离线词，状态栏都会挂一句
        # 「缺少同义语」——状态栏是常驻的，恒定提示等于没有提示，
        # 真正的缺项（接口这次少给了一条）反而被淹没。
        missing = entry.missing_parts(provided_fields(entry.source))
        if missing:
            self.statusBar().showMessage(
                f"{entry.lemma} — {origin}，但缺少{'、'.join(missing)}"
            )
        else:
            self.statusBar().showMessage(f"{entry.lemma} — {origin}")

    def _on_error(self, message: str, seq: int) -> None:
        # 已被更新的请求取代时静默丢弃：用户已经在等新结果，
        # 为一次过期的失败弹窗只会造成困惑
        if not self._is_current(seq):
            return
        # 统计只记「哪一类操作失败了」，不记异常文本：异常里常带绝对路径、
        # 用户输入的词、甚至接口返回的片段。
        self._lookup_failed = True
        analytics.record_event("app_error", {"error_code": f"{self._op_name}_failed"})
        self.statusBar().showMessage("查询失败")
        QMessageBox.warning(self, "查询失败", message)

    # ---------- 批量重查 ----------
    # 详情区那条「重新查询」解决的是「这一条结果不理想」；接上接口之后真正要
    # 处理的是另一件事：整本生词本是离线词库攒下来的，现在全都要换成接口内容。
    # 逐个点的问题不是慢，而是**点了也没法知道查到哪儿了、还剩多少**。

    def _refresh_blocked_reason(self) -> str:
        """当前来源不足以支撑「重查」时返回一句说明，否则返回空串。

        判据必须看**真实生效的 provider**，不能看 `config["provider"]`：接口模式下
        没配密钥时，`build_provider()` 会静默退回离线词库，而 config 里仍然写着
        "openai-compat"。按 config 判的话，一键重查会一路跑完，逐条把接口内容
        换成离线内容——那是**降级覆盖**，而且不可撤销。
        """
        name = str(getattr(self.service.provider, "name", "") or "")
        if name == "offline":
            return (
                "当前用的是本机离线词库，没有可以重新获取的来源。\n\n"
                "先到右上角「设置」接上接口，词条才有更完整的内容可拿。"
            )
        if name == "mock":
            return (
                "当前是离线示例模式，只有几个演示词条，重查也拿不到真实内容。\n\n"
                "先到右上角「设置」接上接口。"
            )
        return ""

    def _do_refresh_all(self) -> None:
        if self._import_progress is not None or self._refresh_progress is not None:
            QMessageBox.information(self, "重新查询", "上一批还在处理，请稍等片刻。")
            return
        if self._mode != MODE_WORDS:
            # 按钮在词组模式下是隐藏的，正常点不到；这条挡的是程序化路径
            # （测试、快捷键、将来的右键菜单），以及隐藏之前已经排进队列的那一次。
            QMessageBox.information(
                self,
                "重新查询",
                "词组没有「过时」这回事（词组表没有提示词版本列），"
                "暂不支持批量重查。\n\n"
                "想换某条词组的释义：先在词组收藏里把它移出，"
                "再在词组模式下重新输入——已收藏的同名词组不会被覆盖。",
            )
            return
        blocked = self._refresh_blocked_reason()
        if blocked:
            QMessageBox.information(self, "重新查询", blocked)
            return
        # 批量重查会用接口内容整条覆盖内容字段。压在防抖里还没落库的备注与
        # 标签必须先写进去，否则回填时库里没有值可保留，那就是静默丢失。
        self._flush_edits()
        rows = self._refresh_candidates()
        if not rows:
            QMessageBox.information(self, "重新查询", "当前列表里没有可重查的词条。")
            return

        outdated = [row for row in rows if row[2]]
        dialog = RefreshDialog(
            total=len(rows),
            outdated=len(outdated),
            scope=self._refresh_scope(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = outdated if dialog.only_outdated else rows
        if not chosen:
            return
        self._start_refresh([(entry_id, lemma) for entry_id, lemma, _ in chosen])

    def _refresh_candidates(self) -> list[tuple[int, str, bool]]:
        """当前列表的 (id, 词形, 是否需要更新)。

        范围与列表完全一致：`_current_entries()` 同时管着搜索、标签筛选与排序，
        重查跟着它走，所以「看到的」就是「会被重查的」。

        上限必须放开（`unlimited=True`）：`LIST_LIMIT` 是**渲染**上限，不是重查
        范围的上限。沿用它会静默漏掉第 1001 条以后的词条，而进度条按漏掉之后的
        总数走完，看上去还「完成」了——少查的部分没有任何痕迹。
        """
        rows: list[tuple[int, str, bool]] = []
        for entry in self._current_entries(unlimited=True):
            if not entry.id:
                continue
            # 「需要更新」= 内容来自本机离线词库（含接口失败后的兜底），或由旧版
            # 提示词生成。这两种都能靠接口重查变好；已经是接口内容的则大概率
            # 只会拿到同一份结果，白白消耗额度。
            outdated = (entry.source or "") == "offline" or self.service.is_stale(entry)
            rows.append((entry.id, entry.lemma, outdated))
        return rows

    def _refresh_scope(self) -> str:
        """一句话说清这次会动到哪一批，用在对话框那句「重新查询…」里。"""
        query = self.filter.text().strip()
        if self._tag_filter and query:
            return f"标签「{self._tag_filter}」里搜索「{query}」命中的词条"
        if self._tag_filter:
            return f"带标签「{self._tag_filter}」的词条"
        if query:
            return f"搜索「{query}」命中的词条"
        return "生词本里的全部词条"

    def _start_refresh(self, rows: list[tuple[int, str]]) -> None:
        self._refresh_queue = list(rows)
        self._refresh_total = len(rows)
        self._refresh_done = 0
        self._refresh_updated = []
        self._refresh_kept = []
        self._refresh_cancelled = False

        progress = QProgressDialog(
            "正在重新查询…", "取消", 0, self._refresh_total, self
        )
        progress.setWindowTitle("重新查询")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        # 与导入不同，这里第一条就要一次接口往返（秒级起步），进度框必须立刻
        # 出现：让它等 500ms 才冒出来，用户先看到的是「按钮点了没反应」。
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel_refresh)
        progress.setValue(0)
        self._refresh_progress = progress
        self._next_refresh()

    def _cancel_refresh(self) -> None:
        # 只置标志：正在飞的那一条回来后会看到它，不再发下一条。从这里直接拆
        # 队列，等于和正在执行的回调抢着收尾。
        self._refresh_cancelled = True
        progress = self._refresh_progress
        if progress is not None:
            # 与导入同理：不立刻反馈的话，用户点完取消看到的是「原样不动」。
            progress.setLabelText("正在停止，等这一条结束…")
            progress.setCancelButton(None)

    def _next_refresh(self) -> None:
        """发一条、等它回来、再发下一条。

        刻意串行，不并发：接口一般按请求数计费，也有频率限制；一次撒出去几十个
        并发请求，换来的是「快一点」和一堆 429，以及一个没法按顺序解释的失败清单。
        """
        if self._refresh_cancelled or not self._refresh_queue:
            self._finish_refresh()
            return
        _entry_id, lemma = self._refresh_queue.pop(0)
        progress = self._refresh_progress
        if progress is not None:
            progress.setLabelText(
                f"正在重新查询「{lemma}」…"
                f"（{self._refresh_done + 1}/{self._refresh_total}）"
            )
        # `accept_fallback=False`：接口这次没答上来的词条抛错而不是拿兜底内容，
        # 于是它保持原样。批量跑几十条时没人盯得住每一条，这个判定必须由程序做。
        submit(
            self.service.refresh,
            lemma,
            accept_fallback=False,
            on_success=self._on_refresh_one_ok,
            on_error=lambda message, word=lemma: self._on_refresh_one_kept(
                message, word
            ),
        )

    def _on_refresh_one_ok(self, entry) -> None:
        # refresh 返回单个 Entry（与 lookup 的元组契约不同，接错会抛 TypeError）
        self._refresh_updated.append(entry.id)
        self._advance_refresh()

    def _on_refresh_one_kept(self, message: str, word: str) -> None:
        """这一条没换掉：内容、备注、标签全都还在库里，只是这一轮没更新。"""
        self._refresh_kept.append((word, message))
        self._advance_refresh()

    def _advance_refresh(self) -> None:
        self._refresh_done += 1
        progress = self._refresh_progress
        if progress is not None:
            progress.setValue(min(self._refresh_done, self._refresh_total))
        self._next_refresh()

    def _finish_refresh(self) -> None:
        progress, self._refresh_progress = self._refresh_progress, None
        if progress is not None:
            _dismiss_progress(progress)

        updated = set(self._refresh_updated)
        keep = self._displayed_id
        self._reload_list(select_id=keep)
        # 详情区那条若正好被重查过，必须重读一遍再渲染：`_reload_list(select_id=…)`
        # 会程序化选中它，而 `_on_item_changed` 看到 id 没变就直接返回——少了这一步，
        # 屏幕上留着的还是重查前的旧内容，看上去像重查没生效。
        if keep is not None and keep in updated:
            fresh = self.repo.get_by_id(keep)
            if fresh is not None:
                self._displayed_id = keep
                self._show_entry(self.service.mark_stale(fresh))
        self._report_refresh()

    def _report_refresh(self) -> None:
        if not self.isVisible():
            # 与 `_report_import` 同理：关窗口途中不该再弹结果框。
            return
        total = self._refresh_total
        done = self._refresh_done
        ok = len(self._refresh_updated)
        kept = self._refresh_kept
        stopped = self._refresh_cancelled

        bits = [f"更新 {ok} 条"]
        if kept:
            bits.append(f"保持原样 {len(kept)} 条")
        if stopped:
            bits.append(f"未处理 {total - done} 条")
        self.statusBar().showMessage(
            f"重新查询{'已停止' if stopped else '完成'} · " + " · ".join(bits)
        )

        # 一切顺利就不弹窗打断：结果已经在列表与状态栏里。用户要的是「一键」，
        # 不是「一键之后再点一次确定」。
        if not (stopped or kept):
            return

        lines: list[str] = []
        if stopped:
            lines.append(f"已停止，还有 {total - done} 条没处理。已经更新的不会撤销。")
            lines.append("")
        lines.append(f"更新 {ok} 条 · 保持原样 {len(kept)} 条")
        if kept:
            lines.append("")
            lines.append("保持原样（接口这次没返回可用内容）：")
            for word, why in kept[:8]:
                lines.append(f"　{word} — {_brief(why)}")
            extra = len(kept) - 8
            if extra > 0:
                saved = self._save_refresh_kept_list(kept)
                lines.append(f"　……另有 {extra} 条")
                if saved is not None:
                    lines.append(f"\n完整清单已写入：\n{saved}")

        QMessageBox.information(self, "重新查询结果", "\n".join(lines))

    @classmethod
    def _save_refresh_kept_list(cls, kept: list[tuple[str, str]]) -> Path | None:
        """把「这一轮没更新」的词条连同原因写下来。

        只说「有 5 条没更新」等于把问题丢回去：既不知道是哪几个，也没法拿这份
        清单到别处补查。
        """
        return cls._save_report(
            "refresh_kept",
            f"# 重新查询时接口未返回可用内容的词条（共 {len(kept)} 条）",
            [f"{word}\t{why}" for word, why in kept],
        )

    # ---------- 词条操作 ----------

    def _request_delete(self, entry_id: int) -> None:
        """删除请求的统一入口：推迟到本轮事件处理结束，再真正执行。

        删除会重建整个列表（`_reload_list()` → `clear()`），而此刻发出 `clicked`
        的按钮正压在自己的处理栈上——立刻把它连同所在卡片一起销毁，属于 Qt 明确
        不保证的行为（按钮 emit 之后还要访问自身成员）。确认框又会开一个嵌套
        事件循环，让风险更大。`singleShot(0)` 等按钮的处理栈退干净再动手。

        详情区的删除按钮走同一条路，虽然它目前的容器是 `deleteLater()` 销毁、
        当场不炸：那条安全性依赖**另一个类的实现细节**，哪天 `_reset()` 改成立即
        销毁就会变成偶发崩溃。同一个动作只留一条路径，才只有一处需要守。
        """
        QTimer.singleShot(0, lambda: self._delete_entry(entry_id))

    def _delete_entry(self, entry_id: int) -> None:
        entry = self.repo.get_by_id(entry_id)
        if entry is None:
            return
        answer = QMessageBox.question(
            self,
            "删除词条",
            f"确定要从生词本中删除「{entry.lemma}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        was_displayed = entry_id == self._displayed_id
        # 删除前把**整条**留下来，而不只是记一个 id：撤销要放回去的不只是那一行，
        # 还有备注、标签与收录变体——它们只存在这条记录里。
        #
        # 这里原先写的理由是「SQLite 会把删掉的行号分给下一个新词条，带着旧 id
        # 写回去会覆盖掉别人」。**取证后不成立**：`entries.id` 是
        # `INTEGER PRIMARY KEY AUTOINCREMENT`，SQLite 用 `sqlite_sequence`
        # 记住用过的最大值，删行之后也不会再把那个号发出去（自检里有一条断言
        # 专门拿它当反例钉住）。真正必须清空 id 的理由见 `_undo_delete`。
        self._last_deleted = ("word", entry)
        self.repo.delete(entry_id)
        # 「撤销」只在刚刚删过之后才有意义，所以它跟着这次删除出现；下一次删除
        # 会把候选换成新的那一条（见 _undo_delete）。
        self.btn_undo.setVisible(True)
        self.statusBar().showMessage(f"已删除「{entry.lemma}」")

        if not was_displayed:
            # 删的是别人：右侧正在看的那条毫发无伤，重载后把它选回来。列表上删一条
            # 本该是「随手清掉」，不该顺手把当前阅读位置也清掉。
            self._reload_list(select_id=self._displayed_id)
            return

        # 删的正是详情区显示的那条。这个标记必须清掉，**理由是「它记的是详情区
        # 此刻显示着谁」，而不是「行号会被复用」**——后者曾写在这里，取证后不
        # 成立（`entries.id` 是 AUTOINCREMENT，删行之后不会再把那个号发出去，
        # 自检里有反例钉着）。与 `_undo_delete` 里那句假声明同源，一并改掉：
        # 留下一条错的理由，下一个人会照着它去改代码。
        self._displayed_id = None
        self.detail.show_entry(None)
        self._reload_list()

    # ---------- 词组收藏 ----------

    def _on_collocation_collected(self, phrase: Phrase) -> None:
        """把搭配区那个「收藏」落库。

        **先落库、后改按钮**（`mark_collected`）：先改按钮再写库等于替数据库
        打包票，写失败时界面说着「已收藏」而库里什么都没有，而用户不会再去点
        第二次。

        「新增」与「本来就有」靠**收藏条数有没有涨**判定，不看别的：`add_phrase`
        是幂等的（同一条词组只写一次），它返回的行在两种情况下长得一样，
        分不出这次到底写没写进去。
        """
        before = self.repo.count_phrases()
        try:
            saved = self.repo.add_phrase(phrase)
        except (ValueError, sqlite3.Error) as exc:  # noqa: BLE001 - 落库失败要说人话
            QMessageBox.warning(self, "收藏失败", f"没能保存这条词组：{exc}")
            return
        if self.repo.count_phrases() > before:
            self.statusBar().showMessage(f"已收藏「{saved.phrase}」到词组收藏")
        else:
            self.statusBar().showMessage(
                f"「{saved.phrase}」已在词组收藏里（内容以先前那条为准）"
            )
        self.detail.mark_collected(phrase.phrase)
        self.detail.add_collected_phrase(phrase.phrase)
        self._sync_mode_buttons()

    def _request_phrase_delete(self, phrase_id: int) -> None:
        """词组删除请求的统一入口。推迟一轮的理由与 `_request_delete` 完全相同。"""
        QTimer.singleShot(0, lambda: self._delete_phrase(phrase_id))

    def _delete_phrase(self, phrase_id: int) -> None:
        phrase = self.repo.get_phrase_by_id(phrase_id)
        if phrase is None:
            return
        answer = QMessageBox.question(
            self,
            "移出词组收藏",
            f"确定要把「{phrase.phrase}」移出词组收藏吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        was_displayed = phrase_id == self._displayed_id
        # 与删除词条同一个取舍：留**整条**快照，而不是只记一个 id——撤销要放回去
        # 的是简释与例句，它们只存在这条记录里。
        self._last_deleted = ("phrase", phrase)
        self.repo.delete_phrase(phrase_id)
        self.btn_undo.setVisible(True)
        self.statusBar().showMessage(f"已移出词组收藏「{phrase.phrase}」")

        if not was_displayed:
            self._reload_list(select_id=self._displayed_id)
            return
        self._displayed_id = None
        self.detail.show_phrase(None)
        self._reload_list()

    # ---------- 撤销删除 ----------

    def _undo_delete(self) -> None:
        """把刚刚删掉的那条放回去（词条含备注、标签与收录变体；词组含简释与例句）。

        快照是一对 **(类别, 整条数据)**。类别不能省：两类数据分属两张表、
        各自从 1 发号，同一个数字在两边是毫不相干的两条记录。

        恢复词条时走的是 `upsert()`，但**入口形态不同**：`entry.id` 先被清空。

        清空它是必须的，理由是**用户自有字段不被回填覆盖**——而这条理由换过
        两版才站住，前两版都是假的，别再改回去：

        ① 「SQLite 会把删掉的行号分给下一个新词条」。不成立：`entries.id` 声明
           了 AUTOINCREMENT，行号不复用（自检里有反例钉住）。
        ② 「带着旧 id 会 UPDATE 到一行不存在的记录，于是静默落空」。也不成立：
           `upsert()` 的查找条件是 `WHERE lemma = ?`，**根本不按 id 找行**。

        站得住的是：`entry.id` 在这里唯一的用途是 `upsert()` 里
        `entry.id is None` 那个分支开关，它决定「同名声词条已存在时，备注与
        标签听谁的」。撤回来的是一份**删除前的快照**，而删除之后用户完全可能
        又把这个词查了回来、并在上面写下新的备注。保留快照的 id 会让它走
        「用户编辑过的对象」那一支，把用户新写的那句静默盖掉——两条路径都会
        报「已恢复」，看不到任何异常。走「回填」那一支则只补内容、不碰用户
        字段，与本程序其余地方的取舍一致。

        （`created_at` 一并清空是顺带的对齐，不影响分支：INSERT 时它会被写成
        当前时间，回填时会被库里的原值覆盖。）

        备注与标签能不能一起回来，正是上面那条取舍的两面：删除之后没有人把这个
        词再查回来时，整条快照原样落库（用户删除前写的东西一处不少）；而中间
        已经重新收录过它时，以库里现有的用户字段为准。所以「撤销」留下的不是
        一串 id，而是整条 `Entry`——用户删除前写的那部分往往是他唯一没别处
        备份的东西。
        """
        snapshot = self._last_deleted
        self._clear_undo()
        if snapshot is None:
            return
        # 撤销必须认得出删的是哪一类：词条与词组是两张各自从 1 发号的表，
        # 拿错表的 id 去恢复，轻则什么都不发生，重则把同号的那条**另一类数据**
        # 顶掉——而界面上两种情况都显示「已恢复」。
        kind, payload = snapshot
        if kind == "phrase":
            phrase = payload
            # 先把左栏切到词组收藏，再选中恢复的那条：`_reload_list(select_id=…)`
            # 是在**当前集合**里找那个 id，集合不对时它会安静地什么都不选中，
            # 更糟的是同一个数字往往真的存在——那次选中的是另一张表里的同号数据。
            if self._mode != MODE_PHRASES:
                self._switch_mode(MODE_PHRASES)
            # `add_phrase` 是幂等的：删掉之后用户又把它收藏过一次时，这里不会
            # 变出第二条（内容也仍然以先前那条为准）。
            restored = self.repo.add_phrase(phrase)
            self.statusBar().showMessage(f"已恢复词组「{restored.phrase}」")
            self._reload_list(select_id=restored.id)
            return
        entry = payload
        if self._mode != MODE_WORDS:
            self._switch_mode(MODE_WORDS)
        aliases = list(entry.aliases)
        entry.id = None
        entry.created_at = ""
        saved = self.repo.upsert(entry)
        for alias in aliases:
            self.repo.link_alias(alias, saved.id)
        self.statusBar().showMessage(f"已恢复「{saved.lemma}」")
        self._reload_list(select_id=saved.id)

    def _clear_undo(self) -> None:
        """撤销只保留最近一条，用过即失效。

        做成单步而不是撤销栈：删除本身已经有确认框拦一道，撤销是「事后救」，
        而栈会把「撤销」变成「撤销到哪一层」——用户要救的永远是刚刚手滑的
        那一下。按钮放在状态栏永久区，不会被后来的提示消息盖掉。
        """
        self._last_deleted = None
        self.btn_undo.setVisible(False)

    # ---------- 导入 ----------
    # 只有一条批量链路：读文件 → 解析 → 用户核对 → 分块后台导入 → 报告。
    # 三个入口都汇到 `_import_paths`（按钮与快捷键给一个文件，拖拽可能给多个），
    # 而输入框粘进来的一串词走 `_start_batch_lookup`——它从核对之后接入，因为
    # 那些词是用户刚亲手打的，没有「认错了」这回事。

    def _do_import(self) -> None:
        if self._import_progress is not None or self._refresh_progress is not None:
            # 进度框要等 minimumDuration 才会显示，在那之前窗口并不模态，
            # 用户完全来得及再点一次「导入」。两条导入线同时写库会互相
            # 抢 id 快照，报告里的「新增 / 已存在」也就不成立了。
            QMessageBox.information(self, "导入", "上一次导入还没结束，请稍等片刻。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入生词表",
            str(Path.home()),
            _WORD_FILTER,
        )
        if path:
            self._import_paths([Path(path)])

    def _import_paths(self, paths: list[Path]) -> None:
        """导入一批文件。**一个文件走老路，多个文件先合并成一次核对。**

        为什么不「依次调用 `_import_path`」：那个函数每份文件都会弹一次核对框、
        开一轮进度、报一次结果，拖 3 个文件就是 3 次模态加 3 次进度条——而用户
        做的是一个动作，期待的是一个结果。合并之后「新增 / 已存在」的数字才是
        对的：逐次导入时，第二次会把第一次刚收进去的词报成「已存在」。
        """
        if not paths:
            return
        if len(paths) == 1:
            self._import_path(paths[0])
            return
        self._flush_edits()
        self._import_origin = "导入"

        merged: dict[str, ParsedWord] = {}
        detail_lines: list[str] = []
        failed_lines: list[str] = []
        recognized = 0
        recognized_phrases = 0
        for path in paths:
            try:
                text = read_import_text(path)
            except (OSError, ValueError) as exc:
                failed_lines.append(f"{path.name} — 读不出：{exc}")
                continue
            parsed = parse_text(text)
            recognized += parsed.count
            recognized_phrases += parsed.phrase_count
            detail_lines.append(
                f"　{path.name} — {parsed.count} 条（{parsed.source_format}）"
            )
            for item in parsed.words:
                # 键带上 kind：同一个字符串不可能既是单词又是词组，但**列表里
                # 同时出现两种候选时**，跨文件合并必须按「东西本身」去重，
                # 而不是按字符串——将来若允许一条词组与某个词同形，这里不会
                # 悄悄替用户丢掉一条。
                merged.setdefault(f"{item.kind}:{item.word.lower()}", item)

        words = list(merged.values())
        if not words:
            QMessageBox.information(
                self,
                "导入",
                "这次拖入的 "
                f"{len(paths)} 个文件里没有认出任何单词或词组。\n\n"
                + "\n".join(detail_lines + failed_lines),
            )
            return

        lines = [
            f"这次拖入 {len(paths)} 个文件，共认出 {recognized} 条"
            f"（单词 {recognized - recognized_phrases} 条 · "
            f"词组 {recognized_phrases} 条）。"
        ]
        lines.extend(detail_lines)
        if failed_lines:
            lines.append("")
            lines.extend(failed_lines)
        cross = recognized - len(words)
        if cross > 0:
            lines.append(f"\n跨文件重复 {cross} 个，已合并。")
        lines.append(f"实际要收录 {len(words)} 条。")
        if recognized_phrases and self._phrase_blocked_reason():
            lines.append(
                f"\n注意：当前用的是本机离线词库，不含词组，"
                f"词组会被记为未能收录。要收词组请先到「设置」接上接口。"
            )
        lines.append("\n现在开始收录吗？已经收过的会自动跳过。")

        if (
            QMessageBox.question(
                self,
                "导入生词表",
                "\n".join(lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._start_import(ParseResult(words=words, total_lines=recognized))

    def _import_path(self, path: Path) -> None:
        """导入一个文件。读不动、认不出词都在这里收住并说明原因。"""
        self._flush_edits()
        self._import_origin = "导入"
        try:
            text = read_import_text(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导入失败", f"无法读取「{path.name}」：{exc}")
            return

        parsed = parse_text(text)
        if not parsed.words:
            # 说清「按什么规则找词」，用户才知道该怎么改文件；
            # 只回一句「没有单词」，等于让他自己猜。
            QMessageBox.information(
                self,
                "导入",
                f"没有从「{path.name}」里认出单词或词组。\n\n"
                f"文件共 {parsed.total_lines} 行。本程序认这些来源：\n"
                "　· 生词表 —— 每行取第一个英文词，自动忽略行首编号、音标、"
                "括号和中文释义\n"
                "　· 词组 —— 整行是 2–4 个纯英文词时按词组收录，进词组收藏\n"
                "　· Quill 导出的纯文本 / Markdown / JSON —— 按导出格式读\n"
                "　· 有道单词本导出的 XML —— 按 <word> 与 <trans> 读\n\n"
                "如果这是本程序导出的纯文本文件，请确认词条行首的编号还在"
                "（少了编号就没法把它和释义行区分开）。",
            )
            return

        if not self._confirm_import(path, parsed):
            return
        self._start_import(parsed)

    def _confirm_import(self, path: Path, parsed: ParseResult) -> bool:
        """导入前让用户核对识别结果。

        这一步不能省：解析终究是猜的，而导入会**写入**生词本。把认出来的词
        摆到眼前，用户一眼就能发现「我的文件被理解错了」，而不是等导入完
        再去一堆词条里翻找哪些是垃圾。

        「怎么认出来的」也要写明白：同一份文件被当成两种格式读会得到不同的
        结果（比如本程序导出的文件按逐行取词会把释义行挖成假词条），用户
        只有知道程序走的是哪条路，才能判断结果是否可信。
        """
        # 映射表在模块级：它与解析器返回的 source_format 是两份定义，自检里
        # 有一条断言盯着它们不许分家（见 `_FORMAT_HOW` 上方的说明）。
        how = _FORMAT_HOW.get(parsed.source_format, _FORMAT_HOW_DEFAULT)

        lines = [
            f"从「{path.name}」里认出 {parsed.count} 条"
            f"（单词 {parsed.word_count} 条 · 词组 {parsed.phrase_count} 条，"
            f"{parsed.source_format}）。",
            f"识别依据：{how}。",
            "",
            f"　{parsed.preview()}",
        ]
        if parsed.phrase_count:
            # 词组与单词进的是两个集合。不写明的话，用户在生词本里找不到刚
            # 导进来的那几条，会以为收失败了。
            lines.append(
                f"\n其中 {parsed.phrase_count} 条是词组，会进「词组收藏」，"
                "不占生词本的条数。"
            )
        if parsed.duplicates:
            lines.append(f"\n文件里重复 {parsed.duplicates} 个，已合并。")
        if parsed.skipped:
            lines.append(f"\n跳过 {len(parsed.skipped)} 行：")
            for no, why in parsed.skipped[:3]:
                lines.append(f"　第 {no} 行 — {why}")
            if len(parsed.skipped) > 3:
                # 只列 3 行是为了让框不至于撑散，但这个框存在的意义恰恰是
                # 「让用户看出文件被理解错了」——只给前 3 行时，他看不出全局
                # （跳过的行往往集中在文件后半段）。所以剩余的**整份落盘**，
                # 与「未能收录」那份清单走同一个写法。
                lines.append(f"　……另有 {len(parsed.skipped) - 3} 行")
                saved = self._save_skipped_list(parsed.skipped)
                if saved is not None:
                    lines.append(f"（完整清单已写入：{saved}）")
        if parsed.phrase_count and self._phrase_blocked_reason():
            # 明知会失败就别装没事：离线词库结构上没有词组，这 N 条会一条条
            # 报「本机离线词库只收单词」。先说清楚，用户还能当场取消去接接口。
            lines.append(
                f"\n注意：当前用的是本机离线词库，不含词组，"
                f"这 {parsed.phrase_count} 条词组会被记为未能收录。"
                "要收词组，请先到「设置」接上接口。"
            )
        lines.append("\n现在开始收录吗？已经收过的会自动跳过。")

        return (
            QMessageBox.question(
                self,
                "导入生词表",
                "\n".join(lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _start_import(self, parsed: ParseResult) -> None:
        self._import_queue = list(parsed.words)
        self._import_total = len(self._import_queue)
        self._import_done = 0
        self._import_outcome = ImportOutcome()
        # id 快照整批共用，取一次。每批重扫全库既慢，又会在「同一个词在文件里
        # 出现两次」时把第二次误判成新增——快照是导入前的，两次都查出来的
        # 是同一个 id。
        self._import_known = self.repo.all_ids()
        self._import_cancelled = False
        self._import_error = ""

        progress = QProgressDialog(
            "正在导入…", "取消", 0, self._import_total, self
        )
        progress.setWindowTitle("导入生词")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        # 离线词库导入几百个词是瞬间的事，别让进度条闪一下反而碍眼；
        # 接口模式下一个词一次请求，那时它才该出现。
        progress.setMinimumDuration(500)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel_import)
        progress.setValue(0)
        self._import_progress = progress
        self._next_import_batch()

    def _cancel_import(self) -> None:
        # 只置标志。正在飞的那一批会在词与词之间看到它，批次之间也会；
        # 从这里直接把队列拆掉，等于和正在执行的回调抢着收尾。
        self._import_cancelled = True
        progress = self._import_progress
        if progress is None:
            return
        # 光是置标志的话，界面在接下来这一批跑完之前**一动不动**：接口模式下
        # 那是 10 个词、几十秒。用户点完取消，看到进度值停住、按钮还在，读到的
        # 结论就是「取消没用、这框关不掉」。所以立刻给出受理反馈，并把按钮摘掉，
        # 让它不可能被再点一次。
        progress.setLabelText("正在停止，等这一批结束…")
        progress.setCancelButton(None)

    def _next_import_batch(self) -> None:
        if self._import_cancelled or not self._import_queue:
            self._finish_import()
            return
        batch = self._import_queue[:IMPORT_BATCH]
        del self._import_queue[:IMPORT_BATCH]
        size = len(batch)
        submit(
            run_import,
            self.service,
            self.repo,
            batch,
            known=self._import_known,
            is_cancelled=lambda: self._import_cancelled,
            on_success=lambda outcome, n=size: self._on_import_batch(outcome, n),
            on_error=self._on_import_batch_failed,
        )

    def _on_import_batch(self, outcome: ImportOutcome, size: int) -> None:
        self._import_outcome.merge(outcome)
        self._import_done += size
        progress = self._import_progress
        if progress is not None:
            progress.setValue(min(self._import_done, self._import_total))
        self._next_import_batch()

    def _on_import_batch_failed(self, message: str) -> None:
        """整批失败——不是某一个词的问题，而是环境级故障。

        但**不能因此把已写进去的那些词说成没写**：仍然按真实清单收尾，
        只把这次的问题附在后面。
        """
        self._import_queue.clear()
        self._import_error = message
        self._finish_import()

    def _finish_import(self) -> None:
        progress, self._import_progress = self._import_progress, None
        if progress is not None:
            _dismiss_progress(progress)

        self._reload_list(select_id=self._focus_after_import())
        self._report_import()
        # 统计只留「这一批进了几条、有没有半路停下」，不留导入了哪些词。
        # 放进条数而不是文件名：文件名常带日期、书名甚至人名。
        _added = len(self._import_outcome.added) + len(
            self._import_outcome.phrases_added
        )
        analytics.record_event(
            "import_completed",
            {
                "count": _added,
                "success": not (
                    self._import_outcome.cancelled or bool(self._import_error)
                ),
            },
        )
        # 报告读完这一轮的计数之后把它们清干净。窗口不会为下一次导入重建，这些
        # 字段会一直挂着上一轮的数字——任何读它们的地方（调试、日志、将来的功能）
        # 都会把旧数字当成这一轮的结果。`_import_outcome` 刻意留着：它承载的是
        # 「刚刚这一批干了什么」，本来就该在报告之后还能查。
        self._import_queue.clear()
        self._import_total = 0
        self._import_done = 0
        self._import_cancelled = False
        self._import_error = ""

    def _focus_after_import(self) -> int | None:
        """批量查词结束后，把详情区切到刚收进来的第一条。

        导入文件时不需要这个（用户要的是「收进生词本」，结果在列表里看得见）；
        而从输入框粘一串词时，用户的注意力在右侧详情区——不给他切过去，
        「粘了 10 个词只收进 1 个」与「收进 10 个」在屏幕上几乎看不出差别。
        """
        if self._import_origin != "批量查词" or not self._import_outcome.added:
            return None
        entry = self.repo.get(self._import_outcome.added[0])
        return entry.id if entry else None

    def _report_import(self) -> None:
        if not self.isVisible():
            # 窗口已经在关闭途中：状态栏没人看，模态框还会在关掉之后弹出来。
            # 收尾该做的事（重载列表、清状态）上面已经做完，这里只是不报告。
            return
        outcome = self._import_outcome
        bits = [f"新增 {len(outcome.added)} 条"]
        # 词组单独报：它与生词本不是一个集合，合并成一个数字，用户对账时
        # 会发现「说新增 5 条，生词本只多了 3 条」。
        if outcome.phrases_added:
            bits.append(f"新增词组 {len(outcome.phrases_added)} 条")
        if outcome.existing:
            bits.append(f"已存在 {len(outcome.existing)} 条")
        if outcome.phrases_existing:
            bits.append(f"词组已存在 {len(outcome.phrases_existing)} 条")
        if outcome.failed:
            bits.append(f"未能收录 {len(outcome.failed)} 条")
        if outcome.notes_restored:
            bits.append(f"带回备注 {outcome.notes_restored} 条")
        summary = " · ".join(bits)

        # 这一批是「导入文件」还是「输入框批量查词」，报告里必须说清：两者走的
        # 是同一条链路，但从用户的视角是两件事——把批量查词说成「导入完成」，
        # 他会去找自己什么时候导入了文件。
        source = self._import_origin
        stopped = outcome.cancelled or bool(self._import_error)
        self.statusBar().showMessage(
            f"{source}{'已停止' if stopped else '完成'} · {summary}"
        )

        # 一切顺利就不再弹窗打断：结果已经在状态栏和列表里了。用户要的是
        # 「一键」，不是「一键之后再点一次确定」。
        if not (stopped or outcome.failed):
            return

        lines: list[str] = []
        if self._import_error:
            lines.append(f"{source}过程中出错：{self._import_error}")
            lines.append("")
        if outcome.cancelled:
            lines.append(
                f"已停止，还有 {len(self._import_queue)} 个词没处理。"
                "已经收进去的不会撤销。"
            )
            lines.append("")
        lines.append(
            f"新增 {len(outcome.added)} 条 · 已存在 {len(outcome.existing)} 条"
            f" · 未能收录 {len(outcome.failed)} 条"
        )
        if outcome.phrases_added or outcome.phrases_existing:
            lines.append(
                f"词组：新增 {len(outcome.phrases_added)} 条"
                f" · 已存在 {len(outcome.phrases_existing)} 条（在「词组收藏」里）"
            )
        if outcome.notes_restored:
            lines.append(f"其中 {outcome.notes_restored} 条从文件里带回了备注。")

        if outcome.failed:
            lines.append("")
            lines.append("未能收录：")
            for word, why in outcome.failed[:8]:
                lines.append(f"　{word} — {_brief(why)}")
            extra = len(outcome.failed) - 8
            if extra > 0:
                saved = self._save_failed_list(outcome.failed)
                lines.append(f"　……另有 {extra} 条")
                if saved is not None:
                    lines.append(f"\n完整清单已写入：\n{saved}")

        QMessageBox.information(self, f"{source}结果", "\n".join(lines))

    @staticmethod
    def _save_report(prefix: str, header: str, rows: list[str]) -> Path | None:
        """把一份清单写进数据目录，返回落盘路径（失败返回 None）。

        「未能收录」与「跳过的行」两份清单共用这一处实现：它们的差别只在标题
        与行的写法，而**落盘这件事本身**（目录要先建、失败要安静）是一样的。
        各写一份的话，其中一份迟早会忘了 `ensure_dir()`——那会在第一次使用时
        抛 OSError，而它被同一个 except 收住，表现成「清单没生成」，看不出原因。
        """
        try:
            ensure_dir()
            path = APP_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.txt"
            path.write_text(
                "\n".join([header, "", *rows]) + "\n", encoding="utf-8"
            )
        except OSError:
            return None
        return path

    @classmethod
    def _save_failed_list(cls, failed: list[tuple[str, str]]) -> Path | None:
        """把未能收录的词连同原因写成一份清单。

        只报一句「有 5 条没进来」等于把问题丢回给用户：既不知道是哪几个，
        也没法拿这份东西去别处补。
        """
        return cls._save_report(
            "import_failed",
            f"# 导入时未能收录的单词（共 {len(failed)} 条）",
            [f"{word}\t{why}" for word, why in failed],
        )

    @classmethod
    def _save_skipped_list(cls, skipped: list[tuple[int, str]]) -> Path | None:
        """把「整行跳过」的清单写下来（核对框里只列得下前 3 行）。"""
        return cls._save_report(
            "import_skipped",
            f"# 导入时跳过的行（共 {len(skipped)} 行）",
            [f"第 {no} 行\t{why}" for no, why in skipped],
        )

    # ---------- 拖拽导入 ----------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._dropped_paths(event) or self._dropped_images(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._dropped_paths(event) or self._dropped_images(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """一次拖入 N 个文件：**一个都不丢，也不静默丢弃任何一个**。

        以前这里在循环里「第一个后缀匹配就 return」，于是拖 3 个 txt 只导入
        第一个、另外两个无声消失；拖「txt + jpg」时图片分支先命中，**生词表被
        整个忽略**。两种都不报错、不影响任何断言，只是让用户丢文件。

        现在图片与词表**都要处理**：它们的落点是同一个手势下的两类文件，不是
        二选一。图片先问（那是会改配置的动作），词表随后整批进导入。
        """
        images = self._dropped_images(event)
        paths = self._dropped_paths(event)
        if not images and not paths:
            return
        event.acceptProposedAction()
        if images:
            self._offer_background(images[0], ignored=len(images) - 1)
        if paths:
            self._import_paths(paths)

    @staticmethod
    def _pick_drops(event, suffixes: tuple[str, ...]) -> list[Path]:
        """从拖拽内容里挑出**全部**后缀匹配的本地文件（按拖入顺序，去重）。

        返回列表而不是「第一个」：原来的「第一个匹配就 return」正是这个缺陷
        本身——它在签名上就把「其余的被忽略」写成了既定事实，调用方连「还有
        没有别的文件」都无从知道，也就没有任何地方能提示用户。
        """
        data = event.mimeData()
        if not data.hasUrls():
            return []
        out: list[Path] = []
        for url in data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in suffixes and path.is_file() and path not in out:
                out.append(path)
        return out

    @classmethod
    def _dropped_paths(cls, event) -> list[Path]:
        """从拖拽内容里取出全部生词表文件。

        **只认 .txt / .md / .json / .xml**：拖进来一张图或一个 exe 时应当安静地
        不接手，而不是弹一句「无法读取」——用户本来就没打算往这里拖它。
        （图片有它自己的去处，见 `_dropped_images`。）
        """
        return cls._pick_drops(event, _WORD_SUFFIXES)

    @classmethod
    def _dropped_images(cls, event) -> list[Path]:
        """从拖拽内容里取出全部图片文件——用来设为窗口背景。

        后缀表必须与设置里文件对话框的过滤器一致：两处各写一份的话，会出现
        「对话框里选得到、拖进来却不认」这种自相矛盾的行为。
        """
        return cls._pick_drops(event, _IMAGE_SUFFIXES)

    # ---------- 导出 ----------

    def _do_export(self) -> None:
        # 先问格式与范围。**导出不受列表显示上限约束**是默认值：列表为流畅只
        # 显示前若干条，若导出跟着它走，打印稿会少掉一截而用户看不出来。
        #
        # 导出的对象是**当前集合**（生词本或词组收藏），与左栏所见一致。
        # 两个集合各有各的条数，因此这里的 total 必须与 `_current_entries`
        # 走同一个分派——两边不一致时会出现「说有 N 条可导出，实际导出 0 条」。
        phrases_mode = self._mode == MODE_PHRASES
        total = self.repo.count_phrases() if phrases_mode else self.repo.count()
        if not total:
            QMessageBox.information(
                self,
                "导出",
                "当前没有可导出的内容。" if phrases_mode else "当前没有可导出的词条。",
            )
            return

        query = self.filter.text().strip()
        dialog = ExportDialog(total, filtered=bool(query), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        entries = self._current_entries(unlimited=dialog.unlimited)
        if dialog.filtered_only and query:
            # 对话框里选的是「跟随当前筛选」，而 `_current_entries` 本来就跟随
            # 筛选框，这里无需额外过滤；保留分支是为了将来「筛选外的范围」扩展。
            entries = [e for e in entries if e.lemma]
        if not entries:
            QMessageBox.information(self, "导出", "当前没有可导出的内容。")
            return

        # 导出以字母序排版，便于打印后查阅；列表顺序是浏览用的，不适用于纸质版
        entries = sorted(entries, key=lambda item: item.lemma.lower())

        default_title = "词组收藏" if phrases_mode else "英语生词本"
        title = f"{default_title}（筛选：{query}）" if query else default_title
        key, label, _hint, ext = next(
            (k, lab, h, e) for k, lab, h, e in EXPORT_FORMATS if k == dialog.format_key
        )

        # 单词表与纯文本同为 .txt。默认文件名必须区分开，否则同一目录下第二次
        # 导出会弹出「已存在，是否覆盖」，而用户以为自己导的是另一种格式。
        name_suffix = "_单词表" if key == "wordlist" else ""
        suggested = str(
            Path.home() / f"{default_title}{name_suffix}_{datetime.now():%Y%m%d}{ext}"
        )
        filters = {
            "text": "纯文本 (*.txt)",
            "wordlist": "单词表 (*.txt)",
            "markdown": "Markdown (*.md)",
            "pdf": "PDF (*.pdf)",
            "csv": "表格 (*.csv)",
            "anki": "Anki 卡片 (*.txt)",
            "json": "JSON (*.json)",
        }[key]
        path, _ = QFileDialog.getSaveFileName(self, f"导出为{label}", suggested, filters)
        if not path:
            return

        try:
            self._write_export(key, entries, title, Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")
            return
        except PdfFontUnavailable as exc:
            # 字体缺失时程序不会静默产出一份通篇方块的 PDF，而是走到这里。
            # 这条消息必须说清「是环境的问题、不是你的词条有问题」，否则用户
            # 会开始怀疑数据。
            QMessageBox.warning(self, "无法导出 PDF", str(exc))
            return

        # 格式与条数足够判断「哪种导出真有人用」，路径与文件名不进统计。
        analytics.record_event(
            "export_completed", {"format": key, "count": len(entries)}
        )
        self.statusBar().showMessage(f"已导出 {len(entries)} 条")
        scope = "（当前筛选结果）" if query else ""
        extra = ""
        if key == "csv":
            extra = "\n\n已写入 BOM，Excel 打开不会乱码。同义语被压在一格里，是有损的。"
        elif key == "json":
            extra = "\n\n不含本机的编号与时间戳，可以安全地拷到别的机器。"
        elif key == "anki":
            extra = (
                "\n\n开头几行 `#` 是给 Anki 认的指令（分隔符、富文本、标签列），"
                "它导入时会自己读掉；用别的方式打开时删掉那几行即可。"
            )
        elif key == "wordlist":
            # 这里必须把「它不是备份」说清楚：两种 .txt 摆在同一个对话框里，
            # 用户很容易拿单词表当备份，等换机器时才发现例句与搭配都没了。
            extra = (
                "\n\n只有单词与中文释义两列，没有标题也没有表尾，"
                "整份文件可以再导回来。\n"
                "它不是备份：音标、例句、搭配、备注都没有带上，"
                "要完整备份请用 JSON。"
            )
        if (
            QMessageBox.question(
                self,
                "导出完成",
                f"已导出 {len(entries)} 条{scope}到：\n{path}{extra}\n\n现在打开它吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            == QMessageBox.StandardButton.Yes
        ):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _write_export(self, key: str, entries: list[Entry], title: str, path: Path) -> None:
        """把选定格式写到磁盘。**统一用 `utf-8` 编码**：

        `export_csv` 的返回值自带 BOM，所以这里不需要（也不该）用 `utf-8-sig`
        ——两处都加 BOM 会写出两个。把这件事收在一个函数里，就不存在
        「这个分支记得加、那个分支忘了加」的问题。
        """
        if key == "pdf":
            export_pdf(entries, path, title)
            return
        writers = {
            "text": export_text,
            "wordlist": export_wordlist,
            "markdown": export_markdown,
            "csv": export_csv,
            "anki": export_anki,
            "json": export_json,
        }
        content = writers[key](entries, title)
        path.write_text(content, encoding="utf-8")

    def _show_format_help(self) -> None:
        FormatHelpDialog(self).exec()

    # ---------- 备份 ----------

    def _do_backup(self) -> None:
        self._flush_edits()  # 备注可能还压在内存里，先落库再备份，否则会漏
        suggested = str(APP_DIR / f"quill_backup_{datetime.now():%Y%m%d_%H%M}.db")
        path, _ = QFileDialog.getSaveFileName(
            self, "备份数据库", suggested, "SQLite 数据库 (*.db)"
        )
        if not path:
            return
        try:
            saved = backup_to(self.repo.conn, path)
        except (OSError, sqlite3.DatabaseError) as exc:
            # backup_to 先写临时文件再原子替换，因此失败时目标文件保持原样
            QMessageBox.warning(
                self, "备份失败", f"未能完成备份：{exc}\n\n原有文件未被改动。"
            )
            return
        total = self.repo.count()
        self.statusBar().showMessage(f"已备份 {total} 条到 {saved.name}")
        QMessageBox.information(
            self,
            "备份完成",
            f"已备份 {total} 条词条到：\n{saved}\n\n"
            "这是一份完整的数据库文件，需要还原时直接覆盖 quill.db 即可。",
        )

    # ---------- 设置 ----------

    def _rebuild_search_index(self) -> int:
        """按当前词条重建全文索引，返回索引条数。供设置页那个按钮调用。

        事务边界收在这一处：成功才提交，失败先回滚再往上抛。不做回滚的话，
        连接会停在未提交状态，下一次任意 `commit()` 都会把 DROP 之后的半成品
        一并写下去，这正是「索引没了但程序不报错」的成因。

        重建后不需要刷新列表与详情区：索引只参与搜索召回，不参与它们的取数。
        """
        try:
            count = rebuild_fts(self.repo.conn)
            self.repo.conn.commit()
        except Exception:
            self.repo.conn.rollback()
            raise
        self.statusBar().showMessage(f"搜索索引已重建：{count} 条")
        return count

    def _restore_from_backup(self) -> None:
        """从备份恢复：选文件 → 校验 → 二次确认 → 交给 main 里的替换流程。

        校验放在问「确定吗」之前：让用户先为一份坏文件紧张一次，再告诉他
        「这份用不了」，是多余的惊吓。

        恢复完成后**主动退出**，不在运行中热切数据库。连接、FTS 索引、
        列表缓存、详情区全都指向旧文件，要「切完继续用」就得把每一层重建
        一遍，漏掉任何一处都会变成「看起来恢复了，其实还在写旧库」。
        """
        from ..core.db import inspect_backup

        hook = self.restore_hook
        if hook is None:
            self.statusBar().showMessage("恢复功能不可用：没有拿到数据库连接")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择备份文件", str(APP_DIR), "Quill 备份 (*.db);;所有文件 (*)"
        )
        if not path:
            return
        backup = Path(path)
        problem = inspect_backup(backup)
        if problem:
            QMessageBox.warning(
                self, "无法恢复", f"这份备份不可用：{problem}\n\n当前数据没有被改动。"
            )
            return
        answer = QMessageBox.question(
            self,
            "从备份恢复",
            "将用这份备份覆盖当前生词本。\n\n"
            "覆盖前会先把当前数据另存一份，但覆盖之后，词条、备注、"
            "复习进度都以后者为准。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            guard = hook(backup)
        except Exception as exc:  # noqa: BLE001 - 文件锁、权限、坏库都要如实说
            QMessageBox.critical(
                self, "恢复失败", f"{exc}\n\n当前数据没有被改动。"
            )
            return
        QMessageBox.information(
            self,
            "恢复完成",
            f"已从备份恢复。程序会退出，请重新打开。\n\n"
            f"替换前的那份数据另存为：\n{guard}",
        )
        self._quitting = True
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _open_settings(self) -> None:
        # 主题走即时预览：配色是**看着挑**的，若要先保存再看效果，用户得在设置
        # 与主窗口之间来回跑。取消时对话框会把主题还原（见 SettingsDialog.reject）。
        dialog = SettingsDialog(
            self.config,
            load_api_key(KEY_PATH),
            self,
            on_theme_preview=self._apply_theme,
            on_restore=self._restore_from_backup,
            on_rebuild_index=self._rebuild_search_index,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.config = dialog.result_config()
        # 主题在对话框里已经即时生效过，这里再应用一次是为了钉住「存下来的」与
        # 「画出来的」一致——它同时兜住那条没有回调的调用路径（自检 / 截图）。
        self._apply_theme(self.config.get("theme"))
        image = str(self.config.get("background_image") or "").strip()
        if image:
            self._remember_background(image)
        save_config(self.config)
        # 托盘开关就地生效：改完还要重启程序才生效的开关，用户会以为它坏了。
        self._apply_tray_enabled()
        self.service.set_provider(build_provider(self.config, load_api_key(KEY_PATH)))
        # 详情面板的「出路」文案取决于是否联网，切模式后必须同步，
        # 否则会沿用上一次的说法（离线内容让你去切接口模式，而你现在就在接口模式）
        self.detail.set_networked(self._is_networked())
        self._refresh_mode_label()
        # 背景图跟着配置走。加载失败要如实说出来——一句「设置已保存」会让用户
        # 以为背景换上了，然后对着一张没变化的窗口反复改设置。
        loaded = self._apply_background()
        if str(self.config.get("background_image") or "").strip() and not loaded:
            self.statusBar().showMessage(
                "设置已保存，但背景图没能加载（路径或文件格式有问题），已忽略背景"
            )
        else:
            self.statusBar().showMessage("设置已保存")
