"""A small thread pool for CPU-bound detection that serves short texts first.

One inference at a time per worker keeps the CPU from being oversubscribed, but with a plain first-come,
first-served queue a short text then waits behind every long document queued before it.  This pool takes
jobs by *deadline* instead: arrival time plus an allowance proportional to the text length.  A short text
overtakes the long documents that arrived shortly before it, yet no job can be passed over for longer
than its own allowance, so long documents are not starved.  A job already running is never interrupted,
so a short text can still wait for the document being processed when it arrives.
"""

import heapq
import itertools
import os
import threading
import time
from concurrent.futures import Future


class DetectionPool:
    """``workers`` threads that run jobs in deadline order (first come, first served when the allowance is 0).

    Threads start on the first job in each process, so a pool created before gunicorn forks (``--preload``)
    holds no thread across the fork.
    """

    def __init__(self, workers: int, seconds_per_kchar: float = 1.0, name: str = "nlp") -> None:
        self.max_workers = max(1, workers)
        self._allowance = max(0.0, seconds_per_kchar) / 1000.0
        self._name = name
        self._jobs: list = []  # heap of (deadline, arrival order, future, call)
        self._arrivals = itertools.count()
        self._wakeup = threading.Condition()
        self._threads_pid: int | None = None

    def submit(self, text_length: int, call) -> Future:
        """Queue ``call()``; the returned future completes with its result or exception."""
        future: Future = Future()
        deadline = time.monotonic() + text_length * self._allowance
        with self._wakeup:
            self._ensure_threads()
            heapq.heappush(self._jobs, (deadline, next(self._arrivals), future, call))
            self._wakeup.notify()
        return future

    def _ensure_threads(self) -> None:
        if self._threads_pid == os.getpid():
            return
        self._threads_pid = os.getpid()
        for number in range(self.max_workers):
            threading.Thread(target=self._work, name=f"{self._name}-{number}", daemon=True).start()

    def _work(self) -> None:
        while True:
            with self._wakeup:
                while not self._jobs:
                    self._wakeup.wait()
                _, _, future, call = heapq.heappop(self._jobs)
            if not future.set_running_or_notify_cancel():
                continue  # cancelled while it waited
            try:
                future.set_result(call())
            except BaseException as exc:  # reported to the awaiting request, never lost
                future.set_exception(exc)
