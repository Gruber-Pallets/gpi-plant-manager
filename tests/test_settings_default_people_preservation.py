"""Default people must survive Work Centers saves that did not touch them.

The Settings → Work Centers form autosaves the whole ``#wc-form`` on any edit.
Only default-person pickers changed since the last successful save may replace
their live values; an older open tab must not make its stale checkbox snapshot
authoritative. When a picker really does change, every selected person must
still have a rendered checkbox, however ineligible they have since become.
"""

from __future__ import annotations

import asyncio
import os
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from zira_dashboard import settings_context

# ---------------------------------------------------------------------------
# Work-center (exact) defaults — pool comes from the roster
# ---------------------------------------------------------------------------


def _person(name, *, active=True, reserve=False, level=3):
    return SimpleNamespace(
        name=name, active=active, reserve=reserve, level=lambda _skill: level
    )


def _effective(default_people, *, required_skills=("Repair",)):
    def effective_for(_location):
        return {
            "max_ops": 2,
            "required_skills": list(required_skills),
            "min_ops": 1,
            "goal_per_day": 100,
            "note": "",
            "groups": [],
            "department": "Recycled",
            "default_people": list(default_people),
        }

    return effective_for


_LOCATION = SimpleNamespace(meter_id=None, name="Work Orders", bay="Bay 1")


def test_work_center_pool_keeps_selected_person_who_went_inactive():
    people = [_person("Ann A."), _person("Bob B.", active=False)]

    rows = settings_context.work_center_rows(
        [_LOCATION], people, _effective(["Bob B."])
    )

    names = [entry["name"] for entry in rows[0]["default_pool"]]
    assert "Bob B." in names, "an inactive default must still render a checkbox"


def test_work_center_pool_keeps_selected_person_missing_from_the_roster():
    """Excluded (roster-filter) people never reach the route's roster at all."""
    rows = settings_context.work_center_rows(
        [_LOCATION], [_person("Ann A.")], _effective(["Ghost G."])
    )

    pool = {entry["name"]: entry for entry in rows[0]["default_pool"]}
    assert "Ghost G." in pool
    assert pool["Ghost G."]["preserved"] is True


def test_pools_survive_a_roster_relabel_that_matches_nothing():
    """The mass-wipe case: every rendered name disagrees with every saved one.

    ``load_roster`` caches for an hour and ``effective`` for a minute, so right
    after Odoo sync relabels people (e.g. "Juan Delgado" -> "Juan D.") the
    picker can be built from one vocabulary and the saved defaults from the
    other. Nothing matches, so without preservation every checkbox renders
    unchecked and the next save empties both tables at once.
    """
    stale_roster = [_person("Juan Delgado"), _person("Francisco Ramirez")]
    saved = ["Juan D.", "Francisco R."]

    rows = settings_context.work_center_rows(
        [_LOCATION], stale_roster, _effective(saved)
    )
    pool = [entry["name"] for entry in rows[0]["default_pool"]]
    assert set(saved) <= set(pool)

    assert set(saved) <= set(_group_pool_names(stale_roster, saved))


def test_work_center_pool_still_omits_inactive_people_who_are_not_defaults():
    rows = settings_context.work_center_rows(
        [_LOCATION],
        [_person("Ann A."), _person("Bob B.", active=False)],
        _effective([]),
    )

    assert [entry["name"] for entry in rows[0]["default_pool"]] == ["Ann A."]


# ---------------------------------------------------------------------------
# Group defaults — pool is filtered to qualified, non-reserve, active people
# ---------------------------------------------------------------------------


def _group_pool_names(people, selected, *, member_required=("Repair",)):
    member = SimpleNamespace(name="Repair 1")
    rows = settings_context.with_group_default_context(
        [{"name": "Repairs", "count": 1, "auto": 0, "override": "", "effective": 0}],
        people,
        members_for=lambda _kind, _name: [member],
        required_skills_for=lambda _loc: tuple(member_required),
        defaults_for=lambda _name: list(selected),
        conflicts={},
    )
    return [entry["name"] for entry in rows[0]["default_pool"]]


def test_group_pool_keeps_selected_reserve_person():
    people = [_person("Ann A."), _person("Cal C.", reserve=True)]

    assert "Cal C." in _group_pool_names(people, ["Cal C."])


def test_group_pool_keeps_selected_unqualified_person():
    people = [_person("Ann A."), _person("Dee D.", level=0)]

    assert "Dee D." in _group_pool_names(people, ["Dee D."])


def test_group_pool_keeps_selected_person_missing_from_the_roster():
    assert "Ghost G." in _group_pool_names([_person("Ann A.")], ["Ghost G."])


def test_group_pool_still_omits_ineligible_people_who_are_not_defaults():
    people = [
        _person("Ann A."),
        _person("Cal C.", reserve=True),
        _person("Dee D.", level=0),
    ]

    assert _group_pool_names(people, []) == ["Ann A."]


# ---------------------------------------------------------------------------
# End-to-end: render the real page, post it back untouched, lose nothing
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


class _FormSerializer(HTMLParser):
    """Serialize one form the way the browser's ``FormData(form)`` does."""

    def __init__(self, form_id: str):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.inside = False
        self.pairs: list[tuple[str, str]] = []
        self._select: str | None = None
        self._selected: list[tuple[str, str]] = []
        self._textarea: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("id") == self.form_id
            return
        if not self.inside:
            return
        if tag == "input":
            name = a.get("name")
            if not name or "disabled" in a:
                return
            kind = (a.get("type") or "text").lower()
            if kind in {"checkbox", "radio"}:
                if "checked" in a:
                    self.pairs.append((name, a.get("value", "on")))
            elif kind not in {"submit", "button", "reset", "file", "image"}:
                self.pairs.append((name, a.get("value", "")))
        elif tag == "select":
            self._select, self._selected = a.get("name"), []
        elif tag == "option" and self._select and "selected" in a:
            self._selected.append((self._select, a.get("value", "")))
        elif tag == "textarea":
            self._textarea = a.get("name")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select" and self._select:
            self.pairs.extend(self._selected)
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self.inside and self._textarea:
            self.pairs.append((self._textarea, data))
            self._textarea = None


def test_pickers_render_preserved_defaults_so_the_form_reposts_them():
    from pathlib import Path

    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    css = Path("src/zira_dashboard/static/settings.css").read_text()

    # Three pickers post default people: work-center, its reserves sub-list,
    # and group. Each must render preserved entries or the save drops them.
    assert html.count("preserved-default") == 3
    # Hidden until checked — visible only while they'd otherwise be lost.
    assert ".default-people-picker .dd-item.preserved-default { display: none; }" in css
    assert (
        ".default-people-picker .dd-item.preserved-default:has(input:checked)" in css
    )


class _RouteForm(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _RouteRequest:
    headers = {"accept": "application/json"}

    def __init__(self, form):
        self._form = _RouteForm(form)

    async def form(self):
        return self._form


async def _run_inline(work):
    return work()


def _stub_default_save_route(monkeypatch):
    from zira_dashboard.routes import settings

    replacements = []
    monkeypatch.setattr(settings.asyncio, "to_thread", _run_inline)
    monkeypatch.setattr(settings.staffing, "LOCATIONS", [_LOCATION])
    monkeypatch.setattr(settings.work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(settings.work_centers_store, "all_group_names", lambda _kind: [])
    monkeypatch.setattr(
        settings.work_centers_store,
        "_exact_defaults_map",
        lambda: {_LOCATION.name: ["Ana"]},
    )
    monkeypatch.setattr(settings.work_centers_store, "group_defaults_map", lambda: {})
    monkeypatch.setattr(
        settings.work_centers_store, "_normalize_default_targets", lambda **_kwargs: None
    )
    monkeypatch.setattr(settings.work_centers_store, "save_one", lambda *_args: None)
    monkeypatch.setattr(
        settings.work_centers_store,
        "replace_default_targets",
        lambda **kwargs: replacements.append(kwargs),
    )
    return settings, replacements


def test_stale_untouched_picker_cannot_erase_a_newer_saved_default(monkeypatch):
    settings, replacements = _stub_default_save_route(monkeypatch)
    prefix = "wc__name:Work Orders__"

    response = asyncio.run(
        settings.settings_save_work_centers(
            _RouteRequest(
                {
                    prefix + "goal_per_day": "101",
                    prefix + "default_people_present": "1",
                }
            )
        )
    )

    assert response.status_code == 200
    assert replacements == []


def test_dirty_picker_can_intentionally_clear_a_saved_default(monkeypatch):
    settings, replacements = _stub_default_save_route(monkeypatch)
    prefix = "wc__name:Work Orders__"
    field = prefix + "default_people"

    response = asyncio.run(
        settings.settings_save_work_centers(
            _RouteRequest(
                {
                    prefix + "default_people_present": "1",
                    "default_people_dirty": field,
                }
            )
        )
    )

    assert response.status_code == 200
    assert replacements == [
        {"exact_by_center": {_LOCATION.name: []}, "group_by_name": {}}
    ]


def test_autosave_marks_only_changed_default_picker_fields_dirty():
    from pathlib import Path

    javascript = Path("src/zira_dashboard/static/settings.js").read_text()

    assert "function _formDataForSave(form, before, after)" in javascript
    assert "body.append('default_people_dirty', field)" in javascript
    assert "_formDataForSave(form, before, after)" in javascript


@pytestmark_db
def test_replace_default_targets_logs_names_it_could_not_resolve(caplog):
    import logging

    from zira_dashboard import work_centers_store

    before_exact = work_centers_store._exact_defaults_map()
    before_groups = work_centers_store.group_defaults_map()
    try:
        with caplog.at_level(logging.WARNING, logger="zira_dashboard.work_centers_store"):
            work_centers_store.replace_default_targets(
                exact_by_center={"Work Orders": ["__no_such_person__"]},
                group_by_name={},
            )
        assert "__no_such_person__" in caplog.text
    finally:
        work_centers_store.replace_default_targets(
            exact_by_center=before_exact, group_by_name=before_groups
        )


@pytestmark_db
def test_work_centers_autosave_roundtrip_keeps_ineligible_defaults(monkeypatch):
    from fastapi.testclient import TestClient

    from zira_dashboard import db, odoo_sync, staffing, work_centers_store
    from zira_dashboard.app import app

    center = "Work Orders"
    group = "__default_preservation_group__"
    inactive = "__default_preservation_inactive__"
    reserve = "__default_preservation_reserve__"
    monkeypatch.setattr(odoo_sync, "sync", lambda *a, **k: None)

    before_exact = work_centers_store._exact_defaults_map()
    before_groups = work_centers_store.group_defaults_map()
    try:
        for name, is_reserve, active in ((inactive, False, False), (reserve, True, True)):
            db.execute(
                "INSERT INTO people (name, active, reserve, excluded) "
                "VALUES (%s, %s, %s, FALSE) ON CONFLICT (name) DO UPDATE "
                "SET active = EXCLUDED.active, reserve = EXCLUDED.reserve, "
                "excluded = FALSE",
                (name, active, is_reserve),
            )
        db.execute(
            "INSERT INTO work_centers (name, category) VALUES (%s, 'Other') "
            "ON CONFLICT (name) DO NOTHING",
            (center,),
        )
        work_centers_store.add_group(group)
        staffing._invalidate_roster_cache()
        work_centers_store._invalidate_caches()

        work_centers_store.replace_default_targets(
            exact_by_center={center: [inactive]},
            group_by_name={group: [reserve]},
        )
        assert work_centers_store.default_people(
            next(loc for loc in staffing.LOCATIONS if loc.name == center)
        ) == [inactive]
        assert work_centers_store.group_default_people(group) == [reserve]

        client = TestClient(app)
        page = client.get("/settings")
        assert page.status_code == 200
        form = _FormSerializer("wc-form")
        form.feed(page.text)
        saved = client.post(
            "/settings/work_centers",
            content=urlencode(form.pairs),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert saved.status_code == 200, saved.text[:400]

        work_centers_store._invalidate_caches()
        assert work_centers_store._exact_defaults_map().get(center) == [inactive]
        assert work_centers_store.group_default_people(group) == [reserve]
    finally:
        work_centers_store.replace_default_targets(
            exact_by_center=before_exact, group_by_name=before_groups
        )
        db.execute("DELETE FROM groups WHERE name = %s", (group,))
        db.execute("DELETE FROM people WHERE name IN (%s, %s)", (inactive, reserve))
        staffing._invalidate_roster_cache()
        work_centers_store._invalidate_caches()
