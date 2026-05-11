"""OpenAPI spec conformance tests.

Enforces architectural principle 8 (CLAUDE.md): Pydantic models derived
from schemas. The OpenAPI specs in docs/fs/ are the source of truth for
the HTTP API. Pydantic models in sage/models/schemas.py mirror them.

Two specs are validated:
- docs/fs/sage/sage_core_api.openapi.yaml -- SAGE Core API surface
  (/sage_vaults/* paths).
- docs/fs/cas_app_api.openapi.yaml -- CAS Application backend surface
  (/app/* paths).

These tests catch drift in either direction:
- Code-ahead drift: a FastAPI route exists but no spec documents it.
- Spec-ahead drift: a spec documents a route that no longer exists in
  code, EXCEPT for entries explicitly allowlisted as forward
  declarations of architecturally intentional but as-yet-unimplemented
  features.

Run via: pytest tests/sage/test_openapi_conformance.py
"""

from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute

from sage.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"

# Paths exposed by FastAPI/Starlette infrastructure that are not part of
# any documented API surface.
_INFRA_PATHS = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

_SUCCESS_STATUSES = {"200", "201", "202", "204"}

# Operations that are deliberately documented in a spec ahead of their
# implementation. Entries are (path, method) tuples; each must have a
# justification comment pointing at the architectural authority that
# declares the feature as intentional pending work.
SPEC_FORWARD_DECLARATIONS: set[tuple[str, str]] = {
    # Editor-based write control: SAGE Architecture Reference v1.4.2
    # Section 4.3 (Editor Model) and Section 6.3 (Editor-Based Write
    # Control). set_editors / get_editors are listed in the service
    # operations table; FastAPI implementation is pending.
    ("/sage_vaults/{vault_id}/documents/{document_id}/editors", "get"),
    ("/sage_vaults/{vault_id}/documents/{document_id}/editors", "put"),
}


def _load_spec(path: Path) -> dict | None:
    """Load a yaml spec file, or return None if it doesn't exist yet.

    Returning None lets Test 5 surface a missing-spec-file failure with
    a clear message rather than producing a fixture-level error.
    """
    if not path.exists():
        return None
    with path.open() as f:
        return yaml.safe_load(f)


def _operations(spec: dict | None) -> set[tuple[str, str]]:
    """Extract (path, method) operations from a parsed spec, or empty
    set when the spec is missing.
    """
    if spec is None:
        return set()
    ops: set[tuple[str, str]] = set()
    for path, path_item in (spec.get("paths") or {}).items():
        if path in _INFRA_PATHS:
            continue
        for method in path_item:
            if method.lower() in _HTTP_METHODS:
                ops.add((path, method.lower()))
    return ops


@pytest.fixture(scope="module")
def sage_core_spec() -> dict | None:
    """Parsed SAGE Core API spec, or None if the file is missing."""
    return _load_spec(SAGE_CORE_SPEC_PATH)


@pytest.fixture(scope="module")
def cas_app_spec() -> dict | None:
    """Parsed CAS Application API spec, or None if the file is missing."""
    return _load_spec(CAS_APP_SPEC_PATH)


_PATH_CONVERTER_RE = __import__("re").compile(r"\{([^{}:]+):[^{}]+\}")


def _normalize_path(path: str) -> str:
    """Strip FastAPI path-converter annotations (e.g. {heading_path:path})
    so spec paths and FastAPI routes can be compared as plain templates.
    """
    return _PATH_CONVERTER_RE.sub(r"{\1}", path)


@pytest.fixture(scope="module")
def app_operations() -> set[tuple[str, str]]:
    """Set of (path, method) tuples exposed by the FastAPI app.

    Built without vault config, so no vault registry initialization is
    needed; routes are registered at app construction time.
    """
    app = create_app()
    ops: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in _INFRA_PATHS:
            continue
        normalized = _normalize_path(route.path)
        for method in route.methods:
            method_lower = method.lower()
            if method_lower in _HTTP_METHODS:
                ops.add((normalized, method_lower))
    return ops


@pytest.fixture(scope="module")
def all_spec_operations(
    sage_core_spec: dict | None,
    cas_app_spec: dict | None,
) -> set[tuple[str, str]]:
    """Union of operations documented across both specs."""
    return _operations(sage_core_spec) | _operations(cas_app_spec)


# ---------------------------------------------------------------------------
# Test 1: Spec path/method coverage matches FastAPI app routes
# ---------------------------------------------------------------------------


def test_spec_covers_all_app_operations(
    app_operations: set[tuple[str, str]],
    all_spec_operations: set[tuple[str, str]],
):
    """Every (path, method) operation in the FastAPI app appears in some
    spec, and every (path, method) in any spec exists in the app --
    EXCEPT for entries in SPEC_FORWARD_DECLARATIONS.

    Primary drift detector. Aggregates across both specs so endpoints
    can be split between sage_core_api and cas_app_api by URL prefix
    while still being validated as a single coverage set.
    """
    documented = all_spec_operations | SPEC_FORWARD_DECLARATIONS
    code_only = sorted(app_operations - documented)
    spec_only = sorted(all_spec_operations - app_operations - SPEC_FORWARD_DECLARATIONS)

    msg_lines: list[str] = []
    if code_only:
        msg_lines.append("Operations in code but missing from all specs:")
        for path, method in code_only:
            msg_lines.append(f"  {method.upper():6s} {path}")
    if spec_only:
        msg_lines.append(
            "Operations in some spec but missing from code "
            "(if intentional, add to SPEC_FORWARD_DECLARATIONS with justification):"
        )
        for path, method in spec_only:
            msg_lines.append(f"  {method.upper():6s} {path}")

    assert not code_only and not spec_only, "\n".join(msg_lines)


# ---------------------------------------------------------------------------
# Test 2: Every documented operation declares a non-stub response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_fixture", ["sage_core_spec", "cas_app_spec"])
def test_every_documented_operation_has_response_schema(
    spec_fixture: str,
    request: pytest.FixtureRequest,
):
    """Every documented (path, method) defines at least one success-class
    response, and that response either has a JSON schema reference /
    inline schema OR is explicitly 204 No Content.

    Prevents Test 1 from being satisfied by stub path entries with empty
    operation bodies. Runs against each spec independently.
    """
    spec = request.getfixturevalue(spec_fixture)
    if spec is None:
        pytest.skip(f"{spec_fixture}: file does not exist yet")

    issues: list[str] = []
    for path, path_item in (spec.get("paths") or {}).items():
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            responses = operation.get("responses") or {}
            success_statuses = [s for s in responses if s in _SUCCESS_STATUSES]
            if not success_statuses:
                issues.append(f"{method.upper():6s} {path}: no success-class response")
                continue
            for status in success_statuses:
                if status == "204":
                    continue
                response = responses[status] or {}
                content = response.get("content") or {}
                # Accept any content-type with a schema (application/json,
                # text/event-stream for SSE, etc.).
                has_schema = any((entry or {}).get("schema") for entry in content.values())
                if not has_schema:
                    issues.append(
                        f"{method.upper():6s} {path}: {status} response has no schema "
                        f"on any content-type"
                    )

    assert not issues, (
        f"{spec_fixture}: documented operations missing response schemas:\n" + "\n".join(issues)
    )


# ---------------------------------------------------------------------------
# Test 3: Spec is a syntactically valid OpenAPI 3.1 document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_fixture", ["sage_core_spec", "cas_app_spec"])
def test_spec_is_valid_openapi_31(
    spec_fixture: str,
    request: pytest.FixtureRequest,
):
    """The yaml file parses as YAML and has the top-level structure
    expected of an OpenAPI 3.1 document. Cheap regression guard.
    """
    spec = request.getfixturevalue(spec_fixture)
    if spec is None:
        pytest.skip(f"{spec_fixture}: file does not exist yet")

    assert "openapi" in spec, f"{spec_fixture}: missing top-level 'openapi'"
    assert spec["openapi"].startswith("3.1"), (
        f"{spec_fixture}: must declare OpenAPI 3.1.x, got {spec['openapi']!r}"
    )
    assert "info" in spec, f"{spec_fixture}: missing top-level 'info'"
    assert "paths" in spec, f"{spec_fixture}: missing top-level 'paths'"
    assert "components" in spec, f"{spec_fixture}: missing top-level 'components'"
    assert isinstance(spec["components"].get("schemas"), dict), (
        f"{spec_fixture}: components.schemas must be a dict"
    )


# ---------------------------------------------------------------------------
# Test 4: VaultStatsResponse documents lancedb_chunk_count
# ---------------------------------------------------------------------------


def test_vault_stats_response_documents_lancedb_chunk_count(
    sage_core_spec: dict | None,
):
    """Spot regression guard for the field that motivated this work."""
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    assert "VaultStatsResponse" in schemas, "components.schemas.VaultStatsResponse is not defined"
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "lancedb_chunk_count" in properties, (
        "VaultStatsResponse.properties.lancedb_chunk_count is missing"
    )
    field = properties["lancedb_chunk_count"]
    assert field.get("type") == "integer", (
        f"VaultStatsResponse.lancedb_chunk_count must have type 'integer', "
        f"got {field.get('type')!r}"
    )

    required = vault_stats.get("required") or []
    assert "lancedb_chunk_count" in required, (
        "VaultStatsResponse must list 'lancedb_chunk_count' in 'required'"
    )


# ---------------------------------------------------------------------------
# Test 5: Each spec covers only its declared URL-prefix domain
# ---------------------------------------------------------------------------


def test_specs_respect_url_prefix_boundaries(
    sage_core_spec: dict | None,
    cas_app_spec: dict | None,
):
    """sage_core_api.openapi.yaml documents only /sage_vaults/* paths.
    cas_app_api.openapi.yaml documents only /app/* paths. No path
    appears in both specs.

    Enforces the architectural separation between the SAGE Core API
    (graph/document operations) and the CAS Application API (UI-facing
    workflow tools).
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"
    assert cas_app_spec is not None, f"CAS Application API spec missing at {CAS_APP_SPEC_PATH}"

    sage_paths = set((sage_core_spec.get("paths") or {}).keys())
    app_paths = set((cas_app_spec.get("paths") or {}).keys())

    issues: list[str] = []

    sage_misplaced = [p for p in sage_paths if not p.startswith("/sage_vaults")]
    if sage_misplaced:
        issues.append("sage_core_api.openapi.yaml contains paths outside /sage_vaults/*:")
        for p in sorted(sage_misplaced):
            issues.append(f"  {p}")

    app_misplaced = [p for p in app_paths if not p.startswith("/app")]
    if app_misplaced:
        issues.append("cas_app_api.openapi.yaml contains paths outside /app/*:")
        for p in sorted(app_misplaced):
            issues.append(f"  {p}")

    overlap = sage_paths & app_paths
    if overlap:
        issues.append("Paths documented in both specs (must be disjoint):")
        for p in sorted(overlap):
            issues.append(f"  {p}")

    assert not issues, "\n".join(issues)
