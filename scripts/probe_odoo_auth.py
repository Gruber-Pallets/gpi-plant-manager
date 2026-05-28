#!/usr/bin/env python3
"""Diagnostic probe for Odoo XML-RPC auth + hr.leave.type read perms.

Independent of the app — talks directly to Odoo via xmlrpc.client so the
output reflects exactly what the API user sees, with no caching or
fallback in between. Use this when the Settings -> Time Off panel shows
an Odoo error banner to isolate which step is failing.

Run from the project root:
    python -m scripts.probe_odoo_auth

Reads the same four env vars that ``zira_dashboard.odoo_client`` uses:
    ODOO_URL  ODOO_DB  ODOO_LOGIN  ODOO_API_KEY

Loads ``.env`` from the project root (same as the app) so local runs work
without manually exporting the vars.

Output:
    1) Env vars present (values redacted).
    2) ``common.authenticate(...)`` result (uid or False).
    3) If authenticated, ``hr.leave.type.search_read(...)`` count + first
       few records.
    4) A warning if ``requires_allocation`` comes back as a bool instead
       of a string (Odoo version differences).
    5) Final status: OK / auth failed / leave-type read failed.
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load .env from the project root so a local run picks up the same
# values the app sees, matching the pattern in deps.py / zira_probe.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
except ImportError:
    # python-dotenv isn't a hard dep for scripts; if it's not installed,
    # fall back to whatever's already in os.environ.
    pass


_REQUIRED_ENV = ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY")


def _load_env() -> tuple[str, str, str, str]:
    """Return (url, db, login, key); raise with a clear message if any
    are missing. Strips the URL of a trailing slash to match the
    odoo_client._config() shape."""
    values = {k: (os.environ.get(k) or "").strip() for k in _REQUIRED_ENV}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them in .env or your shell before running this probe."
        )
    return (
        values["ODOO_URL"].rstrip("/"),
        values["ODOO_DB"],
        values["ODOO_LOGIN"],
        values["ODOO_API_KEY"],
    )


def _redact(s: str, keep: int = 4) -> str:
    """Show only the last N chars; mask the rest. Empty string stays
    empty so the env-var-missing path renders cleanly."""
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def main() -> int:
    print("=== Odoo auth + hr.leave.type probe ===\n")

    # 1) Env vars.
    try:
        url, db, login, key = _load_env()
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1
    print("Env vars:")
    print(f"  ODOO_URL     = {url}")
    print(f"  ODOO_DB      = {db}")
    print(f"  ODOO_LOGIN   = {login}")
    print(f"  ODOO_API_KEY = {_redact(key)}  (redacted)")
    print()

    # 2) Authenticate.
    print(f"Calling common.authenticate(db, login, key, {{}}) at "
          f"{url}/xmlrpc/2/common ...")
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, login, key, {})
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: XML-RPC transport error during authenticate: "
              f"{type(e).__name__}: {e}")
        print("\nFinal status: FAILED (transport)")
        return 2
    if not uid:
        # Odoo returns False (not an exception) when the credentials
        # are rejected — same path OdooAuthError flags in the app.
        print(f"  -> result: {uid!r}  (Odoo rejected credentials)")
        print("\nFinal status: FAILED (auth)")
        print(
            "Hint: verify ODOO_API_KEY matches a current, non-revoked "
            "key for ODOO_LOGIN, and that ODOO_DB names the right "
            "database."
        )
        return 3
    print(f"  -> uid = {uid}  (authenticated OK)\n")

    # 3) hr.leave.type read.
    print(
        "Calling models.execute_kw(db, uid, key, 'hr.leave.type', "
        "'search_read', [[('active','=',True)]], "
        "{'fields': ['id','name','request_unit','requires_allocation','active']}) ..."
    )
    try:
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        rows = models.execute_kw(
            db, uid, key,
            "hr.leave.type", "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name", "request_unit",
                        "requires_allocation", "active"]},
        )
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        print("\nFinal status: FAILED (hr.leave.type read)")
        print(
            "Hint: most likely a permission error. Give the API user "
            "the Time Off -> Officer (or User) group in Odoo."
        )
        return 4

    count = len(rows)
    print(f"  -> got {count} active leave type(s)")
    if count == 0:
        print(
            "  (No active leave types found — auth + read perms OK, "
            "but Odoo has no active hr.leave.type records. Configure "
            "leave types in Odoo: Time Off -> Configuration -> Time "
            "Off Types.)"
        )

    # 4) requires_allocation type check + sample rows.
    bool_warned = False
    for r in rows[:5]:
        ra = r.get("requires_allocation")
        if isinstance(ra, bool) and not bool_warned:
            print(
                "\nWARN: requires_allocation came back as a bool "
                f"({ra!r}) — this Odoo version returns a bool instead "
                "of a 'yes'/'no' string. The app expects a string; "
                "downstream code may need to coerce."
            )
            bool_warned = True
    if rows:
        print("\nFirst few records:")
        for r in rows[:5]:
            print(
                f"  id={r.get('id')}  "
                f"name={r.get('name')!r}  "
                f"request_unit={r.get('request_unit')!r}  "
                f"requires_allocation={r.get('requires_allocation')!r}  "
                f"active={r.get('active')!r}"
            )

    # 5) Final status.
    print("\nFinal status: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
