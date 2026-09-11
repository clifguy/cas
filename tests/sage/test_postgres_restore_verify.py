"""The restore verifier is read-only, refuses the serving server, and discriminates.

The boundary fake carries catalog state; the refusal ordering, the check set and
the report assembly are the real implementation. One test opens a real disposable
database, because only an actual write attempt proves the session is read-only.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from sage.maintenance.postgres_restore_verify import (
    READ_ONLY_OPTIONS,
    REPORT_PREFIX,
    RestoreVerifyStore,
    read_only_conninfo,
    run,
    settings_from_environ,
)
from tests.helpers.pg_isolation import (
    create_database,
    derive_throwaway_dbname,
    drop_database,
    rewrite_dsn_dbname,
)

SERVING = "psql-serving.postgres.database.azure.com"
RESTORED = "psql-restored.postgres.database.azure.com"
BEFORE_ID = "aaaa1111_sentinel_before"
AFTER_ID = "bbbb2222_sentinel_after"
TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.restore-drill-token"

# Each sentinel body quotes the *other* sentinel's id. A presence check that
# scanned document text rather than looking the row up by key would match both
# ids in either row and report the wrong recovery point.
SENTINEL_BODIES = {
    BEFORE_ID: f"written before the recovery point; its successor is {AFTER_ID}",
    AFTER_ID: f"written after the recovery point; its predecessor is {BEFORE_ID}",
}

SNAPSHOT: dict[str, Any] = {
    "schemas": [
        ["ticket_punch", "id-sage-prod", None],
        ["cas_bff", "id-cas-bff-prod", None],
    ],
    "tables": {
        "ticket_punch.documents": {"count": 1522, "hash": "documents-hash"},
        "ticket_punch.edges": {"count": 311, "hash": "edges-hash"},
        "cas_bff.sessions": {"count": 4, "hash": "sessions-hash"},
    },
    "sequences": {},
}

ROLE_REPORT = {
    "id-sage-prod": {
        "exists": True,
        "database_privileges": ["CONNECT", "CREATE"],
        "pgstattuple_execute": True,
    },
    "id-cas-bff-prod": {
        "exists": True,
        "database_privileges": ["CONNECT", "CREATE"],
        "pgstattuple_execute": True,
    },
}

EXTENSIONS = [("pgstattuple", "1.5", "public"), ("vector", "0.8.0", "public")]


def environ(**overrides: str) -> dict[str, str]:
    base = {
        "PG_FQDN": RESTORED,
        "PG_SERVING_FQDN": SERVING,
        "PG_DATABASE": "sage",
        "PG_ADMIN_USER": "id-pg-bootstrap-prod",
        "SAGE_DB_ROLE": "id-sage-prod",
        "BFF_DB_ROLE": "id-cas-bff-prod",
        "PG_EXPECTED_MAJOR": "17",
        "PG_RESTORE_POINT": "2026-09-11T02:30:00+00:00",
        "PG_SENTINEL_SCHEMA": "cloud_validation",
        "PG_SENTINEL_BEFORE_ID": BEFORE_ID,
        "PG_SENTINEL_AFTER_ID": AFTER_ID,
    }
    base.update(overrides)
    return base


class Store:
    """Boundary fake: catalog reads are canned, every check above them is real."""

    def __init__(self) -> None:
        self.identity = "10.20.2.9:5432/sage"
        self.major = 17
        # The real store carries the access token and the connection string on
        # the instance, so the fake carries them too: that is the leak vector a
        # sanitization claim has to survive.
        self.password = TOKEN
        self.conninfo = f"host={RESTORED} password={TOKEN}"
        self.snapshot_state = deepcopy(SNAPSHOT)
        self.roles_state = deepcopy(ROLE_REPORT)
        self.extensions_state = list(EXTENSIONS)
        self.present = {BEFORE_ID: True, AFTER_ID: False}
        self.missing_schema = False
        self.closed = False

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.snapshot_state)

    def extensions(self) -> list[tuple[str, str, str]]:
        return list(self.extensions_state)

    def role_report(self, roles: tuple[str, ...]) -> dict[str, Any]:
        return {role: deepcopy(self.roles_state.get(role, {"exists": False})) for role in roles}

    def document_present(self, schema: str, document_id: str) -> bool:
        if self.missing_schema:
            raise psycopg.errors.UndefinedTable(f"schema {schema} does not exist")
        return self.present[document_id]

    def close(self) -> None:
        self.closed = True


class Factory:
    """Records every construction attempt so 'never connected' is assertable."""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Store:
        self.calls.append((args, kwargs))
        return self.store


# --- 1. refuses the serving address, before opening anything ------------------


def test_refuses_the_serving_address_without_connecting() -> None:
    factory = Factory()
    with pytest.raises(ValueError, match="serving"):
        run(environ(PG_FQDN=SERVING), factory)
    # The refusal must precede the connection, not follow it: a verifier that
    # connects first has already reached production before it checks.
    assert factory.calls == []


def test_refuses_a_serving_address_differing_only_in_case() -> None:
    factory = Factory()
    with pytest.raises(ValueError, match="serving"):
        run(environ(PG_FQDN=SERVING.upper()), factory)
    assert factory.calls == []


def test_requires_the_serving_address_to_be_supplied() -> None:
    settings = environ()
    del settings["PG_SERVING_FQDN"]
    factory = Factory()
    with pytest.raises(ValueError, match="PG_SERVING_FQDN"):
        run(settings, factory)
    assert factory.calls == []


# --- 2. the session is read-only from the first statement ---------------------


def test_connection_is_read_only_by_connection_option() -> None:
    factory = Factory()
    run(environ(), factory)
    (conninfo, _roles), _kwargs = factory.calls[0]
    fields = conninfo_to_dict(conninfo)
    # By value, not by containment: a session-level SET issued later can be
    # undone, and a second connection would never carry it at all.
    assert fields["options"] == READ_ONLY_OPTIONS
    assert fields["host"] == RESTORED
    assert fields["dbname"] == "sage"
    assert fields["user"] == "id-pg-bootstrap-prod"


def test_store_is_closed_even_when_a_check_fails() -> None:
    store = Store()
    store.major = 16
    factory = Factory(store)
    run(environ(), factory)
    assert store.closed is True


# --- 4. report shape ----------------------------------------------------------


def test_report_carries_every_contracted_field() -> None:
    code, report = run(environ(), Factory())
    assert code == 0
    assert report["status"] == "verified"
    assert report["server"] == "10.20.2.9:5432/sage"
    assert report["server_major"] == 17
    assert report["expected_major"] == 17
    assert report["restore_point"] == "2026-09-11T02:30:00+00:00"
    assert report["tables"]["ticket_punch.documents"] == {
        "count": 1522,
        "hash": "documents-hash",
    }
    assert report["table_count"] == 3
    assert report["row_total"] == 1522 + 311 + 4
    assert report["roles"]["id-sage-prod"]["exists"] is True
    assert report["extensions"] == [list(row) for row in EXTENSIONS]
    assert report["sentinels"]["before"] == {"id": BEFORE_ID, "present": True}
    assert report["sentinels"]["after"] == {"id": AFTER_ID, "present": False}
    assert report["failures"] == []
    assert isinstance(report["snapshot_fingerprint"], str)
    assert len(report["snapshot_fingerprint"]) == 64
    assert report["elapsed_seconds"] >= 0


def test_snapshot_fingerprint_moves_with_the_snapshot() -> None:
    _code, baseline = run(environ(), Factory())
    store = Store()
    store.snapshot_state["tables"]["ticket_punch.edges"]["count"] = 312
    _code, changed = run(environ(), Factory(store))
    assert changed["snapshot_fingerprint"] != baseline["snapshot_fingerprint"]


# --- 5 to 7. sentinel discrimination -----------------------------------------


def test_both_sentinels_correct_is_verified() -> None:
    code, report = run(environ(), Factory())
    assert (code, report["status"]) == (0, "verified")


def test_after_sentinel_present_is_an_overshoot() -> None:
    store = Store()
    store.present[AFTER_ID] = True
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert report["status"] == "recovery_point_overshoot"
    assert report["sentinels"]["after"]["present"] is True


def test_before_sentinel_absent_is_an_undershoot() -> None:
    store = Store()
    store.present[BEFORE_ID] = False
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert report["status"] == "recovery_point_undershoot"


def test_undershoot_outranks_overshoot_when_both_hold() -> None:
    store = Store()
    store.present = {BEFORE_ID: False, AFTER_ID: True}
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert report["status"] == "recovery_point_undershoot"


def test_sentinel_lookup_is_by_key_not_by_body_text() -> None:
    # Each fixture body quotes the other sentinel's id. The presence answer must
    # come from the row key, so a body-scanning implementation cannot agree.
    assert AFTER_ID in SENTINEL_BODIES[BEFORE_ID]
    assert BEFORE_ID in SENTINEL_BODIES[AFTER_ID]
    store = Store()
    store.present = {BEFORE_ID: True, AFTER_ID: False}
    _code, report = run(environ(), Factory(store))
    assert report["sentinels"]["after"]["present"] is False


def test_a_missing_sentinel_schema_fails_rather_than_reading_as_absent() -> None:
    store = Store()
    store.missing_schema = True
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert report["status"] == "failed"
    assert any("cloud_validation" in failure for failure in report["failures"])


# --- 8 and 9. roles and their grants -----------------------------------------


def test_a_missing_workload_role_fails_and_names_it() -> None:
    store = Store()
    del store.roles_state["id-cas-bff-prod"]
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert report["status"] == "failed"
    assert any("id-cas-bff-prod" in failure for failure in report["failures"])


@pytest.mark.parametrize("privilege", ["CONNECT", "CREATE"])
def test_a_stripped_database_grant_fails(privilege: str) -> None:
    store = Store()
    remaining = [
        value
        for value in store.roles_state["id-sage-prod"]["database_privileges"]
        if value != privilege
    ]
    store.roles_state["id-sage-prod"]["database_privileges"] = remaining
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any(privilege in failure for failure in report["failures"])


def test_a_stripped_function_grant_fails() -> None:
    store = Store()
    store.roles_state["id-sage-prod"]["pgstattuple_execute"] = False
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any("pgstattuple" in failure for failure in report["failures"])


def test_the_sage_role_must_own_at_least_one_workload_schema() -> None:
    store = Store()
    store.snapshot_state["schemas"] = [["ticket_punch", "someone-else", None]]
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any("owns no workload schema" in failure for failure in report["failures"])


# --- 10 and 11. extensions and major -----------------------------------------


def test_a_missing_required_extension_fails() -> None:
    store = Store()
    store.extensions_state = [("vector", "0.8.0", "public")]
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any("pgstattuple" in failure for failure in report["failures"])


def test_a_major_mismatch_fails() -> None:
    store = Store()
    store.major = 16
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any("major" in failure for failure in report["failures"])


def test_an_empty_restored_database_fails_rather_than_verifying() -> None:
    store = Store()
    store.snapshot_state["tables"] = {}
    code, report = run(environ(), Factory(store))
    assert code != 0
    assert any("no workload tables" in failure for failure in report["failures"])


# --- 12. the report is sanitized ---------------------------------------------


def test_no_secret_reaches_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    from sage.maintenance import postgres_restore_verify as module

    factory = Factory()
    _code, report = run({**environ(), "PG_UNEXPECTED_CARRIER": TOKEN}, factory)
    module.emit(report)
    line = capsys.readouterr().out
    assert line.startswith(REPORT_PREFIX)
    # Assert on the value, not on a key name: sanitization keyed on a field
    # label would pass here while the value still travelled. The token reaches
    # this report by three routes if anything leaks -- the store's password,
    # its connection string, and an unrelated environment entry.
    assert TOKEN not in line
    assert "password" not in line
    assert json.loads(line[len(REPORT_PREFIX) :])["status"] == "verified"


def test_settings_never_capture_the_whole_environment() -> None:
    settings = settings_from_environ({**environ(), "PG_UNEXPECTED_CARRIER": "secret"})
    assert "secret" not in repr(settings)


# --- 3. the read-only session is real ----------------------------------------


@pytest.fixture
def disposable_store() -> Iterator[RestoreVerifyStore]:
    maintenance = os.environ.get("SAGE_TEST_PG_DSN")
    if not maintenance:
        pytest.skip("requires SAGE_TEST_PG_DSN naming a maintenance database")
    name = derive_throwaway_dbname()
    create_database(maintenance, name)
    target = rewrite_dsn_dbname(maintenance, name)
    # Take the read-only option from the production conninfo builder rather than
    # restating it, so dropping it there breaks the real write probe too.
    fields = conninfo_to_dict(target)
    fields["options"] = conninfo_to_dict(read_only_conninfo(settings_from_environ(environ())))[
        "options"
    ]
    role = "restore-verify-" + uuid.uuid4().hex[:12]
    try:
        with psycopg.connect(target, autocommit=True) as setup:
            setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier("vault_probe")))
            setup.execute(
                sql.SQL("CREATE TABLE {} (id text PRIMARY KEY, body text)").format(
                    sql.Identifier("vault_probe", "documents")
                )
            )
            # Only the before-sentinel row exists, and its body quotes the
            # after-sentinel's id. Any implementation that answers presence by
            # scanning text rather than by key reports the after-sentinel
            # present, which is exactly the wrong recovery-point verdict.
            setup.execute(
                sql.SQL("INSERT INTO {} VALUES (%s, %s)").format(
                    sql.Identifier("vault_probe", "documents")
                ),
                (BEFORE_ID, SENTINEL_BODIES[BEFORE_ID]),
            )
        store = RestoreVerifyStore(psycopg.conninfo.make_conninfo(**fields), (role,))
        try:
            yield store
        finally:
            store.close()
    finally:
        drop_database(maintenance, name)


def test_the_session_actually_refuses_a_write(disposable_store: RestoreVerifyStore) -> None:
    # The mutation probe for the connection-option test above. A module that
    # set the default and later cleared it would pass that test and fail here.
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        disposable_store.conn.execute(
            sql.SQL("INSERT INTO {} VALUES ('written-by-the-verifier')").format(
                sql.Identifier("vault_probe", "documents")
            )
        )


def test_presence_is_answered_from_the_row_key(disposable_store: RestoreVerifyStore) -> None:
    assert disposable_store.document_present("vault_probe", BEFORE_ID) is True
    assert disposable_store.document_present("vault_probe", AFTER_ID) is False


def test_presence_on_an_absent_schema_raises(disposable_store: RestoreVerifyStore) -> None:
    with pytest.raises(psycopg.Error):
        disposable_store.document_present("vault_absent", BEFORE_ID)


def test_a_missing_extension_is_reported_not_raised(
    disposable_store: RestoreVerifyStore,
) -> None:
    # The disposable database has no pgstattuple, so asking whether a role may
    # execute it is a question Postgres refuses. The lost extension is precisely
    # what a restore drill exists to catch, so it must surface as a finding
    # rather than aborting the run before any finding is assembled.
    who = disposable_store.conn.execute("SELECT current_user").fetchone()[0]
    report = disposable_store.role_report((who,))
    assert report[who]["exists"] is True
    assert report[who]["pgstattuple_execute"] is False
    # The session must still be usable afterwards: a failed statement that left
    # the transaction aborted would poison every later read.
    assert disposable_store.document_present("vault_probe", BEFORE_ID) is True
