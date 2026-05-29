"""fetch_spanish_speaker_ids returns Odoo employee ids with a non-zero
Spanish (Languages) skill level."""
from __future__ import annotations

from unittest import mock

from zira_dashboard import odoo_client


def _fake_execute(model, method, *args, **kwargs):
    if model == "hr.skill":
        return [{"id": 7, "name": "Spanish"}]
    if model == "hr.employee.skill":
        return [
            {"employee_id": [11, "Ana"]},
            {"employee_id": [12, "Beto"]},
            {"employee_id": 13},
        ]
    raise AssertionError(f"unexpected call {model}.{method}")


def test_returns_employee_ids_with_spanish():
    with mock.patch.object(odoo_client, "execute", side_effect=_fake_execute):
        assert odoo_client.fetch_spanish_speaker_ids() == {11, 12, 13}


def test_no_spanish_skill_returns_empty_set():
    def no_skill(model, method, *args, **kwargs):
        if model == "hr.skill":
            return []
        raise AssertionError("should not query employee skills when no Spanish skill")
    with mock.patch.object(odoo_client, "execute", side_effect=no_skill):
        assert odoo_client.fetch_spanish_speaker_ids() == set()
