"""Durable personal invitations. Access is deliberately never cached."""
from __future__ import annotations

import re

from . import db

ROLES = ('admin', 'hr', 'manager', 'visitor')
_ACCESS_LOCK = 741903221


def normalize_email(email: str) -> str:
    value = str(email or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@gruberpallets\.com", value):
        raise ValueError('Enter an exact gruberpallets.com email address.')
    return value


def lookup_active(email: str) -> dict | None:
    try:
        email = normalize_email(email)
    except ValueError:
        return None
    with db.cursor() as cur:
        cur.execute('SELECT email, role, active FROM app_users WHERE email = %s AND active = TRUE', (email,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    return db.query('SELECT email, role, active, updated_at, updated_by FROM app_users ORDER BY email')


def save_user(email: str, role: str, active: bool, actor: str) -> None:
    email, actor = normalize_email(email), normalize_email(actor)
    if role not in ROLES:
        raise ValueError('Choose a valid role.')
    if not isinstance(active, bool):
        raise ValueError('Choose active or revoked access.')
    with db.cursor() as cur:
        # All changes share this lock; separate READ COMMITTED statements see
        # the previous writer's commit before checking the remaining admins.
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (_ACCESS_LOCK,))
        cur.execute('SELECT role, active FROM app_users WHERE email = %s', (actor,))
        acting = cur.fetchone()
        if not acting or not acting['active'] or acting['role'] != 'admin':
            raise PermissionError('An active admin must change user access.')
        cur.execute('SELECT role, active FROM app_users WHERE email = %s', (email,))
        previous = cur.fetchone()
        if previous and previous['active'] and previous['role'] == 'admin' and (not active or role != 'admin'):
            cur.execute("SELECT count(*) AS count FROM app_users WHERE active = TRUE AND role = 'admin'")
            if cur.fetchone()['count'] <= 1:
                raise ValueError('You cannot remove or change the last active admin.')
        cur.execute('''INSERT INTO app_users (email, role, active, updated_by)
            VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO UPDATE SET
            role = EXCLUDED.role, active = EXCLUDED.active,
            updated_by = EXCLUDED.updated_by, updated_at = now()''', (email, role, active, actor))
        cur.execute('''INSERT INTO app_user_access_audit
            (email, previous_role, previous_active, role, active, actor)
            VALUES (%s, %s, %s, %s, %s, %s)''',
            (email, previous['role'] if previous else None,
             previous['active'] if previous else None, role, active, actor))
