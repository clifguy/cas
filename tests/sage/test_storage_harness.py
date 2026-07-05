"""Guards on the Postgres-only test-storage harness (CAS-ADR-042).

The suite runs every uninjected service construction against the stack
config pinned by the root conftest: the ``SAGE_TEST_PG_DSN`` server when
one is configured, an unresolvable sentinel host when not. These tests
assert the properties the rest of the suite silently depends on -- that
the pin is active (never the empty default, whose local-socket host is
a live database), that vault schemas do not leak between tests, and
that the sentinel fails fast instead of falling through.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sage.config import SageCoreConfig, StackPostgresConfig
from sage.mcp_init import get_stack_config
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.storage_binding import build_stack_storage_provisioner
from tests.conftest import SENTINEL_PG_HOST
from tests.sage.conftest import initialize_services_for_test


def _harness_doc() -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id="00000001_harness_doc",
        title="Harness isolation probe",
        source_type=SourceType.MARKDOWN,
        source_path="/x/harness.md",
        source_content_hash=f"sha256:{1:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
    )


def test_har_001_stack_config_pinned_to_test_dsn(pg_dsn):
    """The per-test stack config targets the test server, not the default.

    The empty ``SageCoreConfig`` default resolves ``postgres.host`` to None
    (the local unix socket -- a live database), so this equality failing
    means the autouse pin is missing or mis-ordered and the whole suite is
    pointed at real data.
    """
    from psycopg.conninfo import conninfo_to_dict

    parsed = conninfo_to_dict(pg_dsn)
    cfg = get_stack_config()
    assert cfg.storage_backend == "postgres"
    assert cfg.postgres.host == parsed.get("host")
    assert cfg.postgres.port == int(parsed.get("port") or 5432)
    assert cfg.postgres.database == str(parsed.get("dbname") or "postgres")


@pytest.mark.asyncio
async def test_har_002a_uninjected_services_write_lands(pg_dsn, minimal_config):
    """First half of the isolation pair: seed one row through uninjected
    services on the shared ``test_vault`` id and observe it."""
    async with initialize_services_for_test(minimal_config) as services:
        await services.graph_store.insert_document(_harness_doc())
        docs = await services.graph_store.list_all_documents()
        assert [d.id for d in docs] == ["00000001_harness_doc"]


@pytest.mark.asyncio
async def test_har_002b_next_test_sees_empty_vault(pg_dsn, minimal_config):
    """Second half of the pair: a fresh uninjected open of the same vault id
    starts empty. Fails if the schema-hygiene teardown is removed -- the
    ``test_vault`` schema would survive and this test would see 002a's row.
    Depends on 002a running first in file order (pytest default).
    """
    async with initialize_services_for_test(minimal_config) as services:
        docs = await services.graph_store.list_all_documents()
        assert docs == []


def test_har_004_lifespan_config_load_resolves_to_test_server(pg_dsn):
    """``load_stack_config_or_default()`` -- the lifespan path that OVERWRITES
    the module singleton -- must itself resolve to the test server.

    This is the guard for the leak the singleton pin alone cannot stop: an
    app or server booted inside a test re-loads the stack config from disk,
    and without the ``SAGE_CONFIG_PATH`` pin that load returns the committed
    ``sage/config.yaml``, whose postgres block is the developer's live
    local-socket database.
    """
    from psycopg.conninfo import conninfo_to_dict

    from sage.mcp_init import load_stack_config_or_default

    parsed = conninfo_to_dict(pg_dsn)
    cfg = load_stack_config_or_default()
    assert cfg.storage_backend == "postgres"
    assert cfg.postgres.host == parsed.get("host")
    assert cfg.postgres.database == str(parsed.get("dbname") or "postgres")


@pytest.mark.asyncio
async def test_har_005_default_config_cannot_reach_the_local_socket(tmp_path):
    """A default-constructed ``SageCoreConfig()`` (postgres.host None -> the
    local unix socket) must not be able to open storage from inside a test.

    The ``PGHOST`` sentinel pin is the libpq-level backstop: with no explicit
    host in the connection string, libpq consults ``PGHOST`` and resolves the
    unroutable sentinel instead of the socket. Removing that pin makes this
    test open the developer's live database -- which is exactly the failure
    being guarded, surfaced here as a fast, named error instead of a silent
    live-data write somewhere else in the suite.
    """
    import psycopg

    provisioner = build_stack_storage_provisioner(SageCoreConfig())
    # psycopg reports the unresolvable connection by its original host param
    # (None); the name that actually failed to resolve is the PGHOST sentinel.
    # Without the pin this raises nothing -- the connect succeeds against the
    # local socket -- so the raises-check alone catches the pin's removal.
    # The vault id is deliberately probe-named: in the failure mode (pin
    # removed, live socket reached) the collateral schema this open would
    # create is identifiable and disposable.
    with pytest.raises(psycopg.OperationalError, match="failed to resolve host"):
        await provisioner.open_vault_storage(
            "sage_test_har_005_probe", tmp_path, need_graph=True, need_content=False
        )


@pytest.mark.asyncio
async def test_har_003_sentinel_fails_fast_and_names_the_fix(tmp_path):
    """The DSN-unset sentinel config cannot silently reach a live server.

    Built directly (session state is left alone) exactly as the root
    conftest builds it when ``SAGE_TEST_PG_DSN`` is unset. The connect must
    fail -- resolution of the reserved ``.invalid`` name cannot succeed --
    and the error must carry the sentinel host so the failure reads as
    "configure SAGE_TEST_PG_DSN", not as a storage bug.
    """
    sentinel = SageCoreConfig(
        storage_backend="postgres",
        postgres=StackPostgresConfig(host=SENTINEL_PG_HOST),
    )
    provisioner = build_stack_storage_provisioner(sentinel)
    with pytest.raises(Exception, match="sage-test-pg-dsn-unset"):
        await provisioner.open_vault_storage(
            "test_vault", tmp_path, need_graph=True, need_content=False
        )
