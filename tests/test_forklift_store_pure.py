"""DB-free unit tests for forklift_store's pure parsing helpers. Unlike
tests/test_forklift_store.py (whole-module, DATABASE_URL-gated), these exercise
the JSONB-parsing logic directly so they run everywhere."""
from zira_dashboard import forklift_store


def test_operating_hours_counts_hours_with_calls():
    by_hour = {"8": {"calls": 18}, "9": {"calls": 22}, "10": {"calls": 0}}
    assert forklift_store._operating_hours(by_hour) == 2


def test_operating_hours_parses_json_string():
    assert forklift_store._operating_hours('{"8": {"calls": 5}, "9": {"calls": 0}}') == 1


def test_operating_hours_skips_unparseable_by_hour():
    # A non-JSON string can't be _coerce_json'd (json.loads -> JSONDecodeError,
    # a ValueError subclass) -> the day yields 0 operating hours, not a raise.
    assert forklift_store._operating_hours("not json at all") == 0


def test_operating_hours_skips_non_dict_by_hour():
    # A non-dict by_hour (e.g. a JSON list) has no .values() -> AttributeError
    # in the old guard; now it's caught and the day yields 0 operating hours.
    assert forklift_store._operating_hours([1, 2, 3]) == 0
    assert forklift_store._operating_hours("[1, 2, 3]") == 0


def test_operating_hours_skips_non_dict_hour_payload():
    # A non-dict hour payload (e.g. a bare int) raises AttributeError on .get();
    # that hour is skipped, the well-formed hours still count.
    by_hour = {"8": 5, "9": {"calls": 22}, "10": "weird"}
    assert forklift_store._operating_hours(by_hour) == 1


def test_operating_hours_empty_or_none():
    assert forklift_store._operating_hours({}) == 0
    assert forklift_store._operating_hours(None) == 0
