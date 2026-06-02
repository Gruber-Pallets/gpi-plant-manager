from zira_dashboard.time_format import fmt_time_short, fmt_time_range


def test_fmt_time_short():
    assert fmt_time_short("2026-04-29T09:00:00") == "9a"
    assert fmt_time_short("2026-04-29T09:30:00") == "9:30a"
    assert fmt_time_short("2026-04-29T12:00:00") == "12p"
    assert fmt_time_short("2026-04-29T13:15:00") == "1:15p"
    assert fmt_time_short("garbage") == ""


def test_fmt_time_range():
    assert fmt_time_range("2026-04-29T09:00:00", "2026-04-29T10:00:00") == "9-10a"
    assert fmt_time_range("2026-04-29T11:00:00", "2026-04-29T13:00:00") == "11a-1p"
    assert fmt_time_range("2026-04-29T12:00:00", "2026-04-29T13:00:00") == "12-1p"
    assert fmt_time_range("bad", "2026-04-29T13:00:00") == ""
