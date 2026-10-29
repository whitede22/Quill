"""变异测试：故意破坏关键保护，确认自检真的会判失败。

## 为什么需要它

「加了一条保护」和「这条保护真的会失败」是两件事。测试里最危险的形态不是
写错断言，而是断言恒为真：它永远绿，于是把「保护已经失效」这件事盖住了。
本脚本的做法是把保护逐个破坏掉，看自检有没有报警——报警了才说明这条断言
真的有判别力。

## 用法

    .venv/Scripts/python.exe scripts/mutation_check.py
    .venv/Scripts/python.exe scripts/mutation_check.py --only 分裂条
    .venv/Scripts/python.exe scripts/mutation_check.py --only 分裂条 --full

`--only <子串>` 只跑名字含该子串的那几条（可给多个），`--full` 连同套件的**完整
输出**一起打印。这两件事是给「排查单独一条」准备的，不是图方便：

整轮要跑上百条、每条一套完整自检，动辄以小时计（实测一次 7 小时）。某一条报
`MISSED` 时，真正需要的信息往往是一行 `INFO`（判据自己打出来的实测值），而它出现
在套件输出的**开头**——默认只打尾部十几行，恰好把要看的行留在了视野外。为了一条
结论去跑整轮，代价与收益完全不成比例。

进程退出码 0 表示全部变异都被抓住；非 0 表示存在「改了也不会被发现的保护」，
或者有变异的测试**超时**（超时是「不知道」，不能算抓住）。

## 开跑前的两道预检

1. **每条变异的「期望断言名」还找不找得到**（`check_probe_names`）。变异是靠一个
   名字去认「自己要看的失败行」的；断言改了名而 probe 没跟着改，这条变异就再也
   看不见它——报表上是 `MISSED`，读起来像「保护形同虚设」，**而保护好好的**。
   整轮以小时计，跑完再发现等于白等一轮。Round 30 实测：这份清单里同时躺着
   三条这样的陈旧 probe。
2. **残留**（见下「安全性」第 1 条）：拿一份被上次运行破坏过的源码当基线，
   得到的每一个结论都不可信。

## 安全性

变异是**原地写回**的，靠 try/finally 还原；同时在开始时记录全部涉及文件的
字节指纹，结束时逐一核对。任何一处没还原回来都会报错，避免留下一个半坏的仓库。

**但 `finally` 挡不住被强杀。** 实测踩过：脚本跑到一半被终止（换前台任务、
超时、Ctrl-C），那个变异的 `write_bytes(mutated)` 已经落盘、`finally` 的还原
没来得及执行——于是**一份被蓄意破坏的源码留在了仓库里**。危害比「测试没跑完」
大得多：接下来任何一次自检看到的都是坏代码，而失败信息指向的是被破坏的那处
业务逻辑，看起来像「我刚写的功能有 bug」。更糟的是这种残留**不会自己暴露**——
它长得就像一次真实的代码错误。

因此本脚本启动时先做两件事：

1. **残留检测**：把每条变异的「变异后」形态当成特征去扫源文件。命中即说明
   上一次运行留下了残骸（正常的源文件绝不该包含自己的变异形态），直接报错
   并指出文件与还原办法，**不继续跑**——继续跑会拿污染过的代码当基线。
2. **中断即还原**：注册 `SIGINT` / `SIGTERM` 处理，收到信号先把当前变异的
   原文写回，再退出。这只能覆盖「温和中断」，`SIGKILL` 覆盖不了，所以第 1 条
   才是真正的保险。

写回一律走**字节**通道（`read_bytes` / `write_bytes`），不用 `read_text` /
`write_text`：后者在 Windows 上会把 `\n` 翻译成 `\r\n`，于是「还原」其实是在
悄悄改行尾——文件内容看起来没变，字节指纹却变了，而混进仓库的行尾风格会被
diff 无限放大。这一条是实测踩出来的。
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 单套件的超时上限（秒）。**超时要变成一次可见的失败。**
#
# 这里原先没有 `timeout=`。子进程一旦不退出（最典型的是 `offscreen` 下弹出**模态**
# 欢迎页，没有人能点它），`subprocess.run` 就永远等下去——整条回归链静默卡住，
# 报表上什么都不写，看起来像「还在跑」。同一个坑在 `ablation_check` 那边实际吃过
# 一次（卡了 2 小时 34 分，日志停在基线那几行，只能外部强杀）。
#
# 超时**不能算「抓住」**：一个没跑完的套件什么都没证明。它也不能算「没抓住」
# ——那会把「工具自己卡住了」说成「这条保护形同虚设」。所以单列一类，报 `[TIMEOUT]`
# 并让整轮以非零码结束，把「不知道」如实写在结论里。
SUITE_TIMEOUT = 900

# 当前正在被变异的文件与其原文，供信号处理器还原。见模块 docstring「安全性」。
_IN_FLIGHT: dict[str, bytes] = {}


def _restore_in_flight(*_args) -> None:
    """收到中断信号时，把正在变异的文件还原回去再退出。"""
    for fpath, raw in list(_IN_FLIGHT.items()):
        try:
            (ROOT / fpath).write_bytes(raw)
            print(f"\n已还原 {fpath}（收到中断信号）", flush=True)
        except OSError as exc:  # 还原都失败了，至少把话说清楚
            print(f"\n还原 {fpath} 失败：{exc}", flush=True)
    _IN_FLIGHT.clear()
    raise SystemExit(130)


def check_no_leftovers() -> list[str]:
    """扫描源文件里是否残留了上一次运行的变异形态。

    正常的源文件**绝不该包含自己变异后的样子**——那条「变异后」文本只在
    变异生效期间存在。命中即说明上次运行被强杀、`finally` 没跑到。此时必须
    停下来，否则本次运行会拿污染过的代码当基线，得出的每一个结论都不可信。

    判据是「**原文锚点不见了**」——不论这次变异是「替换」还是「删掉一段」，
    只有变异生效期间原文才会消失。干净的源码里每个锚点必然至少出现一次
    （应用变异时会先校验它恰好命中 1 次），所以锚点缺席就说明源码被改过。

    这里踩过一个更隐蔽的坑：早先的写法先按 `new` 是否为空把「纯删除型」
    变异整条跳过（理由是「空串匹配一切，当不了特征」）。但纯删除型的 `new`
    恰恰**就是**空串——于是 7 条删除型变异**全部**落在检测器视野之外。
    它在一次被强杀的运行之后**确实漏掉了一条真实残留**（`_COERCERS` 里的
    `offline_fallback` 登记被删），自检才把它兜住。教训：判据里多加的
    「安全条件」如果恰好覆盖了整整一类对象，那不是更安全，而是**另一只瞎眼**。
    所以现在只看锚点在不在，`new` 不参与判定。
    """
    hits: list[str] = []
    for label, fpath, old, _new, _test, _probe in MUTATIONS:
        if not old.strip():
            # 空锚点匹配一切，当不了特征
            continue
        path = ROOT / fpath
        if not path.exists():
            continue
        try:
            src = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if src.count(old) == 0:
            hits.append(f"{fpath} 残留了变异「{label}」")
    return hits


# (说明, 文件, 原文, 变异后, 跑的测试, 期望被抓住的检查项名)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    (
        "config.py 把 offline_fallback 移出 _COERCERS（保存即被丢弃）",
        "app/config.py",
        '    "offline_fallback": _coerce_bool,\n',
        "",
        "scripts/selfcheck.py",
        "关闭兜底能落盘",
    ),
    (
        "config.py 布尔校验放宽成「字符串也算」",
        "app/config.py",
        "    return value if isinstance(value, bool) else None",
        "    return value if isinstance(value, (bool, str)) else None",
        "scripts/selfcheck.py",
        "非法兜底值退回默认",
    ),
    (
        "openai_compat.py 不再挂兜底",
        "app/providers/openai_compat.py",
        '    if config.get("offline_fallback", True):\n'
        "        from .offline import OfflineDictProvider, resolve_offline_dict\n",
        "    if False:\n",
        "scripts/selfcheck.py",
        "开启兜底时接口 Provider 被包装",
    ),
    (
        "openai_compat.py 状态栏不提兜底",
        "app/providers/openai_compat.py",
        '    if config.get("offline_fallback", True):\n'
        "        from .offline import resolve_offline_dict\n",
        "    if False:\n",
        "scripts/selfcheck.py",
        "状态栏说明已挂兜底",
    ),
    (
        "openai_compat.py 状态栏无视开关、恒定提兜底",
        "app/providers/openai_compat.py",
        '    if config.get("offline_fallback", True):\n'
        "        from .offline import resolve_offline_dict\n"
        "\n"
        "        if resolve_offline_dict() is not None:\n"
        '            return f"{base} · 失败时回落离线词库"\n',
        "    from .offline import resolve_offline_dict\n"
        "\n"
        "    if resolve_offline_dict() is not None:\n"
        '        return f"{base} · 失败时回落离线词库"\n',
        "scripts/selfcheck.py",
        "关闭兜底后状态栏不提回落",
    ),
    (
        "dictionary.py 无条件覆盖来源（把兜底内容说成接口给的）",
        "app/core/dictionary.py",
        "entry.source = entry.source or self.provider.name",
        "entry.source = self.provider.name",
        "scripts/selfcheck.py",
        "服务层保留兜底来源",
    ),
    (
        "fallback.py 兜底也没命中时抛兜底错误（掩盖真实故障）",
        "app/providers/fallback.py",
        "                raise exc from None",
        "                raise",
        "scripts/selfcheck.py",
        "兜底也未命中时抛原始接口错误",
    ),
    (
        "fallback.py 接口成功时也改写来源",
        "app/providers/fallback.py",
        "        try:\n            return self.primary.lookup(word)",
        "        try:\n            _hit = self.primary.lookup(word)\n"
        "            _hit.source = self.fallback.name\n            return _hit",
        "scripts/selfcheck.py",
        "接口成功时不改写来源",
    ),
    (
        "widgets.py 删掉兜底提示",
        "app/ui/widgets.py",
        "        if not reason:\n",
        "        if reason:\n",
        "scripts/gui_smoke.py",
        "兜底来源提示",
    ),
    (
        "widgets.py 缺项出路不区分是否联网",
        "app/ui/widgets.py",
        "        if self._networked:",
        "        if entry.source != \"offline\":",
        "scripts/gui_smoke.py",
        "兜底后缺项改指重查",
    ),
    (
        "widgets.py 缺项提示不再按来源能力过滤",
        "app/ui/widgets.py",
        "entry.missing_parts(provided_fields(entry.source))",
        "entry.missing_parts()",
        "scripts/gui_smoke.py",
        "离线来源缺项提示",
    ),
    (
        "widgets.py 缺项提示对所有来源一律照喊",
        "app/providers/base.py",
        '    "offline": frozenset({"definition_en", "definition_zh", "example"}),',
        '    "offline": frozenset({"definition_en", "definition_zh", "synonyms", "example"}),',
        "scripts/selfcheck.py",
        "离线词条不把同义语算作缺项",
    ),
    (
        "selfcheck.py 行尾检查的后缀表写漏 .py（扫描范围形同虚设）",
        "scripts/selfcheck.py",
        '        ".py",\n',
        '        ".pyx",\n',
        "scripts/selfcheck.py",
        "行尾检查确实扫到了文件",
    ),
    (
        "openai_compat.py 把兜底下沉进接口 Provider 自己",
        "app/providers/openai_compat.py",
        '        raise ProviderError(str(last_error) if last_error else "查词失败")\n',
        "        try:\n"
        "            from .offline import OfflineDictProvider\n"
        "\n"
        '            return OfflineDictProvider().lookup(word)\n'
        "        except Exception:  # noqa: BLE001\n"
        "            pass\n"
        '        raise ProviderError(str(last_error) if last_error else "查词失败")\n',
        "scripts/selfcheck.py",
        "接口 Provider 自身不含兜底",
    ),
    (
        "repository.py upsert 改为从行重建对象（运行时字段被丢）",
        "app/core/repository.py",
        "        self.conn.commit()\n"
        "        return entry\n",
        "        self.conn.commit()\n"
        "        return self.get_by_id(entry.id) or entry\n",
        "scripts/selfcheck.py",
        "服务层保留失败原因",
    ),
    (
        "main_window.py 取消「已显示则不重渲染」的守卫（兜底提示被覆盖）",
        "app/ui/main_window.py",
        "        if entry_id == self._displayed_id:\n            return\n",
        "",
        "scripts/gui_smoke.py",
        "兜底端到端界面提示",
    ),
    (
        "main_window.py 在重载列表之后才声明「已显示」（顺序反了）",
        "app/ui/main_window.py",
        "        self._displayed_id = entry.id\n"
        "        self._show_entry(entry)\n"
        "        self._reload_list(select_id=entry.id)\n",
        "        self._show_entry(entry)\n"
        "        self._reload_list(select_id=entry.id)\n"
        "        self._displayed_id = entry.id\n",
        "scripts/gui_smoke.py",
        "兜底端到端界面提示",
    ),
    (
        "settings.py 兜底开关不再随接入模式置灰",
        "app/ui/settings.py",
        '        networked = self.mode.currentData() == "openai-compat"',
        "        networked = True",
        "scripts/gui_smoke.py",
        "非接口模式兜底开关置灰",
    ),
    (
        "settings.py 词库缺失时兜底开关仍可勾选（谎报有兜底）",
        "app/ui/settings.py",
        "        if self._offline_missing:\n"
        "            self.fallback.setEnabled(False)\n",
        "        if False:\n",
        "scripts/gui_smoke.py",
        "词库缺失时兜底开关置灰",
    ),
    (
        "main_window.py 状态栏把兜底内容说成接口给的",
        "app/ui/main_window.py",
        '        reason = (getattr(entry, "fallback_reason", "") or "").strip()\n'
        '        if reason:\n',
        '        reason = (getattr(entry, "fallback_reason", "") or "").strip()\n'
        '        if False:\n',
        "scripts/gui_smoke.py",
        "兜底端到端状态栏",
    ),
    (
        "main_window.py 状态栏缺项不按来源能力过滤",
        "app/ui/main_window.py",
        "        missing = entry.missing_parts(provided_fields(entry.source))",
        "        missing = entry.missing_parts()",
        "scripts/gui_smoke.py",
        "兜底端到端状态栏",
    ),
    (
        "main_window.py 状态栏来源写死成「接口」",
        "app/ui/main_window.py",
        '        return f"已从{provider_label(entry.source) or \'接口\'}获取并保存"',
        '        return "已从接口获取并保存"',
        "scripts/gui_smoke.py",
        "查询后状态栏",
    ),
    (
        "build_offline_dict.py 的 clean_text 不再解码源语料的字面量 \\n",
        "scripts/build_offline_dict.py",
        '    raw = raw.replace("\\\\n", "\\n").replace("\\\\r", "\\n")\n',
        "",
        "scripts/build_offline_dict.py"
        " --source _ecdict_src/unpacked/ecdict.sqlite"
        " --examples _ecdict_src/unpacked/examples.sqlite"
        " --out _ecdict_src/_mut_probe.db --force",
        "构建失败",
    ),
    (
        "offline.py 不再校验词库版本（版本号写进 meta 却没人读）",
        "app/providers/offline.py",
        "    if found != OFFLINE_SCHEMA_VERSION:\n",
        "    if False:\n",
        "scripts/selfcheck.py",
        "旧版离线词库被拒绝而非静默读取",
    ),
    (
        "offline.py 状态文案不区分「版本过旧」（笼统报不可读）",
        "app/providers/offline.py",
        "    if version != OFFLINE_SCHEMA_VERSION:\n"
        "        return f\"离线词库版本过旧（v{version or '未知'}），需重新构建\"\n",
        "",
        "scripts/selfcheck.py",
        "旧版离线词库状态点明版本问题",
    ),
    (
        "offline.py 不再优先用户自建词库（文档写明的用户路径失效）",
        "app/providers/offline.py",
        "    if user_copy.exists():\n",
        "    if False:\n",
        "scripts/selfcheck.py",
        "离线查词真的读取用户自建库",
    ),
    (
        "build_offline_dict.py 不清空非中文的例句中文侧（界面把英文当翻译）",
        "scripts/build_offline_dict.py",
        "            if chinese and not has_cjk(chinese):\n",
        "            if False:\n",
        "scripts/build_offline_dict.py"
        " --source _ecdict_src/unpacked/ecdict.sqlite"
        " --examples _ecdict_src/unpacked/examples.sqlite"
        " --out _ecdict_src/_mut_probe.db --force",
        "中文侧",
    ),
    (
        "汉字范围端点被挪到 CJK 扩展 A（契约外的字被当成中文）",
        "app/core/db.py",
        'CJK_START = "\\u4e00"\n',
        'CJK_START = "\\u3400"\n',
        "scripts/selfcheck.py",
        "汉字范围端点即常用汉字区",
    ),
    (
        "校验用的 GLOB 范围与 Python 侧分家（上界写成 龥 U+9FA5）",
        "app/core/db.py",
        'CJK_END = "\\u9fff"\n',
        'CJK_END = "\\u9fa5"\n',
        "scripts/selfcheck.py",
        "校验用的 GLOB 与 has_cjk 结论一致",
    ),
    (
        "repository 不再派生、改为自己写一份等价汉字字面量（值相同但定义分裂）",
        "app/core/repository.py",
        # 锚点跟着 `repository.py` 的导入行走：加 `split_tags` 时这里从
        # `from .models import Entry` 变成带 `, split_tags` 的形式，本轮加词组仓储
        # 又变成带 `, Phrase` 的形式。两次旧锚点都命中 0 次——变异会被判成 SKIP
        # 并计成「未抓住」，看起来像保护失效，其实只是**锚点过期**。锚点静态校验
        # （唯一命中 + 变异后仍可解析）就是为这一类「工具自己的误报」准备的：
        # 误报比漏报更难查，因为它看着像证据。
        "from .models import Entry, Phrase, split_tags\n",
        "from .models import Entry, Phrase, split_tags\n\nimport re as _re\n"
        '_CJK_RE = _re.compile(r"[\\u4e00-\\u9fff]+")\n',
        "scripts/selfcheck.py",
        "汉字范围四处消费方共享同一份定义",
    ),
    (
        "db 内部的 `+` 变体改回手写字面量（值恰好相同，绕过「一处定义」）",
        "app/core/db.py",
        '_CJK_RE = re.compile(CJK_CLASS + "+")\n',
        '_CJK_RE = re.compile(r"[\\u4e00-\\u9fff]+")\n',
        "scripts/selfcheck.py",
        "汉字正则由 CJK_CLASS 派生",
    ),
    (
        "build_offline_dict.py 不再 import 范围、自己写一份 GLOB 类",
        "scripts/build_offline_dict.py",
        "CJK_GLOB_CLASS = CJK_CLASS\n",
        'CJK_GLOB_CLASS = f"[\\u4e00-\\u9fff]"\n',
        "scripts/selfcheck.py",
        "汉字范围四处消费方共享同一份定义",
    ),
    (
        "lemmatizer 里新写一份汉字字面量（第五份副本，绕开所有同源断言）",
        "app/core/lemmatizer.py",
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\n"
        "import re as _re\n"
        '_ZH = _re.compile(r"[\\u4e00-\\u9fff]")\n',
        "scripts/selfcheck.py",
        "汉字字面量只存在于 db.py 的定义处",
    ),
    (
        "汉字字面量扫描范围放宽到全仓 rglob（会把 .venv 一起扫进来）",
        "scripts/selfcheck.py",
        '                list((ROOT / "app").rglob("*.py"))\n',
        '                list(ROOT.rglob("*.py"))\n',
        "scripts/selfcheck.py",
        "汉字字面量扫描范围恰为本仓源码",
    ),
    (
        "main_window.py 卡片不再跟随选中（高亮永远不亮）",
        "app/ui/main_window.py",
        "        self._apply_card_selection(current)\n",
        "        self._apply_card_selection(None)\n",
        "scripts/gui_smoke.py",
        "卡片选中态跟随",
    ),
    (
        "main_window.py 删除不再看确认框（点「否」照样删）",
        "app/ui/main_window.py",
        # 这一段的写法在 `_delete_phrase`（本轮新增）里被复制了一份，光靠
        # `QMessageBox.StandardButton.No,` 起头的四行会命中 2 次、变异判 SKIP。
        # 往上多带两行（提问的标题与正文），锚点才重新唯一——**锚点唯一性是
        # 按整段代码判的，新增一段长得像的代码就等于把旧锚点拆了**。
        "            \"删除词条\",\n"
        "            f\"确定要从生词本中删除「{entry.lemma}」吗？\",\n"
        "            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,\n"
        "            QMessageBox.StandardButton.No,\n"
        "        )\n"
        "        if answer != QMessageBox.StandardButton.Yes:\n"
        "            return\n",
        "            \"删除词条\",\n"
        "            f\"确定要从生词本中删除「{entry.lemma}」吗？\",\n"
        "            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,\n"
        "            QMessageBox.StandardButton.No,\n"
        "        )\n"
        "        if False:\n"
        "            return\n",
        "scripts/gui_smoke.py",
        "取消删除不动数据",
    ),
    (
        "main_window.py 不再把卡片挂到列表项上（看着有卡片，其实没有）",
        "app/ui/main_window.py",
        "            self.list.setItemWidget(item, card)\n",
        "            card.setParent(None)\n",
        "scripts/gui_smoke.py",
        "卡片已挂载",
    ),
    # 注：原先这里有一条「放开贴着数字的匹配不算词」的变异，锚点是
    # `_tokens_in` 里那段 `if text[end].isdigit(): continue`。本轮整体 debug
    # 查明那段检查是**过度抑制**（把 apple1 / banana2 整份丢掉），已经删除，
    # 锚点随之消失。替换它的变异在清单末尾（恢复该检查）。
    (
        "importer.py 把句子阈值放到无穷大（整句被当成词条，收下 She）",
        "app/core/importer.py",
        "MAX_WORDS_PER_LINE = 6\n",
        "MAX_WORDS_PER_LINE = 9999\n",
        "scripts/selfcheck.py",
        # 判据名字取**前缀**：自检里两种形态（带标点 / 不带标点）共用这个名字前缀，
        # 任一条红都算抓住。只写其中一条的完整名字，另一条万一将来被删掉，
        # 变异会以「名字找不到」的形式静默变成 MISSED——那是工具的误报，
        # 比漏报更难查（看着像证据）。
        "导入：句子行整行跳过",
    ),
    (
        "importer.py 不再尝试 GBK 等编码（Windows 记事本存的文件读成乱码）",
        "app/core/importer.py",
        '_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")\n',
        '_ENCODINGS = ("utf-8",)\n',
        "scripts/selfcheck.py",
        "导入：GBK 文件可读（中文也读对）",
    ),
    (
        "importer.py 不再区分新增与已存在（一律算新增）",
        "app/core/importer.py",
        "        if entry.id in known:\n"
        "            outcome.existing.append(item.word)\n"
        "            continue\n",
        "        if False:\n"
        "            outcome.existing.append(item.word)\n"
        "            continue\n",
        "scripts/selfcheck.py",
        "导入：已存在的词计入「已存在」且不重复写库",
    ),
    (
        "importer.py 不再识别本程序导出的文件（释义行会被挖成假词条）",
        "app/core/importer.py",
        "    if _EXPORT_MARK in text:\n"
        "        return True\n"
        "    # 用户可能把文件尾的署名行裁掉，所以不能只认那一句。两条线同时出现\n"
        "    # 才算——普通词表不会恰好有 72 个 `=` 又有 72 个 `-`。\n"
        "    return bool(_HEAVY_RE.search(text) and _RULE_RE.search(text))\n",
        "    if False:\n"
        "        return True\n"
        "    return False\n",
        "scripts/selfcheck.py",
        # 名字里带格式名的部分**不能**写进 probe：那条断言早先叫
        # 「导入：纯文本导出文件里只认词条首行」，后来改成 f-string
        # `f"导入：{_imp_fmt}导出文件里只认词条首行"` 以便轮流喂每种格式。
        # 旧字面量从此在源码里不存在——`probe in out` 永远为假，这条变异只能靠
        # 「套件在导入阶段崩了」被记成 CAUGHT\*：**看着像测到了，其实那条断言
        # 根本没轮到出场**。取跨格式都成立的稳定片段。现在 `check_probe_names()`
        # 会在开跑前把这一类拦下来。
        "导出文件里只认词条首行",
    ),
    (
        "main_window.py 导入不再看确认框（点「否」照样写库）",
        "app/ui/main_window.py",
        "        if not self._confirm_import(path, parsed):\n"
        "            return\n"
        "        self._start_import(parsed)\n",
        "        self._start_import(parsed)\n",
        "scripts/gui_smoke.py",
        "导入确认框点否则不写库",
    ),
    # 本轮整体 debug 的四处修复，各配一条变异。它们都是「修好了」的东西，
    # 但修好只说明此刻对；变异测试守的是「以后别改回去」。
    (
        "repository.py 取消 upsert 的回滚（事务悬挂，半成品随下次 commit 落盘）",
        "app/core/repository.py",
        "            self._sync_fts(entry)\n"
        "        except Exception:\n"
        "            self.conn.rollback()\n"
        "            raise\n"
        "        self.conn.commit()\n"
        "        return entry\n",
        "            self._sync_fts(entry)\n"
        "        except Exception:\n"
        "            raise\n"
        "        self.conn.commit()\n"
        "        return entry\n",
        "scripts/selfcheck.py",
        "索引写入失败时抛出且事务被回滚",
    ),
    (
        "repository.py 取消 delete 的回滚（主表删了、索引没删就落盘）",
        "app/core/repository.py",
        "            self.conn.execute(\"DELETE FROM aliases WHERE entry_id = ?\", (entry_id,))\n"
        "        except Exception:\n"
        "            self.conn.rollback()\n"
        "            raise\n",
        "            self.conn.execute(\"DELETE FROM aliases WHERE entry_id = ?\", (entry_id,))\n"
        "        except Exception:\n"
        "            raise\n",
        "scripts/selfcheck.py",
        "索引写入失败时抛出且事务被回滚",
    ),
    (
        "importer.py 恢复「贴着数字就整段丢弃」（apple1 会被整份词表丢掉）",
        "app/core/importer.py",
        "    return [match.group() for match in _TOKEN_RE.finditer(text)]\n",
        "    out = []\n"
        "    for match in _TOKEN_RE.finditer(text):\n"
        "        end = match.end()\n"
        "        if end < len(text) and text[end].isdigit():\n"
        "            continue\n"
        "        out.append(match.group())\n"
        "    return out\n",
        "scripts/selfcheck.py",
        "词尾紧跟序号仍能取出单词",
    ),
    (
        "importer.py 去掉 UTF-16 的容错（docstring 承诺不抛，实际会抛）",
        "app/core/importer.py",
        '    if raw[:2] in (b"\\xff\\xfe", b"\\xfe\\xff"):\n'
        "        try:\n"
        '            return raw.decode("utf-16")\n'
        "        except UnicodeDecodeError:\n"
        "            pass\n",
        '    if raw[:2] in (b"\\xff\\xfe", b"\\xfe\\xff"):\n'
        '        return raw.decode("utf-16")\n',
        "scripts/selfcheck.py",
        "坏 UTF-16 不抛 UnicodeDecodeError",
    ),
    (
        "keystore.py 密钥改回非原子直写（失败留下半截密文）",
        "app/security/keystore.py",
        '    atomic_write_text(path, encrypt_text(api_key), encoding="ascii")\n',
        '    path.write_text(encrypt_text(api_key), encoding="ascii")\n',
        "scripts/selfcheck.py",
        "写入失败时保留原值",
    ),
    # ---- 新增导出格式（CSV / JSON / PDF）----
    (
        "CSV 去掉 BOM（Excel 在中文 Windows 上按 GBK 打开，中文全乱码）",
        "app/core/exporter.py",
        "    return CSV_BOM + buf.getvalue()\n",
        "    return buf.getvalue()\n",
        "scripts/selfcheck.py",
        "CSV 带 BOM",
    ),
    (
        "CSV 行尾改回 LF（表格工具对 LF 的容忍度不如 CRLF）",
        "app/core/exporter.py",
        'writer = csv.writer(buf, lineterminator="\\r\\n")\n',
        'writer = csv.writer(buf, lineterminator="\\n")\n',
        "scripts/selfcheck.py",
        "CSV 行尾为 CRLF",
    ),
    (
        "CSV 手拼字符串而不是用 csv 模块（字段里的逗号会把一列劈成两列）",
        "app/core/exporter.py",
        '    writer = csv.writer(buf, lineterminator="\\r\\n")\n',
        "    class _Naive:\n"
        "        def __init__(self, fp):\n"
        "            self.fp = fp\n"
        "\n"
        "        def writerow(self, row):\n"
        "            self.fp.write(','.join(row) + '\\r\\n')\n"
        "\n"
        "    writer = _Naive(buf)\n",
        "scripts/selfcheck.py",
        "CSV 含逗号的单词原样读回",
    ),
    (
        "JSON 去掉顶层版本号（第一次改结构后旧文件就变成读不了）",
        "app/core/exporter.py",
        '        "version": JSON_VERSION,\n',
        "",
        "scripts/selfcheck.py",
        "JSON 顶层带版本号",
    ),
    (
        "JSON 去掉格式标识（任何 JSON 文件都会被当成生词本读入）",
        "app/core/exporter.py",
        '        "format": JSON_FORMAT_TAG,\n',
        "",
        "scripts/selfcheck.py",
        "JSON 顶层带格式标识",
    ),
    (
        "JSON 打开 ensure_ascii（中文变成 \\uXXXX，文件不再可直读）",
        "app/core/exporter.py",
        '    return json.dumps(payload, ensure_ascii=False, indent=2)\n',
        '    return json.dumps(payload, ensure_ascii=True, indent=2)\n',
        "scripts/selfcheck.py",
        "JSON 中文不转义",
    ),
    (
        "JSON 把本机主键 id 也导出（换台机器写回会撞主键或覆盖别人）",
        "app/core/exporter.py",
        "    out: dict = {}\n"
        "    for name in JSON_ENTRY_FIELDS:\n",
        "    out: dict = {\"id\": entry.id}\n"
        "    for name in JSON_ENTRY_FIELDS:\n",
        "scripts/selfcheck.py",
        "JSON 不含本机主键与运行时字段",
    ),
    (
        "JSON 把运行时字段 prompt_stale 也导出（读回来是条陈旧且可能相反的结论）",
        "app/core/exporter.py",
        "    out: dict = {}\n"
        "    for name in JSON_ENTRY_FIELDS:\n",
        "    out: dict = {\"prompt_stale\": entry.prompt_stale}\n"
        "    for name in JSON_ENTRY_FIELDS:\n",
        "scripts/selfcheck.py",
        "JSON 不含本机主键与运行时字段",
    ),
    (
        "PDF 不再检查字体（缺字体时静默产出通篇缺字方块）",
        "app/core/exporter.py",
        "    _ensure_gui_app()\n    _ensure_fonts()\n",
        "    _ensure_gui_app()\n",
        "scripts/selfcheck.py",
        "无字体时 export_pdf 拒绝输出",
    ),
    (
        "PDF 手工 scale 取代 print_（分页失效，200 条只画出开头一小段）",
        "app/core/exporter.py",
        "    doc.print_(writer)\n",
        "    from PySide6.QtGui import QPainter as _P, QPageSize as _PS\n"
        "    _pt = _P(writer)\n"
        "    _page = PDF_PAGE.size(_PS.Point).toSize()\n"
        "    _m = PDF_MARGIN_MM / 25.4 * 72.0\n"
        "    from PySide6.QtCore import QRectF as _R, QSizeF as _S\n"
        "    doc.setTextWidth(_page.width() - 2 * _m)\n"
        "    doc.setPageSize(_S(_page.width() - 2 * _m, _page.height() - 2 * _m))\n"
        "    _pt.scale(PDF_DPI / 72.0, PDF_DPI / 72.0)\n"
        "    doc.drawContents(_pt, _R(0, 0, _page.width() - 2 * _m, _page.height() - 2 * _m))\n"
        "    _pt.end()\n",
        "scripts/selfcheck.py",
        "PDF 条目多时正确分页",
    ),
    (
        "PDF 词条间的分隔线被删（版式退化成密排，条目多时看串行）",
        "app/core/exporter.py",
        "_PDF_RULE_COLOR = \"#C9C9C9\"\n",
        "_PDF_RULE_COLOR = \"#FFFFFF\"\n",
        "scripts/selfcheck.py",
        "PDF 分隔线不是白色",
    ),
    (
        "PDF 备注的换行被折叠成空格（记忆法的分条糊成一段）",
        "app/core/exporter.py",
        # 这行代码在 Anki 背面里也有一份长得很像的（`_anki_back` 同样把备注的
        # 换行转成 `<br>`），单看那一行会命中 2 次。把 PDF 侧独有的注释一起圈进
        # 锚点，才指得准是哪一处——「锚点唯一」不等于「锚点指的是我想动的那处」。
        "        # 备注是用户手写的，**保留换行**（与文本导出同一决定）；用 <br> 而非\n"
        "        # 折叠成空格，否则记忆法的分条会糊成一段。\n"
        '        note_html = "<br>".join(_html_escape(line) for line in note)\n',
        '        note_html = _html_escape(" ".join(note))\n',
        "scripts/selfcheck.py",
        "PDF 备注的每一行各自成行",
    ),
    (
        "importer.py XML 指纹放宽成「任何 XML 都算生词本」",
        "app/core/importer.py",
        "    return _xml_local_name(root.tag) in _XML_ROOT_NAMES\n",
        "    return True\n",
        "scripts/selfcheck.py",
        "根元素不是 wordbook 的 XML 不被当成生词本",
    ),
    (
        "importer.py 分派里摘掉 XML 路径（有道文件退回逐行取词）",
        "app/core/importer.py",
        "    if _looks_like_xml_export(text):\n"
        "        return _parse_xml_export(text)\n",
        "",
        "scripts/selfcheck.py",
        "认出有道单词本 XML",
    ),
    (
        "importer.py 有道释义一律当中文（纯英文释义被塞进中文字段）",
        "app/core/importer.py",
        '    if _CJK_RE.search(trans):\n'
        '        return {"definition_zh": trans}\n'
        '    return {"definition_en": trans}\n',
        '    return {"definition_zh": trans}\n',
        "scripts/selfcheck.py",
        "纯英文的 XML 释义归到英文释义字段",
    ),
    (
        "importer.py 回填不看白名单（把 source 这类程序元数据也写进库）",
        "app/core/importer.py",
        "        if name in _FILLABLE_FIELDS and value:\n",
        "        if value:\n",
        "scripts/selfcheck.py",
        "文件字段不能回填 source 这类程序元数据",
    ),
    (
        "importer.py 只回填备注、不回填释义（有道释义被丢掉）",
        "app/core/importer.py",
        "    if not (item.notes or item.fill):\n",
        "    if not item.notes:\n",
        "scripts/selfcheck.py",
        "XML 释义真的写进了库",
    ),
    (
        "main_window.py 识别依据的键写错（本程序导出被说成普通词表）",
        "app/ui/main_window.py",
        '    "Quill 文本导出": "按导出格式读的，只取「N. 单词」那一行，并还原备注",\n',
        '    "Quill 导出（纯文本）": "按导出格式读的，只取「N. 单词」那一行，并还原备注",\n',
        "scripts/selfcheck.py",
        "每种带结构的格式都能被「识别依据」表覆盖",
    ),
    # ---- Round 25：界面美化与自定义背景图 ----
    (
        "background.py 缩放改成 contain（长宽比不匹配时留白）",
        "app/ui/background.py",
        "    ratio = max(size.width() / sw, size.height() / sh)",
        "    ratio = min(size.width() / sw, size.height() / sh)",
        "scripts/selfcheck.py",
        "背景图：铺满整幅而不留白",
    ),
    (
        "background.py 模糊半径被置零（开关勾了等于没勾）",
        "app/ui/background.py",
        "    return max(8, min(48, round(size.height() / 70)))\n",
        "    return 0  # 变异：半径置零，apply_blur 会原样返回\n",
        "scripts/selfcheck.py",
        "背景图：模糊开关真的改变画面",
    ),
    (
        "background.py 坏图不再被挡住（拿一张解不开的图当背景）",
        "app/ui/background.py",
        "        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:\n"
        "            return False\n",
        "",
        "scripts/selfcheck.py",
        "背景图：文件损坏同样是「没有背景」，不抛异常",
    ),
    (
        "background.py 每次取图都重新模糊（拖动窗口会卡）",
        "app/ui/background.py",
        "        if blur and self._blurred is None:\n",
        "        if blur:\n",
        "scripts/selfcheck.py",
        "背景图：模糊只算一次，换尺寸只做缩放",
    ),
    (
        "main_window.py 有背景时不重绘（配置对了但屏幕上没图）",
        "app/ui/main_window.py",
        "        if not self._background.has_image:\n            return\n",
        "        if True:\n            return\n",
        "scripts/gui_smoke.py",
        "背景图颜色",
    ),
    (
        "main_window.py 无视模糊开关（永远按模糊画）",
        "app/ui/main_window.py",
        '        self._bg_blur = bool(self.config.get("background_blur", True))\n',
        "        self._bg_blur = True\n",
        "scripts/gui_smoke.py",
        "背景图模糊对比",
    ),
    (
        "main_window.py 换背景后不给子控件重算样式（面板一直是不透明的）",
        "app/ui/main_window.py",
        "        for widget in [self, self.statusBar(), *self.findChildren(QWidget)]:\n",
        "        for widget in [self]:\n",
        "scripts/gui_smoke.py",
        "有背景时面板半透明",
    ),
    (
        "theme.py 有背景时窗口底色不让位（图片被 QSS 铺的底色盖住）",
        "app/ui/theme.py",
        'QMainWindow[hasBackground="true"] { background: transparent; }',
        'QMainWindow[hasBackground="true"] { background: $bg; }',
        "scripts/selfcheck.py",
        "有背景时窗口底色让位给图片",
    ),
    (
        "theme.py 卡片也跟着半透明（列表项文字透成鬼影）",
        "app/ui/theme.py",
        # 锚点带上末尾那一条（视图切换按钮）与收尾的括号：本轮往 targets 末尾
        # 插了 `QPushButton#mode`，`countLabel` 后面紧跟 `)` 的写法就不存在了。
        '        ("QPushButton#mode", _rgb(pal["surface"])),\n    )\n',
        '        ("QPushButton#mode", _rgb(pal["surface"])),\n'
        '        ("QFrame#card", _rgb(pal["surface"])),\n'
        "    )\n",
        "scripts/selfcheck.py",
        "卡片在背景图下保持不透明",
    ),
    (
        "theme.py 规则里的不透明度另抄一份（改了档位表、规则不跟着变）",
        "app/ui/theme.py",
        'f"{{ background: rgba({r}, {g}, {b}, {percent}%); }}"',
        'f"{{ background: rgba({r}, {g}, {b}, 88%); }}"',
        "scripts/selfcheck.py",
        "规则里的不透明度由档位表生成",
    ),
    (
        "theme.py 档位顺序写反（越「明显」反而越不透）",
        "app/ui/theme.py",
        '_VEIL_LIGHT = {"soft": 88, "medium": 72, "bold": 58}\n',
        '_VEIL_LIGHT = {"soft": 58, "medium": 72, "bold": 88}\n',
        "scripts/selfcheck.py",
        "档位名与不透明度同向",
    ),
    (
        "theme.py 最「明显」那档透过头（正文对比度跌破 4.5:1）",
        "app/ui/theme.py",
        '_VEIL_LIGHT = {"soft": 88, "medium": 72, "bold": 58}\n',
        '_VEIL_LIGHT = {"soft": 88, "medium": 72, "bold": 30}\n',
        "scripts/selfcheck.py",
        "最「明显」那一档仍守住正文对比度",
    ),
    (
        "theme_meta.py 不认识的档位不归一（配置被写坏就画不出样式）",
        "app/theme_meta.py",
        "    return text if text in VEIL_NAMES else BG_VEIL_DEFAULT\n",
        "    return text\n",
        "scripts/selfcheck.py",
        "不认识的档位一律退回默认档",
    ),
    (
        "main_window.py 档位没接到样式上（三档长得一模一样）",
        "app/ui/main_window.py",
        '        self.setProperty("bgVeil", self._bg_veil)\n',
        '        self.setProperty("bgVeil", "soft")\n',
        "scripts/gui_smoke.py",
        "各档位的面板不透明度",
    ),
    (
        "theme.py 提示条的文字色没跟着底色改（可读性跌破门槛）",
        "app/ui/theme.py",
        '    "warn_text": "#8A6224",\n',
        '    "warn_text": "#9A6B2F",\n',
        "scripts/selfcheck.py",
        "提示文字在提示块底色上仍达标",
    ),
    (
        "theme.py 样式表忽略主题参数（六套主题画出来是同一套）",
        "app/ui/theme.py",
        "    pal = palette(theme_id)\n",
        "    pal = palette(THEME_DEFAULT)\n",
        "scripts/selfcheck.py",
        "每套主题生成不同的样式表",
    ),
    (
        "theme.py 深色主题沿用浅色的档位表（正文压在亮背景上读不清）",
        "app/ui/theme.py",
        '_VEIL_DARK = {"soft": 96, "medium": 84, "bold": 72}\n',
        '_VEIL_DARK = {"soft": 88, "medium": 72, "bold": 58}\n',
        "scripts/selfcheck.py",
        "每套主题的每个档位都守住正文对比度",
    ),
    (
        "theme.py 最透档不再换加强色（小字与背景融在一起）",
        "app/ui/theme.py",
        '    deepest = min(pal["veil"], key=lambda name: pal["veil"][name])\n',
        '    deepest = "nonexistent"  # 变异：规则匹配不到任何档位\n',
        "scripts/selfcheck.py",
        "加强色规则只挂在最透那一档上",
    ),
    (
        "main_window.py 切主题不换样式表（界面纹丝不动）",
        "app/ui/main_window.py",
        "            app.setStyleSheet(stylesheet(resolved))\n",
        '            app.setStyleSheet(stylesheet("ink"))  # 变异：不换样式表\n',
        "scripts/gui_smoke.py",
        "各主题强调色",
    ),
    (
        "settings.py 取消设置不还原主题（看到的与存的不一致）",
        "app/ui/settings.py",
        "        if self._on_theme_preview is not None:\n"
        "            self._on_theme_preview(self._theme_original)\n"
        "        super().reject()\n",
        "        super().reject()  # 变异：不还原主题\n",
        "scripts/gui_smoke.py",
        "取消设置会还原主题",
    ),
    (
        "main_window.py 拖入图片不问就改配置",
        "app/ui/main_window.py",
        "            QMessageBox.StandardButton.Yes,\n"
        "        )\n"
        "        if answer != QMessageBox.StandardButton.Yes:\n"
        "            return\n",
        "            QMessageBox.StandardButton.Yes,\n"
        "        )\n"
        "        if answer == QMessageBox.StandardButton.Cancel:  # 变异：不按回答拦下\n"
        "            return\n",
        "scripts/gui_smoke.py",
        "拒绝后不改配置",
    ),
    (
        "main_window.py 最近使用不去重（同一张图堆满整个下拉框）",
        "app/ui/main_window.py",
        "        recent = [p for p in (self.config.get(\"background_recent\") or []) if p != text]\n",
        "        recent = list(self.config.get(\"background_recent\") or [])  # 变异：不去重\n",
        "scripts/gui_smoke.py",
        # 判据是**数个数**的那条，不是上面那条 `in`：成员判定分不出「一个」和
        # 「两个」，去掉去重它照样为真。首次跑这条变异时报的就是 MISSED，
        # 而错的是判据而不是实现——于是给 gui_smoke 补了这条观测。
        "同一张图重复使用不占两个位置",
    ),
    (
        "config.py 最近背景图不去重（配置里同一张图重复堆积）",
        "app/config.py",
        "        if text and text not in out:\n",
        "        if text:  # 变异：不去重\n",
        "scripts/selfcheck.py",
        "最近背景图：去空、去重、丢掉非字符串",
    ),
    (
        "config.py 主题名不做归一（配置被写坏就存下一个不存在的主题）",
        "app/config.py",
        "    return theme_id_of(value)\n",
        "    return str(value)\n",
        "scripts/selfcheck.py",
        "主题名写坏就退回默认主题",
    ),
    (
        "config.py 改从界面包取默认值（数据层反过来依赖 ui）",
        "app/config.py",
        "from .theme_meta import BG_VEIL_DEFAULT, THEME_DEFAULT, theme_id_of, veil_level\n",
        "from .ui.theme import BG_VEIL_DEFAULT, THEME_DEFAULT, theme_id_of, veil_level\n",
        "scripts/selfcheck.py",
        "配置模块不 import 界面包",
    ),
    (
        "theme.py 调色板少组一套（名义表放行的主题取不到调色板）",
        "app/ui/theme.py",
        "_THEME_ORDER = THEME_IDS\n",
        "_THEME_ORDER = THEME_IDS[:-1]\n",
        "scripts/selfcheck.py",
        "主题 id 名义表与调色板键集一致",
    ),
    (
        "theme.py 卡片 × 复用 TEXT3（改淡 × 会连占位文字一起弄糊）",
        "app/ui/theme.py",
        "    color: $icon_idle;\n    font-size: 15px;",
        "    color: $text3;\n    font-size: 15px;",
        "scripts/selfcheck.py",
        "卡片删除 × 用独立常量",
    ),
    (
        "widgets.py chip 等宽只按第一个同义词算（长的那几个对不齐）",
        "app/ui/widgets.py",
        "        widest = max(chip.sizeHint().width() for chip in chips)",
        "        widest = chips[0].sizeHint().width()",
        "scripts/gui_smoke.py",
        "同义语 chip 等宽",
    ),
    (
        "widgets.py 底部操作不再靠右收拢（弹簧回到两个按钮中间）",
        "app/ui/widgets.py",
        "        row.addStretch(1)\n\n        refresh = QPushButton(\"重新查询\")",
        "        refresh = QPushButton(\"重新查询\")",
        "scripts/gui_smoke.py",
        "详情区底部操作顺序",
    ),
    (
        "widgets.py 删除按钮不再标成破坏性（悬停不提示风险）",
        "app/ui/widgets.py",
        # 词条那一处带上它上面那句注释：`show_phrase`（本轮新增的词组详情）里
        # 也有一个 `remove.setProperty("role", "destructive")`，只写这一行会命中 2 次。
        "        # 最显眼的按钮，而它恰恰是最不该被顺手点到的那个。\n"
        '        remove.setProperty("role", "destructive")\n',
        "        # 最显眼的按钮，而它恰恰是最不该被顺手点到的那个。\n",
        "scripts/gui_smoke.py",
        "详情区操作的角色标记",
    ),
    (
        "main_window.py 工具栏次级按钮不再挂 #bare（三级层次塌回一级）",
        "app/ui/main_window.py",
        "        for button in (self.btn_format_help, self.btn_backup, self.btn_settings):\n"
        '            button.setObjectName("bare")\n',
        "        pass\n",
        "scripts/gui_smoke.py",
        "工具栏层级",
    ),
    (
        "config.py 把 background_blur 移出 _COERCERS（关掉模糊重启又自己开）",
        "app/config.py",
        '    "background_blur": _coerce_bool,\n',
        "",
        "scripts/selfcheck.py",
        "背景图设置可保存可回读",
    ),
    (
        "config.py 把 background_veil 移出 _COERCERS（选的档位存不下来）",
        "app/config.py",
        '    "background_veil": _coerce_veil,\n',
        "",
        "scripts/selfcheck.py",
        "背景图三项设置已登记且出厂有默认值",
    ),
    (
        "widgets.py 同义语那行重新写死颜色（深色主题下整行看不见）",
        "app/ui/widgets.py",
        # 缩进必须与真实代码**逐字符**一致（这里是 12 空格，在 `if syn.gloss_zh:`
        # 里面）。曾写成 8 空格，而 `text.count(old) == 1` **照样通过**——子串匹配
        # 会无视前面多出来的那 4 个空格。替换之后 `setText` 落在 8 空格、同一块里
        # 其余行在 12 空格，文件直接 `IndentationError`，于是这条变异被记成
        # 「CAUGHT*（代码崩溃）」：看着像「测到了」，其实是**变异自己把源码改坏了**，
        # 而自检那条字面颜色断言根本没轮到出场。
        '            gloss.setObjectName("synGloss")\n',
        '            gloss.setObjectName("synGloss")\n'
        "            gloss.setText(f\"<span style='color:#1F1F1E'>{syn.gloss_zh}</span>\")\n",
        "scripts/selfcheck.py",
        "界面代码里没有字面颜色",
    ),
    (
        "theme.py 同义语简释改用次要色（比差异说明还淡，层级反过来）",
        "app/ui/theme.py",
        "QLabel#synGloss { font-size: 12px; color: $text; }",
        "QLabel#synGloss { font-size: 12px; color: $text2; }",
        "scripts/gui_smoke.py",
        "同义语两行取色",
    ),
    (
        "theme.py 裸按钮不再垫底（工具栏那三个按钮压在原图上读不清）",
        "app/ui/theme.py",
        '        ("QPushButton#bare", _rgb(pal["surface"])),\n',
        "",
        "scripts/selfcheck.py",
        "面板之外直接压在原图上的文字也拿到了受控的表面",
    ),
    # ---- 标签入口 / 批量查词 / 撤销删除 / 测试连接 / Anki 导出 ----
    # 这一批的共同点是「接错了也不报错」：标签不归一只是筛选变得不准、
    # Anki 少消一个制表符只是列错位、撤销不清 id 只是把用户的新备注盖回去、
    # 测试连接多带一个参数只是别的模型直接 400。每一条都不会让任何程序报错。
    (
        "models.py 标签不归一（存储形态随用户怎么敲而变，筛选退化成子串匹配）",
        "app/core/models.py",
        '    return " ".join(split_tags(value))\n',
        '    return str(value or "").strip()\n',
        "scripts/selfcheck.py",
        "标签存储形态统一为单空格",
    ),
    (
        "repository.py 按标签筛选退回纯 LIKE（「考研英语」被当成「考研」带出来）",
        "app/core/repository.py",
        "            if clean in entry.tag_list\n",
        "            if True\n",
        "scripts/selfcheck.py",
        "按标签筛选不把「考研英语」当成「考研」",
    ),
    (
        "repository.py 标签计数退回按字段值（一条挂两个标签的词条只算一次）",
        "app/core/repository.py",
        "            counts[tag] = counts.get(tag, 0) + 1\n",
        '            counts[row["tags"]] = counts.get(row["tags"], 0) + 1\n',
        "scripts/selfcheck.py",
        "标签计数按标签而非按字段值",
    ),
    (
        "dictionary.py 只读缓存永远落空（批量查词前先判「整串是不是词组」的能力消失）",
        "app/core/dictionary.py",
        "        return self._from_cache(surface)\n",
        "        return None\n",
        "scripts/selfcheck.py",
        "cached() 命中已收录词条",
    ),
    (
        "exporter.py Anki 带上 BOM（第一张卡片正面多出一个看不见的字符）",
        "app/core/exporter.py",
        "    lines: list[str] = list(_ANKI_HEADER)\n",
        '    lines: list[str] = ["\\ufeff" + _ANKI_HEADER[0], *_ANKI_HEADER[1:]]\n',
        "scripts/selfcheck.py",
        "Anki 刻意不带 BOM",
    ),
    (
        "exporter.py Anki 不消制表符（整行列数错位，而 Anki 不会报任何错）",
        "app/core/exporter.py",
        "    return _flatten(text)\n",
        '    return text or ""\n',
        "scripts/selfcheck.py",
        "Anki 每张卡片恰好三列",
    ),
    (
        "exporter.py Anki 不转义 HTML（read < write 被当成标签吃掉半截）",
        "app/core/exporter.py",
        "        rows.append(_html_escape(_anki_cell(entry.definition_en)))\n",
        "        rows.append(_anki_cell(entry.definition_en))\n",
        "scripts/selfcheck.py",
        "Anki 背面转义 HTML 元字符",
    ),
    (
        "exporter.py Anki 不跳过正面为空的词条（导出一张没有正面的卡片）",
        "app/core/exporter.py",
        "        if not lemma:\n            continue\n",
        '        if not lemma:\n            lemma = "（空）"\n',
        "scripts/selfcheck.py",
        "Anki 跳过正面为空白的词条",
    ),
    (
        "exporter.py Anki 把标题塞进头部（同一批词条换个标题就换一份文件）",
        "app/core/exporter.py",
        "    lines: list[str] = list(_ANKI_HEADER)\n",
        '    lines: list[str] = [*_ANKI_HEADER, f"#title:{title}"]\n',
        "scripts/selfcheck.py",
        "Anki 忽略标题参数",
    ),
    (
        "main_window.py 撤销时不清空 id（把用户删除之后新写的备注盖回旧快照）",
        "app/ui/main_window.py",
        '        entry.id = None\n        entry.created_at = ""\n',
        '        entry.created_at = ""\n',
        "scripts/gui_smoke.py",
        "撤销不覆盖删除之后新写的备注",
    ),
    (
        "main_window.py 拖入多个文件时只留第一个（其余静默丢弃）",
        "app/ui/main_window.py",
        "                out.append(path)\n        return out\n",
        "                out.append(path)\n        return out[:1]\n",
        "scripts/gui_smoke.py",
        "拖入多个词表文件全部被收下",
    ),
    (
        "main_window.py 编辑标签不落库（界面说已保存、库里没变）",
        "app/ui/main_window.py",
        "        if self._pending_tags is not None:\n"
        "            entry_id, text = self._pending_tags\n"
        '            jobs.append((entry_id, "tags", text))\n',
        "        if False:\n"
        "            entry_id, text = self._pending_tags\n"
        '            jobs.append((entry_id, "tags", text))\n',
        "scripts/gui_smoke.py",
        "标签改动会落库",
    ),
    (
        "main_window.py 批量查词的报告写死「导入」（用户会去找自己什么时候导入了文件）",
        "app/ui/main_window.py",
        "        source = self._import_origin\n",
        '        source = "导入"\n',
        "scripts/gui_smoke.py",
        "批量查词的报告口径不是「导入」",
    ),
    (
        "openai_compat.py 测试连接带上 temperature（推理型模型会直接 400）",
        "app/providers/openai_compat.py",
        '            "max_tokens": 1,\n',
        '            "max_tokens": 1,\n            "temperature": 0,\n',
        "scripts/gui_smoke.py",
        "测试连接的请求体不带 temperature",
    ),
    # ------------------------------------------------------------------
    # 固定搭配与词组收藏。
    #
    # 这一批的共同点是**接错了也不报错**：搭配解析退回 json.loads 一把梭、含词
    # 查询退回子串匹配、去重交给 Python 侧自觉、提示词结构里少一个键、某一种导出
    # 把搭配整块丢掉……程序全都照常运行，只有内容悄悄变少或变错。
    # ------------------------------------------------------------------
    (
        "models.py from_payload 只认字符串形态的搭配（模型返回的 list 被静默丢光）",
        "app/core/models.py",
        '            c for c in (Collocation.from_raw(x) for x in _json_list(payload.get("collocations"))) if c\n',
        '            c for c in (Collocation.from_raw(x) for x in _json_list(payload.get("collocations") if isinstance(payload.get("collocations"), str) else [])) if c\n',
        "scripts/selfcheck.py",
        "from_payload 保住模型返回的搭配",
    ),
    (
        "models.py missing_parts 忽略 provided（每条离线词条都被喊「缺少固定搭配」）",
        "app/core/models.py",
        "        wanted = present.keys() if provided is None else present.keys() & provided\n",
        "        wanted = present.keys()\n",
        "scripts/selfcheck.py",
        "离线来源不被喊",
    ),
    (
        "models.py from_payload 的搭配不封顶（详情面板被一屏搭配撑爆）",
        "app/core/models.py",
        "            collocations=colls[:5],\n",
        "            collocations=colls,\n",
        "scripts/selfcheck.py",
        "搭配条数封顶 5",
    ),
    (
        "models.py Collocation.from_raw 丢掉键名容错（模型换个键名就整条丢）",
        "app/core/models.py",
        '                raw.get("phrase") or raw.get("collocation") or raw.get("term") or ""\n',
        '                raw.get("phrase") or ""\n',
        "scripts/selfcheck.py",
        "Collocation.from_raw 认别名词名",
    ),
    (
        "models.py Collocation.from_raw 不挡空词组（收藏里出现一条没有内容的搭配）",
        "app/core/models.py",
        "            if not phrase:\n"
        "                return None\n"
        "            return cls(\n"
        "                phrase=phrase,\n",
        "            if False:\n"
        "                return None\n"
        "            return cls(\n"
        "                phrase=phrase,\n",
        "scripts/selfcheck.py",
        "Collocation.from_raw 对空 / 无词组键的字典给 None",
    ),
    (
        "repository.py _contains_run 退回子串匹配（run 会命中 trunk / runway）",
        "app/core/repository.py",
        "    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))\n",
        "    return any(needle[0] in token for token in haystack)\n",
        "scripts/selfcheck.py",
        "含词查询不把 trunk",
    ),
    (
        "repository.py phrases_containing 不过滤（每条词条底下都冒出全部词组）",
        "app/core/repository.py",
        "            if _contains_run(_tokens_lower(item.phrase), needle)\n",
        "            if True\n",
        "scripts/selfcheck.py",
        "含词查询对不在任何词组里的词给空",
    ),
    (
        "repository.py add_phrase 返回本次传入的那条（把先前收藏的简释悄悄换掉）",
        "app/core/repository.py",
        "        stored = self.get_phrase(phrase.phrase)\n"
        "        if stored is not None:\n"
        "            return stored\n",
        "        stored = self.get_phrase(phrase.phrase)\n"
        "        if stored is not None:\n"
        "            phrase.id = stored.id\n"
        "            return phrase\n",
        "scripts/selfcheck.py",
        "重复收藏不覆盖已有简释",
    ),
    (
        "db.py phrases 表去掉 UNIQUE（同一个词组被收藏两次变成两条）",
        "app/core/db.py",
        "    phrase       TEXT NOT NULL UNIQUE COLLATE NOCASE,\n",
        "    phrase       TEXT NOT NULL COLLATE NOCASE,\n",
        "scripts/selfcheck.py",
        "重复收藏同一个词组不新增一条",
    ),
    (
        "db.py 把 collocations_json 移出 ENTRIES_ADDED_COLUMNS（老用户的库永远没有这一列）",
        "app/core/db.py",
        '    "collocations_json": "TEXT NOT NULL DEFAULT \'[]\'",\n',
        "",
        "scripts/selfcheck.py",
        "旧库经 init_db 后补上了 collocations_json 列",
    ),
    (
        "prompts.py 提示词的结构示例不再提 collocations（模型不会再给搭配）",
        "app/providers/prompts.py",
        '  "collocations": [\n',
        '  "collocation_items": [\n',
        "scripts/selfcheck.py",
        "提示词已要求输出固定搭配",
    ),
    (
        "exporter.py 纯文本导出丢掉固定搭配（打印稿上没有这一块）",
        "app/core/exporter.py",
        "    for i, coll in enumerate(entry.collocations):\n"
        '        label = "搭" if i == 0 else "  "\n',
        "    for i, coll in enumerate([]):\n"
        '        label = "搭" if i == 0 else "  "\n',
        "scripts/selfcheck.py",
        "文本导出带上搭配与它自己的例句",
    ),
    (
        "exporter.py Markdown 导出丢掉固定搭配",
        "app/core/exporter.py",
        '        if entry.collocations:\n            lines.append("- **搭**")\n',
        '        if False:\n            lines.append("- **搭**")\n',
        "scripts/selfcheck.py",
        "Markdown 导出带上搭配",
    ),
    (
        "exporter.py CSV 的「固定搭配」列留空（表头有这一列，内容一格没有）",
        "app/core/exporter.py",
        "                _collocation_cell(entry),\n",
        '                "",\n',
        "scripts/selfcheck.py",
        "CSV 的最后一列是固定搭配",
    ),
    (
        "exporter.py Anki 背面丢掉固定搭配（卡片照常导入，只是少了一块）",
        "app/core/exporter.py",
        "    for coll in entry.collocations:\n"
        "        text = _html_escape(_anki_cell(coll.phrase))\n",
        "    for coll in []:\n"
        "        text = _html_escape(_anki_cell(coll.phrase))\n",
        "scripts/selfcheck.py",
        "Anki 背面带上搭配与它的中英例句",
    ),
    (
        "exporter.py PDF 版式丢掉固定搭配",
        "app/core/exporter.py",
        "    for i, coll in enumerate(entry.collocations):\n"
        "        text = _html_escape(_flatten(coll.phrase))\n",
        "    for i, coll in enumerate([]):\n"
        "        text = _html_escape(_flatten(coll.phrase))\n",
        "scripts/selfcheck.py",
        "PDF 版式带上搭配",
    ),
    (
        "exporter.py JSON 字段表去掉 collocations（导出的文件里没有这一项）",
        "app/core/exporter.py",
        '    "synonyms",\n    "collocations",\n',
        '    "synonyms",\n',
        "scripts/selfcheck.py",
        "JSON 导出含 collocations 字段且值完整",
    ),
    (
        "importer.py JSON 回读不解析 collocations（导出再导入就丢了搭配）",
        "app/core/importer.py",
        '    raw_colls = item.get("collocations")\n'
        "    if isinstance(raw_colls, list):\n"
        "        colls = [c for c in (Collocation.from_raw(x) for x in raw_colls) if c]\n"
        "        if colls:\n"
        '            fill["collocations"] = colls\n',
        '    raw_colls = item.get("collocations")\n'
        "    if False:\n"
        '        fill["collocations"] = []\n',
        "scripts/selfcheck.py",
        "JSON 回读把搭配取回来",
    ),
    (
        "theme.py 把搭配简释移出加强色表（最透那一档只有这一行看不清）",
        "app/ui/theme.py",
        '    ("QLabel#collGloss", "text2_strong"),\n',
        "",
        "scripts/selfcheck.py",
        "搭配简释登记在加强色表里",
    ),
    (
        "theme.py 把视图切换按钮移出半透明规则（配了背景图那三个字压在原图上）",
        "app/ui/theme.py",
        '        ("QPushButton#mode", _rgb(pal["surface"])),\n',
        "",
        "scripts/selfcheck.py",
        "面板之外直接压在原图上的文字也拿到了受控的表面",
    ),
    (
        "widgets.py 收藏时丢掉简释与例句（只把词组文本收进去）",
        "app/ui/widgets.py",
        "            Phrase(\n"
        "                phrase=coll.phrase,\n"
        "                gloss_zh=coll.gloss_zh,\n"
        "                example=coll.example,\n"
        "                example_zh=coll.example_zh,\n"
        "                source_lemma=entry.lemma,\n"
        "            )\n",
        "            Phrase(phrase=coll.phrase)\n",
        "scripts/gui_smoke.py",
        "收藏下来的词组带着简释、例句与来源",
    ),
    (
        "widgets.py 词组详情的来源标签借用离线词典的名义（谎报比不报更糟）",
        "app/ui/widgets.py",
        '            source = QLabel("来源 词组收藏")\n',
        '            source = QLabel("来源 本机离线词库")\n',
        "scripts/gui_smoke.py",
        "选中词组后详情区走词组渲染",
    ),
    (
        "main_window.py 点收藏不落库（界面说「已收藏」、词组收藏里没有）",
        "app/ui/main_window.py",
        "            saved = self.repo.add_phrase(phrase)\n",
        "            saved = phrase\n",
        "scripts/gui_smoke.py",
        "点收藏把搭配写进词组收藏",
    ),
    (
        "main_window.py 撤销词组只按文本恢复（简释与例句回不来）",
        "app/ui/main_window.py",
        "            restored = self.repo.add_phrase(phrase)\n",
        "            restored = self.repo.add_phrase(Phrase(phrase=phrase.phrase))\n",
        "scripts/gui_smoke.py",
        "撤销把词组连简释与例句一起放回来",
    ),
    (
        "main_window.py 渲染词条时不传「已收藏的含本词的词组」",
        "app/ui/main_window.py",
        "        self.detail.show_entry(entry, collected)\n",
        "        self.detail.show_entry(entry)\n",
        "scripts/gui_smoke.py",
        "详情区回显已收藏的含本词的词组",
    ),
    (
        "main_window.py 词组详情复用词条渲染（备选框带的是词条的 id）",
        "app/ui/main_window.py",
        "            self.detail.show_phrase(phrase)\n",
        "            self.detail.show_entry(None if phrase is None else phrase.to_entry())\n",
        "scripts/gui_smoke.py",
        "词组详情里没有备注与标签输入框",
    ),
    (
        "main_window.py 搜索框提示语不随集合变（词组收藏里写着「搜索生词本」）",
        "app/ui/main_window.py",
        "        self.filter.setPlaceholderText(\n"
        '            "搜索生词本（支持中文）" if words else "搜索词组收藏"\n'
        "        )\n",
        '        self.filter.setPlaceholderText("搜索生词本（支持中文）")\n',
        "scripts/gui_smoke.py",
        "搜索框提示语随集合变化",
    ),
    (
        "main_window.py 在词组收藏里查词不切回生词本（结果落进不显示它的集合）",
        "app/ui/main_window.py",
        "        # 它不会报错，只是什么都不选中。\n"
        "        if self._mode != MODE_WORDS:\n"
        "            self._switch_mode(MODE_WORDS)\n",
        "        # 它不会报错，只是什么都不选中。\n"
        "        if False:\n"
        "            self._switch_mode(MODE_WORDS)\n",
        "scripts/gui_smoke.py",
        # 判据换过一次：触发这句保护的路径原本是「在词组模式输入单词会自动切回
        # 生词本」，而词组模式改成「只查词组、单词当场拒绝」之后，那个入口没有了，
        # 整轮随即报 MISSED（保护完好，只是没人再看它）。现在由「查词途中切到
        # 词组收藏」这个竞态来触发——比原来更接近真实操作。
        "查词途中切到词组收藏，结果回来时自动切回生词本",
    ),
    (
        "review.py 忘了评分不清零次数（答错却保留复习次数）",
        "app/core/review.py",
        "        return max(MIN_EASE, ease - 0.3), FIRST_INTERVAL, 0\n",
        "        return max(MIN_EASE, ease - 0.3), FIRST_INTERVAL, count  # 变异：忘了不清零次数\n",
        "scripts/selfcheck.py",
        "SM-2 忘了：ease 扣 0.3、间隔回 1 天、次数清零",
    ),
    (
        "review.py 记住第三次起间隔不再乘 ease（后续复习间隔不再拉长）",
        "app/core/review.py",
        "            new_interval = interval * ease\n",
        "            new_interval = SECOND_INTERVAL  # 变异：第三次起不再乘 ease\n",
        "scripts/selfcheck.py",
        "SM-2 记住第三次起：间隔乘 ease",
    ),
    (
        # 与「已显示则不重渲染」那条同属一类：都是「漏一处就静默出错」。
        # 手柄没有背景规则时取系统的 `QPalette::Window`，深色系统下就是中缝
        # 一条黑竖线。这条变异抽走规则，由 selfcheck 盯着**模板里还有没有这条**。
        #
        # **它原先挂在 gui_smoke 的像素判据上，那条判据抓不住**（本轮实测）：
        # 抽掉规则后 `INFO 底色：…分裂条手柄 #fbfbfa→#fbfbfa`——手柄像素在两个
        # 调色板下都是窗口底色，深色 palette 对它毫无影响。原因是离屏平台上
        # `QSplitter` 自己也不填背景，漏出来的是**窗口**（窗口有 QSS 底色）。
        # 也就是说这条「效果」在当前环境里读不出来，判据恒为真。
        # 出路不是把变异删掉，而是把判据挪到**能失败的那一层**：模板里规则在不在
        # （`容器底色来自主题而非系统调色板`）。效果本身只在真实桌面 + 系统深色
        # 下才看得见，那件事这台机器证不了，就不该在这里假装证过。
        "theme.py 分裂条手柄不留底色（中缝漏出系统深色，成一条黑竖线）",
        "app/ui/theme.py",
        "QSplitter::handle { background: transparent; }\n",
        "",
        "scripts/selfcheck.py",
        "容器底色来自主题而非系统调色板",
    ),
    (
        # 上一条的姊妹项：容器底色那块还有一个**能读出差别**的成员。
        # 对话框是真正的顶层窗口，它自己会填背景——抽掉规则就真的去取
        # `QPalette::Window`，深色下整块黑。这条同时是「深色调色板像素判据
        # 确实能失败」的自证：若它也没被抓住，说明那套判据整体是假的。
        "theme.py 对话框不留底色（系统深色下对话框整块黑、字看不清）",
        "app/ui/theme.py",
        "QDialog { background: $bg; }\n",
        "QDialog { background: none; }\n",
        "scripts/gui_smoke.py",
        "对话框底色不随系统调色板变黑",
    ),
    (
        # 收尾只 `close()` 不补事件处理时，进度框会以 100% 的样子留在屏幕上：
        # 紧接着的 `_reload_list()` 是同步重活，隐藏请求在那几秒里得不到处理。
        # 这条变异抽走那次 processEvents，由 gui_smoke 直接验收尾动作来抓。
        #
        # 锚点**必须连 `if app is not None:` 一起圈进来**：那一行是那个 `if` 的唯一
        # 函数体，只把 `processEvents` 那行删掉，文件直接「expected an indented
        # block」——变异被 `ast.parse` 守卫记成 `[BAD]`。报表上它和「没抓住」并列
        # 在同一个结论里，读起来像「这条保护形同虚设」，其实是**变异自己写错了**。
        #
        # 锚点还要跟着实现走：补「两次事件处理」那次改动（一次 hide、一次重绘）
        # 把这一行包进了 `for _ in range(2)`。锚点若仍按单行写，`count(old)` 就是
        # 0，残留检测器报「干净状态下误报残留」——读起来像仓库被污染，其实只是
        # 锚点陈旧。
        "main_window 收尾不补事件处理（进度框停在 100% 不掉）",
        "app/ui/main_window.py",
        "    app = QApplication.instance()\n"
        "    if app is not None:\n"
        "        for _ in range(2):\n"
        "            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)\n",
        "    app = QApplication.instance()\n"
        "    if app is not None:\n"
        "        pass  # 变异：收尾不补事件处理，进度框停在屏幕上\n",
        "scripts/gui_smoke.py",
        "收尾补了排除用户输入的事件处理",
    ),
    (
        # 「两次」不是顺手写的数：只 hide 不重绘，屏幕上还是原来那一帧 100%。
        # 判据直接数收尾时调了几次 processEvents，所以把 2 改成 1 必须红。
        "main_window 收尾只补一次事件处理（重绘没跟上，屏幕停在 100%）",
        "app/ui/main_window.py",
        "        for _ in range(2):\n",
        "        for _ in range(1):  # 变异：只补一次，重绘没跟上\n",
        "scripts/gui_smoke.py",
        "收尾补了排除用户输入的事件处理",
    ),
    (
        # 补了事件处理但不排除用户输入：那几毫秒里用户再点一次「导入」就会
        # 重入 `_do_import`（此刻 `_import_progress` 已置 None），新开一轮
        # 与正在收尾的这一轮抢列表状态。判据落在「排除」这一位上。
        "main_window 收尾的事件处理不排除用户输入（点击可重入）",
        "app/ui/main_window.py",
        "        app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)\n",
        "        app.processEvents()\n",
        "scripts/gui_smoke.py",
        "收尾补了排除用户输入的事件处理",
    ),
    # ---- 词组（v1.1.0）----
    #
    # 词组的整条链路是「解析器判定 → run_import 分派 → service 写另一张表」。
    # 每一段接错时界面都照常出报告，所以每段都得有一条能失败的判据。
    (
        # 上限写成 2：`look down upon` 这类 3 词搭配会被当成句子跳过。判据认的是
        # 「3 词还收得下」，它与「4 词以上按句子跳过」是同一根线的两端。
        "importer.py 词组词数上限收紧到 2（3 词搭配被当句子）",
        "app/core/importer.py",
        "MAX_WORDS_PER_PHRASE = 4\n",
        "MAX_WORDS_PER_PHRASE = 2\n",
        "scripts/selfcheck.py",
        "导入：词组的词数上限取 4（3 词的搭配收得下）",
    ),
    (
        # 分派写死成「都当单词」：词组会走 `service.lookup` 进生词本，而
        # `take off` 在离线词库里查不到，于是报告说「失败 1 条」——看着像词库
        # 没收录，实际是走错了集合。
        "importer.py 不按 kind 分派（词组一律当单词查）",
        "app/core/importer.py",
        "        if item.kind == KIND_PHRASE:\n",
        "        if False:  # 变异：词组一律当单词查\n",
        "scripts/selfcheck.py",
        "导入：词组进词组收藏，不进生词本",
    ),
    (
        # 摘掉「单个词不是词组」这一关：同一个词会在生词本与词组收藏里各存
        # 一份，删了一边另一边还在，而两边都叫「释义」。
        "dictionary.py 不再拒绝单个词当词组（两个集合各存一份）",
        "app/core/dictionary.py",
        '        if " " not in surface:\n',
        "        if False:  # 变异：不再区分单词与词组\n",
        "scripts/selfcheck.py",
        "词组：单个词在词组模式下被拒绝并指路",
    ),
    # ---- 单词表导出（v1.1.1）----
    #
    # 这份格式的全部价值就是「导出去还能原样导回来」。它出错的方式只有一种：
    # 多写了一点东西进去。而多写的那点东西在界面上完全看不出来——文件照样
    # 生成、照样能打开，只有导入报告里会多几行。
    (
        # 把标题写进第一行：导入侧会把「英语生词本」判成一行废行，报告里出现
        # 「没认出单词：英语生词本」。用户不会想到这是导出时加的标题。
        "exporter.py 单词表把标题写进第一行（导入时多一行废行）",
        "app/core/exporter.py",
        "    lines: list[str] = []\n"
        "    for entry in entries:\n"
        "        lemma = _flatten(entry.lemma)\n",
        "    lines: list[str] = [title]  # 变异：标题写进第一行\n"
        "    for entry in entries:\n"
        "        lemma = _flatten(entry.lemma)\n",
        "scripts/selfcheck.py",
        "导入：单词表导出没有任何一行被当成跳过",
    ),
    (
        # 多塞一列：界面上仍然叫「单词表」，文件里却成了三列。
        #
        # 这一列刻意取**恒有值**的序号，而不是音标/英文释义——那几个字段在
        # 界面那条路的库里不一定有值，变异就会与原实现逐字节相同，被记成
        # MISSED，读起来像「这条保护形同虚设」。
        "exporter.py 单词表多写一列（不再是两列）",
        "app/core/exporter.py",
        '        lines.append(f"{lemma}{WORDLIST_SEP}{gloss}" if gloss else lemma)\n',
        '        row = f"{lemma}{WORDLIST_SEP}{gloss}" if gloss else lemma\n'
        '        lines.append(row + WORDLIST_SEP + "1")  # 变异：多一列序号\n',
        "scripts/gui_smoke.py",
        "单词表每行最多两列",
    ),
    #
    # 托盘 / 全局热键 / 离线待重查。这三块的共同失败形态是「什么都没发生，
    # 也不报错」：热键没注册上表现为按了没反应，窗口藏进不存在的托盘表现为
    # 进程还在但打不开，标签写错表现为用户不知道该重查。
    (
        # 去掉词数上限：整句会被原样填进输入框。用户从网页上复制一句话，
        # 按 Alt+Q 得到一句英文，得先删一遍才能查——比不预填更烦，而界面
        # 没有任何地方会说「这里不该填整句」。
        "hotkey.py 剪贴板预填不设词数上限（整句也填进去）",
        "app/ui/hotkey.py",
        "    if not 1 <= len(words) <= MAX_WORDS_PER_PHRASE:\n        return \"\"\n",
        "    if not words:\n        return \"\"\n",
        "scripts/selfcheck.py",
        "剪贴板预填只收词与词组",
    ),
    (
        # 热键预填一律落生词本：剪贴板里是 `in charge of` 时，回车会把它拆成
        # in / charge / of 三个词分别去查，而用户要查的是那一条词组。
        "main_window 热键预填不按词数切到词组模式（词组被拆成单词）",
        "app/ui/main_window.py",
        "        target = MODE_PHRASES if len(seed.split()) >= 2 else MODE_WORDS\n",
        "        target = MODE_WORDS\n",
        "scripts/gui_smoke.py",
        "热键预填切到词组收藏",
    ),
    (
        # 不再检查托盘是否存在：窗口被 hide() 进一个不存在的托盘。进程还活着、
        # 数据库锁还占着，而桌面上什么都没有——再双击 exe 只会被告知「已在运行」。
        "main_window 没有托盘也把窗口收起来（窗口再也打不开）",
        "app/ui/main_window.py",
        "        return (\n"
        "            not self._quitting\n"
        "            and self._tray is not None\n"
        "            and self._tray.isVisible()\n"
        "            and bool(self.config.get(\"tray_enabled\", True))\n"
        "        )\n",
        "        return (\n"
        "            not self._quitting\n"
        "            and bool(self.config.get(\"tray_enabled\", True))\n"
        "        )\n",
        "scripts/gui_smoke.py",
        "没有托盘时关闭窗口不收进托盘",
    ),
    (
        # 离线词条也戴上「来源」前缀：那句话变成「来源 本机离线词库」，
        # 用户看不出这条还能靠接口变好，而界面照常显示、不报错。
        "base.py 离线标签也写成「来源 X」（不再提示待重查）",
        "app/providers/base.py",
        '    if key == "offline":\n        return "离线待重查"\n',
        '    if key == "offline":\n        return f"来源 {provider_label(source)}"\n',
        "scripts/selfcheck.py",
        "离线词条标签写「离线待重查」",
    ),
    # ---------- 本地统计：白名单与开关 ----------
    (
        # 开关只在界面上关掉、写入路径照旧：用户点了「不参与」，事件继续落盘。
        # 这种越界不会报错，只能靠「关掉之后库里一条都不该多」来抓。
        "analytics 不看开关，未授权也照记",
        "app/core/analytics.py",
        "        if not enabled():\n            return False\n",
        "",
        "scripts/selfcheck.py",
        "关闭统计后不再写入",
    ),
    (
        # 属性白名单失效：业务侧多传一个字段就跟着进库。统计库一旦收自由字段，
        # 「不记生词和笔记」这句承诺就只剩注释里那一行。
        #
        # 第一版变异写的是「未知键当成 count 校验」，结果 MISSED——多传的那个
        # 字符串本来就不满足 count，照样被丢，等于没变异。可见那条断言当时
        # 只判了「该在的在」，没判「不该在的不在」，两处都已改。
        "analytics 未知属性原样放行（不再按白名单过滤）",
        "app/core/analytics.py",
        "        kind = spec.get(key)\n        if kind is None:\n            continue\n",
        "        kind = spec.get(key)\n"
        "        if kind is None:\n"
        "            clean[key] = value\n"
        "            continue\n",
        "scripts/selfcheck.py",
        "事件写入且属性经白名单过滤",
    ),
    (
        # 未知事件名照样入库：事件表变成自由文本入口，等于把「只记这七件事」
        # 换成「谁调用谁说了算」。
        "analytics 未知事件名不再拒绝",
        "app/core/analytics.py",
        "        spec = EVENTS.get(event_name)\n        if spec is None:\n            return False\n",
        "        spec = EVENTS.get(event_name, {})\n",
        "scripts/selfcheck.py",
        "未知事件名被拒绝",
    ),
    (
        # 枚举属性只判「是不是字符串」，不看值在不在表里。这一松，报告里会出现
        # 同一列第四种写法（"接口"、"online"、"ONLINE" 各算一类），做分析时
        # 得先手工归一次，而归一化的依据已经没了。
        "analytics 枚举属性不再校验取值",
        "app/core/analytics.py",
        "        return value if isinstance(value, str) and value in allowed else None\n",
        "        return value\n",
        "scripts/selfcheck.py",
        "查词来源只认 online/offline/fallback",
    ),
    (
        # 旧库不补列。INSERT 会带着 session_id 去写一张没有这列的表，异常被
        # 「失败不致命」兜掉——表现是打开旧版本积累的统计后，事件一条不再增加，
        # 而界面上一切正常。
        "统计库升级时不补 session_id 列",
        "app/core/analytics.py",
        "    _migrate(conn)\n",
        "    pass  # 变异：不迁移旧库\n",
        "scripts/selfcheck.py",
        "旧统计库自动补上 session_id 列",
    ),
    (
        # 只看表名、不看完整性：被截断或被写坏的备份同样有 entries 表，
        # 放行之后用户拿它覆盖，词库就没了。
        "db.inspect_backup 不查 integrity_check",
        "app/core/db.py",
        '        row = conn.execute("PRAGMA integrity_check").fetchone()\n'
        '        if row is None or row[0] != "ok":\n'
        "            return f\"完整性检查未通过：{row[0] if row else '无返回'}\"\n",
        '        row = ("ok",)\n'
        '        if row is None or row[0] != "ok":\n'
        "            return f\"完整性检查未通过：{row[0] if row else '无返回'}\"\n",
        "scripts/selfcheck.py",
        "损坏的备份被拒绝",
    ),
    (
        # 只查完整性、不查有没有词条表：拿任何一个别的 sqlite 文件当备份都会
        # 通过，替换之后用户的词库变成别人的表。
        "db.inspect_backup 不查 entries 表",
        "app/core/db.py",
        '        if "entries" not in tables:\n'
        '            return "这个文件里没有词条表，不像是 Quill 的备份"\n',
        '        if "entries" not in tables:\n            pass\n',
        "scripts/selfcheck.py",
        "完整性 ok 但没有词条表被拒绝",
    ),
    (
        # 暂存改成把备份搬走：恢复一次，用户选的备份文件就没了。再恢复一次
        # 只能重新去挑，而界面上什么都没提示。
        "db.stage_restore 把备份搬走而不是复制",
        "app/core/db.py",
        "    tmp.write_bytes(source.read_bytes())\n",
        "    os.replace(source, tmp)\n",
        "scripts/selfcheck.py",
        "暂存不动备份本身",
    ),
    (
        # 不清 -wal：旧日志里的已提交页会在新库上回放，恢复出来的是一份混合体，
        # 而且不报错。
        "db.replace_with_staged 不清残留的 -wal/-shm",
        "app/core/db.py",
        '    for suffix in ("-wal", "-shm"):\n'
        "        target_path.with_name(target_path.name + suffix).unlink(missing_ok=True)\n",
        "    for suffix in ():\n"
        "        target_path.with_name(target_path.name + suffix).unlink(missing_ok=True)\n",
        "scripts/selfcheck.py",
        "替换清掉了残留日志",
    ),
    # ---------- 统计告知页：顺序与表态 ----------
    (
        # 两页换个先后。都是模态框，后弹的压住先弹的；离屏环境下表现为整个进程
        # 卡在 exec() 上，且不报错。
        "main_window 首启把告知页排在欢迎页前面",
        "app/ui/main_window.py",
        "        self._maybe_show_onboarding()\n        self._maybe_show_consent()\n",
        "        self._maybe_show_consent()\n        self._maybe_show_onboarding()\n",
        "scripts/gui_smoke.py",
        "首启先欢迎后告知",
    ),
    (
        # 关掉窗口当成同意：用户按 Esc 就被算作参与了统计。这类越界最讲究
        # 「有没有被记成同意」，界面上完全看不出来。
        "main_window 把「关掉告知页」当成同意",
        "app/ui/main_window.py",
        "        if dialog.choice is None:\n            return\n",
        "        if dialog.choice is None:\n            dialog.choice = True\n",
        "scripts/gui_smoke.py",
        "关掉告知页不算同意",
    ),
    (
        # 记录与提交合并成一个决定：勾着记录就默认允许发出去，而用户在告知页
        # 上看过的那句话是「提交是另一件事」。
        "settings 未勾选记录也照样生成报告",
        "app/ui/settings.py",
        "        if not self.analytics.isChecked():\n",
        "        if False:\n",
        "scripts/gui_smoke.py",
        "未勾选记录时拒绝提交报告",
    ),
    (
        # 索引少了行也不管：症状是「部分词搜不到」，而 search() 在 FTS 结果为空时
        # 会回退 LIKE，所以「全空」反而还能搜到。只有部分缺失那种才真丢结果，
        # 也最难发现。
        "init_db 不再检查索引与主表的条数",
        "app/core/db.py",
        "        elif not fts_in_sync(conn):\n"
        "            # 列集对得上，但条数对不上：上次的 DROP 建了一半就断了，或索引里留着\n"
        "            # 已删词条的残行。列集检查看不见这一类，必须单独查一次。\n"
        "            rebuild_fts(conn)\n",
        "        elif False:\n"
        "            pass\n",
        "scripts/selfcheck.py",
        "条数不符时自动重建索引",
    ),
    (
        # 失败后不回滚：DROP 留在未提交事务里，连接一旦被下一次 commit 带上，
        # 就是一个真正空掉的索引，而 has_fts 只认表名。
        "init_db 失败后不回滚",
        "app/core/db.py",
        "        conn.rollback()\n        raise",
        "        raise",
        "scripts/selfcheck.py",
        "重建失败后索引回到故障前状态",
    ),
    (
        # 一律重建：启动时白跑一遍全量索引，词多了就是明显的启动卡顿。
        "init_db 无条件重建索引",
        "app/core/db.py",
        "        elif not fts_in_sync(conn):\n",
        "        else:\n",
        "scripts/selfcheck.py",
        "结构正确时不重建索引",
    ),
    (
        # 把线程归属放回去，共享连接又变回「不报错但事务互相踩」。
        "db 连接放宽线程归属",
        "app/core/db.py",
        "    conn = sqlite3.connect(str(path))",
        "    conn = sqlite3.connect(str(path), check_same_thread=False)",
        "scripts/selfcheck.py",
        "主连接拒绝在别的线程上使用",
    ),
    (
        # 克隆偷懒返回自己：后台任务拿到的还是主线程那条连接。
        "仓储克隆退回共享同一实例",
        "app/core/repository.py",
        "        return Repository(connect(self.db_path))",
        "        return self",
        "scripts/selfcheck.py",
        "克隆出来的是另一条连接",
    ),
    (
        # 保留 with 结构但让它不互斥，比删掉三行更像一次「重构失误」。
        "离线词库懒加载去掉锁",
        "app/providers/offline.py",
        "            with self._conn_lock:",
        "            if True:",
        "scripts/selfcheck.py",
        "并发首次访问只打开一条词库连接",
    ),
    (
        # 后台查词改回直接把主线程的 service 交给 worker。
        "主窗口查词退回共享主连接",
        "app/ui/main_window.py",
        "            self._worker_lookup,\n            word,",
        "            self.service.lookup,\n            word,",
        "scripts/gui_smoke.py",
        "生词本模式查词照常落库",
    ),
]


def check_probe_names() -> list[str]:
    """静态校验：每条变异的「期望断言名」必须真的还出现在它跑的测试脚本里。

    Round 30 踩过两次这个坑，两次的症状都一样：**断言改了名字，变异还指着旧名字**。
    于是套件跑完报「失败 0 项」，变异被记成 `MISSED`，结论里写着「这些保护形同
    虚设」——**而保护好好的**，只是工具再也没在看它。同一轮还有第三次是判据本身
    恒为真（换了个更隐蔽的形态）。**工具自己的误报比测试漏报更难查，因为它看起来
    像证据、而且指着一个真实存在的位置。**

    判据是「脚本源码里还找得到这个名字」，走**子串**匹配：变异允许只写名字的
    一段（例如 `导入：句子行整行跳过` 同时覆盖「带标点 / 不带标点」两条断言），
    也允许名字本身是 f-string 拼出来的（源码里仍留着那段字面前缀）。

    带参数的测试命令（构建脚本那两条）取第一个词当脚本名。

    判据本身也要能失败——把任意一条 `probe` 改成不存在的名字，它必须报出来。
    """
    hits: list[str] = []
    cache: dict[str, str] = {}
    for label, _fpath, _old, _new, test, probe in MUTATIONS:
        script = test.split()[0]
        path = ROOT / script
        if path.suffix != ".py" or not path.exists():
            continue
        if script not in cache:
            cache[script] = path.read_bytes().decode("utf-8")
        if probe not in cache[script]:
            hits.append(
                f"「{probe}」在 {script} 里已经不存在了"
                f"（变异「{label}」再也看不到它）"
            )
    return hits


def _fingerprint() -> dict[str, str]:
    paths = {m[1] for m in MUTATIONS}
    return {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(paths)
    }


def selfcheck_leftover_detector() -> list[str]:
    """自证残留检测器不是瞎的。

    「检测到残留就中止」这条保护本身也可能失效——比如判据写错成永远为假，
    于是它一次都不报警，而**恰好**在真的有残留时继续跑下去。这正是本项目
    反复踩到的形态：一个恒为真的断言比没有断言更危险。

    办法是造一次**真的残留**（挑一条删除型的变异，把它落盘）、要求检测器
    报出来，再还原。挑删除型是关键：它的 `new` 是 `old` 的子串，只看
    「变异后在不在」的写法在这里必然误报或漏报，能顺带守住那个判据。
    """
    problems: list[str] = []

    # 先确认干净状态下不误报
    clean = check_no_leftovers()
    if clean:
        problems.append(f"干净状态下误报残留：{clean}")

    # 两种形态都必须自证：**纯删除**（`new` 是空串）与**替换**（`new` 非空，
    # 且可能是 `old` 的子串）。只测其中一种，另一种的判据坏了就没人知道——
    # 这次正是纯删除那一支整条失明，才让一条真实残留溜过了检测器。
    def _pick(pred) -> tuple | None:
        for entry in MUTATIONS:
            if entry[2].strip() and pred(entry[2], entry[3]):
                return entry
        return None

    targets = [
        t
        for t in (
            _pick(lambda old, new: new.strip() == ""),  # 纯删除
            _pick(lambda old, new: bool(new.strip()) and new in old),  # 子串替换
        )
        if t is not None
    ]
    if not targets:
        return problems  # 清单里没有可注入的变异就无从自证，不硬造问题

    for target in targets:
        label, fpath, old, new = target[0], target[1], target[2], target[3]
        path = ROOT / fpath
        raw = path.read_bytes()
        try:
            src = raw.decode("utf-8")
            if src.count(old) != 1:
                continue  # 锚点不唯一，跳过自证（主循环会报 SKIP）
            path.write_bytes(src.replace(old, new, 1).encode("utf-8"))
            found = check_no_leftovers()
            if not any(fpath in hit for hit in found):
                problems.append(
                    f"注入残留后检测器没报出来（注入的是「{label}」）——"
                    "这条中止保护形同虚设"
                )
        finally:
            path.write_bytes(raw)

    # 还原之后必须回到「无残留」
    if check_no_leftovers():
        problems.append("自证后未能还原干净")
    return problems


def _parse_argv(argv: list[str]) -> tuple[list[str], bool]:
    """取出 `--only <子串>`（可多次）与 `--full`。见模块 docstring「用法」。"""
    keys: list[str] = []
    full = "--full" in argv
    for index, arg in enumerate(argv):
        if arg == "--only" and index + 1 < len(argv):
            keys.append(argv[index + 1])
    return keys, full


def main(argv: list[str] | None = None) -> int:
    keys, want_full = _parse_argv(list(argv) if argv is not None else sys.argv[1:])

    # 先自证检测器：一个不会失败的检测器，等于没有检测器。
    detector_problems = selfcheck_leftover_detector()
    if detector_problems:
        print("残留检测器自证失败：")
        for line in detector_problems:
            print(f"  - {line}")
        return 4

    # 再查残留：拿一份被上次运行破坏过的源码当基线，得到的每个结论都不可信。
    # 这一步必须在记录指纹之前，否则会把「已经坏掉的状态」当成原状。
    #
    # **残留检查永远扫全表**，不因为 `--only` 缩小范围：它的职责是回答「仓库干净吗」，
    # 只扫自己要跑的那几条会把别处的残骸留在视野之外。
    leftovers = check_no_leftovers()
    if leftovers:
        print("检测到上一次运行残留的变异，已中止（继续跑会拿坏代码当基线）：")
        for line in leftovers:
            print(f"  - {line}")
        print("\n请手动把上述位置改回原样，或从备份 / 版本库恢复后重跑。")
        return 3

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _restore_in_flight)
        except (ValueError, OSError):
            # 非主线程或平台不支持，退化成「没有中断还原」——
            # 真正兜底的是下次运行时的残留检测。
            pass

    # 静态预检：probe 名字还在不在。必须在跑之前——名字失效的变异只会以 `MISSED`
    # 的形式出现（读起来像「保护形同虚设」），而整轮要跑好几个小时，跑完再发现
    # 等于白等一轮。见 `check_probe_names`。
    stale = check_probe_names()
    if stale:
        print("有变异的「期望断言名」已经不在它跑的测试脚本里（断言改了名没同步）：")
        for line in stale:
            print(f"  - {line}")
        print(
            "\n先修名字再跑：否则它们会被记成 MISSED，"
            "把「工具看不见」说成「保护失效」。"
        )
        return 4

    selected = [
        m for m in MUTATIONS if not keys or any(key in m[0] for key in keys)
    ]
    if keys and not selected:
        print(f"--only 没有匹配到任何变异：{keys}")
        return 4
    if keys:
        print(f"只跑匹配 {keys} 的 {len(selected)} 条（共 {len(MUTATIONS)} 条）")

    before = _fingerprint()
    failed: list[str] = []
    unknown: list[str] = []

    for label, fpath, old, new, test, probe in selected:
        path = ROOT / fpath
        # 按字节读，按字节写：见模块 docstring 里关于行尾的那一段
        raw = path.read_bytes()
        src = raw.decode("utf-8")

        # 锚点必须唯一。若不校验，「把 provider_status 改坏了、build_provider
        # 原封不动」会被当成「测试没抓住」——变异工具的误报比测试漏报更难查，
        # 因为它看起来像证据。
        count = src.count(old)
        if count != 1:
            print(f"[SKIP  ] {label} —— 锚点命中 {count} 次，无法定向变异")
            failed.append(label)
            continue

        _IN_FLIGHT[fpath] = raw
        mutated = src.replace(old, new, 1)

        # 变异必须让文件**仍然能解析**。
        #
        # 锚点唯一（`count == 1`）**不等于**锚点对得齐：替换是按**子串**做的，
        # 少写 4 个空格照样「唯一命中」——因为前面多出来的那 4 个空格仍在文件里。
        # 替换之后新行落在旧缩进上，文件直接 `IndentationError`；测试是**在导入
        # 阶段就死**，报表上只会写一句「CAUGHT*（代码崩溃）」——看着像「测到了」，
        # 其实是**变异自己把源码改坏了**，它要验的那条断言根本没轮到出场。
        # 本环境实际踩过一次（`widgets.py` 的锚点写成 8 空格，真实代码是 12 空格）。
        #
        # 挡在跑测试之前：省一次误判，也省一整轮等待。
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            print(
                f"[BAD   ] {label} —— 变异让源码无法解析（{exc.msg}，第 {exc.lineno} 行）。"
                f"这是**变异写错了**，不是测试没抓住"
            )
            failed.append(label)
            path.write_bytes(raw)
            _IN_FLIGHT.pop(fpath, None)
            continue

        path.write_bytes(mutated.encode("utf-8"))
        try:
            # 允许带参数的测试命令：构建脚本这类「自己断言产物」的工具，
            # 只能靠真跑一次构建来验证，光跑自检是查不到它的。
            try:
                res = subprocess.run(
                    [sys.executable, *test.split()],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=SUITE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                unknown.append(label)
                print(
                    f"[TIMEOUT] {label} —— 套件 {SUITE_TIMEOUT}s 未退出，"
                    "这一条既不算抓住也不算没抓住"
                )
                continue
            out = (res.stdout or "") + (res.stderr or "")
            hit = [
                line
                for line in out.splitlines()
                if probe in line and ("FAIL" in line or "✗" in line or "—" in line)
            ]
            caught = bool(hit) or ("失败 0 项" not in out and probe in out)
            crashed = "Traceback (most recent call last)" in out
            if not caught and not crashed:
                failed.append(label)
            tag = ("CAUGHT*" if crashed else "CAUGHT") if (caught or crashed) else "MISSED"
            note = "（代码崩溃，而非断言命中）" if crashed else ""
            print(f"[{tag}] {label}{note}")
            # 默认只打尾部，但**尾部恰好会漏掉判据自己打的 INFO 行**（它在开头），
            # 而排查一条 MISSED 时那往往正是唯一要看的一行。`--full` 打完整输出。
            if want_full or tag == "MISSED":
                lines = out.splitlines() if want_full else out.splitlines()[-12:]
                print(f"    ---- {'完整输出' if want_full else '输出尾部'} ----")
                for line in lines:
                    print("    " + line)
        finally:
            path.write_bytes(raw)
            _IN_FLIGHT.pop(fpath, None)

    after = _fingerprint()
    dirty = [p for p in before if before[p] != after.get(p)]
    if dirty:
        print()
        print("还原失败，以下文件未回到原状：")
        for p in dirty:
            print(f"  {p}")
        return 2

    print()
    if unknown:
        print(f"结论：{len(unknown)} 项变异**没能得出结论**（测试超时）：")
        for label in unknown:
            print(f"  - {label}")
        print("超时说明套件没跑完，既不能算抓住也不能算没抓住。先查它为什么卡住。")
    if failed:
        print(f"结论：{len(failed)} 项变异未被抓住，这些保护形同虚设：")
        for label in failed:
            print(f"  - {label}")
    if unknown or failed:
        return 1
    print(f"结论：{len(selected)} 项变异全部被抓住，源码已逐字节还原")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
