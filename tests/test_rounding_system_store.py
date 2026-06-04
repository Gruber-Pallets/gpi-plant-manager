"""Tests for rounding_system_store: CRUD, department map, cache. Postgres-backed."""

import os

import pytest

from zira_dashboard import db, rounding_system_store
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

SYS_NAME = "ZZ Test System"
DEPT = "ZZ Test Dept"


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Test%'")
    rounding_system_store.reload()
    yield
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Test%'")
    rounding_system_store.reload()


def _sid(name=SYS_NAME):
    return next(s.id for s in rounding_system_store.all_systems() if s.name == name)


def test_add_then_save_windows():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 5))
    sysrec = next(s for s in rounding_system_store.all_systems() if s.id == sid)
    assert sysrec.rounding == RoundingSettings(20, 0, 0, 5)


def test_map_department_resolves_windows():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 0))
    rounding_system_store.set_department_system(DEPT, sid)
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(20, 0, 0, 0)


def test_unmapped_or_blank_department_returns_none():
    assert rounding_system_store.windows_for_department(DEPT) is None
    assert rounding_system_store.windows_for_department("") is None


def test_delete_system_unsets_mapping():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.set_department_system(DEPT, sid)
    rounding_system_store.delete_system(sid)  # ON DELETE SET NULL -> no system
    assert rounding_system_store.windows_for_department(DEPT) is None


def test_rename_system():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.rename_system(sid, "ZZ Test System Renamed")
    names = {s.name for s in rounding_system_store.all_systems()}
    assert "ZZ Test System Renamed" in names


def test_department_map_includes_unset_as_none():
    rounding_system_store.set_department_system(DEPT, None)
    assert rounding_system_store.department_map().get(DEPT) is None


def test_cache_invalidated_on_reload():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(10, 0, 0, 0))
    rounding_system_store.set_department_system(DEPT, sid)
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(10, 0, 0, 0)
    db.execute("UPDATE rounding_systems SET in_before_min = 30 WHERE id = %s", (sid,))
    assert rounding_system_store.windows_for_department(DEPT).in_before_min == 10  # stale cache
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT).in_before_min == 30
