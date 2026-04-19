#!/usr/bin/env python3
"""Delete all registered applications whose name starts with a test prefix.

Reads the API URL from ``examples/test_config.yml`` based on the ``--test``
section (local / azure / scale / library). CLI flags override config values.

Examples
--------
    # Preview what would be deleted (no changes)
    python scripts/cleanup_test_apps.py --dry-run

    # Delete __test* apps on local (default)
    python scripts/cleanup_test_apps.py

    # Delete from the azure deployment
    python scripts/cleanup_test_apps.py --test azure

    # Scale section with custom prefix
    python scripts/cleanup_test_apps.py --test scale --prefix scale-

    # Override the URL entirely
    python scripts/cleanup_test_apps.py --api-url http://host:8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))
from _config import load_section  # noqa: E402

TEST_SECTIONS = {"local", "azure", "scale", "library", "lib"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--test",
        default="local",
        choices=sorted(TEST_SECTIONS),
        help="Which section of examples/test_config.yml to read (default: %(default)s). 'lib' is an alias for 'library'.",
    )
    parser.add_argument("--api-url", default=None, help="Override API base URL from config")
    parser.add_argument(
        "--prefix",
        required=True,
        help="Delete apps whose name starts with this prefix. Pass '*' to delete ALL apps.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching apps without deleting them",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout in seconds")
    args = parser.parse_args()

    section = "library" if args.test == "lib" else args.test

    if args.api_url:
        api_url = args.api_url
    else:
        cfg = load_section(section)
        api_url = cfg.get("api_url")
        if not api_url:
            print(
                f"[FAIL] Section '{section}' in test_config.yml has no 'api_url'. "
                f"Pass --api-url explicitly or add one to the section.",
                file=sys.stderr,
            )
            return 2

    base = api_url.rstrip("/") + "/"
    match_all = args.prefix == "*"
    prefix_desc = "<ALL APPS>" if match_all else repr(args.prefix)
    print(f"Target: {base}  (section: {section}, prefix: {prefix_desc})")

    try:
        r = requests.get(urljoin(base, "apps"), timeout=args.timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[FAIL] Could not fetch apps from {base}apps: {e}", file=sys.stderr)
        return 2

    apps = r.json()
    if match_all:
        matches = list(apps)
    else:
        matches = [a for a in apps if a.get("app_name", "").startswith(args.prefix)]

    print(f"Found {len(apps)} total apps, {len(matches)} match prefix {prefix_desc}:")
    for a in matches:
        print(f"  - {a['app_id']}  {a['app_name']}")

    if not matches:
        return 0

    if args.dry_run:
        print("\nDry-run. Re-run without --dry-run to delete.")
        return 0

    deleted = 0
    failed = 0
    for a in matches:
        url = urljoin(base, f"apps/{a['app_id']}")
        try:
            resp = requests.delete(url, timeout=args.timeout)
            if resp.status_code in (200, 204):
                deleted += 1
                print(f"  [OK]   deleted {a['app_id']} ({a['app_name']})")
            else:
                failed += 1
                print(f"  [FAIL] {a['app_id']}: HTTP {resp.status_code} {resp.text[:120]}")
        except requests.RequestException as e:
            failed += 1
            print(f"  [FAIL] {a['app_id']}: {e}")

    print(f"\nDeleted {deleted}, failed {failed}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
