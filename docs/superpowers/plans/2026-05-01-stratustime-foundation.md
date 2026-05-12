# StratusTime Integration — Foundation

**Goal:** Prove we can authenticate against StratusTime's web services and pull real data. Build the client module + a smoke endpoint surfaced on the Settings page. No data sync into the rest of the app yet.

**Architecture:** New module `src/zira_dashboard/stratustime_client.py` reads two env vars (`STRATUSTIME_SHARED_KEY`, `STRATUSTIME_WS_PASSWORD`), and exposes `health_check()` and `list_employees()` helpers. Auth scheme is unknown from public docs, so `health_check()` tries common patterns in order (Basic auth, header pair, in-body wrapper) and reports which one worked. A new "Integrations" section on `/settings` displays the result so Dale can verify connectivity.

**Tech Stack:** Python 3.11+, `httpx` (already a transitive dep via FastAPI/starlette; if not available, fall back to `urllib.request` from stdlib).

**Sub-project context:** This is sub-project #1 of a larger StratusTime integration (foundation → time-off sync → custom hours → attendance confirmation). Each later sub-project builds on this client module.

---

## File touch map

- **Create:** `src/zira_dashboard/stratustime_client.py` — client module + auth-discovery
- **Modify:** `src/zira_dashboard/routes/settings.py` — accept `section=integrations`, fetch `health_check()` lazily
- **Modify:** `src/zira_dashboard/templates/settings.html` — add sidebar item + integrations panel
- **Tests:** `tests/test_stratustime_client.py` — auth-pattern selection logic with mocked HTTP responses (no real network calls)

No DB migrations. Two new env vars.

---

## Step 1 — Create `stratustime_client.py`

Create `src/zira_dashboard/stratustime_client.py`:

```python
"""Client for the StratusTime time-clock web services API.

Auth is configured via two env vars:
  - STRATUSTIME_SHARED_KEY   (UUID configured in StratusTime's "Inbound Services" admin page)
  - STRATUSTIME_WS_PASSWORD  (the wsuser password set on that same page)

The exact auth wire format is not documented publicly, so `health_check()`
tries several common patterns in order and returns which one worked.
Once we know the right one, callers can use `request()` directly.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error
from typing import Any

BASE_URL = "https://stratustime.centralservers.com/Service/ws-json"
DEFAULT_VERSION = "v1"
TIMEOUT_SECONDS = 30


def _shared_key() -> str | None:
    return os.environ.get("STRATUSTIME_SHARED_KEY")


def _ws_password() -> str | None:
    return os.environ.get("STRATUSTIME_WS_PASSWORD")


def _build_url(path: str, version: str = DEFAULT_VERSION) -> str:
    path = path.lstrip("/")
    return f"{BASE_URL}/{version}/{path}"


# Auth strategies — each is (name, request-builder).
# A request-builder receives the URL, body, shared key, password and returns
# (final_url, headers_dict, final_body_bytes). The body is JSON-encoded by us
# at the call site.

def _auth_basic(url: str, body: dict, key: str, pwd: str):
    """Basic auth: base64(SharedKey:wsPassword)."""
    token = base64.b64encode(f"{key}:{pwd}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}",
    }
    return url, headers, json.dumps(body).encode()


def _auth_header_pair(url: str, body: dict, key: str, pwd: str):
    """Custom headers Shared-Key + Password."""
    headers = {
        "Content-Type": "application/json",
        "Shared-Key": key,
        "Password": pwd,
    }
    return url, headers, json.dumps(body).encode()


def _auth_body_wrapper(url: str, body: dict, key: str, pwd: str):
    """Credentials embedded in JSON body."""
    wrapped = {"SharedKey": key, "Password": pwd, **body}
    headers = {"Content-Type": "application/json"}
    return url, headers, json.dumps(wrapped).encode()


AUTH_STRATEGIES = [
    ("basic", _auth_basic),
    ("header-pair", _auth_header_pair),
    ("body-wrapper", _auth_body_wrapper),
]


def _try_request(method: str, url: str, body: dict, scheme_name: str, builder) -> tuple[int, str]:
    """Make an HTTP call using the given auth strategy. Returns (status, body_text)."""
    key = _shared_key() or ""
    pwd = _ws_password() or ""
    final_url, headers, payload = builder(url, body, key, pwd)
    req = urllib.request.Request(
        final_url,
        data=payload if method != "GET" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"
    except Exception as e:
        return 0, f"error: {e}"


def health_check() -> dict:
    """Try to authenticate against a known endpoint and report results.

    Returns:
      {
        "ok": bool,
        "configured": bool,           # both env vars present
        "scheme": str | None,         # which auth pattern worked (None if all failed)
        "endpoint": str,              # the URL we hit
        "status": int,                # HTTP status of the working call (0 if all failed)
        "body_preview": str,          # first 200 chars of the working response
        "attempts": [                 # diagnostic trail
          {"scheme": str, "status": int, "body_preview": str},
          ...
        ],
      }
    """
    if not _shared_key() or not _ws_password():
        return {
            "ok": False,
            "configured": False,
            "scheme": None,
            "endpoint": "",
            "status": 0,
            "body_preview": "",
            "attempts": [],
        }

    # Use a likely-stable smoke endpoint. We don't know the exact path yet,
    # so try the employees list — most TWS systems expose one. If 404, the
    # error body should hint at the correct path.
    smoke_path = "/Employees"
    url = _build_url(smoke_path)

    attempts = []
    for name, builder in AUTH_STRATEGIES:
        status, body = _try_request("GET", url, {}, name, builder)
        preview = body[:200].replace("\n", " ")
        attempts.append({"scheme": name, "status": status, "body_preview": preview})
        if 200 <= status < 300:
            return {
                "ok": True,
                "configured": True,
                "scheme": name,
                "endpoint": url,
                "status": status,
                "body_preview": preview,
                "attempts": attempts,
            }

    return {
        "ok": False,
        "configured": True,
        "scheme": None,
        "endpoint": url,
        "status": attempts[-1]["status"] if attempts else 0,
        "body_preview": attempts[-1]["body_preview"] if attempts else "",
        "attempts": attempts,
    }


def list_employees() -> list[dict]:
    """Smoke fetch — once health_check succeeds, this should return employees.

    Uses the auth scheme discovered by health_check. If health_check hasn't
    succeeded, returns []. Caller should display health_check details first.
    """
    hc = health_check()
    if not hc["ok"]:
        return []
    builder = dict(AUTH_STRATEGIES)[hc["scheme"]]
    status, body = _try_request("GET", hc["endpoint"], {}, hc["scheme"], builder)
    if not (200 <= status < 300):
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Employees", "employees", "data", "Data", "Items"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []
```

## Step 2 — Tests for `stratustime_client.py`

Create `tests/test_stratustime_client.py`:

```python
"""Tests for the StratusTime client module.

Covers the auth-strategy selection logic. Real network calls are mocked.
"""
from unittest.mock import patch, MagicMock

import pytest

from zira_dashboard import stratustime_client as stc


@pytest.fixture
def env_creds(monkeypatch):
    monkeypatch.setenv("STRATUSTIME_SHARED_KEY", "test-key-uuid")
    monkeypatch.setenv("STRATUSTIME_WS_PASSWORD", "test-password")


def test_health_check_unconfigured_when_no_env_vars(monkeypatch):
    monkeypatch.delenv("STRATUSTIME_SHARED_KEY", raising=False)
    monkeypatch.delenv("STRATUSTIME_WS_PASSWORD", raising=False)
    result = stc.health_check()
    assert result["configured"] is False
    assert result["ok"] is False


def test_health_check_partial_config(monkeypatch):
    monkeypatch.setenv("STRATUSTIME_SHARED_KEY", "k")
    monkeypatch.delenv("STRATUSTIME_WS_PASSWORD", raising=False)
    result = stc.health_check()
    assert result["configured"] is False
    assert result["ok"] is False


def test_health_check_first_scheme_succeeds(env_creds):
    with patch.object(stc, "_try_request", return_value=(200, '{"Employees": []}')):
        result = stc.health_check()
    assert result["ok"] is True
    assert result["scheme"] == "basic"
    assert result["status"] == 200


def test_health_check_falls_through_to_second_scheme(env_creds):
    responses = iter([(401, "unauthorized"), (200, '{"Employees": []}'), (0, "")])
    with patch.object(stc, "_try_request", side_effect=lambda *a, **k: next(responses)):
        result = stc.health_check()
    assert result["ok"] is True
    assert result["scheme"] == "header-pair"
    assert len(result["attempts"]) == 2


def test_health_check_all_schemes_fail(env_creds):
    with patch.object(stc, "_try_request", return_value=(401, "Unauthorized")):
        result = stc.health_check()
    assert result["ok"] is False
    assert result["scheme"] is None
    assert len(result["attempts"]) == 3


def test_list_employees_returns_empty_when_unhealthy(env_creds):
    with patch.object(stc, "_try_request", return_value=(401, "nope")):
        result = stc.list_employees()
    assert result == []


def test_list_employees_unwraps_dict_response(env_creds):
    payload = '{"Employees": [{"id": 1}, {"id": 2}]}'
    with patch.object(stc, "_try_request", return_value=(200, payload)):
        result = stc.list_employees()
    assert result == [{"id": 1}, {"id": 2}]


def test_list_employees_handles_list_response(env_creds):
    payload = '[{"id": 1}, {"id": 2}]'
    with patch.object(stc, "_try_request", return_value=(200, payload)):
        result = stc.list_employees()
    assert result == [{"id": 1}, {"id": 2}]
```

## Step 3 — Add Integrations section to settings route

In `src/zira_dashboard/routes/settings.py`, find the validation in `settings_page`:

```python
    if section not in ("work_centers", "schedule"):
        section = "work_centers"
```

Change to include the new section:

```python
    if section not in ("work_centers", "schedule", "integrations"):
        section = "work_centers"
```

In the same handler, lazily call `health_check()` only when the user is on the integrations section (the call hits the network and shouldn't fire on every settings page load):

```python
    integration_status = None
    if section == "integrations":
        from .. import stratustime_client
        integration_status = stratustime_client.health_check()
```

Add `"integration_status": integration_status,` to the template context dict.

## Step 4 — Add Integrations section to settings template

In `src/zira_dashboard/templates/settings.html`, find the sidebar:

```jinja
  <aside class="settings-sidebar" aria-label="Settings sections">
    <a href="?section=work_centers" ...>Work Centers &amp; Goals</a>
    <a href="?section=schedule" ...>Company Schedule</a>
  </aside>
```

Add a third item:

```jinja
    <a href="?section=integrations"
       class="settings-nav-item {% if active_section == 'integrations' %}active{% endif %}">
      Integrations
    </a>
```

Inside the `.settings-content` div, AFTER the existing Company Schedule form and before its closing tag, add the Integrations section:

```jinja
  <!-- Integrations -->
  <section class="panel" id="integrations-panel"
           {% if active_section != 'integrations' %}style="display:none"{% endif %}>
    <h2>Integrations</h2>
    <h3 style="margin-top:0.8rem">StratusTime</h3>
    {% if integration_status is none %}
      <p style="color:var(--muted)">Loading…</p>
    {% elif not integration_status.configured %}
      <p style="color:var(--bad)">❌ Not configured. Set <code>STRATUSTIME_SHARED_KEY</code> and <code>STRATUSTIME_WS_PASSWORD</code> env vars on Railway.</p>
    {% elif integration_status.ok %}
      <p style="color:var(--good)">✓ Connected via <strong>{{ integration_status.scheme }}</strong> auth.</p>
      <p style="font-size:0.85rem;color:var(--muted)">Endpoint: <code>{{ integration_status.endpoint }}</code> · Status {{ integration_status.status }}</p>
      <details>
        <summary>Response preview</summary>
        <pre style="font-size:0.78rem;background:var(--panel-2);padding:0.6rem;border-radius:6px;overflow:auto">{{ integration_status.body_preview }}</pre>
      </details>
    {% else %}
      <p style="color:var(--bad)">❌ Auth failed. Tried {{ integration_status.attempts|length }} scheme{{ '' if integration_status.attempts|length == 1 else 's' }}.</p>
      <details open>
        <summary>Diagnostic trail</summary>
        <ul style="font-size:0.85rem">
          {% for a in integration_status.attempts %}
            <li><strong>{{ a.scheme }}</strong> — HTTP {{ a.status }}: <code>{{ a.body_preview }}</code></li>
          {% endfor %}
        </ul>
      </details>
    {% endif %}
  </section>
```

## Step 5 — Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('settings.html'); print('OK')"
```

Expected: 8 tests pass; Jinja prints `OK`.

## Step 6 — Commit (and push)

```bash
git add src/zira_dashboard/stratustime_client.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html tests/test_stratustime_client.py
git commit -m "Add StratusTime integration foundation (client + settings panel)"
git push origin main
```

## Step 7 — Dale's manual step

Set the two env vars on Railway:
- `STRATUSTIME_SHARED_KEY=a63844a1-d97b-4c00-a81c-38da0ba6841b`
- `STRATUSTIME_WS_PASSWORD=TimeTest7!`

After the redeploy, hit `/settings?section=integrations`. The page reports which auth scheme worked (or which all failed). That tells us how to talk to the API for sub-projects #2-4.

---

## Acceptance criteria

- `stratustime_client.health_check()` works without env vars (returns `configured=False`, no crash).
- 8 unit tests pass.
- `/settings?section=integrations` renders the new section.
- With env vars set, the page either shows ✓ + working scheme, or ❌ + a useful diagnostic trail listing each attempted scheme and its HTTP response.
- No regression to existing settings sections (Work Centers, Company Schedule).
- No real-network test calls in CI (mocks only).
