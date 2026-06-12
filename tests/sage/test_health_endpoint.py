"""Liveness endpoint tests (HLT-001..003).

The SAGE Core API exposes an operational ``/health`` liveness probe for
container orchestrators. It is a constant, store-free response: it reports
process liveness and the running release version, and must answer 200 even
when no vault is registered and no durable store is reachable.
"""

from fastapi.testclient import TestClient

from sage.app import create_app
from sage.build_info import RELEASE_VERSION


def _client() -> TestClient:
    # No ``with`` block on purpose: the route is registered at app
    # construction, so the liveness probe is exercised without running the
    # vault-discovery lifespan. That is exactly the store-free guarantee
    # HLT-003 asserts -- the probe must not depend on any lifespan state.
    return TestClient(create_app())


def test_hlt_001_health_returns_ok_envelope() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    # Strict whole-body equality: a route returning a different shape, or a
    # different 200 route masking a missing /health, fails here.
    assert resp.json() == {"status": "ok", "version": RELEASE_VERSION}


def test_hlt_002_health_reports_build_info_version() -> None:
    body = _client().get("/health").json()
    # Equality with the imported constant (not a hardcoded literal) means a
    # version string baked into the handler by hand would diverge and fail.
    assert body["version"] == RELEASE_VERSION


def test_hlt_003_health_is_liveness_only_without_vaults_or_storage() -> None:
    # create_app() with no vault_root leaves the registry empty and never
    # opens a durable store; a handler that iterated the registry or touched
    # storage would error here instead of returning 200.
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
