"""Core PII detection and anonymization engine.

``PiiShieldEngine`` wraps Microsoft Presidio's ``AnalyzerEngine`` with
PII Shield's custom Indian recognizers, post-processing pipeline, and
operator strategies.  It is **stateless** and **thread-safe** — create
one instance at startup and reuse it across all threads/calls.

No dependencies on FastAPI, Redis, OpenTelemetry, or any I/O layer.
"""

import hashlib
import logging
import os
from collections import Counter

from pathlib import Path

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer

from pii_shield.context_config import apply_recognizer_contexts, load_recognizer_contexts
from pii_shield.models import AnonymizeResult, DetectedEntity, EntityConfig
from pii_shield.nlp_engine import create_nlp_engine, get_nlp_engine_name
from pii_shield.operator_config import ENCRYPTION_BACKEND, _DEFAULTS
from pii_shield.pipeline import (
    is_valid_datetime,
    merge_address_entities,
    reclassify_person_as_location,
    remove_overlapping,
)
from pii_shield.recognizers import (
    CreditCardImprovedRecognizer,
    CustomerIdRecognizer,
    GeoCoordinateRecognizer,
    InAadhaarImprovedRecognizer,
    InApaarRecognizer,
    InBankAccountRecognizer,
    InCkycRecognizer,
    InDrivingLicenseRecognizer,
    InPhoneRecognizer,
    InPinCodeRecognizer,
    InPranRecognizer,
    InUpiIdRecognizer,
    NaturalDateRecognizer,
    UsBankAccountRecognizer,
)
from presidio_analyzer.predefined_recognizers import (
    InGstinRecognizer,
    InPanRecognizer,
    InPassportRecognizer,
    InVehicleRegistrationRecognizer,
    InVoterRecognizer,
)

logger = logging.getLogger("pii-shield")

_DEFAULT_DISABLED_RECOGNIZERS = (
    "CreditCardRecognizer,"
    "InAadhaarRecognizer,NhsRecognizer,UsBankRecognizer,SgFinRecognizer,"
    "AuAbnRecognizer,AuAcnRecognizer,AuTfnRecognizer,AuMedicareRecognizer,"
    "MedicalLicenseRecognizer"
)

# Recognizers to remove at startup — configurable via DISABLED_RECOGNIZERS env var.
_RECOGNIZERS_TO_REMOVE = [
    name.strip()
    for name in os.getenv("DISABLED_RECOGNIZERS", _DEFAULT_DISABLED_RECOGNIZERS).split(",")
    if name.strip()
]

# Lazy-initialised operators (avoids import-time failures in environments
# that lack mlkem or Fernet key configuration).
_encrypt_op = None
_fake_op = None


def _get_encrypt_op():
    global _encrypt_op
    if _encrypt_op is None:
        if ENCRYPTION_BACKEND == "pqc":
            from pii_shield.operators.pqc_encrypt import PqcEncryptOperator
            _encrypt_op = PqcEncryptOperator()
        else:
            from pii_shield.operators.fernet_encrypt import FernetEncryptOperator
            _encrypt_op = FernetEncryptOperator()
    return _encrypt_op


def _get_fake_op():
    global _fake_op
    if _fake_op is None:
        from pii_shield.operators.fake_data import FakeDataOperator
        _fake_op = FakeDataOperator()
    return _fake_op


class PiiShieldEngine:
    """Core PII detection and anonymization engine.

    Thread-safe.  Create once, call from many threads.

    Parameters
    ----------
    nlp_engine : str
        NLP backend: ``"spacy"`` | ``"stanza"`` | ``"transformers"`` | ``"onnx"``.
        Read from ``NLP_ENGINE`` env var by default.
    score_threshold : float
        Minimum confidence to accept an entity (default 0.35).
    extra_recognizers : list, optional
        Additional Presidio recognizer instances to register.
    disabled_recognizers : list[str], optional
        Recognizer names to remove beyond the default foreign-country set.
    context_file : str, optional
        Path to a YAML file with recognizer context overrides.
        Falls back to ``RECOGNIZER_CONTEXTS_FILE`` env var if not provided.
    """

    def __init__(
        self,
        score_threshold: float = 0.35,
        extra_recognizers: list | None = None,
        disabled_recognizers: list[str] | None = None,
        context_file: str | None = None,
    ):
        nlp_engine = create_nlp_engine()
        context_factor = float(os.getenv("CONTEXT_SIMILARITY_FACTOR", "0.45"))
        enhancer_kwargs = {
            "context_similarity_factor": context_factor,
            "context_suffix_count": 5,
        }
        # whole_word matching avoids substring false positives
        # (e.g. "ahmedabad" matching US context "aba")
        try:
            enhancer = LemmaContextAwareEnhancer(
                **enhancer_kwargs, context_matching_mode="whole_word",
            )
        except TypeError:
            # Older presidio-analyzer (<2.2.36) lacks context_matching_mode
            enhancer = LemmaContextAwareEnhancer(**enhancer_kwargs)
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=["en"],
            context_aware_enhancer=enhancer,
        )
        self._score_threshold = score_threshold

        # Remove non-India country-specific recognizers
        all_to_remove = list(_RECOGNIZERS_TO_REMOVE)
        if disabled_recognizers:
            all_to_remove.extend(disabled_recognizers)
        for name in all_to_remove:
            try:
                self._analyzer.registry.remove_recognizer(name)
            except ValueError:
                pass  # already removed or not present

        # Register custom recognizers
        self._analyzer.registry.add_recognizer(CreditCardImprovedRecognizer())
        self._analyzer.registry.add_recognizer(CustomerIdRecognizer())
        self._analyzer.registry.add_recognizer(GeoCoordinateRecognizer())
        self._analyzer.registry.add_recognizer(InAadhaarImprovedRecognizer())
        self._analyzer.registry.add_recognizer(InApaarRecognizer())
        self._analyzer.registry.add_recognizer(InBankAccountRecognizer())
        self._analyzer.registry.add_recognizer(InCkycRecognizer())
        self._analyzer.registry.add_recognizer(InDrivingLicenseRecognizer())
        self._analyzer.registry.add_recognizer(InPanRecognizer())
        self._analyzer.registry.add_recognizer(InPassportRecognizer())
        self._analyzer.registry.add_recognizer(InVehicleRegistrationRecognizer())
        self._analyzer.registry.add_recognizer(InVoterRecognizer())
        self._analyzer.registry.add_recognizer(InGstinRecognizer())
        self._analyzer.registry.add_recognizer(InPhoneRecognizer())
        self._analyzer.registry.add_recognizer(InPinCodeRecognizer())
        self._analyzer.registry.add_recognizer(InPranRecognizer())
        self._analyzer.registry.add_recognizer(InUpiIdRecognizer())
        self._analyzer.registry.add_recognizer(NaturalDateRecognizer())
        self._analyzer.registry.add_recognizer(UsBankAccountRecognizer())

        # Register additional user-provided recognizers
        if extra_recognizers:
            for rec in extra_recognizers:
                self._analyzer.registry.add_recognizer(rec)

        # Load custom regex recognizers and global allow-list from YAML
        self._global_allow_list: list[str] = []
        custom_rec_path = os.getenv(
            "CUSTOM_RECOGNIZERS_FILE", "config/custom_recognizers.yml"
        )
        if custom_rec_path and Path(custom_rec_path).is_file():
            try:
                import yaml
                with open(custom_rec_path, encoding="utf-8") as f:
                    custom_cfg = yaml.safe_load(f) or {}
                # Load global allow-list
                if isinstance(custom_cfg.get("global_allow_list"), list):
                    self._global_allow_list = [
                        str(w) for w in custom_cfg["global_allow_list"]
                    ]
                    logger.info(
                        "Global allow-list loaded (%d terms)",
                        len(self._global_allow_list),
                    )
                # Load recognizers via Presidio's native loader
                self._analyzer.registry.add_recognizers_from_yaml(custom_rec_path)
                logger.info("Custom recognizers loaded from %s", custom_rec_path)
            except Exception:
                logger.warning(
                    "Failed to load custom recognizers from %s",
                    custom_rec_path,
                    exc_info=True,
                )

        # Apply recognizer context words from YAML config
        ctx_path = context_file or os.getenv(
            "RECOGNIZER_CONTEXTS_FILE", "config/recognizer_contexts.yml"
        )
        if ctx_path:
            try:
                overrides = load_recognizer_contexts(ctx_path)
                if overrides:
                    modified = apply_recognizer_contexts(
                        self._analyzer.registry.recognizers, overrides
                    )
                    if modified:
                        logger.info("Context overrides applied to: %s", modified)
            except FileNotFoundError:
                logger.warning("Recognizer contexts file not found: %s", ctx_path)
            except Exception:
                logger.warning("Failed to load recognizer contexts from %s", ctx_path, exc_info=True)

        logger.info(
            "PiiShieldEngine ready (NLP engine: %s, threshold: %.2f)",
            get_nlp_engine_name(),
            score_threshold,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_pipeline(
        self,
        text: str,
        language: str,
        allow_list: list[str] | None,
        entity_type_allow_list: set[str] | None,
        entity_keyword_allow_list: dict[str, list[str]] | None = None,
    ) -> list[RecognizerResult]:
        """Run Presidio analysis + full PII Shield post-processing pipeline."""
        # Merge per-request allow_list with global allow_list
        merged_allow = list(self._global_allow_list)
        if allow_list:
            merged_allow.extend(allow_list)

        analyzer_results = self._analyzer.analyze(
            text=text,
            language=language,
            score_threshold=self._score_threshold,
            allow_list=merged_allow or None,
        )

        # Filter entity types in allow-list (and suppress overlapping entities)
        if entity_type_allow_list:
            allowed_spans: list[tuple[int, int]] = []
            kept: list[RecognizerResult] = []
            for r in analyzer_results:
                if r.entity_type in entity_type_allow_list:
                    allowed_spans.append((r.start, r.end))
                else:
                    kept.append(r)
            # Remove entities that overlap with allowed spans (e.g., URL inside EMAIL)
            if allowed_spans:
                analyzer_results = [
                    r for r in kept
                    if not any(r.start < end and r.end > start for start, end in allowed_spans)
                ]
            else:
                analyzer_results = kept

        # Filter entity-keyword combinations (and suppress overlapping entities)
        if entity_keyword_allow_list:
            allowed_spans = []
            kept = []
            for r in analyzer_results:
                matched_text = text[r.start:r.end]
                if (r.entity_type in entity_keyword_allow_list
                        and matched_text in entity_keyword_allow_list[r.entity_type]):
                    allowed_spans.append((r.start, r.end))
                else:
                    kept.append(r)
            if allowed_spans:
                analyzer_results = [
                    r for r in kept
                    if not any(r.start < end and r.end > start for start, end in allowed_spans)
                ]
            else:
                analyzer_results = kept

        # Filter SpaCy NER DATE_TIME false positives
        analyzer_results = [
            r for r in analyzer_results
            if r.entity_type != "DATE_TIME"
            or r.recognition_metadata.get("recognizer_name") != "SpacyRecognizer"
            or is_valid_datetime(text[r.start : r.end])
        ]

        # Reclassify PERSON → LOCATION when preceded by location context
        analyzer_results = reclassify_person_as_location(analyzer_results, text)

        # Merge adjacent LOCATION / IN_PIN_CODE entities into ADDRESS
        analyzer_results = merge_address_entities(analyzer_results, text)

        return remove_overlapping(analyzer_results)

    @property
    def supported_entities(self) -> list[str]:
        """Return all entity types the engine can detect."""
        entities = set(self._analyzer.get_supported_entities())
        entities.add("ADDRESS")
        return sorted(entities)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        text: str,
        language: str = "en",
        allow_list: list[str] | None = None,
        entity_type_allow_list: set[str] | None = None,
        entity_keyword_allow_list: dict[str, list[str]] | None = None,
    ) -> list[DetectedEntity]:
        """Detect PII entities without anonymizing.

        Returns a list of ``DetectedEntity`` objects sorted by position.
        """
        results = self._run_pipeline(
            text, language, allow_list, entity_type_allow_list,
            entity_keyword_allow_list,
        )
        return sorted(
            [
                DetectedEntity(
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=r.score,
                    text=text[r.start : r.end],
                )
                for r in results
            ],
            key=lambda e: e.start,
        )

    def anonymize(
        self,
        text: str,
        language: str = "en",
        config: EntityConfig | None = None,
        allow_list: list[str] | None = None,
        entity_type_allow_list: set[str] | None = None,
        entity_keyword_allow_list: dict[str, list[str]] | None = None,
    ) -> AnonymizeResult:
        """Detect and anonymize PII in a single text string.

        Parameters
        ----------
        text : str
            Input text to anonymize.
        language : str
            ISO 639-1 language code (default ``"en"``).
        config : EntityConfig, optional
            Per-entity-type strategy overrides.  Entities not listed
            default to ``"replace"``.
        allow_list : list[str], optional
            Terms to exclude from anonymization.
        entity_type_allow_list : set[str], optional
            Entity types to exclude from anonymization.
        entity_keyword_allow_list : dict[str, list[str]], optional
            Per-entity-type keyword exclusions.  ``{entity_type: [keywords]}``

        Returns
        -------
        AnonymizeResult
            Contains ``anonymized_text``, ``entity_mapping``,
            ``hash_mapping``, ``encrypt_mapping``, and ``entities``.
        """
        config = config or EntityConfig()

        # Build strategy lookup from defaults + user overrides (simple dict, no lock needed)
        strategies = dict(_DEFAULTS)
        strategies.update(config.strategies)

        non_overlapping = self._run_pipeline(
            text, language, allow_list, entity_type_allow_list,
            entity_keyword_allow_list,
        )

        # Sort by start position descending so replacements don't shift indices
        sorted_results = sorted(
            non_overlapping, key=lambda r: r.start, reverse=True
        )

        def _get_strategy(entity_type: str) -> str:
            return strategies.get(entity_type, "replace")

        hashed_types = {
            r.entity_type for r in sorted_results
            if _get_strategy(r.entity_type) == "hash"
        }
        encrypted_types = {
            r.entity_type for r in sorted_results
            if _get_strategy(r.entity_type) == "encrypt"
        }
        fake_types = {
            r.entity_type for r in sorted_results
            if _get_strategy(r.entity_type) == "fake"
        }

        type_counters: dict[str, int] = {}
        value_to_placeholder: dict[tuple[str, str], str] = {}
        entity_mapping: dict[str, str] = {}
        hash_mapping: dict[str, str] = {}
        encrypt_mapping: dict[str, str] = {}

        # First pass (reverse order): compute replacements and build mappings
        replacements: list[tuple[int, int, str]] = []
        for result in sorted_results:
            original_value = text[result.start : result.end]

            if result.entity_type in hashed_types:
                replacement = hashlib.sha3_256(original_value.encode()).hexdigest()
                hash_mapping[replacement] = original_value
            elif result.entity_type in encrypted_types:
                replacement = _get_encrypt_op().operate(original_value)
                encrypt_mapping[replacement] = original_value
            elif result.entity_type in fake_types:
                key = (result.entity_type, original_value)
                if key not in value_to_placeholder:
                    fake_val = _get_fake_op().operate(
                        original_value, {"entity_type": result.entity_type}
                    )
                    value_to_placeholder[key] = fake_val
                    entity_mapping[fake_val] = original_value
                replacement = value_to_placeholder[key]
            else:
                key = (result.entity_type, original_value)
                if key not in value_to_placeholder:
                    type_counters[result.entity_type] = (
                        type_counters.get(result.entity_type, 0) + 1
                    )
                    counter = type_counters[result.entity_type]
                    placeholder = f"{{{{{result.entity_type}_{counter}}}}}"
                    value_to_placeholder[key] = placeholder
                    entity_mapping[placeholder] = original_value

                replacement = value_to_placeholder[key]

            replacements.append((result.start, result.end, replacement))

        # Single-pass forward build: O(n) instead of O(n × text_length)
        replacements.sort(key=lambda r: r[0])
        parts: list[str] = []
        last_end = 0
        for start, end, replacement in replacements:
            parts.append(text[last_end:start])
            parts.append(replacement)
            last_end = end
        parts.append(text[last_end:])
        result_text = "".join(parts)

        entities = sorted(
            [
                DetectedEntity(
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=r.score,
                    text=text[r.start : r.end],
                )
                for r in non_overlapping
            ],
            key=lambda e: e.start,
        )

        logger.info(
            "Anonymization complete: %d entities detected",
            len(non_overlapping),
        )

        return AnonymizeResult(
            anonymized_text=result_text,
            entity_mapping=entity_mapping,
            hash_mapping=hash_mapping,
            encrypt_mapping=encrypt_mapping,
            entities=entities,
        )

    def deanonymize(
        self,
        text: str,
        entity_mapping: dict[str, str],
        hash_mapping: dict[str, str] | None = None,
        encrypt_mapping: dict[str, str] | None = None,
    ) -> str:
        """Restore original PII values from mappings.

        Parameters
        ----------
        text : str
            Text containing placeholders / hashes / encrypted tokens.
        entity_mapping : dict[str, str]
            Mapping from placeholder to original value.
        hash_mapping : dict[str, str], optional
            Mapping from SHA3 hash to original value.
        encrypt_mapping : dict[str, str], optional
            Mapping from encrypted token to original value.

        Returns
        -------
        str
            Text with PII restored.
        """
        restored_text = text

        # Sort placeholders longest-first to avoid partial replacements
        for placeholder in sorted(entity_mapping, key=len, reverse=True):
            restored_text = restored_text.replace(
                placeholder, entity_mapping[placeholder]
            )

        if hash_mapping:
            for hash_val in sorted(hash_mapping, key=len, reverse=True):
                restored_text = restored_text.replace(
                    hash_val, hash_mapping[hash_val]
                )

        if encrypt_mapping:
            for cipher_val in sorted(encrypt_mapping, key=len, reverse=True):
                restored_text = restored_text.replace(
                    cipher_val, encrypt_mapping[cipher_val]
                )

        return restored_text
