"""Tests for scripts/compare_branches.py and scripts/compare_branches_profile.py: everything that does not need Docker.

The Docker side is exercised with a fake runner and a stub HTTP server standing in for the API container, so the
orchestration (command lines, order of the rounds, cleanup) is checked without starting anything.
"""

import http.server
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


cb = _load("compare_branches_under_test", "compare_branches.py")
prof = _load("compare_branches_profile_under_test", "compare_branches_profile.py")


# ---------------------------------------------------------------------------------------------------------------
# A throwaway git repository
# ---------------------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    """Two commits, then a working tree with an edit, an untracked file, an ignored file and a deletion."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, ".gitignore", "__pycache__/\nignored.txt\n")
    _write(root, "app/main.py", "main v1\n")
    _write(root, "pii_shield/engine.py", "engine v1\n")
    _write(root, "config/a.yml", "a: 1\n")
    _write(root, "README.md", "readme\n")
    _write(root, "examples/indian_banking_test_data.json", json.dumps([{"text": "Account 12345678 of Rajesh Sharma"}]))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    _write(root, "pii_shield/engine.py", "engine v2\n")
    _write(root, "app/added.py", "added in the second commit\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    _write(root, "app/main.py", "main edited, not committed\n")
    _write(root, "app/untracked.py", "untracked\n")
    _write(root, "app/ignored.txt", "ignored\n")
    _write(root, "app/__pycache__/main.cpython-312.pyc", "bytecode")
    (root / "config" / "a.yml").unlink()
    return root


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


# ---------------------------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------------------------


def test_defaults():
    args = cb.parse_args(["--base", "origin/main", "--head", "WORKTREE"])
    assert (args.base, args.head) == ("origin/main", "WORKTREE")
    assert (args.cpus, args.reps, args.clients, args.seconds, args.rounds) == (2.0, 7, 4, 40.0, 2)
    assert args.docs == cb.FAMILIES == ("bank", "prose", "json", "statement")
    assert args.image is None and args.out is None
    assert not (args.keep or args.steps_only or args.load_only)


def test_all_options():
    args = cb.parse_args([
        "--base", "v1", "--head", "HEAD", "--image", "img:1", "--cpus", "1.5", "--docs", "json, bank,json",
        "--reps", "3",
        "--clients", "2", "--seconds", "5", "--steps-only", "--out", "r.md", "--keep", "--env", "A=b=c", "--env", "X=",
    ])
    assert args.image == "img:1" and args.cpus == 1.5 and args.docs == ("json", "bank")
    assert (args.reps, args.clients, args.seconds) == (3, 2, 5.0)
    assert args.steps_only and args.keep and args.out == Path("r.md")
    assert args.env == {"A": "b=c", "X": ""}


@pytest.mark.parametrize("argv", [
    ["--base", "a", "--head", "b", "--docs", "nope"],
    ["--base", "a", "--head", "b", "--reps", "0"],
    ["--base", "a", "--head", "b", "--cpus", "0"],
    ["--base", "a", "--head", "b", "--env", "NOVALUE"],
    ["--base", "a", "--head", "b", "--port", "70000"],
    ["--base", "a", "--head", "b", "--steps-only", "--load-only"],
    ["--base", "-oops", "--head", "b"],
    ["--head", "b"],
])
def test_bad_arguments_are_refused(argv):
    with pytest.raises(SystemExit) as excinfo:
        cb.parse_args(argv)
    assert excinfo.value.code == 2


def test_container_env_follows_the_cap_and_the_overrides():
    args = cb.parse_args(["--base", "a", "--head", "b", "--cpus", "8", "--env", "WEB_CONCURRENCY=2",
                          "--env", "NLP_ENGINE=spacy"])
    env = cb.container_env(args)
    assert env["ORT_INTRA_OP_THREADS"] == "4" and env["NLP_ENGINE"] == "spacy" and env["AUTH_ENABLED"] == "false"
    small = cb.container_env(cb.parse_args(["--base", "a", "--head", "b"]))
    assert small["ORT_INTRA_OP_THREADS"] == "2"


# ---------------------------------------------------------------------------------------------------------------
# Exporting a ref
# ---------------------------------------------------------------------------------------------------------------


def _checkout_state(repo: Path):
    return (_tree(repo), _git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "branch", "--show-current"))


def test_materialise_a_commit_does_not_touch_the_checkout(repo, tmp_path):
    runner = cb.SubprocessRunner()
    before = _checkout_state(repo)
    first = _git(repo, "rev-parse", "HEAD~1")
    info = cb.resolve_ref(runner, repo, first)
    out = cb.materialise(runner, repo, info, tmp_path / "out")
    assert _tree(out) == {
        "app/main.py": "main v1\n", "pii_shield/engine.py": "engine v1\n", "config/a.yml": "a: 1\n",
    }
    assert before == _checkout_state(repo)


def test_materialise_head_is_the_committed_state_not_the_edits(repo, tmp_path):
    runner = cb.SubprocessRunner()
    out = cb.materialise(runner, repo, cb.resolve_ref(runner, repo, "HEAD"), tmp_path / "out")
    assert (out / "app" / "main.py").read_text() == "main v1\n"
    assert (out / "pii_shield" / "engine.py").read_text() == "engine v2\n"
    assert (out / "app" / "added.py").exists() and (out / "config" / "a.yml").exists()
    assert not (out / "app" / "untracked.py").exists()


def test_materialise_worktree_has_tracked_untracked_and_edited_but_not_ignored_or_deleted(repo, tmp_path):
    runner = cb.SubprocessRunner()
    info = cb.resolve_ref(runner, repo, "WORKTREE")
    assert info.is_worktree and info.dirty and info.describe().endswith("+ uncommitted changes")
    out = cb.materialise(runner, repo, info, tmp_path / "out")
    assert _tree(out) == {
        "app/main.py": "main edited, not committed\n",
        "app/added.py": "added in the second commit\n",
        "app/untracked.py": "untracked\n",
        "pii_shield/engine.py": "engine v2\n",
    }
    assert (out / "config").is_dir()  # a bind-mount source must exist even when everything in it was deleted


def test_resolve_ref_reports_unknown_refs_and_a_clean_worktree(repo):
    runner = cb.SubprocessRunner()
    with pytest.raises(cb.HarnessError, match="not a commit"):
        cb.resolve_ref(runner, repo, "no-such-ref")
    _git(repo, "checkout", "-q", "--", "app/main.py", "config/a.yml")
    (repo / "app" / "untracked.py").unlink()
    assert cb.resolve_ref(runner, repo, "WORKTREE").dirty is False


def test_export_of_a_ref_without_the_code_directories_fails_clearly(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    _write(bare, "app/main.py", "x")
    _git(bare, "add", "-A")
    _git(bare, "commit", "-q", "-m", "only app")
    runner = cb.SubprocessRunner()
    with pytest.raises(cb.HarnessError, match="cannot export"):
        cb.materialise(runner, bare, cb.resolve_ref(runner, bare, "HEAD"), tmp_path / "out")


# ---------------------------------------------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------------------------------------------

BANK = ["Account 12345678 of Rajesh Sharma at HDFC Bank, Mumbai " * 5] * 3 + ["Short record for Priya Menon"]


@pytest.mark.parametrize("family", cb.FAMILIES)
def test_documents_are_deterministic_and_close_to_the_target_size(family):
    for size, target in cb.SIZE_CHARS.items():
        first = cb.make_document(family, size, 0, BANK)
        assert first == cb.make_document(family, size, 0, BANK)
        assert first != cb.make_document(family, size, 1, BANK)
        assert abs(len(first) - target) < 0.35 * target + 300, (family, size, len(first))


def test_document_families_look_like_their_name():
    assert "@gmail.com" in cb.make_document("prose", "medium", 0)
    assert cb.make_document("json", "small", 0).startswith('{"customer"')
    assert "UPI/CR/" in cb.make_document("statement", "small", 0)
    assert cb.make_document("bank", "small", 0, ["only record " * 24]) == "only record " * 24  # one record is nearest


def test_document_payload_has_a_profile_text_per_family_and_the_check_texts(repo):
    docs = cb.DocumentSet(lambda: cb.load_bank_records(repo))
    payload = cb.document_payload(docs, ("bank", "prose"))
    assert set(payload["profile"]) == {"bank", "prose"}
    expected = {f"{f}/{s}/{i}" for f in ("bank", "prose") for s in cb.SIZE_CHARS for i in cb.CHECK_INDEXES}
    assert set(payload["check"]) == expected
    assert "Rajesh Sharma" in payload["check"]["bank/small/0"]


def test_missing_bank_records_are_a_harness_error(tmp_path):
    with pytest.raises(cb.HarnessError, match="banking records"):
        cb.load_bank_records(tmp_path)


# ---------------------------------------------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------------------------------------------


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert [cb.percentile(values, p) for p in (50, 95, 99, 100)] == [50, 95, 99, 100]
    assert cb.percentile([7], 99) == 7 and cb.percentile([], 50) is None


def test_compare_values_reduces_rounds_and_flags_noise():
    comparison = cb.compare_values([100.0, 110.0], [90.0, 100.0])
    assert comparison.base == 105.0 and comparison.head == 95.0
    assert comparison.change == pytest.approx(-9.52, abs=0.01)
    assert comparison.base_spread == pytest.approx(9.52, abs=0.01)
    assert comparison.within_noise  # 9.5% change, 9.5% spread
    assert not cb.compare_values([100.0, 101.0], [50.0, 51.0]).within_noise
    single = cb.compare_values([100.0], [50.0])
    assert single.base_spread is None and not single.within_noise
    assert cb.compare_values([], [1.0]).change is None


def test_merge_steps_keeps_pipeline_order_and_drops_negligible_steps():
    order = ["a", "b", "c", "d", "t"]
    base = {"a": 10.0, "b": 0.01, "d": 5.0, "zzz": 1.0, "t": 0.02}
    head = {"a": 5.0, "b": 0.02, "c": 2.0, "zzz": 0.0, "t": 0.08}
    rows = cb.merge_steps(base, head, order)
    assert [row.step for row in rows] == ["a", "c", "d", "t", "zzz"]
    changes = {row.step: row.change for row in rows}
    assert changes["a"] == -50.0 and changes["zzz"] == -100.0 and changes["c"] is None
    text = cb.step_rows_text(rows, (16.0, 8.0), (20.0, 10.0))
    assert text[0] == ["a", "10.0", "5.0", "-50%"]
    assert text[1][3] == "new"
    assert text[2] == ["d", "5.0", "0.0", "gone"]
    assert text[3] == ["t", "0.0", "0.1", "n/a"]  # too small for a percentage to mean anything
    assert text[-2] == ["**sum of the steps**", "16.0", "8.0", "-50%"]
    assert text[-1][0] == "**total, no timers**"


def test_diff_entities_by_span_and_type():
    base = [[0, 5, "PERSON", 0.9], [10, 20, "EMAIL_ADDRESS", 1.0], [30, 33, "IN_PAN", 0.8]]
    assert cb.diff_entities(base, [list(e) for e in base]).status == "identical"
    moved = [[0, 5, "PERSON", 0.95], [10, 20, "EMAIL_ADDRESS", 1.0], [30, 33, "IN_PAN", 0.8]]
    score_only = cb.diff_entities(base, moved)
    assert score_only.status == "scores differ" and score_only.score_changes == 1
    head = [[0, 5, "PERSON", 0.9], [10, 20, "URL", 1.0], [31, 33, "IN_PAN", 0.8]]
    diff = cb.diff_entities(base, head)
    assert diff.status == "differs"
    assert diff.only_base == [(10, 20, "EMAIL_ADDRESS"), (30, 33, "IN_PAN")]
    assert diff.only_head == [(10, 20, "URL"), (31, 33, "IN_PAN")]
    assert cb.diff_entities([], []).status == "identical"


# ---------------------------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------------------------


def _profile(total, steps, entities, second_pass, runs):
    return {
        "step_order": ["NER", "Post"], "timer_overhead_us": 0.4, "notes": [],
        "documents": {"prose": {"chars": 3000, "entities": len(entities["prose/medium/0"]), "second_pass": second_pass,
                                "ner_runs": runs, "total_ms": total, "steps": steps}},
        "entities": entities,
    }


def _data(**overrides):
    person = [0, 5, "PERSON", 0.9]
    small = {"prose/small/0": [[0, 4, "PERSON", 0.9]]}
    base_entities = {"prose/medium/0": [person, [23, 41, "EMAIL_ADDRESS", 1.0]], **small}
    head_entities = {"prose/medium/0": [person, [24, 41, "EMAIL_ADDRESS", 1.0]], **small}

    def run(role, rnd, lone, rps, p95):
        closed = {"requests": 50, "errors": 0, "req_per_s": rps, "p50": p95 / 3, "p95": p95, "p99": p95 * 1.2}
        return {"role": role, "round": rnd, "startup_s": 20.0, "lone": {"prose": lone}, "closed": {"prose": closed}}

    data = {
        "meta": {
            "refs": {"base": {"name": "origin/main", "commit": "aaaaaaaaaaaa", "dirty": False,
                              "label": "origin/main @ aaaaaaaaaaaa"},
                     "head": {"name": "WORKTREE", "commit": "bbbbbbbbbbbb", "dirty": True,
                              "label": "WORKTREE @ bbbbbbbbbbbb + uncommitted changes"}},
            "image": "proj-app", "image_id": "sha256:123456789abc...", "cpus": 2.0, "memory": "4g", "host_cpus": 8,
            "platform": "TestOS", "docker": "29 (VM 8 CPUs)",
            "api_env": {"WEB_CONCURRENCY": "2", "AUTH_ENABLED": "false"},
            "env_shown": ("WEB_CONCURRENCY", "AUTH_ENABLED"), "families": ["prose"], "mode": "both",
            "generated": "2026-01-01 00:00:00 UTC", "load_before": [0.5, 0.5, 0.5], "load_after": [0.6, 0.5, 0.5],
            "settings": "documents prose; reps 7",
            "settings_dict": {"reps": 7, "clients": 4, "seconds": 40.0, "rounds": 2},
        },
        "profile": {
            "base": _profile(1000.0, {"NER": 900.0, "Post": 90.0}, base_entities, True, 6),
            "head": _profile(500.0, {"NER": 400.0, "Post": 90.0}, head_entities, False, 3),
        },
        "load": {"rounds": 2, "order": ["base", "head", "base", "head"], "runs": [
            run("base", 1, {"small": 100.0, "medium": 1000.0, "large": 2000.0}, 2.0, 3000.0),
            run("head", 1, {"small": 50.0, "medium": 500.0, "large": 1000.0}, 4.0, 1500.0),
            run("base", 2, {"small": 110.0, "medium": 1100.0, "large": 2100.0}, 2.2, 3300.0),
            run("head", 2, {"small": 51.0, "medium": 520.0, "large": 1200.0}, 4.4, 1400.0),
        ]},
        "check_texts": {"prose/medium/0": "Rajesh Sharma wrote to rajesh@example.com today", "prose/small/0": "Rajesh"},
        "errors": [],
    }
    data.update(overrides)
    return data


def test_report_has_the_environment_the_step_table_the_api_tables_and_the_differences():
    report = cb.format_report(_data())
    assert report.startswith("# Latency: origin/main vs WORKTREE")
    for fragment in (
        "- base: `origin/main @ aaaaaaaaaaaa`", "- head: `WORKTREE @ bbbbbbbbbbbb + uncommitted changes`",
        "- image: `proj-app` (sha256:123456789abc...)", "- cpu cap: 2 CPUs per container", "- host: 8 CPUs",
        "host load average (1/5/15 min): before 0.5, 0.5, 0.5; after 0.6, 0.5, 0.5",
        "### prose (3,000 characters)", "entities 2 -> 2; second NER pass yes -> no; NER model runs 6 -> 3",
        "| step | base ms | head ms | change |", "| NER | 900.0 | 400.0 | -56% |",
        "| **sum of the steps** | 990.0 | 490.0 | -51% |", "| **total, no timers** | 1,000.0 | 500.0 | -50% |",
        "### Lone request (median of 7 requests per size", "| prose | small (~0.3 KB) | 105 | 50.5 | -52% | 10% | 2% |",
        "| prose | medium (~3 KB) | 1,050 | 510 | -51% | 10% | 4% |",
        "| prose | requests/s (higher is better) | 2.10 | 4.20 | +100% | 10% | 10% |",
        "| prose | p95 ms |", "Container start to healthy (median): base 20 s, head 20 s.",
        "2 documents compared: 1 identical, 0 with the same spans and types", "1 with different spans or types",
        "| prose/medium/0 | 2 | 2 | 1 | 1 |", "- `prose/medium/0` base only: EMAIL_ADDRESS 'rajesh@example.com' at 23",
        "- `prose/medium/0` head only: EMAIL_ADDRESS 'ajesh@example.com' at 24",
    ):
        assert fragment in report, fragment
    assert "warning" not in report and "## Errors" not in report


def test_report_marks_noise_and_warns_about_a_busy_host():
    data = _data()
    data["meta"]["load_before"] = [6.0, 5.0, 4.0]
    data["load"]["runs"][1]["lone"]["prose"]["large"] = 1990.0
    data["load"]["runs"][3]["lone"]["prose"]["large"] = 2110.0
    data["load"]["runs"][0]["lone_errors"] = 2
    data["load"]["runs"][3]["closed"]["prose"]["errors"] = 3
    report = cb.format_report(data)
    assert "warning: the host was busy (load 6.0 on 8 CPUs)" in report
    assert "| prose | large (~6 KB) | 2,050 | 2,050 | 0% (noise) | 5% | 6% |" in report
    assert "Head against base, 7 comparisons: 6 better, 0 worse, 1 within noise." in report
    assert "Failed requests (lone and closed loop): base 2, head 3." in report


def test_verdict_counts_direction_per_metric_and_ignores_missing_data():
    faster = cb.compare_values([100.0, 100.0], [50.0, 50.0])
    slower = cb.compare_values([100.0, 100.0], [150.0, 150.0])
    noisy = cb.compare_values([100.0, 120.0], [105.0, 106.0])
    assert cb.verdict_line([(faster, False), (slower, False), (noisy, False), (cb.compare_values([], []), False)]) == (
        "Head against base, 3 comparisons: 1 better, 1 worse, 1 within noise."
    )
    expected = "Head against base, 2 comparisons: 1 better, 1 worse, 0 within noise."
    assert cb.verdict_line([(faster, True), (slower, True)]) == expected
    assert cb.verdict_line([(cb.compare_values([], []), False)]) is None


def test_report_for_one_phase_and_for_failures():
    load_only = _data(profile=None)
    load_only["meta"]["mode"] = "load"
    report = cb.format_report(load_only)
    assert "Not run (--load-only)" in report and "Where the time goes" not in report
    steps_only = _data(load=None)
    assert "Latency through the API" not in cb.format_report(steps_only)
    failed = _data(errors=["load phase failed: boom"])
    failed["profile"]["head"] = None
    report = cb.format_report(failed)
    assert "Not available for both refs" in report and "## Errors\n\n- load phase failed: boom" in report


# ---------------------------------------------------------------------------------------------------------------
# The load generator, against a stub server
# ---------------------------------------------------------------------------------------------------------------


class _Stub:
    """A stand-in for the API: /health and /anonymize_unique, which can be told to fail."""

    def __init__(self, fail=False):
        self.bodies = []
        self.fail = fail
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.bodies.append(json.loads(body))
                self.send_response(500 if stub.fail else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": "x"}')

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def stub():
    server = _Stub()
    yield server
    server.close()


def test_closed_loop_measures_requests_percentiles_and_sends_the_documents(stub):
    docs = cb.DocumentSet(lambda: BANK)
    result = cb.closed_loop(stub.url, docs, "prose", clients=2, seconds=0.4)
    assert result["errors"] == 0 and result["requests"] == len(stub.bodies) >= 1
    assert result["req_per_s"] > 0
    assert 0 < result["p50"] <= result["p95"] <= result["p99"]
    assert all(set(body) == {"text"} and "@gmail.com" in body["text"] for body in stub.bodies)


def test_closed_loop_counts_failures_instead_of_raising():
    failing = _Stub(fail=True)
    try:
        result = cb.closed_loop(failing.url, cb.DocumentSet(lambda: BANK), "json", clients=1, seconds=0.3)
    finally:
        failing.close()
    assert result["errors"] >= 1 and result["requests"] == 0 and result["p50"] is None


def test_lone_latencies_and_wait_healthy_and_warm_up(stub):
    docs = cb.DocumentSet(lambda: BANK)
    latencies = cb.lone_latencies(stub.url, docs, "statement", "small", reps=3)
    assert len(latencies) == 3 and len(stub.bodies) == 3
    cb.wait_healthy(stub.url, timeout=5)
    cb.warm_up(stub.url, docs, ("prose", "json"), workers=2)
    assert len(stub.bodies) == 3 + 2 * 2 * 2


def test_wait_healthy_gives_up_and_notices_a_dead_container():
    with pytest.raises(cb.HarnessError, match="exited"):
        cb.wait_healthy("http://127.0.0.1:9", timeout=5, is_running=lambda: False, sleep=lambda s: None)
    ticks = iter(range(100))
    with pytest.raises(cb.HarnessError, match="did not become healthy"):
        cb.wait_healthy("http://127.0.0.1:9", timeout=3, sleep=lambda s: None, clock=lambda: next(ticks))


# ---------------------------------------------------------------------------------------------------------------
# Docker orchestration with a fake runner
# ---------------------------------------------------------------------------------------------------------------


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    """Real git, fake Docker: every docker command is recorded and answered like a healthy daemon would."""

    def __init__(self, stub_url=None, fail=None, raises=None, compose=None):
        self.calls: list[list[str]] = []
        self.real = cb.SubprocessRunner()
        self.port = stub_url.rsplit(":", 1)[1] if stub_url else "1"
        self.fail = fail  # the docker command is refused when its arguments start with this tuple
        self.raises = raises or cb.HarnessError
        self.compose = compose

    def docker_calls(self, *prefix):
        return [c for c in self.calls if c[0] == "docker" and c[1:1 + len(prefix)] == list(prefix)]

    def run(self, args, *, check=True, text=True, timeout=None, cwd=None, on_stderr=None):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "git":
            return self.real.run(args, check=check, text=text)
        if self.fail and args[1:1 + len(self.fail)] == list(self.fail):
            raise self.raises(f"injected failure of {' '.join(self.fail)}")
        verb = args[1]
        if args[1:3] == ["compose", "config"]:
            if self.compose is None:
                raise cb.HarnessError("no compose project")
            return _done(self.compose["--images" if "--images" in args else "json"])
        return {
            "image": lambda: _done("sha256:" + "ab" * 32 + "\n"),
            "info": lambda: _done("29.0|8|8589934592|Docker Desktop\n"),
            "port": lambda: _done(f"127.0.0.1:{self.port}\n"),
            "logs": lambda: _done("NLP model warm-up complete\n" * 2),
            "inspect": lambda: _done("true\n"),
            "exec": lambda: _done("PONG\n"),
            "ps": lambda: _done(""),
            "run": lambda: self._run(args),
        }.get(verb, lambda: _done())()

    def _run(self, args):
        if "--rm" not in args:
            return _done("container-id\n")
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        role = "base" if "/src/base/" in mounts[0] else "head"
        docs_mount = next(m for m in mounts if m.endswith(":/cmp/documents.json:ro"))
        documents = json.loads(Path(docs_mount.split(":")[0]).read_text())
        entities = {name: [[0, 5, "PERSON", 0.9]] for name in documents["check"]}
        if role == "head":
            entities["prose/medium/0"] = [[0, 6, "PERSON", 0.9]]
        profile = {
            "step_order": ["NER", "Post"], "timer_overhead_us": 0.3, "notes": [],
            "documents": {family: {"chars": len(text), "entities": 1, "second_pass": role == "base", "ner_runs": 2,
                                   "total_ms": 100.0 if role == "base" else 50.0,
                                   "steps": {"NER": 90.0 if role == "base" else 40.0, "Post": 10.0}, "timed_ms": 101.0}
                          for family, text in documents["profile"].items()},
            "entities": entities,
        }
        return _done("noise from a library\n" + json.dumps(profile) + "\n")


def _fast_args(repo, *extra):
    return ["--base", "HEAD~1", "--head", "WORKTREE", "--image", "img", "--docs", "prose", "--reps", "1",
            "--clients", "1", "--seconds", "0.2", *extra]


@pytest.fixture()
def tmp_root(tmp_path, monkeypatch):
    root = tmp_path / "tmp-root"
    root.mkdir()
    monkeypatch.setattr(cb.tempfile, "tempdir", str(root))
    return root


def test_main_end_to_end_with_a_fake_docker(repo, tmp_root, stub, capsys, tmp_path):
    runner = FakeRunner(stub.url)
    out = tmp_path / "reports" / "report.md"
    code = cb.main(_fast_args(repo, "--out", str(out)), runner=runner, repo=repo)
    assert code == 0
    report = capsys.readouterr().out
    assert report.strip() == out.read_text().strip()
    for fragment in ("# Latency: HEAD~1 vs WORKTREE", "| NER | 90.0 | 40.0 | -56% |", "### Lone request",
                     "### Closed loop", "1 with different spans or types", "| prose/medium/0 |",
                     "second NER pass yes -> no"):
        assert fragment in report, fragment
    # one API container per round and ref, interleaved base, head, base, head, never two at once
    starts = [c for c in runner.docker_calls("run", "-d") if "--network" in c]
    names = [c[c.index("--name") + 1] for c in starts]
    api_names = [n for n in names if "-api-" in n]
    assert [n.split("-api-")[1] for n in api_names] == ["base-r1", "head-r1", "base-r2", "head-r2"]
    removed_one_by_one = [c[3] for c in runner.docker_calls("rm", "-f") if len(c) == 4]
    assert removed_one_by_one == api_names
    api = next(c for c in starts if "-api-base-r1" in c[c.index("--name") + 1])
    assert api[api.index("--cpus") + 1] == "2" and "127.0.0.1::8000" in api
    assert any(a.startswith("REDIS_URL=redis://cmp-") and a.endswith("-redis:6379/0") for a in api)
    assert sum(1 for a in api if a.endswith(":ro")) == 3 and api[-1] == "img"
    assert all(n.startswith("cmp-") for n in names)
    assert list(tmp_root.iterdir()) == []  # the temporary directory is gone


@pytest.mark.parametrize("failing", [("run", "-d"), ("network", "create"), ("run", "--rm")])
def test_cleanup_runs_when_the_harness_fails(repo, tmp_root, stub, failing):
    # "run --rm" is the profile container, "network create" and "run -d" (Redis) belong to the API phase
    runner = FakeRunner(stub.url, fail=failing)
    phase = "--steps-only" if failing == ("run", "--rm") else "--load-only"
    assert cb.main(_fast_args(repo, phase), runner=runner, repo=repo) == 1
    created = {c[c.index("--name") + 1] for c in runner.docker_calls("run") if "--name" in c}
    removal = runner.docker_calls("rm", "-f", "-v")
    if created:
        assert removal, "containers must be removed"
        removed = set(removal[0][4:])
        assert created <= removed and all(name.startswith("cmp-") for name in removed)
    network_calls = runner.docker_calls("network", "rm")
    if failing == ("run", "--rm"):
        assert not network_calls  # no network was created
    else:
        assert len(network_calls) == 1
        assert network_calls[0][3].startswith("cmp-") and network_calls[0][3].endswith("-net")
    assert list(tmp_root.iterdir()) == []


def test_a_failed_phase_is_reported_and_the_other_phase_still_runs(repo, tmp_root, stub, capsys):
    runner = FakeRunner(stub.url, fail=("run", "--rm"))
    code = cb.main(_fast_args(repo), runner=runner, repo=repo)
    report = capsys.readouterr().out
    assert code == 1
    assert "## Errors" in report and "profile phase failed" in report
    assert "### Lone request" in report  # the API phase ran regardless


def test_cleanup_runs_on_ctrl_c(repo, tmp_root, stub):
    runner = FakeRunner(stub.url, fail=("network", "create"), raises=KeyboardInterrupt)
    assert cb.main(_fast_args(repo, "--load-only"), runner=runner, repo=repo) == 130
    assert runner.docker_calls("network", "rm") and list(tmp_root.iterdir()) == []


def test_sigterm_becomes_an_exit_that_runs_the_cleanup(repo, tmp_root):
    class Terminated(FakeRunner):
        def run(self, args, **kwargs):
            if list(args[:3]) == ["docker", "network", "create"]:
                os.kill(os.getpid(), signal.SIGTERM)
            return super().run(args, **kwargs)

    before = signal.getsignal(signal.SIGTERM)
    runner = Terminated()
    with pytest.raises(SystemExit) as excinfo:
        cb.main(_fast_args(repo, "--load-only"), runner=runner, repo=repo)
    assert excinfo.value.code == 128 + signal.SIGTERM
    assert runner.docker_calls("network", "rm") and list(tmp_root.iterdir()) == []
    assert signal.getsignal(signal.SIGTERM) == before


def test_keep_leaves_the_temporary_directory_with_the_exports_and_results(repo, tmp_root, stub):
    runner = FakeRunner(stub.url)
    assert cb.main(_fast_args(repo, "--steps-only", "--keep"), runner=runner, repo=repo) == 0
    (kept,) = list(tmp_root.iterdir())
    assert (kept / "src" / "base" / "app" / "main.py").read_text() == "main v1\n"
    assert (kept / "src" / "head" / "app" / "untracked.py").exists()
    assert json.loads((kept / "results.json").read_text())["profile"]["head"]["documents"]["prose"]["entities"] == 1


def test_an_unknown_ref_or_missing_image_is_a_clean_failure(repo, tmp_root, capsys):
    assert cb.main(["--base", "nope", "--head", "WORKTREE", "--image", "img"], runner=FakeRunner(), repo=repo) == 1
    assert "not a commit" in capsys.readouterr().err
    runner = FakeRunner(fail=("image", "inspect"))
    assert cb.main(_fast_args(repo), runner=runner, repo=repo) == 1
    assert "not available locally" in capsys.readouterr().err
    assert list(tmp_root.iterdir()) == []


def test_default_image_comes_from_the_compose_project(repo):
    compose = {"json": json.dumps({"name": "my-proj", "services": {"app": {"build": {}}}}),
               "--images": "x\nmy-proj-app\ny\n"}
    assert cb.default_image(FakeRunner(compose=compose), repo) == "my-proj-app"
    explicit = {"json": json.dumps({"name": "p", "services": {"app": {"image": "reg/app:1"}}}), "--images": ""}
    assert cb.default_image(FakeRunner(compose=explicit), repo) == "reg/app:1"
    assert cb.default_image(FakeRunner(compose={"json": "not json", "--images": "a\nother-app\n"}), repo) == "other-app"
    assert cb.default_image(FakeRunner(compose=None), repo) == "repo-app"


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------


def test_subprocess_runner_streams_stderr_and_reports_failures():
    runner = cb.SubprocessRunner()
    lines = []
    script = "import sys; print('result'); print('progress', file=sys.stderr)"
    done = runner.run([sys.executable, "-c", script], on_stderr=lines.append)
    assert done.stdout == "result\n" and lines == ["progress"]
    with pytest.raises(cb.HarnessError, match="exited with 3"):
        runner.run([sys.executable, "-c", "import sys; print('why', file=sys.stderr); sys.exit(3)"])
    assert runner.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=False).returncode == 3
    with pytest.raises(cb.HarnessError, match="not found"):
        runner.run(["definitely-not-a-command-xyz"])


def test_parse_profile_output_takes_the_last_json_line():
    assert cb.parse_profile_output('warning\n{"a": 1}\n') == {"a": 1}
    with pytest.raises(cb.HarnessError):
        cb.parse_profile_output("no json here\n")


# ---------------------------------------------------------------------------------------------------------------
# The in-container profiler (the parts that need no engine)
# ---------------------------------------------------------------------------------------------------------------


def test_timers_record_exclusive_time(monkeypatch):
    clock = iter([0.0, 1.0, 3.0, 6.0])  # outer starts, inner starts, inner ends, outer ends
    monkeypatch.setattr(prof, "time", types.SimpleNamespace(perf_counter=lambda: next(clock)))
    prof._EXCLUSIVE_MS.clear()
    prof._CALLS.clear()
    inner = prof.timed("inner", lambda: "result")
    outer = prof.timed("outer", lambda: inner())
    assert outer() == "result"
    assert prof._EXCLUSIVE_MS["inner"] == pytest.approx(2000.0)
    assert prof._EXCLUSIVE_MS["outer"] == pytest.approx(4000.0)  # 6 s minus the 2 s spent in the inner timer
    assert prof._CALLS["inner"] == prof._CALLS["outer"] == 1
    prof._EXCLUSIVE_MS.clear()
    prof._CALLS.clear()


def test_timers_are_named_when_entered_and_survive_exceptions():
    prof._EXCLUSIVE_MS.clear()

    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        prof.timed(lambda: "late." + "name", boom)()
    assert "late.name" in prof._EXCLUSIVE_MS and not prof._stack()
    prof._EXCLUSIVE_MS.clear()


def test_group_timers_sums_into_the_report_steps_and_keeps_the_unknown():
    grouped = prof.group_timers({
        "nlp.ner_onnx_forward.p1": 5.0, "nlp.ner_onnx_forward.p2": 4.0, "recognizer.InPanRecognizer.p1": 1.0,
        "recognizer.InPhoneRecognizer.p1": 2.0, "ner_readout.TransformersRecognizer.p1": 0.5, "dedup.final.p1": 0.25,
        "post.remove_overlapping": 0.75, "glue.detect_self": 0.5, "mystery": 3.0,
    })
    assert grouped["NER model forward (ONNX), pass 1"] == 5.0
    assert grouped["2nd NER pass, re-cased text (all of it)"] == 4.0
    assert grouped["Regex / validator recognizers"] == 3.0 and grouped["NER read-out recognizer"] == 0.5
    assert grouped["Dedup (per-recognizer + final)"] == 0.25
    assert grouped["Post-processing (merge/split/filters)"] == 0.75
    assert grouped[prof.UNASSIGNED] == 3.0
    assert list(grouped)[:2] == [label for label, _ in prof.STEPS[:2]]
    assert sum(grouped.values()) == pytest.approx(5 + 4 + 3 + 0.5 + 0.25 + 0.75 + 0.5 + 3)


def test_entity_list_is_sorted_and_rounded():
    entities = [types.SimpleNamespace(start=10, end=12, entity_type="B", score=0.123456),
                types.SimpleNamespace(start=0, end=3, entity_type="A", score=1.0)]
    assert prof.entity_list(entities) == [[0, 3, "A", 1.0], [10, 12, "B", 0.1235]]


def test_install_times_what_a_ref_has_and_names_what_it_lacks(monkeypatch):
    """An old ref has no context-scoring hook, no handling helpers and fewer pipeline functions: still profiled."""

    class FakeEntityRecognizer:
        @staticmethod
        def remove_duplicates(results):
            return results

    class FakeHFTokenPipe:
        def __call__(self, doc):
            return doc

    monkeypatch.setitem(sys.modules, "presidio_analyzer.entity_recognizer",
                        types.SimpleNamespace(EntityRecognizer=FakeEntityRecognizer))
    monkeypatch.setitem(sys.modules, "spacy_huggingface_pipelines.token_classification",
                        types.SimpleNamespace(HFTokenPipe=FakeHFTokenPipe))
    engine_module = types.SimpleNamespace(remove_overlapping=lambda results: results)

    class FakeEngine:
        def __init__(self):
            model = types.SimpleNamespace(forward=lambda: 1)
            pipe = types.SimpleNamespace(hf_pipeline=types.SimpleNamespace(model=model))
            nlp = types.SimpleNamespace(pipeline=[("onnx_ner", pipe)])
            self._analyzer = types.SimpleNamespace(
                nlp_engine=types.SimpleNamespace(process_text=lambda text, language="en": "artifacts", nlp={"en": nlp}),
                analyze=lambda text, **kwargs: [],
                registry=types.SimpleNamespace(recognizers=[]),
            )

        def _run_pipeline(self, text):
            for _ in range(2):  # the second round is the case-recovery pass
                self._analyzer.nlp_engine.process_text(text)
                self._analyzer.analyze(text)
            return engine_module.remove_overlapping([])

        def anonymize(self, text, config=None):
            return self._run_pipeline(text)

    engine, notes = FakeEngine(), []
    prof._EXCLUSIVE_MS.clear()
    prof._CALLS.clear()
    try:
        prof.install(engine, engine_module, notes)
        engine.anonymize("text")
        engine.anonymize("text")  # the phase starts over with every request
        assert prof._CALLS["glue.anonymize_self"] == prof._CALLS["glue.detect_self"] == 2
        for name in ("nlp.spacy_artifacts.p1", "nlp.spacy_artifacts.p2", "presidio.analyze_glue.p1",
                     "presidio.analyze_glue.p2", "post.remove_overlapping"):
            assert prof._CALLS[name] > 0, name
        assert prof._CALLS["nlp.spacy_artifacts.p1"] == prof._CALLS["nlp.spacy_artifacts.p2"] == 2
        grouped = prof.group_timers(prof._EXCLUSIVE_MS)
        assert grouped["2nd NER pass, re-cased text (all of it)"] > 0 and prof.UNASSIGNED not in grouped
    finally:
        prof._EXCLUSIVE_MS.clear()
        prof._CALLS.clear()
    assert sorted(note.split(" timers")[0] for note in notes) == ["context scoring", "handling"]
