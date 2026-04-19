"""Shared YAML config loader for the example test scripts.

Used by:
  - examples/run_indian_banking_tests.py        (section: local)
  - examples/run_indian_banking_tests_azure.py  (section: azure)
  - examples/scale_test.py                      (section: scale)
  - examples/library_usage.py                   (section: library)

The config file lives at examples/test_config.yml. If it is missing the
caller exits with a clear error (use test_config.example.yml as a starting
point).

Each section may set its own keys; missing keys fall back to the top-level
`defaults` block. CLI flags in each test still take highest precedence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "test_config.yml"
EXAMPLE_PATH = Path(__file__).parent / "test_config.example.yml"


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def load_section(section: str, *, required_keys: list[str] | None = None) -> dict[str, Any]:
    """Load a section from examples/test_config.yml.

    Returns a dict merging top-level `defaults` with the section's overrides.
    Hard-errors with a clear message if the file is missing, malformed,
    or any of `required_keys` are absent from the merged result.
    """
    if not CONFIG_PATH.exists():
        _fail(
            f"Required config file not found: {CONFIG_PATH}\n"
            f"       Copy {EXAMPLE_PATH.name} to {CONFIG_PATH.name} and "
            f"fill in your deployment values."
        )

    try:
        with CONFIG_PATH.open() as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        _fail(f"Could not parse {CONFIG_PATH}: {exc}")

    if not isinstance(raw, dict):
        _fail(f"{CONFIG_PATH} must be a YAML mapping at the top level.")

    defaults = raw.get("defaults") or {}
    section_cfg = raw.get(section)

    if section_cfg is None:
        _fail(
            f"Section '{section}' missing from {CONFIG_PATH}. "
            f"Available sections: {', '.join(k for k in raw if k != 'defaults') or '(none)'}"
        )
    if not isinstance(section_cfg, dict):
        _fail(f"Section '{section}' in {CONFIG_PATH} must be a mapping.")

    merged: dict[str, Any] = {**defaults, **section_cfg}

    for key in required_keys or []:
        if not merged.get(key):
            _fail(
                f"Section '{section}' (or top-level defaults) in {CONFIG_PATH} "
                f"is missing required key '{key}'."
            )

    return merged
