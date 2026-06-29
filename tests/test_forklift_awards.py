import datetime as dt

import pytest

from zira_dashboard import forklift_awards as fa
from zira_dashboard import forklift_score as fs


def _row(day, did, name, calls, on_time, late, avg_ms, util):
    return {"day": day, "driver_id": did, "name": name, "calls": calls,
            "on_time": on_time, "late": late, "avg_ms": avg_ms,
            "max_ms": avg_ms, "utilization_pct": util}


@pytest.fixture
def rows(monkeypatch):
    data = [
        _row(dt.date(2026, 4, 14), "d1", "Trent", 31, 31, 0, 40000, 22),  # big day, high score
        _row(dt.date(2026, 4, 15), "d1", "Trent", 10, 9, 1, 90000, 15),
        _row(dt.date(2026, 4, 14), "d2", "Isidro", 29, 29, 0, 50000, 20),
        _row(dt.date(2026, 4, 16), "d3", "Juan", 5, 5, 0, 30000, 99),     # below gate (5<8)
    ]
    monkeypatch.setattr(fa, "driver_days", lambda start, end: [
        r for r in data if start <= r["day"] <= end])
    fa.invalidate()
    return data


def test_goat_is_highest_single_day_score(rows):
    g = fa.goat(fs.DEFAULT_SCORE_CONFIG)
    assert g["name"] == "Trent" and g["day"] == dt.date(2026, 4, 14)
    assert g["score"] > 0


def test_below_gate_day_never_wins(rows):
    g = fa.goat(fs.DEFAULT_SCORE_CONFIG)
    assert g["name"] != "Juan"  # Juan's only day is below the 8-call gate


def test_annual_top_days_sorted_by_score(rows):
    top = fa.annual_top_days(2026, fs.DEFAULT_SCORE_CONFIG)
    assert [t["name"] for t in top][:1] == ["Trent"]
    assert all(top[i]["score"] >= top[i+1]["score"] for i in range(len(top)-1))


def test_annual_fastest_respects_min_calls(rows):
    # Juan has the fastest avg (30s) but only 5 calls < min_calls -> excluded
    f = fa.annual_fastest(2026, min_calls=8)
    assert f["name"] != "Juan"


def test_leaderboard_four_lists_with_gated_overall(rows):
    lb = fa.leaderboard(dt.date(2026, 4, 1), dt.date(2026, 4, 30),
                        fs.DEFAULT_SCORE_CONFIG, min_calls=8)
    assert set(lb) == {"most_calls", "on_time", "fastest", "overall"}
    # most_calls is volume-ranked, Trent's two days sum 41 -> top
    assert lb["most_calls"][0]["name"] == "Trent"
    # overall = avg of eligible daily scores; Juan (only sub-gate day) absent
    assert all(r["name"] != "Juan" for r in lb["overall"])
    # overall rows expose an average score and a day count
    assert "score" in lb["overall"][0] and "days" in lb["overall"][0]
