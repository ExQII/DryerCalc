"""Фоновые расчёты: калибровка и проверка модели идут в отдельном потоке, окно не замирает.

Как устроено: функция выполняется в пуле потоков Qt (QThreadPool); результат, ошибка и ход расчёта
передаются сигналами объекту-ретранслятору, который живёт в потоке окна, — Qt доставляет их через очередь
событий, поэтому обработчики (заполнение таблиц, диалоги) всегда выполняются в потоке интерфейса.
Расчётные функции получают копии данных (строки журнала, параметры) и ничего не меняют в окне сами.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Set

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, Signal, Slot


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(int, int)


class _Relay(QObject):
    """Живёт в потоке окна: сюда сигналы приходят через очередь событий."""

    def __init__(self, job: "Job", on_done, on_error, on_progress):
        super().__init__()
        self.job = job
        self.on_done, self.on_error, self.on_progress = on_done, on_error, on_progress

    @Slot(object)
    def done(self, result: Any) -> None:
        _JOBS.discard(self.job)
        if self.on_done and not self.job.cancelled():
            self.on_done(result)

    @Slot(object)
    def failed(self, exc: Any) -> None:
        _JOBS.discard(self.job)
        if self.on_error and not self.job.cancelled():
            self.on_error(exc)

    @Slot(int, int)
    def progress(self, k: int, n: int) -> None:
        if self.on_progress and not self.job.cancelled():
            self.on_progress(k, n)


class Job(QRunnable):
    def __init__(self, fn: Callable, args: tuple, kwargs: dict):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()
        self._cancel = False
        self.relay: Optional[_Relay] = None

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:  # выполняется в фоновом потоке
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # noqa: BLE001 — ошибка уходит в поток окна
            self.signals.failed.emit(e)
        else:
            self.signals.done.emit(result)


_JOBS: Set[Job] = set()


def start(fn: Callable, *args, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None,
          on_progress: Optional[Callable] = None, with_progress: bool = False, **kwargs) -> Job:
    """Запустить fn(*args, **kwargs) в фоне. with_progress — передать в fn progress=… и cancelled=…"""
    job = Job(fn, args, kwargs)
    if with_progress:
        job.kwargs["progress"] = job.signals.progress.emit
        job.kwargs["cancelled"] = job.cancelled
    job.relay = _Relay(job, on_done, on_error, on_progress)
    job.signals.done.connect(job.relay.done)
    job.signals.failed.connect(job.relay.failed)
    job.signals.progress.connect(job.relay.progress)
    _JOBS.add(job)
    QThreadPool.globalInstance().start(job)
    return job


def busy() -> bool:
    return bool(_JOBS)


def wait_all(timeout_ms: int = 600_000) -> None:
    """Дождаться всех фоновых расчётов и доставить их результаты (самопроверка, тесты, закрытие окна)."""
    QThreadPool.globalInstance().waitForDone(timeout_ms)
    app = QCoreApplication.instance()
    for _ in range(100):
        if app is not None:
            app.processEvents()
        if not _JOBS:
            break


def cancel_all() -> None:
    for job in list(_JOBS):
        job.cancel()
