"""Where the time of one anonymize() call goes: the recorder, and the steps/details/timeline it puts in the stats.

Only ``pii_shield`` is imported here (never ``app.main``): the engine is built once for the module.
"""

import ast
import collections
import contextvars
import json
import pathlib
import threading
import time

import pytest
from presidio_analyzer.entity_recognizer import EntityRecognizer

import pii_shield
from pii_shield import fast_analysis
from pii_shield.conversation import ConversationAnonymizer
from pii_shield.engine import PiiShieldEngine
from pii_shield.errors import DetectionError
from pii_shield.observability import (
    DETAIL_NAMES,
    LIBRARY_STEPS,
    NO_RECORDER,
    StepRecorder,
    current_recorder,
)
from pii_shield.session_store import InMemorySessionStore

EPSILON = 1e-6  # ms of float noise tolerated when comparing offsets and sums

PLAIN = "Please send the signed form to rajesh.kumar@gmail.com tomorrow"  # no second NER pass
ALL_CAPS = "Dear RAJESH KUMAR, your form went to rajesh.kumar@gmail.com"  # the case-recovery pass runs

_RECORDS = pathlib.Path(__file__).resolve().parents[1] / "examples" / "indian_banking_test_data.json"


def _bank_text(min_chars: int) -> str:
    records = [r["text"] for r in json.loads(_RECORDS.read_text(encoding="utf-8"))]
    text, i = "", 0
    while len(text) < min_chars:
        text += records[i % len(records)] + "\n"
        i += 1
    return text


def _recorded_by_the_no_op():
    """What the shared no-op recorder holds: nothing, whatever runs."""
    return NO_RECORDER.steps, NO_RECORDER.details, NO_RECORDER.timeline, NO_RECORDER.ner_windows


class TestStepRecorder:
    def test_steps_come_in_execution_order_and_time_their_block(self):
        recorder = StepRecorder()
        with recorder.step("analyze") as analyze:
            time.sleep(0.01)
        with recorder.step("handling"):
            pass
        assert list(recorder.steps) == ["analyze", "handling"]
        assert recorder.steps["analyze"] == analyze.ms >= 10 - 1  # sleep() may undershoot by a clock tick

    def test_a_repeated_step_sums_and_the_timeline_keeps_each_run(self):
        recorder = StepRecorder()
        for _ in range(3):
            with recorder.step("analyze"):
                time.sleep(0.002)
        assert list(recorder.steps) == ["analyze"]
        assert len(recorder.timeline) == 3
        assert recorder.steps["analyze"] == pytest.approx(sum(d for _, _, d in recorder.timeline))

    def test_a_detail_attaches_to_the_step_that_is_active_and_sums(self):
        recorder = StepRecorder()
        with recorder.step("analyze"):
            recorder.add_detail("dedup", 1.5)
            recorder.add_detail("dedup", 2.0)
            recorder.add_detail("context_enhance", 0.25)
        with recorder.step("recovery_pass2"):
            recorder.add_detail("dedup", 4.0)
        assert recorder.details == {"analyze.dedup": 3.5, "analyze.context_enhance": 0.25, "recovery_pass2.dedup": 4.0}
        assert list(recorder.ordered_details()) == [
            "analyze.context_enhance", "analyze.dedup", "recovery_pass2.dedup",
        ]

    def test_a_detail_outside_a_step_or_with_an_unknown_name_is_ignored(self):
        recorder = StepRecorder()
        recorder.add_detail("dedup", 1.0)
        with recorder.step("analyze"):
            recorder.add_detail("spacy", 1.0)
        recorder.add_detail("dedup", 1.0)  # the step is over
        assert recorder.details == {}

    def test_a_step_is_closed_and_recorded_when_its_block_raises(self):
        recorder = StepRecorder()
        with pytest.raises(RuntimeError):
            with recorder.step("analyze") as analyze:
                raise RuntimeError("boom")
        assert "analyze" in recorder.steps and analyze.ms == recorder.steps["analyze"]
        recorder.add_detail("dedup", 1.0)
        assert recorder.details == {}, "no step is left active"

    def test_the_timeline_counts_from_the_creation_of_the_recorder_and_never_overlaps(self):
        recorder = StepRecorder()
        time.sleep(0.005)
        for name in ("nlp_pass1", "analyze", "handling"):
            with recorder.step(name):
                time.sleep(0.002)
        assert [name for name, _, _ in recorder.timeline] == ["nlp_pass1", "analyze", "handling"]
        assert recorder.timeline[0][1] >= 4.0
        end = 0.0
        for _, offset, duration in recorder.timeline:
            assert offset >= end - EPSILON
            end = offset + duration

    def test_windows_add_up(self):
        recorder = StepRecorder()
        recorder.add_windows()
        recorder.add_windows(2)
        assert recorder.ner_windows == 3

    def test_the_no_op_recorder_records_nothing_but_its_steps_still_time_their_block(self):
        with NO_RECORDER.step("analyze") as lap:
            NO_RECORDER.add_detail("dedup", 1.0)
            NO_RECORDER.add_windows(5)
            time.sleep(0.002)
        assert lap.ms > 1
        assert _recorded_by_the_no_op() == ({}, {}, [], 0)

    def test_the_current_recorder_is_the_one_entered_in_this_thread_only(self):
        recorder = StepRecorder()
        seen = []
        assert current_recorder() is NO_RECORDER
        with recorder:
            assert current_recorder() is recorder
            thread = threading.Thread(target=lambda: seen.append(current_recorder()))
            thread.start()
            thread.join()
        assert seen == [NO_RECORDER] and current_recorder() is NO_RECORDER

    def test_the_current_recorder_is_reset_when_the_block_raises(self):
        with pytest.raises(RuntimeError):
            with StepRecorder():
                raise RuntimeError("boom")
        assert current_recorder() is NO_RECORDER

    def test_a_timer_costs_well_under_a_few_microseconds(self):
        # Priced as the best of several runs, so a busy machine does not fail it; typically ~0.4 us. The bound is
        # loose on purpose: a slow CI runner or a tracing tool (coverage) multiplies it.
        runs, best = 20_000, float("inf")
        for _ in range(5):
            recorder = StepRecorder()
            started = time.perf_counter()
            for _ in range(runs):
                with recorder.step("analyze"):
                    pass
            best = min(best, (time.perf_counter() - started) / runs)
        assert best < 20e-6


@pytest.fixture(scope="module")
def engine():
    return PiiShieldEngine()


@pytest.fixture(scope="module")
def onnx(engine):
    """Whether the NER model is the ONNX one: only its forward passes are timed (other NLP engines have no hook)."""
    return bool(getattr(engine._analyzer.nlp_engine, "_pipelines", None))


@pytest.fixture(scope="module")
def forward_model(engine, onnx):
    """The Hugging Face model of the ONNX NER engine, whose ``forward`` the timing wraps."""
    if not onnx:
        pytest.skip("needs NLP_ENGINE=onnx")
    return engine._analyzer.nlp_engine._pipelines[0].model


@pytest.fixture()
def forward_calls(forward_model, monkeypatch):
    """Counts the forward passes per thread, outside the timing wrapper."""
    calls = collections.Counter()
    original = forward_model.forward

    def counting(*args, **kwargs):
        calls[threading.get_ident()] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(forward_model, "forward", counting)
    return calls


@pytest.fixture(params=("true", "false"))
def fast_paths(request, monkeypatch):
    """The engine with FAST_ANALYSIS on and off; the previous state of the class attribute is restored."""
    monkeypatch.setattr(EntityRecognizer, "remove_duplicates", EntityRecognizer.__dict__["remove_duplicates"])
    monkeypatch.setenv("FAST_ANALYSIS", request.param)
    fast_analysis.install()
    return request.param


def _spans(entities):
    return [(e.start, e.end, e.entity_type) for e in entities]


class TestStatsContract:
    @pytest.mark.parametrize("text", (PLAIN, ALL_CAPS, _bank_text(1200)), ids=("plain", "all-caps", "bank-1k"))
    def test_the_steps_details_and_timeline_follow_the_contract(self, engine, onnx, text):
        stats = engine.anonymize(text).stats
        assert stats.steps and set(stats.steps) <= set(LIBRARY_STEPS)
        assert list(stats.steps) == [s for s in LIBRARY_STEPS if s in stats.steps], "in execution order"
        for step in ("nlp_pass1", "analyze", "post_processing", "handling"):
            assert step in stats.steps
        assert ("recovery_pass2" in stats.steps) is stats.recovery_pass
        assert all(ms >= 0 for ms in stats.steps.values())
        for key in stats.details:
            step, _, detail = key.partition(".")
            assert step in stats.steps and detail in DETAIL_NAMES
        assert (stats.ner_windows > 0) is onnx

    def test_a_second_ner_pass_adds_a_step_only_when_it_ran(self, engine, onnx):
        plain, caps = engine.anonymize(PLAIN).stats, engine.anonymize(ALL_CAPS).stats
        assert (plain.recovery_pass, "recovery_pass2" in plain.steps) == (False, False)
        assert (caps.recovery_pass, "recovery_pass2" in caps.steps) == (True, True)
        if onnx:
            assert "recovery_pass2.ner_model" in caps.details and caps.ner_windows > plain.ner_windows

    def test_the_legacy_fields_equal_their_step(self, engine):
        for text in (PLAIN, ALL_CAPS):
            stats = engine.anonymize(text).stats
            assert stats.nlp_ms == stats.steps["nlp_pass1"]
            assert stats.recovery_ms == stats.steps.get("recovery_pass2", 0)
            assert stats.anonymize_ms == stats.steps["handling"]

    def test_the_steps_tile_the_call(self, engine):
        text = _bank_text(1200)
        assert len(text) >= 1000
        engine.anonymize(text)  # warm
        stats = engine.anonymize(text).stats
        total = sum(stats.steps.values())
        assert total <= stats.total_ms + EPSILON
        assert total >= 0.9 * stats.total_ms

    def test_the_timeline_lists_the_steps_in_order_without_overlap(self, engine):
        stats = engine.anonymize(ALL_CAPS).stats
        assert [name for name, _, _ in stats.timeline] == list(stats.steps)
        end = 0.0
        for name, offset, duration in stats.timeline:
            assert offset >= end - EPSILON and duration == pytest.approx(stats.steps[name])
            end = offset + duration
        assert end <= stats.total_ms + EPSILON

    def test_the_details_are_in_step_order_and_inside_their_step(self, engine):
        stats = engine.anonymize(ALL_CAPS).stats
        expected = [f"{s}.{d}" for s in stats.steps for d in DETAIL_NAMES if f"{s}.{d}" in stats.details]
        assert list(stats.details) == expected
        for key, ms in stats.details.items():
            assert 0 <= ms <= stats.steps[key.partition(".")[0]] + EPSILON

    def test_the_event_carries_a_copy_of_the_steps(self, engine):
        events = []
        stats = engine.anonymize(PLAIN, on_event=events.append).stats
        assert events[0].steps == stats.steps and events[0].steps is not stats.steps

    def test_recording_changes_no_entity(self, engine):
        text = _bank_text(1200)
        assert _spans(engine.anonymize(text).entities) == _spans(engine.detect(text))

    def test_detect_records_nothing(self, engine):
        engine.detect(ALL_CAPS)
        assert _recorded_by_the_no_op() == ({}, {}, [], 0)
        assert current_recorder() is NO_RECORDER

    def test_a_call_run_in_a_copied_context_leaves_the_callers_context_alone(self, engine):
        # How the API runs a detection on its thread pool: ``copy_context().run(engine.anonymize, ...)``.
        context = contextvars.copy_context()
        stats = context.run(lambda: engine.anonymize(PLAIN).stats)
        assert stats.steps
        assert current_recorder() is NO_RECORDER and context.run(current_recorder) is NO_RECORDER

    def test_a_failing_detection_leaves_no_recorder_behind(self, engine, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("analyzer down")

        monkeypatch.setattr(engine._analyzer, "analyze", boom)
        with pytest.raises(DetectionError):
            engine.anonymize(PLAIN)
        assert current_recorder() is NO_RECORDER

    def test_the_conversation_anonymizer_fills_the_same_stats(self, engine):
        conversation = ConversationAnonymizer(engine, "steps-1", store=InMemorySessionStore())
        stats = conversation.anonymize(ALL_CAPS).stats
        assert set(stats.steps) <= set(LIBRARY_STEPS) and {"nlp_pass1", "analyze", "handling"} <= set(stats.steps)
        assert ("recovery_pass2" in stats.steps) is stats.recovery_pass is True
        assert stats.nlp_ms == stats.steps["nlp_pass1"] and stats.anonymize_ms == stats.steps["handling"]
        assert sum(stats.steps.values()) <= stats.total_ms + EPSILON


class TestDetails:
    def test_dedup_context_scoring_and_the_model_are_measured_with_the_fast_paths_on_and_off(
        self, engine, onnx, fast_paths
    ):
        stats = engine.anonymize(ALL_CAPS).stats
        keys = [f"{step}.{detail}" for step in ("analyze", "recovery_pass2") for detail in ("context_enhance", "dedup")]
        if onnx:
            keys += ["nlp_pass1.ner_model", "recovery_pass2.ner_model"]
        for key in keys:
            assert stats.details.get(key, 0) > 0, key

    def test_with_the_fast_paths_off_the_results_are_the_same(self, engine, monkeypatch):
        text = _bank_text(1200)
        fast = _spans(engine.anonymize(text).entities)
        monkeypatch.setattr(EntityRecognizer, "remove_duplicates", EntityRecognizer.__dict__["remove_duplicates"])
        monkeypatch.setenv("FAST_ANALYSIS", "false")
        fast_analysis.install()
        assert _spans(engine.anonymize(text).entities) == fast

    def test_the_windows_are_the_forward_passes_of_every_ner_pass(self, engine, forward_calls):
        text = _bank_text(2500)
        stats = engine.anonymize(text).stats
        assert stats.ner_windows == sum(forward_calls.values()) > 2
        assert stats.details["nlp_pass1.ner_model"] <= stats.steps["nlp_pass1"]

    def test_two_threads_running_different_texts_each_get_their_own_numbers(self, engine, forward_calls):
        doc = _bank_text(2500)
        started = threading.Event()
        long_call = {}

        def run_long_call():
            long_call["thread"] = threading.get_ident()
            started.set()
            long_call["stats"] = engine.anonymize(doc).stats

        worker = threading.Thread(target=run_long_call)
        worker.start()
        started.wait()
        # These run on this thread while the long call is (very likely) still in flight on the other.
        short = [engine.anonymize(text).stats for text in (PLAIN, ALL_CAPS) * 3]
        worker.join()

        long_stats = long_call["stats"]
        assert long_stats.ner_windows == forward_calls[long_call["thread"]] > 2
        assert sum(s.ner_windows for s in short) == forward_calls[threading.get_ident()]
        assert [s.ner_windows for s in short] == [1, 2] * 3
        assert ["recovery_pass2" in s.steps for s in short] == [False, True] * 3


def test_the_library_never_imports_opentelemetry():
    """The library only returns numbers; ``app/`` turns them into spans and metrics."""
    imported = []
    for path in sorted(pathlib.Path(pii_shield.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                imported += [(path.name, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.append((path.name, node.module))
    assert [(file, name) for file, name in imported if name.split(".")[0] == "opentelemetry"] == []
