"""Client for the StratusTime time-clock web services API.

Auth flow (confirmed by live probe):
  1. POST /CreateToken with {CustomerAlias, SharedKey, UserName, UserPass}
     → returns a base64 token (JSON-quoted string).
  2. POST /<Method> with {"AuthToken": <token>, ...method-specific fields...}.

Required env vars:
  STRATUSTIME_SHARED_KEY      — UUID from Inbound Services admin page
  STRATUSTIME_WS_PASSWORD     — wsuser password from same page
  STRATUSTIME_CUSTOMER_ALIAS  — tenant alias (e.g., "gruberpallets")
  STRATUSTIME_WS_USERNAME     — defaults to "wsuser"

Module-level token cache keeps the same token in memory across calls within
one process for TOKEN_TTL_SECONDS. Callers can force refresh.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE_URL = "https://stratustime.centralservers.com/service/ws-json/2.0"
TIMEOUT_SECONDS = 30
TOKEN_TTL_SECONDS = 60 * 30  # refresh well before any reasonable expiry


def _config() -> dict:
    return {
        "shared_key": os.environ.get("STRATUSTIME_SHARED_KEY"),
        "ws_password": os.environ.get("STRATUSTIME_WS_PASSWORD"),
        "customer_alias": os.environ.get("STRATUSTIME_CUSTOMER_ALIAS"),
        "ws_username": os.environ.get("STRATUSTIME_WS_USERNAME") or "wsuser",
    }


def _is_configured(cfg: dict) -> bool:
    return bool(cfg["shared_key"] and cfg["ws_password"] and cfg["customer_alias"])


# Module-level token cache: (token, expires_at_epoch_seconds).
_token_cache: tuple[str, float] | None = None
# Guards _create_token so concurrent callers (e.g., the parallel
# ThreadPoolExecutor on /staffing) don't all stampede CreateToken on a
# cold cache. See get_token() for the double-checked-locking pattern.
_token_lock = threading.Lock()


def _post(path: str, body: dict, timeout: int = TIMEOUT_SECONDS) -> tuple[int, str]:
    """Raw POST to a service endpoint. Returns (status, body_text)."""
    url = f"{BASE_URL}/{path.lstrip('/')}"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = str(e)
        return e.code, body_text
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"
    except Exception as e:
        return 0, f"error: {e}"


def ping() -> tuple[int, str]:
    """Unauthenticated ping. Returns (status, body)."""
    return _post("PingTest", {})


def _create_token() -> tuple[str | None, str]:
    """Request a fresh token. Returns (token, error_message)."""
    cfg = _config()
    if not _is_configured(cfg):
        return None, "Missing env vars (need SHARED_KEY, WS_PASSWORD, CUSTOMER_ALIAS)"
    body = {
        "CustomerAlias": cfg["customer_alias"],
        "CustomerAliasExternal": "",
        "SharedKey": cfg["shared_key"],
        "UserName": cfg["ws_username"],
        "UserPass": cfg["ws_password"],
    }
    status, resp = _post("CreateToken", body)
    if not (200 <= status < 300):
        return None, f"HTTP {status}: {resp[:200]}"
    try:
        token = json.loads(resp)
    except json.JSONDecodeError:
        return None, f"Invalid JSON token response: {resp[:200]}"
    if not isinstance(token, str) or not token:
        return None, f"Unexpected token shape: {repr(resp)[:200]}"
    return token, ""


def get_token(force_refresh: bool = False) -> tuple[str | None, str]:
    """Cached token getter. Returns (token, error_message).

    Uses double-checked-locking around `_create_token` so concurrent
    callers don't all fire CreateToken when the cache is cold/expired.
    """
    global _token_cache
    now = time.time()
    if not force_refresh and _token_cache is not None:
        token, expires_at = _token_cache
        if expires_at > now:
            return token, ""
    with _token_lock:
        # Double-check inside the lock — another thread may have
        # populated the cache while we were waiting.
        if not force_refresh and _token_cache is not None:
            token, expires_at = _token_cache
            if expires_at > time.time():
                return token, ""
        token, err = _create_token()
        if token:
            _token_cache = (token, time.time() + TOKEN_TTL_SECONDS)
        return token, err


def _now_wcf_date() -> str:
    """Current time formatted as Microsoft WCF date string: /Date(epoch_ms+0000)/."""
    ms = int(time.time() * 1000)
    return f"/Date({ms}+0000)/"


def authenticated_post(method: str, body: dict | None = None) -> tuple[int, dict | str]:
    """POST a method with an injected AuthToken. Returns (status, parsed_json_or_text)."""
    token, err = get_token()
    if not token:
        return 0, err or "No token"
    full_body = dict(body or {})
    full_body["AuthToken"] = token
    status, resp_text = _post(method, full_body)
    if 200 <= status < 300:
        try:
            return status, json.loads(resp_text)
        except json.JSONDecodeError:
            return status, resp_text
    return status, resp_text


def health_check() -> dict:
    """Verify connectivity + auth.

    Returns:
      {
        "ok": bool,                      # ping_ok AND token_ok
        "configured": bool,              # all three required env vars present
        "ping_ok": bool,                 # /PingTest returned 2xx
        "ping_status": int,
        "token_ok": bool,                # /CreateToken returned a token
        "token_error": str,              # only set when token_ok is False
        "endpoint": str,                 # base URL we used
      }
    """
    cfg = _config()
    if not _is_configured(cfg):
        missing = [
            n for n, v in [
                ("STRATUSTIME_SHARED_KEY", cfg["shared_key"]),
                ("STRATUSTIME_WS_PASSWORD", cfg["ws_password"]),
                ("STRATUSTIME_CUSTOMER_ALIAS", cfg["customer_alias"]),
            ] if not v
        ]
        return {
            "ok": False,
            "configured": False,
            "ping_ok": False,
            "ping_status": 0,
            "token_ok": False,
            "token_error": f"Set on Railway: {', '.join(missing)}.",
            "endpoint": BASE_URL,
        }
    ping_status, _ = ping()
    ping_ok = 200 <= ping_status < 300
    token, token_err = get_token(force_refresh=True)
    token_ok = token is not None
    return {
        "ok": ping_ok and token_ok,
        "configured": True,
        "ping_ok": ping_ok,
        "ping_status": ping_status,
        "token_ok": token_ok,
        "token_error": token_err if not token_ok else "",
        "endpoint": BASE_URL,
    }


def list_employees() -> list[dict]:
    """Smoke fetch via GetUserBasic (DataAction SELECT-ALL).

    Returns a list of employee dicts with keys like:
      Badge, Email, EmpIdentifier, FirstName, LastName, Phone1/2/3,
      Status, TimeZoneDisplayName, ...
    Returns [] on failure (caller should display health_check details first).
    """
    status, parsed = authenticated_post("GetUserBasic", {
        "EffectiveDate": _now_wcf_date(),
        "DataAction": {"Name": "SELECT-ALL", "Values": []},
    })
    if status < 200 or status >= 300:
        return []
    if isinstance(parsed, dict):
        results = parsed.get("Results")
        if isinstance(results, list):
            return results
    return []


# --- Time-off + employee directory caching ---

# (cache_key) -> (value, expires_at_epoch_seconds)
_data_cache: dict[tuple, tuple[object, float]] = {}
DATA_CACHE_TTL_SECONDS = 5 * 60


def _cache_get(key):
    entry = _data_cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at < time.time():
        return None
    return value


def _cache_set(key, value):
    _data_cache[key] = (value, time.time() + DATA_CACHE_TTL_SECONDS)


def _cache_get_with_ttl(key, ttl_seconds):
    """Like _cache_get but treats entries written via _cache_set_with_ttl correctly.

    The existing _cache_get already checks expiry against the stored expires_at,
    so this is just an alias for symmetry with _cache_set_with_ttl.
    """
    return _cache_get(key)


def _cache_set_with_ttl(key, value, ttl_seconds):
    """Set with custom TTL (overrides DATA_CACHE_TTL_SECONDS)."""
    _data_cache[key] = (value, time.time() + ttl_seconds)


def cache_clear() -> None:
    """Drop all cached data (token cache untouched)."""
    _data_cache.clear()


def _wcf_date(epoch_ms: int) -> str:
    return f"/Date({epoch_ms}+0000)/"


def _epoch_ms(d) -> int:
    """Convert a `datetime.date` to UTC epoch ms (midnight)."""
    from datetime import datetime, timezone
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _employee_id_to_name_map() -> dict[str, str]:
    """{ EmpIdentifier: 'FirstName LastName' } — cached 5 min."""
    cached = _cache_get(("emp_map",))
    if cached is not None:
        return cached
    out: dict[str, str] = {}
    for emp in list_employees():
        emp_id = emp.get("EmpIdentifier")
        first = (emp.get("FirstName") or "").strip()
        last = (emp.get("LastName") or "").strip()
        if emp_id and (first or last):
            out[str(emp_id)] = f"{first} {last}".strip()
    _cache_set(("emp_map",), out)
    return out


def name_to_emp_id_map() -> dict[str, str]:
    """Reverse of _employee_id_to_name_map: 'FirstName LastName' → 'EmpIdentifier'.

    Cached separately from emp_map but populated from the same source.
    """
    cached = _cache_get(("name_to_id_map",))
    if cached is not None:
        return cached
    forward = _employee_id_to_name_map()
    inverted = {name: emp_id for emp_id, name in forward.items()}
    _cache_set(("name_to_id_map",), inverted)
    return inverted


def get_time_off_requests(start_d, end_d) -> list[dict]:
    """Return raw time-off request dicts for [start_d, end_d] (inclusive).

    StratusTime caps each call at a 60-day window — caller passes ranges
    within that. Cached 5 minutes per (start, end).
    """
    key = ("time_off", start_d.isoformat(), end_d.isoformat())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    status, parsed = authenticated_post("GetUserTimeOffRequest", {
        "StartDate": _wcf_date(_epoch_ms(start_d)),
        "EndDate": _wcf_date(_epoch_ms(end_d)),
        "DateTimeSchema": 0,
        "IgnoreDeletedRequests": True,
        "IgnoreDetails": False,
        "DataAction": {"Name": "SELECT-ALL", "Values": []},
    })
    if status < 200 or status >= 300 or not isinstance(parsed, dict):
        return []
    results = parsed.get("Results")
    if not isinstance(results, list):
        return []
    _cache_set(key, results)
    return results


def _fmt_time_short(dt_str: str) -> str:
    """Format an ISO datetime string like '2026-04-29T09:00:00' as a short
    time-of-day: '9a', '9:30a', '12p', '1:15p'. Returns '' on parse failure.
    """
    from datetime import datetime as _dt
    try:
        dt = _dt.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return ""
    h, m = dt.hour, dt.minute
    period = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    if m == 0:
        return f"{h12}{period}"
    return f"{h12}:{m:02d}{period}"


def _fmt_time_range(start_str: str, end_str: str) -> str:
    """Compact time range. Drops am/pm from start when both share the same period.
    Examples: '9-10a', '11a-1p', '9:30-10:15a', '12-1p'.
    """
    s = _fmt_time_short(start_str)
    e = _fmt_time_short(end_str)
    if not s or not e:
        return ""
    if s[-1] == e[-1]:
        s = s[:-1]
    return f"{s}-{e}"


def _request_covers_day(req: dict, day) -> bool:
    """True if the time-off request `req` includes `day`.

    Uses StartDateTimeSchema/EndDateTimeSchema (ISO local strings).
    Honors `IncludeWeekends` — if False, skips Sat/Sun within the range.
    """
    from datetime import date as _date
    s_str = (req.get("StartDateTimeSchema") or "")[:10]  # YYYY-MM-DD
    e_str = (req.get("EndDateTimeSchema") or "")[:10]
    if not s_str or not e_str:
        return False
    try:
        s = _date.fromisoformat(s_str)
        e = _date.fromisoformat(e_str)
    except ValueError:
        return False
    if not (s <= day <= e):
        return False
    if not req.get("IncludeWeekends", False) and day.weekday() >= 5:
        return False
    return True


def time_off_entries_for_day(day) -> list[dict]:
    """Return list of {name, pay_type, hours, status_type, request_id} for `day`.

    Treats StatusType==1 as approved/active. Other StatusType values are
    skipped (likely pending/rejected). Names come from the employee map;
    unmapped EmpIdentifiers are surfaced as 'Unknown ({id})' so it's visible.
    """
    # Use a 7-day window centered on `day` — small request, hits cache often.
    from datetime import timedelta
    start_d = day - timedelta(days=3)
    end_d = day + timedelta(days=3)
    requests_ = get_time_off_requests(start_d, end_d)
    emp_map = _employee_id_to_name_map()
    out = []
    for r in requests_:
        if r.get("StatusType") != 1:
            continue
        if not _request_covers_day(r, day):
            continue
        emp_id = str(r.get("EmpIdentifier") or "")
        name = emp_map.get(emp_id) or f"Unknown ({emp_id})"
        secs = r.get("DurationPerDaySecs") or 0
        start_str = r.get("StartDateTimeSchema") or ""
        end_str = r.get("EndDateTimeSchema") or ""
        # Show a time range only when the request is single-day. Multi-day
        # requests (e.g., 3-day PTO) report their first-day start time and
        # last-day end time, which would be misleading on middle days.
        if start_str[:10] == end_str[:10] and start_str:
            time_range = _fmt_time_range(start_str, end_str)
        else:
            time_range = ""
        out.append({
            "name": name,
            "pay_type": r.get("PayTypeName") or "",
            "hours": round(secs / 3600.0, 1),
            "time_range": time_range,
            "status_type": r.get("StatusType"),
            "request_id": r.get("ID"),
        })
    return out


def time_off_names_for_day(day) -> list[str]:
    """Just the names — convenience for callers that only need a list of strings."""
    return [e["name"] for e in time_off_entries_for_day(day)]


def partial_off_intervals_for_day(day) -> dict[str, list]:
    """Return {name: [(start_utc, end_utc), ...]} of partial-off intervals on `day`.

    Only includes entries where:
    - StatusType == 1 (approved)
    - DurationPerDaySecs < 28800 (under 8h, i.e., partial)
    - StartDateTimeSchema and EndDateTimeSchema fall on the same day as `day`

    Returns datetime objects in UTC for overlap math against shift windows.
    Multi-day requests and full-day off entries are excluded.
    """
    from datetime import datetime as _dt, timedelta, timezone
    from . import shift_config

    # Reuse the same 7-day window the existing helper queries to share cache.
    start_d = day - timedelta(days=3)
    end_d = day + timedelta(days=3)
    requests_ = get_time_off_requests(start_d, end_d)
    emp_map = _employee_id_to_name_map()
    out: dict[str, list] = {}
    site_tz = shift_config.SITE_TZ
    for r in requests_:
        if r.get("StatusType") != 1:
            continue
        secs = r.get("DurationPerDaySecs") or 0
        if secs >= 28800:  # full-day; not partial
            continue
        s_str = r.get("StartDateTimeSchema") or ""
        e_str = r.get("EndDateTimeSchema") or ""
        if not s_str or not e_str:
            continue
        if s_str[:10] != e_str[:10] or s_str[:10] != day.isoformat():
            continue  # not on `day` or spans multiple days
        try:
            s_local = _dt.fromisoformat(s_str).replace(tzinfo=site_tz)
            e_local = _dt.fromisoformat(e_str).replace(tzinfo=site_tz)
        except (ValueError, TypeError):
            continue
        s_utc = s_local.astimezone(timezone.utc)
        e_utc = e_local.astimezone(timezone.utc)
        if e_utc <= s_utc:
            continue
        emp_id = str(r.get("EmpIdentifier") or "")
        name = emp_map.get(emp_id)
        if not name:
            continue
        out.setdefault(name, []).append((s_utc, e_utc))
    return out


# Public deep-link to StratusTime's time-off page (for "Manage in StratusTime ↗" links).
STRATUSTIME_TIME_OFF_URL = "https://stratustime.centralservers.com/"


# --- Attendance (sub-project #4) ---

ATTENDANCE_CACHE_TTL_SECONDS = 60  # punches move fast


def _parse_status_board_datetime(date_str: str):
    """Parse '05/01/2026 06:41 AM' into a SITE_TZ-aware datetime.

    Returns None on parse failure or empty input.
    """
    from datetime import datetime as _dt
    from . import shift_config
    if not date_str:
        return None
    try:
        dt = _dt.strptime(date_str, "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=shift_config.SITE_TZ)


def attendance_for_day(day, emp_ids, grace_minutes: int = 7) -> dict:
    """Return per-EmpIdentifier attendance status against `day`'s shift-start.

    Result shape:
      {
        emp_id: {
          "status": "on_time" | "late" | "clocked_out" | "no_punch" | "unknown",
          "clocked_in_at": "06:41 AM" | None,    # display string (site-local)
          "minutes_late": int,                    # 0 if on_time, positive if late
          "transaction_type": str,                # raw LastTransactionType
        },
        ...
      }

    Status values:
      - on_time: Clock In transaction on `day`, at or before shift_start + grace.
      - late: Clock In transaction on `day`, after shift_start + grace; minutes_late > 0.
      - clocked_out: most-recent transaction is a Clock Out today. Person worked today
        but is not currently on the clock (lunch, left for the day, etc.).
      - no_punch: no transaction record found for this emp on `day`.
      - unknown: data shape unexpected.

    Empty `emp_ids` returns {}. Cached 60s per (day, sorted_emp_ids tuple).
    """
    if not emp_ids:
        return {}
    from datetime import datetime as _dt, timedelta
    from . import shift_config

    cache_key = ("attendance", day.isoformat(), tuple(sorted(set(emp_ids))))
    cached = _cache_get_with_ttl(cache_key, ATTENDANCE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    status, parsed = authenticated_post("GetUserTimeOnStatusBoard", {
        "DataAction": {"Name": "SELECT-EMPID", "Values": list(emp_ids)},
    })
    out: dict = {}
    if status < 200 or status >= 300 or not isinstance(parsed, dict):
        _cache_set_with_ttl(cache_key, out, ATTENDANCE_CACHE_TTL_SECONDS)
        return out

    results = parsed.get("Results")
    seen_ids: set[str] = set()
    if isinstance(results, list):
        shift_start_dt = _dt.combine(
            day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ
        )
        on_time_cutoff = shift_start_dt + timedelta(minutes=grace_minutes)
        for r in results:
            emp_id = str(r.get("EmpIdentifier") or "")
            if not emp_id:
                continue
            seen_ids.add(emp_id)
            tx_type = r.get("LastTransactionType") or ""
            tx_dt = _parse_status_board_datetime(r.get("LastTransactionDate") or "")
            entry = {
                "status": "unknown",
                "clocked_in_at": None,
                "minutes_late": 0,
                "transaction_type": tx_type,
            }
            if tx_dt is not None and tx_dt.date() == day:
                hr_min = tx_dt.strftime("%I:%M %p").lstrip("0")
                if tx_type.lower().startswith("clock in"):
                    entry["clocked_in_at"] = hr_min
                    if tx_dt <= on_time_cutoff:
                        entry["status"] = "on_time"
                    else:
                        late = int((tx_dt - shift_start_dt).total_seconds() // 60)
                        entry["status"] = "late"
                        entry["minutes_late"] = max(0, late)
                elif tx_type.lower().startswith("clock out"):
                    entry["status"] = "clocked_out"
                    entry["clocked_in_at"] = hr_min
                else:
                    # Lunch, transfer, etc. — treat as on the clock today.
                    entry["status"] = "on_time"
                    entry["clocked_in_at"] = hr_min
            out[emp_id] = entry

    # Anyone we asked about who didn't appear in Results -> no_punch.
    for emp_id in emp_ids:
        sid = str(emp_id)
        if sid not in seen_ids and sid not in out:
            out[sid] = {
                "status": "no_punch",
                "clocked_in_at": None,
                "minutes_late": 0,
                "transaction_type": "",
            }

    _cache_set_with_ttl(cache_key, out, ATTENDANCE_CACHE_TTL_SECONDS)
    return out
