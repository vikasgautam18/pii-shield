"""A small thread pool for CPU-bound detection that serves short texts first.

One inference at a time per worker keeps the CPU from being oversubscribed, but with a plain first-come,
first-served queue a short text then waits behind every long document queued before it.  This pool takes
jobs by *deadline* instead: arrival time plus an allowance proportional to the text length.  A short text
overtakes the long documents that arrived shortly before it, yet no job can be passed over for longer
than its own allowance, so long documents are not starved.

A job already running is never interrupted, so a short text could still wait for the document being
processed when it arrives: seconds, behind a 6 KB document.  An *express lane* avoids that: texts up to
``express_chars`` run on their own thread, beside the long document, instead of queueing for the main
threads.  A short text costs about a tenth of a second of CPU, so one extra inference in flight barely
slows the long one, and the lane has its own cap, so a burst of short texts cannot oversubscribe the CPU.
"""

import collections
import heapq
import itertools
import os
import threading
import time
from concurrent.futures import Future


class DetectionPool:
    """``workers`` threads that run jobs in deadline order (first come, first served when the allowance is 0),
    plus ``express_workers`` threads for texts of at most ``express_chars`` characters (0 turns the lane off).

    Threads start on the first job in each process, so a pool created before gunicorn forks (``--preload``)
    holds no thread across the fork.
    """

    def __init__(
        self,
        workers: int,
        seconds_per_kchar: float = 1.0,
        name: str = "nlp",
        express_chars: int = 0,
        express_workers: int = 1,
    ) -> None:
        self.max_workers = max(1, workers)
        self.express_chars = max(0, express_chars)
        self.express_workers = max(0, express_workers) if self.express_chars else 0
        self._allowance = max(0.0, seconds_per_kchar) / 1000.0
        self._name = name
        self._jobs: list = []  # heap of (deadline, arrival order, future, call)
        self._express_jobs: collections.deque = collections.deque()  # first come, first served
        self._arrivals = itertools.count()
        self._wakeup = threading.Condition()
        self._threads_pid: int | None = None

    def submit(self, text_length: int, call) -> Future:
        """Queue ``call()``; the returned future completes with its result or exception."""
        future: Future = Future()
        with self._wakeup:
            self._ensure_threads()
            if self.express_workers and text_length <= self.express_chars:
                self._express_jobs.append((future, call))
            else:
                deadline = time.monotonic() + text_length * self._allowance
                heapq.heappush(self._jobs, (deadline, next(self._arrivals), future, call))
            self._wakeup.notify_all()
        return future

    def _ensure_threads(self) -> None:
        if self._threads_pid == os.getpid():
            return
        self._threads_pid = os.getpid()
        for number in range(self.max_workers):
            threading.Thread(target=self._work, args=(False,), name=f"{self._name}-{number}", daemon=True).start()
        for number in range(self.express_workers):
            threading.Thread(
                target=self._work, args=(True,), name=f"{self._name}-express-{number}", daemon=True
            ).start()

    def _work(self, express: bool) -> None:
        while True:
            with self._wakeup:
                while not (self._express_jobs if express else self._jobs):
                    self._wakeup.wait()
                if express:
                    future, call = self._express_jobs.popleft()
                else:
                    _, _, future, call = heapq.heappop(self._jobs)
            if not future.set_running_or_notify_cancel():
                continue  # cancelled while it waited
            try:
                future.set_result(call())
            except BaseException as exc:  # reported to the awaiting request, never lost
                future.set_exception(exc)
