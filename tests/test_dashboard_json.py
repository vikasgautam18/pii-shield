"""Validate the structure of provisioned Grafana dashboard JSON files."""

import json
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
