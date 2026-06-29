#!/usr/bin/env python3
"""Copy ALL data from the OLD Postgres to the NEW Postgres using psycopg2.

Used when pg_dump/pg_restore aren't available locally. The NEW database must
already have the schema (run zira_dashboard.db.bootstrap_schema against it
first — see the runbook). The OLD database is opened read-only and is NEVER
modified, so a failed/partial run is always safe to retry or abandon (just
point `web` back at OLD).

Approach (version-safe, FK-order-safe):
  * COPY ... TO/FROM STDOUT in Postgres TEXT format (portable, round-trips NULL
    as \\N) with explicit column lists taken from the OLD table's column order.
  * session_replication_role = replica on the target during load so FK triggers
    don't force a dependency order.
  * TRUNCATE every target table once up front so re-runs are idempotent.
  * setval() every owned sequence to MAX(col) after load so IDs keep climbing.
  * Verify row counts OLD vs NEW at the end; non-zero exit on any mismatch.

Usage:
  OLD_URL='postgresql://...@mainline.proxy.rlwy.net:43228/railway' \\
  NEW_URL='postgresql://...@<new-host>.proxy.rlwy.net:<port>/railway' \\
  .venv/bin/python scripts/migrate_db_region.py
"""
from __future__ import annotations

import io
import os
import sys

import psycopg2

OLD = os.environ.get("OLD_URL")
NEW = os.environ.get("NEW_URL")
if not OLD or not NEW:
    sys.exit("set OLD_URL and NEW_URL")


def _base_tables(conn) -> list[str]:
    with conn.cursor() as c:
        c.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        return [r[0] for r in c.fetchall()]


def _columns(conn, table: str) -> list[str]:
    with conn.cursor() as c:
        c.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in c.fetchall()]


def main() -> int:
    old = psycopg2.connect(OLD)
    old.set_session(readonly=True, autocommit=True)
    new = psycopg2.connect(NEW)

    old_tables = _base_tables(old)
    new_tables = set(_base_tables(new))
    missing = [t for t in old_tables if t not in new_tables]
    if missing:
        print("ERROR: NEW db is missing tables — bootstrap the schema first:")
        print("  ", ", ".join(missing))
        return 1

    ncur = new.cursor()
    # Disable FK/triggers during the load so table order doesn't matter.
    ncur.execute("SET session_replication_role = replica;")
    # Idempotent: clear every target table before loading.
    joined = ", ".join('"%s"' % t for t in old_tables)
    ncur.execute(f"TRUNCATE {joined} CASCADE;")

    src_counts: dict[str, int] = {}
    for t in old_tables:
        cols = _columns(old, t)
        collist = ", ".join('"%s"' % c for c in cols)
        buf = io.StringIO()
        with old.cursor() as oc:
            oc.copy_expert(f'COPY "{t}" ({collist}) TO STDOUT', buf)
            oc.execute(f'SELECT count(*) FROM "{t}"')
            src_counts[t] = oc.fetchone()[0]
        buf.seek(0)
        ncur.copy_expert(f'COPY "{t}" ({collist}) FROM STDIN', buf)
        print(f"  copied {t}: {src_counts[t]} rows")

    ncur.execute("SET session_replication_role = origin;")
    new.commit()

    # Reset every sequence owned by a column to MAX(col). Query the TARGET's
    # own sequences, NOT the source's: a table that was renamed in the app's
    # history can leave the source with a legacy sequence name (e.g.
    # kiosk_punches_log_id_seq after the table became timeclock_punches_log)
    # that doesn't exist on a freshly-bootstrapped target.
    with new.cursor() as sc:
        sc.execute(
            """
            SELECT s.relname AS seq, t.relname AS tbl, a.attname AS col
            FROM pg_class s
            JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
            JOIN pg_class t ON t.oid = d.refobjid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
            WHERE s.relkind = 'S'
            """
        )
        seqs = sc.fetchall()
    for seq, tbl, col in seqs:
        ncur.execute(
            f'SELECT setval(%s, COALESCE((SELECT MAX("{col}") FROM "{tbl}"), 1), true)',
            (seq,),
        )
    new.commit()
    print(f"reset {len(seqs)} sequences")

    print("=== verify (old vs new) ===")
    mismatches = 0
    for t in old_tables:
        with new.cursor() as nc:
            nc.execute(f'SELECT count(*) FROM "{t}"')
            n = nc.fetchone()[0]
        ok = n == src_counts[t]
        mismatches += 0 if ok else 1
        print(f"  {t}: old={src_counts[t]} new={n}{'' if ok else '  <-- MISMATCH'}")

    if mismatches:
        print(f"DONE WITH {mismatches} MISMATCH(es) — do NOT cut over.")
        return 2
    print("DONE — all row counts match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
