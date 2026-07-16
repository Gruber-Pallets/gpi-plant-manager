import os
from datetime import date

import pytest

from zira_dashboard import db, staffing


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_record_delivery_updates_only_matching_current_version():
    day = date(2099, 12, 30)
    db.execute("DELETE FROM schedules WHERE day = %s", (day,))
    try:
        staffing.save_schedule(staffing.Schedule(
            day=day, published=True, published_delivery={"version": "current"},
        ))

        delivery = staffing.record_delivery(
            day, "current", {"printed_at": "2099-12-30T12:00:00+00:00"},
        )

        assert delivery["version"] == "current"
        assert delivery["printed_at"] == "2099-12-30T12:00:00+00:00"
        assert staffing.record_delivery(day, "old", {"printed_at": "no"}) is None
    finally:
        db.execute("DELETE FROM schedules WHERE day = %s", (day,))
