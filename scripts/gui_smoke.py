"""GUI 冒烟测试：离屏构建窗口，模拟查询、中文检索与同义语跳转。

用法：.venv/Scripts/python.exe scripts/gui_smoke.py
"""

from __future__ import annotations

import atexit
import faulthandler
import gc
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_GUI_HOME = tempfile.mkdtemp(prefix="quill_gui_")
os.environ["QUILL_HOME"] = _GUI_HOME

# 整轮兜底超时的上限。正常一轮在一分钟以内（最后一个阶段排在 26s，它自己再跑
# 十几秒），这里给三倍余量。超时不是「慢」，是「卡住」——见 main() 里的 _watchdog。
WATCHDOG_MS = 180_000


def _clean_home() -> None:
    """退出时删掉本次用的临时数据目录。

    **不能写 `atexit.register(shutil.rmtree, _GUI_HOME, True)`**——那个 `True` 是
    `ignore_errors`，它把「删干净了」和「静默没删」变成了同一件事。Qt 与 SQLite
    的句柄未必在解释器退出前释放，首次 `rmtree` 会被挡住，于是每跑一次就悄悄漏
    一个装着数据库的目录。本项目的铁律里有一条专讲这个（曾经累积到 3763 个目录 /
    2.6 GB，把系统盘写满，症状是 Bash 报 `ENOSPC`——看起来像工具坏了）。
    改为重试 + `gc.collect()`，仍失败就打印警告，不装作已经删掉。
    （被外部强杀时 `atexit` 不会执行，那种情况由 `clean_temp.py` 兜底——它已登记
    `quill_gui_*` 前缀。）
    """
    for _ in range(3):
        gc.collect()
        try:
            shutil.rmtree(_GUI_HOME)
            return
        except OSError:
            pass
    print(f"警告：冒烟测试临时目录未能清理：{_GUI_HOME}", flush=True)


atexit.register(_clean_home)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QMimeData, QPointF, QSize, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDropEvent, QImage, QPainter, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QProgressDialog,
    QPushButton,
)

from app.config import APP_DIR, DB_PATH, load_config  # noqa: E402
from app.core.db import connect, init_db  # noqa: E402
from app.core.dictionary import DictionaryService  # noqa: E402
from app.core.models import Entry, Synonym  # noqa: E402
from app.core.repository import Repository  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.ui import theme as theme_mod  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.settings import SettingsDialog  # noqa: E402
from app.ui.theme import stylesheet  # noqa: E402
from app.ui.widgets import EntryCard  # noqa: E402


class DelayingProvider:
    """按词设定延迟的 Provider，用于制造「先发的请求后返回」的乱序竞态。"""

    name = "delaying"

    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}

    def lookup(self, word: str) -> Entry:
        time.sleep(self.delays.get(word, 0.0))
        return Entry(
            lemma=word,
            definition_en=f"definition of {word}",
            definition_zh=f"{word} 的释义",
        )


class AlwaysFailingProvider:
    """任何查询都失败，用来在真实链路上模拟「接口挂掉」。"""

    name = "openai-compat"

    def lookup(self, word: str) -> Entry:
        from app.providers.base import ProviderError

        raise ProviderError("请求超时（30 秒）。可能是网络不稳定，或接口响应过慢。")


class PickyProvider:
    """认大多数词，但不认 z 开头的。

    导入的结果分三档——新增 / 已存在 / 未能收录——报告必须把三档都说清楚。
    拿「全认」或「全不认」的 Provider 来测，都只能覆盖其中两档；只认 3 个
    演示词的 MockProvider 更不行，它连「新增」这一档都验不出来。
    """

    name = "picky"

    def lookup(self, word: str) -> Entry:
        if word.startswith("z"):
            from app.providers.base import ProviderError

            raise ProviderError(f"词库里没有「{word}」")
        return Entry(
            lemma=word,
            definition_en=f"a word spelled {word}",
            definition_zh=f"{word} 的释义",
        )


class FallbackingProvider:
    """接口对指定词失败、由离线词库接住的形态。

    `accept_fallback=False` 要拦的正是这一种：内容拿得到，但来源是离线词库，
    写进去等于把这一条从接口内容**降级**。它与「Provider 直接抛错」是两条
    不同的失败路径，前者连错误都不会传到界面。
    """

    name = "openai-compat"

    def __init__(self, offline_words: set[str]) -> None:
        self.offline_words = offline_words
        self.calls: list[str] = []

    def lookup(self, word: str) -> Entry:
        self.calls.append(word)
        if word in self.offline_words:
            return Entry(
                lemma=word,
                definition_en="offline body",
                definition_zh="兜底释义",
                source="offline",
                fallback_reason="请求超时（30 秒）",
            )
        return Entry(
            lemma=word,
            definition_en=f"fresh {word}",
            definition_zh=f"{word} 的新释义",
        )


results: dict = {}
# 跨阶段传递（finalize 发起重查，check_refresh 校验结果）
refresh_target: dict = {}


class OfflineStubProvider:
    """真实生效的 provider 是离线词库。

    与 `config["provider"]` 无关——这正是关键：接口模式下没配密钥时，
    `build_provider()` 会静默退回离线词库，而 config 里仍写着
    "openai-compat"。判重查能不能跑，必须看这个 `name`。
    """

    name = "offline"

    def lookup(self, word: str) -> Entry:
        return Entry(lemma=word, definition_en="offline body", definition_zh="兜底释义")

def _match(expectation, actual) -> bool:
    """比对单项观测。expectation 可以是具体值，也可以是判定函数。"""
    if callable(expectation):
        try:
            return bool(expectation(actual))
        except Exception:  # noqa: BLE001 - 判定函数自身出错即视为不通过
            return False
    return actual == expectation


def _chips_equal_width(observed) -> bool:
    """同义语 chip 是否真的等宽、且宽度足够放下最长的那个词。

    三件事缺一不可：
    - 三个 chip 都找到了（少一个就说明这个场景没跑起来）；
    - `minimumWidth` 只有一个取值且大于 0——全是 0 时「取值唯一」也成立，
      那正是「没做等宽」的样子；
    - 实际宽度也只有一个取值，且不小于最长 chip 的自然宽度——宽度是从
      最短那个词算出来的话，实际宽度会参差（布局给每个 chip 的是它自己的
      sizeHint，除非被 minimumWidth 抬起来）。
    """
    if not isinstance(observed, dict):
        return False
    if observed.get("数量") != 3:
        return False
    minimums = observed.get("最小宽度") or []
    widths = observed.get("实际宽度") or []
    if len(minimums) != 1 or minimums[0] <= 0:
        return False
    if len(widths) != 1:
        return False
    return widths[0] >= (observed.get("自然宽度上限") or 0)


def _strip_delta(image: QImage, y: int, x0: int, x1: int) -> int:
    """一条横线上相邻像素的最大通道差。硬边越大，模糊后越小。"""
    worst = 0
    prev = image.pixelColor(x0, y).green()
    for x in range(x0 + 1, x1):
        current = image.pixelColor(x, y).green()
        worst = max(worst, abs(current - prev))
        prev = current
    return worst


def _checkerboard(size: int, cell: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255))
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                painter.drawRect(x, y, cell, cell)
    painter.end()
    return image


def _verdicts(observed: dict, table: dict) -> list[tuple[str, bool, str]]:
    """把「观测」变成「结论」。

    规则刻意从严，两条都源于本脚本原先的毛病：

    1. **观测到但没有判定标准 → 失败。** 原先 40 项观测全部只打印不比对，
       结论甚至被写成中文字符串（"(未提示！)"、"误报！"）却无人读取，
       于是任意一项真的退化时，机器拿到的仍是退出码 0。
    2. **有判定标准但没有观测到 → 失败。** 原先多处写成
       「无当前词条，跳过」——场景没跑起来却看起来一切正常，
       是最容易被当成通过的一种失败。
    """
    rows: list[tuple[str, bool, str]] = []
    for key in sorted(set(observed) | set(table)):
        if key not in table:
            rows.append((key, False, f"缺少判定标准｜观测={observed[key]!r}"))
        elif key not in observed:
            rows.append((key, False, "未采集到该观测（场景被跳过了？）"))
        else:
            rows.append((key, _match(table[key], observed[key]), f"实际={observed[key]!r}"))
    return rows


# 每项观测的判定标准。任何新增观测都必须在此登记，否则判定为失败——
# 这是刻意的：没有判定标准的观测等于没测。
EXPECT: dict = {
    "窗口标题": "Quill · 英语生词本",
    "接入状态标签": "离线示例模式（仅演示词条）",
    "查询后条数": 1,
    "详情区渲染词条": "ubiquitous",
    "详情区英文释义": lambda v: isinstance(v, str) and v.startswith("seeming to be everywhere at"),
    "同义语数量": 3,
    "左列表行数": 1,
    "计数标签": "共 1 条",
    "中文检索命中行数": 1,
    "英文同义语命中行数": 1,
    "完整词条缺项提示": "(无，符合预期)",
    "不完整词条缺项提示": lambda v: isinstance(v, str) and v.startswith("此词条不完整"),
    # 离线来源结构上没有同义语，缺项提示不能照喊；接口来源则照旧要喊。
    # 两条成对存在：只断言「离线不喊」的话，把提示整个删掉也能通过。
    "离线来源缺项提示": (
        lambda v: isinstance(v, str)
        and "例句" in v
        and "同义语" not in v
        and "离线词库" in v
    ),
    "接口来源缺项提示": lambda v: isinstance(v, str) and "同义语" in v,
    # 兜底产出必须看得出「不是接口给的」，并说明接口这次失败了。
    # 三条成组：只断言「有提示」的话，把来源标签或出路文案删掉也能通过。
    "兜底来源提示": (
        lambda v: isinstance(v, str) and "接口请求失败" in v and "离线词库" in v
    ),
    # 离线词条的标签**不是**「来源 X」的句式：它要同时说清「从哪来」和
    # 「还能更好」——接上接口重查一次，例句与搭配才会出现。
    "兜底来源标签": "离线待重查",
    "接口来源标签仍写来源": "来源 接口",
    "兜底后缺项改指重查": lambda v: isinstance(v, str) and "重新查询" in v,
    "非联网时缺项指向设置": lambda v: isinstance(v, str) and "切到接口模式" in v,
    "占位态已清除缺项提示": None,
    "占位态已清除陈旧提示": None,
    "旧版提示词词条已提示": lambda v: isinstance(v, str) and v.startswith("此词条由旧版提示词生成"),
    "版本一致时不提示陈旧": "(无，符合预期)",
    "同义语跳转后条数": 3,
    "同义语跳转后详情": "ephemeral",
    "备注已持久化": True,
    "按字母排序首行": "ephemeral",
    "按最早排序首行": "ubiquitous",
    "导出数据源条数": 3,
    "列表受显示上限约束": 2,
    "导出不受显示上限约束": 3,
    "非法配置被拦下": True,
    "非法配置提示": lambda v: isinstance(v, str) and len(v) > 0,
    "提示对用户可见": True,
    "合法配置可保存": True,
    "示例模式无需地址": True,
    "状态文件已生成": True,
    # 离屏平台可用区域只有 800x600，首次恢复会被钳制，故只校验格式而不锁死数值
    "还原窗口尺寸": lambda v: isinstance(v, str) and re.fullmatch(r"\d+x\d+", v) is not None,
    "还原分隔条": lambda v: isinstance(v, list) and len(v) == 2 and all(int(x) > 0 for x in v),
    "还原排序项": "oldest",
    "几何二次恢复稳定": True,
    "重查前备注未落盘": True,
    "重查后备注仍在": True,
    "重查后备注仍可检索": True,
    "重查状态栏": lambda v: isinstance(v, str) and "已重新查询并更新" in v,
    "竞态后界面显示": "fastword",
    "竞态后状态栏": lambda v: isinstance(v, str) and v.startswith("fastword"),
    # 兜底链路的**端到端**观测：真实走一遍「接口失败 → 离线词库 → 入库 → 界面」。
    # 上面那几条兜底提示是手工构造 Entry 验的，只覆盖渲染；链路中间的接线
    # （服务层是否保留运行时字段、界面是否走同一条渲染路径）它测不到。
    "兜底端到端来源": "offline",
    "兜底端到端失败原因": True,
    "兜底端到端界面提示": (
        lambda v: isinstance(v, str) and "接口请求失败" in v and "离线词库" in v
    ),
    "兜底端到端来源标签": "离线待重查",
    # fallback_reason 刻意不入库，但 source 必须落盘——否则下次打开这条
    # 离线内容就会冒充成接口结果
    "兜底来源已入库": "offline",
    # 真实数据上屏后不得出现字面量转义。源语料把换行存成两个字面量
    # （反斜杠 + n），构建层漏解码时这里是最近的现场；`apple` 的释义正好
    # 含一处，能真验到东西。
    "兜底词条释义无字面量转义": True,
    # 状态栏这句必须如实说来源。曾经它对所有非缓存结果都写「已从接口获取并保存」，
    # 于是兜底/离线内容在**常驻可见**的状态栏上被冒充成接口结果——正是兜底
    # 功能专门要避免的那件事，而且比详情区更难不被看见。
    "查询后状态栏": lambda v: isinstance(v, str) and "内置示例" in v,
    "兜底端到端状态栏": (
        lambda v: isinstance(v, str)
        and "接口请求失败" in v
        and "离线词库" in v
        # 离线来源结构上没有同义语，状态栏不能照喊缺项（恒定提示＝没有提示）
        and "同义语" not in v
    ),
    # 非接口模式下，兜底开关必须置灰：留着可编辑会让人以为离线模式也读它
    "非接口模式兜底开关置灰": True,
    "接口模式兜底开关可用": True,
    # 词库缺失时同样必须置灰：`build_provider()` 找不到词库就不挂兜底，
    # 开关可勾选等于谎报「有兜底」
    "词库缺失时兜底开关置灰": True,
    # ---- 左侧卡片与「从列表上删除」----
    # 卡片是绘制层的东西，数据层自检看不见；删除又必须走真按钮 → 真确认框 →
    # 真写库，直接调 repo.delete() 是替身，测不到接线。
    "卡片已挂载": True,
    "卡片数与行数一致": True,
    # 列表项文本被卡片盖住，但它是「这一行是什么」的可读真值，改成卡片式不该
    # 顺手把它丢掉——测试与无障碍接口都靠它
    "列表项文本仍可读": True,
    "卡片内容与词条一致": True,
    # 只验「有卡亮」的话，一个把所有卡都点亮的实现也能通过，故成对断言
    "卡片选中态跟随": True,
    # 先验反向：连确认框都不看的实现必须被这条挡住
    "取消删除不动数据": True,
    "卡片删除已落库": True,
    "卡片删除后行数减少": True,
    "卡片删除后状态栏": (
        lambda v: isinstance(v, str) and v.startswith("已删除「") and "」" in v
    ),
    # 删一张不相关的卡不该把右侧正在读的词条一起清掉
    "删除他人不打断阅读": True,
    "删除他人后仍选中原条": True,
    # ---- 界面层级的样式接线 ----
    # 工具栏分三级靠的是 objectName（主操作实心 / 常规描边 / 次要无边框）。
    # 断言整串而不是逐个「非空」：有人把某个按钮顺手改成 bare 也该被发现。
    "工具栏层级": ("primary", "", "", "bare", "bare", "bare"),
    # 下拉框从 86 放宽到 96。断言实际宽度——只断言「设了某个值」的话，
    # 一个把它改回 86 的实现照样通过。
    "排序下拉框宽度": 96,
    # 同义语 chip 等宽：右边那一列释义才对得齐。三个宽度都要看——
    # 只看「最小宽度相同」的话，一个把宽度全设成 0 的实现也能通过。
    "同义语 chip 等宽": _chips_equal_width,
    # 底部两个操作收拢到右侧（弹簧在最前）。顺序是观测值本身，
    # 「有弹簧」和「弹簧在最前」是两件事。
    "详情区底部操作顺序": ["弹簧", "重新查询", "删除词条"],
    # 「破坏性动作只在悬停时报警」这件事有两个半边：CSS 里那条规则（自检守），
    # 以及按钮**挂着 role=destructive 这个属性**（这里守）。缺任一半，警示色
    # 都不会出现，而且不报任何错——所以两条断言必须都在。
    "详情区操作的角色标记": {"重新查询": "", "删除词条": "destructive"},
    # ---- 自定义背景图 ----
    # 下面这三条是**从屏幕像素上读出来的**。只断言「配置里写了路径」是不够的：
    # 图片没画出来、或被窗口自己的底色盖住，配置照样是对的。
    "无背景时窗口底色": (0xFB, 0xFB, 0xFA),
    # 面板本身是白的。配了背景图之后它必须半透明——否则图片只会出现在 16px 的
    # 页边距里，等于「换了背景」看不出来。
    # 这一对读的是**控件的解析后调色板**而不是屏幕像素：离屏平台不会把子控件的
    # 半透明底色与父窗口画上去的图片做合成（实测读到的永远是纯白），读像素会
    # 得到一条误导性的失败。调色板里的 alpha 是同一个事实的直接证据。
    "无背景时面板不透明": (lambda v: isinstance(v, dict) and v.get("alpha") == 255),
    "有背景时面板半透明": (
        lambda v: isinstance(v, dict) and 120 < v.get("alpha", 255) < 255
    ),
    "背景图颜色": (
        lambda v: isinstance(v, tuple)
        and len(v) == 3
        and abs(v[0] - 255) <= 8
        and v[1] <= 8
        and abs(v[2] - 255) <= 8
    ),
    # 清晰与模糊成对：只写一条的话，一个「永远模糊」或「永远不模糊」的实现
    # 都能在单边上通过。
    # 比值是主判据（≥3 倍），绝对值那一半是防「糊得不够」——实测清晰 187、
    # 模糊 42，边缘过渡摊开到约 6 像素。
    "背景图模糊对比": (
        lambda v: isinstance(v, dict)
        and v.get("清晰", 0) > v.get("模糊", 0) * 3
        and v.get("模糊", 999) < 60
    ),
    "清除背景后属性": False,
    "清除背景后窗口底色": (0xFB, 0xFB, 0xFA),
    # 档位必须真的接上样式：三档的 alpha 依次变小、都还留着一点透明度。
    # 只写「有个半透明的面」不够——档位没接上样式时（属性名写错、选择器少写
    # 一段），面板会**一直是同一个 alpha**，看起来一切正常，那个下拉框白做了。
    # 下限 100 是防「透到文字读不清」：alpha 100 时最坏底色只有 100。
    "各档位的面板不透明度": (
        lambda v: isinstance(v, dict)
        and set(v) == {"soft", "medium", "bold"}
        and 255 > v["soft"] > v["medium"] > v["bold"] > 100
    ),
    "背景蒙版档位属性": "medium",
    # 设置界面：档位顺序与默认档。顺序反了会让「明显」看起来最含蓄。
    "背景透出档位顺序": ["soft", "medium", "bold"],
    "背景透出默认档": "medium",
    "无图时背景控件置灰": True,
    "有图时背景控件可用": True,
    "选中的档位会写进配置": "bold",
    # ---- 主题换色 ----
    "主题下拉项": ["ink", "sage", "clay", "plum", "graphite", "night"],
    # 每项都要有个色块：纯文字列表看不出「这套是什么颜色」，得先挨个点一遍
    "主题下拉项都带色块": True,
    "主题默认选中当前配置": "ink",
    "主题选了会写进配置": "plum",
    # 即时预览与还原：预览没生效的话，配置存对了界面也不会变；取消不还原的话，
    # 用户试完几套按取消，界面会停在最后试的那套上，而存的是原来那套。
    "主题预览即时生效": True,
    "取消设置会还原主题": True,
    # 「换色」体现在**强调色**上：主按钮的底色就是它。逐套与调色板对表，
    # 而不是只看「六套之间互不相同」——把六套全换成同一份也能满足后者。
    "各主题强调色": (
        lambda v: isinstance(v, list)
        and [x.lower() for x in v]
        == [theme_mod.palette(t)["accent"].lower() for t in theme_mod.theme_ids()]
    ),
    # 浅色的几套共用同一份底色（换的是强调色，不是底色），深色那套必须真的换掉底。
    "各主题窗口底色": (
        lambda v: isinstance(v, list)
        and len(v) == 6
        and len({tuple(x) for x in v[:5]}) == 1
        and tuple(v[5]) != tuple(v[0])
    ),
    "深色主题的窗口底色确实深": lambda v: isinstance(v, tuple) and sum(v) < 200,
    "浅色主题的窗口底色确实浅": lambda v: isinstance(v, tuple) and sum(v) > 700,
    # 最透档把直接压面板的小节文字换成加强色。**成对**：只验「换过了」的话，
    # 一个连 soft/medium 都一起换掉的实现也能通过，而那会把「淡 + 细」的层次压平。
    #
    # 期望值这一轮从 `text3_*` 改到 `text2_*`：分区小标题的颜色**跟着基础色走**
    # ——基础色从 `$text3`（占位/禁用，只守 3:1）归到了 `$text2`（真内容，守 4.5:1），
    # 加强色必须同步换档，否则会出现「平时是次要色、最透档反而换成更浅的占位色」。
    "最透档的小节文字色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("ink")["text2_strong"].lower()
    ),
    "适中档的小节文字保持原色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("ink")["text2"].lower()
    ),
    "深色主题最透档的小节文字色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("night")["text2_strong"].lower()
    ),
    # 交互状态：chip 与视图切换的实际底色。这两条**必须读像素**——「样式表里
    # 有没有那条规则」是文本检查，而 `QLabel#chip` 那种死规则照样能过文本检查，
    # 画面上那两处却什么都没变。
    "同义语 chip 底色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("ink")["accent_soft"].lower()
    ),
    "视图切换选中态底色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("ink")["accent_soft"].lower()
    ),
    "视图切换未选中态底色": (
        lambda v: isinstance(v, str)
        and v.lower() == theme_mod.palette("ink")["surface"].lower()
    ),
    # 同义语那一行的两段文字必须**取自调色板**：简释用 text、差异说明用 text2。
    # 判定在这里和调色板对表，而不是信观测里的某个布尔——写死的实现会在浅色
    # 那几套上恰好蒙对，所以逐套都要对（六套都得在观测里）。
    "同义语两行取色": (
        lambda v: isinstance(v, dict)
        and set(v) == set(theme_mod.theme_ids())
        and all(
            tuple(x.lower() for x in v[t])
            == (
                theme_mod.palette(t)["text"].lower(),
                theme_mod.palette(t)["text2"].lower(),
            )
            for t in theme_mod.theme_ids()
        )
    ),
    # ---- 把图片拖进窗口当背景 ----
    # 反向在前：不看确认框就直接改配置的实现，必须被第一条挡住。
    "拖入图片会先问一句": True,
    "拒绝后不改配置": True,
    "同意后配置指向这张图": True,
    "同意后记进最近使用": True,
    # 去重：数**个数**，不是判存在。上面那条 `in` 天生分不出「一个」和「两个」，
    # 所以去掉去重它照样为真——变异测试报 MISSED 的就是这种情况，错的不是实现。
    "同一张图重复使用不占两个位置": (1, 1),
    "清除背景后最近使用仍留着": True,
    "同意后状态栏如实报": lambda v: isinstance(v, str) and "已把" in v,
    "拖入词表仍走导入": True,
    # ---- 设置里的背景预览 ----
    # 判据是「画出来了」而且尺寸对：一个空 pixmap 也能骗过「有没有设过 pixmap」。
    "背景预览条尺寸": (224, 64),
    "有图时预览条非空": True,
    "无图时预览条为空": True,
    # ---- 一键导入生词表 ----
    # 反向在前：不看确认框就直接写库的实现，必须被第一条挡住。
    "导入确认框点否则不写库": True,
    "导入确认框点否时不落任何词": True,
    # 识别结果要在写库**之前**摆给用户看——解析终究是猜的，用户得有机会发现
    # 「我的文件被理解错了」，而不是导入完再去一堆词条里翻找垃圾
    "导入前先报识别结果": True,
    "导入确认框列出识别到的词": True,
    # 三档都要出现在报告里。只断言「新增 4 条」的话，一个把失败悄悄吞掉的
    # 实现照样通过——那正是「静默丢失」最典型的样子
    "导入新增落库数": 4,
    "导入结果报告": (
        lambda v: isinstance(v, str)
        and "新增 4 条" in v
        and "未能收录 1 条" in v
        and "zzzznotarealword" in v
    ),
    "导入状态栏": (
        lambda v: isinstance(v, str) and v.startswith("导入完成") and "新增 4 条" in v
    ),
    "导入后列表与库一致": True,
    "导入的词均已落库": True,
    # 被跳过的句子与查不到的词都不许写进库：一条是「宁可少收，不收错的」，
    # 一条是「空壳拒收」。这两条是导入最容易漏掉的边界
    "导入未写入被跳过的句子": True,
    "导入未写入查不到的词": True,
    "导入不留下进度框": True,
    # `导入不留下进度框` 只说明引用被清空了——**它证明不了窗口从屏幕上撤掉**。
    # 修复前那一条一直是绿的，而用户看到的是「卡在 100%、要过一会才能关掉」：
    # 收尾紧接着要重建整表（同步重活），隐藏请求在那几秒里得不到处理。
    # 下面两条直接验**收尾动作本身**，它们才有能力失败。
    "收尾时进度框被真正撤掉": True,
    "收尾补了排除用户输入的事件处理": True,
    # 另外两条「框留在屏幕上」的路径：点取消之后没有受理反馈（进度值停住、
    # 按钮还在，读起来就是「点了没用」），以及在导入途中关掉窗口（父窗口没了，
    # WindowModal 子窗口留在屏幕上，再没有任何东西能收回它的状态）。
    "导入取消立刻给出反馈": True,
    "关窗口时导入进度框一起撤掉": True,
    # 收尾之后计数必须归零：窗口不会为下一次导入重建，残留的数字会被
    # 后来的读者当成这一轮的结果。
    "导入完成后导入状态已复位": True,
    "docx 端到端导入": True,
    # 定时器回调里抛异常，Qt 只把 traceback 打到 stderr 就继续跑，**不会**让
    # 进程失败——等着 app.quit() 的测试于是挂到外层超时，看起来像环境抽风。
    # 实测踩过：一个 NameError 让整轮测试卡死 60 秒且一行结果都没打出来。
    "测试内部异常": "(无异常)",
    # ---- 导出对话框与格式说明 ----
    # 从前按钮写着「导出文本」，而保存框里藏着 Markdown——按钮名在替用户
    # 做决定。断言按钮名与提示都提到格式，是为了让「说了有没有别的」这件事
    # 有判据，而不是靠人点开下拉框才发现。
    "导出按钮名称": "导出",
    "导出按钮提示含所有格式": (
        lambda v: isinstance(v, str)
        and all(
            w in v
            for w in ("文本", "单词表", "Markdown", "PDF", "表格", "Anki", "JSON")
        )
    ),
    "导出对话框选项数": 7,
    "导出对话框条目数": [
        "text",
        "wordlist",
        "markdown",
        "pdf",
        "csv",
        "anki",
        "json",
    ],
    # 默认必须是最通用的纯文本，而不是最花哨的那个
    "导出默认格式": "text",
    # 列表有显示上限，导出默认跟随它会让打印稿少一截而用户看不出来
    "导出默认不受上限约束": True,
    # 没有筛选时该选项必须置灰。「能勾但不起作用」等同虚假选项
    "无筛选时范围选项置灰": True,
    "有筛选时范围选项可用": True,
    # 「选哪个」是这屏唯一要回答的问题，只给格式名等于把问题丢回给用户
    "导出每项都有用途说明": True,
    "格式说明可打开": True,
    "格式说明样例齐全": True,
    # 样例必须来自真实导出函数。这两条是**可验证的**证据：BOM 与格式标识
    # 都不可能被手写样例碰巧带上，除非它真的调了导出函数。
    "CSV 样例带 BOM": True,
    "JSON 样例带格式标识": True,
    # 界面这条路径真的能落盘（而不只是对话框长得对）
    "六种文本格式均可落盘": True,
    # 单词表是唯一「导出去是为了再导回来」的格式：多一行标题就会被导入侧
    # 当成一行废行，多一列就不叫单词表了。
    "单词表文件无表头表尾": True,
    "单词表每行最多两列": True,
    "单词表带着真实词条与中文释义": True,
    # PDF 需要字体：无字体环境下**拒绝输出**才是正确行为，故两种结局都算通过，
    # 但必须二选一——「既没写出来也没说原因」才是失败
    "PDF 要么落盘、要么明确说明原因": True,
    # ---- 本轮新增：托盘常驻 / 全局热键 / 离线待重查 / 全量词库 ----
    # 前两条判的是「没有托盘时不许藏窗口」：读不出来差别的话，用户拿到的是一个
    # 再也打不开的常驻进程（窗口藏进不存在的托盘，进程还占着数据库）。
    "没有托盘时不建托盘图标": True,
    "没有托盘时关闭窗口不收进托盘": True,
    "有托盘时关闭窗口收进托盘": True,
    "托盘里的退出是真的退出": True,
    "设置里关掉托盘后关闭即退出": True,
    # 预填的是「摸出来的词」，不是整段文字：整句填进去，用户先要删一遍
    "热键预填把词组填进输入框": "in charge of",
    "热键预填切到词组收藏": "phrases",
    "热键不预填整句": "",
    "托盘开关可用": True,
    "托盘开关默认勾上": True,
    "托盘开关如实反映配置": True,
    "无回调时隐藏重建索引按钮": True,
    "有回调时显示重建索引按钮": True,
    "重建索引按钮就地显示结果": True,
    # 离线词条说「待重查」，接口词条仍说「来源 接口」——两条成对，
    # 只测一条的话，把标签写死成「离线待重查」也能通过
    "接口来源标签仍写来源": "来源 接口",
    # ---- 本轮新增：标签入口与筛选 / 批量查词 / 撤销删除 / 测试连接 / 多文件拖入 ----
    # 这一批的共同点是「接错了也不报错」：标签框改了不落库、筛选下拉列了标签
    # 但点了不过滤、撤销按钮亮着但点了不恢复、拖入多个文件只留第一个。
    "标签输入框已出现在详情区": True,
    # 断言的是**归一后的存储形态**（「回归练习 高频」而不是「回归练习,   高频」）。
    # 不归一的话，按标签筛选只能退化成子串匹配，而子串匹配一定会在「某个标签是
    # 另一个的前缀」时出错（筛「考研」带出「考研英语」）。
    "标签改动会落库（走防抖定时器）": True,
    "标签筛选下拉列出了库里的标签": (
        lambda v: isinstance(v, list) and "高频" in v and "回归练习" in v
    ),
    "标签筛选那一行只在有标签时出现": True,
    "选了标签后列表只剩带该标签的词条": True,
    "标签筛选确实作用于列表": True,
    # 「标签」与「搜索」是**与**的关系。查询词取自一条**没有**该标签的词条，
    # 所以交集必空——「后者顶掉前者」与「两者并起来」都会给出非空结果。
    "标签与搜索取交集（不是谁顶掉谁）": True,
    "清掉标签筛选后回到全部": True,
    "批量查词把整串拆开收录": True,
    # 反面：整串本身不该变成一条词条。此前这里正是把整串当一个词去查，
    # 于是「粘 10 个词」得到的是一条 lemma 为整串的记录——不报错、不提示。
    "批量查词没有把整串当成一个词条": True,
    # 落到哪一条不重要（批次顺序是实现细节），「切过去了」才是这条要守的
    "批量查词后详情区切到新收的词": (
        lambda v: v in ("multecho", "multfoxtrot", "sharedword")
    ),
    # 从输入框粘进来的词与「导入文件」走同一条链路，但报告口径必须分开说：
    # 把批量查词写成「导入完成」，用户会去找自己什么时候导入了文件。
    "批量查词的报告口径不是「导入」": (
        lambda v: isinstance(v, str) and v.startswith("批量查词完成")
    ),
    # ---- 一键重查 ----
    # 这两条成对：「离线内容要算待更新」与「接口内容不算」缺一条，「只重查需要
    # 更新的」就可能退化成「全库重跑」或「一条都不查」，而两种都不会报错。
    "重查把离线来的内容标为待更新": True,
    "重查不把已是接口内容的标为待更新": True,
    # 范围跟着搜索框走：左栏看到的是哪些，被重查的就是哪些
    "重查范围跟随筛选": "搜索「rr」命中的词条",
    "重查只对需要更新的给出条数": 2,
    "重查只把需要更新的交给批量": 2,
    "重查跳过已是接口内容的词条": True,
    "重查把成功那条换成接口内容": True,
    "重查保留备注与标签": True,
    # 核心保护：接口这次没答上来（这里是兜底接住了）的那条必须保持原样。
    # 与「换成接口内容」成对——只有前者能过说明什么都没写，只有后者能过
    # 说明兜底内容把这条覆盖掉了。
    "重查让兜底的词条保持原样": True,
    "重查失败清单只含兜底那一条": ["rrfallback"],
    "重查报告口径": (
        lambda v: isinstance(v, str)
        and v.startswith("重新查询完成")
        and "更新 1 条" in v
        and "保持原样 1 条" in v
    ),
    # 离线来源下发起的重查必须被拒绝，并说明出路（去设置里接上接口）。
    # 判据看的是**真实 provider**：config 写着接口模式、实际却退回离线词库时，
    # 同样要拒绝——否则一键重查会把接口内容逐条降级成离线内容。
    "接口模式但实际用离线词库时重查被拒绝": True,
    "词组模式下重查被拒绝": True,
    "词组模式下重查按钮隐藏": True,
    # ---- 词组查询（本轮新增） ----
    # 「进词组收藏」与「不写生词本」必须成对：只验前者，一个把词组同时写进
    # 两张表的实现照样通过；只验后者，一个什么都不写的实现也过。
    "词组查询进词组收藏": True,
    "词组查询不写生词本": True,
    "词组查询后详情区显示该词组": "become ubiquitous",
    "词组查询后列表选中该条": True,
    "词组模式下输入框提示改为词组": (
        lambda v: isinstance(v, str) and "词组" in v and "单词" not in v
    ),
    "生词本模式下输入框提示是单词": (
        lambda v: isinstance(v, str) and "单词" in v and "词组" not in v
    ),
    "词组查询状态栏": (
        lambda v: isinstance(v, str) and "已收藏到词组收藏" in v
    ),
    "重复查词组不重复写库": True,
    "重复查词组的提示指向已有那条": (
        lambda v: isinstance(v, str) and "已在词组收藏里" in v
    ),
    # 单个词误入词组模式：拒绝 + 指路。写进去会在两个集合各留一条。
    "词组模式下单词被拒绝并指路": (
        lambda v: isinstance(v, str) and "不是词组" in v and "生词本" in v
    ),
    "词组模式下单词没有写进任何集合": True,
    "词组模式下离线来源被拒绝": True,
    # 导入：词组进词组收藏，报告要单列条数（混进「新增 N 条」会让用户对账对不上）
    "导入词组文件进词组收藏": True,
    "导入词组的报告单列条数": (
        lambda v: isinstance(v, str) and "新增词组 1 条" in v
    ),
    # 这一节种进去的词组必须撤干净：后面的搭配阶段拿「左栏正好列出那两条」
    # 当判据，上一个阶段留下的数据会让它报成「词组列表不刷新」。
    "词组查询用过的词组已清掉": True,
    "撤销按钮在删除后出现": True,
    "撤销把词条放回来了（含备注与标签）": True,
    # 反例：代码注释里曾声称「SQLite 会把删掉的行号分给下一个新词条」，
    # 取证后不成立（`entries.id` 带 AUTOINCREMENT，记在 sqlite_sequence 里）。
    "删除后新插入的词条拿到的是一个新 id（行号不复用）": True,
    # 与上一条**成对**：撤销会把一份「删除前的快照」写回库，而 `upsert()` 认的是
    # lemma、`entry.id is None` 才是分支开关。不清空 id 就会走「用户编辑过的对象」
    # 那一支，把用户在这次删除**之后**新写的备注静默盖掉——两条路径都报「已恢复」。
    # 只验 4a 的话，「永远覆盖」的实现照样通过。
    "撤销不覆盖删除之后新写的备注": True,
    "撤销用过即失效": True,
    # 结论必须是中文。httpx 的原始异常串是英文的，既说不出发生了什么，
    # 也说不出该改哪个字段——那正是「测试连接」这个按钮要避免的东西。
    "测试连接：不通时给出中文说明": (
        lambda v: isinstance(v, str)
        and len(v) > 6
        and ("接口地址" in v or "网络" in v or "超时" in v)
        and "Traceback" not in v
    ),
    "测试连接：通时给出模型名": (
        lambda v: isinstance(v, str) and v.startswith("连接正常") and "probe-model" in v
    ),
    # 推理型模型（deepseek-reasoner / R1 系列）不接受 temperature，带上会直接
    # 400——而那并不是用户配置错了。这条靠 stub 服务端读到的**真实请求体**来守。
    "测试连接的请求体不带 temperature（推理型模型会 400）": True,
    "测试连接只花 1 个 token 且带上密钥": True,
    "非接口模式下测试连接按钮置灰": True,
    "拖入多个词表文件全部被收下": ["drop_a.txt", "drop_b.txt"],
    "同一个文件拖两次只算一个": ["drop_a.txt"],
    "图片与词表混着拖时各归各的": (["drop_pic.png"], ["drop_a.txt"]),
    # 两份文件合成**一次**核对：拖 3 个文件弹 3 次框，做的是一个动作却要应 3 回
    "多文件导入只核对一次": 1,
    "多文件导入两份文件的词都在库里": True,
    "多文件合并时重复词只算一次": 3,
    # ---- 固定搭配与词组收藏 ----
    # 搭配区必须**列出词组本身**，而且每条各带自己的例句。只断言「这一块存在」
    # 的话，把例句整段去掉也能通过——而那正是这一块存在的理由（搭配的价值在
    # 「怎么用」上，一条条列出来等于把最该看的东西丢了）。
    "右侧固定搭配区列出每条搭配": lambda v: v == ["in charge of", "free of charge"],
    "每条搭配各配一条例句": lambda v: v == ["She is in charge of sales."],
    "搭配例句的翻译也跟着渲染": True,
    "每条搭配各有一个收藏按钮": lambda v: v == ["free of charge", "in charge of"],
    # 收藏这条链路的四种失败：写不进去 / 写进去但丢了简释与例句（组装那步漏字段）/
    # 按钮不改 / 按钮改了但库里没有。四条都验才盖得住——只验「按钮变成已收藏」
    # 的话，一个「改按钮但不写库」的实现照样全绿。
    "点收藏把搭配写进词组收藏": 1,
    "收藏下来的词组带着简释、例句与来源": (
        lambda v: v == ("负责", "She is in charge of sales.", "charge")
    ),
    "收藏按钮随即变成已收藏": "已收藏",
    "已收藏的按钮被禁用": True,
    "切换按钮上的词组条数跟着涨": "词组收藏 1",
    # 界面挡住了重复点击，但库这一层也必须幂等。两条成对：只断言「不新增」的话，
    # 一个「新增一条、但顺手把旧的那条删掉」的实现也能通过。
    "重复收藏在库这一层是幂等的（不新增、不覆盖）": True,
    "重复收藏时状态栏说明以先前那条为准": (
        lambda v: isinstance(v, str) and "已在词组收藏里" in v
    ),
    # 「含本词的词组」按词边界回显。顺序由创建时间决定，不写死。
    "详情区回显已收藏的含本词的词组": (
        lambda v: isinstance(v, str)
        and set(part for part in v.split("、") if part)
        == {"in charge of", "free of charge"}
    ),
    # 切集合之后**整套界面口径**都要跟着换：列表、计数、标签行、按钮、搜索提示。
    # 只验列表变了的话，计数标签会一直显示生词本的条数，而没有任何提示说明
    # 那个数字指的是另一个集合。
    "切到词组收藏后左栏列出已收藏的词组": lambda v: v == ["free of charge", "in charge of"],
    "词组模式下计数标签按词组算": "共 2 条",
    "词组模式下标签筛选那一行藏起来": True,
    "模式按钮反映当前集合": lambda v: v == (False, True),
    "搜索框提示语随集合变化": "搜索词组收藏",
    # 词组详情走另一条渲染路径：来源标签、**没有**备注与标签输入框、有移除按钮。
    # 中间那条是重点：那两个输入框发信号时带的是词条的 id，而词组的 id 落进去
    # 会指到另一张表里的同号词条，两边都会显示「已保存」。
    "选中词组后详情区走词组渲染（来源标成词组收藏）": "来源 词组收藏",
    "词组详情里没有备注与标签输入框": True,
    "词组详情区有移除按钮": ["从词组收藏中移除"],
    # 两张表各自从 1 发号：这一对是**造出真实撞车**之后才成立的判据——同一个
    # 数字在两张表里各有一条记录，删词条不得顺手把同号的那条词组也带走。
    "两张表里同时存在同一个行号（这正是删除必须带类别的原因）": True,
    "删词条不会连带删掉同号的词组": True,
    "撞车探针用过的词组已清掉": True,
    "移除按钮真的把词组删掉了": True,
    "移除词组不动生词本": True,
    "删词组后撤销按钮出现": True,
    # 撤销要放回来的是**整条**（简释与例句只存在这条记录里）。只记一个 id 的
    # 实现会把一个空壳放回来，而界面同样报「已恢复」。
    "撤销把词组连简释与例句一起放回来": (
        lambda v: v == ("负责", "She is in charge of sales.")
    ),
    "撤销词组后左栏切到词组收藏": "phrases",
    "撤销用过即失效": True,
    # 生词本模式下的查词不能因为「输入框按模式分派」而坏掉。分派写错（比如
    # 写成 `if self._mode:` 的真值判断）时，生词本里查一个词会走进词组那条路，
    # 界面照常弹一句「不是词组」，用户以为自己的词有问题。
    # 这一条原本断言的是「词组模式查词会自动切回生词本」——那个行为已被
    # ③c 的「词组模式下单词被拒绝并指路」取代（词组模式只查词组，单词当场
    # 拒绝并指向生词本，不再静默切集合替用户查），这里遂改为守住留下那一半。
    "生词本模式查词照常落库": True,
    # 查词结果属于生词本。响应回来时用户已经切到词组收藏了，就必须把集合切回去；
    # 否则右边渲染着刚查到的词、左栏一条都不选中，而状态栏照说「已收录」。
    "查词途中切到词组收藏，结果回来时自动切回生词本": "words",
    # 欢迎页 / 教程（welcome_check）。
    "欢迎弹窗标题": "欢迎来到 Quill",
    "欢迎弹窗署名": "made by wtd22",
    "欢迎弹窗功能点数": 5,
    "教程步数": 4,
    "欢迎弹窗按钮": lambda v: {"先看看教程", "开始使用", "返回欢迎页"} <= set(v),
    "欢迎弹窗被触发": True,
    "版本号已写回": True,
    "版本已最新不再弹": True,
    # 统计告知页（analytics_check）。两条硬边界：看过说明不等于同意；
    # 关掉窗口既不是同意也不是拒绝（下次还会问）。
    "告知页标题": "帮助我们改进 Quill",
    "告知页按钮": lambda v: v == ["暂不参与", "知道了，继续使用"],
    "告知页默认没表态": True,
    "点「知道了」记为同意": True,
    "点「暂不参与」记为拒绝": True,
    "只看说明时没有选择按钮": True,
    "只看说明不改表态": True,
    "首启先欢迎后告知": ["onboarding", "consent"],
    "拒绝后开关与表态都落盘": True,
    "关掉告知页不算同意": True,
    "已表态不再弹": True,
    "未勾选记录时拒绝提交报告": (0, True),
    # 复习模式（review_check）。
    "待复习按钮在生词本模式可见": True,
    "新词进入复习计划（待复习数 +1）": True,
    "待复习按钮显示计数": lambda v: isinstance(v, str) and v.startswith("待复习 "),
    "词组模式隐藏待复习按钮": True,
    "复习会话能取到到期词": True,
    "复习会话正面先看词不翻面": True,
    "复习会话翻面后给四档评分": lambda v: v == ["忘了", "模糊", "记住了", "太简单"],
    "复习评分推进了记忆状态": True,
    "复习会话评完推进或结束": True,
    # 容器底色（详情区面板 / 对话框）必须由主题给。系统开深色模式时，没有
    # 背景规则的容器会漏出系统色，与主题给的文字色撞成「深字压深底」。
    #
    # 分裂条手柄**不在这里**：同一条规则，但它的效果在离屏平台读不出来（实测
    # 抽掉规则后像素仍是窗口底色），判据恒为真。它改由 selfcheck 盯模板。
    "详情区底色不随系统调色板变黑": True,
    "对话框底色不随系统调色板变黑": True,
}


def main() -> int:
    config = load_config()
    config["provider"] = "mock"
    # 主流程把教程版本号标记为「已看过」，避免首次启动的欢迎弹窗（modal）阻塞
    # 后续场景。欢迎弹窗本身有专门的场景单独测（见 welcome_check）。
    from app.ui.onboarding import CURRENT_ONBOARD_VERSION

    config["onboard_version"] = CURRENT_ONBOARD_VERSION
    # 统计告知页同理：它也是模态框，未表态时每次启动都会弹。主流程标记为已表态，
    # 告知页本身另有场景单独测（见 analytics_check）。
    config["analytics_consent_decided"] = True

    conn = connect(DB_PATH)
    init_db(conn)
    repo = Repository(conn)
    service = DictionaryService(repo, MockProvider())

    app = QApplication(sys.argv)
    app.setStyleSheet(stylesheet())

    window = MainWindow(repo, service, config)
    window.show()
    results["窗口标题"] = window.windowTitle()
    results["接入状态标签"] = window.mode_label.text()

    def do_lookup() -> None:
        window.input.setText("ubiquitous")
        window._do_lookup()

    def verify() -> None:
        entry = window.detail._entry
        results["查询后条数"] = repo.count()
        results["详情区渲染词条"] = entry.lemma if entry else None
        results["详情区英文释义"] = (entry.definition_en[:28] + "…") if entry else None
        results["同义语数量"] = len(entry.synonyms) if entry else 0
        results["左列表行数"] = window.list.count()
        results["计数标签"] = window.count_label.text()
        results["查询后状态栏"] = window.statusBar().currentMessage()

        window.filter.setText("普遍")
        window._reload_list()
        results["中文检索命中行数"] = window.list.count()

        window.filter.setText("pervasive")
        window._reload_list()
        results["英文同义语命中行数"] = window.list.count()

        window.filter.setText("")
        window._reload_list()

        # 完整词条不应出现缺项提示
        results["完整词条缺项提示"] = (
            window.detail._warn_label.text() if window.detail._warn_label else "(无，符合预期)"
        )

        # 不完整词条必须给出可见提示
        repo.upsert(Entry(lemma="sparse", definition_en="english only"))
        window.detail.show_entry(repo.get("sparse"))
        results["不完整词条缺项提示"] = (
            window.detail._warn_label.text() if window.detail._warn_label else "(未提示！)"
        )
        window.detail.show_entry(None)
        results["占位态已清除缺项提示"] = window.detail._warn_label
        results["占位态已清除陈旧提示"] = window.detail._stale_label
        window._reload_list()

        # 离线来源的渲染：离线词库结构上没有同义语，若照喊，每条离线词条
        # 底下都会挂一句「缺少同义语」——提示恒定出现就等于没有提示。
        from app.core.models import Entry as _Entry

        window.detail.show_entry(
            _Entry(
                lemma="offlineprobe",
                definition_en="probe",
                definition_zh="探针",
                source="offline",
            )
        )
        _warn = window.detail._warn_label
        results["离线来源缺项提示"] = _warn.text() if _warn is not None else "(无提示)"

        window.detail.show_entry(
            _Entry(
                lemma="apiprobe",
                definition_en="probe",
                definition_zh="探针",
                source="openai-compat",
            )
        )
        _warn = window.detail._warn_label
        results["接口来源缺项提示"] = _warn.text() if _warn is not None else "(无提示)"

        # 兜底（接口失败 → 离线词库）。三件事必须在界面上同时成立：
        #   ① 说明接口这次失败了，否则用户看到释义就以为接口是通的；
        #   ② 来源标签写明「本机离线词库」，不冒充接口结果；
        #   ③ 缺项出路改成「重新查询」——此时模式就是接口，重查真的会去问接口，
        #      再叫他「去设置里切到接口模式」是句废话。
        window.detail.set_networked(True)
        window.detail.show_entry(
            _Entry(
                lemma="fallbackprobe",
                definition_en="probe",
                definition_zh="探针",
                source="offline",
                fallback_reason="请求超时（30 秒）。",
            )
        )
        _fb = window.detail._fallback_label
        results["兜底来源提示"] = _fb.text() if _fb is not None else "(无提示)"
        _src = window.detail._source_label
        results["兜底来源标签"] = _src.text() if _src is not None else "(无标签)"
        _warn = window.detail._warn_label
        results["兜底后缺项改指重查"] = _warn.text() if _warn is not None else "(无提示)"

        # 反向一条：接口来源必须保留「来源 接口」。若把「离线待重查」写成所有
        # 来源的通用标签，用户会以为自己拿到的接口内容同样残缺——而且界面不报
        # 任何错，只是那句话在撒谎。上面那两条读的是同一个 `_source_label`
        # 控件，所以先读再换，顺序反了会把兜底的观测一起冲掉。
        window.detail.show_entry(
            _Entry(
                lemma="apiprobe",
                definition_en="probe",
                definition_zh="接口探针",
                source="openai-compat",
            )
        )
        _api_src = window.detail._source_label
        results["接口来源标签仍写来源"] = (
            _api_src.text() if _api_src is not None else "(无标签)"
        )

        # 反向一条：主动选离线模式时，出路必须指回设置——重查还是同一本词典。
        # 不成对断言的话，把文案写死成其中一种也能通过。
        window.detail.set_networked(False)
        window.detail.show_entry(
            _Entry(
                lemma="offlineonlyprobe",
                definition_en="probe",
                definition_zh="探针",
                source="offline",
            )
        )
        _warn = window.detail._warn_label
        results["非联网时缺项指向设置"] = _warn.text() if _warn is not None else "(无提示)"

        # 提示词版本落后的词条必须提示，且提示要能与「缺项」区分开。
        # 做法是把库里的版本号改成上一版——这正是维护者改完 SYSTEM_PROMPT
        # 并递增 PROMPT_VERSION 之后，存量词条所处的状态。
        from app.providers.prompts import PROMPT_VERSION

        target_lemma = entry.lemma if entry else None
        if target_lemma:
            conn.execute(
                "UPDATE entries SET prompt_version=? WHERE lemma=?",
                (PROMPT_VERSION - 1, target_lemma),
            )
            conn.commit()
            # 走「点击列表打开」的路径：它不经过查词缓存，全靠 mark_stale 补标记
            stale_entry = service.mark_stale(repo.get_by_id(entry.id))
            window.detail.show_entry(stale_entry)
            results["旧版提示词词条已提示"] = (
                window.detail._stale_label.text()
                if window.detail._stale_label
                else "(未提示！)"
            )
            # 反向约束必须真的走「版本一致」这条比较，而不是靠「标记没算过」蒙对：
            # 把版本号改回当前值，再走同一条 mark_stale 路径，此时不该出现提示。
            conn.execute(
                "UPDATE entries SET prompt_version=? WHERE lemma=?",
                (PROMPT_VERSION, target_lemma),
            )
            conn.commit()
            window.detail.show_entry(service.mark_stale(repo.get_by_id(entry.id)))
            results["版本一致时不提示陈旧"] = (
                "(无，符合预期)" if not window.detail._stale_label else "误报！"
            )
        else:
            results["旧版提示词词条已提示"] = "无当前词条，跳过"

        window._reload_list()

        window.detail.synonymClicked.emit("ephemeral")

    def first_lemma() -> str | None:
        if window.list.count() == 0:
            return None
        return window.list.item(0).text().split("  ·")[0].strip()

    def finalize() -> None:
        results["同义语跳转后条数"] = repo.count()
        current = window.detail._entry
        results["同义语跳转后详情"] = current.lemma if current else None

        # 备注：编辑后应自动落库
        if current is not None:
            window.detail._note_edit.setPlainText("这是一条测试备注")
            window._flush_edits()
            saved = repo.get_by_id(current.id)
            results["备注已持久化"] = bool(
                saved and saved.notes == "这是一条测试备注"
            )

        # 排序：切换后首行应随之变化
        window.order.setCurrentIndex(2)  # 字母
        window._reload_list()
        results["按字母排序首行"] = first_lemma()
        window.order.setCurrentIndex(1)  # 最早
        window._reload_list()
        results["按最早排序首行"] = first_lemma()
        window.order.setCurrentIndex(0)  # 最近
        window._reload_list()

        # 导出与列表共用数据源
        results["导出数据源条数"] = len(window._current_entries())

        # 但上限不共用：列表可被显示上限截断，导出必须完整。
        # 把上限临时压到 2 来验证——否则要造 1000+ 条才能碰到边界。
        import app.ui.main_window as mw

        saved_limit = mw.LIST_LIMIT
        mw.LIST_LIMIT = 2
        try:
            results["列表受显示上限约束"] = len(window._current_entries())
            results["导出不受显示上限约束"] = len(
                window._current_entries(unlimited=True)
            )
        finally:
            mw.LIST_LIMIT = saved_limit

        # 设置对话框必须挡住非法输入：否则填错的地址要等到下次查词失败才
        # 暴露，那时用户早已忘了自己改过设置
        bad = SettingsDialog(
            {"provider": "openai-compat", "base_url": "", "model": ""}, "", window
        )
        bad._on_save()
        results["非法配置被拦下"] = bad.result() != QDialog.DialogCode.Accepted
        results["非法配置提示"] = bad._error.text()
        results["提示对用户可见"] = not bad._error.isHidden()

        good = SettingsDialog(
            {
                "provider": "openai-compat",
                "base_url": "https://api.test.com/v1",
                "model": "gpt-4o-mini",
            },
            "",
            window,
        )
        good._on_save()
        results["合法配置可保存"] = good.result() == QDialog.DialogCode.Accepted

        offline = SettingsDialog({"provider": "mock", "base_url": "", "model": ""}, "", window)
        offline._on_save()
        results["示例模式无需地址"] = offline.result() == QDialog.DialogCode.Accepted

        # 兜底开关只在接口模式下有意义。非接口模式必须置灰——留着可编辑会让人
        # 以为离线模式也会去读它，而它一个都不用。反向一条同时断言接口模式下可用，
        # 否则「开关永远是灰的」也能通过。
        results["非接口模式兜底开关置灰"] = not offline.fallback.isEnabled()
        _net_dialog = SettingsDialog(
            {"provider": "openai-compat", "base_url": "https://a/v1", "model": "m"},
            "",
            window,
        )
        results["接口模式兜底开关可用"] = _net_dialog.fallback.isEnabled()

        # 托盘开关。三件事：Windows 上能勾、默认勾上、如实反映配置。
        # 默认值写反的表现是「用户从没同意过常驻，关掉窗口程序却还在跑」；
        # 不反映配置的表现是「设置里关掉了，重启又自己勾上」。
        results["托盘开关可用"] = _net_dialog.tray.isEnabled()
        results["托盘开关默认勾上"] = _net_dialog.tray.isChecked()
        _tray_off_dialog = SettingsDialog(
            {"provider": "mock", "base_url": "", "model": "", "tray_enabled": False},
            "",
            window,
        )
        results["托盘开关如实反映配置"] = not _tray_off_dialog.tray.isChecked()

        # 重建搜索索引。与「从备份恢复」同一个规矩：主窗口没给回调时（自检、
        # 截图脚本）隐藏按钮，而不是摆一个点了没反应的。两个方向都要断言，
        # 否则「按钮永远是隐藏的」也能通过。
        results["无回调时隐藏重建索引按钮"] = offline.btn_rebuild_index.isHidden()
        _rb_dialog = SettingsDialog(
            {"provider": "mock", "base_url": "", "model": ""},
            "",
            window,
            on_rebuild_index=lambda: 7,
        )
        results["有回调时显示重建索引按钮"] = not _rb_dialog.btn_rebuild_index.isHidden()
        _rb_dialog.btn_rebuild_index.click()
        results["重建索引按钮就地显示结果"] = "7" in _rb_dialog._rebuild_note.text()

        # 词库文件不在时，兜底开关必须置灰——`build_provider()` 找不到词库就
        # 直接不挂兜底，留着可勾选等于在界面上撒谎。这条分支在本机不会自然
        # 出现（词库随包分发），所以用替换解析函数的方式把它造出来。
        import app.ui.settings as settings_module

        _real_resolver = settings_module.resolve_offline_dict
        settings_module.resolve_offline_dict = lambda: None
        try:
            no_dict = SettingsDialog(
                {"provider": "openai-compat", "base_url": "https://a/v1", "model": "m"},
                "",
                window,
            )
            results["词库缺失时兜底开关置灰"] = not no_dict.fallback.isEnabled()
        finally:
            settings_module.resolve_offline_dict = _real_resolver

        # 背景图那一行：档位下拉框必须真的存在、按档位顺序排好，并且**没有图时
        # 与模糊开关一起置灰**——两个控件归一处判断，分两处写迟早漏一个，漏掉
        # 的那个会一直可点、点了没反应。
        _probe_img = Path(os.environ["QUILL_HOME"]) / "settings_bg.png"
        _shot = QImage(40, 30, QImage.Format.Format_RGB32)
        _shot.fill(QColor(10, 20, 30))
        _shot.save(str(_probe_img))
        _no_bg = SettingsDialog(
            {"provider": "mock", "base_url": "", "model": ""}, "", window
        )
        results["无图时背景控件置灰"] = (
            not _no_bg.bg_blur.isEnabled() and not _no_bg.bg_veil.isEnabled()
        )
        _has_bg = SettingsDialog(
            {
                "provider": "mock",
                "base_url": "",
                "model": "",
                "background_image": str(_probe_img),
            },
            "",
            window,
        )
        results["有图时背景控件可用"] = (
            _has_bg.bg_blur.isEnabled() and _has_bg.bg_veil.isEnabled()
        )
        results["背景透出档位顺序"] = [
            _has_bg.bg_veil.itemData(i) for i in range(_has_bg.bg_veil.count())
        ]
        results["背景透出默认档"] = _no_bg.bg_veil.currentData()
        _has_bg.bg_veil.setCurrentIndex(2)
        _has_bg._on_save()
        results["选中的档位会写进配置"] = _has_bg.result_config().get("background_veil")

        # 主题：下拉项、色块、默认选中当前配置、选了会写进配置。
        # 色块不是装饰——纯文字列表看不出「这套是什么颜色」，用户得挨个点一遍。
        _theme_dlg = SettingsDialog(
            {"provider": "mock", "base_url": "", "model": "", "theme": "ink"}, "", window
        )
        results["主题下拉项"] = [
            _theme_dlg.theme.itemData(i) for i in range(_theme_dlg.theme.count())
        ]
        results["主题下拉项都带色块"] = all(
            not _theme_dlg.theme.itemIcon(i).isNull()
            for i in range(_theme_dlg.theme.count())
        )
        results["主题默认选中当前配置"] = _theme_dlg.theme.currentData()
        _theme_dlg.theme.setCurrentIndex(_theme_dlg.theme.findData("plum"))
        _theme_dlg._on_save()
        results["主题选了会写进配置"] = _theme_dlg.result_config().get("theme")

        # 背景预览条：它画的是「图 + 模糊 + 蒙版」三步之后的成品。判据不能只是
        # 「设过 pixmap」——一张纯色图也会给出非空 pixmap；用棋盘图，预览条里
        # 必须同时出现深浅两色，才说明图真的进去了。
        _prev_img = Path(os.environ["QUILL_HOME"]) / "settings_bg_checker.png"
        _checkerboard(320, 32).save(str(_prev_img))
        _prev_dlg = SettingsDialog(
            {
                "provider": "mock",
                "base_url": "",
                "model": "",
                "background_image": str(_prev_img),
                "background_blur": False,
                "background_veil": "soft",
            },
            "",
            window,
        )
        _pm = _prev_dlg._bg_preview.pixmap()
        _pm_img = _pm.toImage()
        _shades = {
            _pm_img.pixelColor(x, y).getRgb()[:3]
            for y in range(0, _pm_img.height(), 4)
            for x in range(0, _pm_img.width(), 4)
        }
        results["背景预览条尺寸"] = (_pm.width(), _pm.height())
        results["有图时预览条非空"] = not _pm.isNull() and len(_shades) >= 2
        results["无图时预览条为空"] = _no_bg._bg_preview.pixmap().isNull()

        # 窗口状态持久化：改尺寸/分隔条/排序 → 存 → 重建窗口验证还原
        window.resize(880, 640)
        window.splitter.setSizes([250, 590])
        window.order.setCurrentIndex(1)  # 最早
        window._save_state()
        results["状态文件已生成"] = (APP_DIR / "ui.ini").exists()

        second = MainWindow(repo, service, config)
        results["还原窗口尺寸"] = f"{second.size().width()}x{second.size().height()}"
        results["还原分隔条"] = second.splitter.sizes()
        results["还原排序项"] = second.order.currentData()

        # 不动点检验：首次恢复可能被屏幕可用区域修正（离屏平台只有 800x600），
        # 这属于正常适配。真正要守住的是「修正后不再漂移」——把 second 存盘后
        # 再建一个窗口，两轮几何必须字节级一致，否则每次开关窗口都会累积偏移。
        second._save_state()
        third = MainWindow(repo, service, config)
        results["几何二次恢复稳定"] = (
            bytes(third.saveGeometry()) == bytes(second.saveGeometry())
        )
        third.deleteLater()
        second.deleteLater()

        # 重新查询不得抹掉备注。刻意模拟最危险的时序：备注刚改、还压在防抖窗口里
        # （尚未落盘）就点了「重新查询」——这正是数据丢失最容易发生的时刻。
        target = window.detail._entry
        if target is not None:
            window._reload_list(select_id=target.id)
            window.detail._note_edit.setPlainText("防抖窗口内写的备注")
            results["重查前备注未落盘"] = window._pending_note is not None
            refresh_target["lemma"] = target.lemma
            window._refresh_word(target.lemma)
        else:
            results["重查前备注未落盘"] = "无当前词条，跳过"

    def check_refresh() -> None:
        lemma = refresh_target.get("lemma")
        saved = repo.get(lemma) if lemma else None
        results["重查后备注仍在"] = bool(
            saved and saved.notes == "防抖窗口内写的备注"
        )
        results["重查后备注仍可检索"] = bool(
            lemma and any(e.lemma == lemma for e in repo.search("防抖"))
        )
        # 状态栏能证明走的是重查专用回调：接错回调会抛 TypeError 落到
        # _on_error，状态栏会变成「查询失败」
        results["重查状态栏"] = window.statusBar().currentMessage()

        # 乱序竞态：先发一个慢请求，紧接着再发一个快请求。
        # 用户最后要的是 fastword，因此界面上必须是 fastword——
        # 慢请求姗姗来迟时不得把界面「抢」回去。
        service.set_provider(DelayingProvider({"slowword": 0.7}))
        window._lookup_word("slowword")
        QTimer.singleShot(60, lambda: window._lookup_word("fastword"))

    def race_check() -> None:
        current = window.detail._entry
        results["竞态后界面显示"] = current.lemma if current else None
        results["竞态后状态栏"] = window.statusBar().currentMessage()

        # 端到端兜底：真实走一遍「接口失败 → 离线词库 → 服务层写库 → 界面标注」。
        # 手工构造 Entry 只能验渲染；服务层丢掉运行时字段、或界面走了另一条
        # 渲染路径，它都测不出来。用 apple 是因为它必然不在离线词库的盲区、
        # 且此刻尚未入过库（否则会命中缓存、根本走不到 Provider）。
        from app.providers.fallback import FallbackProvider
        from app.providers.offline import OfflineDictProvider, resolve_offline_dict

        if resolve_offline_dict() is None:
            # 词库缺失是**功能不可用**，不是「跳过」。用显式值让判定如实报失败，
            # 而不是安静地把这一格留空。
            for key in (
                "兜底端到端来源",
                "兜底端到端界面提示",
                "兜底端到端来源标签",
                "兜底端到端状态栏",
                "兜底来源已入库",
                "兜底词条释义无字面量转义",
            ):
                results[key] = "(离线词库缺失)"
            results["兜底端到端失败原因"] = False
        else:
            window.config["provider"] = "openai-compat"
            # 走主窗口自己的判据，避免测试另立一套「算不算联网」的标准
            window.detail.set_networked(window._is_networked())
            service.set_provider(
                FallbackProvider(AlwaysFailingProvider(), OfflineDictProvider())
            )
            window._lookup_word("apple")

    def fallback_check() -> None:
        current = window.detail._entry
        if "兜底端到端来源" not in results:
            results["兜底端到端来源"] = current.source if current else None
            results["兜底端到端失败原因"] = bool(
                current and getattr(current, "fallback_reason", "")
            )
            _fb = window.detail._fallback_label
            results["兜底端到端界面提示"] = _fb.text() if _fb is not None else "(无提示)"
            _src = window.detail._source_label
            results["兜底端到端来源标签"] = (
                _src.text() if _src is not None else "(无标签)"
            )
            # 重新读库：fallback_reason 刻意不入库（那是历史信息），但 source
            # 必须落盘，否则下次打开这条离线内容就会冒充成接口结果
            reopened = repo.get("apple")
            results["兜底来源已入库"] = reopened.source if reopened else "(未入库)"
            results["兜底端到端状态栏"] = window.statusBar().currentMessage()
            # 界面上真实渲染出来的每一个字：只要有一处字面量 `\n` 就说明
            # 源语料的转义没被解码。比「断言库里有没有反斜杠」更贴近用户所见。
            _shown = "\n".join(
                lb.text() for lb in window.detail.findChildren(QLabel) if lb.text()
            )
            results["兜底词条释义无字面量转义"] = (
                "\\n" not in _shown and "\\r" not in _shown
            )

    def card_check() -> None:
        """左侧卡片：内容映射、选中态，以及「从列表上直接删除」。

        删除走真按钮点击 → 真确认框分支 → 真写库。手工调 `repo.delete()` 只能
        证明库能删，证明不了按钮接对了、确认框被真的看过了、列表跟着重载了。
        """

        def _pump(ms: int) -> None:
            # 删除被 `_request_delete` 推到了事件循环的下一轮（singleShot），
            # 必须真把事件泵起来，否则观测到的是「还没删」。
            deadline = time.monotonic() + ms / 1000
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

        cards = [
            window.list.itemWidget(window.list.item(i))
            for i in range(window.list.count())
        ]
        fallback_keys = (
            "卡片已挂载",
            "卡片数与行数一致",
            "列表项文本仍可读",
            "卡片内容与词条一致",
            "卡片选中态跟随",
            "取消删除不动数据",
            "卡片删除已落库",
            "卡片删除后行数减少",
            "删除他人不打断阅读",
            "删除他人后仍选中原条",
        )
        # 场景没跑起来要**如实报失败**，不能安静跳过：「没观测到」与「观测到且
        # 正确」在这一栏里长得一样，正是最容易被当成通过的一种失败。
        if len(cards) < 2:
            results.update(dict.fromkeys(fallback_keys, "(列表不足两行，场景未跑起来)"))
            results["卡片删除后状态栏"] = "(列表不足两行，场景未跑起来)"
            return

        results["卡片已挂载"] = all(isinstance(c, EntryCard) for c in cards)
        results["卡片数与行数一致"] = len(cards) == window.list.count()
        first_entry = repo.get_by_id(int(window.list.item(0).data(Qt.ItemDataRole.UserRole)))
        # 列表项文本被卡片盖住，但它是「这一行是什么」的可读真值；改成卡片式
        # 不该顺手把它丢掉——测试与无障碍接口都靠它
        results["列表项文本仍可读"] = (
            window.list.item(0).text().split("  ·")[0].strip() == cards[0].lemma
        )
        results["卡片内容与词条一致"] = bool(
            first_entry is not None and cards[0].lemma == first_entry.lemma
        )

        # 选中态。选第一行，第二张卡必须**不亮**——只断言「有卡亮着」的话，
        # 一个把所有卡都点亮的实现也能通过。
        window.list.setCurrentRow(0)
        results["卡片选中态跟随"] = bool(cards[0].property("selected")) and not bool(
            cards[1].property("selected")
        )

        # 删的是第二行：刻意不删当前显示的那条，才能验「删别人不打断阅读」
        target_card = cards[1]
        target_id = target_card.entry_id
        kept_id = int(window.list.item(0).data(Qt.ItemDataRole.UserRole))
        before = repo.count()

        import app.ui.main_window as mw

        real_box = mw.QMessageBox

        def _stub(answer: object) -> type:
            class _Box:
                StandardButton = real_box.StandardButton

                @staticmethod
                def question(*_a: object, **_k: object) -> object:
                    return answer

                # 兜底：别的方法在桩存续期间被调到也要能跑过，否则会把
                # 「缺方法」伪装成「查询失败」
                warning = staticmethod(lambda *a, **k: None)
                information = staticmethod(lambda *a, **k: None)

            return _Box

        # 反向一条：点「否」必须一条都不删。只验「删得掉」的话，一个连确认框
        # 都不看的实现照样通过。
        mw.QMessageBox = _stub(real_box.StandardButton.No)
        try:
            target_card.delete_button.click()
            _pump(120)
        finally:
            mw.QMessageBox = real_box
        results["取消删除不动数据"] = (
            repo.count() == before and repo.get_by_id(target_id) is not None
        )

        # 正向：点「是」
        mw.QMessageBox = _stub(real_box.StandardButton.Yes)
        try:
            target_card.delete_button.click()
            _pump(200)
        finally:
            mw.QMessageBox = real_box
        results["卡片删除已落库"] = repo.get_by_id(target_id) is None
        results["卡片删除后行数减少"] = repo.count() == before - 1
        results["卡片删除后状态栏"] = window.statusBar().currentMessage()
        current = window.detail._entry
        results["删除他人不打断阅读"] = bool(
            current is not None and int(current.id) == kept_id
        )
        selected = window.list.currentItem()
        results["删除他人后仍选中原条"] = bool(
            selected is not None and int(selected.data(Qt.ItemDataRole.UserRole)) == kept_id
        )
        # 这里**不** app.quit()：后面还有导入阶段要跑。退出只交给最后一个阶段；
        # 中间任何一处提前退出，都会让后面的观测变成「未采集到」——那看起来
        # 像那些用例失败了，其实根本没跑过。

    def import_check() -> None:
        """一键导入：真读文件 → 真解析 → 真写库 → 真报告。

        这一环没有替身可用：解析对不对要看真实文件的字节，写入有没有发生要看
        真实的库，而「被跳过的句子有没有溜进库」更是只有端到端才验得出来。
        """
        import app.ui.main_window as mw

        def _pump(ms: int) -> None:
            # 导入被拆成多次后台往返，必须真把事件泵起来，否则观测到的是
            # 「还没导完」——那种失败看起来像功能坏了，其实只是测早了。
            deadline = time.monotonic() + ms / 1000
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

        def _write_min_docx(path: Path, words: list[str]) -> None:
            """现造一份最小 .docx：zip 里只放 `word/document.xml`，一行一个词。

            刻意不用第三方库来造——读取端也是纯标准库解出来的，这样测到的
            就是产品真正会走的那条路（而不是「我以为的 docx」）。
            """
            ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            body = "".join(
                f'<w:p><w:r><w:t xml:space="preserve">{word}</w:t></w:r></w:p>'
                for word in words
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>',
                )

        home = Path(os.environ["QUILL_HOME"])
        src = home / "import_words.txt"
        src.write_text(
            "1. alphaone  /ˈælfə/  adj. 甲的\n"
            "bravotwo\n"
            "charlie, delta\n"
            "zzzznotarealword\n"
            "She went to the store to buy some milk\n",
            encoding="utf-8",
        )

        # 本阶段验的是导入本身的接线，不是某个词库收没收录某个词
        window.service.set_provider(PickyProvider())

        real_box = mw.QMessageBox
        seen: list[tuple[str, str]] = []

        def _stub(answer):
            class _Box:
                StandardButton = real_box.StandardButton

                @staticmethod
                def question(*a, **_k):
                    seen.append(("question", str(a[2])))
                    return answer

                @staticmethod
                def information(*a, **_k):
                    seen.append(("information", str(a[2])))
                    return _Box.StandardButton.Ok

                @staticmethod
                def warning(*a, **_k):
                    seen.append(("warning", str(a[2])))
                    return _Box.StandardButton.Ok

            return _Box

        before = repo.count()

        # 反向一条：确认框点「否」，一个词都不许写进去。
        #
        # 这里**必须把事件泵起来再断言**：导入是后台进行的，一个「不看确认框、
        # 直接开导」的实现当场什么都不会写，立即断言会得到「库里没变」而被
        # 误判为通过。变异测试正是这样漏掉过一次——它把这条保护破坏掉之后，
        # 测试居然还是绿的。
        mw.QMessageBox = _stub(real_box.StandardButton.No)
        try:
            window._import_path(src)
            _pump(2000)
        finally:
            mw.QMessageBox = real_box
        results["导入确认框点否则不写库"] = repo.count() == before
        results["导入确认框点否时不落任何词"] = repo.get("alphaone") is None
        seen.clear()

        # 正向
        mw.QMessageBox = _stub(real_box.StandardButton.Yes)
        try:
            window._import_path(src)
            _pump(3000)
        finally:
            mw.QMessageBox = real_box

        preview = next((t for k, t in seen if k == "question"), "")
        report = next((t for k, t in seen if k == "information"), "")

        # 识别出 5 条，其中 z 开头那个查不到（PickyProvider 不认），
        # 所以最终落库 4 个——「认出多少」与「收进多少」是两件事，报告要分开说。
        # 文案里现在把「单词 / 词组」分开计数（词组另进一个集合），这条断言
        # 认的是**总数与跳过行**，不跟着措辞漂。
        results["导入前先报识别结果"] = (
            "认出 5 条" in preview and "跳过 1 行" in preview
        )
        results["导入确认框列出识别到的词"] = "alphaone" in preview and "delta" in preview
        results["导入新增落库数"] = repo.count() - before
        results["导入结果报告"] = report
        results["导入状态栏"] = window.statusBar().currentMessage()
        results["导入后列表与库一致"] = window.list.count() == repo.count()
        results["导入的词均已落库"] = all(
            repo.get(w) is not None for w in ("alphaone", "bravotwo", "charlie", "delta")
        )
        results["导入未写入被跳过的句子"] = repo.get("she") is None
        results["导入未写入查不到的词"] = repo.get("zzzznotarealword") is None
        results["导入不留下进度框"] = window._import_progress is None

        # `_import_progress is None` 只说明引用被清空——**它证不了窗口撤掉了**。
        # 所以这里直接验收尾动作本身：撤掉，外加补一次**排除用户输入**的事件处理。
        # 排除用户输入是必须的：那几毫秒里若放进用户点击，用户再点一次「导入」
        # 就会重开一轮，和正在收尾的这一次抢列表状态。
        dlg = QProgressDialog("正在导入…", "取消", 0, 10, window)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(10)
        dlg.show()
        pumps: list = []

        class _FakeApp:
            @staticmethod
            def instance():
                return _FakeApp()

            @staticmethod
            def processEvents(*args, **_kwargs):
                pumps.append(args[0] if args else None)

        real_app_cls = mw.QApplication
        mw.QApplication = _FakeApp
        try:
            mw._dismiss_progress(dlg)
        finally:
            mw.QApplication = real_app_cls

        results["收尾时进度框被真正撤掉"] = not dlg.isVisible()
        # 两次：一次处理 hide，一次处理重绘。屏幕上那块残留的 100% 只有重绘之后
        # 才真的消失——只 hide 不重绘，用户看到的还是原来那一帧。
        results["收尾补了排除用户输入的事件处理"] = (
            len(pumps) == 2
            and all(p == QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents for p in pumps)
        )

        # 取消必须**立刻**有反应。只置 `_import_cancelled` 的话，界面在接下来
        # 这一批跑完之前一动不动：接口模式下那是 10 个词、几十秒。用户看到进度
        # 值停住、按钮还在，读到的结论就是「取消没用、这个框关不掉」。
        cancel_probe = QProgressDialog("正在导入…", "取消", 0, 10, window)
        cancel_probe.setAutoClose(False)
        cancel_probe.setAutoReset(False)
        cancel_probe.setValue(3)
        cancel_probe.show()
        app.processEvents()
        buttons_before = [
            b for b in cancel_probe.findChildren(QPushButton) if b.isVisible()
        ]
        window._import_progress = cancel_probe
        window._cancel_import()
        app.processEvents()
        buttons_after = [
            b for b in cancel_probe.findChildren(QPushButton) if b.isVisible()
        ]
        results["导入取消立刻给出反馈"] = (
            len(buttons_before) >= 1
            and len(buttons_after) == 0
            and "正在停止" in cancel_probe.labelText()
            and window._import_cancelled
        )
        window._import_progress = None
        window._import_cancelled = False
        cancel_probe.hide()
        cancel_probe.deleteLater()

        # 导入跑到一半被关窗口：进度框是窗口的 WindowModal 子对象，父窗口没了
        # 之后它会留在屏幕上，而那时已经没有任何东西能收回它的状态——用户看到的
        # 就是一个「关不掉的 100%」。单独开一个窗口来验，主窗口还要继续用。
        closer = MainWindow(repo, service, config)
        closer.show()
        orphan = QProgressDialog("正在导入…", "取消", 0, 10, closer)
        orphan.setAutoClose(False)
        orphan.setAutoReset(False)
        orphan.setValue(10)
        orphan.show()
        app.processEvents()
        orphan_was_visible = orphan.isVisible()
        closer._import_progress = orphan
        closer.close()
        app.processEvents()
        results["关窗口时导入进度框一起撤掉"] = (
            orphan_was_visible
            and not orphan.isVisible()
            and closer._import_progress is None
        )
        closer.deleteLater()

        # 老师的 Word 单词表：真造一份 .docx（zip + word/document.xml）走同一条
        # 导入链路。这条验的是「读得出」；「选得到、拖得进」由后缀表与过滤器
        # 保证（selfcheck 里盯着），两者合起来才是用户实际遇到的那条路。
        docx_src = home / "import_words.docx"
        _write_min_docx(docx_src, ["echozulu", "foxtrot"])
        mw.QMessageBox = _stub(real_box.StandardButton.Yes)
        try:
            window._import_path(docx_src)
            _pump(3000)
        finally:
            mw.QMessageBox = real_box
        results["docx 端到端导入"] = bool(
            repo.get("echozulu") is not None and repo.get("foxtrot") is not None
        )
        # 收尾之后这一轮的计数必须清干净：窗口不会为下一次导入重建，这些字段会
        # 一直挂着上一轮的数字，任何读它们的地方都会把旧数字当成这一轮的结果。
        results["导入完成后导入状态已复位"] = (
            window._import_total == 0
            and window._import_done == 0
            and not window._import_queue
            and window._import_cancelled is False
            and window._import_error == ""
        )
        export_stage()

    def export_stage() -> None:
        """导出对话框与格式说明。

        这两块是纯界面，数据层自检看不见。而它们恰好是「用户点下去会遇到的那
        一屏」：格式选不了、说明打不开，用户就不知道能导出什么——这正是这一轮
        要解决的问题本身。
        """
        from app.ui.export_dialog import EXPORT_FORMATS, ExportDialog, FormatHelpDialog

        # 按钮名必须与它实际能出什么一致。从前写着「导出文本」，保存框里却藏着
        # Markdown——按钮名在替用户做决定，而他不知道还有别的。
        results["导出按钮名称"] = window.btn_export.text()
        results["导出按钮提示含所有格式"] = window.btn_export.toolTip()

        dlg = ExportDialog(total=window.repo.count(), filtered=False, parent=window)
        try:
            results["导出对话框选项数"] = len(dlg._buttons)
            results["导出对话框条目数"] = [k for k, _l, _h, _e in EXPORT_FORMATS]
            results["导出默认格式"] = dlg.format_key
            results["导出默认不受上限约束"] = dlg.unlimited
            # 没有筛选时「跟随当前筛选」必须置灰：可勾选却不起作用是虚假选项
            results["无筛选时范围选项置灰"] = not dlg.scope_limited.isEnabled()
            # 每一项都必须带用途说明——「选哪个」是这屏唯一要回答的问题，
            # 只给格式名等于把问题原样丢回给用户
            hints = [
                dlg._buttons[k].toolTip() for k, _l, _h, _e in EXPORT_FORMATS
            ]
            results["导出每项都有用途说明"] = all(bool(h) for h in hints)
        finally:
            dlg.deleteLater()

        with_filter = ExportDialog(total=1, filtered=True, parent=window)
        try:
            results["有筛选时范围选项可用"] = with_filter.scope_limited.isEnabled()
        finally:
            with_filter.deleteLater()

        help_dlg = FormatHelpDialog(parent=window)
        try:
            results["格式说明可打开"] = help_dlg.windowTitle() == "导出格式说明"
            # 说明里的样例必须与真实导出函数同源。若把样例写成死字符串，
            # 格式一改说明就开始骗人——而它骗的正是「不知道怎么选」的用户。
            from app.ui.export_dialog import format_samples

            samples = format_samples()
            results["格式说明样例齐全"] = (
                set(samples) == {k for k, _l, _h, _e in EXPORT_FORMATS}
            )
            # 抽查两条：CSV 的样例必须真带 BOM、JSON 的样例必须真带格式标识。
            # 这样「样例来自真实导出函数」这件事本身是可验的，而不是靠注释声明。
            results["CSV 样例带 BOM"] = samples["csv"].startswith("\ufeff")
            results["JSON 样例带格式标识"] = '"quill-entries"' in samples["json"]
        finally:
            help_dlg.deleteLater()

        # 真实写一遍五种格式，验证界面这条路径（含编码、含 PDF 的字体分支）。
        # 上面那几条验的是「对话框长什么样」，这里验的是「点下去真的能落盘」。
        import tempfile as _tf

        out_dir = Path(_tf.mkdtemp(prefix="quill_export_"))
        _tf_atexit = __import__("atexit")

        def _clean_out_dir() -> None:
            # 同 `_clean_home`：不能用 `ignore_errors=True`，那会把「删干净了」
            # 和「静默没删」变成同一件事。
            for _ in range(3):
                gc.collect()
                try:
                    shutil.rmtree(out_dir)
                    return
                except OSError:
                    pass
            print(f"警告：导出临时目录未能清理：{out_dir}", flush=True)

        _tf_atexit.register(_clean_out_dir)
        written: list[str] = []
        pdf_note = ""
        for key, _label, _hint, ext in EXPORT_FORMATS:
            # 文件名必须带上格式键：纯文本、单词表、Anki 三种都是 .txt，只按
            # 扩展名命名会让后一轮覆盖前一轮的产物——检查单词表内容时读到的
            # 可能是 Anki 那份，而断言照样全绿（读到的确实是「一个 .txt」）。
            target = out_dir / f"probe_{key}{ext}"
            try:
                window._write_export(key, window._current_entries(unlimited=True),
                                     "测试册", target)
                if target.exists() and target.stat().st_size > 0:
                    written.append(key)
            except Exception as exc:  # noqa: BLE001 - PDF 的字体分支要如实报告
                pdf_note = f"{type(exc).__name__}: {exc}"
        # PDF 需要字体，无字体环境下不写 PDF 是**正确行为**，不算失败；
        # 但其余六种必须都写出来，否则就是真的坏了一条路径。
        others = [
            k
            for k in ("text", "wordlist", "markdown", "csv", "anki", "json")
            if k not in written
        ]
        results["六种文本格式均可落盘"] = not others
        results["PDF 要么落盘、要么明确说明原因"] = "pdf" in written or bool(pdf_note)

        # 单词表是这一轮新增的那一种，光「落了盘」说明不了它是不是真的能导回来。
        # 这里验两件界面路径才会出错的事：文件里没有能被导入侧当成词条的标题，
        # 以及每行只有两列（多一列就说明有人把音标或搭配塞了进来）。
        wl_path = out_dir / "probe_wordlist.txt"
        if wl_path.exists():
            wl_text = wl_path.read_text(encoding="utf-8")
            results["单词表文件无表头表尾"] = not any(
                marker in wl_text
                for marker in ("导出时间", "Generated by", "==", "----")
            )
            results["单词表每行最多两列"] = all(
                len(line.split("\t")) <= 2
                for line in wl_text.splitlines()
                if line.strip()
            )
            # 有这个字符串就只能来自真实数据（导出前库里查过这个词），
            # 手写的样例碰不巧带上它。
            results["单词表带着真实词条与中文释义"] = "\t" in wl_text
        print(f"  INFO  导出落盘：成功={written} pdf说明={pdf_note or '(无)'}", flush=True)

        app.quit()

    def polish_check() -> None:
        """界面层级的样式接线：工具栏三级、chip 等宽、底部操作收拢、下拉框宽度。

        这些都是「样式表挂对了没有」的问题——样式错了不会报任何错，只是界面
        看起来仍然平、仍然参差。所以得把 objectName 与几何量读出来断言。
        """
        results["工具栏层级"] = (
            window.btn_query.objectName(),
            window.btn_import.objectName(),
            window.btn_export.objectName(),
            window.btn_format_help.objectName(),
            window.btn_backup.objectName(),
            window.btn_settings.objectName(),
        )
        results["排序下拉框宽度"] = window.order.width()

        # 同义语 chip 等宽。用一个自带三个长短不一的同义词的词条来验——
        # 等宽是从这批词里派生的，用固定的一两个词量不出「派生」这件事。
        window.detail.show_entry(
            Entry(
                lemma="chiptest",
                definition_en="probe",
                definition_zh="探针",
                synonyms=[
                    Synonym(word="pervasive", gloss_zh="弥漫的"),
                    Synonym(word="omnipresent", gloss_zh="无所不在的"),
                    Synonym(word="universal", gloss_zh="普遍的"),
                ],
            )
        )
        app.processEvents()
        body = window.detail.scroll.widget()
        if body is not None and body.layout() is not None:
            body.layout().activate()
        app.processEvents()
        chips = [
            button
            for button in (body.findChildren(QPushButton) if body else [])
            if button.objectName() == "chip"
        ]
        results["同义语 chip 等宽"] = {
            "数量": len(chips),
            "最小宽度": sorted({chip.minimumWidth() for chip in chips}),
            "实际宽度": sorted({chip.width() for chip in chips}),
            "自然宽度上限": max((chip.sizeHint().width() for chip in chips), default=0),
        }

        actions = window.detail._actions_layout
        order: list[str] = []
        if actions is not None:
            for index in range(actions.count()):
                item = actions.itemAt(index)
                widget = item.widget()
                if item.spacerItem() is not None:
                    order.append("弹簧")
                elif widget is not None:
                    order.append(widget.text())
        results["详情区底部操作顺序"] = order

        # 每个操作按钮的 `role` 动态属性。删除是破坏性动作，靠这个属性挂悬停
        # 警示色（`QPushButton#bare[role="destructive"]:hover`）。
        #
        # **只断言样式表里有那条规则是不够的**——规则在、按钮却没挂属性，界面
        # 上就是「点得到、但没有任何风险提示」，而且整条链路上没有任何东西会
        # 报错。这条观测是变异测试逼出来的：把 `setProperty("role", ...)` 整行
        # 删掉，此前全部测试依然全绿（那条自检只查了 CSS 文本，没查控件）。
        roles: dict[str, str] = {}
        if actions is not None:
            for index in range(actions.count()):
                widget = actions.itemAt(index).widget()
                if widget is not None:
                    roles[widget.text()] = str(widget.property("role") or "")
        results["详情区操作的角色标记"] = roles

        # chip 与视图切换的**实际底色**。此前这类「样式对不对」只能靠文本检查
        # （样式表里有没有那条规则），而 `QLabel#chip` 恰恰是一条约等于死规则的
        # 合规文本——它挡不住任何东西。真判据只有画出来的像素。
        #
        # 取样点取左侧内边距区（x=6）：chip 的左右内边距是 11，文字从 11px 起，
        # 所以 6 一定落在底色上；纵向取中线，避开 9px 圆角裁剪的角落。
        def bg_at(widget, x: int = 6) -> str:
            image = widget.grab().toImage()
            yy = max(1, image.height() // 2)
            return image.pixelColor(min(x, image.width() - 1), yy).name()

        results["同义语 chip 底色"] = bg_at(chips[0]) if chips else ""
        # 选中态与未选中态**各读一次**：只读一条的话，「两个长得一模一样」这个
        # 缺陷（加 `:checked` 之前的真实状态）照样能通过。
        window.btn_mode_words.setChecked(True)
        window.btn_mode_phrases.setChecked(False)
        app.processEvents()
        results["视图切换选中态底色"] = bg_at(window.btn_mode_words)
        results["视图切换未选中态底色"] = bg_at(window.btn_mode_phrases)

        print(
            f"  INFO  chip={results['同义语 chip 等宽']} 操作顺序={order} 角色={roles}",
            flush=True,
        )

    def background_check() -> None:
        """自定义背景图：从配置一路验到屏幕像素。

        为什么要读像素：`config` 里有路径、`BackgroundRenderer` 也认得那路径，
        这两件事都对，图片仍然可能**根本没画到屏幕上**（被窗口自己的底色盖住、
        或者 paintEvent 里提前返回）。只有抓一张真实的窗口位图才能证明它到位了。
        """
        home = Path(os.environ["QUILL_HOME"])
        solid = QImage(200, 120, QImage.Format.Format_RGB32)
        solid.fill(QColor(255, 0, 255))
        solid_path = home / "bg_solid.png"
        solid.save(str(solid_path))
        checker_path = home / "bg_checker.png"
        # 64px 的格子：格子太小的话，把图缩放到窗口尺寸这一步本身就是一次平滑，
        # 硬边先被缩放抹钝，模糊前后的差别就缩到判不出来了。
        _checkerboard(640, 64).save(str(checker_path))

        def apply(image: str, blur: bool, veil: str | None = None) -> None:
            window.config["background_image"] = image
            window.config["background_blur"] = blur
            if veil is not None:
                window.config["background_veil"] = veil
            window._apply_background()
            window.update()
            app.processEvents()

        def sample(x: int, y: int) -> tuple:
            return window.grab().toImage().pixelColor(x, y).getRgb()[:3]

        # 面板上的取样点：按**面板自己的几何**算，不写死坐标——离屏平台的可用
        # 区域只有 800x600，窗口会被钳小，写死的坐标一旦越界读到的是纯黑，
        # 那条断言就变成在验「黑色等于纯白」，报出来的失败还指向错误的方向。
        panel = window.detail.parentWidget()

        def panel_alpha() -> dict:
            """面板解析后的底色与它的 alpha。"""
            color = panel.palette().color(QPalette.ColorRole.Window)
            return {
                "rgb": color.getRgb()[:3],
                "alpha": color.alpha(),
            }

        # ① 没有背景时应当是干净的底色，右侧面板不透明（后面都要与它对比）
        apply("", False)
        results["无背景时窗口底色"] = sample(8, 6)
        results["无背景时面板不透明"] = panel_alpha()
        # 档位取自配置的默认值——属性没设上时这里会是 None，而 None 在样式表里
        # 匹配不到任何一档，面板会退回完全不透明。
        results["背景蒙版档位属性"] = str(window.property("bgVeil"))

        # ② 配一张纯色图：留白处必须真的变成这个颜色，面板必须转为半透明
        apply(str(solid_path), False)
        results["背景图颜色"] = sample(8, 6)
        results["有背景时面板半透明"] = panel_alpha()

        # ③ 棋盘 + 关模糊 → 硬边还在；开模糊 → 硬边被抹开。两条成对。
        apply(str(checker_path), False)
        sharp = _strip_delta(window.grab().toImage(), 6, 20, 520)
        apply(str(checker_path), True)
        blurred = _strip_delta(window.grab().toImage(), 6, 20, 520)
        results["背景图模糊对比"] = {"清晰": sharp, "模糊": blurred}

        # ④ 档位真的接上了样式：换一档，面板解析出来的 alpha 必须跟着变，且
        #    三档互不相同。只断言「有一个半透明的面」是不够的——档位若没接到
        #    样式上（比如属性名写错、选择器少写一段），面板会**一直是同一个
        #    alpha**，看上去像功能正常，实际那个下拉框白做了。
        by_level: dict[str, int] = {}
        for level in ("soft", "medium", "bold"):
            apply(str(solid_path), False, level)
            by_level[level] = panel_alpha()["alpha"]
        results["各档位的面板不透明度"] = by_level

        # ⑤ 清掉背景，窗口必须回到纯底色、属性也要翻回去。
        #    属性不翻回来的话，样式表仍认为「有背景」，底色是透明的，
        #    窗口会直接把没画东西的区域透出去。
        apply("", False)
        results["清除背景后属性"] = bool(window.property("hasBackground"))
        results["清除背景后窗口底色"] = sample(8, 6)
        print(
            f"  INFO  背景：底色={results['无背景时窗口底色']} "
            f"纯色图={results['背景图颜色']} 面板={results['无背景时面板不透明']}"
            f"→{results['有背景时面板半透明']} 硬边 清晰{sharp}/模糊{blurred} "
            f"档位={by_level}",
            flush=True,
        )

    def theme_check() -> None:
        """主题换色 + 把图片拖进窗口当背景。

        为什么必须读像素与控件的解析后颜色：`window._theme` 记对了、样式表也
        重新灌过了，界面仍可能一点没变（某处把颜色写死了、或者重算样式没做）。
        只有抓窗口位图、读控件解析出来的颜色，才能证明「换主题」真的发生了。
        """
        import app.ui.main_window as mw

        def settle() -> None:
            window.update()
            app.processEvents()

        original = window._theme

        # ① 换主题必须体现在**看得见的东西**上：强调色（主按钮的底色）与深色
        #    那套的窗口底色。只验 `window._theme` 记对了是不够的——样式表没重灌、
        #    或某处把颜色写死了，这个字段照样是对的。
        shades: list[tuple] = []
        accents: list[str] = []
        for theme_id in theme_mod.theme_ids():
            window._apply_theme(theme_id)
            settle()
            shades.append(window.grab().toImage().pixelColor(8, 6).getRgb()[:3])
            accents.append(
                window.btn_query.palette().color(QPalette.ColorRole.Button).name()
            )
        results["各主题强调色"] = accents
        results["各主题窗口底色"] = [list(shade) for shade in shades]
        results["浅色主题的窗口底色确实浅"] = shades[0]
        results["深色主题的窗口底色确实深"] = shades[-1]

        # ② 即时预览 / 取消还原，走的是**主窗口真正用的那条回调**
        #    （`_open_settings` 里把 `self._apply_theme` 传进对话框）。
        window._apply_theme("ink")
        settle()
        dlg = SettingsDialog(
            dict(window.config), "", window, on_theme_preview=window._apply_theme
        )
        dlg.theme.setCurrentIndex(dlg.theme.findData("clay"))
        settle()
        results["主题预览即时生效"] = window._theme == "clay"
        dlg.reject()
        settle()
        results["取消设置会还原主题"] = window._theme == original
        dlg.deleteLater()

        # ③ 背景 + 最透档：直接压在面板上的小节文字换成加强色；适中档保持原色。
        #    这一对必须成对存在——只验「最透档换了色」的话，一个把三档全换掉的
        #    实现也能通过，而那会把「淡 + 细」的层次整个压平。
        home = Path(os.environ["QUILL_HOME"])
        board = home / "theme_checker.png"
        _checkerboard(480, 24).save(str(board))

        def panel_text_color(name: str) -> str:
            """读详情面板里某一类小字的**解析后**颜色（离屏平台也解析得出来）。"""
            for label in window.detail.findChildren(QLabel):
                if label.objectName() == name:
                    return label.palette().color(QPalette.ColorRole.WindowText).name()
            return ""

        def apply_bg(veil: str) -> None:
            window.config["background_image"] = str(board)
            window.config["background_blur"] = False
            window.config["background_veil"] = veil
            window._apply_background()
            settle()

        # 详情区要真的有小节标题才读得到颜色。用带同义语的词条——同义语那一段
        # 必然带一个小节标题。`diff_note` 也一起给，下面 ③.5 要读那一行。
        window.detail.show_entry(
            Entry(
                lemma="themecheck",
                definition_en="probe entry for theme check",
                definition_zh="主题检查用的探针词条",
                synonyms=[
                    Synonym(word="pervasive", gloss_zh="弥漫的", diff_note="更强调渗透")
                ],
            )
        )
        settle()

        window._apply_theme("ink")
        apply_bg("bold")
        results["最透档的小节文字色"] = panel_text_color("section")
        apply_bg("medium")
        results["适中档的小节文字保持原色"] = panel_text_color("section")
        window._apply_theme("night")
        apply_bg("bold")
        results["深色主题最透档的小节文字色"] = panel_text_color("section")

        # ③.5 同义语那一行的两段文字**取自调色板**，不是写死的。
        #
        # 这一条是真事逼出来的：那一行原先用富文本写死 `#1F1F1E` / `#8A8A86`
        # ——正是浅色主题的 text / text2。深色主题的卡片底色是 `#24262A`，
        # 中文简释压上去只有 1.09:1，**整行看不见**，而且不报任何错。
        #
        # 观测给的是**逐套主题读到的实际颜色**，判定交给 EXPECT 里的 lambda 去
        # 和调色板对表。逐套都要对——只验一套的话，写死浅色值的实现会在那一套
        # 上恰好蒙对，而在别的套上露馅。
        window.config["background_image"] = ""
        window._apply_background()
        syn_colors: dict[str, tuple[str, str]] = {}
        for theme_id in theme_mod.theme_ids():
            window._apply_theme(theme_id)
            settle()
            syn_colors[theme_id] = (
                panel_text_color("synGloss"),
                panel_text_color("synNote"),
            )
        results["同义语两行取色"] = syn_colors

        # ④ 把图片拖进窗口当背景：先问一句，同意才改配置。
        #    反向在前——不看确认框就直接改配置的实现，必须被第一条挡住。
        real_box = mw.QMessageBox
        asked: list[str] = []

        def _box(answer: object) -> type:
            class _B:
                StandardButton = real_box.StandardButton

                @staticmethod
                def question(*args: object, **_k: object) -> object:
                    asked.append(str(args[1]) if len(args) > 1 else "")
                    return answer

                warning = staticmethod(lambda *a, **k: None)
                information = staticmethod(lambda *a, **k: None)

            return _B

        def drop(target: Path) -> None:
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(target))])
            window.dropEvent(
                QDropEvent(
                    QPointF(10, 10),
                    Qt.DropAction.CopyAction,
                    mime,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )

        window.config["background_image"] = ""
        window.config["background_recent"] = []
        window._apply_background()
        settle()

        mw.QMessageBox = _box(real_box.StandardButton.No)
        try:
            drop(board)
        finally:
            mw.QMessageBox = real_box
        settle()
        results["拖入图片会先问一句"] = "背景" in (asked[0] if asked else "")
        results["拒绝后不改配置"] = window.config.get("background_image") == ""

        mw.QMessageBox = _box(real_box.StandardButton.Yes)
        try:
            drop(board)
        finally:
            mw.QMessageBox = real_box
        settle()
        results["同意后配置指向这张图"] = window.config.get("background_image") == str(board)
        results["同意后记进最近使用"] = str(board) in (
            window.config.get("background_recent") or []
        )
        results["同意后状态栏如实报"] = window.statusBar().currentMessage()

        # 同一张图再拖一次：**「最近使用」是位置，不是流水账**——重复使用同一张图
        # 不该在预览条上占两个位置。
        #
        # 这条是**补出来的**。原先只有上面那条 `in` 判定，而去掉去重之后那张图
        # **照样在列表里**（只是出现了两次），`in` 一样为真——变异测试因此报
        # 「MISSED：最近使用不去重」。不是去重没写，是**判据看不见它**：成员判定
        # 天生分不出「一个」和「两个」。所以这里改成数**个数**。
        mw.QMessageBox = _box(real_box.StandardButton.Yes)
        try:
            drop(board)
        finally:
            mw.QMessageBox = real_box
        settle()
        _recent = list(window.config.get("background_recent") or [])
        results["同一张图重复使用不占两个位置"] = (_recent.count(str(board)), len(_recent))

        # 拖进来的若是生词表，仍然走导入那条路（同一落点按后缀分派，顺序写反了
        # 也不会报错，只会把图片当词表去解析）。
        doc = home / "theme_drop.md"
        doc.write_text("themecheck\n", encoding="utf-8")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(doc))])
        word_event = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        results["拖入词表仍走导入"] = (
            MainWindow._dropped_images(word_event) == []
            and MainWindow._dropped_paths(word_event) == [doc]
        )

        # 清掉这一阶段留下的状态：背景、最近使用、主题都恢复，免得影响后面的阶段。
        window.config["background_image"] = ""
        window._apply_background()
        settle()
        results["清除背景后最近使用仍留着"] = str(board) in (
            window.config.get("background_recent") or []
        )
        window._apply_theme(original)
        settle()
        print(
            f"  INFO  主题底色={results['各主题窗口底色']} "
            f"小节文字 最透={results['最透档的小节文字色']} "
            f"适中={results['适中档的小节文字保持原色']} 夜={results['深色主题最透档的小节文字色']}",
            flush=True,
        )

    def welcome_check() -> None:
        """欢迎页与教程：内容齐全、署名正确、版本落后时弹窗并写回版本号。

        不真正 exec()：exec 是模态阻塞，会把整个冒烟测试挂住。所以内容靠
        「只构造不显示」验证，触发逻辑靠 monkeypatch exec 拦截验证。
        """
        import app.ui.onboarding as ob

        # ① 弹窗内容（只构造，不 exec）
        dlg = ob.OnboardingDialog(window._theme, parent=window)
        results["欢迎弹窗标题"] = dlg.windowTitle()
        results["欢迎弹窗署名"] = ob._MADE_BY
        results["欢迎弹窗功能点数"] = len(ob._FEATURES)
        results["教程步数"] = len(ob._TUTORIAL)
        button_texts = sorted(b.text() for b in dlg.findChildren(QPushButton))
        results["欢迎弹窗按钮"] = button_texts

        dlg.deleteLater()

        # ② 版本落后 → _maybe_show_onboarding 会弹窗，并把版本号写回。
        original_exec = ob.OnboardingDialog.exec

        def fake_exec(self):  # noqa: ANN001
            called.append(True)
            return 0

        called: list[bool] = []
        ob.OnboardingDialog.exec = fake_exec
        window.config["onboard_version"] = 0
        try:
            window._maybe_show_onboarding()
        finally:
            ob.OnboardingDialog.exec = original_exec
        results["欢迎弹窗被触发"] = bool(called)
        results["版本号已写回"] = (
            window.config.get("onboard_version") == ob.CURRENT_ONBOARD_VERSION
        )

        # ③ 版本已最新 → 不再弹。
        called_again: list[bool] = []
        ob.OnboardingDialog.exec = lambda self: (called_again.append(True), 0)[1]
        try:
            window._maybe_show_onboarding()
        finally:
            ob.OnboardingDialog.exec = original_exec
        results["版本已最新不再弹"] = not called_again

        print(
            f"  INFO  欢迎弹窗按钮={button_texts} 署名={ob._MADE_BY} "
            f"功能点={len(ob._FEATURES)} 教程步数={len(ob._TUTORIAL)}",
            flush=True,
        )

    def analytics_check() -> None:
        """统计告知页：两页的先后、表态才写配置、关掉不算同意、未勾选拒绝提交。

        与 welcome_check 同一个原则：对话框只构造不 exec（exec 是模态阻塞）。
        这里额外要证明的是「写进文件了」，所以核对读的是磁盘上的配置，而不是内存
        里那份 dict——`_maybe_show_consent` 同时改内存和文件，只看内存分不出
        「存了」和「没存」。
        """
        import app.config as config_mod
        import app.ui.consent as cs
        import app.ui.onboarding as ob
        import app.ui.settings as sm
        from app.ui.settings import SettingsDialog

        # ① 告知页的内容与默认态。默认必须是「没表态」，不是「关」——
        #    这两件事在配置里是两个字段，混成一个的结果是拒绝之后每次启动都再问。
        dlg = cs.ConsentDialog(window._theme, parent=window)
        results["告知页标题"] = dlg.windowTitle()
        results["告知页按钮"] = sorted(b.text() for b in dlg.findChildren(QPushButton))
        results["告知页默认没表态"] = dlg.choice is None

        # 点哪个按钮就记哪个值。打开弹窗本身不产生任何表态。
        agree = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "知道了，继续使用"
        )
        agree.click()
        results["点「知道了」记为同意"] = dlg.choice is True
        dlg.deleteLater()

        dlg_no = cs.ConsentDialog(window._theme, parent=window)
        decline = next(
            b for b in dlg_no.findChildren(QPushButton) if b.text() == "暂不参与"
        )
        decline.click()
        results["点「暂不参与」记为拒绝"] = dlg_no.choice is False
        dlg_no.deleteLater()

        # 设置里的「隐私说明」复用同一个对话框，但不给选择按钮：
        # 那里是复习说明，不该顺手产生一次新的表态。
        review = cs.ConsentDialog(window._theme, parent=window, review_only=True)
        results["只看说明时没有选择按钮"] = sorted(
            b.text() for b in review.findChildren(QPushButton)
        ) == ["关闭"]
        results["只看说明不改表态"] = review.choice is None
        review.deleteLater()

        # ② 首启两页的顺序。两个都是模态框，反序或并行都会把后一个压在底下：
        #    用户关掉上面那个才发现下面还有一个，离屏环境下则是直接卡死在 exec()。
        order: list[str] = []
        ob_orig, cs_orig = ob.OnboardingDialog.exec, cs.ConsentDialog.exec
        ob.OnboardingDialog.exec = lambda self: (order.append("onboarding"), 0)[1]
        cs.ConsentDialog.exec = lambda self: (order.append("consent"), 0)[1]
        saved = dict(window.config)
        try:
            window.config["onboard_version"] = 0
            window.config["analytics_consent_decided"] = False
            window._maybe_show_first_run()
        finally:
            ob.OnboardingDialog.exec = ob_orig
            cs.ConsentDialog.exec = cs_orig
        results["首启先欢迎后告知"] = list(order)

        # ③ 明确拒绝 → 开关与「已表态」都真的落到文件里。
        #    基线刻意设成「开 + 未表态」且与期望值相反，这样两个方向都能失败。
        #    **内存那一份也要一起重置**：`_maybe_show_consent` 先看 `self.config`，
        #    只改磁盘的话它会在开头就 return，接下来「什么都没写」会被判成「没写错」。
        config_mod.save_config(
            {**config_mod.load_config(), "analytics_enabled": True,
             "analytics_consent_decided": False}
        )
        window.config["analytics_enabled"] = True
        window.config["analytics_consent_decided"] = False

        def _decline(self):  # noqa: ANN001
            self.choice = False
            return 0

        cs.ConsentDialog.exec = _decline
        try:
            window._maybe_show_consent()
        finally:
            cs.ConsentDialog.exec = cs_orig
        disk = config_mod.load_config()
        results["拒绝后开关与表态都落盘"] = (
            disk.get("analytics_enabled") is False
            and disk.get("analytics_consent_decided") is True
        )

        # ④ 关掉窗口（Esc / 右上角）不算同意，也不算拒绝：配置一个字都不写。
        #    基线设成「开 + 未表态」，若代码把 None 当成某种表态，两半都会变。
        config_mod.save_config(
            {**config_mod.load_config(), "analytics_enabled": True,
             "analytics_consent_decided": False}
        )
        window.config["analytics_enabled"] = True
        window.config["analytics_consent_decided"] = False
        cs.ConsentDialog.exec = lambda self: 0
        try:
            window._maybe_show_consent()
        finally:
            cs.ConsentDialog.exec = cs_orig
        disk = config_mod.load_config()
        results["关掉告知页不算同意"] = (
            disk.get("analytics_consent_decided") is False
            and disk.get("analytics_enabled") is True
        )

        # ⑤ 已表态就不再弹。
        asked_again: list[bool] = []
        cs.ConsentDialog.exec = lambda self: (asked_again.append(True), 0)[1]
        window.config["analytics_consent_decided"] = True
        try:
            window._maybe_show_consent()
        finally:
            cs.ConsentDialog.exec = cs_orig
        results["已表态不再弹"] = not asked_again

        # ⑥ 记录与提交是两个决定：没勾选记录时，「提交报告」直接拒绝，
        #    连报告都不生成（判定用 build_report 的调用次数，不看文案）。
        sd = SettingsDialog({**window.config, "analytics_enabled": False}, "", window)
        said: list[str] = []
        built: list[int] = []
        real_box_s = sm.QMessageBox
        real_build = sm.report.build_report

        class _Box:
            StandardButton = real_box_s.StandardButton

            @staticmethod
            def information(parent, title, text, *a, **k):  # noqa: ANN001
                said.append(text)
                return _Box.StandardButton.Ok

        sm.QMessageBox = _Box
        sm.report.build_report = lambda *a, **k: (built.append(1), {})[1]
        try:
            sd.analytics.setChecked(False)
            sd._submit_report()
        finally:
            sm.QMessageBox = real_box_s
            sm.report.build_report = real_build
        results["未勾选记录时拒绝提交报告"] = (len(built), bool(said))
        sd.deleteLater()

        # 收尾：把配置恢复成「已表态、关着」，与 main() 开头的基线一致，
        # 后面场景埋点时不会因为上一段测试的残留而改写统计库。
        window.config["analytics_enabled"] = False
        window.config["analytics_consent_decided"] = True
        window.config["onboard_version"] = saved.get("onboard_version", 0)
        config_mod.save_config(window.config)

    def review_check() -> None:
        """复习模式：待复习入口的计数、会话的翻面与四档评分、评分落库推进状态。

        与 welcome_check 同一个原则：ReviewDialog 只构造不 exec（exec 是模态阻塞）。
        评分走真实仓储（apply_review），所以「点评分」这一下真的改库、真的推进 SM-2，
        而不是只看按钮存不存在。
        """
        import app.ui.main_window as mw
        import app.ui.review_dialog as rd
        from app.core import review as review_mod

        # ① 待复习按钮：生词本可见、词组隐藏；新词自动进入复习计划
        window._switch_mode(mw.MODE_WORDS)
        window._reload_list()
        app.processEvents()
        results["待复习按钮在生词本模式可见"] = window.btn_review.isVisible()
        due_before = repo.due_count(review_mod.now_timestamp())
        probe = repo.upsert(Entry(lemma="reviewprobe", definition_zh="复习探针"))
        window._reload_list()
        app.processEvents()
        results["新词进入复习计划（待复习数 +1）"] = (
            repo.due_count(review_mod.now_timestamp()) == due_before + 1
        )
        results["待复习按钮显示计数"] = window.btn_review.text()
        window._switch_mode(mw.MODE_PHRASES)
        app.processEvents()
        results["词组模式隐藏待复习按钮"] = not window.btn_review.isVisible()
        window._switch_mode(mw.MODE_WORDS)
        app.processEvents()

        # ② 复习会话：正面 → 翻面 → 评分 → 推进，全部走真仓储
        dlg = rd.ReviewDialog(repo, window._theme, parent=window)
        results["复习会话能取到到期词"] = dlg.total >= 1
        first = dlg.current_entry
        results["复习会话正面先看词不翻面"] = (
            not dlg.revealed and first is not None and bool(first.lemma)
        )
        dlg._reveal()
        results["复习会话翻面后给四档评分"] = [b.text() for b in dlg._grade_buttons]
        before_id = int(first.id) if first is not None else None
        before_count = (
            repo.get_by_id(before_id).review_count if before_id is not None else None
        )
        dlg._grade(review_mod.REMEMBERED)
        after = repo.get_by_id(before_id) if before_id is not None else None
        results["复习评分推进了记忆状态"] = bool(
            after is not None
            and before_count is not None
            and after.review_count == before_count + 1
        )
        results["复习会话评完推进或结束"] = (
            (dlg.current_entry is not None and dlg.graded == 1)
            or (dlg.current_entry is None and dlg.graded == dlg.total)
        )
        dlg.deleteLater()

        print(
            f"  INFO  复习：按钮={results['待复习按钮显示计数']} "
            f"会话总数={dlg.total} 评分推进={results['复习评分推进了记忆状态']}",
            flush=True,
        )

    def new_features_check() -> None:
        """本轮新增的界面能力：标签入口与筛选、批量查词、撤销删除、测试连接、多文件拖入。

        这一批的共同失败模式是「接线断了也不报错」——标签框改了不落库、筛选
        下拉列出了标签但点了不过滤、撤销按钮亮着但点了不恢复、测试连接只回
        一句写死的话、拖入多个文件时只留第一个。所以每一项都走真控件读真库，
        而不是只看函数存不存在。
        """
        import app.ui.main_window as mw
        from app.ui.settings import SettingsDialog, probe_config

        def _pump(ms: int) -> None:
            deadline = time.monotonic() + ms / 1000
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

        home = Path(os.environ["QUILL_HOME"])

        # 阶段标记不是调试残留：离屏会话里一旦弹出真的模态框，脚本会静止且
        # 没有任何输出（实测卡过一次，9 分钟才被外部超时发现）。留着它，
        # 下次卡住能一眼看出停在哪一段。
        print("  INFO  阶段 ① 标签落库", flush=True)
        # ---------- ① 标签：详情区输入框 → 落库（并归一成存储形态） ----------
        # 自己种一条：不从「列表里恰好有什么」碰运气，场景才有确定性。
        probe = repo.upsert(
            Entry(
                lemma="tagprobe",
                definition_en="probe entry for tag editing",
                definition_zh="标签编辑探针",
            )
        )
        window._reload_list(select_id=probe.id)
        _pump(120)
        editor = window.detail._tag_edit
        results["标签输入框已出现在详情区"] = editor is not None
        # 故意用「逗号 + 连续空格」：落库侧必须把它归一成单空格。不归一的话，
        # 「按标签筛选」只能退化成子串匹配，而子串匹配一定会在「某个标签是
        # 另一个的前缀」时出错（筛「考研」带出「考研英语」）。
        if editor is not None:
            editor.setText("回归练习,   高频")
            # 不直接调 `_flush_edits()`：这条要验的正是**防抖定时器**接上了，
            # 直接调等于绕过那条线，只有真把事件泵过 600ms 才算数。
            _pump(1100)
        stored = repo.get_by_id(int(probe.id))
        results["标签改动会落库（走防抖定时器）"] = bool(
            stored is not None and stored.tags == "回归练习 高频"
        )

        print("  INFO  阶段 ② 标签筛选", flush=True)
        # ---------- ② 标签筛选：下拉里列出库里的标签，选了就收窄 ----------
        window._refresh_tag_filter()
        app.processEvents()
        results["标签筛选下拉列出了库里的标签"] = [
            window.tag_filter.itemData(i) for i in range(window.tag_filter.count())
        ]
        results["标签筛选那一行只在有标签时出现"] = not window.tag_row.isHidden()
        window.tag_filter.setCurrentIndex(window.tag_filter.findData("高频"))
        app.processEvents()
        shown = window._current_entries()
        results["选了标签后列表只剩带该标签的词条"] = bool(shown) and all(
            "高频" in e.tag_list for e in shown
        )
        results["标签筛选确实作用于列表"] = window.list.count() == len(shown)

        # 「标签」与「搜索」是**与**的关系，不是谁把谁顶掉。为了这条断言真的有
        # 分辨力，查询词取自一条**没有**这个标签的词条：交集应当为空，而「顶掉」
        # 或「并起来」都会给出非空结果。
        other = next(
            (e for e in repo.list_entries(limit=None) if "高频" not in e.tag_list), None
        )
        if other is not None:
            window.filter.setText(other.lemma)
            _pump(700)
            both_sides_have_hits = bool(repo.search(other.lemma)) and bool(
                repo.list_by_tag("高频")
            )
            results["标签与搜索取交集（不是谁顶掉谁）"] = (
                both_sides_have_hits and window.list.count() == 0
            )
        window.filter.clear()
        _pump(700)
        window.tag_filter.setCurrentIndex(0)
        app.processEvents()
        results["清掉标签筛选后回到全部"] = (
            window._tag_filter == "" and window.list.count() == repo.count()
        )

        print("  INFO  阶段 ③ 批量查词", flush=True)
        # ---------- ③ 批量查词：一串词走导入链路，整串本身不当成一个词条 ----------
        window.service.set_provider(PickyProvider())
        batch_before = repo.count()
        window.input.setText("multecho multfoxtrot sharedword")
        window._do_lookup()
        deadline = time.monotonic() + 6
        while window._import_progress is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        results["批量查词把整串拆开收录"] = repo.count() - batch_before == 3
        # 关键的反面：此前这里把整串当成一个词去查，于是「粘 10 个词」得到的是
        # 一条 lemma 为整串的记录——不报错、不提示，用户以为 10 个词都收进去了。
        results["批量查词没有把整串当成一个词条"] = repo.get(
            "multecho multfoxtrot sharedword"
        ) is None
        results["批量查词后详情区切到新收的词"] = (
            window.detail._entry.lemma if window.detail._entry else None
        )
        results["批量查词的报告口径不是「导入」"] = window.statusBar().currentMessage()

        print("  INFO  阶段 ③b 一键重查", flush=True)
        # ---------- ③b 一键重查：成功的换掉、兜底的保持原样 ----------
        #
        # 这是**覆盖写**。不拦兜底内容的话，一次网络抖动就能把已经拿到的接口
        # 内容换成离线内容，而且不可撤销。所以两条相反的结果必须同时验到：
        # 只验「更新了」的话，把 `accept_fallback=False` 拿掉照样通过。
        #
        # 范围用筛选限定到 rr*：既避免把前面场景的数据一起改掉，也正好验了
        # 「重查范围跟随当前筛选」这条设计。
        import app.ui.main_window as mw

        fallback_provider = FallbackingProvider({"rrfallback"})
        window.service.set_provider(fallback_provider)
        # rrfresh 走一次真查询：它要带着**当前**提示词版本入库，才能验
        # 「已经是接口内容的不该被标成待更新」。
        window.service.lookup("rrfresh")
        repo.upsert(
            Entry(
                lemma="rrgood",
                definition_en="旧内容",
                definition_zh="旧内容",
                source="offline",
                notes="我的备注",
                tags="考研",
            )
        )
        repo.upsert(
            Entry(
                lemma="rrfallback",
                definition_en="旧内容",
                definition_zh="该保留",
                source="offline",
            )
        )
        window.filter.setText("rr")
        window._reload_list()
        candidates = {
            word: outdated for _id, word, outdated in window._refresh_candidates()
        }
        results["重查把离线来的内容标为待更新"] = (
            candidates.get("rrgood") is True
            and candidates.get("rrfallback") is True
        )
        results["重查不把已是接口内容的标为待更新"] = (
            candidates.get("rrfresh") is False
        )

        real_refresh_dialog = mw.RefreshDialog
        real_box = mw.QMessageBox
        notices: list[str] = []

        class _AutoRefreshDialog:
            seen: dict = {}

            def __init__(self, *, total, outdated, scope, parent=None):
                _AutoRefreshDialog.seen = {
                    "total": total,
                    "outdated": outdated,
                    "scope": scope,
                }

            def exec(self):  # noqa: A003
                return QDialog.DialogCode.Accepted

            # 走「只重查需要更新的」这一支：它才是接上接口之后默认会走的路，
            # 而且能用「rrfresh 有没有被调用」反过来验过滤真的生效了。
            only_outdated = True

        class _QuietBox:
            StandardButton = real_box.StandardButton

            @staticmethod
            def information(*args, **kwargs):
                notices.append(args[2] if len(args) > 2 else "")

            @staticmethod
            def warning(*args, **kwargs):
                notices.append(args[2] if len(args) > 2 else "")

            @staticmethod
            def question(*args, **kwargs):
                return real_box.StandardButton.Yes

        mw.RefreshDialog = _AutoRefreshDialog
        mw.QMessageBox = _QuietBox
        fallback_provider.calls.clear()
        # **`QMessageBox` 的打桩必须一直留到这一整段结束**：后面那两次「被拒绝」
        # 也走 `QMessageBox.information`，而离屏会话里没人能点掉一个真的模态框
        # ——提前恢复会让脚本永久停在那一句上，且没有任何输出（实测卡过一轮）。
        try:
            window._do_refresh_all()
        finally:
            mw.RefreshDialog = real_refresh_dialog

        deadline = time.monotonic() + 20
        while window._refresh_progress is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

        refreshed = repo.get("rrgood")
        untouched = repo.get("rrfallback")
        results["重查范围跟随筛选"] = _AutoRefreshDialog.seen.get("scope")
        results["重查只对需要更新的给出条数"] = _AutoRefreshDialog.seen.get("outdated")
        results["重查只把需要更新的交给批量"] = window._refresh_total
        results["重查跳过已是接口内容的词条"] = "rrfresh" not in fallback_provider.calls
        results["重查把成功那条换成接口内容"] = bool(
            refreshed is not None and refreshed.definition_en == "fresh rrgood"
        )
        results["重查保留备注与标签"] = bool(
            refreshed is not None
            and refreshed.notes == "我的备注"
            and refreshed.tags == "考研"
        )
        results["重查让兜底的词条保持原样"] = bool(
            untouched is not None and untouched.definition_en == "旧内容"
        )
        results["重查失败清单只含兜底那一条"] = [
            word for word, _why in window._refresh_kept
        ]
        results["重查报告口径"] = window.statusBar().currentMessage()

        # 判据必须看**真实 provider**，不是 config：接口模式下没配密钥时，
        # `build_provider()` 会静默退回离线词库，而 config 里仍写着
        # "openai-compat"。第二条才是真正的反例——按 config 判，它不会被拒绝，
        # 于是一键重查会一路跑完，逐条把接口内容换成离线内容。
        window.service.set_provider(OfflineStubProvider())
        notices.clear()
        window.config["provider"] = "offline"
        window._do_refresh_all()
        blocked_offline = (
            window._refresh_progress is None
            and bool(notices)
            and "离线词库" in notices[0]
        )
        notices.clear()
        window.config["provider"] = "openai-compat"
        window._do_refresh_all()
        results["接口模式但实际用离线词库时重查被拒绝"] = (
            blocked_offline
            and window._refresh_progress is None
            and bool(notices)
            and "离线词库" in notices[0]
        )
        window.config["provider"] = "mock"

        window.filter.clear()
        window._reload_list()
        window.service.set_provider(PickyProvider())

        # 词组模式下重查必须被拒绝：词组是从固定搭配里收下来的，没有接口内容。
        # 按钮本身在这一模式下是隐藏的，所以这条挡的是程序化路径。
        window._switch_mode(mw.MODE_PHRASES)
        notices.clear()
        window._do_refresh_all()
        results["词组模式下重查被拒绝"] = (
            window._refresh_progress is None
            and bool(notices)
            and "词组" in notices[0]
        )
        results["词组模式下重查按钮隐藏"] = not window.btn_refresh_all.isVisible()
        window._switch_mode(mw.MODE_WORDS)
        app.processEvents()
        mw.QMessageBox = real_box

        print("  INFO  阶段 ③c 词组查询", flush=True)
        # ---------- ③c 词组查询：查词组、重复查、单词误入、离线拒绝、导入词组 ----------
        #
        # 词组的整条链路是新的：输入框 → provider.lookup_phrase → phrases 表 →
        # 词组详情。四段里任何一段接错，界面都不会报错——结果是「查不到」，
        # 而用户会把责任算到自己那条词太生僻上。所以每段都读真库、看真控件。
        #
        # 用 MockProvider 的示例搭配当数据（`become ubiquitous` 等），
        # 不另造一套只在测试里存在的词典。
        window.service.set_provider(MockProvider())

        class _PhraseBox:
            StandardButton = real_box.StandardButton

            @staticmethod
            def information(*a, **k):
                notices.append(a[2] if len(a) > 2 else "")

            warning = staticmethod(information)

            @staticmethod
            def question(*a, **k):
                return real_box.StandardButton.Yes

        def _detail_title() -> str | None:
            for label in window.detail.findChildren(QLabel):
                if label.objectName() == "lemma":
                    return label.text()
            return None

        mw.QMessageBox = _PhraseBox
        notices.clear()
        # 先把搜索框的防抖定时器停掉。上一步 `filter.clear()` 会起一个 280ms 的
        # `_reload_list()`（不带 select_id），它会在下面几次 pump 里把列表整份
        # 重建一遍、把刚选中的那行清掉——断言于是读到「没选中」，而实际上选中
        # 与清除都是真的发生过的。这条竞态不影响功能，只让判据失去分辨力。
        window._filter_timer.stop()
        window.filter.clear()
        window._switch_mode(mw.MODE_PHRASES)
        app.processEvents()
        # 同一个输入框在两个模式下查的是两种东西，提示文字必须跟着变：
        # 词组模式里还写着「输入英语单词」，用户输入 take off 被拆开时会
        # 以为是程序把词组拆错了，而不是自己站错了集合。
        results["词组模式下输入框提示改为词组"] = window.input.placeholderText()

        # 每个场景都用**等到条件成立**而不是固定睡够：这一整段插在定时器
        # 编排里（别的检查按绝对时间排在被后面），多睡几秒就会把后面的
        # `collocation_check` / `surface_check` 挤到 `app.quit()` 之后，
        # 那些场景随即变成「未采集到该观测」——看起来像它们坏了。
        def _wait_until(pred, ms: int = 3000) -> bool:
            deadline = time.monotonic() + ms / 1000
            while time.monotonic() < deadline:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.005)
            return False

        words_before = repo.count()
        phrases_before = repo.count_phrases()
        # 这一节写进去的词组，收尾时全部撤掉（见本节末尾）。记下进这一节时的家底，
        # 收尾核对就比这个数——不写死成 0，否则别的阶段哪天先种一条，失败会报成
        # 「词组查询留下的没清掉」，指向错误的方向。
        phrases_at_entry = phrases_before
        window.input.setText("become ubiquitous")
        window._do_lookup()
        # **等待条件只能读界面，不能读库。** 查询在 worker 线程里跑，而仓储与
        # 主线程共用同一个 sqlite 连接——主线程在 worker 还在写的时候读一次，
        # 直接 `InterfaceError: bad parameter or other API misuse`（实测踩过）。
        # 详情区渲染出来时，worker 早已写完并回到主线程。
        _wait_until(lambda: _detail_title() == "become ubiquitous", 4000)
        stored_phrase = repo.get_phrase("become ubiquitous")
        results["词组查询进词组收藏"] = bool(
            stored_phrase is not None and repo.count_phrases() == phrases_before + 1
        )
        # **生词本一条都不许多**：词组模式下输入 `become ubiquitous`，若被拆成
        # become / ubiquitous 两条生词，界面照样显示「批量查词完成 · 新增 2 条」，
        # 用户看不出这跟他要的那条词组毫无关系。
        results["词组查询不写生词本"] = repo.count() == words_before
        results["词组查询后详情区显示该词组"] = _detail_title()
        results["词组查询后列表选中该条"] = bool(
            window.list.currentItem() is not None
            and stored_phrase is not None
            and window.list.currentItem().data(mw._USER_ROLE) == stored_phrase.id
        )
        results["词组查询状态栏"] = window.statusBar().currentMessage()

        # 重复查：命中库里那条，不再请求接口。判据落在「条数没涨」上，
        # 与详情区那个「收藏」按钮同一个判据。
        window.input.setText("  Become   Ubiquitous ")
        window._do_lookup()
        _wait_until(lambda: "已在词组收藏里" in window.statusBar().currentMessage(), 1500)
        results["重复查词组不重复写库"] = repo.count_phrases() == phrases_before + 1
        results["重复查词组的提示指向已有那条"] = window.statusBar().currentMessage()

        # 单个词误入词组模式：拒绝，并指向生词本。写进去的话，同一个词会在
        # 两个集合里各有一条，删了一边另一边还在，而两边都自称「释义」。
        notices.clear()
        words_before = repo.count()
        phrases_before = repo.count_phrases()
        window.input.setText("ubiquitous")
        window._do_lookup()
        _wait_until(lambda: bool(notices), 1500)
        results["词组模式下单词被拒绝并指路"] = notices[-1] if notices else ""
        results["词组模式下单词没有写进任何集合"] = (
            repo.count() == words_before and repo.count_phrases() == phrases_before
        )

        # 离线来源：结构上不含词组，必须当场拒绝并说明出路，而不是逐条报
        # 「离线词库未收录」——后者会把配置问题伪装成词库容量问题。
        window.service.set_provider(OfflineStubProvider())
        notices.clear()
        window.input.setText("look after")
        window._do_lookup()
        _wait_until(lambda: bool(notices), 1500)
        results["词组模式下离线来源被拒绝"] = bool(
            notices and "离线词库" in notices[-1] and repo.get_phrase("look after") is None
        )
        window.service.set_provider(MockProvider())

        # 导入词组：2–4 个纯英文词的行按词组收，进词组收藏；5 词以上仍跳过。
        phrase_doc = home / "phrase_list.txt"
        phrase_doc.write_text(
            "be ubiquitous in\nlook after this cat now\n", encoding="utf-8"
        )
        phrases_before = repo.count_phrases()
        window._import_path(phrase_doc)
        _wait_until(
            lambda: window._import_progress is None
            and "新增词组" in window.statusBar().currentMessage(),
            6000,
        )
        results["导入词组文件进词组收藏"] = bool(
            repo.get_phrase("be ubiquitous in") is not None
            and repo.count_phrases() == phrases_before + 1
        )
        results["导入词组的报告单列条数"] = window.statusBar().currentMessage()

        # 收尾：把这一节种进去的词组撤掉。**谁种的数据谁收拾。**
        # 后面的 collocation_check 拿「左栏正好列出已收藏的那两条」当判据，
        # 库里的家底一变它就报「词组列表漏了」——而真正的原因是上一阶段留下的
        # 数据。实测踩过：本节的 `become ubiquitous` / `be ubiquitous in` 留在
        # 库里，那边三条断言全红，症状指向「词组模式不刷新列表」，方向完全错。
        # 清完当场核对，别让「没清掉」这件事等到两百行之后才以别的名义爆出来。
        for seeded in ("become ubiquitous", "be ubiquitous in"):
            row = repo.get_phrase(seeded)
            if row is not None and row.id is not None:
                repo.delete_phrase(int(row.id))
        results["词组查询用过的词组已清掉"] = bool(
            repo.get_phrase("become ubiquitous") is None
            and repo.get_phrase("be ubiquitous in") is None
            and repo.count_phrases() == phrases_at_entry
        )

        window._switch_mode(mw.MODE_WORDS)
        app.processEvents()
        window.service.set_provider(PickyProvider())
        mw.QMessageBox = real_box
        results["生词本模式下输入框提示是单词"] = window.input.placeholderText()

        print("  INFO  阶段 ④ 撤销删除", flush=True)
        # ---------- ④ 撤销删除：两个场景，各自有分辨力 ----------
        #
        # 「清空 id」这一步真正起作用的地方是**删除之后用户又把这个词查了回来**
        # 的那种情况：`upsert()` 认的是 lemma 不是 id（自检里有一条断言钉住），
        # 而 `entry.id is None` 决定「同名声词条已存在时备注与标签听谁的」。
        # 撤销是一份删除前的快照，它必须走「回填」那一支，不去盖掉用户在这次
        # 删除之后新写下的东西。
        real_box = mw.QMessageBox

        class _YesBox:
            StandardButton = real_box.StandardButton

            @staticmethod
            def question(*_a: object, **_k: object) -> object:
                return real_box.StandardButton.Yes

            warning = staticmethod(lambda *a, **k: None)
            information = staticmethod(lambda *a, **k: None)

        def _delete_via_ui(entry_id: int) -> None:
            mw.QMessageBox = _YesBox
            try:
                window._delete_entry(entry_id)
            finally:
                mw.QMessageBox = real_box
            app.processEvents()

        # —— 4a：删完之后没有同名声词条，撤销应当把整条原样放回来 ——
        victim = repo.upsert(
            Entry(
                lemma="undotarget",
                definition_en="probe",
                definition_zh="撤销目标",
                notes="删除前写的备注",
                tags="回归练习",
            )
        )
        old_id = int(victim.id)
        _delete_via_ui(old_id)
        results["撤销按钮在删除后出现"] = (
            not window.btn_undo.isHidden() and repo.get_by_id(old_id) is None
        )
        window.btn_undo.click()
        app.processEvents()
        back = repo.get("undotarget")
        results["撤销把词条放回来了（含备注与标签）"] = bool(
            back is not None
            and back.notes == "删除前写的备注"
            and "回归练习" in back.tag_list
        )
        # 行号不复用。这一条本身是**反例**：注释里曾声称「SQLite 会把删掉的
        # 行号分给下一个新词条」，取证后不成立（AUTOINCREMENT 记在 sqlite_sequence
        # 里，删行也不回收）。留着它是为了让下次再有人写那句话时立刻看到反例。
        squatter = repo.upsert(Entry(lemma="idsquat", definition_en="probe"))
        results["删除后新插入的词条拿到的是一个新 id（行号不复用）"] = (
            int(squatter.id) != old_id
        )
        results["撤销用过即失效"] = bool(
            window.btn_undo.isHidden() and window._last_deleted is None
        )

        # —— 4b：删完之后用户又把这个词查了回来、还写了新备注 ——
        # 撤销**不能**把这份新写的备注盖回删除前那一句。
        revive_victim = repo.upsert(
            Entry(
                lemma="revivetarget",
                definition_en="probe",
                definition_zh="撤销目标二",
                notes="删除前写的备注",
                tags="回归练习",
            )
        )
        _delete_via_ui(int(revive_victim.id))
        # 删除之后用户又把这个词收了回来（走「新内容回填」那条路：id 为空，
        # 同 lemma 的行已经没有了，所以是一条全新记录），并在上面写了新备注。
        repo.upsert(
            Entry(
                lemma="revivetarget",
                definition_en="probe again",
                definition_zh="重新收录",
                notes="删除之后新写的备注",
            )
        )
        window.btn_undo.click()
        app.processEvents()
        after = repo.get("revivetarget")
        results["撤销不覆盖删除之后新写的备注"] = bool(
            after is not None and after.notes == "删除之后新写的备注"
        )

        print("  INFO  阶段 ⑤ 测试连接", flush=True)
        # ---------- ⑤ 测试连接 ----------
        # 不通的那条：结论必须是中文，而不是 httpx 的原始异常串。
        probe_note = ""
        try:
            probe_note = probe_config(
                "http://127.0.0.1:1/v1", "probe-model", "sk-probe", 0.6
            )
        except Exception as exc:  # noqa: BLE001 - 失败路径正是这条要验的东西
            probe_note = str(exc)
        results["测试连接：不通时给出中文说明"] = probe_note

        # 通的那条：拿一个只活在本次检查里的 stub 服务端把**成功路径**也走一遍。
        # 只用「不可达地址」验的话，probe 里解析响应那半边一条都不会执行，而
        # 「payload 里到底带了什么」更是没有任何观测——「不带 temperature」
        # 正是为推理型模型（deepseek-reasoner / R1）写下的约束，没有观测它就会
        # 被后来的「统一一下 payload」顺手改掉。
        import json as _json  # noqa: PLC0415
        import threading  # noqa: PLC0415
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        seen_payload: dict = {}

        class _Stub(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - 与 HTTP 动词对齐
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                try:
                    seen_payload.update(_json.loads(body or b"{}"))
                except ValueError:
                    seen_payload["__unparsable__"] = True
                seen_payload["__auth__"] = self.headers.get("Authorization") or ""
                payload = _json.dumps(
                    {"choices": [{"message": {"content": "pong"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_a: object) -> None:
                """别把每次请求打到测试输出里。"""
                return

        server = HTTPServer(("127.0.0.1", 0), _Stub)
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            ok_note = probe_config(
                f"http://127.0.0.1:{port}/v1", "probe-model", "sk-probe", 5.0
            )
        finally:
            server.shutdown()
            server.server_close()
        results["测试连接：通时给出模型名"] = ok_note
        results["测试连接的请求体不带 temperature（推理型模型会 400）"] = (
            "temperature" not in seen_payload
        )
        results["测试连接只花 1 个 token 且带上密钥"] = (
            seen_payload.get("max_tokens") == 1
            and seen_payload.get("__auth__") == "Bearer sk-probe"
        )

        offline_dlg = SettingsDialog(dict(window.config) | {"provider": "mock"}, "", window)
        try:
            results["非接口模式下测试连接按钮置灰"] = not offline_dlg.btn_test.isEnabled()
        finally:
            offline_dlg.deleteLater()

        print("  INFO  阶段 ⑥ 多文件拖入", flush=True)
        # ---------- ⑥ 一次拖入多个文件：一个都不丢 ----------
        # 用一组**没在别处出现过**的词：第 ③ 步那三个词此刻已经在库里了，
        # 再导一次会被记成「已存在」，「新增 3 条」就变成了「新增 0 条」。
        drop_a = home / "drop_a.txt"
        drop_b = home / "drop_b.txt"
        drop_a.write_text("dropone\ndropshared\n", encoding="utf-8")
        drop_b.write_text("droptwo\ndropshared\n", encoding="utf-8")
        drop_pic = home / "drop_pic.png"
        QImage(60, 40, QImage.Format.Format_RGB32).save(str(drop_pic))

        # QMimeData 必须**活到事件被用完之后**：`QDropEvent` 不持有它的 Python
        # 引用，函数一返回 `mime` 就被回收，`event.mimeData()` 于是给出一个
        # 已经析构的包装对象（读属性时报 "'QObject' object has no attribute
        # 'hasUrls'"——看起来像事件 API 用错了，其实只是生命周期）。
        _keepalive: list = []

        def _drop_event(files: list) -> QDropEvent:
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(f)) for f in files])
            _keepalive.append(mime)
            return QDropEvent(
                QPointF(10, 10),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

        results["拖入多个词表文件全部被收下"] = [
            p.name for p in MainWindow._dropped_paths(_drop_event([drop_a, drop_b]))
        ]
        results["同一个文件拖两次只算一个"] = [
            p.name for p in MainWindow._dropped_paths(_drop_event([drop_a, drop_a]))
        ]
        # 图片与词表**各归各的**：从前图片分支先命中，生词表被整个忽略。
        results["图片与词表混着拖时各归各的"] = (
            [p.name for p in MainWindow._dropped_images(_drop_event([drop_pic, drop_a]))],
            [p.name for p in MainWindow._dropped_paths(_drop_event([drop_pic, drop_a]))],
        )

        # 真跑一遍多文件导入：两份文件合成**一次**核对（拖 3 个文件不该弹 3 次框）。
        asked: list[str] = []

        class _ImportBox:
            StandardButton = real_box.StandardButton

            @staticmethod
            def question(*a: object, **_k: object) -> object:
                asked.append("question")
                return real_box.StandardButton.Yes

            @staticmethod
            def information(*a: object, **_k: object) -> object:
                asked.append("information")
                return real_box.StandardButton.Ok

            warning = staticmethod(lambda *a, **k: None)

        multi_before = repo.count()
        mw.QMessageBox = _ImportBox
        try:
            window._import_paths([drop_a, drop_b])
            _pump(3000)
        finally:
            mw.QMessageBox = real_box
        results["多文件导入只核对一次"] = asked.count("question")
        results["多文件导入两份文件的词都在库里"] = bool(
            repo.get("dropone") is not None and repo.get("droptwo") is not None
        )
        # 同一个词在两份文件里都出现：合并去重后只收一条。
        results["多文件合并时重复词只算一次"] = repo.count() - multi_before

        # 收尾：把标签筛选的下拉重置（阶段之间不留下筛选状态）。
        window._refresh_tag_filter()
        app.processEvents()
        print(
            f"  INFO  新功能：标签={results['标签改动会落库（走防抖定时器）']} "
            f"Batch={results['批量查词把整串拆开收录']} "
            f"撤销={results['撤销不覆盖删除之后新写的备注']} "
            f"probe={results['测试连接：通时给出模型名']} "
            f"多文件={results['多文件导入两份文件的词都在库里']}",
            flush=True,
        )

    def surface_check() -> None:
        """容器底色必须由主题给，不能漏出系统调色板。

        复现的是「浅色主题 + 系统深色」这一组合：把应用调色板换成深色，再看
        详情区那块面板与一个对话框的底色有没有跟着变黑。

        为什么必须**主动**换调色板：样式表只覆盖具名控件，没有背景规则的容器
        会去取 `QPalette::Window` / `Base`——那是**系统**的。跑测试的机器
        palette 未必是深色，不主动换就永远测不出（本机开发机恰好是深色，
        用户也是）。修复前实测：详情区 `#1e1e1e`、对话框 `#1e1e1e`，压在
        上面的却是由主题给的深色字——「一个字看不清」且不报任何错。

        **这里曾经还测第三个成员：分裂条手柄。已经删掉（第 30 轮）。**
        实测它的像素在两个调色板下都是 `#fbfbfa`（窗口底色），把
        `QSplitter::handle { background: transparent; }` 整行抽掉也**读不出差别**
        ——离屏平台上 `QSplitter` 自己也不填背景，中缝漏出来的是窗口（窗口有 QSS
        底色），系统的深色根本到不了那一层。也就是说那条判据**恒为真**，它给的是
        「测过了」的错觉而不是证据。手柄那一处改由 `selfcheck` 盯**模板里规则还在
        不在**（判据落在能失败的那一层），效果本身只在真实桌面 + 系统深色下可见，
        这台机器证不了的事不在这里假装证过。

        面板与对话框这两条是**真的能失败**的（对话框是顶层窗口，自己会填背景；
        对应的变异 `对话框不留底色` 抓得住），所以留着。
        """
        detail = window.detail
        panel = detail.parentWidget()
        original = QApplication.palette()

        def sample(widget) -> str:
            image = widget.grab().toImage()
            return image.pixelColor(image.width() - 40, image.height() - 40).name()

        def dialog_sample() -> str:
            box = QDialog(window)
            box.resize(220, 140)
            box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            box.show()
            try:
                QApplication.processEvents()
                image = box.grab().toImage()
                return image.pixelColor(
                    image.width() - 30, image.height() - 30
                ).name()
            finally:
                box.close()
                box.deleteLater()

        # 详情区切回占位态：内容居中，右下角必定是空白，采到的一定是底色，
        # 而不是某一行文字。
        detail.show_entry(None)
        QApplication.processEvents()
        light_panel = sample(panel)
        light_dialog = dialog_sample()

        dark = QPalette()
        for role, name in (
            (QPalette.ColorRole.Window, "#1e1e1e"),
            (QPalette.ColorRole.Base, "#2d2d2d"),
            (QPalette.ColorRole.WindowText, "#ffffff"),
            (QPalette.ColorRole.Text, "#ffffff"),
        ):
            dark.setColor(role, QColor(name))
        QApplication.setPalette(dark)
        QApplication.processEvents()
        try:
            deep_panel = sample(panel)
            deep_dialog = dialog_sample()
        finally:
            QApplication.setPalette(original)
            QApplication.processEvents()

        results["详情区底色不随系统调色板变黑"] = (
            deep_panel == light_panel and deep_panel != "#1e1e1e"
        )
        results["对话框底色不随系统调色板变黑"] = (
            deep_dialog == light_dialog and deep_dialog != "#1e1e1e"
        )
        print(
            f"  INFO  底色：详情区 {light_panel}→{deep_panel}，"
            f"对话框 {light_dialog}→{deep_dialog}",
            flush=True,
        )

    def collocation_check() -> None:
        """右侧固定搭配区 + 词组收藏（含集合切换、删除与撤销）。

        这一批的共同失败模式同样是「接线断了也不报错」：

        - 搭配区渲染出来了、但收藏按钮发出去的是**空的** `Phrase`（简释与例句
          丢在组装那一步），库里多出一条只有词组文本的记录；
        - 「已收藏」按钮改在了写库**之前**，写失败时界面说收藏成功、库里没有；
        - 切到词组收藏后左栏还按生词本的计数与搜索提示走，数字对不上也不报错；
        - 词组删掉了、撤销放回来的是**空的**壳（快照只记了 id）；
        - 拿词条的 id 当词组 id 用，删掉的是另一张表里的同号数据。

        所以每一项都读真库、点真按钮、看真标签，不看函数在不在。
        """
        import app.ui.main_window as mw
        from app.core.models import Collocation, Phrase

        def _pump(ms: int) -> None:
            deadline = time.monotonic() + ms / 1000
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

        real_box = mw.QMessageBox

        class _YesBox:
            StandardButton = real_box.StandardButton

            @staticmethod
            def question(*_a: object, **_k: object) -> object:
                return real_box.StandardButton.Yes

            warning = staticmethod(lambda *a, **k: None)
            information = staticmethod(lambda *a, **k: None)

        # ---------- ① 右侧固定搭配区 ----------
        # 词条直接种进库：这一节要验的是「渲染与收藏这条链路」，不是某个词库
        # 收没收录某个词。词组文本刻意**含 lemma 本身**（`charge` 出现在
        # `in charge of` / `free of charge` 里），否则「已收藏的含本词的词组」
        # 那一块永远是空的，那条断言就成了空话。
        probe = repo.upsert(
            Entry(
                lemma="charge",
                definition_en="probe entry for collocations",
                definition_zh="搭配探针",
                collocations=[
                    Collocation(
                        phrase="in charge of",
                        gloss_zh="负责",
                        example="She is in charge of sales.",
                        example_zh="她负责销售。",
                    ),
                    Collocation(phrase="free of charge", gloss_zh="免费"),
                ],
            )
        )
        window.service.set_provider(MockProvider())
        window._switch_mode(mw.MODE_WORDS)
        window._reload_list(select_id=probe.id)
        _pump(200)

        results["右侧固定搭配区列出每条搭配"] = [
            lbl.text() for lbl in window.detail.findChildren(QLabel, "collPhrase")
        ]
        results["每条搭配各配一条例句"] = [
            lbl.text() for lbl in window.detail.findChildren(QLabel, "collExample")
        ]
        results["搭配例句的翻译也跟着渲染"] = any(
            "她负责销售" in lbl.text() for lbl in window.detail.findChildren(QLabel)
        )
        results["每条搭配各有一个收藏按钮"] = sorted(window.detail._collect_buttons)

        # ---------- ② 收藏：先落库，再改按钮 ----------
        ph_before = repo.count_phrases()
        window.detail._collect_buttons["in charge of"].click()
        _pump(200)
        results["点收藏把搭配写进词组收藏"] = repo.count_phrases() - ph_before
        saved = repo.get_phrase("in charge of")
        results["收藏下来的词组带着简释、例句与来源"] = (
            (saved.gloss_zh, saved.example, saved.source_lemma) if saved else None
        )
        results["收藏按钮随即变成已收藏"] = window.detail._collect_buttons[
            "in charge of"
        ].text()
        # 已收藏的按钮被禁用：重复收藏在界面上就被挡住，不给用户「点了没反应」的机会。
        results["已收藏的按钮被禁用"] = not window.detail._collect_buttons[
            "in charge of"
        ].isEnabled()
        results["切换按钮上的词组条数跟着涨"] = window.btn_mode_phrases.text()

        # 按钮挡住了重复点击，但**库这一层也必须幂等**——否则界面一旦放开（或换个
        # 入口进来）就会多出第二条，而两次操作都会显示「已收藏」。
        before_again = repo.count_phrases()
        window._on_collocation_collected(
            Phrase(phrase="in charge of", gloss_zh="后来一次的简释")
        )
        _pump(120)
        after_again = repo.get_phrase("in charge of")
        results["重复收藏在库这一层是幂等的（不新增、不覆盖）"] = (
            repo.count_phrases() == before_again
            and bool(after_again)
            and after_again.gloss_zh == "负责"
        )
        results["重复收藏时状态栏说明以先前那条为准"] = (
            window.statusBar().currentMessage()
        )

        # 第二条搭配也收下：含词的词组应当有两条。
        window.detail._collect_buttons["free of charge"].click()
        _pump(200)
        window._show_entry(repo.get("charge"))
        _pump(150)
        results["详情区回显已收藏的含本词的词组"] = (
            window.detail._collected_label.text()
            if window.detail._collected_label is not None
            else None
        )

        # ---------- ③ 集合切换 ----------
        window._switch_mode(mw.MODE_PHRASES)
        _pump(200)
        results["切到词组收藏后左栏列出已收藏的词组"] = sorted(
            e.lemma for e in window._current_entries()
        )
        results["词组模式下计数标签按词组算"] = window.count_label.text()
        results["词组模式下标签筛选那一行藏起来"] = window.tag_row.isHidden()
        results["模式按钮反映当前集合"] = (
            window.btn_mode_words.isChecked(),
            window.btn_mode_phrases.isChecked(),
        )
        results["搜索框提示语随集合变化"] = window.filter.placeholderText()

        # ---------- ④ 词组详情走另一条渲染路径 ----------
        phrase_row = repo.get_phrase("in charge of")
        assert phrase_row is not None and phrase_row.id is not None
        phrase_id = int(phrase_row.id)
        window._reload_list(select_id=phrase_id)
        _pump(200)
        results["选中词组后详情区走词组渲染（来源标成词组收藏）"] = (
            window.detail._source_label.text()
            if window.detail._source_label is not None
            else None
        )
        # 词组详情**不能**建备注与标签输入框：它们发信号时带的是词条的 id，
        # 词组的 id 落进去会指到另一张表里的同号词条，而两边都显示「已保存」。
        results["词组详情里没有备注与标签输入框"] = (
            window.detail._note_edit is None and window.detail._tag_edit is None
        )
        results["词组详情区有移除按钮"] = [
            b.text()
            for b in window.detail.findChildren(QPushButton)
            if "移除" in b.text()
        ]

        # ---------- ⑤ 两张表各自发号：造一次**真实的行号撞车** ----------
        # 只说「删词条之后词组还在」是一句近乎恒真的话——两张表的行号在本次运行
        # 里根本撞不上，那个断言没有任何分辨力。所以这里**先把撞车造出来**：把一条
        # 词组的 id 直接写成某条词条的 id，两张表里于是同时存在同一个数字。
        # 这时「删词条」若顺手按行号去动 phrases，就会静默删掉一条毫不相干的词组。
        collide_entry = repo.upsert(Entry(lemma="collideword", definition_en="probe"))
        collide_id = int(collide_entry.id)
        phrase_id_before = sorted(
            p.id for p in repo.list_phrases() if p.id is not None
        )
        repo.conn.execute(
            "INSERT INTO phrases"
            " (id, phrase, gloss_zh, example, example_zh, source_lemma, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (collide_id, "collide phrase", "", "", "", "", Entry.now()),
        )
        repo.conn.commit()
        results["两张表里同时存在同一个行号（这正是删除必须带类别的原因）"] = (
            repo.get_by_id(collide_id) is not None
            and repo.get_phrase_by_id(collide_id) is not None
        )
        mw.QMessageBox = _YesBox
        try:
            window._delete_entry(collide_id)
        finally:
            mw.QMessageBox = real_box
        _pump(250)
        results["删词条不会连带删掉同号的词组"] = (
            repo.get_by_id(collide_id) is None
            and repo.get_phrase_by_id(collide_id) is not None
            and repo.get_phrase("in charge of") is not None
            and repo.get_phrase("free of charge") is not None
        )
        # 清掉这条合成出来的词组，别让它影响后面的计数。
        repo.delete_phrase(collide_id)
        results["撞车探针用过的词组已清掉"] = (
            repo.get_phrase_by_id(collide_id) is None
            and sorted(p.id for p in repo.list_phrases() if p.id is not None)
            == phrase_id_before
        )

        # ---------- ⑥ 删词组与撤销：快照必须留整条 ----------
        words_before = repo.count()
        window._switch_mode(mw.MODE_PHRASES)
        window._reload_list(select_id=phrase_id)
        _pump(150)
        remove_btn = next(
            (
                b
                for b in window.detail.findChildren(QPushButton)
                if b.text() == "从词组收藏中移除"
            ),
            None,
        )
        mw.QMessageBox = _YesBox
        try:
            if remove_btn is not None:
                remove_btn.click()
            _pump(300)
        finally:
            mw.QMessageBox = real_box
        results["移除按钮真的把词组删掉了"] = repo.get_phrase("in charge of") is None
        # 反过来的那一半：删词组不得动到生词本。同一个行号在 entries 里也是存在的，
        # 按行号去动它就等于删掉一条毫不相干的词条。
        results["移除词组不动生词本"] = (
            repo.count() == words_before and repo.get("collideword") is None
        )
        results["删词组后撤销按钮出现"] = not window.btn_undo.isHidden()
        # 撤销要切回词组收藏这一条，只有「删完之后先跑到生词本」才测得到：就地
        # 撤销时左栏本来就在词组模式，`_undo_delete` 里那行「不在词组模式就切回」
        # 根本没触发，抽掉它测试照样全绿（A32 正是这么漏抓的）。所以这里先切回
        # 生词本、再点撤销，逼出「撤销时按行号选、选错集合」的缺陷。
        window._switch_mode(mw.MODE_WORDS)
        window._reload_list()
        app.processEvents()
        window.btn_undo.click()
        _pump(250)
        revived = repo.get_phrase("in charge of")
        results["撤销把词组连简释与例句一起放回来"] = (
            (revived.gloss_zh, revived.example) if revived else None
        )
        results["撤销词组后左栏切到词组收藏"] = window._mode
        results["撤销用过即失效"] = bool(
            window.btn_undo.isHidden() and window._last_deleted is None
        )

        # ---------- ⑦ 生词本模式查词照常 ----------
        # 这一段原先验的是「在词组模式查词会自动切回生词本」。那个行为已经不
        # 存在了，而且是**刻意**改掉的：词组模式只查词组，输入单个词当场拒绝
        # 并指向生词本（③c 里用同一个词、读数更细地测过：提示文案 + 两个集合
        # 的条数都没动）。替用户静默换集合查词会让「take off 是词组、apple 是
        # 单词」两种语义在同一个输入框里随机切换，用户无从预期。
        #
        # 留下的一半仍然值得守：`_do_lookup()` 现在按模式分派，分派写错（例如
        # 写成 `if self._mode:` 的真值判断）时，**生词本**里查词会走进词组那条
        # 路，用户看到一句「不是词组」，而自己的词再也没有被收进来——症状指向
        # 完全错误的方向。
        #
        # 词是**这一轮才第一次出现**的（`meticulous` 在别处没有任何引用），
        # 否则「库里有这条」恒为真：`ubiquitous` 在 main() 开头就被查过一次，
        # 拿它当判据的断言永远绿。
        assert repo.get("meticulous") is None, "meticulous 不该在本轮此前出现过"
        window._switch_mode(mw.MODE_WORDS)
        window.input.setText("meticulous")
        window._do_lookup()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
            shown = window.detail._entry
            if shown is not None and shown.lemma == "meticulous":
                break
        results["生词本模式查词照常落库"] = repo.get("meticulous") is not None

        # ---------- ⑦b 查词在路上时切到词组收藏：结果回来要把集合切回去 ----------
        # 保护是 `_present_entry()` 开头那句「查词结果属于生词本，先切回去」。
        # 触发它的路径**不再是**「在词组模式输入单词」（那个现在当场被拒绝），
        # 而是竞态：在生词本里查一个词、响应还没回来就点「词组收藏」。
        # 少了这句，右边渲染着刚查到的词、左栏列的全是词组、一条都不选中——
        # 而状态栏说「已收录」，用户以为自己的词丢了。
        #
        # 这条同时替掉了一条已经失去视野的旧断言：`_do_lookup` 在词组模式下
        # 不再走单词查询以后，原来那个「在词组模式查词会自动切回生词本」的入口
        # 就没有了，变异检测随即报 MISSED（保护还在，判据看不见它）。
        #
        # 词用 `meticulous`（MockProvider 的示例词，刚查过所以走缓存，不会有网络
        # 往返）：这里要证的是「回调回来时集合被切回去」，与查到了什么无关。
        # 等待条件读 `_mode` 而**不是**详情区——详情区上一步就已经是这个词了，
        # 拿它当条件会在回调到达之前就跳出循环，断言随即读到「还是词组模式」。
        window._switch_mode(mw.MODE_WORDS)
        window.input.setText("meticulous")
        mw.QMessageBox = _YesBox
        try:
            window._lookup_word("meticulous")  # 只管发出去，不等回调
            window._switch_mode(mw.MODE_PHRASES)  # 同一轮栈里切走，先于回调到达
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and window._mode != mw.MODE_WORDS:
                app.processEvents()
                time.sleep(0.01)
        finally:
            mw.QMessageBox = real_box
        results["查词途中切到词组收藏，结果回来时自动切回生词本"] = window._mode

        # 收尾：回到生词本，清掉搜索框，免得影响后面的导入阶段。
        window.filter.clear()
        window._switch_mode(mw.MODE_WORDS)
        app.processEvents()
        print(
            f"  INFO  搭配：渲染={results['右侧固定搭配区列出每条搭配']} "
            f"收藏={results['点收藏把搭配写进词组收藏']} "
            f"撤销={results['撤销把词组连简释与例句一起放回来']} "
            f"查词回到生词本={results['生词本模式查词照常落库']}",
            flush=True,
        )

    def tray_hotkey_check() -> None:
        """托盘常驻与全局热键（Alt+Q）的界面侧。

        **offscreen 环境没有系统托盘**，真实的 QSystemTrayIcon 建不出来，所以
        这里不测「托盘图标长什么样」，只测那条能读出差别的判断：
        **没有托盘时绝不允许把窗口藏起来。**

        这不是形式主义。窗口藏进一个不存在的托盘之后，进程还活着、数据库锁还
        占着，而桌面上什么都没有；再双击 exe 只会被告知「已在运行」。一个再也
        打不开的常驻进程，比没有托盘功能糟得多。
        """
        # 各阶段各自 import，不用模块级别名：这里的 `mw` 只在本函数作用域内
        # 存在，漏写一行就是 NameError，而它会把整轮冒烟从这一阶段起全部带走。
        import app.ui.main_window as mw

        results["没有托盘时不建托盘图标"] = window._tray is None
        results["没有托盘时关闭窗口不收进托盘"] = window._should_hide_to_tray() is False

        # 有托盘时允许收起来。托盘图标在 offscreen 下造不出来，用一个只提供
        # `isVisible()` 的最小替身：这条判据读的是我们自己的三条件，而不是托盘
        # 本身的行为，所以替身不会让它变成恒为真——下面三条反向断言就是证据。
        class _TrayStub:
            def isVisible(self) -> bool:  # noqa: N802 - 模仿 Qt 命名
                return True

        real_tray = window._tray
        window._tray = _TrayStub()
        results["有托盘时关闭窗口收进托盘"] = window._should_hide_to_tray() is True
        window._quitting = True
        results["托盘里的退出是真的退出"] = window._should_hide_to_tray() is False
        window._quitting = False
        window.config["tray_enabled"] = False
        results["设置里关掉托盘后关闭即退出"] = window._should_hide_to_tray() is False
        window.config["tray_enabled"] = True
        window._tray = real_tray

        # 热键预填。剪贴板里的词组要落到词组模式：留在生词本模式的话，回车会把
        # 它拆成两个词分别去查，而用户想查的是那一条词组。
        original = QApplication.clipboard().text()
        window._switch_mode(mw.MODE_WORDS)
        QApplication.clipboard().setText("in charge of")
        window._on_hotkey()
        results["热键预填把词组填进输入框"] = window.input.text()
        results["热键预填切到词组收藏"] = window._mode

        # 整句不预填：填进去用户先要删一遍，比不填更烦。
        QApplication.clipboard().setText("She walked into the room slowly")
        window.input.setText("")
        window._on_hotkey()
        results["热键不预填整句"] = window.input.text()
        QApplication.clipboard().setText(original)

        window.input.clear()
        window._switch_mode(mw.MODE_WORDS)
        print(
            f"  INFO  托盘/热键：收进托盘={results['有托盘时关闭窗口收进托盘']} "
            f"预填={results['热键预填把词组填进输入框']!r} "
            f"整句={results['热键不预填整句']!r}",
            flush=True,
        )

    # 阶段串行的闸门。见 _guard 里的说明——阶段内部会 processEvents()，
    # 那会把**已经到期的定时器**一并派发，于是后一个阶段在前一个阶段内部重入。
    running = {"active": False}

    def _guard(stage):
        """把每个阶段包起来：抛异常也要变成一条显式失败并收尾。

        不加这一层，定时器回调里的异常只会被 Qt 打印到 stderr 然后继续——没人
        调 `app.quit()`，进程就挂到外层超时。而「卡住」看起来像环境抽风，
        不像「我这处改动写错了」。宁可报一条刺眼的失败。

        **串行闸门也不是可选项。** 阶段内部会调 `app.processEvents()`，而它会把
        已经到期的定时器一起派发出去：后一个阶段于是跑在前一个阶段**内部**。
        实测踩过——背景阶段要跑 8 秒（逐像素统计拖的），而下一个阶段只隔 300ms，
        结果「换主题」的观测全在背景图还没清掉的状态下采集，六套主题的底色读出来
        全是那张测试图的洋红，看起来像「主题换色没生效」。**这种失败指向的方向
        完全是错的**，比测试跑得慢贵得多。后来的阶段改为排队重试，不插队。
        """

        def _run() -> None:
            if running["active"]:
                QTimer.singleShot(60, _guard(stage))
                return
            running["active"] = True
            try:
                stage()
            except Exception as exc:  # noqa: BLE001 - 这里就是要兜住一切，见上
                results["测试内部异常"] = f"{type(exc).__name__}: {exc}"
                app.quit()
            finally:
                running["active"] = False

        return _run

    # 先立下「没异常」的默认值：这条判定项若压根没被观测到，判定循环会按
    # 「有判定标准却没有观测到」判失败，届时失败得莫名其妙。
    results["测试内部异常"] = "(无异常)"

    # 整轮兜底超时。**没有它，「卡住」只表现为「慢」**：某个阶段弹出的模态框在
    # offscreen 下没人能点，nested event loop 就停在那儿，后面的阶段一个都不触发，
    # 日志停在半句上（实测卡过 9 分钟、也卡过 2h34m）。看日志的人分不清是环境
    # 抽风还是自己改错了——而这正是不该让测试自己承担的判断。
    # 顺带把栈打出来：卡住的调用栈比「耗时 4 分钟」有用得多。
    def _watchdog() -> None:
        stuck = QApplication.activeModalWidget()
        if stuck is not None:
            # nested exec() 不响应 app.quit()，得让模态框自己退出去，外层循环
            # 才回得来。这里只是兜底，真正的修法永远是「别让它弹出来」。
            stuck.reject()
        results["测试内部异常"] = (
            f"整轮超时（{WATCHDOG_MS // 1000}s）：有阶段卡住了"
            + ("（当时有模态框）" if stuck is not None else "")
        )
        print("  INFO  看门狗触发，打印当前调用栈：", flush=True)
        faulthandler.dump_traceback()
        app.quit()
        # 模态框刚被 reject、外层循环可能还没回到，再补一次退出。
        QTimer.singleShot(200, app.quit)

    QTimer.singleShot(WATCHDOG_MS, _watchdog)
    QTimer.singleShot(300, _guard(do_lookup))
    QTimer.singleShot(1300, _guard(verify))
    QTimer.singleShot(2600, _guard(finalize))
    QTimer.singleShot(4200, _guard(check_refresh))
    QTimer.singleShot(5600, _guard(race_check))
    QTimer.singleShot(6400, _guard(fallback_check))
    QTimer.singleShot(8000, _guard(card_check))
    QTimer.singleShot(9500, _guard(polish_check))
    QTimer.singleShot(9800, _guard(background_check))
    QTimer.singleShot(10100, _guard(theme_check))
    QTimer.singleShot(10300, _guard(welcome_check))
    QTimer.singleShot(10400, _guard(review_check))
    # 告知页排在 review 之后、new_features 之前。它会改写磁盘上的配置来造
    # 「未表态」的基线，收尾时又恢复成「已表态、关着」——放在靠后的位置可以
    # 少牵连别的阶段读到的配置。
    QTimer.singleShot(10450, _guard(analytics_check))
    # 这一阶段自己带一个 stub 服务端并真发一次请求，加上几处防抖等待，
    # 整体明显比别的阶段长；导入阶段因此往后让出足够的时间窗。
    QTimer.singleShot(10600, _guard(new_features_check))
    # 托盘与热键排在 new_features 之后、collocation（14000）之前：它要用同一个
    # 活着的窗口，而 collocation 会重排列表与模式，放在它后面读到的就不是这里
    # 造的状态了。闸门会在前一个阶段未结束时重试，不会插队。
    QTimer.singleShot(11300, _guard(tray_hotkey_check))
    # 排在 new_features 之后：那一阶段自己带一个 stub 服务端、还要等一段多文件
    # 导入，比别的阶段都长。串行闸门会兜住「前一个还没跑完」的情况——这里给的是
    # 一个尽量晚的起点，不等于硬顺序。
    QTimer.singleShot(14000, _guard(collocation_check))
    # 底座场景排在最后一批：它会临时把应用调色板换成深色再换回来，必须独占
    # 一个没有别的阶段在跑的时间窗（串行闸门能保证不并发，这里给的是足够晚
    # 的起点）。放在 import_check（26000 且会 quit）之前。
    QTimer.singleShot(25000, _guard(surface_check))
    # 导入是最后一个阶段，它在结尾 app.quit()。串行闸门靠「60ms 一次」的重试排队，
    # 无法保证「14000ms 的 collocation 一定比 17000ms 的 import 先拿到执行权」——
    # 一旦 import 抢先在 collocation 之前跑完并 quit，collocation 的 30 项观测
    # 就变成「未采集到」，而且偶发（实测 198/0 与 168/30 交替出现）。所以 import
    # 必须晚到「new_features（最坏约 8s）+ collocation（最坏约 3s）」都必然结束
    # 之后，给 collocation 一个不受挤压的时间窗。
    QTimer.singleShot(26000, _guard(import_check))
    app.exec()
    conn.close()

    rows = _verdicts(results, EXPECT)
    label_width = max(len(k) for k, _, _ in rows)
    for key, ok, detail in rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {key.ljust(label_width)} : {detail}")

    # 判据自证：把一项观测故意改错，判定循环必须报出失败。否则说明判定逻辑
    # 本身在空转——这正是本脚本原先的样子（只打印、无条件 return 0）。
    # 挑一个「期望值是具体值」的项来注入，避免选到判定函数恰好接受的字符串。
    probe_key = next(
        (k for k in sorted(EXPECT) if not callable(EXPECT[k]) and k in results), None
    )
    probe_ok = False
    if probe_key is None:
        print("\n判据自证：失败 — 找不到可用于注入的观测项")
    else:
        corrupted = dict(results)
        corrupted[probe_key] = "<故意写错的值>"
        probe_ok = any(not ok for _, ok, _ in _verdicts(corrupted, EXPECT))
        print(
            f"\n判据自证：{'通过' if probe_ok else '失败'}"
            f" — 把「{probe_key}」改错后{'正确报出失败' if probe_ok else '仍报通过，判定逻辑空转'}"
        )

    failed = [key for key, ok, _ in rows if not ok]
    print("\n" + "=" * 56)
    print(f"通过 {len(rows) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for key in failed:
            print(f"  FAIL  {key}")
    return 0 if not failed and probe_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
