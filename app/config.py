"""运行时配置与路径管理。

**容错原则**：配置文件是用户可写、也可能被别的工具写坏的文件。它一旦让程序
起不来，用户就没有自救手段了——软件打不开，也就没有界面可以把设置改回来。
因此本模块的两个入口（读 / 写）都必须对任意输入保持稳定：

- 读：无论文件内容是坏 JSON、合法 JSON 却不是对象、还是字段类型不对，都退回
  默认值，绝不抛异常。
- 写：只保存通过校验的值，不把界面上的非法输入原样落盘、留到下次启动再踩。

需要「告诉用户哪里填错了」的场合，用 `check_config()` 取一条人类可读的描述。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import atomic

# 主题与档位的名义表从 `theme_meta` 取，不从 `ui.theme` 取。配置属于数据层，
# 数据层 import 界面包是方向反了：本模块被自检、报告生成这类无界面代码共用，
# 一旦界面包将来真的拉起 Qt，这些消费方会跟着被拖进去，而且不会有任何报错。
# 自检里有一条子进程断言：导入本模块之后，不得出现 PySide6 或 shiboken。
from .theme_meta import BG_VEIL_DEFAULT, THEME_DEFAULT, theme_id_of, veil_level

# 软件名。窗口标题、应用名、数据目录、exe 名都从这里派生，避免「同一属性
# 有多份定义」。旧名 Lexicon 已于 2026-10 改为 Quill。
APP_NAME = "Quill"

# 数据目录。优先 QUILL_HOME（新名），其次 LEXICON_HOME（旧名，向后兼容），
# 默认 ~/.quill。旧版本把数据放在 ~/.lexicon，首次启动由 migrate_legacy_home()
# 复制过来（保留旧目录作备份）。
APP_DIR = Path(
    os.environ.get("QUILL_HOME")
    or os.environ.get("LEXICON_HOME")
    or (Path.home() / ".quill")
)
CONFIG_PATH = APP_DIR / "config.json"
KEY_PATH = APP_DIR / "api_key.bin"
DB_PATH = APP_DIR / "quill.db"
# 本地使用统计的数据库。**刻意与词库分开**：统计写失败、被锁、被用户删掉，
# 都不该碰到生词本；反过来，备份或导出词库时也不会把统计数据一起带走。
ANALYTICS_PATH = APP_DIR / "analytics.db"

# 旧数据目录与旧数据库文件名（迁移用）。它们只在这里定义一份，迁移逻辑与
# 自检都从这取值，避免「改了一处忘了另一处」。
LEGACY_HOME = Path.home() / ".lexicon"
LEGACY_DB_NAME = "lexicon.db"

DEFAULT_CONFIG: dict = {
    "provider": "openai-compat",
    # 出厂默认指向 DeepSeek，因为它对国内用户可直接注册使用；OpenAI 的地址
    # 与模型名同样合法，在设置里改掉即可。这里只是一个能直接跑通的起点，
    # 不表示程序偏袒某一家——协议本身是通用的。
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "timeout": 30.0,
    # 接口失败（超时、断网、额度用尽）时自动改用本机离线词库。
    # 默认开启：这类失败都不是用户当场能解决的问题，但「查不到词」会让他
    # 什么都做不了。兜底产出会在详情里标明来源，不会冒充接口结果。
    "offline_fallback": True,
    # 关闭窗口时留在托盘，全局热键（Alt+Q）随时唤出。
    # 默认开启：热键的价值就在「窗口没开着的时候按一下」，不常驻的话它退化成
    # 一个比 Alt+Tab 更慢的切换方式。
    # 代价必须让用户看得见：关掉窗口 = 程序还在跑。所以第一次收进托盘时会弹一次
    # 托盘气泡说明，设置里也留了关掉它的开关；关掉之后关闭窗口就真的退出。
    "tray_enabled": True,
    # 窗口背景图：留空表示不使用。存的是绝对路径，程序不做拷贝——用户换图只要
    # 改路径，不必先导入一份。
    # 路径失效（文件被删/被挪）**不算配置错误**：背景图是装饰，它失效时程序
    # 照常可用，只是没有背景。见 check_config 里那段说明。
    "background_image": "",
    # 给背景图加模糊蒙版。默认开：照片直接铺在界面后面通常太吵，糊一层既保留
    # 氛围又不跟正文抢注意力。不想要的人关掉即可（关掉就是原图）。
    "background_blur": True,
    # 背景图下内容面的「蒙版档位」：soft / medium / bold。
    # 合法值只有 theme.BG_VEIL_LEVELS 那一份定义——样式表里那些 rgba 规则就是从
    # 它生成的，档位名若在这里再抄一遍，将来加一档必然漏改一处。
    # 依赖方向（config → ui.theme）看着别扭，但 theme 是纯字符串模块、不碰 Qt，
    # 而「哪些档位合法」本来就是界面的事。
    "background_veil": BG_VEIL_DEFAULT,
    # 最近用过的背景图，最近的排在最前。给「换回上一张」省一次翻文件对话框；
    # 只存路径，不拷贝文件。上限见 RECENT_BACKGROUNDS_MAX。
    "background_recent": [],
    # 界面主题。合法值只有 theme.THEMES 的那一份定义，写在这里再抄一遍的话，
    # 将来加主题必然漏改一处。
    "theme": THEME_DEFAULT,
    # 教程 / 欢迎页的版本号。主窗口拿它跟 onboarding.CURRENT_ONBOARD_VERSION
    # 比，配置值更小就弹出欢迎页并写回新版本。用版本号而不是 bool 的「看过没有」：
    # 教程大改后递增，老用户也能看到新版。
    "onboard_version": 0,
    # ---- 本地使用统计 ----
    # 三个字段一起读才成立：
    #   analytics_consent_decided 记「用户是否明确选过」，没选过就还要弹告知页；
    #   analytics_enabled 记「现在是否记录」，用户可以选了参与之后再关掉。
    # 两者不能合并成 bool：否则「关掉统计」与「还没问过」无法区分，关掉之后
    # 每次启动都会重新弹告知页。
    # 默认全关：未告知之前不得开始记录。
    "analytics_enabled": False,
    "analytics_consent_decided": False,
    # 统计数据结构版本。事件表结构变化时 +1，用于报告与旧库兼容判断。
    # 报告里报的是 `analytics.SCHEMA_VERSION`，不是这里——这一项留着是配置
    # 结构的一部分，改它不影响报告写什么。
    "analytics_schema_version": 2,
    # 提交报告时的署名，空表示还没填过。只为了下次不用重输，它不进统计、
    # 也不参与任何别的判断——删掉这一行等于清空预填，没有其他后果。
    "report_name": "",
}

# 接入模式白名单。写在这里而不是散落在 UI 与 provider 里，避免各判一套。
PROVIDERS: tuple[str, ...] = ("openai-compat", "offline", "mock")

RECENT_BACKGROUNDS_MAX = 6

DEFAULT_TIMEOUT = float(DEFAULT_CONFIG["timeout"])


def migrate_legacy_home() -> bool:
    """把旧版本的数据目录 ~/.lexicon 迁移到 ~/.quill。

    只在「旧目录存在、新目录还不存在、且用户没有通过环境变量指定数据目录」时
    迁移：复制整个目录（含 quill 之前的 config.json / api_key.bin / lexicon.db），
    再把数据库文件名 lexicon.db 改成 quill.db。旧目录**保留不删**，作为备份。

    - 返回是否发生了迁移。
    - 失败返回 False 但不抛异常：数据还在旧目录，下次启动会再试。宁可少迁一次，
      也不在迁移失败时把用户挡在启动之外——迁移是便利，不该变成故障源。
    """
    if APP_DIR != Path.home() / ".quill":
        return False  # 用户显式指定了数据目录，不擅自迁移
    if not LEGACY_HOME.is_dir() or APP_DIR.exists():
        return False
    try:
        shutil.copytree(LEGACY_HOME, APP_DIR)
        old_db = APP_DIR / LEGACY_DB_NAME
        new_db = APP_DIR / "quill.db"
        if old_db.exists() and not new_db.exists():
            old_db.rename(new_db)
        return True
    except OSError:
        return False


def ensure_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


# ---------- 单字段校验（返回 None 表示该值不可用） ----------


def _coerce_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_provider(value: object) -> str | None:
    text = _coerce_text(value)
    return text if text in PROVIDERS else None


def _coerce_veil(value: object) -> str:
    """背景蒙版档位。不认识的值一律退回默认档，而不是留着一个界面用不了的值。"""
    return veil_level(value)


def _coerce_theme(value: object) -> str:
    """界面主题。不认识的值一律退回默认主题（同 _coerce_veil 的理由）。"""
    return theme_id_of(value)


def _coerce_recent(value: object) -> list[str]:
    """最近背景图列表。

    只认字符串列表，逐项去空、去重（保留首次出现的顺序）、截断到上限。任何
    非列表输入（包括被手改坏成字符串的）都退回空列表——这个字段纯粹是便利，
    它坏掉不该影响别的任何东西，也不值得为此给用户报错。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= RECENT_BACKGROUNDS_MAX:
            break
    return out


def _coerce_timeout(value: object) -> float | None:
    # bool 是 int 的子类，`True` 会被当成 1 —— 必须显式排除，
    # 否则 `"timeout": true` 会静默变成一个 1 秒的超时。
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _coerce_bool(value: object) -> bool | None:
    """只认真正的布尔值。

    不做「字符串 'false' 也当假」这类宽松转换：配置文件被手改坏时，
    猜出来的值与用户的本意可能正好相反，而开关类字段猜反了不会报错，
    只会表现成「设置没生效」——比退回默认值更难查。
    """
    return value if isinstance(value, bool) else None


def _coerce_int(value: object) -> int | None:
    """非负整数。bool 是 int 的子类，必须显式排除（同 _coerce_timeout 的理由）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


_COERCERS: dict = {
    "provider": _coerce_provider,
    "base_url": _coerce_text,
    "model": _coerce_text,
    "timeout": _coerce_timeout,
    "offline_fallback": _coerce_bool,
    "tray_enabled": _coerce_bool,
    "background_image": _coerce_text,
    "background_blur": _coerce_bool,
    "background_veil": _coerce_veil,
    "background_recent": _coerce_recent,
    "theme": _coerce_theme,
    "onboard_version": _coerce_int,
    "analytics_enabled": _coerce_bool,
    "analytics_consent_decided": _coerce_bool,
    "analytics_schema_version": _coerce_int,
    "report_name": _coerce_text,
    # 必须登记在这里。save_config() 只保存 _COERCERS 里出现过的字段，
    # 漏登记的表现是「在设置里关掉兜底、重启后又自己打开了」——
    # 开关形同虚设，而且不会报任何错。
}


def _read_raw() -> object:
    """读取配置文件内容；任何读取 / 解析失败都返回 None。"""
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def load_config() -> dict:
    """读取配置，非法或缺失的字段一律退回默认值。**本函数不抛异常。**"""
    cfg = dict(DEFAULT_CONFIG)
    raw = _read_raw()
    # 合法 JSON 不等于我们期望的对象：`5` / `true` / `null` / `[1,2]` 都会让
    # dict.update() 抛 TypeError，而它并不被 except ValueError 覆盖——
    # 「容错」必须显式判断类型，不能指望异常类型恰好对得上。
    if isinstance(raw, dict):
        for key, coerce in _COERCERS.items():
            if key in raw:
                value = coerce(raw[key])
                if value is not None:
                    cfg[key] = value
    # 列表字段必须是**新的一份**：`dict(DEFAULT_CONFIG)` 是浅拷贝，配置文件里
    # 没有这个键时 cfg 与 DEFAULT_CONFIG 会指向同一个列表对象，改一处等于改全局
    # 默认值——下一个实例就会带着上一个实例的「最近使用」启动。
    cfg["background_recent"] = _coerce_recent(cfg.get("background_recent"))
    return cfg


def save_config(cfg: dict) -> None:
    """写入配置。只保存已知字段，且只保存通过校验的值。

    落盘走 `atomic.write_text`（同目录 .tmp → os.replace），不是直接 write_text。
    配置里现在存着「用户已同意统计」「用户已拒绝统计」这类**必须留存**的选择：
    写一半坏掉时，读取侧会静默退回默认值，用户的选择就丢了，下次启动还会重新
    弹一遍告知页——看起来像程序没记住，实际是文件被写坏了。
    """
    ensure_dir()
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        for key, coerce in _COERCERS.items():
            if key in cfg:
                value = coerce(cfg[key])
                if value is not None:
                    merged[key] = value
    atomic.write_text(
        CONFIG_PATH, json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_config(cfg: dict) -> str | None:
    """返回第一条人类可读的问题描述；配置可用时返回 None。

    给界面在「保存」之前调用，把问题挡在源头——否则用户要等到下一次查词
    失败（甚至下次启动）才知道自己填错了。
    """
    if not isinstance(cfg, dict):
        return "配置内容无效"

    provider = cfg.get("provider")
    if provider not in PROVIDERS:
        return "接入模式无效，请重新选择"

    if provider == "offline":
        # 离线词典不访问网络，但词库文件必须真的在——否则用户切过去
        # 只会在第一次查词时失败，那时他早已忘了自己改过设置
        from .providers.offline import resolve_offline_dict

        if resolve_offline_dict() is None:
            return "未找到离线词库文件，无法使用离线词典模式"
        return None

    if provider == "mock":
        # 离线示例模式不访问网络，地址与模型都无关紧要
        return None

    base_url = cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return "请填写接口地址，例如 https://api.deepseek.com/v1"
    if not base_url.strip().lower().startswith(("http://", "https://")):
        return "接口地址需要以 http:// 或 https:// 开头"

    model = cfg.get("model")
    if not isinstance(model, str) or not model.strip():
        return "请填写模型名称，例如 deepseek-chat"

    if _coerce_timeout(cfg.get("timeout", DEFAULT_TIMEOUT)) is None:
        return "超时时间需要是大于 0 的秒数"

    # `background_image` 刻意**不在这里校验**。理由：它是装饰性字段，而
    # check_config 的作用是「拦住会让程序不可用的配置」。若图片路径失效也报错，
    # 用户把图片挪了个位置之后就再也保存不了设置——一个装饰把他的配置界面锁死
    # 了。路径失效的正确处理是「不显示背景」，由设置界面就地说明（见
    # SettingsDialog._sync_background）。
    return None
