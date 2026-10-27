"""全局热键（Alt+Q）与剪贴板预填。

## 为什么用 RegisterHotKey，而不是键盘钩子

`RegisterHotKey` 是系统提供的一次性注册：告诉 Windows「按下这个组合键时通知
我」，系统在把按键交给前台程序之前就截住。它不注入任何进程、不需要管理员权限、
不读取用户的输入内容。

用 `SetWindowsHookEx(WH_KEYBOARD_LL)` 也能做到「双击某个键」这类更花哨的触发，
但那是**系统级监听每一次按键**：既是杀软的重点关注对象，也让「不联网、不收集」
这条承诺变得难以自证。为一个唤出窗口的功能付这个代价不值。

## 代价：注册是独占的，而且会静默失败

同一个组合键全系统只能注册一次。被别的程序占了，`RegisterHotKey` 返回 0——
**必须当场把这件事说出来**。不说的话用户看到的是「按了没反应」，会以为程序坏了，
而实际上功能根本没装上。

## 为什么是两键

三键（Ctrl+Alt+Q）几乎不会冲突，但用户的目的是「读 PDF 时不想离开键盘」，
每多一个键就是多一次手指移动。两键方案在 Windows 上就是「修饰键 + 主键」：

| 候选 | 落到前台程序里的含义 | 判断 |
|---|---|---|
| `Alt+Q` | 少数程序用它做菜单快捷键 | 采用，冲突时由注册失败暴露 |
| `Ctrl+Q` | 浏览器、编辑器的「退出」 | 不用：抢走它等于弄坏别的软件 |
| `Alt+Z` / `Alt+X` | 同样少见 | 留给用户改，见设置里的说明 |

## 平台

只有 Windows 有这几个 API。其他平台 `available()` 返回 False，界面据此不建
托盘、不装热键——留一个点了没反应的开关，比没有这个开关更糟。
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject
from PySide6.QtWidgets import QApplication

from ..core.importer import MAX_WORDS_PER_PHRASE

# Win32 常量。名字与含义都来自 user32 文档，改之前先确认你改的是哪一个。
MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# 线程热键的 id 只能用 0x0000–0xBFFF。用 1 太容易和别的程序（它们多半也用
# 小整数编号，但那是在**自己的线程**里，本来不冲突）撞上，取一个大一点的固定值
# 只是为了让日志里一眼能认出来源。
HOTKEY_ID = 0x0A17

# 组合键只有一份定义：显示用的文字从这两个名字生成，注册用的虚拟键码从同一个
# 名字算出来。分开写的话，改键位时必然漏改一处，而漏掉的是「显示」那一处时
# 用户会按着一个从来没被注册的键。
_MOD_NAME = "Alt"
_KEY_NAME = "Q"
HOTKEY_TEXT = f"{_MOD_NAME}+{_KEY_NAME}"
_MODS = MOD_ALT | MOD_NOREPEAT
_VK = ord(_KEY_NAME)


def available() -> bool:
    """当前平台能不能装全局热键。"""
    return sys.platform == "win32"


# 剪贴板里什么样的文本才值得填进输入框。
#
# 判据是「它像不像一个要查的词条」，而不是「它是不是英文」：网页上随手复制的
# 一整句话、一个带编号的列表项、一个含数字的型号，填进去只会让用户先删一遍。
# 上限取导入侧同一个常量——两处各写一个「4」的话，改了一边就会出现
# 「导入认得出、热键填不进」这种只在特定长度下复现的怪事。
_SEED_CHARS_RE = re.compile(r"^[A-Za-z][A-Za-z' -]*$")
_SEED_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]*$")
# 两端要剥掉的字符：引号、括号、句读，以及 Markdown 的强调符。
# 只剥两端——`don't` 里的撇号在中间，不受影响。
_SEED_EDGE = " \t\"'“”‘’()[]{}<>.,;:!?*_~`|/\\+="
_SEED_MAX_CHARS = 60


def clipboard_seed(text: str) -> str:
    """从剪贴板文本里挑出可以直接查的候选词；挑不出来返回空串。

    只取第一个非空行：用户复制的常常是「一个词 + 后面的解释」，把整块塞进
    输入框等于让他手工删。挑不出来时**返回空串而不是硬塞**，界面据此只把焦点
    放到输入框上，不预填一堆等着被删的垃圾。
    """
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    candidate = first.strip(_SEED_EDGE)
    if not candidate or len(candidate) > _SEED_MAX_CHARS:
        return ""
    if not _SEED_CHARS_RE.match(candidate):
        return ""
    words = candidate.split()
    if not 1 <= len(words) <= MAX_WORDS_PER_PHRASE:
        return ""
    if any(not _SEED_WORD_RE.match(word) for word in words):
        return ""
    return " ".join(words)


_user32_cache: ctypes.WinDLL | None = None


def _user32() -> ctypes.WinDLL:
    """单独一份 user32 句柄。

    不给 `ctypes.windll.user32` 设全局 argtypes：那是进程内共享的对象，设一次
    会影响所有调用方，而本程序的其他地方也可能用它。自带一份，参数类型只影响
    自己。
    """
    global _user32_cache
    if _user32_cache is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        lib.RegisterHotKey.restype = wintypes.BOOL
        lib.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        lib.UnregisterHotKey.restype = wintypes.BOOL
        _user32_cache = lib
    return _user32_cache


class _HotkeyFilter(QAbstractNativeEventFilter):
    """把 `WM_HOTKEY` 从 Qt 的原始消息里捞出来。

    注册时传 `hwnd=None`：热键挂在线程上，系统把 `WM_HOTKEY` 投进本线程的
    消息队列，于是它一定会经过 Qt 的事件分发，不需要另起一个窗口或消息循环。
    """

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt 命名
        name = eventType
        if isinstance(name, (bytes, bytearray)):
            name = name.decode("ascii", "replace")
        if str(name) in ("windows_generic_MSG", "windows_dispatcher_MSG"):
            # `message` 是 MSG* 的地址。读失败就当作不是我们的消息——原始消息
            # 过滤器抛异常会让整个事件分发出问题，而这里没有值得让程序停下的
            # 理由。
            try:
                msg = wintypes.MSG.from_address(int(message))
            except (TypeError, ValueError):
                return False, 0
            if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                self._callback()
                return True, 0
        return False, 0


class GlobalHotkey(QObject):
    """一个已经注册好的全局热键。`register()` 的返回值必须被调用方检查。"""

    def __init__(self, callback: Callable[[], None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._filter = _HotkeyFilter(callback)
        self._callback = callback
        self._app: QApplication | None = None
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self) -> bool:
        """成功返回 True；平台不支持、没有 QApplication、按键被占用都返回 False。"""
        if self._registered:
            return True
        if not available():
            return False
        app = QApplication.instance()
        if app is None:
            return False
        if not _user32().RegisterHotKey(None, HOTKEY_ID, _MODS, _VK):
            return False
        app.installNativeEventFilter(self._filter)
        self._app = app
        self._registered = True
        return True

    def unregister(self) -> None:
        """撤掉注册。重复调用是安全的。"""
        if not self._registered:
            return
        self._registered = False
        _user32().UnregisterHotKey(None, HOTKEY_ID)
        if self._app is not None:
            self._app.removeNativeEventFilter(self._filter)
            self._app = None
