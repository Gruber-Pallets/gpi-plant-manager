import asyncio
from zira_dashboard.routes import staffing as staffing_routes


class _FakeReq:
    def __init__(self, body): self._body = body
    async def json(self): return self._body


def _call(body, monkeypatch):
    captured = {}
    def fake_add(day, wc, person, start_utc, end_utc=None, source="manual"):
        captured["end_utc"] = end_utc
        captured["start_utc"] = start_utc
        return 7
    monkeypatch.setattr("zira_dashboard.wc_attributions.add", fake_add)
    monkeypatch.setattr("zira_dashboard._http_cache.invalidate_today_cache", lambda: None)
    resp = asyncio.run(staffing_routes.staffing_attribute(_FakeReq(body)))
    return captured, resp


def test_attribute_open_ended_when_end_omitted(monkeypatch):
    body = {"day": "2026-06-02", "wc_name": "Dismantler 4",
            "person_name": "Eulogio Mendez", "start_utc": "2026-06-02T15:00:00+00:00"}
    captured, resp = _call(body, monkeypatch)
    assert resp.status_code == 200
    assert captured["end_utc"] is None  # open


def test_attribute_closed_when_end_provided(monkeypatch):
    body = {"day": "2026-06-02", "wc_name": "Dismantler 4",
            "person_name": "Eulogio Mendez",
            "start_utc": "2026-06-02T15:00:00+00:00",
            "end_utc": "2026-06-02T18:00:00+00:00"}
    captured, resp = _call(body, monkeypatch)
    assert resp.status_code == 200
    assert captured["end_utc"] is not None
