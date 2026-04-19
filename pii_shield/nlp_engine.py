"""NLP engine factory for Presidio Analyzer.

Reads the ``NLP_ENGINE`` environment variable (loaded from ``.env`` via
python-dotenv) and returns the matching Presidio NLP engine.

Supported values:
    - ``spacy``  (default) — uses ``en_core_web_lg``
    - ``stanza`` — uses the Stanza ``en`` model
    - ``transformers`` — uses a HuggingFace token-classification model
      (default ``dslim/bert-base-NER``, configurable via ``TRANSFORMERS_MODEL``)
    - ``onnx`` — same as transformers but with ONNX Runtime (2-3× faster CPU inference)
"""

import logging
import os

from presidio_analyzer.nlp_engine import NlpEngine, NlpEngineProvider

logger = logging.getLogger("pii-shield")

SPACY_MODEL = "en_core_web_lg"
STANZA_MODEL = "en"

# Entity-type mapping — Stanza NER labels differ from spaCy's defaults.
_STANZA_ENTITY_MAPPING = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "NORP": "NRP",
    "LOC": "LOCATION",
    "GPE": "LOCATION",
    "LOCATION": "LOCATION",
    "ORG": "ORGANIZATION",
    "ORGANIZATION": "ORGANIZATION",
    "FAC": "LOCATION",
    "DATE": "DATE_TIME",
    "TIME": "DATE_TIME",
}

# Entity-type mapping for HuggingFace token-classification models.
# IndicNER and most CoNLL-based models use BIO labels (B-PER, I-PER, etc.).
# With aggregation_strategy="max", the pipeline strips B-/I- prefixes.
_TRANSFORMERS_ENTITY_MAPPING = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "LOC": "LOCATION",
    "GPE": "LOCATION",
    "LOCATION": "LOCATION",
    "ORG": "ORGANIZATION",
    "ORGANIZATION": "ORGANIZATION",
    "MISC": "NRP",
    "DATE": "DATE_TIME",
    "TIME": "DATE_TIME",
}

_VALID_ENGINES = ("spacy", "stanza", "transformers", "onnx")


def get_nlp_engine_name() -> str:
    """Return the configured NLP engine name."""
    engine = os.getenv("NLP_ENGINE", "spacy").strip().lower()
    if engine not in _VALID_ENGINES:
        raise ValueError(
            f"Invalid NLP_ENGINE='{engine}'. Must be one of {_VALID_ENGINES}."
        )
    return engine


def _patch_torch_load_for_stanza() -> None:
    """Allow ``torch.load`` to use ``weights_only=False`` for Stanza models.

    PyTorch ≥ 2.6 defaults ``weights_only=True`` which breaks Stanza's
    model loading (the pretrained embeddings use numpy globals).  We
    monkey-patch ``torch.load`` to default ``weights_only=False`` only
    while Stanza models are being loaded.
    """
    import torch

    _original_load = torch.load

    def _patched_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _original_load(*args, **kwargs)

    torch.load = _patched_load
    logger.debug("Patched torch.load for Stanza compatibility (weights_only=False)")


def create_nlp_engine() -> NlpEngine:
    """Build and return a Presidio-compatible NLP engine."""
    engine_name = get_nlp_engine_name()

    if engine_name == "transformers" or engine_name == "onnx":
        transformers_model = os.getenv(
            "TRANSFORMERS_MODEL", "dslim/bert-base-NER"
        )
        spacy_tokenizer = os.getenv(
            "TRANSFORMERS_SPACY_MODEL", "en_core_web_sm"
        )

        if engine_name == "onnx":
            from presidio_analyzer.nlp_engine import NerModelConfiguration

            from pii_shield.onnx_nlp_engine import OnnxTransformersNlpEngine

            nlp_engine = OnnxTransformersNlpEngine(
                models=[{
                    "lang_code": "en",
                    "model_name": {
                        "spacy": spacy_tokenizer,
                        "transformers": transformers_model,
                    },
                }],
                ner_model_configuration=NerModelConfiguration(
                    model_to_presidio_entity_mapping=_TRANSFORMERS_ENTITY_MAPPING,
                    aggregation_strategy="max",
                    alignment_mode="expand",
                ),
            )
            nlp_engine.load()
            logger.info("NLP engine initialised: %s", engine_name)
            return nlp_engine

        configuration = {
            "nlp_engine_name": "transformers",
            "models": [
                {
                    "lang_code": "en",
                    "model_name": {
                        "spacy": spacy_tokenizer,
                        "transformers": transformers_model,
                    },
                }
            ],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": _TRANSFORMERS_ENTITY_MAPPING,
                "aggregation_strategy": "max",
                "alignment_mode": "expand",
            },
        }
    elif engine_name == "stanza":
        _patch_torch_load_for_stanza()
        configuration = {
            "nlp_engine_name": "stanza",
            "models": [{"lang_code": "en", "model_name": STANZA_MODEL}],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": _STANZA_ENTITY_MAPPING,
            },
        }
    else:
        configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
        }

    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    logger.info("NLP engine initialised: %s", engine_name)
    return nlp_engine
