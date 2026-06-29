#!/usr/bin/env bash
#
# Migrate the GPI Plant Manager Postgres from the OLD instance (Railway EU West)
# to a NEW instance you create in a US region. Dump -> restore -> sanity-check.
#
# Run this from your Mac DURING the maintenance window, AFTER the `web` service
# is paused (so no warmers/kiosk writes are in flight and the dump is consistent).
#
# Prereqs:
#   brew install postgresql@16            # gives pg_dump/pg_restore/psql >= server major
#   OLD_URL = EU Postgres public URL  (railway variables --service Postgres --kv | grep DATABASE_PUBLIC_URL)
#   NEW_URL = the new US Postgres public URL (Variables tab of the service you create)
#
# Usage:
#   OLD_URL='postgresql://user:pass@mainline.proxy.rlwy.net:43228/railway' \
#   NEW_URL='postgresql://user:pass@<new-host>.proxy.rlwy.net:<port>/railway' \
#   ./scripts/migrate_db_region.sh
#
set -euo pipefail

OLD_URL="${OLD_URL:?set OLD_URL to the EU Postgres DATABASE_PUBLIC_URL}"
NEW_URL="${NEW_URL:?set NEW_URL to the new US Postgres DATABASE_PUBLIC_URL}"

DUMP="plant_db_$(date +%Y%m%d_%H%M%S).pgc"

echo "==> client tooling"
pg_dump --version

echo "==> source (OLD) server version + size"
psql "$OLD_URL" -tAc "select version();"
psql "$OLD_URL" -tAc "select pg_size_pretty(pg_database_size(current_database()));"

echo "==> dumping OLD -> $DUMP (custom format, schema+data, includes sequences)"
pg_dump --no-owner --no-acl --format=custom --verbose --file="$DUMP" "$OLD_URL"
ls -lh "$DUMP"

echo "==> restoring $DUMP -> NEW (drops/recreates objects if any pre-exist)"
pg_restore --no-owner --no-acl --verbose --clean --if-exists --dbname="$NEW_URL" "$DUMP"

echo "==> row-count sanity check (old vs new)"
for t in people time_off_requests production_daily inbox_events schedules attendance_cache; do
  o=$(psql "$OLD_URL" -tAc "select count(*) from $t" 2>/dev/null || echo "n/a")
  n=$(psql "$NEW_URL" -tAc "select count(*) from $t" 2>/dev/null || echo "n/a")
  printf "    %-22s old=%-8s new=%-8s\n" "$t" "$o" "$n"
done

echo "==> DONE. Keep $DUMP until the US cutover is verified, then delete it."
