"""Load recognizer context overrides from a YAML configuration file.

The YAML file lets operators customise context words per recognizer
without modifying source code.  Two modes are supported for each
recognizer entry:

* ``context`` — **replaces** the recognizer's default context list.
* ``context_append`` — **extends** the default list with extra words.

Example YAML::

    recognizers:
      InBankAccountRecognizer:
        context_append:
          - "new bank name"
      UsBankAccountRecognizer:
        context:
          - "wells"
          - "fargo"
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("pii-shield")

_VALID_KEYS = {"context", "context_append"}


def load_recognizer_contexts(path: str | Path) -> dict[str, dict[str, list[str]]]:
    """Read a recognizer-contexts YAML file.

    Parameters
    ----------
    path : str or Path
        Path to the YAML configuration file.

    Returns
    -------
    dict
        ``{recognizer_name: {"context": [...], "context_append": [...]}}``
        Only keys present in the YAML are included.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Recognizer contexts file not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "recognizers" not in raw:
        logger.warning(
            "Recognizer contexts file %s has no 'recognizers' key; ignoring", path
        )
        return {}

    recognizers = raw["recognizers"]
    if not isinstance(recognizers, dict):
        logger.warning(
            "Recognizer contexts file %s: 'recognizers' is not a mapping; ignoring",
            path,
        )
        return {}

    result: dict[str, dict[str, list[str]]] = {}
    for name, config in recognizers.items():
        if not isinstance(config, dict):
            logger.warning("Recognizer '%s': expected a mapping, got %s; skipping", name, type(config).__name__)
            continue

        unknown = set(config.keys()) - _VALID_KEYS
        if unknown:
            logger.warning("Recognizer '%s': unknown keys %s (valid: %s)", name, unknown, _VALID_KEYS)

        entry: dict[str, list[str]] = {}
        for key in _VALID_KEYS:
            if key in config:
                val = config[key]
                if not isinstance(val, list) or not all(isinstance(w, str) for w in val):
                    logger.warning("Recognizer '%s'.%s: expected list of strings; skipping", name, key)
                    continue
                entry[key] = val

        if entry:
            result[name] = entry

    return result


def apply_recognizer_contexts(
    recognizers: list[Any],
    overrides: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Apply context overrides to a list of recognizer instances.

    Parameters
    ----------
    recognizers : list
        Recognizer instances from ``analyzer.registry.recognizers``.
    overrides : dict
        Output of :func:`load_recognizer_contexts`.

    Returns
    -------
    list[str]
        Names of recognizers that were modified.
    """
    modified: list[str] = []
    for rec in recognizers:
        name = rec.name
        if name not in overrides:
            continue
        cfg = overrides[name]
        if not hasattr(rec, "context"):
            logger.warning("Recognizer '%s' has no context attribute; skipping", name)
            continue

        if "context" in cfg:
            rec.context = list(cfg["context"])
            modified.append(name)
            logger.info("Recognizer '%s': context replaced (%d words)", name, len(rec.context))
        elif "context_append" in cfg:
            if rec.context is None:
                rec.context = []
            rec.context.extend(cfg["context_append"])
            modified.append(name)
            logger.info("Recognizer '%s': context extended (+%d words)", name, len(cfg["context_append"]))

    unmatched = set(overrides.keys()) - {rec.name for rec in recognizers}
    if unmatched:
        logger.warning("Context overrides for unknown recognizers: %s", unmatched)

    return modified
