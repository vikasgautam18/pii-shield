"""Quantize an ONNX NER model to INT8 (dynamic, AVX2).

Called at Docker build time to create a smaller, faster model.

Usage:
    python build/quantize_model.py <model_name_or_path> <output_dir>
"""

import shutil
import sys

from optimum.onnxruntime import ORTModelForTokenClassification, ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig
from transformers import AutoTokenizer


def main():
    model = sys.argv[1]
    out = sys.argv[2]
    local = "/tmp/onnx-fp32"

    print(f"Quantizing {model} to INT8...")
    m = ORTModelForTokenClassification.from_pretrained(model)
    m.save_pretrained(local)
    AutoTokenizer.from_pretrained(model).save_pretrained(local)

    ORTQuantizer.from_pretrained(local).quantize(
        save_dir=out,
        quantization_config=AutoQuantizationConfig.avx2(is_static=False),
    )
    AutoTokenizer.from_pretrained(model).save_pretrained(out)

    shutil.rmtree(local)
    print("INT8 quantization complete")


if __name__ == "__main__":
    main()
