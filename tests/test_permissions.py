"""Security boundaries for invited personal access; TVs retain their own policy."""
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from zira_dashboard import auth, permissions


@pytest.mark.parametrize('role,method,path,expected', [
    ('visitor', 'GET', '/recycling', True),
    ('visitor', 'GET', '/people-performance/rows', True),
    ('visitor', 'POST', '/api/layout/recycling', False),
    ('visitor', 'GET', '/staffing', False),
    ('manager', 'POST', '/staffing', True),
    ('manager', 'POST', '/staffing/skills/cell', True),
    ('manager', 'GET', '/staffing/hours', False),
    ('manager', 'GET', '/staffing/time-off', False),
    ('manager', 'GET', '/staffing/people/Dale/acknowledgements', False),
    ('manager', 'POST', '/api/exceptions/time-off/1/approve', False),
    ('manager', 'POST', '/settings/api-keys', False),
    ('hr', 'POST', '/api/exceptions/time-off/1/approve', True),
    ('hr', 'POST', '/staffing/people/add', True),
    ('hr', 'GET', '/staffing/hours', True),
    ('hr', 'POST', '/settings', False),
    ('hr', 'GET', '/admin/users', False),
    ('admin', 'POST', '/admin/users', True),
    ('manager', 'GET', '/future-sensitive-page', False),
    ('visitor', 'GET', '/staffing/people/Alice', False),
    ('visitor', 'GET', '/recycling/../settings', False),
])
def test_role_matrix(role, method, path, expected):
    assert permissions.allowed(role, method, path) is expected


@pytest.fixture
def personal_client(monkeypatch):
    from zira_dashboard import user_access
    monkeypatch.delenv('AUTH_DISABLED', raising=False)
    access = {'role': 'manager', 'email': 'person@gruberpallets.com', 'active': True}
    monkeypatch.setattr(user_access, 'lookup_active', lambda email: access if access['active'] else None)
    app = FastAPI()
    app.add_middleware(auth.RequireAuthMiddleware)
    for path in ['/staffing', '/settings', '/tv/example', '/recycling']:
        app.add_api_route(path, lambda: PlainTextResponse('protected data'), methods=['GET', 'POST'])
    token = auth.mint_session(sub='person', upn=access['email'], name='Person')
    return TestClient(app, cookies={auth.SESSION_COOKIE_NAME: token}), access


def test_existing_session_rechecks_access_and_role(personal_client):
    client, access = personal_client
    assert client.get('/staffing').status_code == 200
    assert client.get('/settings').status_code == 403
    access['role'] = 'admin'
    assert client.get('/settings').status_code == 200
    access['role'] = 'visitor'
    assert client.post('/staffing').status_code == 403
    access['active'] = False
    assert client.get('/recycling').status_code == 403


def test_uninvited_domain_user_is_denied(personal_client):
    client, access = personal_client
    access['active'] = False
    response = client.get('/staffing')
    assert response.status_code == 403
    assert 'protected data' not in response.text


def test_tv_still_anonymous_and_unaffected_by_revocation(personal_client):
    client, access = personal_client
    access['active'] = False
    client.cookies.clear()
    assert client.get('/tv/example').status_code == 200


def test_access_store_failure_fails_closed(personal_client, monkeypatch):
    from zira_dashboard import user_access
    client, _ = personal_client
    def unavailable(email):
        raise RuntimeError('private database details')
    monkeypatch.setattr(user_access, 'lookup_active', unavailable)
    result = client.get('/recycling')
    assert result.status_code == 503
    assert 'private database details' not in result.text


def test_signed_outside_domain_session_is_denied(personal_client):
    client, _ = personal_client
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.mint_session(sub='x', upn='x@elsewhere.com', name='x'))
    assert client.get('/recycling').status_code == 403


def test_personal_response_not_browser_cached(personal_client):
    client, _ = personal_client
    assert client.get('/recycling').headers['cache-control'] == 'private, no-store'


def test_rendered_cache_is_partitioned_by_role():
    from zira_dashboard import _http_cache
    token = permissions.current_role.set('admin')
    try:
        _http_cache.store_cached_response(('page',), includes_today=True, response=PlainTextResponse('secret'))
        permissions.current_role.set('manager')
        assert _http_cache.get_cached_response(('page',), includes_today=True) is None
        permissions.current_role.set('admin')
        assert _http_cache.get_cached_response(('page',), includes_today=True).body == b'secret'
    finally:
        permissions.current_role.reset(token)


def test_manager_staffing_projection_removes_pay_and_edit_information():
    original = [{'name': 'Alice', 'pay_type': 'Medical leave', 'timing_label': 'Absent · PTO',
                 'manual_absent': True, 'editable': True, 'request_id': 7, 'hours': None}]
    result = permissions.staffing_entries(original, role='manager')
    assert result[0]['timing_label'] == 'Absent'
    assert result[0]['editable'] is False
    assert 'Medical' not in str(result) and 'PTO' not in str(result)
    assert 'request_id' not in result[0]
    assert original[0]['editable'] is True


def test_manager_inbox_excludes_sensitive_and_unknown_sections():
    original = {'sections': [
        {'id': 'assignments', 'count': 1, 'title': 'Assignments', 'tone': 'warn', 'rows': [{'name': 'Alice', 'priority': 'urgent'}]},
        {'id': 'time_off', 'count': 1, 'rows': [{'detail': 'Medical leave'}]},
        {'id': 'future_hr_data', 'count': 1, 'rows': [{'detail': 'secret'}]},
    ], 'queue': [{'detail': 'Medical leave'}], 'total': 3, 'source_errors': [{'source': 'Pending Time Off'}]}
    result = permissions.inbox_snapshot(original, role='manager')
    assert result['total'] == 1
    assert result['urgent_total'] == 1
    assert 'Medical' not in str(result) and 'secret' not in str(result)
    assert original['total'] == 3


@pytest.mark.parametrize('role,expected', [(None, 403), ('visitor', 302), ('hr', 302)])
def test_microsoft_callback_requires_invitation(monkeypatch, role, expected):
    from types import SimpleNamespace
    from zira_dashboard import user_access
    from zira_dashboard.routes import auth as auth_routes
    async def exchange(request):
        return {'userinfo': {'sub': '1', 'preferred_username': 'person@gruberpallets.com', 'name': 'Person'}}
    monkeypatch.setattr(auth, 'oauth_client', lambda: SimpleNamespace(azure=SimpleNamespace(authorize_access_token=exchange)))
    monkeypatch.setattr(user_access, 'lookup_active', lambda email: {'role': role} if role else None)
    app = FastAPI()
    app.include_router(auth_routes.router)
    client = TestClient(app)
    response = client.get('/auth/callback', follow_redirects=False)
    assert response.status_code == expected
    assert (auth.SESSION_COOKIE_NAME in response.cookies) is bool(role)


def test_every_registered_personal_route_has_explicit_policy():
    """A future route addition must consciously choose its minimum role."""
    import ast
    from pathlib import Path
    policy = {item for entries in permissions.ROUTES.values() for item in entries}
    discovered = set()
    for source in (Path(__file__).parents[1] / 'src/zira_dashboard/routes').glob('*.py'):
        if source.stem in {'auth', 'object_api'}:
            continue
        tree = ast.parse(source.read_text())
        prefix = ''
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'APIRouter':
                prefix = next((ast.literal_eval(k.value) for k in node.keywords if k.arg == 'prefix'), '')
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr.upper()
                if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
                    continue
                path = prefix + ast.literal_eval(decorator.args[0])
                if not auth._is_bypass_path(path):
                    discovered.add((method, path))
    assert discovered == policy


@pytest.mark.parametrize('role,staffing,hr,admin', [
    ('visitor', False, False, False), ('manager', True, False, False),
    ('hr', True, True, False), ('admin', True, True, True),
])
def test_navigation_obeys_role(monkeypatch, role, staffing, hr, admin):
    from zira_dashboard.deps import templates
    monkeypatch.setitem(templates.env.globals, 'nav_inbox_summary', lambda: {'total': 0, 'urgent_total': 0, 'source_errors': []})
    token = permissions.current_role.set(role)
    try:
        nav = templates.env.get_template('_topnav.html').render()
        subnav = templates.env.get_template('_staffing_subnav.html').render()
    finally:
        permissions.current_role.reset(token)
    assert ('href="/staffing"' in nav) is staffing
    assert ('href="/admin/users"' in nav) is admin
    assert ('href="/settings"' in nav) is admin
    assert ('href="/staffing/time-off"' in subnav) is hr


def test_inbox_json_and_html_manager_do_not_contain_hr_data(monkeypatch):
    from zira_dashboard import exception_inbox
    from zira_dashboard.routes import exceptions
    from zira_dashboard.deps import templates
    from tests.test_exception_inbox import _snapshot
    snapshot = _snapshot()
    snapshot['sections'].append({'id': 'time_off', 'count': 1, 'rows': [{'detail': 'PRIVATE_MEDICAL_REASON'}]})
    monkeypatch.setattr(exception_inbox, 'build_snapshot', lambda: snapshot)
    monkeypatch.setitem(templates.env.globals, 'static_v', lambda name: 'test')
    app = FastAPI()
    app.include_router(exceptions.router)
    token = permissions.current_role.set('manager')
    try:
        client = TestClient(app)
        for path in ['/api/exceptions', '/exceptions', '/api/exceptions/summary']:
            response = client.get(path)
            assert response.status_code == 200
            assert 'PRIVATE_MEDICAL_REASON' not in response.text
            assert 'data-archive-toggle' not in response.text
    finally:
        permissions.current_role.reset(token)


def test_new_admin_role_grants_existing_super_admin_controls():
    from starlette.requests import Request
    request = Request({'type': 'http', 'headers': []})
    request.state.user_upn = 'invited-admin@gruberpallets.com'
    request.state.user_role = 'admin'
    assert auth.request_is_super_admin(request)
    request.state.user_upn = 'dale@gruberpallets.com'
    request.state.user_role = 'manager'
    assert not auth.request_is_super_admin(request)


def test_admin_form_survives_legacy_session_refresh(monkeypatch):
    """A pre-upgrade session gets a stable ID without invalidating its form."""
    import re
    import time
    from zira_dashboard import user_access
    from zira_dashboard.routes import user_access as routes
    from zira_dashboard.deps import templates
    monkeypatch.delenv('AUTH_DISABLED', raising=False)
    monkeypatch.setattr(auth, 'needs_refresh', lambda payload: True)
    monkeypatch.setattr(user_access, 'lookup_active', lambda email: {'email': email, 'role': 'admin', 'active': True})
    monkeypatch.setattr(user_access, 'list_users', lambda: [])
    calls = []
    monkeypatch.setattr(user_access, 'save_user', lambda *args: calls.append(args))
    monkeypatch.setitem(templates.env.globals, 'nav_inbox_summary', lambda: {'total': 0, 'urgent_total': 0, 'source_errors': []})
    monkeypatch.setitem(templates.env.globals, 'static_v', lambda name: 'test')
    old = auth.jwt.encode({'alg': 'HS256'}, {
        'sub': '1', 'upn': 'dale@gruberpallets.com', 'name': 'Dale',
        'iat': int(time.time()), 'exp': int(time.time()) + 3600,
    }, auth._jwt_key(auth._session_secret()))
    app = FastAPI()
    app.add_middleware(auth.RequireAuthMiddleware)
    app.include_router(routes.router)
    client = TestClient(app, base_url='https://testserver')
    client.cookies.set(auth.SESSION_COOKIE_NAME, old, domain='testserver.local', path='/')
    page = client.get('/admin/users')
    assert page.status_code == 200
    refreshed = client.cookies.get(auth.SESSION_COOKIE_NAME)
    assert refreshed != old
    assert auth.verify_session(refreshed)['sid']
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = client.post('/admin/users', data={
        'csrf_token': token, 'email': 'new@gruberpallets.com', 'role': 'manager', 'active': 'true',
    }, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [('new@gruberpallets.com', 'manager', True, 'dale@gruberpallets.com')]
