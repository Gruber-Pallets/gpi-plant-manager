"""Quick-punch fixer Off / Preview / Live setting.

The store, schema, warmer, context, template, and route contracts run
everywhere against fakes. The Postgres-backed round trips skip without
DATABASE_URL (run them against a local pgserver, never production).
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import re
from unittest.mock import Mock

import pytest

from zira_dashboard import db, quick_punch_fix_settings as qpf
from zira_dashboard._schema import SCHEMA_DDL

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src" / "zira_dashboard" / "templates" / "settings.html"
)
SETTINGS_JS = (
    Path(__file__).resolve().parents[1]
    / "src" / "zira_dashboard" / "static" / "settings.js"
)

OFF_TEXT = "Don't touch Odoo."
PREVIEW_TEXT = (
    "List what would be fixed in the Exception Inbox archive, "
    "but don't change Odoo."
)
LIVE_TEXT = "Fix quick sign-in mistakes in Odoo automatically."
HELP_TEXT = (
    "Merges sign-outs and wrong-station taps fixed within 5 minutes into one "
    "continuous Odoo record. Never touches days payroll has processed."
)


# ---- fakes ----------------------------------------------------------------

class RecordingCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FailingAuditCursor(RecordingCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "INSERT INTO quick_punch_fix_setting_events" in sql:
            raise RuntimeError("audit unavailable")


def cursor_context(cursor, *, fail_after_yield=False):
    @contextmanager
    def opened():
        yield cursor
        if fail_after_yield:
            raise RuntimeError("commit failed")
    return opened


def _event_inserts(cursor):
    return [
        call for call in cursor.executed
        if "INSERT INTO quick_punch_fix_setting_events" in call[0]
    ]


# ---- settings shape -------------------------------------------------------

def test_default_mode_is_preview():
    assert qpf.MODES == ("off", "preview", "live")
    assert qpf.Settings().mode == "preview"
    assert qpf.DEFAULT == qpf.Settings(mode="preview")


def test_settings_are_frozen():
    with pytest.raises(Exception):
        qpf.Settings().mode = "live"  # type: ignore[misc]


def test_advisory_lock_key_is_ascii_qpfixset_and_distinct_from_auto_lunch():
    from zira_dashboard import auto_lunch_settings

    assert qpf._TRANSACTION_LOCK_KEY == int.from_bytes(b"QPFIXSET", "big")
    assert qpf._TRANSACTION_LOCK_KEY < 2 ** 63  # fits a signed bigint
    assert qpf._TRANSACTION_LOCK_KEY != auto_lunch_settings._TRANSACTION_LOCK_KEY


def test_load_falls_back_to_default_when_the_row_is_missing(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    assert qpf._load_from_db() == qpf.DEFAULT


def test_load_reads_the_persisted_mode(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [{"mode": "live"}])
    assert qpf._load_from_db() == qpf.Settings("live")


# ---- schema ---------------------------------------------------------------

def test_schema_declares_singleton_and_append_only_audit_tables():
    assert "CREATE TABLE IF NOT EXISTS quick_punch_fix_settings" in SCHEMA_DDL
    assert (
        "mode  TEXT NOT NULL DEFAULT 'preview' "
        "CHECK (mode IN ('off','preview','live'))"
    ) in SCHEMA_DDL
    assert (
        "INSERT INTO quick_punch_fix_settings (id) VALUES (1) "
        "ON CONFLICT (id) DO NOTHING;"
    ) in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS quick_punch_fix_setting_events" in SCHEMA_DDL
    assert (
        "before_mode TEXT CHECK (before_mode IN ('off','preview','live'))"
    ) in SCHEMA_DDL
    assert (
        "after_mode  TEXT NOT NULL CHECK (after_mode IN ('off','preview','live'))"
    ) in SCHEMA_DDL
    assert (
        "source      TEXT NOT NULL "
        "CHECK (source IN ('settings','external','baseline'))"
    ) in SCHEMA_DDL
    assert "quick_punch_fix_setting_events_changed_at_idx" in SCHEMA_DDL
    # Sits next to the Auto-Lunch setting tables.
    assert (
        SCHEMA_DDL.index("auto_lunch_setting_events_changed_at_idx")
        < SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS quick_punch_fix_settings")
        < SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS forklift_settings")
    )


# ---- save -----------------------------------------------------------------

def test_changed_save_writes_setting_and_one_attributed_audit_event(monkeypatch):
    cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    assert qpf.save(
        qpf.Settings("live"),
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    ) is True

    assert len(cursor.executed) == 4
    assert "pg_advisory_xact_lock" in cursor.executed[0][0]
    assert cursor.executed[0][1] == (qpf._TRANSACTION_LOCK_KEY,)
    assert "FROM quick_punch_fix_settings" in cursor.executed[1][0]
    assert "FOR UPDATE" in cursor.executed[1][0]
    assert "INSERT INTO quick_punch_fix_settings" in cursor.executed[2][0]
    assert cursor.executed[2][1] == ("live",)
    assert "INSERT INTO quick_punch_fix_setting_events" in cursor.executed[3][0]
    assert cursor.executed[3][1] == (
        "preview", "live", "dale@gruberpallets.com", "Dale", "settings",
    )
    cache_set.assert_called_once_with(qpf.Settings("live"))


def test_first_save_without_a_row_audits_a_null_before(monkeypatch):
    cursor = RecordingCursor([None])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())

    assert qpf.save(qpf.Settings("off")) is True

    assert _event_inserts(cursor)[0][1] == (None, "off", None, None, "settings")


def test_save_of_the_same_mode_writes_nothing(monkeypatch):
    cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    assert qpf.save(qpf.Settings("preview")) is False

    assert len(cursor.executed) == 2
    assert _event_inserts(cursor) == []
    cache_set.assert_called_once_with(qpf.Settings("preview"))


@pytest.mark.parametrize("mode", ["", "observe", "LIVE", None, "on"])
def test_save_rejects_an_unknown_mode_before_touching_the_db(monkeypatch, mode):
    cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    with pytest.raises(ValueError, match="invalid quick-punch fixer mode"):
        qpf.save(qpf.Settings(mode))

    assert cursor.executed == []


def test_save_rejects_an_unknown_audit_source(monkeypatch):
    cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    with pytest.raises(
        ValueError, match="invalid quick-punch fixer audit source: manual",
    ):
        qpf.save(qpf.Settings("live"), source="manual")

    assert cursor.executed == []


def test_cache_is_not_changed_when_the_save_commit_fails(monkeypatch):
    cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(
        db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        qpf.save(qpf.Settings("live"))

    cache_set.assert_not_called()


def test_recent_events_reads_newest_first_with_a_bounded_limit(monkeypatch):
    events = [{"id": 7, "source": "settings"}]
    query = Mock(return_value=events)
    monkeypatch.setattr(db, "query", query)

    assert qpf.recent_events(999) == events

    sql, params = query.call_args.args
    assert "FROM quick_punch_fix_setting_events" in sql
    assert "ORDER BY changed_at DESC, id DESC LIMIT %s" in sql
    assert params == (100,)
    qpf.recent_events(0)
    assert query.call_args.args[1] == (1,)


# ---- reconcile ------------------------------------------------------------

def test_save_and_reconcile_take_the_same_lock_before_the_row(monkeypatch):
    save_cursor = RecordingCursor([{"mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(save_cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())
    qpf.save(qpf.Settings())

    reconcile_cursor = RecordingCursor([{"mode": "preview"}, {"after_mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(reconcile_cursor))
    qpf.reconcile_external_change()

    assert "pg_advisory_xact_lock" in save_cursor.executed[0][0]
    assert "pg_advisory_xact_lock" in reconcile_cursor.executed[0][0]
    assert save_cursor.executed[0][1] == reconcile_cursor.executed[0][1]
    assert "FOR UPDATE" in reconcile_cursor.executed[1][0]


def test_reconcile_seeds_one_baseline_when_history_is_empty(monkeypatch):
    cursor = RecordingCursor([{"mode": "preview"}, None])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())

    assert qpf.reconcile_external_change() == qpf.Settings("preview")

    inserts = _event_inserts(cursor)
    assert len(inserts) == 1
    assert inserts[0][1] == (None, "preview", None, None, "baseline")


def test_reconcile_records_an_external_change(monkeypatch):
    cursor = RecordingCursor([{"mode": "live"}, {"after_mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())

    assert qpf.reconcile_external_change() == qpf.Settings("live")

    inserts = _event_inserts(cursor)
    assert len(inserts) == 1
    assert inserts[0][1] == ("preview", "live", None, None, "external")


def test_reconcile_does_not_duplicate_a_matching_event(monkeypatch):
    cursor = RecordingCursor([{"mode": "off"}, {"after_mode": "off"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())

    qpf.reconcile_external_change()

    assert _event_inserts(cursor) == []


def test_reconcile_without_a_row_observes_the_default(monkeypatch):
    cursor = RecordingCursor([None, None])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(qpf._store, "set", Mock())

    assert qpf.reconcile_external_change() == qpf.DEFAULT
    assert _event_inserts(cursor)[0][1][-1] == "baseline"


def test_reconcile_refreshes_the_shared_cache(monkeypatch):
    cursor = RecordingCursor([{"mode": "off"}, {"after_mode": "preview"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    qpf.reconcile_external_change()

    cache_set.assert_called_once_with(qpf.Settings("off"))


def test_reconcile_does_not_publish_cache_when_commit_fails(monkeypatch):
    cursor = RecordingCursor([{"mode": "off"}, {"after_mode": "off"}])
    monkeypatch.setattr(
        db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        qpf.reconcile_external_change()

    cache_set.assert_not_called()


def test_reconcile_returns_persisted_state_when_the_audit_append_fails(
    monkeypatch, caplog,
):
    cursor = FailingAuditCursor([{"mode": "off"}, {"after_mode": "live"}])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(qpf._store, "set", cache_set)

    assert qpf.reconcile_external_change() == qpf.Settings("off")
    assert "external change audit failed" in caplog.text
    assert any("ROLLBACK TO SAVEPOINT" in sql for sql, _ in cursor.executed)
    cache_set.assert_called_once_with(qpf.Settings("off"))


# ---- 20 s inbox warmer ----------------------------------------------------

def _quiet_inbox_sources(monkeypatch, calls):
    from zira_dashboard import auto_lunch_guard

    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.assignments_todo_payload",
        lambda force=False: calls.append("assign"),
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.late_report_payload",
        lambda force=False: calls.append("late"),
    )
    monkeypatch.setattr(
        auto_lunch_guard, "refresh", lambda: calls.append("auto_lunch"))


def test_warm_inbox_once_reconciles_the_quick_punch_fix_setting(monkeypatch):
    from zira_dashboard import page_warmer

    calls = []
    _quiet_inbox_sources(monkeypatch, calls)
    monkeypatch.setattr(
        qpf, "reconcile_external_change",
        lambda: calls.append("quick_punch_fix") or qpf.DEFAULT,
    )
    monkeypatch.setattr(qpf, "reload", lambda: calls.append("reload"))

    page_warmer.warm_inbox_once()

    assert calls.count("quick_punch_fix") == 1
    assert calls.index("auto_lunch") < calls.index("quick_punch_fix")
    assert "reload" not in calls  # reconcile already refreshed the cache


def _failing_reconcile():
    raise RuntimeError("advisory lock timed out")


def test_warm_inbox_once_reloads_the_mode_when_reconcile_fails(monkeypatch):
    from zira_dashboard._singleton import CachedSingleton
    from zira_dashboard import page_warmer

    calls = []
    _quiet_inbox_sources(monkeypatch, calls)
    monkeypatch.setattr(qpf, "reconcile_external_change", _failing_reconcile)
    # An isolated cache holding the stale mode; the DB now says Off.
    monkeypatch.setattr(qpf, "_store", CachedSingleton(qpf._load_from_db))
    qpf._store.set(qpf.Settings("preview"))
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [{"mode": "off"}])

    page_warmer.warm_inbox_once()  # must not raise

    assert qpf.current() == qpf.Settings("off")
    assert calls == ["auto_lunch", "assign", "late"]


def test_warm_inbox_once_survives_reconcile_and_reload_both_failing(monkeypatch):
    from zira_dashboard import page_warmer

    calls = []
    _quiet_inbox_sources(monkeypatch, calls)
    monkeypatch.setattr(qpf, "reconcile_external_change", _failing_reconcile)

    def reload_boom():
        calls.append("reload")
        raise RuntimeError("db down")

    monkeypatch.setattr(qpf, "reload", reload_boom)

    page_warmer.warm_inbox_once()  # must not raise

    assert calls == ["auto_lunch", "reload", "assign", "late"]


# ---- settings context -----------------------------------------------------

@pytest.mark.parametrize("mode", ["off", "preview", "live"])
def test_context_exposes_the_mode(mode):
    from zira_dashboard import settings_context

    assert settings_context.quick_punch_fix_context(qpf.Settings(mode)) == {
        "mode": mode,
    }


def test_history_context_uses_plain_labels_and_site_time():
    from zira_dashboard import settings_context

    at = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
    rows = [
        {"source": "settings", "actor_name": "Dale",
         "actor_upn": "dale@gruberpallets.com", "changed_at": at,
         "before_mode": "preview", "after_mode": "live"},
        {"source": "external", "actor_name": None, "actor_upn": None,
         "changed_at": at, "before_mode": "live", "after_mode": "off"},
        {"source": "baseline", "actor_name": None, "actor_upn": None,
         "changed_at": at, "before_mode": None, "after_mode": "preview"},
        {"source": "settings", "actor_name": None,
         "actor_upn": "manager@gruberpallets.com", "changed_at": at,
         "before_mode": None, "after_mode": "off"},
    ]

    result = settings_context.quick_punch_fix_history_context(rows)

    assert result[0] == {
        "time_label": "9/18/2026 4:30 PM",
        "before_label": "Preview",
        "after_label": "Live",
        "actor_label": "Dale",
        "has_before": True,
        "is_baseline": False,
    }
    assert result[1]["actor_label"] == "Outside app / detected automatically"
    assert (result[1]["before_label"], result[1]["after_label"]) == ("Live", "Off")
    assert result[2]["actor_label"] == "Monitoring started"
    assert result[2]["has_before"] is False
    assert result[2]["is_baseline"] is True
    assert result[2]["before_label"] is None
    assert result[3]["actor_label"] == "manager@gruberpallets.com"


# ---- template + autosave --------------------------------------------------

_RADIO = re.compile(
    r'<input type="radio" name="mode" value="(off|preview|live)"\s*(checked)?\s*>'
)


def _quick_punch_form(html: str) -> str:
    match = re.search(
        r'<form method="post" action="/settings/quick_punch_fix"'
        r' id="quick-punch-fix-form">.*?</form>',
        html,
        flags=re.DOTALL,
    )
    assert match, "Quick-punch fixer form missing"
    return match.group(0)


def _checked_modes(form_html: str) -> dict[str, bool]:
    return {value: bool(checked) for value, checked in _RADIO.findall(form_html)}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_template_places_the_fixer_section_right_after_auto_lunch():
    html = TEMPLATE.read_text()

    auto_lunch_history = html.index('id="auto-lunch-history"')
    fixer_form = html.index('<form method="post" action="/settings/quick_punch_fix"')
    rules_end = html.index("<!-- /tc-tab-rules -->")
    assert auto_lunch_history < fixer_form < rules_end
    between = html[html.index("</form>", html.index('id="auto-lunch-form"')):fixer_form]
    assert "<form" not in between, "no other form between Auto-Lunch and the fixer"


@pytest.mark.parametrize("mode", ["off", "preview", "live"])
def test_template_renders_three_options_with_the_current_one_checked(mode):
    from zira_dashboard.routes import settings as settings_routes

    source = _quick_punch_form(TEMPLATE.read_text())
    rendered = settings_routes.templates.env.from_string(source).render(
        quick_punch_fix={"mode": mode})

    assert _checked_modes(rendered) == {
        "off": mode == "off",
        "preview": mode == "preview",
        "live": mode == "live",
    }
    text = _normalized(rendered)
    assert "Quick-punch fixer" in text
    assert OFF_TEXT in text
    assert PREVIEW_TEXT in text
    assert LIVE_TEXT in text
    assert HELP_TEXT in text


def test_template_history_block_lists_changes():
    from zira_dashboard.routes import settings as settings_routes

    html = TEMPLATE.read_text()
    match = re.search(
        r'(<div id="quick-punch-fix-history".*?</div>)', html, flags=re.DOTALL)
    assert match, "Quick-punch fixer history block missing"
    block = settings_routes.templates.env.from_string(match.group(1))

    rendered = block.render(quick_punch_fix_history=[
        {"time_label": "9/18/2026 4:30 PM", "before_label": "Preview",
         "after_label": "Live", "actor_label": "Dale",
         "has_before": True, "is_baseline": False},
        {"time_label": "9/18/2026 4:00 PM", "before_label": None,
         "after_label": "Preview", "actor_label": "Monitoring started",
         "has_before": False, "is_baseline": True},
    ])
    text = _normalized(rendered)
    assert "Recent quick-punch fixer changes" in text
    assert "Preview → Live" in text
    assert "Monitoring started" in text
    assert "None" not in text

    empty = _normalized(block.render(quick_punch_fix_history=[]))
    assert "No changes recorded yet." in empty


def test_settings_js_autosaves_the_fixer_form_like_auto_lunch():
    js = SETTINGS_JS.read_text()

    auto_lunch = (
        "attachAutosaver(document.getElementById('auto-lunch-form'), "
        "'/settings/auto_lunch');"
    )
    fixer = (
        "attachAutosaver(document.getElementById('quick-punch-fix-form'), "
        "'/settings/quick_punch_fix');"
    )
    assert auto_lunch in js
    assert fixer in js
    assert js.index(auto_lunch) < js.index(fixer)


# ---- POST route (fake request) --------------------------------------------

class _FormRequest:
    def __init__(self, values, *, accept="application/json"):
        self._values = dict(values)
        self.headers = {"accept": accept} if accept else {}

    async def form(self):
        return self._values


def _route_with_recorded_save(monkeypatch):
    from zira_dashboard import inbox_log

    saves = []
    monkeypatch.setattr(
        inbox_log, "actor_from",
        lambda _request: ("manager@gruberpallets.com", "Plant Manager"),
    )
    monkeypatch.setattr(
        qpf, "save",
        lambda settings, **kwargs: saves.append((settings, kwargs)) or True,
    )
    return saves


def test_post_saves_the_mode_with_the_request_actor(monkeypatch):
    from zira_dashboard.routes import settings as settings_routes

    saves = _route_with_recorded_save(monkeypatch)

    response = asyncio.run(
        settings_routes.settings_save_quick_punch_fix(
            _FormRequest({"mode": "live"})))

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'
    assert saves == [(
        qpf.Settings("live"),
        {"actor_upn": "manager@gruberpallets.com",
         "actor_name": "Plant Manager"},
    )]


def test_post_without_json_accept_redirects_to_timeclock(monkeypatch):
    from zira_dashboard.routes import settings as settings_routes

    saves = _route_with_recorded_save(monkeypatch)

    response = asyncio.run(
        settings_routes.settings_save_quick_punch_fix(
            _FormRequest({"mode": " Off "}, accept=None)))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&section=timeclock"
    assert saves[0][0] == qpf.Settings("off")


@pytest.mark.parametrize("mode", ["", "observe", "bogus"])
def test_post_rejects_an_unknown_mode_without_saving(monkeypatch, mode):
    from zira_dashboard.routes import settings as settings_routes

    saves = _route_with_recorded_save(monkeypatch)
    values = {"mode": mode} if mode else {}

    response = asyncio.run(
        settings_routes.settings_save_quick_punch_fix(_FormRequest(values)))

    assert response.status_code == 400
    assert response.body == b'{"ok":false,"error":"invalid mode"}'
    assert saves == []


def test_post_route_is_registered():
    from zira_dashboard.app import app
    from zira_dashboard.page_views import _leaf_routes

    assert any(
        getattr(route, "path", None) == "/settings/quick_punch_fix"
        and "POST" in (getattr(route, "methods", None) or set())
        for route in _leaf_routes(app.routes)
    )


# ---- Postgres-backed round trips ------------------------------------------

def _reset_rows():
    db.execute("DELETE FROM quick_punch_fix_setting_events")
    db.execute("UPDATE quick_punch_fix_settings SET mode = 'preview' WHERE id = 1")
    qpf.reload()


@pytest.fixture
def fresh_setting():
    db.bootstrap_schema()
    _reset_rows()
    yield
    db.execute(
        "INSERT INTO quick_punch_fix_settings (id) VALUES (1) "
        "ON CONFLICT (id) DO NOTHING")
    _reset_rows()


@db_required
def test_db_bootstrap_seeds_preview_and_missing_row_reads_default(fresh_setting):
    db.execute("DELETE FROM quick_punch_fix_settings")
    assert qpf.reload() == qpf.DEFAULT

    db.bootstrap_schema()

    rows = db.query("SELECT id, mode FROM quick_punch_fix_settings")
    assert rows == [{"id": 1, "mode": "preview"}]
    assert qpf.reload() == qpf.Settings("preview")
    assert qpf.current().mode == "preview"


@db_required
def test_db_change_writes_one_event_and_same_value_writes_none(fresh_setting):
    assert qpf.save(
        qpf.Settings("live"),
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    ) is True
    assert qpf.save(
        qpf.Settings("live"),
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    ) is False

    events = qpf.recent_events()
    assert len(events) == 1
    assert events[0]["before_mode"] == "preview"
    assert events[0]["after_mode"] == "live"
    assert events[0]["actor_upn"] == "dale@gruberpallets.com"
    assert events[0]["actor_name"] == "Dale"
    assert events[0]["source"] == "settings"
    assert events[0]["changed_at"] is not None
    assert qpf.current() == qpf.Settings("live")
    assert qpf.reload() == qpf.Settings("live")


@db_required
def test_db_invalid_mode_or_source_raises_and_changes_nothing(fresh_setting):
    with pytest.raises(ValueError):
        qpf.save(qpf.Settings("observe"))
    with pytest.raises(ValueError):
        qpf.save(qpf.Settings("live"), source="manual")
    with pytest.raises(Exception):
        db.execute("UPDATE quick_punch_fix_settings SET mode = 'bogus' WHERE id = 1")

    assert qpf.recent_events() == []
    assert qpf.reload() == qpf.Settings("preview")


@db_required
def test_db_reconcile_logs_baseline_then_external_after_a_direct_change(
    fresh_setting,
):
    assert qpf.reconcile_external_change() == qpf.Settings("preview")
    assert [
        (e["source"], e["before_mode"], e["after_mode"])
        for e in qpf.recent_events()
    ] == [("baseline", None, "preview")]

    # Nothing changed: no duplicate.
    qpf.reconcile_external_change()
    assert len(qpf.recent_events()) == 1

    db.execute("UPDATE quick_punch_fix_settings SET mode = 'off' WHERE id = 1")
    assert qpf.current() == qpf.Settings("preview")  # cache still stale

    assert qpf.reconcile_external_change() == qpf.Settings("off")

    events = qpf.recent_events()
    assert [
        (e["source"], e["before_mode"], e["after_mode"], e["actor_upn"])
        for e in events
    ] == [
        ("external", "preview", "off", None),
        ("baseline", None, "preview", None),
    ]
    assert qpf.current() == qpf.Settings("off")  # cache refreshed


@db_required
def test_db_warmer_reload_publishes_a_direct_switch_when_reconcile_fails(
    fresh_setting, monkeypatch,
):
    from zira_dashboard import page_warmer

    _quiet_inbox_sources(monkeypatch, [])
    monkeypatch.setattr(qpf, "reconcile_external_change", _failing_reconcile)
    assert qpf.current() == qpf.Settings("preview")
    db.execute("UPDATE quick_punch_fix_settings SET mode = 'off' WHERE id = 1")

    page_warmer.warm_inbox_once()

    assert qpf.current() == qpf.Settings("off")


@db_required
def test_db_settings_page_renders_three_options_with_current_checked(
    fresh_setting,
):
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    actor = "Quintessa Fixerly"
    qpf.save(
        qpf.Settings("live"),
        actor_upn="quintessa.fixerly@gruberpallets.com", actor_name=actor,
    )

    response = TestClient(app).get("/settings?section=timeclock")

    assert response.status_code == 200
    form = _quick_punch_form(response.text)
    assert _checked_modes(form) == {"off": False, "preview": False, "live": True}
    text = _normalized(response.text)
    assert "Quick-punch fixer" in text
    for option in (OFF_TEXT, PREVIEW_TEXT, LIVE_TEXT, HELP_TEXT):
        assert option in text
    history = re.search(
        r'<div id="quick-punch-fix-history".*?</div>', response.text,
        flags=re.DOTALL,
    )
    assert history, "Quick-punch fixer history block missing"
    history_text = _normalized(history.group(0))
    assert "Recent quick-punch fixer changes" in history_text
    assert actor in history_text
    assert "Preview → Live" in history_text


@db_required
def test_db_post_round_trips_and_records_the_actor(fresh_setting, monkeypatch):
    from fastapi.testclient import TestClient
    from zira_dashboard import inbox_log
    from zira_dashboard.app import app

    monkeypatch.setattr(
        inbox_log, "actor_from",
        lambda _request: ("manager@gruberpallets.com", "Plant Manager"),
    )
    client = TestClient(app)

    response = client.post(
        "/settings/quick_punch_fix", data={"mode": "off"},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert qpf.current() == qpf.Settings("off")
    assert qpf.reload() == qpf.Settings("off")

    response = client.post(
        "/settings/quick_punch_fix", data={"mode": "live"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert qpf.reload() == qpf.Settings("live")

    response = client.post(
        "/settings/quick_punch_fix", data={"mode": "bogus"},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 400
    assert qpf.reload() == qpf.Settings("live")

    events = qpf.recent_events()
    assert [
        (e["before_mode"], e["after_mode"], e["actor_upn"], e["actor_name"],
         e["source"])
        for e in events
    ] == [
        ("off", "live", "manager@gruberpallets.com", "Plant Manager", "settings"),
        ("preview", "off", "manager@gruberpallets.com", "Plant Manager", "settings"),
    ]

    page = client.get("/settings?section=timeclock")
    assert _checked_modes(_quick_punch_form(page.text))["live"] is True
