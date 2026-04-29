import os
import pytest

from zira_dashboard import db, skill_matrix_views_store as views


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM skill_matrix_views WHERE name LIKE 'TestView%'")
    yield
    db.execute("DELETE FROM skill_matrix_views WHERE name LIKE 'TestView%'")


def test_create_and_get_view():
    v = views.create_view("TestViewA", {
        "hidden_skills": ["Loading"],
        "visible_people": ["Alice", "Bob"],
        "active_filter": "active",
        "reserve_filter": "exclude",
    })
    assert v["name"] == "TestViewA"
    assert v["hidden_skills"] == ["Loading"]
    assert v["visible_people"] == ["Alice", "Bob"]
    assert v["active_filter"] == "active"
    assert v["reserve_filter"] == "exclude"
    got = views.get_view("TestViewA")
    assert got == v


def test_update_view_overwrites_fields():
    views.create_view("TestViewB", {"hidden_skills": ["Loading"]})
    views.update_view("TestViewB", {
        "hidden_skills": ["Heat Treat"],
        "visible_people": None,
        "active_filter": "all",
        "reserve_filter": "only",
    })
    v = views.get_view("TestViewB")
    assert v["hidden_skills"] == ["Heat Treat"]
    assert v["visible_people"] is None
    assert v["active_filter"] == "all"
    assert v["reserve_filter"] == "only"


def test_set_default_clears_other_defaults():
    views.create_view("TestViewC", {})
    views.create_view("TestViewD", {})
    views.set_default("TestViewC")
    views.set_default("TestViewD")
    c = views.get_view("TestViewC")
    d = views.get_view("TestViewD")
    assert c["is_default"] is False
    assert d["is_default"] is True


def test_set_default_none_clears_default():
    views.create_view("TestViewE", {})
    views.set_default("TestViewE")
    views.set_default(None)
    assert views.get_default_view() is None


def test_delete_view():
    views.create_view("TestViewF", {})
    views.delete_view("TestViewF")
    assert views.get_view("TestViewF") is None


def test_create_drops_invalid_active_filter():
    v = views.create_view("TestViewG", {"active_filter": "garbage"})
    assert v["active_filter"] == "active"  # default coerced


def test_visible_people_empty_list_normalizes_to_none():
    v = views.create_view("TestViewH", {"visible_people": []})
    assert v["visible_people"] is None
