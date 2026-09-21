"""Admin-only invitation and role management."""
from __future__ import annotations

import asyncio
import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import auth, user_access
from ..deps import templates

router = APIRouter()
ROLE_LABELS = {'admin': 'Owner/Admin', 'hr': 'HR', 'manager': 'Manager', 'visitor': 'Visitor', 'timeclock': 'Timeclock only'}


def _admin(request: Request) -> str:
    actor = getattr(request.state, 'user_upn', None)
    if getattr(request.state, 'user_role', None) != 'admin' or not actor:
        raise HTTPException(403, 'Admin access is required.')
    return actor


def _serializer():
    return URLSafeTimedSerializer(auth._session_secret(), salt='user-access-csrf')


def _session_binding(request: Request) -> str:
    cookie = request.cookies.get(auth.SESSION_COOKIE_NAME, '')
    if not cookie:
        raise HTTPException(403, 'Sign in again before changing access.')
    payload = auth.verify_session(cookie)
    if payload is None:
        raise HTTPException(403, 'Sign in again before changing access.')
    # sid survives sliding refresh but changes on a new sign-in. Legacy
    # cookies keep the same digest when middleware first upgrades them.
    return payload.get('sid') or hashlib.sha256(cookie.encode()).hexdigest()


def _csrf_token(request: Request) -> str:
    return _serializer().dumps(_session_binding(request))


def _valid_csrf(request: Request, token: str) -> bool:
    try:
        value = _serializer().loads(token, max_age=7200)
        return isinstance(value, str) and secrets.compare_digest(value, _session_binding(request))
    except (BadSignature, HTTPException, TypeError):
        return False


async def _page(request: Request, error: str | None = None, status: int = 200):
    users = await asyncio.to_thread(user_access.list_users)
    response = templates.TemplateResponse(request=request, name='user_access.html', context={
        'users': users, 'role_labels': ROLE_LABELS, 'csrf_token': _csrf_token(request),
        'error': error, 'sign_in_url': str(request.base_url) + 'auth/login',
    }, status_code=status)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@router.get('/admin/users')
async def users_page(request: Request):
    _admin(request)
    return await _page(request)


@router.post('/admin/users')
async def users_save(request: Request):
    actor = _admin(request)
    form = await request.form()
    if not _valid_csrf(request, str(form.get('csrf_token', ''))):
        raise HTTPException(403, 'This form has expired. Reload the page and try again.')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Use the form on this site to change access.')
    if form.get('active') not in ('true', 'false'):
        return await _page(request, 'Choose active or revoked access.', 400)
    try:
        await asyncio.to_thread(user_access.save_user, str(form.get('email', '')),
                                str(form.get('role', '')), form['active'] == 'true', actor)
    except ValueError as exc:
        return await _page(request, str(exc), 400)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return RedirectResponse('/admin/users', status_code=303, headers={'Cache-Control': 'private, no-store'})
