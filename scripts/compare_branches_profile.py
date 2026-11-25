"""Per-step profile of one ``PiiShieldEngine.anonymize()`` call, for ``scripts/compare_branches.py``.

It runs inside the app image with the code of the git ref under test mounted over ``/app``, so it uses only the
standard library and the repository's own modules, and it prints one JSON document on stdout (progress and
library noise go to stderr).  It can also be run by hand in any environment that can import ``pii_shield``:

    python scripts/compare_branches_profile.py --docs documents.json --reps 5

``documents.json`` is ``{"profile": {name: text}, "check": {name: text}}``: the texts to time, and the texts whose
detected entities are recorded for the correctness comparison.

Timers are attached by monkey-patching, so the profile works on any ref, including one without the library's own
step timing.  Every timer records its *exclusive* time (a per-thread stack subtracts the time spent in nested
timers), which makes the steps add up to the wall time of the call.  A timer whose target does not exist in a
ref is skipped and named in ``notes``; its time then shows up in the enclosing step.
"""

from __future__ import annotations

import argparse
import collections
import functools
import json
import os
import statistics
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

_LOCAL = threading.local()
_EXCLUSIVE_MS: dict[str, float] = collections.defaultdict(float)
_CALLS: collections.Counter = collections.Counter()

# Post-processing functions of ``pii_shield.engine``, and the timer name each reports under; one that a ref does not
# have (the gate helpers, noise shortening) is skipped.
_PIPELINE_FUNCTIONS = {
    "normalize_case": "recovery.logic",
    "needs_name_recovery": "recovery.logic",
    "has_recoverable_case_change": "recovery.logic",
    "merge_recovered_results": "recovery.logic",
    "shorten_noise_for_ner": "glue.noise_shortening",
    "ordinary_word_starts": "glue.pos_helpers",
    "common_word_starts": "glue.pos_helpers",
    "normalize_person_titles": "post.normalize_person_titles",
    "split_at_line_breaks": "post.split_at_line_breaks",
    "prefer_line_context": "post.prefer_line_context",
    "remove_allowed_overlaps": "post.remove_allowed_overlaps",
    "reclassify_person_as_location": "post.reclassify_person_as_location",
    "merge_adjacent_person_tokens": "post.merge_adjacent_person_tokens",
    "extend_person_over_initials": "post.extend_person_over_initials",
    "filter_person_false_positives": "post.filter_person_false_positives",
    "merge_address_entities": "post.merge_address_entities",
    "filter_attributive_nrp": "post.filter_attributive_nrp",
    "remove_overlapping": "post.remove_overlapping",
    "reclassify_phone_as_bank_account": "post.reclassify_phone_as_bank_account",
    "is_valid_datetime": "post.is_valid_datetime",
}

_HANDLING_METHODS = {"_assign_replacements": "handling.assign_replacements", "_build_text": "handling.build_text"}

# Report steps in pipeline order: (label, predicate on the timer name).  The first predicate that matches wins, and
# a timer that matches none is reported as "(unassigned)", so the steps always add up.
STEPS: list[tuple[str, Callable[[str], bool]]] = [
    ("NER model forward (ONNX), pass 1", lambda n: n == "nlp.ner_onnx_forward.p1"),
    ("NER tokenize + decode, pass 1", lambda n: n == "nlp.ner_tokenize_decode.p1"),
    ("spaCy tokenize/tag/lemma + artifacts, pass 1", lambda n: n == "nlp.spacy_artifacts.p1"),
    ("Regex / validator recognizers", lambda n: n.startswith("recognizer.") and n.endswith(".p1")),
    ("NER read-out recognizer", lambda n: n.startswith("ner_readout.") and n.endswith(".p1")),
    ("Context scoring (enhancer)", lambda n: n == "context.enhance.p1"),
    ("Dedup (per-recognizer + final)", lambda n: n.startswith("dedup.") and n.endswith(".p1")),
    ("Presidio glue (registry, threshold, allow-list)", lambda n: n == "presidio.analyze_glue.p1"),
    ("2nd NER pass, re-cased text (all of it)", lambda n: n.endswith(".p2")),
    ("Case-recovery logic (normalize, merge)", lambda n: n.startswith("recovery.")),
    ("Post-processing (merge/split/filters)", lambda n: n.startswith("post.")),
    ("Handling (replace, build text)", lambda n: n.startswith("handling.")),
    ("Glue (detect/anonymize self, noise, POS)", lambda n: n.startswith("glue.")),
]
UNASSIGNED = "(unassigned)"


def log(message: str) -> None:
    print(f"[profile] {message}", file=sys.stderr, flush=True)


def _stack() -> list:
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = _LOCAL.stack = []
    return stack


def timed(name, function):
    """Wrap *function* so its exclusive time is added to ``_EXCLUSIVE_MS[name]``.

    *name* is a string, or a callable evaluated on entry (the phase and the parent timer are only known then).
    """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        stack = _stack()
        stack.append([name() if callable(name) else name, time.perf_counter(), 0.0])
        try:
            return function(*args, **kwargs)
        finally:
            label, started, nested = stack.pop()
            elapsed = time.perf_counter() - started
            _EXCLUSIVE_MS[label] += (elapsed - nested) * 1000.0
            _CALLS[label] += 1
            if stack:
                stack[-1][2] += elapsed

    return wrapper


def _phase() -> str:
    return getattr(_LOCAL, "phase", "p1")


def install(engine, engine_module, notes: list[str]) -> None:
    """Attach the timers to *engine* and to the process-wide objects it uses.

    Each group of timers is attached on its own: a ref that lacks what one group needs loses that group (it is named
    in *notes*, and its time lands in the enclosing step) instead of making the whole profile fail.
    """
    analyzer = engine._analyzer
    nlp_engine = analyzer.nlp_engine
    engine_class = type(engine)

    def starts_request(function):
        """Back to phase one at the start of every request (the second process_text call flips it to phase two)."""

        @functools.wraps(function)
        def wrapper(self, *args, **kwargs):
            _LOCAL.phase, _LOCAL.process_text_calls = "p1", 0
            return function(self, *args, **kwargs)

        return wrapper

    def request() -> None:
        engine_class.anonymize = timed("glue.anonymize_self", starts_request(engine_class.anonymize))
        engine_class._run_pipeline = timed("glue.detect_self", starts_request(engine_class._run_pipeline))

    def nlp_pass() -> None:
        process_text = nlp_engine.process_text

        def label() -> str:
            # The second process_text call of a request is the case-recovery pass.  The phase flips before the timer
            # is named, so the spaCy work of pass 2 is not counted under pass 1; the analyze() of pass 2 (whose
            # argument is that call) then runs in phase two as well.
            _LOCAL.process_text_calls = getattr(_LOCAL, "process_text_calls", 0) + 1
            if _LOCAL.process_text_calls == 2:
                _LOCAL.phase = "p2"
            return "nlp.spacy_artifacts." + _phase()

        nlp_engine.process_text = timed(label, process_text)

    def ner_model() -> None:
        from spacy_huggingface_pipelines.token_classification import HFTokenPipe

        model = dict(nlp_engine.nlp["en"].pipeline)["onnx_ner"].hf_pipeline.model  # raises for another NLP engine
        HFTokenPipe.__call__ = timed(lambda: "nlp.ner_tokenize_decode." + _phase(), HFTokenPipe.__call__)
        model.forward = timed(lambda: "nlp.ner_onnx_forward." + _phase(), model.forward)

    def analyze_glue() -> None:
        analyzer.analyze = timed(lambda: "presidio.analyze_glue." + _phase(), analyzer.analyze)

    def context_scoring() -> None:
        analyzer._enhance_using_context = timed(lambda: "context.enhance." + _phase(), analyzer._enhance_using_context)

    def recognizers() -> None:
        for recognizer in analyzer.registry.recognizers:
            reads_ner = any(cls.__name__ == "SpacyRecognizer" for cls in type(recognizer).__mro__)
            prefix = "ner_readout" if reads_ner else "recognizer"
            recognizer.analyze = timed(lambda r=recognizer, p=prefix: f"{p}.{r.name}.{_phase()}", recognizer.analyze)

    def dedup() -> None:
        from presidio_analyzer.entity_recognizer import EntityRecognizer

        remove_duplicates = EntityRecognizer.remove_duplicates

        def label() -> str:
            stack = _stack()
            inside_recognizer = bool(stack) and stack[-1][0].startswith(("recognizer.", "ner_readout."))
            return ("dedup.per_recognizer." if inside_recognizer else "dedup.final.") + _phase()

        EntityRecognizer.remove_duplicates = staticmethod(timed(label, remove_duplicates))

    def pipeline_functions() -> None:
        for function_name, label in _PIPELINE_FUNCTIONS.items():
            original = getattr(engine_module, function_name, None)
            if original is not None:  # the gate and noise helpers exist only in newer refs
                setattr(engine_module, function_name, timed(label, original))

    def handling() -> None:
        for method, label in _HANDLING_METHODS.items():
            original = getattr(engine_class, method)
            if isinstance(engine_class.__dict__.get(method), staticmethod):
                original = staticmethod(timed(label, original))
            else:
                original = timed(label, original)
            setattr(engine_class, method, original)

    groups = (("request", request), ("spaCy", nlp_pass), ("NER model", ner_model), ("Presidio analyze", analyze_glue),
              ("context scoring", context_scoring), ("recognizer", recognizers), ("dedup", dedup),
              ("pipeline function", pipeline_functions), ("handling", handling))
    for what, attach in groups:
        try:
            attach()
        except Exception as exc:
            notes.append(f"{what} timers not installed ({type(exc).__name__}: {exc})")


def group_timers(exclusive_ms: dict[str, float]) -> dict[str, float]:
    """Sum the per-timer exclusive times into the report steps (see ``STEPS``)."""
    grouped = collections.OrderedDict((label, 0.0) for label, _ in STEPS)
    for name, ms in exclusive_ms.items():
        for label, matches in STEPS:
            if matches(name):
                grouped[label] += ms
                break
        else:
            grouped[UNASSIGNED] = grouped.get(UNASSIGNED, 0.0) + ms
    return dict(grouped)


def timer_overhead_us() -> float:
    """Cost of one timer (enter + leave) in microseconds, to judge how much the timers distort short steps."""
    noop = timed("noop", lambda: None)
    plain = lambda: None  # noqa: E731
    count = 100_000
    started = time.perf_counter()
    for _ in range(count):
        noop()
    wrapped = time.perf_counter() - started
    started = time.perf_counter()
    for _ in range(count):
        plain()
    bare = time.perf_counter() - started
    _EXCLUSIVE_MS.pop("noop", None)
    _CALLS.pop("noop", None)
    return max(0.0, (wrapped - bare) / count * 1e6)


def entity_list(result_entities) -> list[list]:
    """``[start, end, type, score]`` per entity, in position order."""
    rows = [[e.start, e.end, e.entity_type, round(float(e.score), 4)] for e in result_entities]
    return sorted(rows, key=lambda row: (row[0], row[1], row[2]))


def run_unwrapped(engine, config, text: str, reps: int) -> tuple[float, int]:
    """Median wall time (ms) of ``anonymize`` without timers, after one warm-up call, and the entity count."""
    result = engine.anonymize(text, config=config)
    times = []
    for _ in range(reps):
        started = time.perf_counter()
        result = engine.anonymize(text, config=config)
        times.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(times), len(result.entities)


def run_profiled(engine, config, text: str, reps: int) -> dict:
    """Median per-step times over *reps* calls with the timers on (after one warm-up call)."""
    engine.anonymize(text, config=config)
    per_rep, walls, calls = [], [], []
    ner_runs, second_pass = 0, False
    for _ in range(reps):
        _EXCLUSIVE_MS.clear()
        _CALLS.clear()
        started = time.perf_counter()
        engine.anonymize(text, config=config)
        walls.append((time.perf_counter() - started) * 1000.0)
        per_rep.append(group_timers(_EXCLUSIVE_MS))
        calls.append(sum(_CALLS.values()))
        ner_runs = _CALLS["nlp.ner_onnx_forward.p1"] + _CALLS["nlp.ner_onnx_forward.p2"]
        second_pass = any(name.endswith(".p2") for name in _EXCLUSIVE_MS)
    steps = {label: statistics.median(rep.get(label, 0.0) for rep in per_rep) for label in per_rep[0]}
    return {
        "steps": steps,
        "timed_ms": statistics.median(walls),
        "timer_calls": int(statistics.median(calls)),
        "ner_runs": ner_runs,
        "second_pass": second_pass,
    }


def configure_environment(repo: Path) -> None:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.chdir(repo)
    sys.path.insert(0, str(repo))


def default_repo() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here if (here / "pii_shield").is_dir() else Path("/app")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", required=True, help='JSON file: {"profile": {name: text}, "check": {name: text}}')
    parser.add_argument("--reps", type=int, default=5, help="timed calls per text after one warm-up (default 5)")
    parser.add_argument("--repo", type=Path, default=None, help="root holding pii_shield/ and config/ (default /app)")
    args = parser.parse_args(argv)

    # Library and framework chatter must not corrupt the JSON on stdout.
    json_out, sys.stdout = sys.stdout, sys.stderr
    # Read the documents first: configure_environment changes the working directory.
    documents = json.loads(Path(args.docs).read_text(encoding="utf-8"))
    configure_environment((args.repo or default_repo()).resolve())

    import pii_shield.engine as engine_module
    from pii_shield.models import EntityConfig

    started = time.perf_counter()
    engine = engine_module.PiiShieldEngine()
    engine.detect("Warm up: John Smith at john@example.com")
    log(f"engine ready after {time.perf_counter() - started:.1f}s (python {sys.version.split()[0]}, "
        f"{os.cpu_count()} cpus visible)")
    config = EntityConfig()

    entities = {}
    for name, text in documents.get("check", {}).items():
        entities[name] = entity_list(engine.detect(text))
    log(f"recorded the entities of {len(entities)} documents")

    profile_texts = documents.get("profile", {})
    unwrapped = {}
    for name, text in profile_texts.items():
        unwrapped[name] = run_unwrapped(engine, config, text, args.reps)
        log(f"{name}: {unwrapped[name][0]:,.0f} ms without timers ({len(text):,} chars)")

    notes: list[str] = []
    install(engine, engine_module, notes)
    overhead = timer_overhead_us()
    profiles = {}
    for name, text in profile_texts.items():
        total_ms, entity_count = unwrapped[name]
        profiled = run_profiled(engine, config, text, args.reps)
        profiled.update(chars=len(text), entities=entity_count, total_ms=total_ms)
        profiles[name] = profiled
        covered = sum(profiled["steps"].values())
        log(f"{name}: steps add up to {covered:,.0f} ms of {profiled['timed_ms']:,.0f} ms with timers")

    json_out.write(json.dumps({
        "python": sys.version.split()[0],
        "cpus_visible": os.cpu_count(),
        "step_order": [label for label, _ in STEPS],
        "timer_overhead_us": round(overhead, 2),
        "notes": notes,
        "documents": profiles,
        "entities": entities,
    }))
    json_out.write("\n")
    json_out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
