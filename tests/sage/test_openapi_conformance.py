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

import json
import re
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute

from sage import build_info
from sage.app import create_app
from tests.helpers.adapter_claims import ENABLEMENT_CLAIM_MARKERS

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"
SUBSTRATE_ROOT = _REPO_ROOT / "docs" / "fs"
SUBSTRATE_MANIFEST_PATH = SUBSTRATE_ROOT / "manifest.json"

# Paths exposed by FastAPI/Starlette infrastructure that are not part of
# any documented API surface.
_INFRA_PATHS = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    # Operational liveness probe for container health checks; not part of the
    # documented API surface (also excluded from the OpenAPI schema via
    # include_in_schema=False on the route).
    "/health",
    # OIDC redirect callback: a browser-facing redirect mechanism (302 + cookie),
    # not a documented JSON API operation -- the same category as /health. The
    # route carries include_in_schema=False; this entry keeps it out of the
    # code-vs-spec coverage comparison.
    "/app/auth/callback",
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

# YAML schemas in cas_app_api.openapi.yaml that have no same-named
# BaseModel under app.backend.{models,router} by design. Same
# justification discipline as YAML_ONLY_FORWARD_DECLARATIONS.
CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS: set[str] = set()


# Pydantic BaseModel classes in sage.models.schemas that have no same-named
# entry in components/schemas of sage_core_api.openapi.yaml by design. The
# allowlist is the symmetric counterpart of YAML_ONLY_FORWARD_DECLARATIONS
# and is governed by the same discipline: each grouping must carry a
# justification comment explaining why the class is intentionally
# Python-only-by-design (e.g., used only as an internal type hint, never
# serialized over HTTP). The allowlist is not a hiding place for drift --
# entries require a defensible architectural reason.
PYTHON_ONLY_FORWARD_DECLARATIONS: set[str] = set()


# YAML schemas in sage_core_api.openapi.yaml that have no same-named
# BaseModel in sage.models.schemas by design. Each grouping carries a
# justification comment in the same style as SPEC_FORWARD_DECLARATIONS.
YAML_ONLY_FORWARD_DECLARATIONS: set[str] = {
    # Enums codified in sage/models/enums.py as StrEnum subclasses, not
    # as BaseModel classes.
    "CatalogSortBy",
    "EdgeType",
    "FacetField",
    "PipelineStatus",
    "RationaleKind",
    "ReabstractOutcome",
    "ResolutionPolicy",
    "ResponseMode",
    "RetrievalMode",
    "RetrievalScope",
    "RetrievalTarget",
    "SortOrder",
    "SourceType",
    "StalenessBasis",
    "TraversalDirection",
    "UserType",
    # Extensible vocabularies. Vaults add domain-specific members to the
    # base sets, so both surfaces are typed as `str` and validated against
    # vault config at the API boundary rather than by the Python type
    # system -- deliberately not StrEnum members, per sage/models/enums.py.
    "LifecycleAction",
    "LifecycleStatus",
    # Constrained scalars, carried in Python as validating type aliases
    # (`DocumentIdStr`, `VaultIdStr` in sage/models/schemas.py) rather
    # than as BaseModel classes. A model would wrap a bare string in an
    # object on the wire, which is not the shape either surface sends.
    "DocumentId",
    "VaultId",
}

# Anti-vacuity floor for the field-level Pydantic->YAML direction, which
# skips any schema with no same-named model and would therefore pass over
# an empty loop if the reflection helpers returned nothing. 125 schemas
# pair today, across both specs; the floor sits well below that so
# ordinary movement does not trip it while a collapse does.
MIN_SCHEMAS_COMPARED_TO_PYDANTIC: int = 100


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


def _yaml_non_2xx_responses(
    spec: dict | None,
) -> list[tuple[str, str, str, str]]:
    """(path, method, status, yaml_description) for every non-2xx response
    declared in `spec`, excluding SPEC_FORWARD_DECLARATIONS.

    Used to build the parametrize list for the error-envelope conformance
    test. `default` and other non-numeric response keys are skipped --
    only numeric HTTP statuses outside _SUCCESS_STATUSES qualify.
    """
    out: list[tuple[str, str, str, str]] = []
    if spec is None:
        return out
    for path, path_item in (spec.get("paths") or {}).items():
        if path in _INFRA_PATHS:
            continue
        for method, operation in (path_item or {}).items():
            method_lower = method.lower()
            if method_lower not in _HTTP_METHODS:
                continue
            if (path, method_lower) in SPEC_FORWARD_DECLARATIONS:
                continue
            for status, response in (operation.get("responses") or {}).items():
                if not status.isdigit() or status in _SUCCESS_STATUSES:
                    continue
                description = (response or {}).get("description", "") or ""
                out.append((path, method_lower, status, description))
    return out


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
def live_openapi() -> dict:
    """Live /openapi.json dict produced by create_app().

    Module-scoped so the FastAPI app and its OpenAPI dict are built once
    for the whole module rather than per parametrized case.
    """
    return create_app().openapi()


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
# Test 2a: Live /openapi.json matches YAML non-2xx error envelopes
# ---------------------------------------------------------------------------


_YAML_NON_2XX_OPERATIONS = _yaml_non_2xx_responses(_load_spec(SAGE_CORE_SPEC_PATH))


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@pytest.mark.parametrize(
    "path,method,status,yaml_description",
    _YAML_NON_2XX_OPERATIONS,
    ids=[f"{m.upper()} {p} -> {s}" for (p, m, s, _d) in _YAML_NON_2XX_OPERATIONS],
)
def test_live_openapi_matches_yaml_error_envelope(
    path: str,
    method: str,
    status: str,
    yaml_description: str,
    live_openapi: dict,
):
    """For every non-2xx status declared in sage_core_api.openapi.yaml,
    the live /openapi.json produced by create_app():

    1. declares the same status code on the same (path, method);
    2. points the response at #/components/schemas/ErrorResponse via $ref;
    3. carries a description matching the YAML description after
       whitespace normalization (re.sub(r'\\s+', ' ', s).strip()).

    Regression gate for. Per-operation parametrization so failures
    attribute to one (path, method, status) rather than aggregating into a
    single multi-line message. Forward-declared operations
    (SPEC_FORWARD_DECLARATIONS) are excluded at parametrize-build time.
    """
    live_path = (live_openapi.get("paths") or {}).get(path)
    assert live_path is not None, f"Live /openapi.json is missing path {path!r} declared by YAML"

    operation = live_path.get(method)
    assert operation is not None, f"Live /openapi.json is missing operation {method.upper()} {path}"

    responses = operation.get("responses") or {}
    assert status in responses, (
        f"{method.upper()} {path}: live spec is missing response {status} "
        f"declared by YAML (router likely dropped its responses= entry)"
    )

    response = responses[status] or {}
    schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
    assert schema.get("$ref") == "#/components/schemas/ErrorResponse", (
        f"{method.upper()} {path}: {status} response must $ref "
        f"#/components/schemas/ErrorResponse, got {schema!r}"
    )

    live_description = response.get("description", "") or ""
    assert _norm_ws(live_description) == _norm_ws(yaml_description), (
        f"{method.upper()} {path}: {status} description drift\n"
        f"  YAML: {_norm_ws(yaml_description)!r}\n"
        f"  live: {_norm_ws(live_description)!r}"
    )


# ---------------------------------------------------------------------------
# Test 2b: Documented per-error-code HTTP status matches the status the
# endpoint actually raises (eval-retrieval)
# ---------------------------------------------------------------------------


def _eval_retrieval_error_instances() -> list:
    """The eval-retrieval assertions errors, instantiated so `.code` and the
    status the endpoint actually returns (`.status_code`) can be read.

    Throwaway constructor args -- only the code and status are inspected.
    """
    from sage.api.errors import (
        AssertionsFileInvalidError,
        AssertionsFileNotFoundError,
        AssertionsNotConfiguredError,
    )

    return [
        AssertionsFileNotFoundError("x"),
        AssertionsFileInvalidError("x", "reason"),
        AssertionsNotConfiguredError(),
    ]


def _documented_status_for_code(spec: dict, path: str, method: str, code: str) -> str | None:
    """The numeric response status under which `code` is documented for
    (path, method) in `spec`, located by the backtick token `` `code` `` in
    the response description. None if no response documents it.
    """
    operation = ((spec.get("paths") or {}).get(path) or {}).get(method) or {}
    for status, response in (operation.get("responses") or {}).items():
        if not status.isdigit():
            continue
        description = (response or {}).get("description", "") or ""
        if f"`{code}`" in description:
            return status
    return None


def test_eval_retrieval_error_status_matches_documented(sage_core_spec: dict | None):
    """Every eval-retrieval assertions error is documented in the committed
    OpenAPI under exactly the HTTP status the endpoint actually raises for it.

    Closes the gate gap that let F34 through: Test 2a
    (test_live_openapi_matches_yaml_error_envelope) pins router == YAML and the
    service tests in test_utilities.py pin the raised status, but nothing
    cross-checked documented-vs-actual -- so the two halves could each be green
    while contradicting each other (the endpoint raised 400 while the spec
    documented 404). This test compares the status the error class carries
    against the status its `code` is documented under, per CAS-ADR-008 (the
    committed YAML is the contract of record).
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"

    path = "/sage_vaults/{vault_id}/eval-retrieval"
    issues: list[str] = []
    for error in _eval_retrieval_error_instances():
        documented = _documented_status_for_code(sage_core_spec, path, "post", error.code)
        if documented is None:
            issues.append(
                f"{error.code}: endpoint raises {error.status_code} but no "
                f"eval-retrieval response documents the code"
            )
            continue
        if str(error.status_code) != documented:
            issues.append(
                f"{error.code}: endpoint raises {error.status_code} but the OpenAPI "
                f"documents the code under {documented}"
            )

    assert not issues, (
        "eval-retrieval error-code HTTP status drift (implementation vs. committed "
        "OpenAPI):\n  " + "\n  ".join(issues)
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
# Test 3a: info.version is single-sourced from build_info.API_VERSION
# ---------------------------------------------------------------------------


def test_openapi_info_version_matches_api_version(
    sage_core_spec: dict | None,
    cas_app_spec: dict | None,
    live_openapi: dict,
):
    """The live /openapi.json info.version and BOTH committed specs that the
    single FastAPI app serves (SAGE Core API and CAS Application API) equal
    build_info.API_VERSION.

    Single-source guard. The API version is VCS-derived and read once via
    build_info.API_VERSION; the FastAPI ``version=`` argument (and thus the
    live OpenAPI document) and every committed contract for that one app must
    track it, so the literals that previously drifted (app version, package
    version, committed specs) can no longer diverge. The CAS App spec is
    mounted at /app on the same server, so it shares the live version and is
    checked alongside the SAGE Core spec. Skipped only when the distribution
    metadata is absent (API_VERSION == UNKNOWN), i.e. a bare uninstalled
    checkout where no version can resolve.
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"
    assert cas_app_spec is not None, f"CAS Application API spec missing at {CAS_APP_SPEC_PATH}"
    if build_info.API_VERSION == build_info.UNKNOWN:
        pytest.skip("distribution metadata absent; API_VERSION is unknown")

    assert live_openapi["info"]["version"] == build_info.API_VERSION, (
        "live /openapi.json info.version diverges from build_info.API_VERSION "
        "(the FastAPI version= argument is not wired to the single source)"
    )
    for label, spec in (("sage_core_api", sage_core_spec), ("cas_app_api", cas_app_spec)):
        assert spec["info"]["version"] == build_info.API_VERSION, (
            f"committed {label}.openapi.yaml info.version "
            f"{spec['info']['version']!r} diverges from build_info.API_VERSION "
            f"{build_info.API_VERSION!r}; update the committed info.version to track the "
            "release tag"
        )


# ---------------------------------------------------------------------------
# Test 4: VaultStatsResponse documents content_store_chunk_count
# ---------------------------------------------------------------------------


def test_vault_stats_response_documents_content_store_chunk_count(
    sage_core_spec: dict | None,
):
    """Spot regression guard for the field that motivated this work."""
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    assert "VaultStatsResponse" in schemas, "components.schemas.VaultStatsResponse is not defined"
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "content_store_chunk_count" in properties, (
        "VaultStatsResponse.properties.content_store_chunk_count is missing"
    )
    field = properties["content_store_chunk_count"]
    assert field.get("type") == "integer", (
        f"VaultStatsResponse.content_store_chunk_count must have type 'integer', "
        f"got {field.get('type')!r}"
    )

    required = vault_stats.get("required") or []
    assert "content_store_chunk_count" in required, (
        "VaultStatsResponse must list 'content_store_chunk_count' in 'required'"
    )


def test_vault_stats_response_documents_content_store_version_count(
    sage_core_spec: dict | None,
):
    """Spot regression guard for the bloat-indicator version-count field."""
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    assert "VaultStatsResponse" in schemas, "components.schemas.VaultStatsResponse is not defined"
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "content_store_version_count" in properties, (
        "VaultStatsResponse.properties.content_store_version_count is missing"
    )
    field = properties["content_store_version_count"]
    assert field.get("type") == "integer", (
        f"VaultStatsResponse.content_store_version_count must have type 'integer', "
        f"got {field.get('type')!r}"
    )

    required = vault_stats.get("required") or []
    assert "content_store_version_count" in required, (
        "VaultStatsResponse must list 'content_store_version_count' in 'required'"
    )


def test_vault_stats_response_documents_content_store_small_fragment_count(
    sage_core_spec: dict | None,
):
    """Spot regression guard for the bloat-indicator small-fragment field."""
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    assert "VaultStatsResponse" in schemas, "components.schemas.VaultStatsResponse is not defined"
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "content_store_small_fragment_count" in properties, (
        "VaultStatsResponse.properties.content_store_small_fragment_count is missing"
    )
    field = properties["content_store_small_fragment_count"]
    assert field.get("type") == "integer", (
        f"VaultStatsResponse.content_store_small_fragment_count must have type 'integer', "
        f"got {field.get('type')!r}"
    )

    required = vault_stats.get("required") or []
    assert "content_store_small_fragment_count" in required, (
        "VaultStatsResponse must list 'content_store_small_fragment_count' in 'required'"
    )


def test_vault_stats_response_documents_graph_store_size_bytes(
    sage_core_spec: dict | None,
):
    """Spot regression guard for the backend-neutral live graph-store-size field."""
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    assert "VaultStatsResponse" in schemas, "components.schemas.VaultStatsResponse is not defined"
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "graph_store_size_bytes" in properties, (
        "VaultStatsResponse.properties.graph_store_size_bytes is missing"
    )
    field = properties["graph_store_size_bytes"]
    assert field.get("type") == "integer", (
        f"VaultStatsResponse.graph_store_size_bytes must have type 'integer', "
        f"got {field.get('type')!r}"
    )

    required = vault_stats.get("required") or []
    assert "graph_store_size_bytes" in required, (
        "VaultStatsResponse must list 'graph_store_size_bytes' in 'required'"
    )


def test_vault_stats_response_documents_last_optimize(
    sage_core_spec: dict | None,
):
    """Spot guard for the optional last-optimize summary.

    The property is a nested optional ($ref to LastOptimizeSummary) and so
    must NOT appear in VaultStatsResponse.required; the referenced schema
    must define the four summary fields and mark them required.
    """
    assert sage_core_spec is not None, "sage_core_api spec is missing"

    schemas = sage_core_spec["components"]["schemas"]
    vault_stats = schemas["VaultStatsResponse"]

    properties = vault_stats.get("properties") or {}
    assert "last_optimize" in properties, "VaultStatsResponse.properties.last_optimize is missing"
    required = vault_stats.get("required") or []
    assert "last_optimize" not in required, (
        "last_optimize is optional and must not be listed in VaultStatsResponse.required"
    )

    assert "LastOptimizeSummary" in schemas, "components.schemas.LastOptimizeSummary is not defined"
    summary = schemas["LastOptimizeSummary"]
    summary_props = summary.get("properties") or {}
    summary_required = summary.get("required") or []
    for name in ("at", "bytes_reclaimed", "versions_cleaned", "fragments_merged"):
        assert name in summary_props, f"LastOptimizeSummary.properties.{name} is missing"
        assert name in summary_required, f"LastOptimizeSummary must list '{name}' in 'required'"


@pytest.mark.parametrize("spec_fixture", ["sage_core_spec", "cas_app_spec"])
def test_summary_event_errors_items_reference_batch_ingest_file_error(
    request: pytest.FixtureRequest, spec_fixture: str
):
    """The batch summary's per-file error entries are typed at both
    declaration sites: ``SummaryEvent.errors`` points at the
    ``BatchIngestFileError`` component, whose required set is the
    message-only shape plus the file's zero-based position in the batch,
    and whose ``code`` and ``detail`` are optional.

    The schema-parity gate above checks that a same-named component exists
    on both sides of each spec; it cannot see whether ``errors`` actually
    references it. A component added but left unreferenced would leave the
    wire shape declared as a bare object, which is the drift this pins.
    """
    spec = request.getfixturevalue(spec_fixture)
    assert spec is not None, f"{spec_fixture} is missing"
    schemas = spec["components"]["schemas"]

    errors = schemas["SummaryEvent"]["properties"]["errors"]
    assert errors.get("type") == "array"
    assert errors.get("items", {}).get("$ref") == "#/components/schemas/BatchIngestFileError", (
        f"{spec_fixture}: SummaryEvent.errors.items must $ref BatchIngestFileError, "
        f"got {errors.get('items')!r}"
    )

    assert "BatchIngestFileError" in schemas, (
        f"{spec_fixture}: components.schemas.BatchIngestFileError is not defined"
    )
    entry = schemas["BatchIngestFileError"]
    assert sorted(entry.get("required") or []) == [
        "file_index",
        "filename",
        "message",
        "source_path",
    ]
    properties = entry.get("properties") or {}
    assert {"file_index", "filename", "source_path", "message", "code", "detail"} <= set(properties)
    assert properties["file_index"].get("type") == "integer"
    assert properties["file_index"].get("minimum") == 0
    assert properties["detail"].get("type") == "object"


@pytest.mark.parametrize("spec_fixture", ["sage_core_spec", "cas_app_spec"])
def test_progress_event_file_index_is_bounded_like_the_error_entrys(
    request: pytest.FixtureRequest, spec_fixture: str
):
    """``ProgressEvent.file_index`` and ``BatchIngestFileError.file_index``
    are one concept -- the file's zero-based position in the batch -- and the
    two declarations carry the same lower bound in both specs.

    The Pydantic side expresses the shared shape through one alias; the YAML
    has no alias mechanism, so parity there is only what this test pins.
    """
    spec = request.getfixturevalue(spec_fixture)
    assert spec is not None, f"{spec_fixture} is missing"
    schemas = spec["components"]["schemas"]
    progress = schemas["ProgressEvent"]["properties"]["file_index"]
    entry = schemas["BatchIngestFileError"]["properties"]["file_index"]
    assert progress.get("type") == "integer"
    assert progress.get("minimum") == 0, f"{spec_fixture}: ProgressEvent.file_index is unbounded"
    assert progress.get("minimum") == entry.get("minimum")


# ---------------------------------------------------------------------------
# Test 5: Every public Pydantic field in sage.models.schemas has a description
# ---------------------------------------------------------------------------


def test_every_pydantic_field_has_description():
    """Every BaseModel field in sage.models.schemas declares Field(description=...).

    Source-of-truth check for the discipline established by The
    FastAPI-generated OpenAPI inherits its per-field documentation from
    Pydantic Field descriptions, so an empty description here surfaces as
    an empty description in the rendered /docs page. Walks the module
    directly rather than the generated OpenAPI so failures attribute to a
    specific model.field rather than a $ref-resolved schema path.
    """
    from pydantic import BaseModel

    from sage.models import schemas as schemas_module

    issues: list[str] = []
    for name in dir(schemas_module):
        if name.startswith("_"):
            continue
        obj = getattr(schemas_module, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
            continue
        for field_name, field_info in obj.model_fields.items():
            description = field_info.description
            if not (isinstance(description, str) and description.strip()):
                issues.append(f"{name}.{field_name}: missing Field(description=...)")

    assert not issues, "Pydantic models missing field descriptions:\n  " + "\n  ".join(issues)


def test_every_cas_app_pydantic_field_has_description():
    """Every BaseModel field in app.backend.{models,router} declares
    Field(description=...).

    Companion to test_every_pydantic_field_has_description and
    test_every_sage_config_field_has_description. Closes the
    presence-test gap identified: the CAS App surface was previously
    gated only transitively through the YAML-parity check, which
    catches divergence but not absence. Walks both modules for symmetry
    with the parity test's module set, even though router currently
    defines no BaseModel classes -- guards against future router-defined
    response models silently bypassing the gate.
    """
    from pydantic import BaseModel

    from app.backend import models as app_models_module
    from app.backend import router as app_router_module

    issues: list[str] = []
    for module in (app_models_module, app_router_module):
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
                continue
            for field_name, field_info in obj.model_fields.items():
                description = field_info.description
                if not (isinstance(description, str) and description.strip()):
                    issues.append(f"{name}.{field_name}: missing Field(description=...)")

    assert not issues, "Pydantic models missing field descriptions:\n  " + "\n  ".join(issues)


# ---------------------------------------------------------------------------
# Test 5a: Every property in docs/fs/ OpenAPI YAML and JSON Schema files has a
# description
# ---------------------------------------------------------------------------


def _has_nonempty_description(node: object) -> bool:
    """True iff `node` is a dict carrying a non-empty `description` string."""
    if not isinstance(node, dict):
        return False
    desc = node.get("description")
    return isinstance(desc, str) and bool(desc.strip())


def _is_pure_ref(node: object) -> bool:
    """A property schema that is a bare $ref inherits its description from
    the referenced schema; the description requirement does not apply to
    the property site itself.
    """
    return isinstance(node, dict) and set(node.keys()) == {"$ref"}


def _walk_substrate_schema(
    node: object,
    pointer: str,
    file_label: str,
    issues: list[str],
) -> None:
    """Recursively walk a JSON-Schema-ish dict, recording missing
    descriptions on every leaf inside a `properties` mapping. Also
    recurses through standard combinators (allOf, oneOf, anyOf), `items`,
    dict-valued `additionalProperties`, `$defs`, and `definitions`. The
    description requirement applies to property schemas only; intermediate
    object subschemas are checked through their own properties, not as
    schemas in their own right.

    `if`/`then`/`else` blocks are intentionally not recursed into: in
    JSON Schema 2020-12 they encode conditional validation constraints
    (e.g., "when event_type matches this enum, require these fields"),
    so `properties` inside them references *existing* fields by name to
    match constraints, not to define new ones. The same field's true
    definition (with its description) lives in the schema's main
    `properties` block, which the walker reaches via the normal path.
    """
    if not isinstance(node, dict):
        return

    properties = node.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            prop_pointer = f"{pointer}/properties/{prop_name}"
            if isinstance(prop_schema, dict) and not _is_pure_ref(prop_schema):
                if not _has_nonempty_description(prop_schema):
                    issues.append(f"{file_label}::{prop_pointer}")
            _walk_substrate_schema(prop_schema, prop_pointer, file_label, issues)

    items = node.get("items")
    if isinstance(items, dict):
        _walk_substrate_schema(items, f"{pointer}/items", file_label, issues)
    elif isinstance(items, list):
        for i, item in enumerate(items):
            _walk_substrate_schema(item, f"{pointer}/items/{i}", file_label, issues)

    add_props = node.get("additionalProperties")
    if isinstance(add_props, dict):
        _walk_substrate_schema(add_props, f"{pointer}/additionalProperties", file_label, issues)

    for combinator in ("allOf", "oneOf", "anyOf"):
        members = node.get(combinator)
        if isinstance(members, list):
            for i, member in enumerate(members):
                _walk_substrate_schema(member, f"{pointer}/{combinator}/{i}", file_label, issues)

    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for name, def_schema in defs.items():
                _walk_substrate_schema(
                    def_schema, f"{pointer}/{defs_key}/{name}", file_label, issues
                )


def test_every_substrate_property_has_description():
    """Every OpenAPI components/schemas entry and every JSON Schema root
    in `docs/fs/`, and every property at every nesting depth, carries a
    non-empty `description`.

    YAML/JSON-side counterpart to test_every_pydantic_field_has_description
    . docs/fs/ is the formal substrate authority per CAS-ADR-008;
    Pydantic descriptions are derived from these files, so any gap here
    propagates to the rendered /docs page when the corresponding Pydantic
    Field reuses the same text.

    Walks every file referenced by `docs/fs/manifest.json`. Bare-$ref
    property schemas are exempt at the property site (the referenced
    schema's own description applies).
    """
    manifest = json.loads(SUBSTRATE_MANIFEST_PATH.read_text())

    missing_files: list[str] = []
    issues: list[str] = []

    for entry in manifest["schemas"]:
        rel_path = entry["path"]
        file_path = SUBSTRATE_ROOT / rel_path
        if not file_path.exists():
            missing_files.append(rel_path)
            continue

        if rel_path.endswith(".openapi.yaml"):
            spec = yaml.safe_load(file_path.read_text())
            components_schemas = (spec.get("components") or {}).get("schemas") or {}
            for schema_name, schema_def in components_schemas.items():
                pointer = f"#/components/schemas/{schema_name}"
                if not _has_nonempty_description(schema_def):
                    issues.append(f"{rel_path}::{pointer}")
                _walk_substrate_schema(schema_def, pointer, rel_path, issues)
        elif rel_path.endswith(".schema.json"):
            schema = json.loads(file_path.read_text())
            if not _has_nonempty_description(schema):
                issues.append(f"{rel_path}::#")
            _walk_substrate_schema(schema, "#", rel_path, issues)
        else:
            issues.append(
                f"{rel_path}: unknown substrate file extension; expected "
                f".openapi.yaml or .schema.json"
            )

    msg_lines: list[str] = []
    if missing_files:
        msg_lines.append(
            "Manifest references files that do not exist on disk "
            "(filename drift; align manifest.json with the on-disk path):"
        )
        for path in sorted(missing_files):
            msg_lines.append(f"  {path}")
    if issues:
        msg_lines.append(
            "Substrate schemas/properties missing `description` "
            "(formal substrate authority per CAS-ADR-008):"
        )
        for line in sorted(issues):
            msg_lines.append(f"  {line}")

    assert not msg_lines, "\n".join(msg_lines)


# ---------------------------------------------------------------------------
# Test 5b: Pydantic Field descriptions match YAML verbatim
# ---------------------------------------------------------------------------


def _norm_description(text: object) -> str:
    """Normalize a description string for verbatim comparison.

    Collapses YAML folded-scalar whitespace and Pydantic implicit
    string-literal concatenation to the same canonical form. Non-string
    inputs (None, missing) collapse to empty string so divergences
    surface as a textual mismatch rather than a TypeError.
    """
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


# Pydantic fields whose description is intentionally allowed to diverge
# from the same-named YAML property description. Entries are
# (schema_name, field_name) tuples; each must carry a justification
# comment explaining why the divergence is intentional. The allowlist
# is not a hiding place for drift -- entries require a defensible
# architectural reason.
DESCRIPTION_DIVERGENCE_ALLOWLIST: set[tuple[str, str]] = set()


def _basemodels_in(*modules: object) -> dict[str, type]:
    """Public Pydantic BaseModel subclasses exported by the given modules,
    keyed by class name. Later modules win on a name collision.
    """
    from pydantic import BaseModel

    out: dict[str, type] = {}
    for module in modules:
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                out[name] = obj
    return out


def _sage_pydantic_classes() -> dict[str, type]:
    """BaseModels backing the SAGE Core API spec."""
    from sage.models import schemas as sage_schemas_module

    return _basemodels_in(sage_schemas_module)


def _cas_app_pydantic_classes() -> dict[str, type]:
    """BaseModels backing the CAS Application API spec.

    The app declares request and response bodies across two modules; both
    are read so a class defined beside its route is found.
    """
    from app.backend import models as app_models_module
    from app.backend import router as app_router_module

    return _basemodels_in(app_models_module, app_router_module)


def _yaml_field_descriptions(spec: dict | None) -> dict[str, dict[str, str]]:
    """Build {schema_name: {field_name: yaml_description}} from a spec.

    Resolves `allOf` composition via `_flatten_yaml_properties`. Properties
    that are a pure `$ref` (one-key dict) are excluded from the returned
    map: the description belongs to the referenced schema, not the
    property site, and the verbatim-equality test would otherwise flag
    legitimate Pydantic-side context as a divergence. Properties with a
    missing or empty description contribute an empty string so divergences
    surface as text mismatches.
    """
    if spec is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    components_schemas = (spec.get("components") or {}).get("schemas") or {}
    for schema_name, schema_def in components_schemas.items():
        flat = _flatten_yaml_properties(schema_def, spec)
        field_map: dict[str, str] = {}
        for prop_name, prop_schema in flat.items():
            if isinstance(prop_schema, dict):
                if _is_pure_ref(prop_schema):
                    continue
                desc = prop_schema.get("description")
                field_map[prop_name] = desc if isinstance(desc, str) else ""
            else:
                field_map[prop_name] = ""
        out[schema_name] = field_map
    return out


def _check_pydantic_yaml_description_parity(
    yaml_descriptions: dict[str, dict[str, str]],
    pydantic_classes: dict[str, type],
    spec_label: str,
) -> list[str]:
    """Compare YAML property descriptions to Pydantic Field descriptions
    for every same-named (schema, field) pair. Returns a list of
    human-readable divergence messages; empty list means full parity.

    Deliberately takes no skip list. A YAML-only forward declaration has no
    entry in `pydantic_classes`, so the membership check below already passes
    over it; excluding such a schema by name would change nothing. The only
    schemas a skip list can actually suppress are those that *do* have a
    Pydantic counterpart -- exactly the ones this check exists to compare --
    so accepting one could only ever open a hole.
    """
    from pydantic import BaseModel

    issues: list[str] = []
    for schema_name, field_map in yaml_descriptions.items():
        if schema_name not in pydantic_classes:
            # Coverage gap is reported by test_every_yaml_schema_has_pydantic_class.
            continue
        model = pydantic_classes[schema_name]
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        for field_name, yaml_desc in field_map.items():
            if (schema_name, field_name) in DESCRIPTION_DIVERGENCE_ALLOWLIST:
                continue
            field_info = model.model_fields.get(field_name)
            if field_info is None:
                # Field-coverage gap is reported by the parity tests
                # (Tests 6 / 6b). Skip here to avoid double-reporting.
                continue
            pyd_desc = field_info.description
            yaml_norm = _norm_description(yaml_desc)
            pyd_norm = _norm_description(pyd_desc)
            if yaml_norm != pyd_norm:
                issues.append(
                    f"{spec_label} {schema_name}.{field_name}:\n"
                    f"    YAML:    {yaml_norm!r}\n"
                    f"    Pydantic: {pyd_norm!r}"
                )
    return issues


def test_pydantic_descriptions_match_yaml_verbatim(
    sage_core_spec: dict | None,
    cas_app_spec: dict | None,
):
    """For every same-named (schema, field) pair across the SAGE Core API
    YAML and `sage.models.schemas`, and across the CAS App API YAML and
    `app.backend.{models,router}`, the Pydantic Field(description=...)
    text equals the YAML property description verbatim (after whitespace
    normalization).

    Closes the drift class left open: authored Pydantic
    descriptions independently while YAML descriptions were absent;
    filled the YAML gaps. Without this gate, the two sides can
    silently diverge because no existing test asserts text equality --
    only presence on each side. Per CAS-ADR-008 the YAML is authoritative,
    so Pydantic is expected to match it.

    Intentional divergences live in DESCRIPTION_DIVERGENCE_ALLOWLIST with
    a justification comment.
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"
    assert cas_app_spec is not None, f"CAS Application API spec missing at {CAS_APP_SPEC_PATH}"

    sage_yaml_descs = _yaml_field_descriptions(sage_core_spec)
    cas_yaml_descs = _yaml_field_descriptions(cas_app_spec)

    issues: list[str] = []
    issues.extend(
        _check_pydantic_yaml_description_parity(
            sage_yaml_descs,
            _sage_pydantic_classes(),
            spec_label="sage_core_api",
        )
    )
    issues.extend(
        _check_pydantic_yaml_description_parity(
            cas_yaml_descs,
            _cas_app_pydantic_classes(),
            spec_label="cas_app_api",
        )
    )

    assert not issues, (
        "Pydantic Field(description=...) text diverges from YAML "
        "(formal substrate is authoritative per CAS-ADR-008; sync verbatim "
        "or add an entry to DESCRIPTION_DIVERGENCE_ALLOWLIST with justification):\n"
        + "\n".join(issues)
    )


@pytest.mark.parametrize(
    ("schema_name", "field_name"),
    [
        ("IngestRequest", "source_type"),
        ("TraverseRequest", "edge_type"),
    ],
)
def test_request_schema_descriptions_are_gated(
    sage_core_spec: dict | None,
    schema_name: str,
    field_name: str,
):
    """Injecting a divergence into a request schema's field description is
    caught by the parity check.

    Test 5b asserts that the two sides currently agree; it cannot tell
    agreement apart from a schema that is never compared. This test closes
    that gap for the two request schemas that were silently skipped: it
    plants a divergence and requires the check to report it.

    Stated against the observable behaviour of the parity check rather than
    against the mechanism that skipped these schemas, so it still fires if
    coverage is lost some other way -- a reintroduced skip parameter, a
    renamed class, or a property shape the description extractor drops.
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"

    yaml_descriptions = _yaml_field_descriptions(sage_core_spec)
    assert field_name in yaml_descriptions.get(schema_name, {}), (
        f"{schema_name}.{field_name} carries no YAML description to diverge from; "
        "the schema or property was renamed, or its shape is one "
        "_yaml_field_descriptions drops"
    )

    yaml_descriptions[schema_name][field_name] = "injected divergence sentinel"

    issues = _check_pydantic_yaml_description_parity(
        yaml_descriptions,
        _sage_pydantic_classes(),
        spec_label="sage_core_api",
    )

    assert any(f"{schema_name}.{field_name}" in issue for issue in issues), (
        f"A planted divergence in {schema_name}.{field_name} was not reported. "
        "The schema is being skipped, so its Pydantic and YAML descriptions can "
        "drift with no gate catching it."
    )


def _pydantic_backed(names: set[str], classes: dict[str, type]) -> list[str]:
    """Names that resolve to a Pydantic class in `classes`.

    Non-empty output means a forward-declaration list is carrying a schema
    that is not a forward declaration.
    """
    return sorted(name for name in names if name in classes)


def test_yaml_only_forward_declarations_have_no_pydantic_counterpart():
    """Every entry in a YAML-only forward-declaration list genuinely has no
    same-named Pydantic class.

    The lists exist for YAML schemas with no Python counterpart -- enums, and
    schemas documented ahead of implementation. A schema that does have a
    BaseModel is a real model being skipped, not a forward declaration, and
    parking one here suppresses the field-superset check in Test 6 for a model
    that check was written to cover.

    Enum entries are unaffected: they have no BaseModel, which is exactly what
    this test requires.
    """
    sage_offenders = _pydantic_backed(YAML_ONLY_FORWARD_DECLARATIONS, _sage_pydantic_classes())
    cas_app_offenders = _pydantic_backed(
        CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS, _cas_app_pydantic_classes()
    )

    assert not sage_offenders and not cas_app_offenders, (
        "Forward-declaration lists carry schemas that have a Pydantic class "
        "(remove them so the parity and field-superset checks cover them):\n"
        f"  YAML_ONLY_FORWARD_DECLARATIONS: {sage_offenders}\n"
        f"  CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS: {cas_app_offenders}"
    )


def test_pydantic_backed_detects_a_real_model():
    """`_pydantic_backed` reports a name that does have a Pydantic class.

    Guards the test above from passing vacuously: both lists could be empty
    of offenders because the audit holds, or because the helper never reports
    anything. `Document` is a long-standing BaseModel in sage.models.schemas,
    so a helper that discriminates must flag it.
    """
    classes = _sage_pydantic_classes()
    assert "Document" in classes, "sage.models.schemas.Document was renamed; pick another probe"
    assert _pydantic_backed({"Document"}, classes) == ["Document"]
    assert _pydantic_backed({"EdgeType"}, classes) == []


# Description surfaces that tell a caller when a `source_type` is rejected.
# Adapter resolution runs against the process-wide registry built by
# `build_source_adapter_registry`; vault configuration is never consulted for
# adapter availability, so no surface here may condition it on vault config.
_ADAPTER_AVAILABILITY_CLAIM_SURFACES: tuple[tuple[str, str], ...] = (
    ("IngestRequest", "source_type"),
    ("ParseFilenameRequest", "source_type"),
    ("IngestRequest", "config"),
    ("VaultSummary", "adapters"),
    ("VaultAdapterInfo", "extensions"),
)

_ADAPTER_NOT_FOUND_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("/sage_vaults/{vault_id}/documents", "post", "400"),
    ("/sage_vaults/{vault_id}/parse-filename", "post", "400"),
)

# The vault-config read is where a caller goes to look up why a source_type
# was rejected, so its operation description carries the same obligation as
# the error responses: it must not present vault config as the authority.
_ADAPTER_CLAIM_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("/sage_vaults/{vault_id}/config", "get"),
    ("/sage_vaults/{vault_id}/config", "put"),
)

#: Component schemas whose whole description block is swept, not just one
#: named field. The adapter-display models describe a set the caller cannot
#: influence, so any per-vault framing anywhere in them misleads.
_ADAPTER_CLAIM_SCHEMAS: tuple[str, ...] = (
    "VaultAdapterInfo",
    "UpdateVaultConfigRequest",
)

# The marker vocabulary is shared with the MCP-docstring sweep in
# tests/sage/test_mcp_self_documentation.py so the two arms cannot drift
# apart; see tests/helpers/adapter_claims.py for the rationale.
_ENABLEMENT_CLAIM_MARKERS: tuple[str, ...] = ENABLEMENT_CLAIM_MARKERS


def test_source_type_descriptions_do_not_claim_vault_config_enablement(
    sage_core_spec: dict | None,
):
    """No `source_type` description or `adapter_not_found` response says a
    source type must be *enabled* in vault configuration.

    Adapter resolution is a lookup in the process-wide registry
    (`build_source_adapter_registry`), which is built from a fixed mapping and
    never reads `source_adapters` from any vault's config. A claim to the
    contrary is not a divergence -- both sides carried the same false text, so
    Test 5b stays green either way -- which is why it needs its own gate.
    """
    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"

    yaml_descriptions = _yaml_field_descriptions(sage_core_spec)
    classes = _sage_pydantic_classes()

    surfaces: list[tuple[str, str]] = []
    for schema_name, field_name in _ADAPTER_AVAILABILITY_CLAIM_SURFACES:
        surfaces.append(
            (
                f"YAML {schema_name}.{field_name}",
                yaml_descriptions.get(schema_name, {}).get(field_name, ""),
            )
        )
        field_info = classes[schema_name].model_fields[field_name]
        surfaces.append((f"Pydantic {schema_name}.{field_name}", field_info.description or ""))

    for path, method, status in _ADAPTER_NOT_FOUND_OPERATIONS:
        response = sage_core_spec["paths"][path][method]["responses"][status]
        surfaces.append((f"YAML {method.upper()} {path} {status}", response.get("description", "")))

    for path, method in _ADAPTER_CLAIM_OPERATIONS:
        operation = sage_core_spec["paths"][path][method]
        surfaces.append((f"YAML {method.upper()} {path}", operation.get("description", "")))

    for schema_name in _ADAPTER_CLAIM_SCHEMAS:
        schema = sage_core_spec["components"]["schemas"][schema_name]
        surfaces.append((f"YAML {schema_name}", schema.get("description", "")))
        for field_name, field_schema in (schema.get("properties") or {}).items():
            surfaces.append(
                (
                    f"YAML {schema_name}.{field_name}",
                    (field_schema or {}).get("description", ""),
                )
            )
        for field_name, field_info in classes[schema_name].model_fields.items():
            surfaces.append((f"Pydantic {schema_name}.{field_name}", field_info.description or ""))

    offenders = [
        f"{label}: {marker!r}"
        for label, text in surfaces
        for marker in _ENABLEMENT_CLAIM_MARKERS
        if marker in text
    ]

    assert not offenders, (
        "Caller-facing text conditions adapter availability on vault "
        "configuration, which adapter resolution never consults:\n  " + "\n  ".join(offenders)
    )


def test_enablement_claim_markers_have_teeth():
    """The marker set flags text that makes the claim it exists to catch.

    Without this, the sweep above reports zero offenders whether the live
    text is clean or the marker tuple is empty, misspelled, or shadowed --
    the same vacuous-green failure mode the sweep was written to close.
    """
    clean = "Selects the source adapter. Availability is process-wide."
    offending = [
        "Only an enabled adapter can be used.",
        "The source type must be enabled in the vault config.",
        'Files whose adapter is off report status "adapter_disabled".',
        "Replacement for the source_adapters section.",
    ]

    def _flagged(text: str) -> list[str]:
        return [m for m in _ENABLEMENT_CLAIM_MARKERS if m in text]

    assert _flagged(clean) == []
    for text in offending:
        assert _flagged(text), f"marker set missed a live claim: {text!r}"


# ---------------------------------------------------------------------------
# Test 5c: sage.config Pydantic fields carry descriptions sourced verbatim
# from the JSON Schemas under docs/fs/sage/
# ---------------------------------------------------------------------------


# Maps each Pydantic class in sage.config to the JSON Schema file (relative
# to docs/fs/) and the JSON pointer that addresses its `properties` mapping.
# Drives both the presence test (which fields exist) and the verbatim test
# (which descriptions must match). VaultConfig.metadata_extraction /
# edge_inference resolve at the parent-property level in
# vault_config.schema.json; their full sub-schemas live in their own files
# but are not Pydantic-modeled in sage.config (passed through as dict).
# adapter_defaults is declared inline in vault_config.schema.json and is
# likewise a passed-through dict.
SAGE_CONFIG_CLASS_TO_SCHEMA: list[tuple[str, str, str]] = [
    ("VaultIdentity", "sage/vault_config.schema.json", "#/properties/vault"),
    ("LifecycleState", "sage/lifecycle.schema.json", "#/properties/states/items"),
    (
        "LifecycleTransition",
        "sage/lifecycle.schema.json",
        "#/properties/transitions/items",
    ),
    ("LifecycleConfig", "sage/lifecycle.schema.json", "#"),
    (
        "DocTypeEntry",
        "sage/document_types.schema.json",
        "#/properties/doc_types/items",
    ),
    ("DocumentTypesConfig", "sage/document_types.schema.json", "#"),
    (
        "VaultAbstractionConfig",
        "sage/vault_config.schema.json",
        "#/properties/abstraction",
    ),
    (
        "StackAbstractionConfig",
        "sage/sage_core_config.schema.json",
        "#/properties/abstraction",
    ),
    (
        "StackPostgresConfig",
        "sage/sage_core_config.schema.json",
        "#/properties/postgres",
    ),
    (
        "StackAuthConfig",
        "sage/sage_core_config.schema.json",
        "#/properties/auth",
    ),
    (
        "SageCoreConfig",
        "sage/sage_core_config.schema.json",
        "#",
    ),
    (
        "RetrievalHealthConfig",
        "sage/vault_config.schema.json",
        "#/properties/retrieval_health",
    ),
    ("VaultConfig", "sage/vault_config.schema.json", "#"),
]


# Same allowlist discipline as DESCRIPTION_DIVERGENCE_ALLOWLIST above:
# (class_name, field_name) tuples; each entry must carry a justification
# comment. The allowlist is not a hiding place for drift.
SAGE_CONFIG_DESCRIPTION_DIVERGENCE_ALLOWLIST: set[tuple[str, str]] = set()


def _resolve_json_pointer(schema: dict, pointer: str) -> dict:
    """Walk a `#/...`-style JSON pointer into a loaded schema dict.

    Handles the subset of JSON Pointer used by SAGE_CONFIG_CLASS_TO_SCHEMA:
    plain object property steps separated by `/`. The pointer always starts
    with `#`; trailing path is dereferenced step by step.
    """
    if pointer == "#":
        return schema
    assert pointer.startswith("#/"), f"expected fragment pointer, got {pointer!r}"
    node: dict = schema
    for step in pointer[2:].split("/"):
        assert isinstance(node, dict), (
            f"non-object node at intermediate step of pointer {pointer!r}"
        )
        node = node[step]
    return node


def test_every_sage_config_field_has_description():
    """Every BaseModel field in sage.config declares Field(description=...).

    Mirror of test_every_pydantic_field_has_description, scoped to
    sage.config rather than sage.models.schemas. Closes the gap surfaced as
    an F4 finding during the commit-time cas-code-review pass: the
    module walk does not reach sage.config, so the vault-config
    Pydantic models could carry zero descriptions without tripping any
    existing gate. Per CAS-ADR-008 these models derive from the JSON
    Schemas under docs/fs/sage/, so missing descriptions are drift from
    the formal substrate authority.
    """
    from pydantic import BaseModel

    from sage import config as sage_config_module

    issues: list[str] = []
    for name in dir(sage_config_module):
        if name.startswith("_"):
            continue
        obj = getattr(sage_config_module, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
            continue
        for field_name, field_info in obj.model_fields.items():
            description = field_info.description
            if not (isinstance(description, str) and description.strip()):
                issues.append(f"{name}.{field_name}: missing Field(description=...)")

    assert not issues, "sage.config models missing field descriptions:\n  " + "\n  ".join(issues)


def test_sage_config_descriptions_match_json_schema_verbatim():
    """For every (PydanticClass, field) pair in sage.config, the
    Field(description=...) text equals the corresponding property
    description in docs/fs/sage/*.schema.json verbatim (after whitespace
    normalization).

    Mirror of test_pydantic_descriptions_match_yaml_verbatim on
    the JSON-Schema side: that test only compares OpenAPI YAML <-> Pydantic
    and never reaches the vault-config schemas. Per CAS-ADR-008 the JSON
    Schemas are the formal substrate authority for vault configuration;
    sage.config descriptions must track them.

    The class-to-schema mapping lives in SAGE_CONFIG_CLASS_TO_SCHEMA above;
    each entry resolves to a `properties` mapping whose keys must match
    Pydantic field names one-for-one. Intentional divergences require an
    entry in SAGE_CONFIG_DESCRIPTION_DIVERGENCE_ALLOWLIST with
    justification.
    """
    from pydantic import BaseModel

    from sage import config as sage_config_module

    schema_cache: dict[str, dict] = {}
    issues: list[str] = []

    for class_name, schema_rel_path, pointer in SAGE_CONFIG_CLASS_TO_SCHEMA:
        model = getattr(sage_config_module, class_name, None)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            issues.append(f"{class_name}: not found in sage.config or not a BaseModel")
            continue

        if schema_rel_path not in schema_cache:
            schema_path = SUBSTRATE_ROOT / schema_rel_path
            schema_cache[schema_rel_path] = json.loads(schema_path.read_text())
        schema = schema_cache[schema_rel_path]

        node = _resolve_json_pointer(schema, pointer)
        properties = node.get("properties") or {}

        for field_name, field_info in model.model_fields.items():
            if (class_name, field_name) in SAGE_CONFIG_DESCRIPTION_DIVERGENCE_ALLOWLIST:
                continue
            schema_prop = properties.get(field_name)
            if not isinstance(schema_prop, dict):
                issues.append(
                    f"{schema_rel_path} {class_name}.{field_name}:\n"
                    f"    JSON Schema: no property {field_name!r} at {pointer}\n"
                    f"    Pydantic:    {_norm_description(field_info.description)!r}"
                )
                continue
            json_norm = _norm_description(schema_prop.get("description"))
            pyd_norm = _norm_description(field_info.description)
            if json_norm != pyd_norm:
                issues.append(
                    f"{schema_rel_path} {class_name}.{field_name}:\n"
                    f"    JSON Schema: {json_norm!r}\n"
                    f"    Pydantic:    {pyd_norm!r}"
                )

    assert not issues, (
        "sage.config Field(description=...) text diverges from JSON Schema "
        "(formal substrate is authoritative per CAS-ADR-008; sync verbatim "
        "or add an entry to SAGE_CONFIG_DESCRIPTION_DIVERGENCE_ALLOWLIST "
        "with justification):\n" + "\n".join(issues)
    )


# ---------------------------------------------------------------------------
# Test 6: YAML components/schemas have parity-checked Pydantic counterparts
# ---------------------------------------------------------------------------


def _flatten_yaml_properties(schema_def: dict, spec: dict) -> dict:
    """Resolve `allOf` composition into a flat properties dict.

    For `allOf`, walk each part: `$ref` parts resolve against
    `components/schemas` and recurse; inline parts contribute their
    `properties` directly. Non-`allOf` schemas pass through unchanged.
    """
    if "allOf" in schema_def:
        merged: dict = {}
        for part in schema_def["allOf"]:
            ref = part.get("$ref")
            if ref:
                # "#/components/schemas/Document" -> "Document"
                name = ref.rsplit("/", 1)[-1]
                ref_def = spec["components"]["schemas"].get(name, {})
                merged.update(_flatten_yaml_properties(ref_def, spec))
            else:
                merged.update(part.get("properties") or {})
        return merged
    return schema_def.get("properties") or {}


def test_every_yaml_schema_has_pydantic_class(sage_core_spec: dict | None):
    """Every components/schemas entry in the SAGE Core API YAML has a
    same-named BaseModel in sage.models.schemas, and that BaseModel's
    fields are a superset of the YAML schema's properties.

    Deterministic guard against the gap class that produced (seven
    YAML-only response classes with no Pydantic counterpart, discovered
    by chance). Reads the YAML on disk per CAS-ADR-008 (the formal
    substrate is the source of truth), not via FastAPI introspection --
    schemas not referenced by any handler still get checked.

    Forward direction only (YAML -> Pydantic). Python-only classes
    (e.g. `ExportProjectionResponse`, `RefreshViewsResponse`) are out of
    scope; their absence from the YAML, if a drift, is tracked separately.

    Scope: SAGE Core API spec only. Parallel parity assertion for
    `docs/fs/cas_app_api.openapi.yaml` is tracked by. The ROOT
    Harness OpenAPI spec has no Python implementation yet (only the
    spec exists), so YAML<->Pydantic parity is not yet meaningful for
    that surface.

    Required-vs-optional parity is *not* asserted here. OpenAPI's
    `required` means "property always appears in the serialized JSON";
    Pydantic's `is_required()` means "caller must supply at construction
    time". A Pydantic field with a default value is not required in
    Pydantic's sense but is always present in the output, so the YAML
    correctly marks it required. The two notions of "required" do not map
    cleanly; comparing them produces noise, not signal.
    """
    from pydantic import BaseModel

    from sage.models import schemas as schemas_module

    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"

    pydantic_classes: dict[str, type[BaseModel]] = {}
    for name in dir(schemas_module):
        if name.startswith("_"):
            continue
        obj = getattr(schemas_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            pydantic_classes[name] = obj

    yaml_schemas = sage_core_spec["components"]["schemas"]

    missing_classes: list[str] = []
    missing_fields: list[str] = []

    for schema_name, schema_def in yaml_schemas.items():
        if schema_name in YAML_ONLY_FORWARD_DECLARATIONS:
            continue
        if schema_name not in pydantic_classes:
            missing_classes.append(schema_name)
            continue

        model = pydantic_classes[schema_name]
        yaml_props = _flatten_yaml_properties(schema_def, sage_core_spec)

        pyd_field_names = set(model.model_fields.keys())
        yaml_field_names = set(yaml_props.keys())
        gap = yaml_field_names - pyd_field_names
        if gap:
            missing_fields.append(f"{schema_name}: Pydantic missing fields {sorted(gap)}")

    msg_lines: list[str] = []
    if missing_classes:
        msg_lines.append(
            "YAML schemas without a same-named BaseModel in sage.models.schemas "
            "(if intentional, add to YAML_ONLY_FORWARD_DECLARATIONS with justification):"
        )
        for name in sorted(missing_classes):
            msg_lines.append(f"  {name}")
    if missing_fields:
        msg_lines.append("Pydantic models missing YAML-declared fields:")
        for line in sorted(missing_fields):
            msg_lines.append(f"  {line}")

    assert not msg_lines, "YAML<->Pydantic parity violations:\n" + "\n".join(msg_lines)


# ---------------------------------------------------------------------------
# Test 6a: Every Pydantic BaseModel has a counterpart in YAML components/schemas
# ---------------------------------------------------------------------------


def test_every_pydantic_class_has_yaml_schema(sage_core_spec: dict | None):
    """Every BaseModel in sage.models.schemas has a same-named entry in
    components/schemas of the SAGE Core API YAML.

    Reverse-direction parity counterpart to
    test_every_yaml_schema_has_pydantic_class. Closes the gap
    that produced Python-side additions could previously drift
    away from the YAML spec silently. Reads the YAML on disk per
    CAS-ADR-008 (the formal substrate is the source of truth), not via
    FastAPI introspection -- BaseModels not referenced by any handler
    still get checked.

    Reverse direction only (Pydantic -> YAML), and class-existence only.
    The field-level reverse direction is a separate signal rather than a
    duplicate of the forward one -- the forward test asserts YAML
    properties are a subset of the model's fields, which says nothing
    about a field the model declares and the YAML omits -- and it is
    asserted by `test_every_pydantic_field_has_a_yaml_property` below.

    Scope: SAGE Core API spec only. Parallel parity assertion for
    `docs/fs/cas_app_api.openapi.yaml` is tracked separately by.
    The ROOT Harness OpenAPI spec has no Python implementation yet (only
    the spec exists), so YAML<->Pydantic parity is not yet meaningful
    for that surface.
    """
    from pydantic import BaseModel

    from sage.models import schemas as schemas_module

    assert sage_core_spec is not None, f"SAGE Core API spec missing at {SAGE_CORE_SPEC_PATH}"

    yaml_schema_names = set(sage_core_spec["components"]["schemas"].keys())

    missing_schemas: list[str] = []
    for name in dir(schemas_module):
        if name.startswith("_"):
            continue
        obj = getattr(schemas_module, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
            continue
        if name in PYTHON_ONLY_FORWARD_DECLARATIONS:
            continue
        if name not in yaml_schema_names:
            missing_schemas.append(name)

    msg_lines: list[str] = []
    if missing_schemas:
        msg_lines.append(
            "Pydantic BaseModels without a same-named schema in "
            "sage_core_api.openapi.yaml components/schemas "
            "(if intentional, add to PYTHON_ONLY_FORWARD_DECLARATIONS with justification):"
        )
        for name in sorted(missing_schemas):
            msg_lines.append(f"  {name}")

    assert not msg_lines, "Pydantic->YAML parity violations:\n" + "\n".join(msg_lines)


# ---------------------------------------------------------------------------
# Test 6a-ii: Every Pydantic field is declared as a YAML property
# ---------------------------------------------------------------------------


def test_every_pydantic_field_has_a_yaml_property(
    sage_core_spec: dict | None, cas_app_spec: dict | None
):
    """Every field a response model declares is declared in the YAML too.

    The field-level reverse direction. Its sibling asserts the YAML's
    properties are a subset of the model's fields, which is silent on
    the mirror case: a field the API really returns and the published
    contract never mentions. A caller reading the spec does not know to
    look for it, and the verbatim description-parity gate cannot see it
    either, because that gate iterates YAML properties and a property
    that does not exist has no description to compare.

    Found eight such fields when first written, across both specs --
    among them `Document.metadata_confirmed`, which four caller-facing
    narratives name while the contract did not declare it, and
    `ChainResponse.total_length`, the field a caller needs in order to
    page correctly.

    Class-existence in both directions is covered by the neighbouring
    tests; this one runs only over schemas that already have a
    same-named model, so a missing class is reported once, there. That
    skip is also this test's vacuity risk -- reflection returning
    nothing would make every schema skip and the assertion pass over an
    empty loop -- so the count of schemas actually compared is floored
    below.
    """
    violations: list[str] = []
    compared = 0
    for label, spec, classes in (
        ("sage_core", sage_core_spec, _sage_pydantic_classes()),
        ("cas_app", cas_app_spec, _cas_app_pydantic_classes()),
    ):
        if spec is None:
            continue
        for schema_name, schema_def in (
            (spec.get("components") or {}).get("schemas") or {}
        ).items():
            model = classes.get(schema_name)
            if model is None:
                continue
            compared += 1
            undeclared = set(model.model_fields) - set(_flatten_yaml_properties(schema_def, spec))
            if undeclared:
                violations.append(f"  {label}: {schema_name} -> {sorted(undeclared)}")

    assert compared >= MIN_SCHEMAS_COMPARED_TO_PYDANTIC, (
        f"only {compared} schemas were compared against a Pydantic model; "
        "the reflection helpers returned little or nothing, so this test "
        "passed over an almost-empty loop rather than finding no violations"
    )
    assert not violations, (
        "Pydantic fields with no YAML property (the API returns these and "
        "the published contract does not declare them):\n" + "\n".join(sorted(violations))
    )


# ---------------------------------------------------------------------------
# Test 6b: CAS App YAML components/schemas have parity-checked Pydantic counterparts
# ---------------------------------------------------------------------------


def test_every_cas_app_yaml_schema_has_pydantic_class(cas_app_spec: dict | None):
    """Parallel of test_every_yaml_schema_has_pydantic_class for the CAS
    App API surface. Walks app.backend.models (the centralized
    home for /app/scan response shapes) and app.backend.router (ingest-
    chain models that remain in the router module pending their own
    typing pass) for BaseModel definitions.

    Same shape as the SAGE counterpart: forward-direction parity only
    (YAML -> Pydantic); required-vs-optional comparison not asserted.
    """
    from pydantic import BaseModel

    from app.backend import models as models_module
    from app.backend import router as router_module

    assert cas_app_spec is not None, f"CAS Application API spec missing at {CAS_APP_SPEC_PATH}"

    pydantic_classes: dict[str, type[BaseModel]] = {}
    for module in (models_module, router_module):
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                pydantic_classes[name] = obj

    yaml_schemas = cas_app_spec["components"]["schemas"]

    missing_classes: list[str] = []
    missing_fields: list[str] = []

    for schema_name, schema_def in yaml_schemas.items():
        if schema_name in CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS:
            continue
        if schema_name not in pydantic_classes:
            missing_classes.append(schema_name)
            continue

        model = pydantic_classes[schema_name]
        yaml_props = _flatten_yaml_properties(schema_def, cas_app_spec)

        pyd_field_names = set(model.model_fields.keys())
        yaml_field_names = set(yaml_props.keys())
        gap = yaml_field_names - pyd_field_names
        if gap:
            missing_fields.append(f"{schema_name}: Pydantic missing fields {sorted(gap)}")

    msg_lines: list[str] = []
    if missing_classes:
        msg_lines.append(
            "CAS App YAML schemas without a same-named BaseModel in app.backend.models "
            "or app.backend.router (if intentional, add to "
            "CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS with justification):"
        )
        for name in sorted(missing_classes):
            msg_lines.append(f"  {name}")
    if missing_fields:
        msg_lines.append("Pydantic models missing YAML-declared fields:")
        for line in sorted(missing_fields):
            msg_lines.append(f"  {line}")

    assert not msg_lines, "CAS App YAML<->Pydantic parity violations:\n" + "\n".join(msg_lines)


# ---------------------------------------------------------------------------
# Test 7: Each spec covers only its declared URL-prefix domain
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

    # The transfer endpoints are process-scoped, not vault-scoped: the vault
    # binding lives inside the one-time token, and the recipe embeds the URL
    # verbatim, so the paths stay top-level by design. They remain part of
    # the SAGE Core API surface.
    sage_non_vault_paths = {"/upload", "/download/{transfer_id}"}

    sage_misplaced = [
        p for p in sage_paths if not p.startswith("/sage_vaults") and p not in sage_non_vault_paths
    ]
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


# ---------------------------------------------------------------------------
# Test 2c: every operation that can emit the store-refusal codes declares them
# ---------------------------------------------------------------------------


# The operations whose service path reads or writes through the vault-source
# store inside a refusal translation, and which therefore can answer with
# either store-refusal code. Adding a translation to a fifth operation without
# adding it here is the drift this pins.
STORE_REFUSAL_OPERATIONS = frozenset(
    {
        "ingest_document",
        "restore_vault_source_file",
        "verify_vault_source_files",
        "get_document",
        "get_document_content",
        "get_document_download_url",
        "transfer_download",
    }
)


def test_store_refusal_operations_declare_both_codes(live_openapi: dict):
    """The set of operations declaring the two vault-source store-refusal
    statuses is exactly the set whose service path can raise them.

    The envelope gate above is driven from the YAML, so it catches a router
    that dropped a declared status but not an operation that can emit a status
    it never declared. That direction matters here: the translation and the
    declaration are separate edits, and an operation carrying the first without
    the second answers with a status absent from its own contract.

    Pinned as an equality rather than a subset so the reverse drift -- a
    declaration left on an operation whose translation was removed, which would
    document a status the operation can no longer return -- fails too.
    """
    declaring = {
        operation.get("operationId")
        for methods in (live_openapi.get("paths") or {}).values()
        for operation in methods.values()
        if isinstance(operation, dict)
        for responses in [operation.get("responses") or {}]
        if "502" in responses and "503" in responses
    }

    assert declaring == set(STORE_REFUSAL_OPERATIONS), (
        "store-refusal declaration drift\n"
        f"  declared but not expected: {sorted(declaring - STORE_REFUSAL_OPERATIONS)}\n"
        f"  expected but not declared: {sorted(STORE_REFUSAL_OPERATIONS - declaring)}"
    )


def test_store_refusal_operations_each_declare_both_not_one(live_openapi: dict):
    """No operation declares one of the pair without the other.

    Anti-coincidental-pass: the equality above matches on operations carrying
    *both* statuses, so an operation that declared only the 502 would drop out
    of ``declaring`` and be reported as a missing expectation -- a message that
    points at the wrong fix. This separates the two failures.
    """
    lopsided = [
        (operation.get("operationId"), sorted(set(responses) & {"502", "503"}))
        for methods in (live_openapi.get("paths") or {}).values()
        for operation in methods.values()
        if isinstance(operation, dict)
        for responses in [operation.get("responses") or {}]
        if len(set(responses) & {"502", "503"}) == 1
    ]

    assert not lopsided, (
        f"operations declaring one store-refusal status but not the pair: {lopsided}"
    )
