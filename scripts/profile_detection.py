"""Show where the time of one detection goes, for a text of your own.

    python scripts/profile_detection.py --file slow_document.txt
    python scripts/profile_detection.py --text "Dear RAJESH KUMAR, mail rajesh@example.com" --repeat 5

Runs the real engine with your environment (``NLP_ENGINE``, ``DISABLED_RECOGNIZERS``, ``CUSTOM_RECOGNIZERS_FILE``,
...) and prints, as medians over the repeats: the time of the first NLP pass and of the case-recovery pass (the
second trip through the NER model, run for ALL-CAPS and uncased names), of every recognizer and of the context
enhancer and duplicate removal, and how many NER windows the model ran.  A recognizer that costs more than a few
milliseconds is a slow regular expression; model time that grows with the text is the cost of the NER model itself.
"""

import argparse
import collections
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from presidio_analyzer.entity_recognizer import EntityRecognizer  # noqa: E402

from pii_shield.engine import PiiShieldEngine  # noqa: E402


def _timed(label, function, sink):
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            sink[label].append((time.perf_counter() - started) * 1000.0)

    return wrapper


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="text file to analyse")
    source.add_argument("--text", help="text to analyse")
    parser.add_argument("--repeat", type=int, default=3, help="timed runs after one warm-up (default 3)")
    parser.add_argument("--top", type=int, default=10, help="recognizers to list (default 10)")
    args = parser.parse_args()
    text = args.text if args.text is not None else open(args.file, encoding="utf-8").read()

    engine = PiiShieldEngine()
    sink: dict[str, list[float]] = collections.defaultdict(list)
    analyzer = engine._analyzer
    windows: list[int] = []
    for recognizer in analyzer.registry.recognizers:
        recognizer.analyze = _timed(f"recognizer {recognizer.name}", recognizer.analyze, sink)
    enhancer = analyzer.context_aware_enhancer
    enhancer.enhance_using_context = _timed("context enhancer", enhancer.enhance_using_context, sink)
    EntityRecognizer.remove_duplicates = staticmethod(
        _timed("duplicate removal", EntityRecognizer.remove_duplicates, sink)
    )
    pipe = dict(analyzer.nlp_engine.nlp["en"].pipeline).get("onnx_ner")
    if pipe is not None:
        model = pipe.hf_pipeline.model
        timed_forward = _timed("ONNX model windows", model.forward, sink)

        def counting(*args, **kwargs):
            windows.append(1)
            return timed_forward(*args, **kwargs)

        model.forward = counting

    engine.detect(text)
    runs = []
    for _ in range(args.repeat):
        sink.clear()
        windows.clear()
        started = time.perf_counter()
        entities = engine.detect(text)
        total = (time.perf_counter() - started) * 1000.0
        nlp_ms, recovery_ms, recovery_pass = engine._last_detection()
        runs.append((total, nlp_ms, recovery_ms, recovery_pass, len(windows), {k: sum(v) for k, v in sink.items()}))

    median = statistics.median
    total = median(r[0] for r in runs)
    print(f"{len(text)} characters, {len(entities)} entities, {total:.0f} ms per detection (median of {args.repeat})")
    print(f"  first NLP pass (tokenise, tag, NER)   {median(r[1] for r in runs):8.0f} ms")
    recovery = "ran" if runs[-1][3] else "skipped"
    print(f"  case-recovery pass ({recovery})".ljust(40) + f"{median(r[2] for r in runs):8.0f} ms")
    if windows or runs[-1][4]:
        print(f"  NER model windows of up to 512 tokens {runs[-1][4]:8d}")
    labels = {label for r in runs for label in r[5]}
    rows = sorted(((median(r[5].get(label, 0.0) for r in runs), label) for label in labels), reverse=True)
    print("  largest steps (inclusive: the model time sits inside the NLP passes):")
    shown = 0
    for ms, label in rows:
        if label.startswith("recognizer") and shown >= args.top:
            continue
        shown += label.startswith("recognizer")
        print(f"    {ms:8.1f} ms  {label}")


if __name__ == "__main__":
    main()
