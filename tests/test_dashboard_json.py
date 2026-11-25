"""Validate the structure of provisioned Grafana dashboard JSON files."""

import ast
import json
import re
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "observability" / "grafana" / "dashboards"

EXPECTED_DATASOURCE_UIDS = {"prometheus", "loki", "tempo"}

DASHBOARD_FILES = sorted(DASHBOARDS_DIR.glob("*.json"))


@pytest.fixture(params=DASHBOARD_FILES, ids=[f.name for f in DASHBOARD_FILES])
def dashboard(request) -> dict:
    """Load each dashboard JSON file as a dict."""
    path: Path = request.param
    with open(path) as f:
        return json.load(f)


class TestDashboardStructure:
    """Validate that every provisioned dashboard has the required structure."""

    def test_has_required_top_level_fields(self, dashboard):
        for field in ("uid", "title", "panels", "schemaVersion"):
            assert field in dashboard, f"Missing required field: {field}"

    def test_uid_follows_naming_convention(self, dashboard):
        assert dashboard["uid"].startswith("pii-shield-"), (
            f"UID '{dashboard['uid']}' should start with 'pii-shield-'"
        )

    def test_panels_are_nonempty(self, dashboard):
        assert len(dashboard["panels"]) > 0

    def test_panel_ids_are_unique(self, dashboard):
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids)), f"Duplicate panel IDs: {ids}"

    def test_all_panels_have_required_fields(self, dashboard):
        for panel in dashboard["panels"]:
            for field in ("id", "type", "title", "gridPos"):
                assert field in panel, (
                    f"Panel '{panel.get('title', '?')}' missing field: {field}"
                )

    def test_datasource_uids_are_valid(self, dashboard):
        """Every datasource reference should point to a known uid."""
        invalid = []
        for panel in dashboard["panels"]:
            # Check panel-level datasource
            ds = panel.get("datasource", {})
            uid = ds.get("uid") if isinstance(ds, dict) else None
            if uid and uid not in EXPECTED_DATASOURCE_UIDS:
                invalid.append((panel["title"], uid))
            # Check target-level datasources
            for target in panel.get("targets", []):
                tds = target.get("datasource", {})
                tuid = tds.get("uid") if isinstance(tds, dict) else None
                if tuid and tuid not in EXPECTED_DATASOURCE_UIDS:
                    invalid.append((panel["title"], tuid))
        assert not invalid, f"Invalid datasource UIDs: {invalid}"

    def test_gridpos_values_are_non_negative(self, dashboard):
        for panel in dashboard["panels"]:
            gp = panel["gridPos"]
            for key in ("h", "w", "x", "y"):
                assert key in gp, f"Panel '{panel['title']}' gridPos missing '{key}'"
                assert gp[key] >= 0, (
                    f"Panel '{panel['title']}' has negative gridPos.{key}: {gp[key]}"
                )

    def test_prometheus_queries_aggregate_across_workers(self, dashboard):
        """Each worker process exports its metrics under its own service.instance.id (app/telemetry.py), so a query
        that selects a metric without aggregating returns one series per worker, and a Stat panel then shows the
        same number once for each of them."""
        aggregating = re.compile(r"\s*(\d+(\.\d+)?\s*\*\s*)?(sum|max|min|avg|count|topk|bottomk|histogram_quantile)\b")

        def panels(items):
            for panel in items:
                yield panel
                yield from panels(panel.get("panels", []))

        raw = []
        for panel in panels(dashboard["panels"]):
            for target in panel.get("targets", []):
                ds = target.get("datasource", {})
                if isinstance(ds, dict) and ds.get("uid") == "prometheus" and "expr" in target:
                    if not aggregating.match(target["expr"]):
                        raw.append((panel["title"], target["expr"]))
        assert not raw, f"Prometheus queries that do not aggregate across workers: {raw}"


# ---------------------------------------------------------------------------------------------------------------
# The latency dashboard: it reads the per-step timing contract (metrics, step names, span and log names). Each
# check is a function that returns its problems, so the contract can be exercised on a broken copy as well.

REPO_ROOT = Path(__file__).resolve().parent.parent
AZURE_DASHBOARDS_DIR = REPO_ROOT / "observability" / "grafana" / "dashboards-azure"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
LATENCY_FILE = "pii-shield-latency.json"

AZURE_DATASOURCE_UID = "azure-monitor-oob"
# What the deploy script (infra/scripts/04-deploy-dashboards.sh) rewrites; no real tenant, subscription or
# resource names may be committed.
AZURE_RESOURCE_PLACEHOLDER = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/__SOURCE_RG__"
    "/providers/microsoft.insights/components/__SOURCE_APPI__"
)
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NIL_GUID = "00000000-0000-0000-0000-000000000000"

AZURE_FILES = sorted(AZURE_DASHBOARDS_DIR.glob("*.json"))


def _module_constant(path: Path, name: str):
    """The literal assigned to *name* at module level, read without importing the module (importing
    ``pii_shield`` loads the whole NLP stack)."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


# The step names are an API (dashboards, the Server-Timing header and the span names read them): the library
# owns its five, the API adds the rest.
LIBRARY_STEPS = _module_constant(REPO_ROOT / "pii_shield" / "observability.py", "LIBRARY_STEPS")
DETAIL_NAMES = _module_constant(REPO_ROOT / "pii_shield" / "observability.py", "DETAIL_NAMES")
API_STEPS = {"auth", "app_config", "queue_wait", "store_save", "store_get", "restore", "other"}
TOP_LEVEL_STEPS = set(LIBRARY_STEPS) | API_STEPS
STEP_DETAILS = {f"{step}.{detail}" for step in LIBRARY_STEPS for detail in DETAIL_NAMES}

# What Prometheus holds: the OTLP histograms (unit ms) and Tempo's span metrics.
PROMETHEUS_METRICS = {
    f"{base}_{suffix}"
    for base in (
        "pii_step_duration_milliseconds",
        "pii_detect_duration_milliseconds",
        "pii_detect_queue_wait_milliseconds",
        "http_server_duration_milliseconds",
        "traces_spanmetrics_latency",
    )
    for suffix in ("bucket", "sum", "count")
} | {"traces_spanmetrics_calls_total"}
PROMETHEUS_LABELS = {"le", "step", "size", "recovery_pass", "span_name", "service", "service_name", "http_target"}
DETAIL_MATCHER = '.*\\\\..*'  # as written in PromQL: .*\\..*  (the regex .*\..*, a name that contains a dot)

# The lines the dashboards parse, before and after the per-step timing added its two fields.
LOG_LINES = [
    "Unique anonymization complete: 4 entities, id=a5554b53-8a90-4df7-8427-5bcdff4734df, chars=109, queue_ms=0, "
    "detect_ms=17 (nlp_ms=16, recovery_pass=False, recovery_ms=0)",
    "Unique anonymization complete: 95 entities, id=1b63dd0d-8690-4c24-a71a-4c57b5c607b1, chars=2694, queue_ms=3, "
    "detect_ms=785 (nlp_ms=388, recovery_pass=True, recovery_ms=377) ner_windows=10 steps=queue_wait:3.0,"
    "nlp_pass1:388.1,analyze:15.8,recovery_pass2:377.0,post_processing:1.0,handling:0.1,store_save:2.4,other:3.2",
]


def _load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _targets(dash: dict, datasource_type: str):
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            ds = target.get("datasource") or panel.get("datasource") or {}
            if ds.get("type") == datasource_type:
                yield panel, target


def _query_texts(dash: dict) -> list[str]:
    texts = []
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            texts.append(target.get("expr") or target.get("query") or target["azureLogAnalytics"]["query"])
    return texts


def _promql(dash: dict):
    for panel, target in _targets(dash, "prometheus"):
        yield panel, target["expr"]


def _stacked(panel: dict) -> bool:
    stacking = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {}).get("stacking", {})
    return stacking.get("mode", "none") != "none"


def description_problems(dash: dict) -> list[str]:
    problems = []
    for panel in dash["panels"]:
        if not panel.get("title", "").strip():
            problems.append(f"panel {panel['id']} has no title")
        if panel["type"] != "row":
            description = panel.get("description", "").strip()
            if not 20 <= len(description) <= 450:
                problems.append(f"'{panel['title']}': the description must be a short sentence or two")
    return problems


def overlap_problems(dash: dict) -> list[str]:
    boxes = [(p["title"], p["gridPos"]) for p in dash["panels"]]
    problems = [f"'{t}' leaves the 24-column grid" for t, g in boxes if g["x"] + g["w"] > 24]
    for i, (title_a, a) in enumerate(boxes):
        for title_b, b in boxes[i + 1:]:
            across = a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            down = a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
            if across and down:
                problems.append(f"'{title_a}' overlaps '{title_b}'")
    return problems


def variable_problems(dash: dict) -> list[str]:
    """Every $variable a query uses is defined, every defined variable is used, and the threshold is a number."""
    defined = {v["name"] for v in dash["templating"]["list"]}
    used = set()
    for text in _query_texts(dash):
        used |= {name for name in re.findall(r"\$\{?([A-Za-z_]\w*)", text) if not name.startswith("__")}
    problems = [f"${name} is used but not defined" for name in sorted(used - defined)]
    problems += [f"${name} is defined but never used" for name in sorted(defined - used)]
    for variable in dash["templating"]["list"]:
        if variable["name"] == "slow_ms":
            # It is pasted into TraceQL, LogQL and KQL as a bare number.
            options = variable["query"].split(",")
            problems += [f"slow_ms option {o!r} is not a number" for o in options if not o.isdigit()]
    return problems


def metric_problems(dash: dict) -> list[str]:
    """Prometheus queries read only metrics, labels and step names of the contract. A step histogram always says
    whether it wants the top-level steps or the details: details are inclusive and must never be summed with them."""
    problems = []
    for panel, expr in _promql(dash):
        where = f"'{panel['title']}'"
        for metric in re.findall(r"([A-Za-z_:][A-Za-z0-9_:]*)\{", expr):
            if metric not in PROMETHEUS_METRICS:
                problems.append(f"{where}: {metric} is not a metric of the contract")
        labels = set(re.findall(r"([a-z_]+)\s*(?:=~|!~|!=|=)\s*\"", expr))
        for group in re.findall(r"\bby \(([^)]*)\)", expr):
            labels |= {label.strip() for label in group.split(",")}
        if labels - PROMETHEUS_LABELS:
            problems.append(f"{where}: unknown labels {sorted(labels - PROMETHEUS_LABELS)}")
        uses_spans = "traces_spanmetrics_" in expr
        if uses_spans and 'service="pii-shield"' not in expr:
            problems.append(f"{where}: span metrics are labelled service, not service_name")
        if not uses_spans and 'service_name="pii-shield"' not in expr:
            problems.append(f"{where}: no service_name=\"pii-shield\" filter")
        for span_filter in re.findall(r'span_name=~"([^"]*)"', expr):
            unknown = set(span_filter.split("|")) - TOP_LEVEL_STEPS
            if unknown:
                problems.append(f"{where}: span_name {sorted(unknown)} is not a step")
        if "pii_step_duration_milliseconds" in expr:
            matchers = re.findall(r'\bstep(!~|=~|=)"([^"]*)"', expr)
            if not matchers:
                problems.append(f"{where}: step histogram without a step matcher (top-level or detail)")
            for operator, value in matchers:
                if operator == "=" and value not in TOP_LEVEL_STEPS | STEP_DETAILS:
                    problems.append(f"{where}: {value} is not a step")
                if operator != "=" and value != DETAIL_MATCHER:
                    problems.append(f"{where}: step matcher {value!r} is not the 'contains a dot' regex")
            if _stacked(panel) and not re.search(r'\bstep!~"' + re.escape(DETAIL_MATCHER) + '"', expr):
                problems.append(f"{where}: a stacked panel must select the top-level steps only")
    return problems


def pattern_regex(pattern: str) -> re.Pattern:
    """A LogQL ``| pattern`` as a regular expression: <name> captures, <_> skips."""
    out = ""
    for part in re.split(r"(<[A-Za-z_]+>)", pattern):
        if re.fullmatch(r"<[A-Za-z_]+>", part):
            out += "(?:.*?)" if part == "<_>" else f"(?P<{part[1:-1]}>.*?)"
        else:
            out += re.escape(part)
    return re.compile(out + "$")


@pytest.fixture(scope="module")
def latency() -> dict:
    return _load(DASHBOARDS_DIR / LATENCY_FILE)


@pytest.fixture(scope="module")
def azure_latency() -> dict:
    return _load(AZURE_DASHBOARDS_DIR / LATENCY_FILE)


class TestLatencyDashboard:
    """observability/grafana/dashboards/pii-shield-latency.json"""

    def test_every_panel_has_a_title_and_a_short_description(self, latency):
        assert not description_problems(latency)

    def test_panels_do_not_overlap(self, latency):
        assert not overlap_problems(latency)

    def test_variables_are_defined_and_used(self, latency):
        assert not variable_problems(latency)

    def test_prometheus_queries_follow_the_contract(self, latency):
        assert not metric_problems(latency)
        assert len(list(_promql(latency))) >= 15

    def test_slow_requests_come_from_tempo_and_loki(self, latency):
        (_, tempo), = _targets(latency, "tempo")
        assert tempo["queryType"] == "traceql"
        assert 'resource.service.name="pii-shield"' in tempo["query"]
        assert 'name="pii_shield.anonymize"' in tempo["query"]
        assert re.search(r"duration > \$\{slow_ms\}ms", tempo["query"])
        (_, loki), = _targets(latency, "loki")
        assert loki["expr"].startswith('{service_name="pii-shield"} |= "Unique anonymization complete"')

    def test_the_loki_pattern_reads_the_request_log_line_before_and_after_the_step_fields(self, latency):
        (_, loki), = _targets(latency, "loki")
        pattern = re.search(r'\| pattern "([^"]*)"', loki["expr"]).group(1)
        compared = set(re.findall(r"(\w+) > \$\{slow_ms\}", loki["expr"]))
        regex = pattern_regex(pattern)
        assert compared == {"detect_ms", "queue_ms"}
        for line, want in zip(LOG_LINES, [("109", "0", "17"), ("2694", "3", "785")]):
            match = regex.search(line)
            assert match, f"the pattern does not match: {line}"
            assert (match["chars"], match["queue_ms"], match["detect_ms"]) == want

    def test_the_log_line_still_has_the_fields_the_dashboard_reads(self):
        tree = ast.parse((REPO_ROOT / "app" / "main.py").read_text())
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        messages = [s for s in strings if "Unique anonymization complete" in s]
        assert messages, "app/main.py no longer logs 'Unique anonymization complete'"
        assert any("chars=%d, queue_ms=%.0f, detect_ms=%.0f (" in m for m in messages)

    def test_the_contract_check_catches_a_broken_copy(self, latency):
        """The checks above are only worth something if they fail when the dashboard drifts from the contract."""
        broken = json.loads(json.dumps(latency))
        for panel in broken["panels"]:
            for target in panel.get("targets", []):
                if "expr" in target:
                    target["expr"] = (
                        target["expr"]
                        .replace("pii_step_duration_milliseconds_sum", "pii_step_duration_ms_sum")
                        .replace('step!~".*\\\\..*"', 'step=~".+"')
                    )
        assert metric_problems(broken)
        assert overlap_problems({"panels": [{"title": "a", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4}},
                                            {"title": "b", "gridPos": {"x": 12, "y": 2, "w": 12, "h": 4}}]})


class TestAzureDashboards:
    """The Azure twins are deployed by infra/scripts/04-deploy-dashboards.sh after it swaps its placeholders."""

    @pytest.fixture(params=AZURE_FILES, ids=[f.name for f in AZURE_FILES])
    def azure_file(self, request) -> Path:
        return request.param

    def test_parses_and_has_the_required_fields(self, azure_file):
        dash = _load(azure_file)
        for field in ("uid", "title", "panels", "schemaVersion"):
            assert field in dash, f"{azure_file.name} misses {field}"
        ids = [p["id"] for p in dash["panels"]]
        assert len(ids) == len(set(ids))

    def test_queries_target_the_azure_monitor_datasource_only(self, azure_file):
        dash = _load(azure_file)
        for panel in dash["panels"]:
            datasources = [panel.get("datasource")] + [t.get("datasource") for t in panel.get("targets", [])]
            for ds in filter(None, datasources):
                assert ds["uid"] == AZURE_DATASOURCE_UID, f"{panel['title']}: {ds}"

    def test_resources_are_the_deploy_script_placeholder(self, azure_file):
        dash = _load(azure_file)
        resources = [r for _, t in _targets(dash, "grafana-azure-monitor-datasource")
                     for r in t["azureLogAnalytics"]["resources"]]
        assert resources and set(resources) == {AZURE_RESOURCE_PLACEHOLDER}

    def test_no_real_identifiers_or_connection_strings(self, azure_file):
        text = azure_file.read_text()
        assert set(GUID.findall(text)) <= {NIL_GUID}, "a real GUID (tenant, subscription, workspace?)"
        leftover = text.replace(AZURE_RESOURCE_PLACEHOLDER, "")
        assert "__SOURCE_" not in leftover, "a placeholder outside the resource id"
        for secret in ("InstrumentationKey", "IngestionEndpoint", "applicationinsights.azure.com", "/workspaces/"):
            assert secret not in text


class TestAzureLatencyDashboard:
    """observability/grafana/dashboards-azure/pii-shield-latency.json (KQL written from the exporter schema, not run)"""

    def test_identity_and_rows_match_the_local_dashboard(self, azure_latency, latency):
        assert azure_latency["uid"] == latency["uid"]
        assert azure_latency["title"] == latency["title"] + " (Azure)"
        rows = lambda d: [p["title"] for p in d["panels"] if p["type"] == "row"]  # noqa: E731
        assert rows(azure_latency) == rows(latency)

    def test_every_panel_has_a_title_and_a_short_description(self, azure_latency):
        assert not description_problems(azure_latency)

    def test_panels_do_not_overlap(self, azure_latency):
        assert not overlap_problems(azure_latency)

    def test_variables_are_defined_and_used(self, azure_latency):
        assert not variable_problems(azure_latency)

    def test_queries_read_the_documented_tables_and_attributes(self, azure_latency):
        queries = _query_texts(azure_latency)
        assert len(queries) >= 15
        for query in queries:
            assert query.split()[0] in {"requests", "dependencies", "customMetrics", "traces", "union"}, query
            assert "$__timeFilter(timestamp)" in query, "an unbounded scan of the whole retention"
            if query.startswith("dependencies"):
                assert 'type == "InProc"' in query, "step spans are InProc dependencies"
            if query.startswith("customMetrics"):
                assert 'name == "pii.step.duration"' in query
                assert "percentile" not in query, "Azure keeps only sum/count of a histogram: averages only"
            for name in re.findall(r'\bname == "([^"]+)"', query):
                assert name in TOP_LEVEL_STEPS | {"pii_shield.anonymize", "pii.step.duration"}, name
            for step_list in re.findall(r"\bstep in \(([^)]*)\)", query):
                assert set(re.findall(r'"([^"]+)"', step_list)) <= TOP_LEVEL_STEPS
            for detail in re.findall(r"pii\.detail\.(\w+?)_ms", query):
                assert detail in DETAIL_NAMES, detail
        text = " ".join(queries)
        assert 'customDimensions["pii.step"]' in text
        assert 'customDimensions["pii.detail.ner_model_ms"]' in text


class TestComposeMounts:
    """docker-compose.yml mounts every dashboard file by itself: a new file that is not listed never appears."""

    @pytest.mark.parametrize("path", DASHBOARD_FILES, ids=[f.name for f in DASHBOARD_FILES])
    def test_every_dashboard_is_mounted_into_grafana(self, path):
        mount = (f"./observability/grafana/dashboards/{path.name}:"
                 f"/otel-lgtm/grafana/conf/provisioning/dashboards/custom/{path.name}:ro")
        assert mount in COMPOSE_FILE.read_text()
