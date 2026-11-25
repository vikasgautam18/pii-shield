"""Time ONNX NER models on this CPU, window by window.

Which model file is fastest depends on the CPU (INT8 needs VNNI or AVX2 to win; an FP32 or fused graph can
beat it), so measure on the hardware the API runs on, for example inside a replica:

    python scripts/bench_onnx_models.py plain=/app/models/onnx-int8/model_quantized.onnx \
        fused=/path/to/fused/model_quantized.onnx --threads 2

Prints the median latency of one window of 128, 256 and 512 tokens and the speed-up over the first model.
The models' outputs differ slightly; compare them with the test suites, not with this script.
"""

import argparse
import statistics
import time

import numpy as np
import onnxruntime as ort

LENGTHS = (128, 256, 512)


def median_ms(path: str, threads: int, length: int, repeats: int) -> float:
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}
    rng = np.random.default_rng(0)
    inputs = {
        "input_ids": rng.integers(1000, 20000, size=(1, length), dtype=np.int64),
        "attention_mask": np.ones((1, length), dtype=np.int64),
        "token_type_ids": np.zeros((1, length), dtype=np.int64),
    }
    inputs = {k: v for k, v in inputs.items() if k in names}
    session.run(None, inputs)
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        session.run(None, inputs)
        times.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="+", metavar="label=path.onnx")
    parser.add_argument("--threads", type=int, default=2, help="ONNX Runtime intra-op threads (default 2)")
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()

    baseline: dict[int, float] = {}
    print(f"{'model':20s}" + "".join(f"{f'{n} tokens':>18s}" for n in LENGTHS) + f"   threads={args.threads}")
    for spec in args.models:
        label, _, path = spec.partition("=")
        cells = []
        for length in LENGTHS:
            ms = median_ms(path, args.threads, length, args.repeats)
            baseline.setdefault(length, ms)
            cells.append(f"{ms:9.1f} ms ({baseline[length] / ms:4.2f}x)")
        print(f"{label:20s}" + "".join(f"{c:>18s}" for c in cells))


if __name__ == "__main__":
    main()
