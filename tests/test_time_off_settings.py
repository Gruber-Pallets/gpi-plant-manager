import pytest
from unittest.mock import patch

from zira_dashboard import settings_store


def test_hidden_leave_type_ids_default_empty(monkeypatch):
    """No row in app_settings → returns empty list."""
    with patch.object(settings_store, "_read_raw", return_value=None):
        assert settings_store.get_hidden_leave_type_ids() == []


def test_hidden_leave_type_ids_round_trip(monkeypatch):
    storage = {}
    monkeypatch.setattr(settings_store, "_read_raw",
                        lambda k: storage.get(k))
    monkeypatch.setattr(settings_store, "_write_raw",
                        lambda k, v: storage.__setitem__(k, v))
    settings_store.set_hidden_leave_type_ids([3, 7, 11])
    assert settings_store.get_hidden_leave_type_ids() == [3, 7, 11]


def test_show_stratustime_overlay_defaults_true(monkeypatch):
    monkeypatch.setattr(settings_store, "_read_raw", lambda k: None)
    assert settings_store.get_show_stratustime_overlay() is True


def test_default_shift_hours_default(monkeypatch):
    monkeypatch.setattr(settings_store, "_read_raw", lambda k: None)
    assert settings_store.get_default_shift_hours() == (6.0, 14.5)


def test_default_shift_hours_round_trip(monkeypatch):
    storage = {}
    monkeypatch.setattr(settings_store, "_read_raw",
                        lambda k: storage.get(k))
    monkeypatch.setattr(settings_store, "_write_raw",
                        lambda k, v: storage.__setitem__(k, v))
    settings_store.set_default_shift_hours(7.0, 15.5)
    assert settings_store.get_default_shift_hours() == (7.0, 15.5)
