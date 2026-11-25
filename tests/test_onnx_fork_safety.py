"""A process forked after the ONNX model was loaded must build its own ONNX Runtime session.

ONNX Runtime starts its intra-op threads when a session is created.  gunicorn --preload (and any
fork-based pool) copies the session into the workers without those threads, so every inference there
used to run on the calling thread alone, about twice as slow with two intra-op threads.  These tests use
a stand-in model: they exercise the fork bookkeeping, not ONNX Runtime itself.
"""

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("optimum.onnxruntime")

from pii_shield import onnx_nlp_engine  # noqa: E402
from pii_shield.observability import StepRecorder  # noqa: E402
from pii_shield.onnx_nlp_engine import OnnxTransformersNlpEngine, _onnx_file  # noqa: E402


class _Recorder:
    """Stands in for ORTModelForTokenClassification.from_pretrained."""

    def __init__(self):
        self.calls = []

    def from_pretrained(self, path, **kwargs):
        self.calls.append((Path(path), kwargs))
        return SimpleNamespace(path=Path(path) / kwargs["file_name"], rebuilt=True, forward=lambda *a, **k: None)


def _engine(owner_pid):
    inherited = SimpleNamespace(path=Path("/models/ner/model_quantized.onnx"), rebuilt=False)
    pipe = SimpleNamespace(model=inherited)
    engine = OnnxTransformersNlpEngine.__new__(OnnxTransformersNlpEngine)
    engine._pipelines = [pipe]
    engine._rebuild_lock = threading.Lock()
    engine._owner_pid = owner_pid
    return engine, pipe, inherited


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(onnx_nlp_engine, "ORTModelForTokenClassification", rec)
    monkeypatch.setattr(onnx_nlp_engine, "_INHERITED_MODELS", [])
    return rec


def test_the_process_that_loaded_the_model_keeps_its_session(recorder):
    engine, pipe, inherited = _engine(os.getpid())
    engine._ensure_own_session()
    assert recorder.calls == [] and pipe.model is inherited


def test_a_forked_process_gets_a_new_session_over_the_same_file(recorder):
    engine, pipe, inherited = _engine(os.getpid() + 1)
    engine._ensure_own_session()
    assert len(recorder.calls) == 1
    path, kwargs = recorder.calls[0]
    assert path == Path("/models/ner") and kwargs["file_name"] == "model_quantized.onnx"
    assert kwargs["provider"] == "CPUExecutionProvider"
    assert kwargs["session_options"].intra_op_num_threads >= 1
    assert pipe.model.rebuilt and pipe.model is not inherited
    assert engine._owner_pid == os.getpid()


def test_the_inherited_session_is_kept_alive_not_destroyed(recorder):
    engine, _, inherited = _engine(os.getpid() + 1)
    engine._ensure_own_session()
    # Releasing an inherited session in the child hangs: it must stay referenced.
    assert any(m is inherited for m in onnx_nlp_engine._INHERITED_MODELS)


def test_concurrent_first_requests_build_the_session_once(recorder):
    engine, pipe, _ = _engine(os.getpid() + 1)
    start = threading.Barrier(8)

    def first_request():
        start.wait()
        engine._ensure_own_session()
        assert pipe.model.rebuilt

    threads = [threading.Thread(target=first_request) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(recorder.calls) == 1


def test_inference_entry_points_check_the_session_first(monkeypatch):
    seen = []
    engine, _, _ = _engine(os.getpid())
    monkeypatch.setattr(OnnxTransformersNlpEngine, "_ensure_own_session", lambda self: seen.append("ensure"))
    base = onnx_nlp_engine.TransformersNlpEngine
    monkeypatch.setattr(base, "process_text", lambda self, text, language: seen.append("text") or "artifacts")
    monkeypatch.setattr(base, "process_batch", lambda self, texts, language, **kw: seen.append("batch") or iter(()))
    assert engine.process_text("x", "en") == "artifacts"
    list(engine.process_batch(["x"], "en"))
    assert seen == ["ensure", "text", "ensure", "batch"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_real_fork_rebuilds_in_the_child_only(recorder):
    engine, pipe, inherited = _engine(os.getpid())
    pid = os.fork()
    if pid == 0:  # child: must rebuild; exit status tells the parent what happened
        code = 3
        try:
            engine._ensure_own_session()
            code = 0 if (pipe.model.rebuilt and len(recorder.calls) == 1) else 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert recorder.calls == [] and pipe.model is inherited, "the parent keeps its own session"


def test_the_model_rebuilt_after_a_fork_is_timed_too(recorder):
    engine, pipe, _ = _engine(os.getpid() + 1)
    engine._ensure_own_session()
    steps = StepRecorder()
    with steps, steps.step("nlp_pass1"):
        pipe.model.forward()
        pipe.model.forward()
    assert steps.ner_windows == 2 and steps.details["nlp_pass1.ner_model"] >= 0


def test_the_model_file_is_found_under_either_optimum_attribute():
    assert _onnx_file(SimpleNamespace(path="/a/b.onnx")) == Path("/a/b.onnx")
    assert _onnx_file(SimpleNamespace(model_path="/a/c.onnx")) == Path("/a/c.onnx")
    with pytest.raises(RuntimeError):
        _onnx_file(SimpleNamespace())
