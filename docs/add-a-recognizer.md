# Add a Recognizer

PII Shield uses Microsoft Presidio recognizers for detection. There are six common ways to add or tune recognizers, depending on whether the change is configuration-only, reusable library code, or per-application runtime code.

## Choose an approach

| Scenario | Use this approach | Files or API |
|---|---|---|
| Add a regex or deny-list recognizer without Python code | Add a YAML recognizer | `config/custom_recognizers.yml` or `CUSTOM_RECOGNIZERS_FILE` |
| Suppress terms that should never be anonymized | Add a global allow-list | `global_allow_list` in the custom recognizers YAML |
| Change context words for an existing recognizer | Add context overrides | `config/recognizer_contexts.yml` or `RECOGNIZER_CONTEXTS_FILE` |
| Add a reusable recognizer to PII Shield itself | Add a Python recognizer class and register it | `pii_shield/recognizers/`, `pii_shield/engine.py` |
| Add a recognizer only for one embedded library instance | Pass `extra_recognizers` when constructing `PiiShieldEngine` | Python caller code |
| Keep or remove built-in Presidio recognizers | Configure disabled recognizers | `DISABLED_RECOGNIZERS`, `disabled_recognizers` |

## Option 1: Add a regex recognizer with YAML

Use this for entity types that can be detected with regular expressions or deny lists. This is the preferred path when no custom Python validation is required.

1. Add a recognizer entry under `recognizers:` in `config/custom_recognizers.yml`.
2. If using a different file, set `CUSTOM_RECOGNIZERS_FILE=/path/to/custom_recognizers.yml`.
3. Restart the app or recreate the `PiiShieldEngine` instance.

Example:

```yaml
recognizers:
  - name: "EmployeeIdRecognizer"
    supported_entity: "EMPLOYEE_ID"
    supported_language: "en"
    patterns:
      - name: "employee_id"
        regex: "\\bEMP-[0-9]{6}\\b"
        score: 0.80
    context:
      - employee
      - employee id
      - staff id
      - personnel number
```

Notes:

- YAML regex strings need escaped backslashes, for example `\\b` and `\\d`.
- Use context words to reduce false positives and improve confidence.
- PII Shield loads this file in `PiiShieldEngine.__init__()` with Presidio's `add_recognizers_from_yaml()`.

## Option 2: Add a global allow-list

Use the same custom recognizers YAML file to suppress terms that should never be anonymized, even if a recognizer or NLP model matches them.

```yaml
global_allow_list:
  - IFSC
  - OTP
  - InternalCode
```

The allow-list is merged into each analysis call before post-processing. This is useful for product names, acronyms, labels, and domain terms that NLP models commonly misclassify.

## Option 3: Tune context words for an existing recognizer

Use `config/recognizer_contexts.yml` when the recognizer already exists but needs better context for a domain, tenant, or deployment.

Keys must match recognizer class names.

```yaml
recognizers:
  EmployeeIdRecognizer:
    context:
      - employee
      - staff
      - personnel

  InBankAccountRecognizer:
    context_append:
      - salary account
      - reimbursement account
```

Use `context` to replace the recognizer's context list. Use `context_append` to add words on top of the defaults.

If using another file, set `RECOGNIZER_CONTEXTS_FILE=/path/to/recognizer_contexts.yml` or pass `context_file` to `PiiShieldEngine`.

## Option 4: Add a Python recognizer to PII Shield

Use Python when a recognizer needs custom validation, normalization, multiple patterns, checksum logic, range checks, or complex post-match filtering.

1. Create a file under `pii_shield/recognizers/`.
2. Implement a Presidio recognizer, usually by extending `PatternRecognizer` or `EntityRecognizer`.
3. Export the class from `pii_shield/recognizers/__init__.py`.
4. Register it in `PiiShieldEngine.__init__()` using `self._analyzer.registry.add_recognizer(...)`.
5. Add context words in the recognizer itself or in `config/recognizer_contexts.yml`.
6. Add tests for positive matches, negative matches, context behavior, overlap behavior if relevant, and anonymization output.

Minimal pattern-based recognizer:

```python
from presidio_analyzer import Pattern, PatternRecognizer

_PATTERN = Pattern(
    name="employee_id",
    regex=r"\bEMP-[0-9]{6}\b",
    score=0.80,
)


class EmployeeIdRecognizer(PatternRecognizer):
    """Detects employee identifiers such as EMP-123456."""

    ENTITIES = ["EMPLOYEE_ID"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="EMPLOYEE_ID",
            supported_language="en",
            patterns=[_PATTERN],
            context=["employee", "staff", "personnel"],
        )
```

Then export and register it:

```python
# pii_shield/recognizers/__init__.py
from pii_shield.recognizers.employee_id import EmployeeIdRecognizer

# Add the class name to the existing __all__ list.
__all__ = [
    "EmployeeIdRecognizer",
]
```

```python
# pii_shield/engine.py, inside PiiShieldEngine.__init__()
self._analyzer.registry.add_recognizer(EmployeeIdRecognizer())
```

## Option 5: Add a recognizer only for one library instance

When using PII Shield as a library, callers can pass Presidio recognizer instances directly:

```python
from pii_shield import PiiShieldEngine
from my_app.recognizers import EmployeeIdRecognizer

engine = PiiShieldEngine(extra_recognizers=[EmployeeIdRecognizer()])
```

This does not modify global project behavior and is useful for application-specific recognizers that should not ship with PII Shield.

## Option 6: Keep or disable built-in Presidio recognizers

PII Shield removes several non-India country-specific Presidio recognizers at startup to reduce false positives. The defaults are defined in `pii_shield/engine.py` and can be overridden or extended with:

- `DISABLED_RECOGNIZERS`, a comma-separated environment variable.
- `disabled_recognizers`, a constructor argument to `PiiShieldEngine`.

Use this when adding multi-country support or when a built-in recognizer conflicts with a custom recognizer.

Example:

```python
engine = PiiShieldEngine(disabled_recognizers=["SomeNoisyRecognizer"])
```

## Validate the recognizer

At minimum, verify:

- the new entity appears in `engine.supported_entities`;
- representative positive examples are detected with the expected entity type;
- similar non-PII examples are not detected;
- scores pass the engine threshold, which defaults to `0.35`;
- anonymization emits the expected placeholder, such as `{{EMPLOYEE_ID_1}}`;
- overlapping entities are resolved as intended by the post-processing pipeline.

Example smoke test:

```python
from pii_shield import PiiShieldEngine

engine = PiiShieldEngine()
entities = engine.detect("Employee ID EMP-123456 belongs to Priya")

assert any(e.entity_type == "EMPLOYEE_ID" for e in entities)
```

## Deployment checklist

1. Commit the recognizer YAML or Python changes.
2. Set `CUSTOM_RECOGNIZERS_FILE` or `RECOGNIZER_CONTEXTS_FILE` if using non-default paths.
3. Restart the FastAPI service, Docker Compose stack, or library process so the engine reloads.
4. Run focused recognizer tests and any endpoint tests that exercise anonymization.
