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

# YAML schemas in cas_app_api.openapi.yaml that have no same-named
# BaseModel under app.backend.{models,router} by design. Same
# justification discipline as YAML_ONLY_FORWARD_DECLARATIONS.
CAS_APP_YAML_ONLY_FORWARD_DECLARATIONS: set[str] = {
    # SSE event payloads; serialized from plain dicts in router.py
    # rather than Pydantic models. T-0047 ports these.
    "ProgressEvent",
    "SummaryEvent",
    # FastAPI HTTPException currently serializes error responses; the
    # Pydantic mirror is a separate concern. T-0048 ports this.
    "ErrorResponse",
}


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
    "LifecycleAction",
    "LifecycleStatus",
    "PipelineStatus",
    "ResolutionPolicy",
    "ResponseLevel",
    "RetrievalMode",
    "RetrievalScope",
    "SortOrder",
    "SourceType",
    "TraversalDirection",
    "UserType",
    # oneOf composition -- variants handled on the Python side by
    # discriminated unions or runtime branching, so field-level parity
    # against a single same-named BaseModel is not meaningful. Same-named
    # BaseModels exist in sage.models.schemas but their internal shape
    # diverges from the YAML envelope.
    "IngestRequest",
    "TraverseRequest",
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
# Test 5: Every public Pydantic field in sage.models.schemas has a description
# ---------------------------------------------------------------------------


def test_every_pydantic_field_has_description():
    """Every BaseModel field in sage.models.schemas declares Field(description=...).

    Source-of-truth check for the discipline established by T-0034: the
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

    Deterministic guard against the gap class that produced T-0040 (seven
    YAML-only response classes with no Pydantic counterpart, discovered
    by chance). Reads the YAML on disk per CAS-ADR-008 (the formal
    substrate is the source of truth), not via FastAPI introspection --
    schemas not referenced by any handler still get checked.

    Forward direction only (YAML -> Pydantic). Python-only classes
    (e.g. `ExportProjectionResponse`, `RefreshViewsResponse`) are out of
    scope; their absence from the YAML, if a drift, is tracked separately.

    Scope: SAGE Core API spec only. Parallel parity assertion for
    `docs/fs/cas_app_api.openapi.yaml` is tracked by T-0043. The ROOT
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
    test_every_yaml_schema_has_pydantic_class (T-0042). Closes the gap
    that produced T-0044: Python-side additions could previously drift
    away from the YAML spec silently. Reads the YAML on disk per
    CAS-ADR-008 (the formal substrate is the source of truth), not via
    FastAPI introspection -- BaseModels not referenced by any handler
    still get checked.

    Reverse direction only (Pydantic -> YAML). Class-existence only; the
    forward-direction test already covers field-level superset, and the
    reverse direction is intentionally scoped to existence -- comparing
    Pydantic field sets to YAML property sets in both directions would
    duplicate the same signal.

    Scope: SAGE Core API spec only. Parallel parity assertion for
    `docs/fs/cas_app_api.openapi.yaml` is tracked separately by T-0043.
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
# Test 6b: CAS App YAML components/schemas have parity-checked Pydantic counterparts
# ---------------------------------------------------------------------------


def test_every_cas_app_yaml_schema_has_pydantic_class(cas_app_spec: dict | None):
    """Parallel of test_every_yaml_schema_has_pydantic_class for the CAS
    App API surface (T-0043). Walks app.backend.models (the centralized
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
