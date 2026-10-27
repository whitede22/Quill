"""左侧词条卡片与右侧详情面板。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Entry, Phrase, join_tags
from ..providers.base import provided_fields, provider_label
from ..providers.prompts import PROMPT_VERSION


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


class EntryDetail(QWidget):
    """右侧详情区。

    每次渲染都重建内容控件，避免残留上一条词条的状态。重建期间用 _loading
    挡住备注框的 textChanged，否则赋值动作会被误判为用户编辑。
    """

    synonymClicked = Signal(str)
    refreshRequested = Signal(str)
    deleteRequested = Signal(int)
    noteChanged = Signal(int, str)
    tagsChanged = Signal(int, str)
    # 收藏一条固定搭配。携带的是**整条 Phrase**（含 source_lemma），不是 id：
    # 此刻库里还没有它，没有任何 id 可给。
    collocationCollected = Signal(object)
    # 词组模式下移除一条词组。与 deleteRequested 分开而不是共用一个信号：
    # 两边带的是**两张表的 id**，同一个数字在 entries 与 phrases 里是两条
    # 毫不相干的数据，合成一个信号就得靠调用方记住「现在是哪个模式」——
    # 记错的那一次会安静地删掉另一样东西。
    phraseDeleteRequested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entry: Entry | None = None
        self._note_edit: QPlainTextEdit | None = None
        self._tag_edit: QLineEdit | None = None
        self._warn_label: QLabel | None = None
        # 与缺项提示分开持引用：两者可同时出现，也可都不出现，
        # 复用同一个变量会让「哪一条在显示」变得不可判定。
        self._stale_label: QLabel | None = None
        self._fallback_label: QLabel | None = None
        self._source_label: QLabel | None = None
        # 搭配行上的「收藏」按钮，按词组文本索引。持引用才能在落库成功之后把
        # 它改成「已收藏」——先改再写库等于替数据库打包票，写失败时界面就在说谎。
        self._collect_buttons: dict[str, QPushButton] = {}
        # 「已收藏的含本词的词组」那块文字。与缺项/陈旧/兜底三块分开持引用，
        # 理由同它们之间那样：几块可同时出现，也可以都不出现。
        self._collected_label: QLabel | None = None
        # 详情区底部那一行操作（重新查询 / 删除词条）的布局。持引用是为了让
        # 「操作按什么顺序摆」这件事可被观测——布局并不挂在按钮的父控件上，
        # 从控件树反查拿不到它。
        self._actions_layout: QHBoxLayout | None = None
        # 当前接入方式是否会联网。决定「离线内容缺例句」时该给哪条出路：
        # 接口模式下点「重新查询」真的会去问接口；离线模式下重查的还是同一本
        # 词典，白点一次不如直说去设置里切模式。
        self._networked = False
        self._loading = False

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

        self._render_placeholder()

    # ---------- 渲染 ----------

    def _reset(self) -> QVBoxLayout:
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        body = QWidget()
        inner = QVBoxLayout(body)
        inner.setContentsMargins(26, 22, 26, 22)
        inner.setSpacing(11)
        self.scroll.setWidget(body)
        return inner

    def _render_placeholder(self, tip_text: str = "") -> None:
        self._entry = None
        self._note_edit = None
        self._tag_edit = None
        self._warn_label = None
        self._stale_label = None
        self._fallback_label = None
        self._source_label = None
        self._collect_buttons = {}
        self._collected_label = None
        self._loading = True
        inner = self._reset()
        tip = QLabel(
            tip_text
            or "在上方输入一个英语单词并回车，\n查询结果会自动收进左侧的生词本。"
        )
        tip.setObjectName("placeholder")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        inner.addStretch(1)
        inner.addWidget(tip)
        inner.addStretch(2)
        self._loading = False

    def set_networked(self, networked: bool) -> None:
        """告知当前接入方式是否会联网（由主窗口按配置同步）。

        用途只有一个：离线内容缺例句时给出**能走通**的那条出路。
        接口模式下说「切到接口模式」是废话，离线模式下说「点重新查询」
        是让人白点一次——两种说法各自只在一种模式下成立。
        """
        self._networked = bool(networked)

    def show_entry(self, entry: Entry | None, collected: list[str] | None = None) -> None:
        """渲染一个词条。

        `collected` 是「已收藏的、含这个词的词组」（由主窗口查好传进来）。
        详情面板自己不查库：它是一块渲染器，多一条数据来源就多一处能不一致。
        """
        if entry is None:
            self._render_placeholder()
            return

        self._entry = entry
        # 必须在 _build_head 之前清掉：标题行由 _build_head 写入，写完之后
        # 再置空会把刚拿到的引用抹掉，自检读到 None 却看不出哪里错了。
        self._source_label = None
        # 搭配行的按钮引用同样必须重置：这一轮渲染会把上一轮的控件销毁，
        # 留着旧引用去 setText 会抛 RuntimeError，而这条路径恰好在
        # 「连着查下一个词」时必经。
        self._collect_buttons = {}
        self._collected_label = None
        self._loading = True
        inner = self._reset()
        try:
            inner.addLayout(self._build_head(entry))
            inner.addWidget(_divider())

            # 缺项提示紧跟在标题下方：空字段是「不渲染」的，不说出来，
            # 用户就会把「接口没给同义语」当成「这个词没有同义语」。
            self._warn_label = None
            self._stale_label = None
            self._fallback_label = None

            # 兜底提示排在缺项之前——它说的是更靠前的一件事：「你选的接口这次
            # 没通」。不说的话，用户看到释义就以为接口是通的，直到发现同义语
            # 一直是空的、例句越来越少，才回头怀疑配置。
            reason = (getattr(entry, "fallback_reason", "") or "").strip()
            if reason:
                fallback = QLabel(f"接口请求失败，以下内容来自本机离线词库　{reason}")
                fallback.setObjectName("warn")
                fallback.setWordWrap(True)
                inner.addWidget(fallback)
                self._fallback_label = fallback

            # 只把该来源「结构上能给」的要素算作缺项。离线词库不含同义语，
            # 若照喊，每条离线词条底下都会挂一句「缺少同义语」——
            # 提示恒定出现就等于没有提示，真正的缺项反而被淹没。
            missing = entry.missing_parts(provided_fields(entry.source))
            if missing:
                # 判据是**当前是否联网**（`self._networked`），不是词条的来源。
                #
                # 曾经这里写的是 `entry.source == "offline"`，而兜底产出的词条
                # 来源**也是** "offline"——于是用户在接口模式下遇到接口失败、
                # 拿到一条兜底内容时，看到的出路是「可在设置中切到接口模式」，
                # 而他本来就在接口模式里。那句话让他去开一个已经开着的开关。
                #
                # 「来源是离线」与「用户选了离线」是两件事：前者说明这条内容
                # 从哪来，后者才决定出路该怎么给。`set_networked` 的说明早就
                # 写对了这件事，只是渲染时没用它。
                if self._networked:
                    text = (
                        f"此词条不完整，缺少：{'、'.join(missing)}　可点下方「重新查询」"
                    )
                else:
                    # 离线模式：重查还是同一本词典，让用户白点一次不如直说怎么补
                    text = (
                        f"此词条不完整，缺少：{'、'.join(missing)}　"
                        "离线词库只为常用词配例句，需要补全可在设置中切到接口模式"
                    )
                warn = QLabel(text)
                warn.setObjectName("warn")
                warn.setWordWrap(True)
                inner.addWidget(warn)
                self._warn_label = warn

            # 提示词版本落后的提示。与「不完整」分开说，因为两者性质不同：
            # 缺项是接口这次没给全，版本落后是这条内容本身按旧标准生成。
            # 只提示、不自动重查——自动重查会在离线时把「内容可能是旧的」
            # 变成「连旧的都看不到了」。
            if getattr(entry, "prompt_stale", False):
                # 版本 0 表示来源不明（正常写入路径不会产生），写成「v0」会让用户
                # 以为存在过一个叫 v0 的提示词。宁可说清楚是「未知」。
                was = (
                    f"v{int(entry.prompt_version)}"
                    if int(entry.prompt_version or 0) > 0
                    else "来源未知"
                )
                stale = QLabel(
                    f"此词条由旧版提示词生成（{was} → v{PROMPT_VERSION}），"
                    "内容可能未按当前标准　可点下方「重新查询」更新"
                )
                stale.setObjectName("warn")
                stale.setWordWrap(True)
                inner.addWidget(stale)
                self._stale_label = stale

            self._add_section(inner, "英", entry.definition_en)
            self._add_section(inner, "中", entry.definition_zh)

            if entry.synonyms:
                inner.addSpacing(4)
                inner.addWidget(_section_title(f"同义语　{len(entry.synonyms)} 个"))
                # 先把整批行建好再统一 chip 宽度，之后才挂进布局：
                # 等宽是「这批词」的属性，一行一行建就只能各自为政。
                synonym_rows = [
                    self._build_synonym_row(syn) for syn in entry.synonyms
                ]
                self._equalize_chips(synonym_rows)
                for row in synonym_rows:
                    inner.addLayout(row)

            # 固定搭配。位置排在例句**之前**：例句说的是「这个词」，搭配说的是
            # 「这个词怎么用」，后者才是查词时真正要解决的事。
            #
            # 整块不渲染的条件只是「没有搭配」，**不按来源分叉**：离线词库结构上
            # 就没有这一块，那种情况由上面的缺项提示如实说出来（缺项名单里含
            # 「固定搭配」），而不是在这里再判一次来源——两处判同一件事，
            # 迟早会有一处忘了改。
            if entry.collocations:
                inner.addSpacing(4)
                inner.addWidget(
                    _section_title(f"固定搭配　{len(entry.collocations)} 条")
                )
                for coll in entry.collocations:
                    inner.addWidget(self._build_collocation_block(entry, coll))

            # 已经收藏过的、含这个词的词组。纯本地查询，用来回答「这个词我到底
            # 攒过哪些搭配」——它和上面那批是两回事：上面是接口刚给的，这里是
            # 用户自己收的。
            if collected:
                inner.addSpacing(4)
                inner.addWidget(_section_title("已收藏的含本词的词组"))
                label = QLabel("、".join(collected))
                label.setObjectName("muted")
                label.setWordWrap(True)
                inner.addWidget(label)
                self._collected_label = label

            if entry.example:
                inner.addSpacing(4)
                inner.addWidget(_section_title("例句"))
                example = QLabel(entry.example)
                example.setObjectName("body")
                example.setWordWrap(True)
                inner.addWidget(example)
                if entry.example_zh:
                    example_zh = QLabel(entry.example_zh)
                    example_zh.setObjectName("muted")
                    example_zh.setWordWrap(True)
                    inner.addWidget(example_zh)

            inner.addSpacing(6)
            inner.addWidget(_section_title("备注"))
            self._note_edit = QPlainTextEdit()
            self._note_edit.setPlainText(entry.notes or "")
            self._note_edit.setPlaceholderText("为这个词写点什么…（自动保存）")
            self._note_edit.setFixedHeight(66)
            self._note_edit.textChanged.connect(self._on_note_changed)
            inner.addWidget(self._note_edit)

            # 标签。**这一行就是 `tags` 字段的全部入口**——在此之前它贯通了模型、
            # 数据库、FTS（权重 3.0，与备注同级）与导出，但界面上没有任何地方能
            # 编辑它，于是那一档权重永远挂在一个恒为空的列上。
            #
            # 值与用户输入之间必须过一道 `join_tags`：写进库里的形态只能是单空格
            # 分隔，否则按标签筛选就退化成子串匹配（筛「考研」带出「考研英语」）。
            inner.addSpacing(4)
            inner.addWidget(_section_title("标签"))
            self._tag_edit = QLineEdit(join_tags(entry.tags))
            self._tag_edit.setPlaceholderText("用空格或逗号分隔，例如：考研 高频（自动保存）")
            self._tag_edit.setToolTip("给这个词分类；左栏可以按标签筛选。")
            self._tag_edit.textChanged.connect(self._on_tags_changed)
            inner.addWidget(self._tag_edit)

            if entry.aliases:
                inner.addSpacing(2)
                alias = QLabel(f"收录变体　{'、'.join(entry.aliases)}")
                alias.setObjectName("muted")
                alias.setWordWrap(True)
                inner.addWidget(alias)

            inner.addSpacing(8)
            inner.addWidget(_divider())
            inner.addLayout(self._build_actions(entry))
            inner.addStretch(1)
        finally:
            self._loading = False

    def _build_collocation_block(self, entry: Entry, coll) -> QWidget:
        """一条固定搭配：词组 + 中文简释 + 收藏按钮，下面配它自己的例句。

        **例句必须跟着搭配走，不能并到上面那个例句区**：`be in charge of` 与
        `take charge of` 的差别只有各自的例句说得清。把搭配列成一张干巴巴的
        词组表，等于把「怎么用」这件最该看的东西丢了。
        """
        block = QWidget()
        col = QVBoxLayout(block)
        # 左侧缩进 2 格，与「英 / 中」两行的正文起点对齐：搭配是词条内容的
        # 一部分，不该看起来像另一个层级的东西。
        col.setContentsMargins(8, 0, 0, 0)
        col.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(8)

        phrase = QLabel(coll.phrase)
        phrase.setObjectName("collPhrase")
        phrase.setWordWrap(True)
        phrase.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(phrase, 1)

        if coll.gloss_zh:
            gloss = QLabel(coll.gloss_zh)
            gloss.setObjectName("collGloss")
            gloss.setWordWrap(True)
            head.addWidget(gloss, 0)

        collect = QPushButton("收藏")
        collect.setObjectName("chip")
        collect.setCursor(Qt.CursorShape.PointingHandCursor)
        collect.setToolTip("把这条搭配收进「词组收藏」")
        collect.clicked.connect(lambda _=False, c=coll: self._emit_collect(entry, c))
        head.addWidget(collect, 0, Qt.AlignmentFlag.AlignTop)
        # 按**词组文本**索引：同一个词条里两条搭配的文本不会相同（提示词要求
        # 按常用度给不同的搭配），而没有 id 可用——此刻库里还没有它。
        self._collect_buttons[coll.phrase] = collect
        col.addLayout(head)

        if coll.example:
            example = QLabel(coll.example)
            example.setObjectName("collExample")
            example.setWordWrap(True)
            col.addWidget(example)
            if coll.example_zh:
                example_zh = QLabel(coll.example_zh)
                example_zh.setObjectName("muted")
                example_zh.setWordWrap(True)
                col.addWidget(example_zh)
        return block

    def _emit_collect(self, entry: Entry, coll) -> None:
        """把「收藏」变成一条完整的 `Phrase` 交给主窗口落库。

        组装放在这里（而不是让主窗口回头去读 `self._entry` 再拼），是因为
        搭配的四个字段此刻只存在于这条 `Collocation` 上；绕一趟只会多一处
        「字段取漏了」的机会。
        """
        self.collocationCollected.emit(
            Phrase(
                phrase=coll.phrase,
                gloss_zh=coll.gloss_zh,
                example=coll.example,
                example_zh=coll.example_zh,
                source_lemma=entry.lemma,
            )
        )

    def add_collected_phrase(self, phrase_text: str) -> bool:
        """把一条刚收藏的词组追加到「已收藏的含本词的词组」那块文字里。

        单独一个方法、而不是重渲染整个词条：重渲染会把滚动位置与焦点一起丢掉，
        而用户刚刚点的就是这一屏上的一个按钮，他的注意力正在这里。

        **只在这一块已经存在时才追加**（返回 False 表示没有可追加的地方）。
        第一次收藏时那一块还没建出来——它由 `show_entry` 按「有没有已收藏的」
        决定要不要渲染——那种情况下这条词组会在**下一次渲染这个词条时**出现。
        宁可晚一格出现，也不要为了这一次而整屏重排。
        """
        text = (phrase_text or "").strip()
        if not text or self._collected_label is None:
            return False
        current = self._collected_label.text()
        if text in [part.strip() for part in current.split("、")]:
            return False
        self._collected_label.setText(f"{current}、{text}" if current else text)
        return True

    def mark_collected(self, phrase_text: str) -> bool:
        """把某条搭配的按钮改成「已收藏」。**必须在落库成功之后调用**。

        返回是否找到了那个按钮：找不到不是异常（用户可能已经切到别的词条，
        这一整屏都重建过了），但调用方拿它做断言时能分辨「按钮不在」与
        「按钮没变」——那是两件不同的事。
        """
        button = self._collect_buttons.get((phrase_text or "").strip())
        if button is None:
            return False
        button.setText("已收藏")
        button.setEnabled(False)
        button.setToolTip("这条搭配已在词组收藏里")
        return True

    def show_phrase(self, phrase: Phrase | None) -> None:
        """词组模式下的详情渲染。

        **刻意不复用 `show_entry`**：那条路径里有备注与标签两个输入框，它们
        发信号时带的是**词条的 id**，而词组与词条是两张各自从 1 发号的表——
        同一个数字在两边是两条毫不相干的数据。复用等于把「改这个的备注」
        接到「改那个的备注」上，而两边都会显示成功。
        """
        if phrase is None:
            self._render_placeholder(
                "在左侧选中一条词组，这里显示它的释义与例句。\n"
                "词组有两个来源：查词时右侧「固定搭配」区的「收藏」，"
                "以及在这个模式下直接输入词组查询。"
            )
            return

        self._entry = None
        self._note_edit = None
        self._tag_edit = None
        self._collect_buttons = {}
        self._collected_label = None
        self._source_label = None
        self._loading = True
        inner = self._reset()
        try:
            head = QHBoxLayout()
            head.setSpacing(10)
            title = QLabel(phrase.phrase)
            title.setObjectName("lemma")
            title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            head.addWidget(title)
            head.addStretch(1)
            source = QLabel("来源 词组收藏")
            source.setObjectName("muted")
            head.addWidget(source)
            self._source_label = source
            inner.addLayout(head)
            inner.addWidget(_divider())

            self._add_section(inner, "中", phrase.gloss_zh)
            if phrase.example:
                inner.addSpacing(4)
                inner.addWidget(_section_title("例句"))
                example = QLabel(phrase.example)
                example.setObjectName("body")
                example.setWordWrap(True)
                inner.addWidget(example)
                if phrase.example_zh:
                    example_zh = QLabel(phrase.example_zh)
                    example_zh.setObjectName("muted")
                    example_zh.setWordWrap(True)
                    inner.addWidget(example_zh)
            if phrase.source_lemma or phrase.created_at:
                bits: list[str] = []
                if phrase.source_lemma:
                    bits.append(f"收藏自「{phrase.source_lemma}」的固定搭配")
                if phrase.created_at:
                    bits.append(f"收藏于 {phrase.created_at}")
                meta = QLabel("　".join(bits))
                meta.setObjectName("muted")
                meta.setWordWrap(True)
                inner.addWidget(meta)

            inner.addSpacing(8)
            inner.addWidget(_divider())
            actions = QHBoxLayout()
            actions.setSpacing(8)
            actions.addStretch(1)
            remove = QPushButton("从词组收藏中移除")
            remove.setObjectName("bare")
            remove.setProperty("role", "destructive")
            remove.clicked.connect(
                lambda _=False: self.phraseDeleteRequested.emit(int(phrase.id or 0))
            )
            actions.addWidget(remove)
            self._actions_layout = actions
            inner.addLayout(actions)
            inner.addStretch(1)
        finally:
            self._loading = False

    # ---------- 区块 ----------

    def _build_head(self, entry: Entry) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        lemma = QLabel(entry.lemma)
        lemma.setObjectName("lemma")
        lemma.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(lemma)

        if entry.phonetic:
            phonetic = QLabel(entry.phonetic)
            phonetic.setObjectName("phonetic")
            row.addWidget(phonetic)

        if entry.pos:
            pos = QLabel(entry.pos)
            pos.setObjectName("pos")
            row.addWidget(pos)

        row.addStretch(1)

        if entry.source:
            source = QLabel(f"来源 {provider_label(entry.source)}")
            source.setObjectName("muted")
            row.addWidget(source)
            # 持引用供自检读取：兜底产出的词条必须看得出「不是接口给的」
            self._source_label = source
        return row

    def _equalize_chips(self, rows: list[QHBoxLayout]) -> None:
        """把一批同义语 chip 拉成等宽，让右侧释义成为一列。

        宽度**派生自当前这批词**（取最长的那个），不写死常量：写死时遇到一个
        长词——比如 conscientious——chip 会撑出省略号，比原本的参差更难读。

        上限保护：最宽的那个超过面板宽度的 40% 就整体放弃等宽。一个长词会把
        整列释义挤到下一行，那种局面比「左边不齐」坏得多。
        """
        chips = [
            widget
            for widget in (row.itemAt(0).widget() for row in rows if row.count())
            if isinstance(widget, QPushButton)
        ]
        if len(chips) < 2:
            return
        # 先 polish 再量：样式表里的内边距与边框要等样式生效后才进得了 sizeHint，
        # 否则量出来的宽度少一圈，chip 反而会被拉窄而截断文字。
        for chip in chips:
            chip.ensurePolished()
        widest = max(chip.sizeHint().width() for chip in chips)
        reference = self.scroll.viewport().width() or self.width()
        # 宽度拿不到时（窗口还没排过版）不做上限判断：不等宽是确定的差，
        # 而「可能偏宽」只是猜测。
        if reference > 0 and widest > reference * 0.4:
            return
        for chip in chips:
            chip.setMinimumWidth(widest)

    def _build_synonym_row(self, syn) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        chip = QPushButton(syn.word)
        chip.setObjectName("chip")
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setToolTip(f"点击查询 {syn.word}")
        chip.clicked.connect(lambda _=False, w=syn.word: self.synonymClicked.emit(w))
        row.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)

        # 释义与差异说明用**两个 QLabel**，颜色一律由样式表按 objectName 给。
        #
        # 原先这里是一段富文本，颜色写死成 `#1F1F1E` / `#8A8A86`——那正是**浅色
        # 主题**的 text / text2 两个字面值。深色主题（night）的卡片底色是
        # `#24262A`，`#1F1F1E` 压在上面只有 1.10:1，**中文简释整个看不见**：
        # 不报错、不影响任何断言，只是让一排释义消失。凡是「某个颜色在另一套
        # 主题下失效」，根因都是它被写在了样式表之外。
        #
        # 拆成两个标签而不是继续拼富文本，是因为一个 QLabel 只有一个 color：
        # 要让释义比差异说明重一档，就只能往里塞字面颜色。
        info = QWidget()
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(1)
        if syn.gloss_zh:
            gloss = QLabel(syn.gloss_zh)
            gloss.setObjectName("synGloss")
            gloss.setWordWrap(True)
            info_layout.addWidget(gloss)
        if syn.diff_note:
            note = QLabel(syn.diff_note)
            note.setObjectName("synNote")
            note.setWordWrap(True)
            info_layout.addWidget(note)
        row.addWidget(info, 1)
        return row

    def _build_actions(self, entry: Entry) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        # 弹簧放在**最前**，整组靠右收拢。原先夹在两个按钮中间，把它们甩到面板
        # 两端、中间空一大片，看起来像两个互不相关的按钮。
        row.addStretch(1)

        refresh = QPushButton("重新查询")
        refresh.setObjectName("bare")
        refresh.setToolTip("忽略本地缓存，重新向接口请求")
        refresh.clicked.connect(lambda _=False: self.refreshRequested.emit(entry.lemma))
        row.addWidget(refresh)

        remove = QPushButton("删除词条")
        remove.setObjectName("bare")
        # 常驻不变色，只在悬停时提示这是破坏性动作（样式见
        # QPushButton#bare[role="destructive"]:hover）。常驻报警会把它变成这一屏
        # 最显眼的按钮，而它恰恰是最不该被顺手点到的那个。
        remove.setProperty("role", "destructive")
        remove.clicked.connect(
            lambda _=False: self.deleteRequested.emit(int(entry.id or 0))
        )
        row.addWidget(remove)

        # 持引用供界面测试读取「这一行有哪些操作、按什么顺序」
        self._actions_layout = row
        return row

    def _add_section(self, layout: QVBoxLayout, tag: str, text: str) -> None:
        if not text:
            return
        row = QHBoxLayout()
        row.setSpacing(10)
        label = QLabel(tag)
        label.setObjectName("section")
        label.setFixedWidth(18)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        value = QLabel(text)
        value.setObjectName("body")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(label)
        row.addWidget(value, 1)
        layout.addLayout(row)

    # ---------- 事件 ----------

    def _on_note_changed(self) -> None:
        if self._loading or self._entry is None or self._entry.id is None:
            return
        if self._note_edit is None:
            return
        self.noteChanged.emit(int(self._entry.id), self._note_edit.toPlainText())

    def _on_tags_changed(self) -> None:
        """标签改动 → 交给主窗口去落库（与备注同一条路：自动保存、防抖）。

        这里**不判「值有没有变」**：那是落库侧的事（`_flush_edits` 会与库里
        的值比对）。在渲染过程中，`setText` 也会触发这个回调——挡住它的手段
        是 `self._loading`，而不是拿值去猜「这次是不是用户敲的」。
        """
        if self._loading or self._entry is None or self._entry.id is None:
            return
        if self._tag_edit is None:
            return
        self.tagsChanged.emit(int(self._entry.id), self._tag_edit.text())


class _ElidedLabel(QLabel):
    """按可用宽度省略的标签。

    QLabel 自己没有省略能力：文字超出宽度会从字形中间硬切，卡片窄一点就出现半个
    字。这里按字体度量算省略号位置，并在每次 resize 后重算。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        # 不把 text 交给 QLabel.__init__：那会在 self._full 建立之前触发下面
        # 被重写的 setText。
        super().__init__(parent)
        self._full = text or ""
        self._shown: str | None = None
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._apply()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self._full = text or ""
        self._apply()

    def fullText(self) -> str:  # noqa: N802 - 与 setText 对称
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        # 宽度还没定下来时先原样放上去：布局随后就会 resize。拿 1px 去算会把
        # 文字整个变成「…」，看上去像内容为空。
        if self.width() <= 0:
            wanted = self._full
        else:
            metrics = QFontMetrics(self.font())
            wanted = metrics.elidedText(
                self._full, Qt.TextElideMode.ElideRight, self.width()
            )
        # setText 会触发布局，布局又回调 resizeEvent —— 内容没变就不要重设，
        # 否则这一对调用会互相触发。
        if wanted != self._shown:
            self._shown = wanted
            super().setText(wanted)


class EntryCard(QFrame):
    """左侧生词本里的一张词条卡片。

    为什么用卡片而不是纯文本行：一行里既要放词、又要放释义摘要，纯文本行只能靠整行
    底色表达选中，词长不齐时视线也对不齐。卡片把「词 / 释义」分成两级，并把选中态
    画在**自己的边框**上——列表项被卡片整个盖住，`QListWidget::item:selected` 的
    背景色在卡片底下根本看不见。

    删除入口放在每张卡自己的右上角，于是删除不必先选中它、再去右侧详情区找按钮。
    """

    deleteRequested = Signal(int)

    # 高度固定：列表开了 setUniformItemSizes，行高一致才有性能收益，
    # 也免得长释义把某一行撑高、整列看上去参差不齐。
    HEIGHT = 52

    def __init__(self, entry: Entry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setProperty("selected", False)
        self.setFixedHeight(self.HEIGHT)
        # 供测试与无障碍读取：列表项自身的文本被卡片盖住了，这里是可见内容的真值
        self.entry_id = int(entry.id or 0)
        self.lemma = entry.lemma

        row = QHBoxLayout(self)
        row.setContentsMargins(11, 7, 7, 7)
        row.setSpacing(8)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)

        title = _ElidedLabel(entry.lemma)
        title.setObjectName("cardTitle")
        title.setToolTip(entry.lemma)
        head.addWidget(title, 1)

        if entry.pos:
            pos = _ElidedLabel(entry.pos)
            pos.setObjectName("cardPos")
            pos.setMaximumWidth(88)
            pos.setToolTip(entry.pos)
            head.addWidget(pos, 0)

        text.addLayout(head)

        gloss = (entry.definition_zh or entry.definition_en or "").replace("\n", " ")
        summary = _ElidedLabel(gloss)
        summary.setObjectName("cardGloss")
        # 完整释义放进 tooltip：卡片只够显示一行，被省略的部分不该就此消失
        summary.setToolTip(entry.definition_zh or entry.definition_en or "")
        text.addWidget(summary)

        row.addLayout(text, 1)

        remove = QPushButton("×")
        remove.setObjectName("cardDelete")
        remove.setFixedSize(20, 20)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setToolTip(f"从生词本中删除 {entry.lemma}")
        remove.clicked.connect(lambda _=False: self.deleteRequested.emit(self.entry_id))
        self.delete_button = remove
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)

    def set_selected(self, flag: bool) -> None:
        """切换选中态。选中与否由列表的 currentItemChanged 决定，卡片自己不判。"""
        if bool(self.property("selected")) == bool(flag):
            return
        self.setProperty("selected", bool(flag))
        # 动态属性变了必须重新 polish，否则 QSS 匹配不到 [selected="true"]，
        # 表现是「点了没反应」：背景与边框都不动。
        self.style().unpolish(self)
        self.style().polish(self)
