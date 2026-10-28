"""应用入口。"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import (
    APP_DIR,
    APP_NAME,
    DB_PATH,
    KEY_PATH,
    ensure_dir,
    load_config,
    migrate_legacy_home,
)
from .core import analytics
from .core.db import (
    backup_to,
    connect,
    init_db,
    replace_with_staged,
    stage_restore,
)
from .core.dictionary import DictionaryService
from .core.repository import Repository
from .providers import build_provider
from .security.keystore import load_api_key
from .ui.main_window import MainWindow
from .ui.theme import stylesheet

LOCK_PATH = APP_DIR / "quill.lock"

# 「把已经开着的那个窗口叫出来」的本地通道名。改名字等于改变协议，
# 版本号后缀让新旧两版同时存在时不会互相误解。
ACTIVATION_SERVER = "Quill.Activate.v1"


def notify_running_instance(name: str | None = None) -> bool:
    """让已经运行的实例把窗口叫到前台。成功返回 True。

    **托盘常驻之后，「已经在运行」不再是终点。** 用户双击图标时，窗口多半正
    藏在托盘里；若只回一句「已在运行」再退出，他看到的是一次毫无反应的双击，
    而他要的其实只是「把窗口给我」。所以第二个进程先试着敲一下第一个进程，
    敲通了就安静退出。
    """
    if sys.platform != "win32":
        return False
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(name or ACTIVATION_SERVER)
    if not socket.waitForConnected(400):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(400)
    socket.disconnectFromServer()
    return True


def start_activation_server(on_activate, parent=None, name: str | None = None):
    """监听唤醒请求。

    **必须给 parent。** QLocalServer 没有父对象又只被局部变量引用时，函数一
    返回它就被回收，监听跟着消失——「双击图标叫窗口」这条路径于是静默失效，
    表现是第二个实例又弹「已在运行」。挂到窗口上，窗口活着它就活着。

    `name` 只为测试留出另一个端点：自检要跑真实的监听与敲击，用生产名字会
    和正在运行的 Quill 抢同一个端点。
    """
    from PySide6.QtNetwork import QLocalServer

    endpoint = name or ACTIVATION_SERVER
    server = QLocalServer(parent)
    # 上一个进程被强杀时可能留下同名端点，先清掉；不清的话 listen 会失败，
    # 于是「双击图标叫窗口」这条路径静默失效。
    QLocalServer.removeServer(endpoint)
    if not server.listen(endpoint):
        return None
    server.newConnection.connect(on_activate)
    return server


def build_service(conn) -> tuple[Repository, DictionaryService]:
    """组装数据层与查词服务（无 GUI 依赖，便于脚本复用与自检）。"""
    config = load_config()
    repo = Repository(conn)
    provider = build_provider(config, load_api_key(KEY_PATH))
    return repo, DictionaryService(repo, provider)


def icon_path() -> Path | None:
    """定位图标文件；打包后位于解包目录，开发时位于项目 assets 下。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[1]
    candidate = base / "assets" / "quill.ico"
    return candidate if candidate.exists() else None


class _InstanceLock:
    """单实例锁的持有者，退出时调用 close() 释放。

    两点是实测踩出来的，改动前请先读：

    1. 用裸文件描述符，不用 open() 返回的缓冲对象。
       缓冲对象在 close() 时会再 flush 一次，一旦此前发生过写入冲突，
       close 自身会抛 PermissionError，把「已有实例」这条正常分支变成崩溃。
    2. 完全不写入任何内容。
       锁的语义只需「某个字节被占用」。若先往锁文件里写字，第二个实例
       写入时会撞上第一个实例已持有的字节区间，直接 PermissionError。
    """

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def acquire_instance_lock():
    """保证同一时刻只有一个实例在写数据库。

    多实例并发写 SQLite 会出现锁竞争，用户侧表现为「时好时坏」，很难排查，
    因此直接禁止。进程退出（包括被任务管理器强杀）时操作系统会释放文件锁，
    因此不存在「锁文件残留导致再也打不开」的问题，无需额外清理逻辑。
    """
    if sys.platform != "win32":
        return True  # 非 Windows 平台不做限制

    import msvcrt

    ensure_dir()
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # LK_NBLCK：非阻塞，抢不到立即返回错误而不是挂起等待
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    return _InstanceLock(fd)


def main() -> int:
    migrate_legacy_home()
    ensure_dir()
    config = load_config()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(stylesheet(config.get("theme")))

    icon_file = icon_path()
    if icon_file is not None:
        app.setWindowIcon(QIcon(str(icon_file)))

    # 先敲一下已经开着的那个。敲通了说明它还在跑（很可能正躺在托盘里），
    # 把它叫出来就够了，这个进程不必再活一份。
    if notify_running_instance():
        return 0

    lock = acquire_instance_lock()
    if lock is None:
        QMessageBox.information(
            None,
            "Quill 已在运行",
            "生词本已经打开了一个窗口。\n\n"
            "同时运行多个实例会让数据库写入相互争用，因此只允许开启一个。\n"
            "如果找不到窗口，看一下任务栏右下角的托盘图标。",
        )
        return 0

    conn = connect(DB_PATH)
    init_db(conn)
    repo, service = build_service(conn)

    window = MainWindow(repo, service, config)

    def _restore_flow(backup: Path) -> Path:
        """用备份覆盖词库，返回替换前那份数据的路径。

        放在 main 里是因为**连接在这里**：恢复必须在关闭连接之后才可能替换
        文件（Windows 会锁住打开的库），而关闭哪个连接只有这里知道。

        三步的顺序都不能换：先做保护性备份 → 把旧库的 WAL 回写并关闭 →
        替换。少了第一步，用户在恢复之后会觉得「还是原来那份好」而回不去。
        """
        guard = backup_to(
            conn, APP_DIR / f"quill_before_restore_{datetime.now():%Y%m%d-%H%M%S}.db"
        )
        staged = stage_restore(backup, DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        replace_with_staged(staged, DB_PATH)
        return guard

    window.restore_hook = _restore_flow

    # 挂到窗口上：窗口活着，监听就在（见 start_activation_server 的说明）。
    start_activation_server(window._restore_from_tray, window)
    window.show()

    # 启动事件放在窗口建好之后：第一次启动时用户还没表态，这次不记；
    # 之后每次启动记一条，用来看「装了之后有没有再打开」。
    analytics.record_event("app_started")

    try:
        return app.exec()
    finally:
        conn.close()
        if lock is not True:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
