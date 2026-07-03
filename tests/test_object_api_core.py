import pytest

from zira_dashboard import object_api


FIELDS = {
    "id": object_api.FieldSpec("integer", "ID", readonly=True),
    "name": object_api.FieldSpec("char", "Name"),
    "active": object_api.FieldSpec("boolean", "Active"),
    "score": object_api.FieldSpec("float", "Score"),
}


def test_apply_domain_supports_implicit_and_and_ilike():
    rows = [
        {"id": 1, "name": "Dale", "active": True, "score": 10},
        {"id": 2, "name": "Ian", "active": True, "score": 7},
        {"id": 3, "name": "Ada", "active": False, "score": 9},
    ]
    out = object_api.apply_domain(
        rows,
        [["active", "=", True], ["name", "ilike", "a"]],
        FIELDS,
    )
    assert [r["id"] for r in out] == [1, 2]


def test_apply_domain_rejects_unknown_field_and_operator():
    with pytest.raises(object_api.ObjectAPIError) as e:
        object_api.apply_domain([{"id": 1}], [["secret", "=", 1]], FIELDS)
    assert e.value.code == "invalid_field"
    with pytest.raises(object_api.ObjectAPIError) as e:
        object_api.apply_domain([{"id": 1}], [["id", "like_regex", ".*"]], FIELDS)
    assert e.value.code == "invalid_domain"


def test_select_fields_rejects_private_fields():
    with pytest.raises(object_api.ObjectAPIError) as e:
        object_api.select_fields([{"id": 1}], ["id", "secret"], FIELDS)
    assert e.value.code == "invalid_field"


def test_apply_order_rejects_unknown_field():
    with pytest.raises(object_api.ObjectAPIError) as e:
        object_api.apply_order([{"id": 1}], "secret desc", FIELDS)
    assert e.value.code == "invalid_field"


class DemoModel(object_api.ObjectModel):
    name = "demo.model"
    display_name = "Demo"
    fields = FIELDS
    writable_fields = {"name"}

    def _records(self):
        return [{"id": 1, "name": "Dale", "active": True, "score": 10}]

    def all_records(self, context):
        return list(self._records())

    def write_records(self, ids, values, context):
        return True


def _registry():
    reg = object_api.Registry()
    reg.register(DemoModel())
    return reg


def test_execute_search_read_returns_ok_result():
    payload = {
        "model": "demo.model",
        "method": "search_read",
        "args": [[["active", "=", True]]],
        "kwargs": {"fields": ["id", "name"]},
    }
    body, status = object_api.execute(
        _registry(), {"scopes": ["object:read"], "name": "Test"}, payload
    )
    assert status == 200
    assert body == {"ok": True, "result": [{"id": 1, "name": "Dale"}]}


def test_execute_write_requires_write_scope():
    payload = {"model": "demo.model", "method": "write", "args": [[1], {"name": "New"}]}
    body, status = object_api.execute(
        _registry(), {"scopes": ["object:read"], "name": "Test"}, payload
    )
    assert status == 403
    assert body["ok"] is False
    assert body["error"]["code"] == "access_denied"
