"""后台任务封装。

PySide6 的硬性约束：任何网络请求或磁盘重活都不能在主线程执行，
否则事件循环被阻塞，界面直接失去响应。统一经由 QThreadPool 派发，
结果通过 Signal 回到主线程。

**生命周期规则（踩过坑）**：`QThreadPool.start()` 不持有 Python 包装对象的
引用，而调用方通常丢弃 `submit()` 的返回值。若不在本模块内持有，包装对象会
被立刻回收，挂在它上面的 `signals` 随之销毁，此时仍排在主线程队列里的结果
投递会全部作废——现象是「任务确实跑完了（库里都写了），界面却毫无反应」，
而且取决于 GC 时机，属间歇性故障。因此 `submit()` 会把任务登记到 `_ACTIVE`，
完成后自行移除。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

# 正在执行中的任务。仅用于「持有引用」，保证投递完成前对象不被回收。
_ACTIVE: set["Worker"] = set()
_ACTIVE_LOCK = threading.Lock()


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - 线程内部执行
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - 需把任何异常传回 UI
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


def submit(
    fn: Callable[..., Any],
    *args: Any,
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Worker:
    worker = Worker(fn, *args, **kwargs)
    if on_success is not None:
        worker.signals.succeeded.connect(on_success)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    if on_finished is not None:
        worker.signals.finished.connect(on_finished)

    # 关掉 Qt 的自动删除，改由 Python 侧掌握生命周期：
    # autoDelete 会在 run() 返回后立刻销毁 QRunnable 及其 signals，
    # 而 succeeded / failed 的投递此时可能还排在主线程队列里。
    worker.setAutoDelete(False)
    with _ACTIVE_LOCK:
        _ACTIVE.add(worker)
    # 最后连接，确保用户在 on_finished 里做的事先执行完再解除持有。
    # 信号按连接顺序投递，而 succeeded/failed 在 finished 之前发出，
    # 因此走到这里时结果投递一定已经完成。
    worker.signals.finished.connect(lambda w=worker: _release(w))

    QThreadPool.globalInstance().start(worker)
    return worker


def _release(worker: Worker) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(worker)
