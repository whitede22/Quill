"""主题与背景档位的名义表：配置层与界面层共用的最小事实集。

这个模块存在的原因只有一个。`config.py` 需要知道默认主题名、默认档位名，以及
把任意输入归一到合法值的两个函数，而它不该为此 import 界面包。界面包要渲染六套
主题，得读全部色值；配置层只需要「有哪些名字」。把名义表放在这里，色值与样式
模板留在 `app/ui/theme.py`，依赖方向就变成 ui 依赖这里，而不是反过来。

依赖倒置本身不报错，代价藏在将来。若有人给 `app/ui/__init__.py` 加一行 Qt 导入，
或者主题模块为了取颜色对象而 import QtGui，`config` 会立刻变成 Qt 依赖，自检、
报告生成这类无界面消费方也跟着被拖进去。所以这里有一条子进程断言盯着：
`import app.config` 之后不得出现 PySide6 或 shiboken 模块。

**同一件事两份定义的风险**，`theme_id_of()` 判的是本模块的 `THEME_IDS`，而
`app/ui/theme.py` 的 `THEMES` 由同一份 `THEME_IDS` 组装。两处若有一天漂开，
不认识的主题名会被放行到一套不存在的调色板里，取值时报错或取到空。自检里
`set(THEMES) == set(THEME_IDS)` 这条断言盯着它，改动任一侧都会红。
"""

from __future__ import annotations

# 默认主题。与 `ui/theme.py` 的六套一一对应，改名要同步 CHANGELOG 与欢迎页。
THEME_DEFAULT = "ink"

# 全部主题 id。顺序即设置界面下拉框的顺序，第一条是默认。
THEME_IDS = ("ink", "sage", "clay", "plum", "graphite", "night")

# 背景透出档位名。名在深浅两套底座下共用，值不共用：同一个 58% 在浅色底上守住
# 5.4:1，在深色底上只剩 3.2:1，所以值由 ui 层按底座分家，这里只管名字。
BG_VEIL_DEFAULT = "medium"
VEIL_NAMES = ("soft", "medium", "bold")


def theme_id_of(text: object) -> str:
    """把配置里的主题名归一到合法值。不认识的一律退回默认。

    配置来自磁盘，`[]` / `{}` 这类不可哈希值必须先挡掉，否则 `in` 会抛
    `TypeError`，而 `load_config()` 承诺过对任意输入都不抛。
    """
    if not isinstance(text, str):
        return THEME_DEFAULT
    return text if text in THEME_IDS else THEME_DEFAULT


def veil_level(text: object) -> str:
    """把配置里的档位名归一到合法值。理由同 `theme_id_of()`。"""
    if not isinstance(text, str):
        return BG_VEIL_DEFAULT
    return text if text in VEIL_NAMES else BG_VEIL_DEFAULT
