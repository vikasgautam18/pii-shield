"""CPU-bound detection runs on a small bounded pool that serves short texts ahead of long documents."""

import asyncio
import contextvars
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.nlp_pool import DetectionPool


@pytest.fixture()
def client():
    return TestClient(main.app)


def _blocked_pool(**kwargs):
    """A one-thread pool whose only thread is busy until the returned event is set."""
    pool = DetectionPool(1, **kwargs)
    release = threading.Event()
    started = threading.Event()
    blocker = pool.submit(0, lambda: (started.set(), release.wait(5)))
    assert started.wait(2)
    return pool, release, blocker


class TestDetectionPool:
    def test_returns_results_and_reraises_errors(self):
        pool = DetectionPool(1)
        assert pool.submit(5, lambda: 42).result(2) == 42
        with pytest.raises(ZeroDivisionError):
            pool.submit(5, lambda: 1 / 0).result(2)

    def test_never_runs_more_jobs_at_once_than_it_has_threads(self):
        pool = DetectionPool(2)
        running, peak, lock = 0, 0, threading.Lock()

        def job():
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.03)
            with lock:
                running -= 1

        for f in [pool.submit(100, job) for _ in range(12)]:
            f.result(5)
        assert peak == 2

    def test_a_short_text_overtakes_a_long_one_that_arrived_before_it(self):
        pool, release, blocker = _blocked_pool(seconds_per_kchar=1.0)
        order = []
        long_job = pool.submit(6000, lambda: order.append("long"))
        short_job = pool.submit(100, lambda: order.append("short"))
        release.set()
        for f in (blocker, long_job, short_job):
            f.result(5)
        assert order == ["short", "long"]

    def test_a_long_text_is_not_starved_by_later_short_ones(self):
        pool, release, blocker = _blocked_pool(seconds_per_kchar=0.2)
        order = []
        long_job = pool.submit(1000, lambda: order.append("long"))  # may be passed over for 0.2 s
        time.sleep(0.3)
        short_job = pool.submit(0, lambda: order.append("short"))
        release.set()
        for f in (blocker, long_job, short_job):
            f.result(5)
        assert order == ["long", "short"]

    def test_without_an_allowance_it_is_first_come_first_served(self):
        pool, release, blocker = _blocked_pool(seconds_per_kchar=0)
        order = []
        jobs = [pool.submit(n, lambda n=n: order.append(n)) for n in (9000, 10, 5000)]
        release.set()
        for f in [blocker, *jobs]:
            f.result(5)
        assert order == [9000, 10, 5000]

    def test_a_job_cancelled_while_queued_never_runs(self):
        pool, release, blocker = _blocked_pool()
        ran = []
        queued = pool.submit(10, lambda: ran.append(1))
        assert queued.cancel()
        release.set()
        blocker.result(5)
        pool.submit(10, lambda: ran.append(2)).result(5)
        assert ran == [2]

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    def test_a_forked_process_starts_its_own_threads(self):
        pool = DetectionPool(1)
        assert pool.submit(1, lambda: "parent").result(2) == "parent"  # threads now exist in this process
        pid = os.fork()
        if pid == 0:  # the child inherited the pool but none of its threads
            code = 1
            try:
                code = 0 if pool.submit(1, lambda: "child").result(5) == "child" else 2
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0


def test_the_pool_size_follows_the_setting_and_is_never_below_one():
    assert main._nlp_pool.max_workers == main._NLP_THREAD_POOL_SIZE >= 1


def test_anonymization_runs_on_an_nlp_thread_not_the_event_loop(client, monkeypatch):
    seen = []
    real = main.engine.anonymize

    def spy(*args, **kwargs):
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(main.engine, "anonymize", spy)
    resp = client.post("/anonymize_unique", json={"text": "Mail rajesh.sharma@example.com"})
    assert resp.status_code == 200
    assert seen and all(name.startswith("nlp") for name in seen)
    assert "rajesh.sharma@example.com" not in resp.json()["anonymized_text"]


def test_the_callers_context_reaches_the_pool_thread():
    marker = contextvars.ContextVar("marker", default="unset")

    async def call():
        marker.set("request-123")
        return await main._run_in_nlp_pool(10, marker.get)

    assert asyncio.run(call()) == "request-123"
