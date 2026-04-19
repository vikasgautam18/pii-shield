"""ONNX Runtime-accelerated NLP engine for Presidio.

Subclasses ``TransformersNlpEngine`` and replaces PyTorch inference with
ONNX Runtime via HuggingFace Optimum.  Provides 2-3× faster CPU inference
for BERT-based NER models.

Usage::

    NLP_ENGINE=onnx
    TRANSFORMERS_MODEL=protectai/bert-base-NER-onnx   # pre-exported ONNX model
"""

import logging
import os

import spacy
from optimum.onnxruntime import ORTModelForTokenClassification
from spacy.language import Language
from spacy_huggingface_pipelines.token_classification import HFTokenPipe
from transformers import AutoTokenizer, pipeline

from presidio_analyzer.nlp_engine import TransformersNlpEngine

logger = logging.getLogger("pii-shield")

# Holds the pre-built HFTokenPipe so the spaCy factory can return it.
_onnx_pipe_instance: HFTokenPipe | None = None

if not Language.has_factory("onnx_ner"):
    @Language.factory("onnx_ner")
    def _create_onnx_ner(nlp, name):
        if _onnx_pipe_instance is None:
            raise RuntimeError("ONNX NER pipe not initialised — call load() first")
        return _onnx_pipe_instance


def _is_onnx_model(model_name_or_path: str) -> bool:
    """Check if the model is already in ONNX format."""
    if os.path.isdir(model_name_or_path):
        return any(f.endswith(".onnx") for f in os.listdir(model_name_or_path))
    return "-onnx" in model_name_or_path.lower()


class OnnxTransformersNlpEngine(TransformersNlpEngine):
    """TransformersNlpEngine backed by ONNX Runtime instead of PyTorch."""

    engine_name = "onnx"
    is_available = True

    def load(self) -> None:
        """Load spaCy tokenizer + ONNX Runtime NER model."""
        global _onnx_pipe_instance

        logger.info("Loading ONNX-accelerated NER engine")
        self.nlp = {}

        for model_conf in self.models:
            self._validate_model_params(model_conf)
            spacy_model = model_conf["model_name"]["spacy"]
            transformers_model = model_conf["model_name"]["transformers"]

            self._download_spacy_model_if_needed(spacy_model)
            nlp = spacy.load(spacy_model, disable=["parser", "ner"])

            # Load ONNX model via Optimum (auto-exports if not already ONNX)
            ort_model = ORTModelForTokenClassification.from_pretrained(
                transformers_model,
                export=not _is_onnx_model(transformers_model),
            )
            tokenizer = AutoTokenizer.from_pretrained(transformers_model)

            hf_pipe = pipeline(
                task="token-classification",
                model=ort_model,
                tokenizer=tokenizer,
                aggregation_strategy=self.ner_model_configuration.aggregation_strategy,
                stride=self.ner_model_configuration.stride,
            )

            # Build HFTokenPipe and register via factory so spaCy accepts it.
            _onnx_pipe_instance = HFTokenPipe(
                name="onnx_ner",
                hf_pipeline=hf_pipe,
                annotate="spans",
                annotate_spans_key=self.entity_key,
                alignment_mode=self.ner_model_configuration.alignment_mode,
            )
            nlp.add_pipe("onnx_ner", last=True)
            self.nlp[model_conf["lang_code"]] = nlp

        logger.info("ONNX NER engine loaded successfully")
