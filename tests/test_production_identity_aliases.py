from __future__ import annotations

from datetime import date

from zira_dashboard import production_identity_aliases as aliases


APPROVED_NAME = "Adrian Aragon"


def _row(
    *,
    name: str = APPROVED_NAME,
    person_id: int | None = 1,
    odoo_id: str | None = "ODOO-42",
    emp_id: str | None = "ODOO-42",
    day: date | None = date(2026, 6, 2),
) -> dict:
    return {
        "approved_name": name,
        "person_id": person_id,
        "odoo_id": odoo_id,
        "emp_id": emp_id,
        "day": day,
    }


def _candidates_for(result, name=APPROVED_NAME):
    return tuple(candidate for candidate in result.candidates if candidate.confirmed_name == name)


def _skip_for(result, name=APPROVED_NAME):
    return next(skipped for skipped in result.skipped if skipped.name == name)


def test_find_confirmed_aliases_emits_each_noncanonical_id_with_auditable_days(monkeypatch):
    calls = []
    rows = [
        _row(emp_id="ODOO-42", day=date(2026, 6, 2)),
        _row(emp_id="LEGACY-7", day=date(2026, 6, 1)),
        _row(emp_id="LEGACY-7", day=date(2026, 6, 3)),
        _row(emp_id="LEGACY-8", day=date(2026, 6, 2)),
    ]

    def fake_query(sql, params):
        calls.append((sql, params))
        return rows

    monkeypatch.setattr(aliases.db, "query", fake_query)

    result = aliases.find_confirmed_aliases()

    assert _candidates_for(result) == (
        aliases.AliasCandidate(
            legacy_emp_id="LEGACY-7",
            canonical_emp_id="ODOO-42",
            confirmed_name=APPROVED_NAME,
            production_days=(date(2026, 6, 1), date(2026, 6, 3)),
        ),
        aliases.AliasCandidate(
            legacy_emp_id="LEGACY-8",
            canonical_emp_id="ODOO-42",
            confirmed_name=APPROVED_NAME,
            production_days=(date(2026, 6, 2),),
        ),
    )
    assert APPROVED_NAME not in {skipped.name for skipped in result.skipped}
    assert calls[0][1] == (sorted(aliases.APPROVED_NAMES),)
    assert "p.name = approved.name" in calls[0][0]
    assert "pd.name = approved.name" in calls[0][0]


def test_find_confirmed_aliases_skips_duplicate_exact_roster_name(monkeypatch):
    rows = [
        _row(person_id=1, odoo_id="ODOO-42", emp_id="ODOO-42"),
        _row(person_id=2, odoo_id="ODOO-77", emp_id="ODOO-77"),
        _row(person_id=1, odoo_id="ODOO-42", emp_id="LEGACY-7"),
        _row(person_id=2, odoo_id="ODOO-77", emp_id="LEGACY-7"),
    ]
    monkeypatch.setattr(aliases.db, "query", lambda sql, params: rows)

    result = aliases.find_confirmed_aliases()

    assert _candidates_for(result) == ()
    assert _skip_for(result) == (
        aliases.SkippedName(
            name=APPROVED_NAME,
            reason="duplicate_exact_roster_name",
            canonical_emp_id=None,
            observed_emp_ids=("LEGACY-7", "ODOO-42", "ODOO-77"),
            production_days=(date(2026, 6, 2),),
        ),
    )[0]


def test_find_confirmed_aliases_skips_when_current_odoo_id_is_absent_from_production(monkeypatch):
    rows = [_row(emp_id="LEGACY-7"), _row(emp_id="LEGACY-8")]
    monkeypatch.setattr(aliases.db, "query", lambda sql, params: rows)

    result = aliases.find_confirmed_aliases()

    assert _candidates_for(result) == ()
    assert _skip_for(result) == (
        aliases.SkippedName(
            name=APPROVED_NAME,
            reason="canonical_id_missing_from_production",
            canonical_emp_id="ODOO-42",
            observed_emp_ids=("LEGACY-7", "LEGACY-8"),
            production_days=(date(2026, 6, 2),),
        ),
    )[0]


def test_find_confirmed_aliases_skips_blank_production_id(monkeypatch):
    rows = [_row(emp_id="ODOO-42"), _row(emp_id="")]
    monkeypatch.setattr(aliases.db, "query", lambda sql, params: rows)

    result = aliases.find_confirmed_aliases()

    assert _candidates_for(result) == ()
    assert _skip_for(result) == (
        aliases.SkippedName(
            name=APPROVED_NAME,
            reason="blank_production_id",
            canonical_emp_id="ODOO-42",
            observed_emp_ids=("", "ODOO-42"),
            production_days=(date(2026, 6, 2),),
        ),
    )[0]


def test_find_confirmed_aliases_never_emits_unapproved_name(monkeypatch):
    rows = [
        _row(name="Unapproved Person", odoo_id="ODOO-42", emp_id="ODOO-42"),
        _row(name="Unapproved Person", odoo_id="ODOO-42", emp_id="LEGACY-7"),
    ]
    monkeypatch.setattr(aliases.db, "query", lambda sql, params: rows)

    result = aliases.find_confirmed_aliases()

    assert result.candidates == ()
    assert all(skipped.name in aliases.APPROVED_NAMES for skipped in result.skipped)


def test_all_proposed_aliases_keep_the_approved_display_name(monkeypatch):
    rows = [
        _row(emp_id="ODOO-42"),
        _row(emp_id="LEGACY-7"),
    ]
    monkeypatch.setattr(aliases.db, "query", lambda sql, params: rows)

    result = aliases.find_confirmed_aliases()

    assert {candidate.confirmed_name for candidate in _candidates_for(result)} == {APPROVED_NAME}
    assert all(candidate.confirmed_name in aliases.APPROVED_NAMES for candidate in result.candidates)


def test_upsert_empty_candidates_performs_no_database_write(monkeypatch):
    writes = []
    monkeypatch.setattr(
        aliases.db,
        "execute_many",
        lambda sql, params: writes.append((sql, params)),
    )

    assert aliases.upsert_confirmed_aliases(()) == 0

    assert writes == []


def test_upsert_writes_only_approved_candidates_with_a_parameterized_conflict_update(monkeypatch):
    writes = []
    monkeypatch.setattr(
        aliases.db,
        "execute_many",
        lambda sql, params: writes.append((sql, list(params))),
    )
    candidates = (
        aliases.AliasCandidate(
            legacy_emp_id="LEGACY-7",
            canonical_emp_id="ODOO-42",
            confirmed_name=APPROVED_NAME,
            production_days=(date(2026, 6, 1),),
        ),
    )

    assert aliases.upsert_confirmed_aliases(candidates) == 1

    sql, params = writes[0]
    assert "ON CONFLICT (legacy_emp_id) DO UPDATE" in sql
    assert "confirmed_at = now()" in sql
    assert params == [("LEGACY-7", "ODOO-42", APPROVED_NAME, aliases.SOURCE)]


def test_upsert_rejects_candidate_outside_the_approved_name_allow_list(monkeypatch):
    monkeypatch.setattr(aliases.db, "execute_many", lambda sql, params: None)
    candidate = aliases.AliasCandidate(
        legacy_emp_id="LEGACY-7",
        canonical_emp_id="ODOO-42",
        confirmed_name="Unapproved Person",
        production_days=(date(2026, 6, 1),),
    )

    try:
        aliases.upsert_confirmed_aliases((candidate,))
    except ValueError as exc:
        assert "approved-name reconciliation" in str(exc)
    else:
        raise AssertionError("expected an unsafe candidate to be rejected")
