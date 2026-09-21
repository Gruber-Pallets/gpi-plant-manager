from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from zira_dashboard import user_access
from zira_dashboard.routes import user_access as routes


@pytest.mark.parametrize('email', ['x@example.com', 'x@sub.gruberpallets.com', '@gruberpallets.com', 'x y@gruberpallets.com', 'x@gruberpallets.com.evil', 'x..y@gruberpallets.com'])
def test_email_rejected(email):
    with pytest.raises(ValueError):
        user_access.normalize_email(email)


def test_email_normalized():
    assert user_access.normalize_email(' Dale@GruberPallets.com ') == 'dale@gruberpallets.com'


class Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def fetchone(self):
        return next(self.rows)


def database(monkeypatch, rows):
    cur = Cursor(rows)
    @contextmanager
    def cursor():
        yield cur
    monkeypatch.setattr(user_access.db, 'cursor', cursor)
    return cur


def test_unknown_role_rejected():
    with pytest.raises(ValueError):
        user_access.save_user('a@gruberpallets.com', 'owner', True, 'dale@gruberpallets.com')


def test_lookup_active(monkeypatch):
    cur = database(monkeypatch, [None])
    assert user_access.lookup_active('a@gruberpallets.com') is None
    assert 'active = TRUE' in cur.calls[0][0]


def test_last_admin_protected_after_lock(monkeypatch):
    cur = database(monkeypatch, [{'role': 'admin', 'active': True}, {'role': 'admin', 'active': True}, {'count': 1}])
    with pytest.raises(ValueError, match='last active admin'):
        user_access.save_user('dale@gruberpallets.com', 'visitor', True, 'dale@gruberpallets.com')
    assert 'pg_advisory_xact_lock' in cur.calls[0][0]
    assert not any('INSERT INTO app_users' in sql for sql, _ in cur.calls)


def test_actor_revalidated(monkeypatch):
    cur = database(monkeypatch, [None])
    with pytest.raises(PermissionError):
        user_access.save_user('a@gruberpallets.com', 'visitor', True, 'dale@gruberpallets.com')
    assert not any('INSERT' in sql for sql, _ in cur.calls)


def test_change_audited(monkeypatch):
    cur = database(monkeypatch, [{'role': 'admin', 'active': True}, None])
    user_access.save_user('A@gruberpallets.com', 'hr', True, 'Dale@gruberpallets.com')
    assert cur.calls[-1][1] == ('a@gruberpallets.com', None, None, 'hr', True, 'dale@gruberpallets.com')
    assert 'app_user_access_audit' in cur.calls[-1][0]


def client(monkeypatch, role='admin'):
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.user_role = role
        request.state.user_upn = 'dale@gruberpallets.com'
        return await call_next(request)
    app.include_router(routes.router)
    monkeypatch.setattr(routes.auth, '_session_secret', lambda: 'test-secret-32-bytes-of-random-data!!')
    monkeypatch.setitem(routes.templates.env.globals, 'static_v', lambda name: 'test')
    return TestClient(app)


def test_admin_required(monkeypatch):
    assert client(monkeypatch, 'manager').get('/admin/users').status_code == 403


def test_csrf_session_binding(monkeypatch):
    from starlette.requests import Request
    def request(cookie):
        return Request({'type':'http', 'headers':[(b'cookie', f'gpi_session={cookie}'.encode())]})
    monkeypatch.setattr(routes.auth, '_session_secret', lambda: 'test-secret-32-bytes-of-random-data!!')
    first = routes.auth.mint_session(sub='1', upn='dale@gruberpallets.com', name='Dale')
    second = routes.auth.mint_session(sub='1', upn='dale@gruberpallets.com', name='Dale')
    token = routes._csrf_token(request(first))
    assert routes._valid_csrf(request(first), token)
    assert not routes._valid_csrf(request(second), token)
    assert not routes._valid_csrf(request(first), token + 'bad')
    refreshed = routes.auth.mint_session(sub='1', upn='dale@gruberpallets.com', name='Dale',
                                        sid=routes.auth.verify_session(first)['sid'])
    assert routes._valid_csrf(request(refreshed), token)


def test_missing_csrf_rejects_write(monkeypatch):
    response = client(monkeypatch).post('/admin/users', data={'email':'a@gruberpallets.com','role':'visitor','active':'true'})
    assert response.status_code == 403


def test_admin_page_and_valid_write(monkeypatch):
    monkeypatch.setattr(user_access, 'list_users', lambda: [])
    calls = []
    monkeypatch.setattr(user_access, 'save_user', lambda *args: calls.append(args))
    browser = client(monkeypatch)
    browser.cookies.set('gpi_session', routes.auth.mint_session(sub='1', upn='dale@gruberpallets.com', name='Dale'))
    response = browser.get('/admin/users')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]
    form = {'email':'a@gruberpallets.com','role':'hr','active':'true','csrf_token':token}
    assert browser.post('/admin/users', data=form, headers={'Origin':'https://evil.example'}).status_code == 403
    assert not calls
    assert browser.post('/admin/users', data=form, follow_redirects=False).status_code == 303
    assert calls == [('a@gruberpallets.com', 'hr', True, 'dale@gruberpallets.com')]


def test_postgres_seed_concurrency_and_atomic_audit(monkeypatch):
    """Optional isolated local integration test; never uses DATABASE_URL."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    import psycopg2
    from psycopg2.extras import RealDictCursor

    from zira_dashboard._schema import USER_ACCESS_DDL

    dsn = os.environ.get('USER_ACCESS_TEST_DSN')
    if not dsn:
        pytest.skip('Set USER_ACCESS_TEST_DSN to an isolated local PostgreSQL database')
    assert '127.0.0.1' in dsn or 'localhost' in dsn
    schema = 'test_access_' + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA {schema}')

    @contextmanager
    def cursor():
        conn = psycopg2.connect(dsn)
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f'SET search_path TO {schema}')
                    yield cur
        finally:
            conn.close()

    monkeypatch.setattr(user_access.db, 'cursor', cursor)
    try:
        with cursor() as cur:
            cur.execute(USER_ACCESS_DDL)
        owner = 'dale@gruberpallets.com'
        second = 'second@gruberpallets.com'
        user_access.save_user(second, 'admin', True, owner)
        barrier = Barrier(2)
        def revoke(email):
            barrier.wait()
            try:
                user_access.save_user(email, 'admin', False, email)
                return 'saved'
            except ValueError:
                return 'protected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(revoke, [owner, second])) == ['protected', 'saved']
        with cursor() as cur:
            cur.execute("SELECT email FROM app_users WHERE active AND role='admin'")
            survivor = cur.fetchone()['email']
        removed = owner if survivor == second else second
        assert user_access.lookup_active(removed) is None
        with pytest.raises(PermissionError):
            user_access.save_user('third@gruberpallets.com', 'admin', True, removed)
        # Explicitly revoke owner, then bootstrap again. The marker is permanent.
        if survivor == owner:
            user_access.save_user(second, 'admin', True, owner)
            user_access.save_user(owner, 'admin', False, second)
        with cursor() as cur:
            cur.execute(USER_ACCESS_DDL)
        assert user_access.lookup_active(owner) is None
        # A failing audit insert rolls back the access change too.
        with cursor() as cur:
            cur.execute("ALTER TABLE app_user_access_audit ADD CONSTRAINT reject_test CHECK (email <> 'fail@gruberpallets.com')")
        with pytest.raises(psycopg2.IntegrityError):
            user_access.save_user('fail@gruberpallets.com', 'visitor', True, second)
        assert user_access.lookup_active('fail@gruberpallets.com') is None
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA {schema} CASCADE')
        admin.close()


def test_timeclock_grant_is_audited(monkeypatch):
    cur = database(monkeypatch, [{'role': 'admin', 'active': True}, None])
    user_access.save_user('info@gruberpallets.com', 'timeclock', True, 'dale@gruberpallets.com')
    assert cur.calls[-1][1] == ('info@gruberpallets.com', None, None, 'timeclock', True, 'dale@gruberpallets.com')


def test_timeclock_role_database_upgrade():
    import os
    from uuid import uuid4
    import psycopg2
    from zira_dashboard._schema import USER_ACCESS_DDL, TIMECLOCK_ACCESS_DDL
    dsn = os.environ.get('USER_ACCESS_TEST_DSN') or os.environ.get('DATABASE_URL')
    if not dsn:
        pytest.skip('Requires isolated test PostgreSQL')
    schema = 'test_timeclock_access_' + uuid4().hex
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA {schema}')
            cur.execute(f'SET LOCAL search_path TO {schema}')
            cur.execute(USER_ACCESS_DDL)
            cur.execute(TIMECLOCK_ACCESS_DDL)
            cur.execute("INSERT INTO app_users (email,role,updated_by) VALUES (%s,'timeclock','test')", ('info@gruberpallets.com',))
            # Repeated bootstrap preserves the granted role.
            cur.execute(USER_ACCESS_DDL + TIMECLOCK_ACCESS_DDL)
            cur.execute('SELECT role FROM app_users WHERE email=%s', ('info@gruberpallets.com',))
            assert cur.fetchone() == ('timeclock',)
            cur.execute('SAVEPOINT invalid_role')
            with pytest.raises(psycopg2.IntegrityError):
                cur.execute("UPDATE app_users SET role='unknown'")
            cur.execute('ROLLBACK TO SAVEPOINT invalid_role')
    finally:
        conn.rollback()
        conn.close()
