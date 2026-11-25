"""Quantize an ONNX NER model to INT8 (dynamic, AVX2).

Called at Docker build time to create a smaller, faster model.

Usage:
    python scripts/quantize_model.py <model_name_or_path> <output_dir> [--fuse]

``--fuse`` (or ``ONNX_FUSE=true``) first fuses the BERT graph (attention, LayerNorm, GELU) with ONNX Runtime's
transformer optimizer and quantizes the fused graph.  On an Apple M4 that ran a 512-token window 1.56x faster
than the plain INT8 model, but the quantization error differs, so about one text in ten gets slightly different
entity spans, and how much it gains on an x86 CPU depends on the CPU: measure it there with
``scripts/bench_onnx_models.py`` and run the test suites before using it.  Off by default.
"""

import json
import os
import shutil
import sys

from optimum.onnxruntime import ORTModelForTokenClassification, ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig
from transformers import AutoTokenizer


def _quantize_fused(fp32_dir: str, out: str) -> None:
    """Fuse the BERT graph in *fp32_dir*, then quantize it with the settings of ``AutoQuantizationConfig.avx2``."""
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.transformers import optimizer
    from optimum.onnxruntime.configuration import ORT_DEFAULT_OPS_DYNAMIC_QUANTIZATION

    with open(os.path.join(fp32_dir, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    if config.get("model_type") not in ("bert", "roberta"):
        sys.exit(f"--fuse supports BERT-style models only, not model_type={config.get('model_type')!r}")
    fused = optimizer.optimize_model(
        os.path.join(fp32_dir, "model.onnx"),
        model_type="bert",
        num_heads=config["num_attention_heads"],
        hidden_size=config["hidden_size"],
        opt_level=0,
    )
    fused_path = os.path.join(fp32_dir, "model_fused.onnx")
    fused.save_model_to_file(fused_path)
    os.makedirs(out, exist_ok=True)
    quantize_dynamic(
        fused_path,
        os.path.join(out, "model_quantized.onnx"),
        op_types_to_quantize=ORT_DEFAULT_OPS_DYNAMIC_QUANTIZATION,
        per_channel=True,
        reduce_range=False,
        weight_type=QuantType.QInt8,
        extra_options={
            "WeightSymmetric": True,
            "ActivationSymmetric": False,
            "DefaultTensorType": onnx.TensorProto.FLOAT,
        },
    )
    shutil.copy(os.path.join(fp32_dir, "config.json"), os.path.join(out, "config.json"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fuse = "--fuse" in sys.argv[1:] or os.getenv("ONNX_FUSE", "false").strip().lower() == "true"
    model = args[0]
    out = args[1]
    local = "/tmp/onnx-fp32"

    print(f"Quantizing {model} to INT8{' (fused graph)' if fuse else ''}...")
    m = ORTModelForTokenClassification.from_pretrained(model)
    m.save_pretrained(local)
    AutoTokenizer.from_pretrained(model).save_pretrained(local)

    if fuse:
        _quantize_fused(local, out)
    else:
        ORTQuantizer.from_pretrained(local).quantize(
            save_dir=out,
            quantization_config=AutoQuantizationConfig.avx2(is_static=False),
        )
    AutoTokenizer.from_pretrained(model).save_pretrained(out)

    shutil.rmtree(local)
    print("INT8 quantization complete")


if __name__ == "__main__":
    main()
