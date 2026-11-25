"""ONNX Runtime-accelerated NLP engine for Presidio.

Subclasses ``TransformersNlpEngine`` and replaces PyTorch inference with
ONNX Runtime via HuggingFace Optimum.  Provides 2-3× faster CPU inference
for BERT-based NER models.

When ``QUANTIZE_MODEL`` is set to ``true`` (the default), the engine looks
for a pre-quantized INT8 model in ``/app/models/onnx-int8``.  This yields
~2.5× faster inference and 4× smaller model compared to FP32.

Usage::

    NLP_ENGINE=onnx
    TRANSFORMERS_MODEL=protectai/bert-base-NER-onnx   # pre-exported ONNX model
"""

import logging
import os
import threading
import time
from pathlib import Path

import onnxruntime as ort
import spacy
import torch
from optimum.onnxruntime import ORTModelForTokenClassification
from spacy.language import Language
from spacy_huggingface_pipelines.token_classification import HFTokenPipe
from transformers import AutoTokenizer, pipeline
from transformers.modeling_outputs import TokenClassifierOutput

from presidio_analyzer.nlp_engine import TransformersNlpEngine

from pii_shield.observability import current_recorder

logger = logging.getLogger("pii-shield")

# Holds the pre-built HFTokenPipe so the spaCy factory can return it.
_onnx_pipe_instance: HFTokenPipe | None = None

# ONNX Runtime sessions this process inherited across fork() and replaced (see
# ``OnnxTransformersNlpEngine._ensure_own_session``).  They are kept referenced on purpose.
_INHERITED_MODELS: list = []

if not Language.has_factory("onnx_ner"):
    @Language.factory("onnx_ner")
    def _create_onnx_ner(nlp, name):
        if _onnx_pipe_instance is None:
            raise RuntimeError("ONNX NER pipe not initialised — call load() first")
        return _onnx_pipe_instance

# Default path where the Docker build stores the INT8-quantized model.
_QUANTIZED_MODEL_DIR = os.getenv("QUANTIZED_MODEL_DIR", "/app/models/onnx-int8")


def _is_onnx_model(model_name_or_path: str) -> bool:
    """Check if the model is already in ONNX format."""
    if os.path.isdir(model_name_or_path):
        return any(f.endswith(".onnx") for f in os.listdir(model_name_or_path))
    return "-onnx" in model_name_or_path.lower()


def _onnx_file(ort_model) -> Path:
    """The .onnx file an Optimum model was loaded from (the attribute name differs between versions)."""
    for attr in ("path", "model_path"):
        path = getattr(ort_model, attr, None)
        if path:
            return Path(path)
    raise RuntimeError("Cannot tell which .onnx file the ONNX Runtime model was loaded from")


def _trim_padding_enabled() -> bool:
    return os.getenv("ONNX_TRIM_PADDING", "true").strip().lower() == "true"


def _trim_padding(model) -> None:
    """Make *model* run every window at its real length instead of the longest window's.

    The Hugging Face pipeline splits a long text into windows of up to 512 tokens and pads all of them to the
    longest one, so the last, shorter window is computed as if it were full: a 3 KB text spends a quarter of
    its model time on padding.  The padding sits at the end and is masked, so it is cut off before the model
    runs and the logits are zero-padded back to the width the pipeline expects; it ignores those positions.
    A text that fits one window has no padding and is left alone.  (The padding is not entirely neutral in an
    INT8 model, whose activation ranges include it, so the last window of a long text can differ marginally.)
    """
    forward = model.forward

    def trimmed(input_ids=None, attention_mask=None, token_type_ids=None, **kwargs):
        untouched = (input_ids, attention_mask, token_type_ids)
        if (
            attention_mask is None
            or not isinstance(input_ids, torch.Tensor)
            or kwargs.get("return_dict") is False
        ):
            return forward(*untouched, **kwargs)
        width = input_ids.shape[-1]
        length = int(attention_mask.sum(dim=-1).max())
        if length >= width or attention_mask[..., length:].any():
            return forward(*untouched, **kwargs)
        out = forward(
            input_ids[..., :length],
            attention_mask[..., :length],
            None if token_type_ids is None else token_type_ids[..., :length],
            **kwargs,
        )
        logits = out["logits"] if isinstance(out, dict) else out[0]
        padded = logits.new_zeros((logits.shape[0], width, logits.shape[-1]))
        padded[:, :length] = logits
        return TokenClassifierOutput(logits=padded)

    model.forward = trimmed


def _time_forward(model) -> None:
    """Report every forward pass of *model* to the recorder of the running ``anonymize()`` call.

    One pass is one window of up to 512 tokens, so this yields the ``ner_model`` time and the window count of
    the step that is running.  Outside such a call the recorder is the no-op one and the cost is two clock reads.
    """
    forward = model.forward

    def timed(*args, **kwargs):
        recorder = current_recorder()
        started = time.perf_counter()
        try:
            return forward(*args, **kwargs)
        finally:
            recorder.add_detail("ner_model", (time.perf_counter() - started) * 1000.0)
            recorder.add_windows()

    model.forward = timed


def _prepare_model(model) -> None:
    """Wrap the forward pass of *model*: cut the padding off (when enabled), outermost the timing."""
    if _trim_padding_enabled():
        _trim_padding(model)
    _time_forward(model)


def _build_session_options() -> ort.SessionOptions:
    """Build tuned ONNX Runtime session options for concurrent inference.

    With multiple NLP threads sharing the CPU, limiting intra-op threads
    prevents internal contention while still allowing SIMD parallelism.
    """
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = int(os.getenv("ORT_INTRA_OP_THREADS", "2"))
    opts.inter_op_num_threads = int(os.getenv("ORT_INTER_OP_THREADS", "1"))
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return opts


class OnnxTransformersNlpEngine(TransformersNlpEngine):
    """TransformersNlpEngine backed by ONNX Runtime instead of PyTorch.

    **Fork safety.**  ONNX Runtime starts its intra-op threads when the session is created.  A process
    forked afterwards (``gunicorn --preload``, a ``ProcessPoolExecutor``) inherits the session's
    bookkeeping but none of its threads, so every inference there runs on the calling thread alone:
    about twice as slow with the default two intra-op threads.  The first inference in such a process
    therefore builds a session of its own, see ``_ensure_own_session``.
    """

    engine_name = "onnx"
    is_available = True

    _owner_pid: int | None = None
    _pipelines: tuple = ()
    _rebuild_lock = None  # a threading.Lock once loaded

    def load(self) -> None:
        """Load spaCy tokenizer + ONNX Runtime NER model."""
        global _onnx_pipe_instance

        logger.info("Loading ONNX-accelerated NER engine")
        self.nlp = {}
        self._pipelines = []
        self._rebuild_lock = threading.Lock()
        self._owner_pid = os.getpid()

        for model_conf in self.models:
            self._validate_model_params(model_conf)
            spacy_model = model_conf["model_name"]["spacy"]
            transformers_model = model_conf["model_name"]["transformers"]

            self._download_spacy_model_if_needed(spacy_model)
            nlp = spacy.load(spacy_model, disable=["parser", "ner"])

            session_options = _build_session_options()
            use_quantized = os.getenv("QUANTIZE_MODEL", "true").lower() == "true"

            if use_quantized and os.path.isdir(_QUANTIZED_MODEL_DIR):
                logger.info("Loading INT8-quantized ONNX model from %s", _QUANTIZED_MODEL_DIR)
                ort_model = ORTModelForTokenClassification.from_pretrained(
                    _QUANTIZED_MODEL_DIR,
                    file_name="model_quantized.onnx",
                    session_options=session_options,
                    provider="CPUExecutionProvider",
                )
                tokenizer = AutoTokenizer.from_pretrained(_QUANTIZED_MODEL_DIR)
            else:
                if use_quantized:
                    logger.warning(
                        "Quantized model not found at %s — falling back to FP32",
                        _QUANTIZED_MODEL_DIR,
                    )
                ort_model = ORTModelForTokenClassification.from_pretrained(
                    transformers_model,
                    export=not _is_onnx_model(transformers_model),
                    session_options=session_options,
                    provider="CPUExecutionProvider",
                )
                tokenizer = AutoTokenizer.from_pretrained(transformers_model)

            _prepare_model(ort_model)
            hf_pipe = pipeline(
                task="token-classification",
                model=ort_model,
                tokenizer=tokenizer,
                aggregation_strategy=self.ner_model_configuration.aggregation_strategy,
                stride=self.ner_model_configuration.stride,
            )
            self._pipelines.append(hf_pipe)

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

    def _ensure_own_session(self) -> None:
        """Give a process forked after ``load()`` its own ONNX Runtime session.

        A no-op in the process that loaded the model.  In a forked one the inherited session is
        replaced by a new session over the same .onnx file, whose threads do exist.  The inherited
        session is kept alive rather than released: destroying it in the child waits for threads that
        were never copied into it, and hangs.
        """
        if self._owner_pid is None or os.getpid() == self._owner_pid:
            return
        with self._rebuild_lock:
            if os.getpid() == self._owner_pid:
                return
            logger.info(
                "Process %d inherited the ONNX Runtime session across fork(); building its own",
                os.getpid(),
            )
            for hf_pipe in self._pipelines:
                inherited = hf_pipe.model
                _INHERITED_MODELS.append(inherited)
                model_file = _onnx_file(inherited)
                hf_pipe.model = ORTModelForTokenClassification.from_pretrained(
                    model_file.parent,
                    file_name=model_file.name,
                    session_options=_build_session_options(),
                    provider="CPUExecutionProvider",
                )
                _prepare_model(hf_pipe.model)
            self._owner_pid = os.getpid()

    def process_text(self, text: str, language: str):
        self._ensure_own_session()
        return super().process_text(text, language)

    def process_batch(self, texts, language, batch_size=1, n_process=1, as_tuples=False):
        self._ensure_own_session()
        return super().process_batch(
            texts, language, batch_size=batch_size, n_process=n_process, as_tuples=as_tuples
        )
