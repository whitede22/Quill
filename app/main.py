"""应用入口。"""

from __future__ import annotations

import os
import sys
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
from .core.db import connect, init_db
from .core.dictionary import DictionaryService
from .core.repository import Repository
from .providers import build_provider
from .security.keystore import load_api_key
from .ui.main_window import MainWindow
from .ui.theme import stylesheet

LOCK_PATH = APP_DIR / "quill.lock"


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

    lock = acquire_instance_lock()
    if lock is None:
        QMessageBox.information(
            None,
            "Quill 已在运行",
            "生词本已经打开了一个窗口。\n\n"
            "同时运行多个实例会让数据库写入相互争用，因此只允许开启一个。",
        )
        return 0

    conn = connect(DB_PATH)
    init_db(conn)
    repo, service = build_service(conn)

    window = MainWindow(repo, service, config)
    window.show()

    try:
        return app.exec()
    finally:
        conn.close()
        if lock is not True:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
