"""The test suite must never run against a production (Railway) database.

DB-backed tests DELETE rows in setup/teardown: a full-suite run against prod
wiped the forklift tables on 2026-07-20, and on 2026-08-24 repeated local runs
kept flipping the Auto-Lunch mode Off in production (the repo-root ``.env``
carried the prod ``DATABASE_URL`` and ``load_dotenv()`` injected it into every
pytest run). ``conftest.forbidden_database_host`` is the predicate behind the
session-abort guard in ``conftest.py``.
"""

from tests._prod_db_guard import forbidden_database_host


def test_railway_public_proxy_is_forbidden():
    dsn = "postgresql://postgres@reseau.proxy.rlwy.net:19253/railway"
    assert forbidden_database_host(dsn) == "reseau.proxy.rlwy.net"


def test_railway_internal_host_is_forbidden():
    dsn = "postgresql://postgres@postgres-raop.railway.internal:5432/railway"
    assert forbidden_database_host(dsn) == "postgres-raop.railway.internal"


def test_railway_app_host_is_forbidden():
    dsn = "postgresql://u@something.up.railway.app:5432/db"
    assert forbidden_database_host(dsn) == "something.up.railway.app"


def test_keyword_value_dsn_with_railway_host_is_forbidden():
    dsn = "host=reseau.proxy.rlwy.net port=19253 dbname=railway user=postgres"
    assert forbidden_database_host(dsn) == "reseau.proxy.rlwy.net"


def test_localhost_is_allowed():
    assert forbidden_database_host(
        "postgresql://postgres@localhost:5432/gpi_test"
    ) is None


def test_loopback_ip_is_allowed():
    assert forbidden_database_host(
        "postgresql://postgres@127.0.0.1:5432/test"
    ) is None


def test_unix_socket_pgserver_dsn_is_allowed():
    # Embedded pgserver hands out socket-dir DSNs; the "host" is a path.
    assert forbidden_database_host(
        "postgresql://postgres@/gpi_test?host=/tmp/pgserver-sock"
    ) is None


def test_empty_and_missing_are_allowed():
    assert forbidden_database_host("") is None
    assert forbidden_database_host(None) is None


def test_unparseable_dsn_is_allowed():
    # Garbage can't reach prod; connecting will fail on its own.
    assert forbidden_database_host("not a dsn at all") is None
