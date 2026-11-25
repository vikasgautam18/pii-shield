#!/usr/bin/env python3
"""Compare the latency of two git refs of this repository, fairly and reproducibly, with Docker.

    python scripts/compare_branches.py --base origin/main --head WORKTREE
    python scripts/compare_branches.py --base v1.2 --head HEAD --docs bank,prose --cpus 2 --out report.md
    python scripts/compare_branches.py --base HEAD --head HEAD --load-only      # the noise floor of this machine

Comparing by hand is error-prone: different machines, noisy neighbours, cold caches, a different CPU share.  This
script removes those variables.  Each ref is exported with ``git archive`` (``WORKTREE`` is the current working tree:
tracked plus untracked, not ignored, files), so the checkout is never touched, and its ``app``, ``pii_shield`` and
``config`` directories are bind-mounted read-only over ``/app`` of the SAME app image, so both refs run identical
dependencies.  Everything runs under the same cgroup CPU cap on the same seeded documents (the bank family is built from
examples/indian_banking_test_data.json of the working tree), one container at a time.

Phases (the report has one section for each):

* steps: the time of one ``anonymize()`` call split into steps whose sum is about the total (recognizers, NER model,
  context scoring, dedup, second NER pass, post-processing, handling), measured in-process by
  ``scripts/compare_branches_profile.py``, which also works on a ref without the new step timing.
* load: the real API in a container per ref, Redis in a throwaway container on a throwaway network.  After a
  warm-up it measures the lone-request latency per document size, then a closed loop of K clients (p50/p95/p99 and
  requests per second).  The refs are interleaved base, head, base, head so drift cancels, and the report gives the
  spread between rounds: a change smaller than that is noise.
* check: the entities detected in the same documents by each ref (spans and types).  Differences are reported,
  they never fail the run.

The script needs docker and git on the host and nothing but the standard library.  Containers and the network it
creates are named cmp-*, and are removed when it ends, also on Ctrl-C or SIGTERM.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import http.client
import json
import math
import os
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from io import BytesIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_SCRIPT = Path(__file__).resolve().with_name("compare_branches_profile.py")
CODE_DIRS = ("app", "pii_shield", "config")  # what a ref contributes; everything else comes from the image
WORKTREE = "WORKTREE"
CONTAINER_PREFIX = "cmp-"
REDIS_IMAGE = "redis:7-alpine"

FAMILIES = ("bank", "prose", "json", "statement")
SIZE_CHARS = {"small": 300, "medium": 3000, "large": 6000}
SIZE_LABELS = {"small": "small (~0.3 KB)", "medium": "medium (~3 KB)", "large": "large (~6 KB)"}
MIX = (("small", 0.70), ("medium", 0.25), ("large", 0.05))  # what the closed loop sends
CHECK_INDEXES = (0, 1)  # documents per family and size whose entities are compared
SCORE_TOLERANCE = 0.005
MIN_STEP_MS = 0.05  # a step below this in both refs is left out of the table

# The environment of the API (and profile) container: what infra/scripts/02-deploy-apps.sh gives a replica, minus
# anything that needs the network.  Telemetry is exported to a closed local port on purpose: the spans and
# metrics are still produced (their cost is part of the request), but no collector is needed or disturbed.
AZURE_DISABLED = (
    "InAadhaarRecognizer,NhsRecognizer,UsBankRecognizer,SgFinRecognizer,AuAbnRecognizer,AuAcnRecognizer,"
    "AuTfnRecognizer,AuMedicareRecognizer,MedicalLicenseRecognizer"
)
BASE_ENV = {
    "NLP_ENGINE": "onnx",
    "TRANSFORMERS_MODEL": "protectai/bert-base-NER-onnx",
    "TRANSFORMERS_SPACY_MODEL": "en_core_web_sm",
    "DISABLED_RECOGNIZERS": AZURE_DISABLED,
    "CONTEXT_SIMILARITY_FACTOR": "0.45",
    "ORT_INTER_OP_THREADS": "1",
    "HF_HUB_OFFLINE": "1",
    "TOKENIZERS_PARALLELISM": "false",
}
API_ENV = {
    "AUTH_ENABLED": "false",
    "SESSION_TTL_SECONDS": "30",
    "WEB_CONCURRENCY": "2",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
}

STARTUP_TIMEOUT_S = 300.0
REQUEST_TIMEOUT_S = 300.0
PROFILE_TIMEOUT_S = 3600.0
CLEANUP_TIMEOUT_S = 120.0


class HarnessError(Exception):
    """The comparison itself failed (git, Docker, a container); it says nothing about the refs."""


# ---------------------------------------------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------------------------------------------


class SubprocessRunner:
    """Runs external commands; the tests pass a fake with the same ``run`` signature."""

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        text: bool = True,
        timeout: float | None = None,
        cwd: str | os.PathLike | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess:
        command = [str(a) for a in args]
        encoding = {"encoding": "utf-8", "errors": "replace"} if text else {}
        try:
            if on_stderr is None:
                proc = subprocess.run(command, capture_output=True, timeout=timeout, cwd=cwd, **encoding)
            else:
                proc = self._run_streaming(command, timeout, cwd, encoding, on_stderr)
        except FileNotFoundError as exc:
            raise HarnessError(f"{command[0]}: command not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(f"timed out after {timeout:g}s: {' '.join(command[:4])} ...") from exc
        if check and proc.returncode != 0:
            raise HarnessError(describe_failure(command, proc))
        return proc

    @staticmethod
    def _run_streaming(command, timeout, cwd, encoding, on_stderr) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True, **encoding
        )
        out: list[str] = []
        err: list[str] = []

        def pump(stream, sink, callback):
            for line in stream:
                sink.append(line)
                if callback:
                    callback(line.rstrip("\n"))

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out, None), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err, on_stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            proc.wait(timeout=timeout)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            for thread in threads:
                thread.join(5)
        return subprocess.CompletedProcess(command, proc.returncode, "".join(out), "".join(err))


def describe_failure(command: Sequence[str], proc: subprocess.CompletedProcess) -> str:
    detail = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
    tail = "\n".join(detail.strip().splitlines()[-12:])
    shown = " ".join(command[:5]) + (" ..." if len(command) > 5 else "")
    return f"`{shown}` exited with {proc.returncode}\n{tail}".rstrip()


# ---------------------------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _families(value: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    unknown = [name for name in names if name not in FAMILIES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"choose from {', '.join(FAMILIES)} (got {value!r})")
    return names


def _ref(value: str) -> str:
    if not value or value.startswith("-"):
        raise argparse.ArgumentTypeError(f"not a git ref: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_branches.py",
        description="Compare the latency of two git refs of this repository, fairly and reproducibly, with Docker.",
        epilog=__doc__.split("\n\n", 1)[1].replace("``", ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", required=True, type=_ref, metavar="REF",
                        help="the reference side, for example origin/main")
    parser.add_argument("--head", required=True, type=_ref, metavar="REF|WORKTREE",
                        help="the side under test; WORKTREE is the current working tree, uncommitted changes included")
    parser.add_argument("--image", default=None, metavar="IMAGE",
                        help="app image both refs run on (default: the app image of this repo's compose project, "
                             "for example <project>-app)")
    parser.add_argument("--cpus", type=_positive_float, default=2.0,
                        help="CPU cap of every container, docker --cpus (default 2)")
    parser.add_argument("--docs", type=_families, default=FAMILIES, metavar="LIST",
                        help=f"document families, comma separated (default {','.join(FAMILIES)})")
    parser.add_argument("--reps", type=_positive_int, default=7,
                        help="timed repetitions per document, steps and lone requests (default 7)")
    parser.add_argument("--clients", type=_positive_int, default=4,
                        help="concurrent clients of the closed loop (default 4)")
    parser.add_argument("--seconds", type=_positive_float, default=40.0,
                        help="length of the closed loop per document family (default 40)")
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--steps-only", action="store_true", help="only the per-step profile and the entity check")
    phases.add_argument("--load-only", action="store_true",
                        help="only the API latency (no step profile, no entity check)")
    parser.add_argument("--out", type=Path, default=None, metavar="FILE", help="also write the Markdown report here")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary directory (exported refs, documents, raw results)")
    advanced = parser.add_argument_group("advanced")
    advanced.add_argument("--rounds", type=_positive_int, default=2,
                          help="interleaved rounds of the API phase: base, head, base, head, ... (default 2)")
    advanced.add_argument("--memory", default="4g", help="memory cap of every container (default 4g)")
    advanced.add_argument("--port", type=int, default=0,
                          help="host port of the API container, on 127.0.0.1 (default: any free port)")
    advanced.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                          help="environment variable for the API and profile containers; repeatable")
    advanced.add_argument("--redis-image", default=REDIS_IMAGE,
                          help=f"image of the throwaway Redis (default {REDIS_IMAGE})")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = {}
    for item in args.env:
        key, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            parser.error(f"--env expects KEY=VALUE, got {item!r}")
        env[key] = value
    args.env = env
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    return args


def container_env(args: argparse.Namespace) -> dict[str, str]:
    """Environment shared by the API and the profile container: the defaults above, then ``--env``."""
    env = {**BASE_ENV, **API_ENV}
    env.update(args.env)
    workers = int(env["WEB_CONCURRENCY"])
    # Like the deployment script: the CPUs of the replica divided among the workers, two at least.
    env.setdefault("ORT_INTRA_OP_THREADS", str(max(2, int(args.cpus) // workers)))
    return env


# ---------------------------------------------------------------------------------------------------------------
# Git: exporting a ref
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class RefInfo:
    name: str  # as given: origin/main, a tag, WORKTREE
    commit: str  # abbreviated commit id (of HEAD for WORKTREE)
    dirty: bool = False  # WORKTREE only: uncommitted changes under app/, pii_shield/, config/

    @property
    def is_worktree(self) -> bool:
        return self.name == WORKTREE

    def describe(self) -> str:
        suffix = " + uncommitted changes" if self.dirty else ""
        return f"{self.name} @ {self.commit}{suffix}"


def git(runner, repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return runner.run(["git", "-C", str(repo), *args], text=text)


def resolve_ref(runner, repo: Path, ref: str) -> RefInfo:
    target = "HEAD" if ref == WORKTREE else ref
    try:
        commit = git(runner, repo, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}").stdout.strip()
    except HarnessError as exc:
        raise HarnessError(f"{ref!r} is not a commit of {repo}: {exc}") from exc
    if not commit:
        raise HarnessError(f"{ref!r} is not a commit of {repo}")
    abbreviated = git(runner, repo, "rev-parse", "--short=12", commit).stdout.strip() or commit[:12]
    dirty = False
    if ref == WORKTREE:
        dirty = bool(git(runner, repo, "status", "--porcelain", "--", *CODE_DIRS).stdout.strip())
    return RefInfo(ref, abbreviated, dirty)


def _extract_tar(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=BytesIO(data)) as archive:
        # The "data" filter (3.12, and the later patch releases of older versions) refuses paths that escape *dest*.
        archive.extractall(dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def materialise(runner, repo: Path, info: RefInfo, dest: Path) -> Path:
    """Put the code of *info* under *dest* (``app``, ``pii_shield``, ``config``) without touching *repo*'s checkout."""
    dest.mkdir(parents=True, exist_ok=True)
    if info.is_worktree:
        listing = git(runner, repo, "ls-files", "-co", "--exclude-standard", "-z", "--", *CODE_DIRS).stdout
        for relative in sorted({p for p in listing.split("\0") if p}):
            source = repo / relative
            if not source.is_file():  # tracked, deleted in the working tree
                continue
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    else:
        try:
            archive = git(runner, repo, "archive", "--format=tar", info.commit, *CODE_DIRS, text=False).stdout
        except HarnessError as exc:
            raise HarnessError(f"cannot export {info.name} ({', '.join(CODE_DIRS)}): {exc}") from exc
        _extract_tar(archive, dest)
    for name in CODE_DIRS:
        (dest / name).mkdir(exist_ok=True)  # a bind-mount source must exist
    return dest


# ---------------------------------------------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------------------------------------------

_FIRST = ["Rajesh", "Priya", "Arun", "Kavita", "Suresh", "Anita", "Vikram", "Neha", "Sanjay", "Meera", "Amit", "Deepa"]
_LAST = ["Sharma", "Menon", "Iyer", "Reddy", "Patel", "Gupta", "Joshi", "Nair", "Singh", "Kulkarni"]
_BANKS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak"]
_CITIES = ["Mumbai", "Pune", "Bengaluru", "Chennai", "Hyderabad", "Kochi"]


class _Synth:
    """Seeded generators for the synthetic families, in the style of Indian banking text."""

    def __init__(self, rnd: random.Random):
        self.rnd = rnd

    def digits(self, count: int) -> str:
        return str(self.rnd.randint(1, 9)) + "".join(self.rnd.choice("0123456789") for _ in range(count - 1))

    def name(self) -> str:
        return f"{self.rnd.choice(_FIRST)} {self.rnd.choice(_LAST)}"

    def pan(self) -> str:
        r = self.rnd
        letters = "".join(r.choice("ABCDEFGHJK") for _ in range(3))
        return letters + "P" + r.choice("ABCDEFGH") + self.digits(4) + r.choice("ABCDEFGH")

    def aadhaar(self) -> str:
        return f"{self.rnd.randint(2, 9)}{self.digits(3)} {self.digits(4)} {self.digits(4)}"

    def statement_line(self) -> str:
        r = self.rnd
        return (f"{r.randint(1, 28):02d}/{r.randint(1, 12):02d}/2025  UPI/CR/{self.digits(12)}/{self.name().upper()}/"
                f"{r.choice(_BANKS)}  Ref {self.digits(12)}  A/c {self.digits(14)}  Dr {r.randint(100, 99999)}.00  "
                f"Bal {r.randint(10000, 999999)}.50")

    def prose_paragraph(self) -> str:
        r, name = self.rnd, self.name()
        first, last = name.lower().split()
        return (f"{name} visited the {r.choice(_BANKS)} Bank {r.choice(_CITIES)} branch on {r.randint(1, 28)}th March "
                f"2025 and discussed the home loan. He can be reached on +91 {r.choice('6789')}{self.digits(9)} or at "
                f"{first}.{last}@gmail.com. The branch manager {self.name()} confirmed the documents were received "
                f"and the account {self.digits(11)} will be updated shortly.")

    def json_record(self) -> str:
        r, name = self.rnd, self.name()
        return ('{"customer": {"name": "%s", "email": "%s@example.com", "phone": "+91 %s%s", "pan": "%s", '
                '"aadhaar": "%s", "account": "%s", "city": "%s"}}'
                % (name, name.split()[0].lower(), r.choice("6789"), self.digits(9), self.pan(), self.aadhaar(),
                   self.digits(13), r.choice(_CITIES)))


def make_document(family: str, size: str, index: int, bank_records: Sequence[str] = ()) -> str:
    """The *index*-th document of a family and size: records joined by newlines, as close to the size as it gets.

    Deterministic: the same arguments give the same text on every machine, so both refs see the same documents.
    """
    rnd = random.Random(f"{family}/{size}/{index}")  # a str seed is hashed, not Python-version dependent
    synth = _Synth(rnd)
    draw = {
        "bank": lambda: rnd.choice(bank_records),
        "prose": synth.prose_paragraph,
        "json": synth.json_record,
        "statement": synth.statement_line,
    }[family]
    target = SIZE_CHARS[size]
    parts: list[str] = []
    length = 0
    while True:
        record = draw()
        grown = length + len(record) + (1 if parts else 0)
        if parts and abs(grown - target) >= abs(length - target):
            return "\n".join(parts)
        parts.append(record)
        length = grown


def load_bank_records(repo: Path) -> list[str]:
    path = repo / "examples" / "indian_banking_test_data.json"
    try:
        return [record["text"] for record in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarnessError(f"cannot read the banking records from {path}: {exc}") from exc


class DocumentSet:
    """The synthetic documents, generated on first use and cached (the bank records are read from the repo)."""

    def __init__(self, bank_loader: Callable[[], Sequence[str]]):
        self._bank_loader = bank_loader
        self._bank: Sequence[str] | None = None
        self._cache: dict[tuple[str, str, int], str] = {}
        self._lock = threading.Lock()

    def get(self, family: str, size: str, index: int) -> str:
        key = (family, size, index)
        with self._lock:
            text = self._cache.get(key)
            if text is None:
                if family == "bank" and self._bank is None:
                    self._bank = self._bank_loader()
                text = self._cache[key] = make_document(family, size, index, self._bank or ())
            return text


def document_payload(docs: DocumentSet, families: Iterable[str]) -> dict:
    """What the profile container reads: one medium document per family to time, and the documents to check."""
    profile = {family: docs.get(family, "medium", 0) for family in families}
    check = {
        f"{family}/{size}/{index}": docs.get(family, size, index)
        for family in families
        for size in SIZE_CHARS
        for index in CHECK_INDEXES
    }
    return {"profile": profile, "check": check}


# ---------------------------------------------------------------------------------------------------------------
# The load generator (standard library only)
# ---------------------------------------------------------------------------------------------------------------

# No proxy: a developer's http_proxy must not route requests to 127.0.0.1 through somewhere else.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_text(url: str, text: str, timeout: float = REQUEST_TIMEOUT_S) -> float:
    """POST *text* to the anonymize endpoint and return the latency in milliseconds; raises on any failure."""
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with _OPENER.open(request, timeout=timeout) as response:
        response.read()
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status != 200:
            raise HarnessError(f"HTTP {response.status}")
    return elapsed


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100.0 * len(ordered)) - 1)]


def wait_healthy(
    base_url: str,
    timeout: float,
    is_running: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    deadline = clock() + timeout
    while True:
        try:
            with _OPENER.open(base_url + "/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, http.client.HTTPException):
            pass
        if is_running is not None and not is_running():
            raise HarnessError("the API container exited before it became healthy")
        if clock() >= deadline:
            raise HarnessError(f"the API did not become healthy within {timeout:g}s")
        sleep(0.5)


def warm_up(base_url: str, docs: DocumentSet, families: Sequence[str], workers: int) -> None:
    """Per worker, a few requests of every family: the first ones pay one-off costs that are not the ref's."""
    url = base_url + "/anonymize_unique"
    for _ in range(workers):
        for family in families:
            for size in ("small", "medium"):
                post_text(url, docs.get(family, size, 1000))


def lone_latencies(base_url: str, docs: DocumentSet, family: str, size: str, reps: int) -> list[float]:
    """Sequential requests with *reps* different documents of one size; a failed request is skipped."""
    latencies = []
    for index in range(reps):
        try:
            latencies.append(post_text(base_url + "/anonymize_unique", docs.get(family, size, index)))
        except (OSError, http.client.HTTPException, HarnessError):
            continue
    return latencies


def closed_loop(
    base_url: str, docs: DocumentSet, family: str, clients: int, seconds: float, timeout: float = REQUEST_TIMEOUT_S
) -> dict:
    """*clients* threads, each sending its next request when the previous one returns, for *seconds*.

    The request mix and every document are a function of the client number and the request number, so both refs
    are offered the same stream.  Each request opens a connection, so the gunicorn workers share the clients evenly.
    """
    url = base_url + "/anonymize_unique"
    sizes, weights = zip(*MIX)
    deadline = time.perf_counter() + seconds
    stop = threading.Event()
    latencies: list[list[float]] = [[] for _ in range(clients)]
    errors = [0] * clients

    def client(number: int) -> None:
        rnd = random.Random(1000 + number)
        sent = 0
        while not stop.is_set() and time.perf_counter() < deadline:
            size = rnd.choices(sizes, weights)[0]
            sent += 1
            try:
                latencies[number].append(post_text(url, docs.get(family, size, number * 7919 + sent), timeout))
            except (OSError, http.client.HTTPException, HarnessError):
                errors[number] += 1
                time.sleep(0.05)  # do not spin when the server is down

    threads = [threading.Thread(target=client, args=(n,), daemon=True) for n in range(clients)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    finally:
        stop.set()
    wall = time.perf_counter() - started
    everything = [value for per_client in latencies for value in per_client]
    return {
        "requests": len(everything),
        "errors": sum(errors),
        "req_per_s": len(everything) / wall if wall > 0 else 0.0,
        "p50": percentile(everything, 50),
        "p95": percentile(everything, 95),
        "p99": percentile(everything, 99),
    }


# ---------------------------------------------------------------------------------------------------------------
# Comparing results (pure functions)
# ---------------------------------------------------------------------------------------------------------------


def percent_change(base: float | None, head: float | None) -> float | None:
    if base is None or head is None or base == 0:
        return None
    return (head / base - 1.0) * 100.0


def spread_percent(values: Sequence[float]) -> float | None:
    """(largest - smallest) / median over the rounds, in percent; ``None`` with fewer than two rounds."""
    if len(values) < 2:
        return None
    middle = statistics.median(values)
    return (max(values) - min(values)) / middle * 100.0 if middle else None


@dataclasses.dataclass
class Comparison:
    """One metric of both refs: a value per round each, reduced to a median, the change and the spread."""

    base: float | None
    head: float | None
    change: float | None
    base_spread: float | None
    head_spread: float | None

    @property
    def within_noise(self) -> bool:
        spreads = [s for s in (self.base_spread, self.head_spread) if s is not None]
        return self.change is not None and bool(spreads) and abs(self.change) <= max(spreads)


def compare_values(base: Sequence[float], head: Sequence[float]) -> Comparison:
    base_value = statistics.median(base) if base else None
    head_value = statistics.median(head) if head else None
    return Comparison(
        base_value, head_value, percent_change(base_value, head_value), spread_percent(base), spread_percent(head)
    )


@dataclasses.dataclass
class StepRow:
    step: str
    base: float
    head: float

    @property
    def change(self) -> float | None:
        return percent_change(self.base, self.head)


def merge_steps(base_steps: dict[str, float], head_steps: dict[str, float], order: Sequence[str]) -> list[StepRow]:
    """The step table: the steps of both refs in pipeline order, without those below MIN_STEP_MS in both."""
    known = [step for step in order if step in base_steps or step in head_steps]
    extra = sorted((set(base_steps) | set(head_steps)) - set(known))
    rows = [StepRow(step, base_steps.get(step, 0.0), head_steps.get(step, 0.0)) for step in known + extra]
    return [row for row in rows if max(row.base, row.head) >= MIN_STEP_MS]


@dataclasses.dataclass
class EntityDiff:
    only_base: list[tuple]  # (start, end, type)
    only_head: list[tuple]
    score_changes: int  # same span and type, score moved by more than SCORE_TOLERANCE

    @property
    def status(self) -> str:
        if self.only_base or self.only_head:
            return "differs"
        return "scores differ" if self.score_changes else "identical"


def diff_entities(base: Sequence[Sequence], head: Sequence[Sequence], tolerance: float = SCORE_TOLERANCE) -> EntityDiff:
    """Compare two ``[start, end, type, score]`` lists by span and type (a score alone is reported separately)."""
    base_keys = Counter((s, e, t) for s, e, t, *_ in base)
    head_keys = Counter((s, e, t) for s, e, t, *_ in head)
    base_scores = {(s, e, t): rest[0] for s, e, t, *rest in base if rest}
    head_scores = {(s, e, t): rest[0] for s, e, t, *rest in head if rest}
    shared = base_keys.keys() & head_keys.keys()
    score_changes = sum(
        1
        for key in shared
        if key in base_scores and key in head_scores and abs(base_scores[key] - head_scores[key]) > tolerance
    )
    only_base = sorted((base_keys - head_keys).elements())
    only_head = sorted((head_keys - base_keys).elements())
    return EntityDiff(only_base, only_head, score_changes)


# ---------------------------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------------------------


def fmt_ms(value: float | None, digits: int | None = None) -> str:
    if value is None:
        return "n/a"
    if digits is None:
        digits = 0 if abs(value) >= 100 else 1
    return f"{value:,.{digits}f}"


def fmt_change(change: float | None, noise: bool = False) -> str:
    if change is None:
        return "n/a"
    text = "0%" if round(change) == 0 else f"{change:+.0f}%"
    return f"{text} (noise)" if noise else text


def fmt_spread(spread: float | None) -> str:
    return "n/a" if spread is None else f"{spread:.0f}%"


def table(header: Sequence[str], rows: Sequence[Sequence[str]], align: str) -> list[str]:
    """A Markdown table; *align* has one letter per column, ``l`` or ``r``."""
    rule = ["---:" if letter == "r" else "---" for letter in align]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(rule) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def step_change_text(row: StepRow) -> str:
    if min(row.base, row.head) < MIN_STEP_MS:
        if max(row.base, row.head) < 1.0:
            return "n/a"  # a step this small has no meaningful percentage
        return "new" if row.base < MIN_STEP_MS else "gone"
    return fmt_change(row.change)


def step_rows_text(rows: Sequence[StepRow], sums: tuple[float, float], totals: tuple[float, float]) -> list[list[str]]:
    lines = [[row.step, fmt_ms(row.base, 1), fmt_ms(row.head, 1), step_change_text(row)] for row in rows]
    for label, (base, head) in (("**sum of the steps**", sums), ("**total, no timers**", totals)):
        lines.append([label, fmt_ms(base, 1), fmt_ms(head, 1), fmt_change(percent_change(base, head))])
    return lines


def _arrow(base, head) -> str:
    return f"{base} -> {head}"


def environment_lines(meta: dict) -> list[str]:
    refs = meta["refs"]
    shown_env = ", ".join(f"{k}={v}" for k, v in sorted(meta["api_env"].items()) if k in meta["env_shown"])
    lines = [
        f"- base: `{refs['base']['label']}`",
        f"- head: `{refs['head']['label']}`",
        f"- image: `{meta['image']}` ({meta['image_id']})",
        f"- cpu cap: {meta['cpus']:g} CPUs per container (docker --cpus), memory cap {meta['memory']}",
        f"- host: {meta['host_cpus']} CPUs, {meta['platform']}; docker {meta['docker']}",
        f"- api container: {shown_env}",
        f"- run: {meta['settings']}",
        f"- date: {meta['generated']}",
    ]
    before, after = meta.get("load_before"), meta.get("load_after")
    if before:
        text = f"- host load average (1/5/15 min): before {', '.join(f'{v:.1f}' for v in before)}"
        if after:
            text += f"; after {', '.join(f'{v:.1f}' for v in after)}"
        lines.append(text)
        busiest = max([before[0]] + ([after[0]] if after else []))
        if busiest > 0.5 * meta["host_cpus"]:
            lines.append(f"- **warning: the host was busy (load {busiest:.1f} on {meta['host_cpus']} CPUs); "
                         "the numbers below carry noise from other work on this machine**")
    return lines


def steps_section(profile: dict, families: Sequence[str]) -> list[str]:
    base, head = profile.get("base"), profile.get("head")
    lines = ["## Where the time goes: one `anonymize()` call, per step", ""]
    if not base or not head:
        return lines + ["Not available for both refs; see the errors at the end.", ""]
    order = base.get("step_order") or head.get("step_order") or []
    lines += [
        "Each step is an exclusive timer (nested steps are subtracted), so the steps add up to the wall time; "
        f"a timer costs about {max(base.get('timer_overhead_us', 0), head.get('timer_overhead_us', 0)):.1f} us. "
        "Medians in milliseconds over the repetitions; change = head against base.",
        "",
    ]
    for family in families:
        b, h = base["documents"].get(family), head["documents"].get(family)
        if not b or not h:
            continue
        rows = merge_steps(b["steps"], h["steps"], order)
        sums = (sum(b["steps"].values()), sum(h["steps"].values()))
        lines += [
            f"### {family} ({b['chars']:,} characters)",
            "",
            f"entities {_arrow(b['entities'], h['entities'])}; second NER pass "
            f"{_arrow('yes' if b['second_pass'] else 'no', 'yes' if h['second_pass'] else 'no')}; "
            f"NER model runs {_arrow(b['ner_runs'], h['ner_runs'])}",
            "",
            *table(["step", "base ms", "head ms", "change"],
                   step_rows_text(rows, sums, (b["total_ms"], h["total_ms"])), "lrrr"),
            "",
        ]
    notes = sorted({f"{role}: {note}" for role in ("base", "head") for note in (profile[role].get("notes") or [])})
    if notes:
        lines += ["Profiler notes: " + "; ".join(notes), ""]
    return lines


def _run_values(load: dict, role: str, getter: Callable[[dict], float | None]) -> list[float]:
    values = (getter(run) for run in load["runs"] if run["role"] == role)
    return [v for v in values if v is not None]


def verdict_line(judged: Sequence[tuple[Comparison, bool]]) -> str | None:
    """Count the comparisons the head wins, loses or cannot tell apart from noise.

    *judged* holds ``(comparison, higher_is_better)``; a change within the spread between rounds counts as noise.
    """
    counts = Counter()
    for comparison, higher_is_better in judged:
        if comparison.change is None:
            continue
        if comparison.within_noise or comparison.change == 0:
            counts["noise"] += 1
        elif (comparison.change > 0) == higher_is_better:
            counts["better"] += 1
        else:
            counts["worse"] += 1
    total = sum(counts.values())
    if not total:
        return None
    return (f"Head against base, {total} comparisons: {counts['better']} better, {counts['worse']} worse, "
            f"{counts['noise']} within noise.")


def api_section(load: dict, families: Sequence[str], settings: dict) -> list[str]:
    lines = [
        "## Latency through the API",
        "",
        f"One container per ref, {load['rounds']} interleaved round(s) ({', '.join(load['order'])}); "
        "each value is the median over the rounds and spread = (largest - smallest round) / median. "
        "`(noise)` marks a change that is not larger than the spread of either side.",
        "",
        f"### Lone request (median of {settings['reps']} requests per size, milliseconds, lower is better)",
        "",
    ]
    judged: list[tuple[Comparison, bool]] = []
    rows = []
    for family in families:
        for size in SIZE_CHARS:
            def lone(run, family=family, size=size):
                return run.get("lone", {}).get(family, {}).get(size)

            comparison = compare_values(_run_values(load, "base", lone), _run_values(load, "head", lone))
            judged.append((comparison, False))
            rows.append([family, SIZE_LABELS[size], fmt_ms(comparison.base), fmt_ms(comparison.head),
                         fmt_change(comparison.change, comparison.within_noise),
                         fmt_spread(comparison.base_spread), fmt_spread(comparison.head_spread)])
    lines += table(["documents", "size", "base", "head", "change", "spread base", "spread head"], rows, "llrrrrr")
    lines += ["", f"### Closed loop ({settings['clients']} clients, {settings['seconds']:g} s per family, "
              "mix 70% small / 25% medium / 5% large)", ""]
    rows = []
    for family in families:
        for metric, label, digits in (("req_per_s", "requests/s (higher is better)", 2), ("p50", "p50 ms", None),
                                      ("p95", "p95 ms", None), ("p99", "p99 ms", None)):
            def closed(run, family=family, metric=metric):
                return run.get("closed", {}).get(family, {}).get(metric)

            comparison = compare_values(_run_values(load, "base", closed), _run_values(load, "head", closed))
            judged.append((comparison, metric == "req_per_s"))
            rows.append([family, label, fmt_ms(comparison.base, digits), fmt_ms(comparison.head, digits),
                         fmt_change(comparison.change, comparison.within_noise),
                         fmt_spread(comparison.base_spread), fmt_spread(comparison.head_spread)])
    lines += table(["documents", "metric", "base", "head", "change", "spread base", "spread head"], rows, "llrrrrr")
    verdict = verdict_line(judged)
    if verdict:
        lines += ["", verdict]
    errors = Counter()
    for run in load["runs"]:
        closed_errors = sum(c.get("errors", 0) for c in run.get("closed", {}).values())
        errors[run["role"]] += run.get("lone_errors", 0) + closed_errors
    if errors["base"] or errors["head"]:
        lines += ["", f"Failed requests (lone and closed loop): base {errors['base']}, head {errors['head']}."]
    startup = {role: [r["startup_s"] for r in load["runs"] if r["role"] == role and r.get("startup_s") is not None]
               for role in ("base", "head")}
    if startup["base"] and startup["head"]:
        lines += ["", f"Container start to healthy (median): base {statistics.median(startup['base']):.0f} s, "
                      f"head {statistics.median(startup['head']):.0f} s."]
    return lines + [""]


def _snippet(text: str, start: int, end: int) -> str:
    shown = text[start:end]
    return repr(shown if len(shown) <= 40 else shown[:37] + "...")


def correctness_section(profile: dict, texts: dict[str, str], limit: int = 25) -> list[str]:
    base, head = profile.get("base"), profile.get("head")
    lines = ["## Detected entities (same documents, spans and types)", ""]
    if not base or not head:
        return lines + ["Not available for both refs; see the errors at the end.", ""]
    names = [name for name in base["entities"] if name in head["entities"]]
    diffs = {name: diff_entities(base["entities"][name], head["entities"][name]) for name in names}
    counts = Counter(diff.status for diff in diffs.values())
    lines.append(f"{len(names)} documents compared: {counts['identical']} identical, {counts['scores differ']} "
                 f"with the same spans and types but scores that moved by more than {SCORE_TOLERANCE:g}, "
                 f"{counts['differs']} with different spans or types.")
    bad = [name for name in names if diffs[name].status == "differs"]
    if bad:
        lines += ["", *table(["document", "entities base", "entities head", "only in base", "only in head"],
                             [[n, str(len(base["entities"][n])), str(len(head["entities"][n])),
                               str(len(diffs[n].only_base)), str(len(diffs[n].only_head))] for n in bad], "lrrrr"), ""]
        shown = 0
        for name in bad:
            text = texts.get(name, "")
            for label, spans in (("base only", diffs[name].only_base), ("head only", diffs[name].only_head)):
                for start, end, kind in spans:
                    if shown >= limit:
                        break
                    lines.append(f"- `{name}` {label}: {kind} {_snippet(text, start, end)} at {start}")
                    shown += 1
        total = sum(len(diffs[n].only_base) + len(diffs[n].only_head) for n in bad)
        if total > shown:
            lines.append(f"- ... and {total - shown} more")
    return lines + [""]


def format_report(data: dict) -> str:
    """The Markdown report for the collected *data* (see ``Harness.run`` for its shape)."""
    meta = data["meta"]
    refs = meta["refs"]
    lines = [f"# Latency: {refs['base']['name']} vs {refs['head']['name']}", "", "## Environment", ""]
    lines += environment_lines(meta) + [""]
    families = meta["families"]
    if data.get("profile") is not None:
        lines += steps_section(data["profile"], families)
    if data.get("load"):
        lines += api_section(data["load"], families, meta["settings_dict"])
    if data.get("profile") is not None:
        lines += correctness_section(data["profile"], data.get("check_texts", {}))
    elif meta["mode"] == "load":
        lines += ["## Detected entities", "", "Not run (--load-only).", ""]
    if data.get("errors"):
        lines += ["## Errors", "", *[f"- {error}" for error in data["errors"]], ""]
    lines += [
        "## How to read this",
        "",
        "- Compare like with like: the same machine, the same image, the same cap, containers one at a time. "
        "Run it when nothing else is using the CPU, and run `--base HEAD --head HEAD` once to see this "
        "machine's own noise.",
        "- The step profile attaches timers by monkey-patching, so each step is slightly slower than in "
        "production; the total row is measured without timers.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Cleanup that survives signals
# ---------------------------------------------------------------------------------------------------------------

_TERMINATING = tuple(s for s in (signal.SIGTERM, getattr(signal, "SIGHUP", None)) if s is not None)
_ALL_SIGNALS = _TERMINATING + (signal.SIGINT,)


@contextlib.contextmanager
def terminate_on_signals() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into ``SystemExit`` so the ``finally`` blocks (the cleanup) run, as Ctrl-C does."""
    previous = {}

    def handler(signum, frame):
        raise SystemExit(128 + signum)

    try:
        for number in _TERMINATING:
            previous[number] = signal.signal(number, handler)
    except ValueError:  # not the main thread: leave the signals alone
        previous = {}
    try:
        yield
    finally:
        for number, old in previous.items():
            if old is not None:  # None: the handler was not installed from Python
                signal.signal(number, old)


@contextlib.contextmanager
def signals_ignored() -> Iterator[None]:
    """Keep a second Ctrl-C from interrupting the cleanup itself."""
    previous = {}
    try:
        for number in _ALL_SIGNALS:
            previous[number] = signal.signal(number, signal.SIG_IGN)
    except ValueError:
        previous = {}
    try:
        yield
    finally:
        for number, old in previous.items():
            if old is not None:
                signal.signal(number, old)


# ---------------------------------------------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------------------------------------------


def project_name(repo: Path) -> str:
    """Compose's default project name: the directory name, lower case, reduced to ``[a-z0-9_-]``."""
    return os.environ.get("COMPOSE_PROJECT_NAME") or re.sub(r"[^a-z0-9_-]", "", repo.name.lower())


def default_image(runner, repo: Path) -> str:
    """The image of the ``app`` service of this repo's compose project, for example ``<project>-app``."""
    try:
        config = json.loads(runner.run(["docker", "compose", "config", "--format", "json"], cwd=repo).stdout)
        service = config.get("services", {}).get("app", {})
        return service.get("image") or f"{config.get('name') or project_name(repo)}-app"
    except (HarnessError, ValueError, AttributeError):
        pass
    try:
        images = runner.run(["docker", "compose", "config", "--images"], cwd=repo).stdout.split()
        candidates = [image for image in images if image.endswith("-app")]
        if candidates:
            return candidates[0]
    except HarnessError:
        pass
    return f"{project_name(repo)}-app"


def docker_summary(runner) -> str:
    try:
        version, cpus, memory, system = runner.run(
            ["docker", "info", "--format", "{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.OperatingSystem}}"]
        ).stdout.strip().split("|", 3)
        return f"{version} ({system}; VM {cpus} CPUs, {int(memory) / 2**30:.1f} GiB)"
    except (HarnessError, ValueError):
        return "unknown"


def parse_profile_output(stdout: str) -> dict:
    """The profile container prints its JSON as the last line of stdout."""
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    raise HarnessError("the profile container printed no JSON result")


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except (AttributeError, OSError):
        return None


class Harness:
    """One comparison run: exports the refs, drives the containers, collects the results, cleans up."""

    def __init__(
        self,
        args: argparse.Namespace,
        runner=None,
        repo: Path = REPO_ROOT,
        log: Callable[[str], None] | None = None,
    ):
        self.args = args
        self.runner = runner or SubprocessRunner()
        self.repo = Path(repo)
        self.run_id = uuid.uuid4().hex[:6]
        self.network = f"{CONTAINER_PREFIX}{self.run_id}-net"
        self.label = f"cmp-run={self.run_id}"
        self.containers: list[str] = []
        self.network_requested = False
        self.tmp: Path | None = None
        self.env = container_env(args)
        self.started = time.monotonic()
        self._log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.data: dict = {"errors": [], "profile": None, "load": None, "check_texts": {}}

    def log(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.started)
        self._log(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {message}")

    def docker(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.runner.run(["docker", *args], **kwargs)

    def container_name(self, role: str) -> str:
        name = f"{CONTAINER_PREFIX}{self.run_id}-{role}"
        self.containers.append(name)  # registered before it exists, so a failed start is still cleaned up
        return name

    # -- the run -------------------------------------------------------------------------------------------

    def run(self) -> dict:
        args = self.args
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{CONTAINER_PREFIX}{self.run_id}-"))
        refs = {role: resolve_ref(self.runner, self.repo, getattr(args, role)) for role in ("base", "head")}
        self.log(f"base {refs['base'].describe()}; head {refs['head'].describe()}")
        image = args.image or default_image(self.runner, self.repo)
        try:
            image_id = self.docker("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
        except HarnessError as exc:
            raise HarnessError(f"image {image!r} is not available locally (docker compose build app?): {exc}") from exc
        if not image_id:
            raise HarnessError(f"image {image!r} not found")
        sources = {}
        for role, info in refs.items():
            sources[role] = materialise(self.runner, self.repo, info, self.tmp / "src" / role)
        self.log(f"image {image} ({image_id[:19]}); exported both refs")

        docs = DocumentSet(lambda: load_bank_records(self.repo))
        payload = document_payload(docs, args.docs)
        self.data["check_texts"] = payload["check"]
        self.docs_file = self.tmp / "documents.json"
        self.docs_file.write_text(json.dumps(payload), encoding="utf-8")
        self.image, self.sources, self.docs = image, sources, docs

        mode = "steps" if args.steps_only else "load" if args.load_only else "both"
        self.data["meta"] = {
            "refs": {role: {"name": info.name, "commit": info.commit, "dirty": info.dirty, "label": info.describe()}
                     for role, info in refs.items()},
            "image": image, "image_id": image_id[:19] + ("..." if len(image_id) > 19 else ""),
            "cpus": args.cpus, "memory": args.memory, "host_cpus": os.cpu_count() or 0,
            "platform": platform.platform(), "docker": docker_summary(self.runner),
            "api_env": self.env, "env_shown": ("WEB_CONCURRENCY", "ORT_INTRA_OP_THREADS", "NLP_ENGINE", "AUTH_ENABLED"),
            "families": list(args.docs), "mode": mode, "generated": now_utc(),
            "load_before": load_average(),
            "settings_dict": {"reps": args.reps, "clients": args.clients, "seconds": args.seconds,
                              "rounds": args.rounds},
        }
        self.data["meta"]["settings"] = (
            f"documents {','.join(args.docs)}; reps {args.reps}; {args.clients} clients for {args.seconds:g} s; "
            f"{args.rounds} round(s); phases {mode}"
        )
        if not args.load_only:
            self.phase("profile", self.run_profiles)
        if not args.steps_only:
            self.phase("load", self.run_load)
        self.data["meta"]["load_after"] = load_average()
        return self.data

    def phase(self, name: str, function: Callable[[], None]) -> None:
        """A phase that fails does not take the others with it: the error goes in the report."""
        try:
            function()
        except Exception as exc:  # HarnessError, or a connection that failed mid-run
            self.log(f"{name} phase failed: {exc}")
            self.data["errors"].append(f"{name} phase failed: {exc}")

    # -- phase 1: in-process profile ------------------------------------------------------------------------

    def code_mounts(self, role: str) -> list[str]:
        mounts = []
        for name in CODE_DIRS:
            mounts += ["-v", f"{self.sources[role] / name}:/app/{name}:ro"]
        return mounts

    def env_args(self, env: dict[str, str]) -> list[str]:
        return [item for key, value in env.items() for item in ("-e", f"{key}={value}")]

    def run_profiles(self) -> None:
        profiles: dict = {}
        self.data["profile"] = profiles  # filled as each ref finishes, so a failure on the second keeps the first
        for role in ("base", "head"):
            name = self.container_name(f"prof-{role}")
            self.log(f"profiling {role} in {name} (a few minutes)")
            env = {k: v for k, v in self.env.items() if k not in ("OTEL_EXPORTER_OTLP_ENDPOINT",)}
            result = self.docker(
                "run", "--rm", "--name", name, "--label", self.label, "--cpus", f"{self.args.cpus:g}",
                "--memory", self.args.memory, *self.env_args(env), *self.code_mounts(role),
                "-v", f"{PROFILE_SCRIPT}:/cmp/compare_branches_profile.py:ro",
                "-v", f"{self.docs_file}:/cmp/documents.json:ro",
                "-w", "/app", "--entrypoint", "python", self.image,
                "-B", "/cmp/compare_branches_profile.py", "--repo", "/app", "--docs", "/cmp/documents.json",
                "--reps", str(self.args.reps),
                timeout=PROFILE_TIMEOUT_S,
                on_stderr=lambda line: self.log("  " + line) if line.startswith("[profile]") else None,
            )
            profiles[role] = parse_profile_output(result.stdout)

    # -- phase 2: through the API ---------------------------------------------------------------------------

    def run_load(self) -> None:
        args = self.args
        order = [role for _ in range(args.rounds) for role in ("base", "head")]
        load = {"rounds": args.rounds, "order": order, "runs": []}
        self.data["load"] = load
        self.network_requested = True
        self.docker("network", "create", "--label", self.label, self.network)
        redis = self.container_name("redis")
        self.docker("run", "-d", "--name", redis, "--label", self.label, "--network", self.network, args.redis_image)
        self.wait_redis(redis)
        for position, role in enumerate(order):
            load["runs"].append(self.api_run(role, position // 2 + 1, redis))

    def wait_redis(self, redis: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if "PONG" in self.docker("exec", redis, "redis-cli", "ping", check=False).stdout:
                return
            if time.monotonic() >= deadline:
                raise HarnessError("the throwaway Redis did not answer within 60 s")
            time.sleep(0.5)

    def api_run(self, role: str, round_number: int, redis: str) -> dict:
        args = self.args
        name = self.container_name(f"api-{role}-r{round_number}")
        env = dict(self.env, REDIS_URL=f"redis://{redis}:6379/0")
        publish = f"127.0.0.1:{args.port}:8000" if args.port else "127.0.0.1::8000"
        workers = int(self.env["WEB_CONCURRENCY"])
        self.log(f"[{role} round {round_number}] starting {name}")
        started = time.monotonic()
        try:
            self.docker(
                "run", "-d", "--name", name, "--label", self.label, "--network", self.network,
                "--cpus", f"{args.cpus:g}", "--memory", args.memory, "-p", publish,
                *self.env_args(env), *self.code_mounts(role), self.image,
            )
            port = self.published_port(name)
            base_url = f"http://127.0.0.1:{port}"
            try:
                wait_healthy(base_url, STARTUP_TIMEOUT_S, is_running=lambda: self.is_running(name))
                self.wait_workers_warm(name, workers)
            except HarnessError as exc:
                logs = self.docker("logs", "--tail", "25", name, check=False)
                raise HarnessError(f"{exc}\n{(logs.stdout + logs.stderr).strip()}") from exc
            result = {"role": role, "round": round_number, "startup_s": time.monotonic() - started,
                      "lone": {}, "closed": {}, "lone_errors": 0}
            self.log(f"[{role} round {round_number}] healthy after {result['startup_s']:.0f} s; warming up")
            warm_up(base_url, self.docs, args.docs, workers)
            for family in args.docs:
                result["lone"][family] = {}
                for size in SIZE_CHARS:
                    latencies = lone_latencies(base_url, self.docs, family, size, args.reps)
                    result["lone_errors"] += args.reps - len(latencies)
                    result["lone"][family][size] = statistics.median(latencies) if latencies else None
                    self.log(f"[{role} round {round_number}] lone {family}/{size}: "
                             f"{fmt_ms(result['lone'][family][size])} ms")
            for family in args.docs:
                result["closed"][family] = closed_loop(base_url, self.docs, family, args.clients, args.seconds)
                closed = result["closed"][family]
                self.log(f"[{role} round {round_number}] closed loop {family}: {closed['req_per_s']:.2f} req/s, "
                         f"p95 {fmt_ms(closed['p95'])} ms, {closed['errors']} errors")
            return result
        finally:
            with contextlib.suppress(Exception):  # never two API containers at once
                self.docker("rm", "-f", name, check=False)

    def published_port(self, name: str) -> int:
        output = self.docker("port", name, "8000/tcp").stdout
        match = re.search(r":(\d+)\s*$", output.strip().splitlines()[0]) if output.strip() else None
        if not match:
            raise HarnessError(f"cannot tell which host port {name} is published on: {output!r}")
        return int(match.group(1))

    def is_running(self, name: str) -> bool:
        return self.docker("inspect", "--format", "{{.State.Running}}", name, check=False).stdout.strip() == "true"

    def wait_workers_warm(self, name: str, workers: int, timeout: float = 90.0) -> None:
        """Best effort: /health is answered by the first worker up, the others may still be loading."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = self.docker("logs", name, check=False)
            if (logs.stdout + logs.stderr).count("NLP model warm-up complete") >= workers:
                return
            time.sleep(1.0)
        self.log(f"note: {name} did not log {workers} worker warm-ups in {timeout:g} s; continuing")

    # -- cleanup ------------------------------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove every container and the network this run created, and the temporary directory (unless --keep)."""
        with signals_ignored():
            steps: list[Callable[[], object]] = []
            if self.containers:
                remove = ("rm", "-f", "-v", *self.containers)
                steps.append(lambda: self.docker(*remove, check=False, timeout=CLEANUP_TIMEOUT_S))
            steps.append(lambda: self._sweep_labelled())
            if self.network_requested:
                steps.append(lambda: self.docker("network", "rm", self.network, check=False, timeout=CLEANUP_TIMEOUT_S))
            for step in steps:
                try:
                    step()
                except Exception as exc:  # keep cleaning whatever else is left
                    self.log(f"cleanup: {exc}")
            if self.tmp is not None:
                if self.args.keep:
                    self.log(f"kept {self.tmp}")
                else:
                    shutil.rmtree(self.tmp, ignore_errors=True)

    def _sweep_labelled(self) -> None:
        """Containers carrying this run's label that were never registered (a start that failed half way)."""
        listed = self.docker("ps", "-aq", "--filter", f"label={self.label}", check=False,
                             timeout=CLEANUP_TIMEOUT_S).stdout.split()
        if listed:
            self.docker("rm", "-f", "-v", *listed, check=False, timeout=CLEANUP_TIMEOUT_S)


# ---------------------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, runner=None, repo: Path = REPO_ROOT) -> int:
    args = parse_args(argv)
    harness = Harness(args, runner, repo)
    exit_code = 0
    data: dict | None = None
    with terminate_on_signals():  # the cleanup is inside too: a SIGTERM during it must not end it half way
        try:
            data = harness.run()
        except Exception as exc:  # HarnessError, or anything else that stops the harness: still clean up and report
            print(f"error: {exc}", file=sys.stderr)
            exit_code = 1
            data = harness.data if "meta" in harness.data else None
            if data is not None:
                data["errors"].append(str(exc))
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
            exit_code = 130
        finally:
            harness.cleanup()
    if data is not None:
        report = format_report(data)
        print(report)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report + "\n", encoding="utf-8")
        if harness.tmp is not None and args.keep:
            results = {k: v for k, v in data.items() if k != "check_texts"}
            (harness.tmp / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        if data["errors"]:
            exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
