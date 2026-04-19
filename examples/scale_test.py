#!/usr/bin/env python3
"""
PII Shield — Scale Test

Simulates multiple concurrent applications hitting the PII Shield API
with configurable throughput (requests per minute per app).

Each "transaction" follows the sandwich pattern:
    1. POST /anonymize_unique  (with X-App-Id)
    2. POST /deanonymize       (with session_id from step 1)

Prerequisites:
    pip install aiohttp

Usage:
    python examples/scale_test.py [options]

Examples:
    # Quick smoke test (3 apps, 30 RPM each, 1 minute)
    python examples/scale_test.py --apps 3 --rpm 30 --duration 60

    # Default load (6 apps × 100 RPM = 600 RPM, 5 minutes)
    python examples/scale_test.py

    # Heavy load (6 apps × 200 RPM, 10 minutes, 80 connections)
    python examples/scale_test.py --rpm 200 --duration 600 --concurrency 80

    # Against local dev server
    python examples/scale_test.py --api-url http://localhost:8000 --apps 2 --rpm 20 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_section  # noqa: E402

# ── Configuration loading ────────────────────────────────────────────────────

_cfg = load_section("scale", required_keys=["api_url"])
DEFAULT_API_URL = _cfg["api_url"]
DEFAULT_OUTPUT = _cfg.get("output", "examples/__results/scale_test_report.json")
DEFAULT_RPM = int(_cfg.get("rpm", 100))
DEFAULT_APPS = int(_cfg.get("apps", 6))
DEFAULT_DURATION = int(_cfg.get("duration_seconds", 300))
DEFAULT_CONCURRENCY = int(_cfg.get("concurrency", 50))
DEFAULT_WORKERS = int(_cfg.get("workers", 4))
DEFAULT_RAMP_UP = int(_cfg.get("ramp_up_seconds", 30))
DEFAULT_TIMEOUT = int(_cfg.get("timeout_seconds", 30))

# ── App Profiles ─────────────────────────────────────────────────────────────

APP_PROFILES: list[dict[str, Any]] = [
    {
        "name": "scale-retail-banking",
        "description": "Retail banking — all defaults (replace)",
        "config": {},
    },
    {
        "name": "scale-internal-audit",
        "description": "Internal audit — DL numbers hashed",
        "config": {"IN_DRIVING_LICENSE": "hash"},
    },
    {
        "name": "scale-compliance",
        "description": "Compliance — email encrypted, phone+DL hashed",
        "config": {
            "EMAIL_ADDRESS": "encrypt",
            "PHONE_NUMBER": "hash",
            "IN_DRIVING_LICENSE": "hash",
        },
    },
    {
        "name": "scale-customer-support",
        "description": "Customer support — all defaults (replace)",
        "config": {},
    },
    {
        "name": "scale-fraud-detection",
        "description": "Fraud detection — emails/aadhaar/phone encrypted",
        "config": {
            "EMAIL_ADDRESS": "encrypt",
            "IN_AADHAAR": "encrypt",
            "PHONE_NUMBER": "encrypt",
        },
    },
    {
        "name": "scale-loan-processing",
        "description": "Loan processing — DL encrypted, Aadhaar hashed",
        "config": {
            "IN_DRIVING_LICENSE": "encrypt",
            "IN_AADHAAR": "hash",
        },
    },
    {
        "name": "scale-identity-verification",
        "description": "Identity verification — CKYC/PRAN/APAAR encrypted",
        "config": {
            "IN_CKYC": "encrypt",
            "IN_PRAN": "encrypt",
            "IN_APAAR": "encrypt",
        },
    },
]


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class AppState:
    """Runtime state for a registered app."""

    name: str
    description: str
    app_id: str
    config: dict[str, str]
    limiter: TokenBucketLimiter


@dataclass
class RequestRecord:
    """Single HTTP request measurement."""

    timestamp: float
    endpoint: str
    app_name: str
    status_code: int
    latency_ms: float
    error: str | None = None


# ── Token Bucket Rate Limiter ────────────────────────────────────────────────


class TokenBucketLimiter:
    """Async-safe token-bucket rate limiter.

    Distributes requests evenly across each minute rather than
    allowing a burst at the start of each window.
    """

    def __init__(self, rpm: float):
        self.rpm = rpm
        self._tokens = float(rpm)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.rpm, self._tokens + elapsed * (self.rpm / 60.0))
            self._last_refill = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (60.0 / self.rpm)
                # Release lock while sleeping so other coroutines proceed
                self._lock.release()
                await asyncio.sleep(wait)
                await self._lock.acquire()
                # Refill after sleep
                now2 = time.monotonic()
                elapsed2 = now2 - self._last_refill
                self._tokens = min(
                    self.rpm, self._tokens + elapsed2 * (self.rpm / 60.0)
                )
                self._last_refill = now2
            self._tokens -= 1.0

    def set_rpm(self, rpm: float) -> None:
        """Update the RPM (used during ramp-up)."""
        self.rpm = rpm


# ── Stats Collector ──────────────────────────────────────────────────────────


class StatsCollector:
    """Collects per-request metrics for later aggregation."""

    def __init__(self):
        self.records: list[RequestRecord] = []
        self._lock = asyncio.Lock()
        self._last_print = time.monotonic()
        self._print_interval = 10.0  # seconds between console updates

    async def record(
        self,
        endpoint: str,
        status: int,
        latency: float,
        app_name: str,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            self.records.append(
                RequestRecord(
                    timestamp=time.time(),
                    endpoint=endpoint,
                    app_name=app_name,
                    status_code=status,
                    latency_ms=latency * 1000,
                    error=error,
                )
            )

    def snapshot(self, window_seconds: float = 10.0) -> dict:
        """Return stats for the last N seconds."""
        cutoff = time.time() - window_seconds
        recent = [r for r in self.records if r.timestamp >= cutoff]
        if not recent:
            return {"rps": 0, "p50": 0, "p95": 0, "errors": 0}
        latencies = [r.latency_ms for r in recent]
        errors = sum(1 for r in recent if r.status_code >= 400)
        return {
            "rps": len(recent) / window_seconds,
            "p50": statistics.median(latencies),
            "p95": _percentile(latencies, 95),
            "errors": errors,
        }

    def build_report(self, config: dict) -> dict:
        """Produce the full JSON report."""
        if not self.records:
            return {"test_config": config, "summary": {}, "error": "No requests recorded"}

        all_latencies = [r.latency_ms for r in self.records]
        errors = [r for r in self.records if r.status_code >= 400]
        start_ts = min(r.timestamp for r in self.records)
        end_ts = max(r.timestamp for r in self.records)
        duration = end_ts - start_ts if end_ts > start_ts else 1.0

        # Per-endpoint breakdown
        endpoints: dict[str, list[float]] = {}
        for r in self.records:
            endpoints.setdefault(r.endpoint, []).append(r.latency_ms)

        latency_by_endpoint = {}
        for ep, lats in endpoints.items():
            latency_by_endpoint[ep] = _latency_stats(lats)

        # Per-app breakdown
        apps: dict[str, list[RequestRecord]] = {}
        for r in self.records:
            apps.setdefault(r.app_name, []).append(r)

        per_app = {}
        for app_name, recs in sorted(apps.items()):
            app_lats = [r.latency_ms for r in recs]
            app_errors = sum(1 for r in recs if r.status_code >= 400)
            per_app[app_name] = {
                "requests": len(recs),
                "errors": app_errors,
                "error_rate_pct": round(app_errors / len(recs) * 100, 2) if recs else 0,
                **_latency_stats(app_lats),
            }

        # Timeline (per-minute buckets)
        timeline = []
        minute_start = start_ts
        minute_idx = 0
        while minute_start < end_ts:
            minute_end = minute_start + 60
            bucket = [r for r in self.records if minute_start <= r.timestamp < minute_end]
            if bucket:
                bucket_lats = [r.latency_ms for r in bucket]
                bucket_errors = sum(1 for r in bucket if r.status_code >= 400)
                timeline.append({
                    "minute": minute_idx,
                    "requests": len(bucket),
                    "rps": round(len(bucket) / min(60, end_ts - minute_start), 1),
                    "errors": bucket_errors,
                    **_latency_stats(bucket_lats),
                })
            minute_start = minute_end
            minute_idx += 1

        # Transaction count (anonymize+deanonymize pairs)
        anon_count = sum(1 for r in self.records if r.endpoint == "/anonymize_unique")
        deanon_count = sum(1 for r in self.records if r.endpoint == "/deanonymize")

        return {
            "test_config": config,
            "summary": {
                "total_requests": len(self.records),
                "anonymize_requests": anon_count,
                "deanonymize_requests": deanon_count,
                "successful_requests": len(self.records) - len(errors),
                "failed_requests": len(errors),
                "error_rate_pct": round(len(errors) / len(self.records) * 100, 2),
                "actual_rps": round(len(self.records) / duration, 1),
                "duration_seconds": round(duration, 1),
                **_latency_stats(all_latencies),
            },
            "latency_by_endpoint": latency_by_endpoint,
            "per_app": per_app,
            "timeline": timeline,
            "errors": [
                {
                    "timestamp": datetime.fromtimestamp(r.timestamp, tz=timezone.utc).isoformat(),
                    "endpoint": r.endpoint,
                    "status_code": r.status_code,
                    "app": r.app_name,
                    "error": r.error,
                }
                for r in errors[:200]  # cap at 200 error entries
            ],
        }


def _percentile(data: list[float], pct: int) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    idx = min(idx, len(sorted_data) - 1)
    return round(sorted_data[idx], 1)


def _latency_stats(latencies: list[float]) -> dict:
    if not latencies:
        return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "max_ms": 0, "min_ms": 0, "mean_ms": 0}
    return {
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "max_ms": round(max(latencies), 1),
        "min_ms": round(min(latencies), 1),
        "mean_ms": round(statistics.mean(latencies), 1),
    }


# ── Setup: App Registration ─────────────────────────────────────────────────


async def ensure_app(
    session: aiohttp.ClientSession,
    base_url: str,
    name: str,
    timeout: int,
) -> str:
    """Return app_id for *name*, creating it if it doesn't exist."""
    async with session.get(f"{base_url}/apps", timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        apps = await resp.json()
    matches = [a for a in apps if a["app_name"] == name]
    if matches:
        return matches[0]["app_id"]
    async with session.post(
        f"{base_url}/apps",
        json={"app_name": name},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data["app_id"]


async def configure_app(
    session: aiohttp.ClientSession,
    base_url: str,
    app_id: str,
    config: dict[str, str],
    timeout: int,
) -> None:
    """Apply per-entity strategy configuration."""
    for entity_type, strategy in config.items():
        async with session.put(
            f"{base_url}/apps/{app_id}/config",
            json={"entity_type": entity_type, "strategy": strategy},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()


async def setup_apps(
    session: aiohttp.ClientSession,
    base_url: str,
    num_apps: int,
    rpm: float,
    timeout: int,
) -> list[AppState]:
    """Register and configure all simulated apps."""
    profiles = APP_PROFILES[:num_apps]
    # If more apps requested than profiles, cycle through profiles
    while len(profiles) < num_apps:
        idx = len(profiles) % len(APP_PROFILES)
        src = APP_PROFILES[idx]
        profiles.append({
            "name": f"{src['name']}-{len(profiles) // len(APP_PROFILES) + 1}",
            "description": f"{src['description']} (copy {len(profiles) // len(APP_PROFILES) + 1})",
            "config": src["config"],
        })

    apps: list[AppState] = []
    for profile in profiles:
        app_id = await ensure_app(session, base_url, profile["name"], timeout)
        await configure_app(session, base_url, app_id, profile["config"], timeout)
        apps.append(
            AppState(
                name=profile["name"],
                description=profile["description"],
                app_id=app_id,
                config=profile["config"],
                limiter=TokenBucketLimiter(rpm),
            )
        )
        print(f" [PASS] {profile['name']:30s} app_id={app_id} config={profile['config'] or '(defaults)'}")
    return apps


# ── Load Phase: Transaction Runner ───────────────────────────────────────────


async def run_transaction(
    session: aiohttp.ClientSession,
    base_url: str,
    app: AppState,
    scenario: dict,
    stats: StatsCollector,
    semaphore: asyncio.Semaphore,
    timeout: int,
) -> None:
    """Execute one anonymize → deanonymize cycle."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    headers = {"X-App-Id": app.app_id, "Content-Type": "application/json"}

    # ── Step 1: Anonymize ────────────────────────────────────────────────
    await app.limiter.acquire()
    async with semaphore:
        t0 = time.monotonic()
        try:
            async with session.post(
                f"{base_url}/anonymize_unique",
                json={"text": scenario["text"], "language": "en"},
                headers=headers,
                timeout=client_timeout,
            ) as resp:
                anon_latency = time.monotonic() - t0
                status = resp.status
                if status == 200:
                    anon_data = await resp.json()
                else:
                    body = await resp.text()
                    await stats.record(
                        "/anonymize_unique", status, anon_latency, app.name,
                        error=body[:200],
                    )
                    return
        except Exception as exc:
            anon_latency = time.monotonic() - t0
            await stats.record(
                "/anonymize_unique", 0, anon_latency, app.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

    await stats.record("/anonymize_unique", status, anon_latency, app.name)

    # ── Step 2: Deanonymize ──────────────────────────────────────────────
    await app.limiter.acquire()
    async with semaphore:
        t1 = time.monotonic()
        try:
            async with session.post(
                f"{base_url}/deanonymize",
                json={
                    "id": anon_data["id"],
                    "text": anon_data["anonymized_text"],
                    "include_hashed": True,
                    "include_encrypted": True,
                },
                headers=headers,
                timeout=client_timeout,
            ) as resp:
                deanon_latency = time.monotonic() - t1
                status = resp.status
                if status != 200:
                    body = await resp.text()
                    await stats.record(
                        "/deanonymize", status, deanon_latency, app.name,
                        error=body[:200],
                    )
                    return
        except Exception as exc:
            deanon_latency = time.monotonic() - t1
            await stats.record(
                "/deanonymize", 0, deanon_latency, app.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

    await stats.record("/deanonymize", status, deanon_latency, app.name)


async def app_worker(
    session: aiohttp.ClientSession,
    base_url: str,
    app: AppState,
    scenarios: list[dict],
    stats: StatsCollector,
    semaphore: asyncio.Semaphore,
    timeout: int,
    stop_event: asyncio.Event,
) -> None:
    """Continuously send transactions for one app until stop_event is set."""
    while not stop_event.is_set():
        scenario = random.choice(scenarios)
        await run_transaction(session, base_url, app, scenario, stats, semaphore, timeout)


async def ramp_up_controller(
    apps: list[AppState],
    target_rpm: float,
    ramp_seconds: float,
) -> None:
    """Gradually increase RPM from ~10% to target over ramp_seconds."""
    if ramp_seconds <= 0:
        return
    start_rpm = max(10, target_rpm * 0.1)
    steps = int(ramp_seconds / 2)  # update every 2 seconds
    for i in range(steps):
        progress = (i + 1) / steps
        current_rpm = start_rpm + (target_rpm - start_rpm) * progress
        for app in apps:
            app.limiter.set_rpm(current_rpm)
        await asyncio.sleep(2)
    # Ensure exact target
    for app in apps:
        app.limiter.set_rpm(target_rpm)


async def console_reporter(
    stats: StatsCollector,
    apps: list[AppState],
    target_rpm: float,
    stop_event: asyncio.Event,
    start_time: float,
    duration: float,
) -> None:
    """Print live console stats every 10 seconds."""
    while not stop_event.is_set():
        await asyncio.sleep(10)
        if stop_event.is_set():
            break
        elapsed = time.monotonic() - start_time
        snap = stats.snapshot(window_seconds=10.0)
        # Each transaction = 2 HTTP requests (anonymize + deanonymize)
        total_target = target_rpm * len(apps) * 2 / 60  # target RPS (HTTP requests)
        bar_len = 20
        time_pct = min(elapsed / duration, 1.0)
        filled = int(bar_len * time_pct)
        bar = "▓" * filled + "░" * (bar_len - filled)
        print(
            f"  [{int(elapsed):>4d}s]  "
            f"RPS: {snap['rps']:5.1f}/{total_target:.0f}  │  "
            f"p50: {snap['p50']:6.0f}ms  │  "
            f"p95: {snap['p95']:6.0f}ms  │  "
            f"Errors: {snap['errors']:<4d} │  "
            f"{bar}  {time_pct*100:.0f}%"
        )


# ── Main Orchestrator ────────────────────────────────────────────────────────


async def run_scale_test(args: argparse.Namespace) -> dict:
    """Top-level async entry point."""
    base_url = args.api_url.rstrip("/")
    scenarios_path = Path(__file__).parent / "indian_banking_test_data.json"
    if not scenarios_path.exists():
        print(f"[FAIL] Test data not found: {scenarios_path}")
        sys.exit(1)

    with open(scenarios_path) as f:
        scenarios = json.load(f)

    print(f"\n PII Shield — Scale Test")
    print(f"   API:         {base_url}")
    print(f"   Apps:        {args.apps}")
    print(f"   RPM/app:     {args.rpm}")
    print(f"   Total RPM:   {args.apps * args.rpm}")
    print(f"   Duration:    {args.duration}s")
    print(f"   Concurrency: {args.concurrency}")
    print(f"   Workers/app: {args.workers}")
    print(f"   Ramp-up:     {args.ramp_up}s")
    print(f"   Scenarios:   {len(scenarios)}")
    print()

    # ── Health check ─────────────────────────────────────────────────────
    connector = aiohttp.TCPConnector(limit=args.concurrency, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        print(" Checking API connectivity...")
        try:
            # Try /docs (FastAPI OpenAPI) as a health probe
            async with session.get(
                f"{base_url}/supported-entities",
                timeout=aiohttp.ClientTimeout(total=args.timeout),
            ) as resp:
                if resp.status == 200:
                    print(" [PASS] API is reachable\n")
                else:
                    print(f" [WARN] API returned {resp.status}, continuing...\n")
        except Exception as exc:
            print(f" [FAIL] Cannot reach API: {exc}")
            sys.exit(1)

        # ── Setup phase ──────────────────────────────────────────────────
        print(" Setting up applications...")
        apps = await setup_apps(session, base_url, args.apps, args.rpm, args.timeout)
        print()

        # ── Load phase ───────────────────────────────────────────────────
        stats = StatsCollector()
        semaphore = asyncio.Semaphore(args.concurrency)
        stop_event = asyncio.Event()
        start_time = time.monotonic()

        target_total_rpm = args.apps * args.rpm
        print(
            f" Starting load: {args.apps} apps × {args.rpm} RPM = "
            f"{target_total_rpm} transactions/min ({target_total_rpm * 2} HTTP req/min)\n"
        )

        # Launch per-app workers (multiple workers per app to enable concurrency)
        workers = []
        for app in apps:
            for _ in range(args.workers):
                workers.append(
                    asyncio.create_task(
                        app_worker(session, base_url, app, scenarios, stats, semaphore, args.timeout, stop_event)
                    )
                )

        # Launch ramp-up controller
        ramp_task = asyncio.create_task(
            ramp_up_controller(apps, args.rpm, args.ramp_up)
        )

        # Launch console reporter
        reporter_task = asyncio.create_task(
            console_reporter(stats, apps, args.rpm, stop_event, start_time, args.duration)
        )

        # Wait for duration
        await asyncio.sleep(args.duration)
        stop_event.set()

        # Cancel workers gracefully
        for w in workers:
            w.cancel()
        ramp_task.cancel()
        reporter_task.cancel()

        # Allow tasks to finish
        await asyncio.gather(*workers, ramp_task, reporter_task, return_exceptions=True)

    elapsed = time.monotonic() - start_time

    # ── Report ───────────────────────────────────────────────────────────
    print(f"\n [PASS] Load phase complete ({elapsed:.1f}s)\n")

    config = {
        "api_url": base_url,
        "apps": args.apps,
        "rpm_per_app": args.rpm,
        "total_rpm": target_total_rpm,
        "duration_seconds": args.duration,
        "concurrency": args.concurrency,
        "workers_per_app": args.workers,
        "ramp_up_seconds": args.ramp_up,
        "scenarios": len(scenarios),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    report = stats.build_report(config)

    # Print summary
    s = report.get("summary", {})
    print("  ═══════════════════════════════════════════════════════════")
    print("                    SCALE TEST RESULTS")
    print("  ═══════════════════════════════════════════════════════════")
    print(f"  Total Requests:    {s.get('total_requests', 0):>8,}")
    print(f"    Anonymize:       {s.get('anonymize_requests', 0):>8,}")
    print(f"    Deanonymize:     {s.get('deanonymize_requests', 0):>8,}")
    print(f"  Successful:        {s.get('successful_requests', 0):>8,}")
    print(f"  Failed:            {s.get('failed_requests', 0):>8,}")
    print(f"  Error Rate:        {s.get('error_rate_pct', 0):>7.2f}%")
    print(f"  Actual RPS:        {s.get('actual_rps', 0):>8.1f}")
    print(f"  Duration:          {s.get('duration_seconds', 0):>7.1f}s")
    print()
    print("  Latency (all requests):")
    print(f"    p50:   {s.get('p50_ms', 0):>8.1f} ms")
    print(f"    p95:   {s.get('p95_ms', 0):>8.1f} ms")
    print(f"    p99:   {s.get('p99_ms', 0):>8.1f} ms")
    print(f"    max:   {s.get('max_ms', 0):>8.1f} ms")
    print()

    # Per-endpoint
    for ep, ep_stats in report.get("latency_by_endpoint", {}).items():
        print(f"  {ep}:")
        print(f"    p50: {ep_stats.get('p50_ms', 0):.0f}ms  "
              f"p95: {ep_stats.get('p95_ms', 0):.0f}ms  "
              f"p99: {ep_stats.get('p99_ms', 0):.0f}ms  "
              f"max: {ep_stats.get('max_ms', 0):.0f}ms")

    # Per-app
    print()
    print("  Per-Application:")
    print(f"  {'App':<35s} {'Reqs':>6s} {'Err':>5s} {'Err%':>6s} {'p50':>7s} {'p95':>7s}")
    print(f"  {'─' * 35} {'─' * 6} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 7}")
    for app_name, app_stats in report.get("per_app", {}).items():
        print(
            f"  {app_name:<35s} "
            f"{app_stats['requests']:>6d} "
            f"{app_stats['errors']:>5d} "
            f"{app_stats['error_rate_pct']:>5.1f}% "
            f"{app_stats['p50_ms']:>6.0f}m "
            f"{app_stats['p95_ms']:>6.0f}m"
        )

    print("  ═══════════════════════════════════════════════════════════")

    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PII Shield — Scale Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-url", default=DEFAULT_API_URL,
        help=f"API base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--apps", type=int, default=DEFAULT_APPS,
        help=f"Number of simulated applications (default: {DEFAULT_APPS})",
    )
    parser.add_argument(
        "--rpm", type=int, default=DEFAULT_RPM,
        help=f"Requests per minute per application (default: {DEFAULT_RPM})",
    )
    parser.add_argument(
        "--duration", type=int, default=DEFAULT_DURATION,
        help=f"Test duration in seconds (default: {DEFAULT_DURATION})",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Max concurrent HTTP connections (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Concurrent worker coroutines per app (default: {DEFAULT_WORKERS}). "
             "More workers = more in-flight requests per app.",
    )
    parser.add_argument(
        "--ramp-up", type=int, default=DEFAULT_RAMP_UP,
        help=f"Ramp-up period in seconds (default: {DEFAULT_RAMP_UP})",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output report path (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = asyncio.run(run_scale_test(args))

    # Write report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n Report saved to: {output_path}\n")


if __name__ == "__main__":
    main()
