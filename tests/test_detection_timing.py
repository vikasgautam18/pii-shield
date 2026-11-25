"""Where the time of a detection goes: queue wait, the first NLP pass and the case-recovery pass.

A slow request is either waiting for a free slot (add capacity) or computing (make it cheaper); the stats, the
log line, the span attributes and the histograms tell the two apart.
"""

import asyncio
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.nlp_pool import DetectionPool


@pytest.fixture()
def client():
    return TestClient(main.app)


def _run(coroutine):
    """Run *coroutine* on a private loop: asyncio.run() would leave the main thread without a current loop,
    which older tests that call asyncio.get_event_loop() rely on."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class TestStats:
    def test_a_normally_cased_text_has_no_recovery_pass(self):
        stats = main.engine.anonymize("Please send the signed form to rajesh.kumar@gmail.com tomorrow").stats
        assert stats.recovery_pass is False and stats.recovery_ms == 0.0
        assert 0 < stats.nlp_ms <= stats.detect_ms

    def test_an_all_caps_name_gets_a_timed_recovery_pass(self):
        stats = main.engine.anonymize("Dear RAJESH KUMAR, your form went to rajesh.kumar@gmail.com").stats
        assert stats.recovery_pass is True and stats.recovery_ms > 0
        assert stats.nlp_ms + stats.recovery_ms <= stats.detect_ms

    def test_the_stats_belong_to_the_thread_that_ran_the_detection(self):
        seen = {}

        def run(name, text):
            seen[name] = main.engine.anonymize(text).stats.recovery_pass

        threads = [
            threading.Thread(target=run, args=("plain", "Please send the signed form tomorrow")),
            threading.Thread(target=run, args=("caps", "Dear RAJESH KUMAR, please sign here")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert seen == {"plain": False, "caps": True}


class TestQueueWait:
    def test_a_job_that_waited_for_the_thread_reports_it(self, monkeypatch):
        monkeypatch.setattr(main, "_nlp_pool", DetectionPool(1))

        async def scenario():
            release = threading.Event()
            running = asyncio.create_task(main._run_in_nlp_pool_timed(10, release.wait, 5))
            await asyncio.sleep(0.05)
            waiting = asyncio.create_task(main._run_in_nlp_pool_timed(10, lambda: "done"))
            await asyncio.sleep(0.25)
            release.set()
            (_, first_wait), (value, second_wait) = await asyncio.gather(running, waiting)
            return value, first_wait, second_wait

        value, first_wait, second_wait = _run(scenario())
        assert value == "done"
        assert first_wait < 100 and second_wait >= 200

    def test_the_plain_helper_still_returns_only_the_result(self):
        assert _run(main._run_in_nlp_pool(10, lambda: 41 + 1)) == 42


def test_size_classes():
    assert [main._size_class(n) for n in (0, 500, 501, 2000, 2001, 8000, 8001)] == [
        "small", "small", "medium", "medium", "large", "large", "xlarge",
    ]


def test_the_request_log_line_carries_the_timings(client, caplog):
    caplog.set_level(logging.INFO, logger="pii-shield")
    resp = client.post("/anonymize_unique", json={"text": "Dear RAJESH KUMAR, mail rajesh.kumar@gmail.com"})
    assert resp.status_code == 200
    line = next(r.getMessage() for r in caplog.records if "Unique anonymization complete" in r.getMessage())
    for field in ("chars=", "queue_ms=", "detect_ms=", "nlp_ms=", "recovery_pass=True", "recovery_ms="):
        assert field in line
