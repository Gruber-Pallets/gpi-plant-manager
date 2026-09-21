"""Shared Odoo-plus-local skill-level writer.

One promotion path used by both the People Matrix cell endpoint and the
training-block completion in :mod:`rotation_training`. Callers pass *local*
``people.id`` / ``skills.id`` (training blocks store local ids). The writer
resolves the Odoo ids, pushes the level to Odoo first, then mirrors the change
into the local ``person_skills`` table and invalidates the roster + HTTP
caches.

Matrix-specific validation (skill_type, 404s, payload parsing) intentionally
stays in the endpoint; this module only knows how to persist a level.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager

from . import _http_cache, db, odoo_client, staffing

log = logging.getLogger(__name__)


class SkillSyncError(RuntimeError):
    """Raised when the change cannot be pushed to Odoo.

    A ``SkillSyncError`` means Odoo rejected the write (or the Odoo id could not
    be resolved) and no local mirror was performed, so callers can map it to the
    "Odoo save failed" outcome. A failure *after* Odoo succeeds (local mirror or
    cache refresh) propagates as its original exception type instead.
    """


def _resolve_odoo_id(sql: str, local_id: int, label: str) -> int:
    # Resolution runs before the Odoo call, so a DB blip here means Odoo was
    # never reached. Surface it as SkillSyncError so callers treat it as an
    # Odoo-side failure (no local write) rather than a post-Odoo mirror failure.
    try:
        rows = db.query(sql, (local_id,))
    except Exception as exc:  # noqa: BLE001 - normalized into a typed failure
        raise SkillSyncError(f"Could not resolve Odoo id for {label} {local_id}: {exc}") from exc
    odoo_id = rows[0].get("odoo_id") if rows else None
    if not odoo_id:
        raise SkillSyncError(f"No Odoo id for {label} {local_id}.")
    return int(odoo_id)


def _mirror_local(person_id: int, skill_id: int, level: int) -> None:
    if level == 0:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM person_skills WHERE person_id = %s AND skill_id = %s",
                (person_id, skill_id),
            )
        return

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO person_skills "
            "(person_id, skill_id, level, last_pushed_at, local_dirty) "
            "VALUES (%s, %s, %s, now(), FALSE) "
            "ON CONFLICT (person_id, skill_id) DO UPDATE SET "
            "level = EXCLUDED.level, last_pushed_at = EXCLUDED.last_pushed_at, "
            "local_dirty = FALSE",
            (person_id, skill_id, level),
        )


@contextmanager
def _skill_write_guard(person_id: int, skill_id: int):
    # Matrix edits and training share this lock, including the external write.
    # Only failures before yielding mean Odoo has definitely not been changed.
    with ExitStack() as stack:
        try:
            cur = stack.enter_context(db.cursor())
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
                        (f"person-skill:{person_id}:{skill_id}",))
            if not cur.fetchone()["acquired"]:
                raise SkillSyncError("This skill is being updated. Please retry.")
        except SkillSyncError:
            raise
        except Exception as exc:
            raise SkillSyncError("Could not reserve this skill update. Please retry.") from exc
        yield


def set_person_skill_level(person_id: int, skill_id: int, level: int) -> None:
    with _skill_write_guard(person_id, skill_id):
        _write_person_skill_level(person_id, skill_id, level)


def promote_untrained_person_skill(person_id: int, skill_id: int) -> None:
    """Raise only a still-missing production skill, without racing Matrix edits."""
    with _skill_write_guard(person_id, skill_id):
        rows = db.query(
            "SELECT s.skill_type, COALESCE(ps.level, 0) AS level FROM skills s "
            "LEFT JOIN person_skills ps ON ps.skill_id=s.id AND ps.person_id=%s "
            "WHERE s.id=%s", (person_id, skill_id),
        )
        if not rows:
            raise SkillSyncError("The training skill could not be found.")
        if rows[0]["skill_type"] != "Certifications" and int(rows[0]["level"]) == 0:
            _write_person_skill_level(person_id, skill_id, 1)


def _write_person_skill_level(person_id: int, skill_id: int, level: int) -> None:
    """Set a person's skill level in Odoo and mirror it locally.

    ``person_id`` and ``skill_id`` are local ``people.id`` / ``skills.id``.
    Odoo is written first; only on success is the local mirror updated and the
    roster/HTTP caches invalidated. Raises :class:`SkillSyncError` when Odoo
    rejects the change (no local write happens in that case).
    """
    person_odoo_id = _resolve_odoo_id(
        "SELECT odoo_id FROM people WHERE id = %s", person_id, "person"
    )
    skill_odoo_id = _resolve_odoo_id(
        "SELECT odoo_id FROM skills WHERE id = %s", skill_id, "skill"
    )

    try:
        odoo_client.set_employee_skill_level(person_odoo_id, skill_odoo_id, level)
    except Exception as exc:  # noqa: BLE001 - normalized into a typed failure
        raise SkillSyncError(str(exc)) from exc

    _mirror_local(person_id, skill_id, level)
    staffing._invalidate_roster_cache()
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
