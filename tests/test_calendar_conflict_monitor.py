import zira_dashboard.calendar_conflict_monitor as mon


def test_decide_unchanged_set_is_not_changed():
    d = mon.decide({1, 2}, {1, 2})
    assert d == {"changed": False, "added": [], "removed": [], "now_empty": False}


def test_decide_new_employee_is_added():
    d = mon.decide({1, 2, 3}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == [3]
    assert d["removed"] == []
    assert d["now_empty"] is False


def test_decide_resolved_employee_is_removed():
    d = mon.decide({1}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == []
    assert d["removed"] == [2]
    assert d["now_empty"] is False


def test_decide_all_resolved_is_now_empty():
    d = mon.decide(set(), {1, 2})
    assert d["changed"] is True
    assert d["removed"] == [1, 2]
    assert d["now_empty"] is True


def test_decide_empty_to_empty_is_not_changed():
    d = mon.decide(set(), set())
    assert d["changed"] is False
    assert d["now_empty"] is False
