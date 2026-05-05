from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _attr(units: float, downtime: float = 0.0):
    return {"units": units, "downtime": downtime, "hours": 8.0, "days_worked": 1}


def test_person_days_400_when_neither_wc_nor_group():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_400_when_both_wc_and_group():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&group=Repair&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_400_on_unparseable_dates():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=garbage&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_filters_to_single_wc():
    """?wc=Repair-1 keeps only that WC; multi-WC days drop the others."""
    fake = [
        (date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95), "Repair-2": _attr(88)}}),
        (date(2026, 4, 28), {"Carlos": {"Repair-2": _attr(90)}}),
        (date(2026, 4, 29), {"Carlos": {"Repair-1": _attr(100)}}),
    ]
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 200
    rows = r.json()["rows"]
    # Newest first; only Repair-1 days for Carlos remain.
    assert [x["date"] for x in rows] == ["2026-04-29", "2026-04-27"]
    assert all(x["wcs"] == ["Repair-1"] for x in rows)
    assert rows[0]["units"] == 100
    assert rows[1]["units"] == 95


def test_person_days_aggregates_group_scope():
    """?group=Repair keeps any WC in the Repair category and aggregates per day."""
    from zira_dashboard import staffing
    fake = [
        (date(2026, 4, 27), {
            "Carlos": {"Repair-1": _attr(95, 5), "Repair-2": _attr(88, 7), "Dismantler-1": _attr(50)},
            "Other": {"Repair-1": _attr(10)},
        }),
        (date(2026, 4, 28), {
            "Carlos": {"Dismantler-1": _attr(60)},  # no Repair WC; should drop the day
        }),
    ]
    # Leaderboards groups are user-defined memberships, not loc.skill
    # categories. Mock work_centers_store.members so the endpoint sees a
    # "Repair" group whose members are Repair-1 + Repair-2.
    fake_repair_members = [
        staffing.Location("Repair-1", "Repair", "Bay 1", "Recycled", None),
        staffing.Location("Repair-2", "Repair", "Bay 1", "Recycled", None),
    ]
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake), \
         patch("zira_dashboard.work_centers_store.members",
               return_value=fake_repair_members):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&group=Repair&start=2026-04-27&end=2026-04-28")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-04-27"
    assert row["wcs"] == ["Repair-1", "Repair-2"]  # alphabetical
    assert row["units"] == 95 + 88
    assert row["downtime"] == 5 + 7


def test_person_days_returns_empty_rows_when_no_match():
    """Person who has no production in the scope/range returns 200 with []."""
    fake = [
        (date(2026, 4, 27), {"Other": {"Repair-1": _attr(50)}}),
    ]
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-27")
    assert r.status_code == 200
    assert r.json() == {"rows": []}


def test_person_days_caches_response(monkeypatch):
    """Repeated calls for the same (name, scope, range) hit the cache and
    don't re-call attribution_per_day."""
    from datetime import date
    from zira_dashboard.routes import leaderboards as lb_mod

    # Clear caches so the test starts clean.
    if hasattr(lb_mod._PERSON_DAYS_CACHE_TODAY, "invalidate"):
        lb_mod._PERSON_DAYS_CACHE_TODAY.invalidate()
    if hasattr(lb_mod._PERSON_DAYS_CACHE_PAST, "invalidate"):
        lb_mod._PERSON_DAYS_CACHE_PAST.invalidate()

    fake = [(date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95)}})]
    call_count = {"n": 0}
    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return fake

    monkeypatch.setattr(lb_mod, "attribution_per_day", _spy)
    client = TestClient(app)

    url = "/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-27"
    client.get(url)
    client.get(url)
    client.get(url)

    assert call_count["n"] == 1, "expected only the first call to hit attribution_per_day"
