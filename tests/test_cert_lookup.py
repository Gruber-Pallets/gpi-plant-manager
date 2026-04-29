import os
import pytest

from zira_dashboard import db, cert_lookup


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM person_skills WHERE person_id IN "
               "(SELECT id FROM people WHERE name LIKE 'TestCertPerson%')")
    db.execute("DELETE FROM people WHERE name LIKE 'TestCertPerson%'")
    db.execute("DELETE FROM skills WHERE name LIKE 'TestCert%'")
    yield
    db.execute("DELETE FROM person_skills WHERE person_id IN "
               "(SELECT id FROM people WHERE name LIKE 'TestCertPerson%')")
    db.execute("DELETE FROM people WHERE name LIKE 'TestCertPerson%'")
    db.execute("DELETE FROM skills WHERE name LIKE 'TestCert%'")


def _insert_person(name: str) -> int:
    rows = db.query(
        "INSERT INTO people (name, active) VALUES (%s, TRUE) RETURNING id",
        (name,),
    )
    return rows[0]["id"]


def _insert_skill(name: str, skill_type: str) -> int:
    rows = db.query(
        "INSERT INTO skills (name, skill_type) VALUES (%s, %s) RETURNING id",
        (name, skill_type),
    )
    return rows[0]["id"]


def _link(person_id: int, skill_id: int, level: int = 3) -> None:
    db.execute(
        "INSERT INTO person_skills (person_id, skill_id, level) "
        "VALUES (%s, %s, %s)",
        (person_id, skill_id, level),
    )


def test_load_person_certs_empty_returns_empty_dict():
    result = cert_lookup.load_person_certs()
    test_rows = {k: v for k, v in result.items() if k.startswith("TestCertPerson")}
    assert test_rows == {}


def test_load_person_certs_groups_certs_by_person():
    pid = _insert_person("TestCertPerson1")
    sid_a = _insert_skill("TestCertA", "Certifications")
    sid_b = _insert_skill("TestCertB", "Certifications")
    _link(pid, sid_a)
    _link(pid, sid_b)

    result = cert_lookup.load_person_certs()
    assert "TestCertPerson1" in result
    assert sorted(result["TestCertPerson1"]) == ["TestCertA", "TestCertB"]


def test_load_person_certs_excludes_non_certification_skill_types():
    pid = _insert_person("TestCertPerson2")
    sid_skill = _insert_skill("TestCertProdSkill", "Production Skills")
    sid_cert = _insert_skill("TestCertReal", "Certifications")
    _link(pid, sid_skill)
    _link(pid, sid_cert)

    result = cert_lookup.load_person_certs()
    assert result.get("TestCertPerson2") == ["TestCertReal"]


def test_load_person_certs_returns_alphabetical_within_person():
    pid = _insert_person("TestCertPerson3")
    sid_z = _insert_skill("TestCertZebra", "Certifications")
    sid_a = _insert_skill("TestCertAlpha", "Certifications")
    _link(pid, sid_z)
    _link(pid, sid_a)

    result = cert_lookup.load_person_certs()
    assert result["TestCertPerson3"] == ["TestCertAlpha", "TestCertZebra"]


def test_load_person_certs_ignores_level():
    pid = _insert_person("TestCertPerson4")
    sid = _insert_skill("TestCertLevelZero", "Certifications")
    _link(pid, sid, level=0)

    result = cert_lookup.load_person_certs()
    assert result.get("TestCertPerson4") == ["TestCertLevelZero"]
