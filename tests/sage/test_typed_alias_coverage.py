"""Conformance gate: typed-alias coverage on Pydantic BaseModel fields.

Cites the CAS Typed-Alias Boundary Conventions steering document (cas
SAGE vault, doc_type=steering_document). A typed alias is a
``pydantic.Annotated[str, AfterValidator(...)]`` declared in
``sage/models/schemas.py``. Every shape-bearing field on a ``BaseModel``
subclass in any module listed in ``_SCOPED_MODULES`` below must either
carry one of those typed aliases or be pinned in ``KNOWN_VIOLATIONS``
with a one-line reason.

Allowlist contract (mirrors ``tests/sage/test_router_conformance.py``):

- Adding a new bare-``str`` shape-bearing field without an allowlist
  entry fails the suite.
- Removing a ``KNOWN_VIOLATIONS`` entry without remediating (typing the
  field) fails the suite.
- Typing a previously-allowlisted field without removing the entry
  fails the suite (stale-allowlist check).

Scope — in scope (``_SCOPED_MODULES``):

- ``sage.models.schemas`` — canonical Core API request/response shapes.
- ``app.backend.router`` — CAS Application request/response shapes.
- ``sage.config`` — vault-config shapes loaded from YAML.

Scope — F4 scan, still-parallel BaseModel locations:

- ``root_harness/`` does not exist yet; future BaseModels there are
  expected to follow the same convention and join ``_SCOPED_MODULES``.
- A repo-wide ``class \\w+\\(BaseModel\\)`` grep at T-0028 commit
  surfaced no additional BaseModel locations outside the three scoped
  modules; coverage is complete for the current source tree.

Scope — not yet typeable (no alias exists):

- ISO timestamp strings (e.g. ``ScanResultResponse.source_modified_at``,
  ``ParsedMetadataResponse.date``) — names do not match the current
  ``*_date`` suffix rule and no ``IsoTimestampStr`` alias exists.
- Filesystem paths (e.g. ``VaultIdentity.storage_root``,
  ``VaultIdentity.brain_root``, ``RetrievalHealthConfig.assertions_file``)
  — no path alias exists. The gate does not flag these today.

Drain plan: T-0026 typed the 22 currently-allowlisted fields whose
aliases already existed; T-0027 introduced ``UserIdStr`` / ``VaultIdStr``
/ ``FunctionIdStr`` and typed the remaining 5 (``KNOWN_VIOLATIONS`` is
now empty); T-0028 extended the gate to ``app.backend.router`` and
``sage.config`` and typed the four shape-bearing fields the extension
surfaced.
"""

from __future__ import annotations

import inspect
import typing
from types import UnionType
from typing import Final

import pytest
from pydantic import AfterValidator, BaseModel

from app.backend import router as router_mod
from sage import config as config_mod
from sage.models import schemas as schemas_mod
from sage.models.schemas import (
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    FunctionIdStr,
    Sha256Str,
    UserIdStr,
    VaultIdStr,
)

# Modules whose ``BaseModel`` subclasses are governed by this gate.
# Order is irrelevant; discovery dedupes by class identity.
_SCOPED_MODULES: Final[tuple] = (schemas_mod, router_mod, config_mod)

# ---------------------------------------------------------------------------
# Shape registry
#
# Specific-name keys (no leading ``*``) win over ``*_suffix`` patterns
# at lookup time.
# ---------------------------------------------------------------------------

SHAPE_REGISTRY: Final[dict[str, type]] = {
    "id": DocumentIdStr,  # exact match — Model.id default
    "edge_id": EdgeIdStr,  # exact match — wins over *_id
    "vault_id": VaultIdStr,  # exact match — wins over *_id
    "function_id": FunctionIdStr,  # exact match — wins over *_id
    "*_id": DocumentIdStr,
    "*_hash": Sha256Str,
    "*_date": DocumentDateStr,
}


# Validators that count as typed-alias coverage. A field whose Pydantic
# metadata contains an ``AfterValidator`` whose ``.func`` is one of these
# is considered shape-validated, regardless of which specific alias the
# registry would have chosen. This relaxation lets ``Edge.id: EdgeIdStr``
# pass the ``id`` registry entry (which defaults to ``DocumentIdStr``)
# without a false positive.
_TYPED_VALIDATORS: Final[frozenset] = frozenset(
    {
        schemas_mod._validate_document_id,
        schemas_mod._validate_edge_id,
        schemas_mod._validate_function_id,
        schemas_mod._validate_sha256,
        schemas_mod._validate_document_date,
        schemas_mod._validate_user_id,
        schemas_mod._validate_vault_id,
    }
)


# ---------------------------------------------------------------------------
# KNOWN_VIOLATIONS
#
# Keyed by (ClassName, field_name). Each value is a one-line reason; the
# leading T-NNNN points at the remediation ticket where applicable.
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS: Final[dict[tuple[str, str], str]] = {}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_basemodels() -> list[type[BaseModel]]:
    """Every ``BaseModel`` subclass declared in any module in ``_SCOPED_MODULES``.

    A class is attributed to a module only if its ``__module__`` matches
    the module's import name, so re-exports do not double-count.
    """
    out: list[type[BaseModel]] = []
    for mod in _SCOPED_MODULES:
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseModel:
                continue
            if not issubclass(obj, BaseModel):
                continue
            if obj.__module__ != mod.__name__:
                continue
            out.append(obj)
    return out


def _expected_alias(field_name: str) -> type | None:
    """Lookup the expected alias for ``field_name``.

    Exact-name keys win; ``*_suffix`` patterns match only when no exact
    entry exists.
    """
    if field_name in SHAPE_REGISTRY:
        return SHAPE_REGISTRY[field_name]
    for pattern, expected in SHAPE_REGISTRY.items():
        if pattern.startswith("*_") and field_name.endswith(pattern[1:]):
            return expected
    return None


def _walk_annotation(annotation) -> tuple[bool, bool]:
    """Walk an annotation tree; return ``(has_str_arm, has_typed_validator)``.

    Pydantic v2 stores typed aliases in two places depending on whether
    the field is optional:

    - Required ``DocumentIdStr``: ``field_info.annotation == str`` and the
      ``AfterValidator`` lives in ``field_info.metadata``.
    - ``DocumentIdStr | None``: the Union arm is the ``Annotated[str, ...]``
      itself; ``field_info.metadata`` is empty.

    This walker covers the second case (and is composed with a direct
    metadata check elsewhere for the first). It also reports whether a
    ``str`` arm exists at all, so callers can exclude e.g.
    ``datetime | None`` fields whose names happen to match a registry
    suffix.
    """
    has_str = False
    has_typed = False
    stack = [annotation]
    while stack:
        node = stack.pop()
        if hasattr(node, "__metadata__"):
            for m in node.__metadata__:
                if isinstance(m, AfterValidator) and m.func in _TYPED_VALIDATORS:
                    has_typed = True
            stack.append(node.__origin__)
            continue
        origin = typing.get_origin(node)
        if origin is typing.Union or origin is UnionType:
            stack.extend(typing.get_args(node))
            continue
        if node is str:
            has_str = True
    return has_str, has_typed


def _field_has_typed_alias(cls: type[BaseModel], name: str) -> bool:
    """True iff the field carries a typed-alias validator anywhere in its annotation."""
    field_info = cls.model_fields[name]
    for m in field_info.metadata:
        if isinstance(m, AfterValidator) and m.func in _TYPED_VALIDATORS:
            return True
    _, has_typed = _walk_annotation(field_info.annotation)
    return has_typed


def _alias_display_name(alias) -> str:
    """Human-readable name for a typed alias (Annotated has no ``__name__``)."""
    if alias is DocumentIdStr:
        return "DocumentIdStr"
    if alias is EdgeIdStr:
        return "EdgeIdStr"
    if alias is Sha256Str:
        return "Sha256Str"
    if alias is DocumentDateStr:
        return "DocumentDateStr"
    if alias is UserIdStr:
        return "UserIdStr"
    if alias is VaultIdStr:
        return "VaultIdStr"
    if alias is FunctionIdStr:
        return "FunctionIdStr"
    return str(alias)


def _shape_bearing_fields() -> list[tuple[type[BaseModel], str, type]]:
    """Yield (cls, field_name, expected_alias) for every own-field whose
    name matches the shape registry *and* whose annotation carries a
    ``str`` arm. Inherited fields are not re-walked; each class accounts
    only for fields declared in its own body. Non-``str`` fields whose
    names happen to match a suffix (e.g. ``DocumentSummary.document_date:
    datetime | None``) are filtered out — bare ``datetime`` is acceptable
    per the convention.
    """
    rows: list[tuple[type[BaseModel], str, type]] = []
    for cls in _discover_basemodels():
        own_annotations = inspect.get_annotations(cls)
        for field_name in own_annotations:
            if field_name not in cls.model_fields:
                # ClassVar / Pydantic-skipped annotation
                continue
            expected = _expected_alias(field_name)
            if expected is None:
                continue
            has_str, _ = _walk_annotation(cls.model_fields[field_name].annotation)
            if not has_str:
                continue
            rows.append((cls, field_name, expected))
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discovered_basemodels_are_nonempty():
    """Sanity: discovery surfaces the expected schemas across every scoped module."""
    classes = _discover_basemodels()
    names = {cls.__name__ for cls in classes}
    # sage.models.schemas
    assert "Document" in names, "Discovery missed core Document model"
    assert "Edge" in names, "Discovery missed core Edge model"
    assert "LinkRequest" in names, "Discovery missed canonical LinkRequest model"
    # app.backend.router
    assert "ScanRequest" in names, "Discovery missed app.backend.router ScanRequest"
    assert "ScanResultResponse" in names, "Discovery missed app.backend.router ScanResultResponse"
    # sage.config
    assert "VaultIdentity" in names, "Discovery missed sage.config VaultIdentity"
    assert "VaultConfig" in names, "Discovery missed sage.config VaultConfig"
    assert len(classes) >= 30, (
        f"Discovery surfaced only {len(classes)} models; expected at least 30. "
        "Did discovery filtering regress?"
    )


@pytest.mark.parametrize(
    "cls,field_name,expected_alias",
    [
        pytest.param(cls, field, expected, id=f"{cls.__name__}.{field}")
        for cls, field, expected in _shape_bearing_fields()
    ],
)
def test_typed_alias_coverage(cls: type[BaseModel], field_name: str, expected_alias: type) -> None:
    """Every shape-bearing field is typed or pinned in KNOWN_VIOLATIONS."""
    key = (cls.__name__, field_name)
    has_alias = _field_has_typed_alias(cls, field_name)
    allowlisted = key in KNOWN_VIOLATIONS

    if has_alias and allowlisted:
        pytest.fail(
            f"{cls.__name__}.{field_name} is typed (carries an AfterValidator "
            f"from sage.models.schemas) AND is allowlisted in KNOWN_VIOLATIONS. "
            f"Remove the stale entry ({KNOWN_VIOLATIONS[key]!r})."
        )
    if has_alias:
        return
    if allowlisted:
        return

    expected_name = _alias_display_name(expected_alias)
    pytest.fail(
        f"{cls.__name__}.{field_name} is shape-bearing (registry expects "
        f"{expected_name}) but is bare `str`. Either annotate it with "
        f'{expected_name} (preferred) or add ("{cls.__name__}", '
        f'"{field_name}") to KNOWN_VIOLATIONS with a comment '
        "explaining why."
    )


def test_known_violations_reference_real_fields():
    """Every KNOWN_VIOLATIONS entry must correspond to a real shape-bearing field."""
    shape_bearing = {(cls.__name__, name) for cls, name, _ in _shape_bearing_fields()}
    stale = sorted(set(KNOWN_VIOLATIONS) - shape_bearing)
    assert not stale, (
        f"KNOWN_VIOLATIONS contains entries that do not correspond to any "
        f"shape-bearing field in sage.models.schemas: {stale}. Did the field "
        "get renamed or removed? Remove the stale entry."
    )


# ---------------------------------------------------------------------------
# Boundary-validation construction tests
#
# DiscoverRequest.document_id is the one request-side field T-0026 typed.
# Property coverage in test_alias_invariants.py locks the validator; this
# test confirms the alias is wired through at the model-construction
# boundary so that bad caller input is rejected before reaching the
# service layer (per the Typed-Alias Boundary Conventions).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "not-a-doc-id",  # no underscore, contains hyphens
        "DEADBEEF_uppercase_prefix",  # uppercase hex
        "deadbeef-no-underscore",  # hyphen instead of underscore
        "1234abcd_",  # empty slug
        "12345678_Trailing-Slash",  # slug has uppercase + hyphen
        "",  # empty string
    ],
)
def test_discover_request_document_id_rejects_non_canonical(bad_value: str) -> None:
    """Non-canonical document_id values must be rejected at request construction."""
    from pydantic import ValidationError

    from sage.models.schemas import DiscoverRequest

    with pytest.raises(ValidationError):
        DiscoverRequest(document_id=bad_value)
