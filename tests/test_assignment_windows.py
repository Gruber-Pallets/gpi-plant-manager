from datetime import datetime, timezone
from zira_dashboard import assignment_windows as aw

UTC = timezone.utc
def t(h, m=0): return datetime(2026, 6, 2, h, m, tzinfo=UTC)

SHIFT_START = t(12)   # 07:00 America/Chicago == 12:00 UTC (CDT)
CAP = t(18)           # "now" = 13:00 CDT == 18:00 UTC


def _resolve(**kw):
    base = dict(assignments={}, attributions=[], punch_windows={},
                shift_start_utc=SHIFT_START, cap_utc=CAP, time_off_key="__time_off")
    base.update(kw)
    return aw.resolve_segments(**base)


def test_scheduled_person_spans_full_shift():
    segs = _resolve(assignments={"Dismantler 1": ["Jose Cabezas"]})
    assert len(segs) == 1
    s = segs[0]
    assert (s.wc_name, s.person_name, s.source) == ("Dismantler 1", "Jose Cabezas", "schedule")
    assert s.start_utc == SHIFT_START and s.end_utc == CAP


def test_time_off_key_is_skipped():
    assert _resolve(assignments={"__time_off": ["Whoever"]}) == []


def test_open_attribution_starts_midday_ends_at_cap():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(15), "end_utc": None}]
    segs = _resolve(attributions=attrs)
    assert len(segs) == 1
    assert segs[0].start_utc == t(15) and segs[0].end_utc == CAP
    assert segs[0].source == "attribution"


def test_closed_attribution_keeps_its_end():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(15), "end_utc": t(16, 30)}]
    segs = _resolve(attributions=attrs)
    assert segs[0].end_utc == t(16, 30)


def test_reassignment_closes_prior_open_segment_at_next_start():
    attrs = [
        {"wc_name": "Dismantler 4", "person_name": "Ana", "start_utc": t(13), "end_utc": None},
        {"wc_name": "Dismantler 3", "person_name": "Ana", "start_utc": t(15), "end_utc": None},
    ]
    segs = sorted(_resolve(attributions=attrs), key=lambda s: s.start_utc)
    assert (segs[0].wc_name, segs[0].start_utc, segs[0].end_utc) == ("Dismantler 4", t(13), t(15))
    assert (segs[1].wc_name, segs[1].start_utc, segs[1].end_utc) == ("Dismantler 3", t(15), CAP)


def test_punches_win_over_attribution_for_same_person():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(13), "end_utc": None}]
    punches = {"Eulogio Mendez": [("Dismantler 2", t(14), None)]}
    segs = _resolve(attributions=attrs, punch_windows=punches)
    assert len(segs) == 1
    assert (segs[0].wc_name, segs[0].source) == ("Dismantler 2", "punch")
    assert segs[0].start_utc == t(14) and segs[0].end_utc == CAP


def test_punches_win_over_schedule_for_same_person():
    segs = _resolve(assignments={"Dismantler 1": ["Bob"]},
                    punch_windows={"Bob": [("Repair 1", t(13), t(16))]})
    assert len(segs) == 1
    assert (segs[0].wc_name, segs[0].source) == ("Repair 1", "punch")


def test_start_floored_and_end_capped_to_shift():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Ana",
              "start_utc": t(10), "end_utc": t(20)}]
    segs = _resolve(attributions=attrs)
    assert segs[0].start_utc == SHIFT_START and segs[0].end_utc == CAP


def test_zero_length_segment_dropped():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Ana",
              "start_utc": t(18), "end_utc": None}]
    assert _resolve(attributions=attrs) == []


def test_expected_by_wc_prorates_per_segment():
    segs = [
        aw.WorkSegment("Dismantler 1", "Jose", SHIFT_START, CAP, "schedule"),
        aw.WorkSegment("Dismantler 4", "Eulogio", t(15), CAP, "attribution"),
    ]
    def prod(name, s, e): return (e - s).total_seconds() / 60.0
    out = aw.expected_by_wc(segs, {"Dismantler 1": 60.0, "Dismantler 4": 60.0}, prod)
    assert out["Dismantler 1"] == 360.0
    assert out["Dismantler 4"] == 180.0


def test_who_by_wc_dedupes_and_orders():
    segs = [
        aw.WorkSegment("Dismantler 4", "Eulogio", t(13), t(15), "attribution"),
        aw.WorkSegment("Dismantler 4", "Ana", t(15), CAP, "attribution"),
    ]
    assert aw.who_by_wc(segs) == {"Dismantler 4": "Eulogio + Ana"}
