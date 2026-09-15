"""The serialized response body conforms to the published schema.

Every other spec-versus-code gate in this suite compares two *declarations*
-- a Pydantic field against a YAML property, a docstring against an operation
description. None of them serializes anything, so none can see the class of
divergence this module exists for: a response body that omits a key the
published contract lists as ``required``.

That gap is not accidental. ``test_openapi_conformance`` records a deliberate
decision to leave required-versus-optional parity unasserted, on the grounds
that OpenAPI's ``required`` ("the property always appears in the serialized
JSON") and Pydantic's ``is_required()`` ("the caller must supply it at
construction time") do not map cleanly onto one another. That reasoning is
sound for a plain ``model_dump``. It fails for a serializer that deletes null
fields, which breaks the *always appears* half directly and silently.

The resolution this module enforces: **the published schema decides whether a
key appears on the wire.** A field declared required always carries its key,
null or not. A field declared optional may be omitted when its value is null.
The way to check that is not to compare the two declarations but to serialize a
response and validate it, which is what these tests do.

Invariants
----------

W1  A serialized response validates against its declared component schema.
W2  W1 compared enough schemas to mean something (vacuity floor).
W3  Every enrolled model could actually be built (names what W2 only counts).
W4  One model has one wire shape: a preview returned directly and the same
    preview nested in a batch summary render identically.
W5  A required-and-nullable key survives serialization, carrying null.
W6  An optional-and-nullable key is still omitted when null.
W7  The rule reaches through nested models and lists of models, on the tool
    path and the event-stream path alike.
W7b The rule reaches a model held as a value in a mapping.
W8  The dump and the rule are keyed alike, so an aliased field is reached.
W9  A model field and its spec property agree on whether null is admitted,
    request and response components alike, in both directions.
W9a Each declaration form of null, spec and model side, is read correctly.
W9b The nullability allowlist carries no stale entry.
W10 A Core API route renders its model under the per-field rule, keeping its
    status, its headers, a sync endpoint and a returned ``Response``.
W11 Every route either built application serves is on that route class.

W5 and W6 are the two halves of the per-field rule, and they fail under
opposite mistakes. An implementation that keeps every key passes W5 and fails
W6; one that keeps none passes W6 and fails W5. Neither half is redundant with
W1, which reports a schema violation several layers below the cause.

**Further rivals pass both of them, and only the depth tests exclude them**: a
rule that reads each field's declaration correctly but stops short of some way a
model can be nested. W5 and W6 both read a top-level model, so a prune that never
descends, one that descends into a nested model but not into a list of them, and
one blind to a model held as a dict *value* all satisfy each of them while
leaving nested optional nulls on the wire. Measured, not reasoned: each rival was
written, and each passed the whole module until W7 gained an assertion that a
nested optional key is *gone* and W7b covered the mapping. Checking only that
required keys survive at depth is not enough, because a rule that does nothing at
depth leaves them there too.

W9 excludes a rival no rendering can: a surface that omits optional nulls
paired with a schema declaring those properties non-nullable. Both transports
omit an optional null, so no body ever puts one against its declaration, and
only a comparison of the declarations themselves sees the disagreement. W11
excludes the rival W10 cannot see: a router left off the route class, which
keeps sending every optional null with every other test green.

Read the pairings as the gate and this paragraph as a claim to re-audit. The
inventory above has been accurate and incomplete twice.

Two tests here are inert by construction: the divergence allowlists are empty,
so their staleness checks loop zero times and pass against any implementation.
Each is a guard for entries that do not exist yet, in the manner of the other
empty allowlists in this suite, and they are named here rather than left to look
like coverage.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import re
import types
import typing
from pathlib import Path
from typing import Any, Final

import jsonschema
import pytest
import referencing
import referencing.jsonschema
import yaml
from fastapi import Response
from pydantic import BaseModel

from sage.mcp_server import _serialize
from sage.models.schemas import BatchIngestFileError, TraverseResponse
from sage.models.wire import to_wire
from sage.services.batch_ingest import IngestSummary

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"

# Vacuity floor for W1/W2. Seventy-six components are reachable from a
# declared response and backed by a same-named model today, across both
# specs; the floor sits far enough below that ordinary movement does not
# trip it, while a reflection helper returning nothing -- or a factory that
# fails on every model -- does. Without this, W1 would pass over an empty
# loop, which is the one failure shape a gate like this must not have.
MIN_MODELS_VALIDATED: Final[int] = 60

# Components whose serialized shape is not checked, each with the reason.
# Empty by intent: an entry here is an admission that some response body is
# not held to its own published contract, and should be justified in review
# rather than added quietly.
KNOWN_WIRE_SHAPE_DIVERGENCE: Final[dict[tuple[str, str], str]] = {}


# ---------------------------------------------------------------------------
# Spec loading and $ref resolution
# ---------------------------------------------------------------------------


def _load_spec(path: Path) -> dict | None:
    """Parsed spec, or None when the file is absent."""
    if not path.exists():
        return None
    with path.open() as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def sage_core_spec() -> dict | None:
    return _load_spec(SAGE_CORE_SPEC_PATH)


@pytest.fixture(scope="module")
def cas_app_spec() -> dict | None:
    return _load_spec(CAS_APP_SPEC_PATH)


_SPEC_BASE_URI: Final[str] = "urn:cas:openapi-spec"


def _registry_for(spec: dict) -> referencing.Registry:
    """A registry that resolves this spec's own ``#/components/schemas`` refs.

    The sibling helper in ``tests/helpers/schema_validation`` builds a registry
    over the standalone ``*.schema.json`` config files; OpenAPI components live
    inside the spec document instead. The whole document is registered under a
    base URI, and each component is addressed through that URI rather than by a
    bare ``#/...`` pointer -- a bare pointer resolves against the one-key
    wrapper schema the validator is built on, which contains nothing.
    """
    resource = referencing.Resource.from_contents(
        spec, default_specification=referencing.jsonschema.DRAFT202012
    )
    return referencing.Registry().with_resources([(_SPEC_BASE_URI, resource)])


def _validator_for(spec: dict, schema_name: str) -> jsonschema.Draft202012Validator:
    """A validator for one component, strict about declared formats.

    ``format_checker`` is live and currently catches nothing, which is the
    correct state for it: every sentinel is well-formed, so nothing here
    violates a ``date-time``. It is a guard rather than dead weight -- drop
    it and a response sending a malformed timestamp against a
    ``format: date-time`` property passes -- so it stays.
    """
    schema = {"$ref": f"{_SPEC_BASE_URI}#/components/schemas/{schema_name}"}
    return jsonschema.Draft202012Validator(
        schema,
        registry=_registry_for(spec),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


# ---------------------------------------------------------------------------
# Sentinel instance construction
# ---------------------------------------------------------------------------


class UnbuildableModel(Exception):
    """No sentinel could be produced for a field of this model.

    Raised rather than returned so a model drops out of the sweep loudly.
    A factory that silently skipped would shrink W1's coverage without
    failing anything, which is exactly the vacuity W2 and W3 exist to catch.
    """


def _non_null_args(annotation: Any) -> tuple[Any, ...]:
    """The non-``None`` members of a union annotation, or the annotation."""
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        return tuple(a for a in typing.get_args(annotation) if a is not type(None))
    return (annotation,)


def _is_nullable(annotation: Any) -> bool:
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        return type(None) in typing.get_args(annotation)
    return annotation is None or annotation is type(None)


def _unwrap_annotated(annotation: Any) -> Any:
    """Strip ``Annotated[...]`` down to its underlying type."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _string_sentinel(field_name: str, constraints: dict) -> str:
    """A string that satisfies the constraints the spec actually declares.

    The specs carry four ``pattern`` constraints (all sha256 digests) and
    twenty ``format`` declarations (date-time and date). Where the property
    states one, it decides; otherwise the field-name conventions the
    typed-alias registry uses pick the shape family.

    The sentinel must be a *valid* instance of the declared property, or a
    failure in the sweep is unattributable: a value the schema was always
    going to reject says nothing about how the response was serialized.
    """
    if "format" in constraints:
        fmt = constraints["format"]
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
    if "pattern" in constraints and "sha256" in constraints["pattern"]:
        return "sha256:" + "0" * 64
    if field_name.endswith("_hash") or field_name in {"sha256", "hashes"}:
        return "sha256:" + "0" * 64
    if field_name.endswith("_at") or field_name.endswith("_time"):
        return "2026-01-01T00:00:00Z"
    if field_name.endswith("_date"):
        return "2026-01-01"
    if field_name.endswith("_id") or field_name == "id":
        return "0123abcd_sentinel_document"
    return "sentinel"


def _sentinel_for(
    annotation: Any,
    field_name: str,
    constraints: dict | None = None,
    depth: int = 0,
    spec: dict | None = None,
) -> Any:
    """A value of the declared type, for a required non-nullable field.

    ``constraints`` is the property's own declaration from the spec, when one
    is available. It is consulted ahead of the annotation for enumerations and
    numeric bounds, because the annotation alone cannot say that an integer
    field has a minimum of one or that a bare ``str`` is really an enum on the
    wire -- both of which the specs declare and both of which would otherwise
    produce sentinels the schema rejects for reasons that have nothing to do
    with serialization.
    """
    if depth > 6:
        raise UnbuildableModel(f"{field_name}: annotation nests too deeply")

    constraints = constraints or {}
    annotation = _unwrap_annotated(annotation)
    origin = typing.get_origin(annotation)

    if origin is typing.Literal:
        return typing.get_args(annotation)[0]

    if origin in (types.UnionType, typing.Union):
        for member in _non_null_args(annotation):
            try:
                return _sentinel_for(member, field_name, constraints, depth + 1, spec)
            except UnbuildableModel:
                continue
        raise UnbuildableModel(f"{field_name}: no union member could be built")

    if origin in (list, set, frozenset, tuple):
        return []
    if origin is dict:
        return {}

    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            # The member, not its value: ``model_construct`` skips coercion,
            # so handing over a bare string leaves the field holding a type
            # its own serializer did not expect.
            return next(iter(annotation))
        if issubclass(annotation, BaseModel):
            # A nested model is a component in its own right, so it gets its
            # own declared properties rather than inheriting the parent's --
            # otherwise an enum-on-the-wire field one level down draws an
            # unconstrained string and fails for a fixture reason.
            nested_props = _properties_for(spec, annotation.__name__) if spec is not None else {}
            return _build_sentinel(annotation, nested_props, depth=depth + 1, spec=spec)
        if issubclass(annotation, bool):
            return False
        if issubclass(annotation, int):
            return int(constraints.get("minimum", 0))
        if issubclass(annotation, float):
            return float(constraints.get("minimum", 0.0))
        if issubclass(annotation, str):
            if constraints.get("enum"):
                return constraints["enum"][0]
            return _string_sentinel(field_name, constraints)
        if issubclass(annotation, dt.datetime):
            return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        if issubclass(annotation, dt.date):
            return dt.date(2026, 1, 1)
        if annotation in (list, set, frozenset, tuple):
            return []
        if annotation is dict:
            return {}
        if annotation is Any or annotation is object:
            return "sentinel"

    if annotation is Any:
        return "sentinel"

    raise UnbuildableModel(f"{field_name}: no sentinel rule for {annotation!r}")


def _build_sentinel(
    model: type[BaseModel],
    properties: dict | None = None,
    depth: int = 0,
    spec: dict | None = None,
) -> BaseModel:
    """An instance with every nullable field null and everything else filled.

    Null is the whole point: the divergence this module hunts is only visible
    on a response whose nullable fields are actually null, which is the state a
    serializer that deletes nulls renders differently from the schema.

    Built with ``model_construct`` so field validators do not reject a
    sentinel. Validation is not what is under test here -- serialization is --
    and a validator-satisfying value for every typed alias would be a second
    fixture surface to keep current for no gain.
    """
    properties = properties or {}
    values: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if _is_nullable(field.annotation):
            values[name] = None
        else:
            values[name] = _sentinel_for(field.annotation, name, properties.get(name), depth, spec)
    return model.model_construct(**values)


def _referenced_names(node: Any) -> set[str]:
    """Component names any ``$ref`` under this node points at."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/components/schemas/" in ref:
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _referenced_names(value)
    elif isinstance(node, list):
        for value in node:
            found |= _referenced_names(value)
    return found


_EVENT_COMPONENT_RE: Final = re.compile(r"components\.schemas\.([A-Za-z_][A-Za-z0-9_]*)")


def _event_stream_components(spec: dict) -> set[str]:
    """Components an event-stream response names in prose rather than by ref.

    An SSE response cannot declare its payload structurally -- the body is a
    sequence of ``data:`` lines, so the schema is ``type: string`` and the
    event components are named in the description instead. Those payloads are
    serialized responses like any other and belong in scope; a purely
    ``$ref``-driven walk would miss every one of them, which is how a whole
    delivery path could sit outside a gate that looked thorough.
    """
    found: set[str] = set()
    for path_item in (spec.get("paths") or {}).values():
        for operation in (path_item or {}).values():
            if not isinstance(operation, dict):
                continue
            for response in (operation.get("responses") or {}).values():
                for media_type, media in ((response or {}).get("content") or {}).items():
                    if "event-stream" not in media_type:
                        continue
                    description = ((media or {}).get("schema") or {}).get("description", "")
                    found |= set(_EVENT_COMPONENT_RE.findall(description or ""))
    return found


def _response_components(spec: dict) -> set[str]:
    """Components reachable from some operation's declared response body.

    Scope, derived rather than guessed. The rule under test governs what the
    server *sends*, so a component that only ever arrives -- a request body, a
    patch, an item inside one -- is outside it: nothing serializes those, and
    an all-null instance of a patch model legitimately renders as the empty
    object its own schema forbids a caller to send.

    Naming was the tempting shortcut and it does not hold: ``BulkLifecycleItem``
    is a request item while ``BulkLifecycleItemResult`` is a response, and the
    two differ by a suffix. Reachability answers it exactly, once the
    event-stream payloads are folded in beside the structural refs.
    """
    components = (spec.get("components") or {}).get("schemas") or {}
    seen: set[str] = set()
    frontier: set[str] = set()

    for path_item in (spec.get("paths") or {}).values():
        for operation in (path_item or {}).values():
            if not isinstance(operation, dict):
                continue
            frontier |= _referenced_names(operation.get("responses") or {})

    frontier |= _event_stream_components(spec) & set(components)

    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        frontier |= _referenced_names(components.get(name) or {}) - seen

    return seen


def _enrolled_models(spec: dict, classes: dict[str, type]) -> dict[str, type[BaseModel]]:
    """Response components that have a same-named model."""
    components = (spec.get("components") or {}).get("schemas") or {}
    reachable = _response_components(spec)
    return {
        name: classes[name]
        for name in components
        if name in reachable and name in classes and issubclass(classes[name], BaseModel)
    }


def _resolve_property(prop: Any, spec: dict, depth: int = 0) -> dict:
    """Follow a property's ``$ref`` to the constraints it actually carries.

    A property declared as a bare ``$ref`` to a shared enum component states
    nothing inline, so a factory reading only inline keywords draws an
    unconstrained string for a field the wire restricts to eight values. The
    Pydantic side gives no help here: several of these fields are annotated
    plain ``str`` and carry their vocabulary only in the spec.
    """
    if not isinstance(prop, dict) or depth > 4:
        return prop if isinstance(prop, dict) else {}
    ref = prop.get("$ref")
    if not isinstance(ref, str) or "/components/schemas/" not in ref:
        return prop
    target = ((spec.get("components") or {}).get("schemas") or {}).get(ref.rsplit("/", 1)[-1])
    return _resolve_property(target or {}, spec, depth + 1)


def _properties_for(spec: dict, schema_name: str) -> dict:
    """The component's own properties, ``allOf`` and ``$ref`` resolved."""
    from tests.sage.test_openapi_conformance import _flatten_yaml_properties

    components = (spec.get("components") or {}).get("schemas") or {}
    flat = _flatten_yaml_properties(components.get(schema_name) or {}, spec)
    return {name: _resolve_property(prop, spec) for name, prop in flat.items()}


def _sage_classes() -> dict[str, type]:
    from tests.sage.test_openapi_conformance import _sage_pydantic_classes

    return _sage_pydantic_classes()


def _cas_app_classes() -> dict[str, type]:
    from tests.sage.test_openapi_conformance import _cas_app_pydantic_classes

    return _cas_app_pydantic_classes()


def _surfaces(sage_core_spec: dict | None, cas_app_spec: dict | None):
    """(label, spec, enrolled models) for each published surface."""
    out = []
    for label, spec, classes in (
        ("sage_core", sage_core_spec, _sage_classes),
        ("cas_app", cas_app_spec, _cas_app_classes),
    ):
        if spec is None:
            continue
        out.append((label, spec, _enrolled_models(spec, classes())))
    return out


def _renderings(instance: BaseModel) -> tuple[tuple[str, dict], ...]:
    """Both ways the surface renders one model, labelled by arm.

    One helper rather than two inline expressions, because every test that
    asks "does this conform?" has to mean the same thing by it. W1 reports a
    violation on either arm; W2 calls an exemption stale only when both are
    clean. Rendered separately, the two would disagree about what a violation
    is, and the allowlist would reject the entries it exists to hold.

    The REST arm is the Core API route class's own rendering, declared as the
    instance's model, so the arm tests what a route sends rather than a dump no
    route performs.
    """
    from sage.api.wire_route import render_response

    return (
        ("mcp", _serialize(instance)),
        ("rest", render_response(type(instance), instance)),
    )


# ---------------------------------------------------------------------------
# W1 / W2: serialized bodies validate against their declared components
# ---------------------------------------------------------------------------


def test_serialized_response_validates_against_declared_schema(
    sage_core_spec: dict | None, cas_app_spec: dict | None
):
    """W1 -- what the surface sends satisfies what the surface published.

    For each component with a same-named model, an instance whose nullable
    fields are all null is rendered **both ways the surface renders** and each
    rendering is validated against the component. A required key a serializer
    dropped surfaces here as a missing property; a null a declaration does not
    admit surfaces as a type violation.

    The two arms send the same body under the same rule by different code: the
    MCP serializer dumps the runtime model, the REST route class validates and
    dumps as the declared type. Each can drop a required key or break a declared
    type on its own, so each is validated.

    Neither arm ever puts a null against an optional property's declared type,
    because both omit it. That property's nullability is W9's to hold, by
    comparing the model's declaration with the spec's directly; this test
    guards required keys, required-and-nullable values, and every non-null
    value's type and format.

    This is the check no sibling gate performs: every one of them compares a
    declaration to a declaration and cannot see the wire at all.
    """
    violations: list[str] = []
    validated = 0

    for label, spec, models in _surfaces(sage_core_spec, cas_app_spec):
        for name, model in sorted(models.items()):
            if (label, name) in KNOWN_WIRE_SHAPE_DIVERGENCE:
                continue
            try:
                instance = _build_sentinel(model, _properties_for(spec, name), spec=spec)
            except UnbuildableModel:
                # Reported by W3, which names them; counted by W2, which
                # fails if too many drop out. Skipping silently here would
                # let coverage erode without any test going red.
                continue
            validated += 1
            validator = _validator_for(spec, name)
            for arm, body in _renderings(instance):
                errors = sorted(
                    validator.iter_errors(body),
                    key=lambda e: list(e.absolute_path),
                )
                for error in errors:
                    where = "/".join(str(p) for p in error.absolute_path) or "(root)"
                    violations.append(f"  {label}/{arm}: {name} at {where} -> {error.message}")

    assert validated >= MIN_MODELS_VALIDATED, (
        f"only {validated} models were serialized and validated; the "
        "reflection helpers or the sentinel factory returned little or "
        "nothing, so this test passed over an almost-empty loop rather "
        "than finding no violations"
    )
    assert not violations, (
        "serialized response bodies that do not satisfy their own published "
        "schema (the surface sends this; the contract forbids it):\n" + "\n".join(violations)
    )


def test_wire_shape_divergence_allowlist_has_no_stale_entries(
    sage_core_spec: dict | None, cas_app_spec: dict | None
):
    """W2 -- every allowlist entry names a component that exists and fails.

    An entry for a component that no longer exists, or one that now conforms,
    is an exemption protecting nothing. Left in place it would hide the next
    real divergence on that component.

    "Conforms" has to mean what W1 means by it, which is **both** renderings,
    and the two must read the same helper rather than each render their own.
    An entry exempting a REST-arm-only violation is exactly what the allowlist
    exists for, and a staleness check that rendered only the MCP arm would see
    a valid body and reject that entry as stale -- the escape hatch and the
    gate disagreeing about what a violation is.
    """
    stale: list[str] = []
    for label, spec, models in _surfaces(sage_core_spec, cas_app_spec):
        for allow_label, name in KNOWN_WIRE_SHAPE_DIVERGENCE:
            if allow_label != label:
                continue
            model = models.get(name)
            if model is None:
                stale.append(f"  {label}: {name} -> no such component/model pair")
                continue
            try:
                instance = _build_sentinel(model, _properties_for(spec, name), spec=spec)
            except UnbuildableModel:
                continue
            validator = _validator_for(spec, name)
            if all(validator.is_valid(body) for _arm, body in _renderings(instance)):
                stale.append(f"  {label}: {name} -> now conforms; remove the entry")

    assert not stale, "stale KNOWN_WIRE_SHAPE_DIVERGENCE entries:\n" + "\n".join(stale)


def test_event_stream_scope_extraction_has_teeth(
    sage_core_spec: dict | None, cas_app_spec: dict | None
):
    """W2b -- the event-stream arm of the scope rule actually finds something.

    Event payloads are named in prose, so this arm depends on a description
    convention rather than on structure. If that convention is reworded, the
    extraction returns nothing, every event component silently leaves scope,
    and W1 keeps passing on a smaller surface -- the exact erosion the vacuity
    floor is too coarse to notice, because the floor counts dozens and this
    would cost a handful.
    """
    for label, spec, models in _surfaces(sage_core_spec, cas_app_spec):
        named = _event_stream_components(spec)
        components = set((spec.get("components") or {}).get("schemas") or {})
        classes = _sage_classes() if label == "sage_core" else _cas_app_classes()
        if not (components & {"SummaryEvent", "ProgressEvent"}):
            continue
        assert named & components, (
            f"{label}: the spec declares event components but the event-stream "
            "response descriptions named none of them, so the scope rule's "
            "prose arm found nothing; check the 'components.schemas.X' "
            "convention in the text/event-stream response description"
        )
        # And that what it found actually reached the sweep. Finding the names
        # and then dropping them is the same erosion by a different route, and
        # the vacuity floor is far too coarse to notice a handful leaving.
        expected = {n for n in named & components if n in classes}
        missing = expected - set(models)
        assert not missing, (
            f"{label}: event components named by an event-stream response are "
            f"not in the swept set, so a whole delivery path sits outside this "
            f"gate: {sorted(missing)}"
        )


def test_every_enrolled_model_can_be_built(sage_core_spec: dict | None, cas_app_spec: dict | None):
    """W3 -- the sentinel factory covers every model W1 sweeps.

    W2's floor says *how many* models were checked; this says *which* were
    not. A model the factory cannot build is invisible to W1, so a new field
    type that defeats the factory must be reported as a gap in the gate
    rather than quietly shrinking its reach.
    """
    unbuildable: list[str] = []
    for label, spec, models in _surfaces(sage_core_spec, cas_app_spec):
        for name, model in sorted(models.items()):
            try:
                _build_sentinel(model, _properties_for(spec, name), spec=spec)
            except UnbuildableModel as exc:
                unbuildable.append(f"  {label}: {name} -> {exc}")

    assert not unbuildable, (
        "models the sentinel factory could not build, so the serialized-shape "
        "gate never reached them; add a rule to _sentinel_for:\n" + "\n".join(unbuildable)
    )


# ---------------------------------------------------------------------------
# Fixtures for the field-level invariants
# ---------------------------------------------------------------------------


def _null_preview():
    """An ingest preview whose every nullable field, at both depths, is null.

    Both depths matter. The three nullable fields on the preview itself are
    required-and-nullable, and so is ``permitted_source_types`` on the nested
    requirements -- so this one object exercises the rule at the top level and
    one model down.
    """
    from sage.models.schemas import DocTypeRequirements, IngestPreview

    requirements = DocTypeRequirements(
        doc_type="note",
        is_declared=True,
        has_metadata_schema=False,
        declared_tier3_fields=[],
        required_tier3_fields=[],
        unique_tier3_fields=[],
        permitted_source_types=None,
    )
    return IngestPreview(
        dry_run=True,
        would_create=True,
        resolved_doc_type="note",
        resolved_source_type="markdown",
        source_content_hash=None,
        duplicate_of=None,
        predecessor_id=None,
        would_supersede=False,
        tier3_validated=False,
        requirements=requirements,
    )


# ---------------------------------------------------------------------------
# W4: one model, one wire shape
# ---------------------------------------------------------------------------


def test_preview_renders_identically_direct_and_nested():
    """W4 -- a preview looks the same returned alone or inside a batch.

    The single-ingest tool returns a preview through the shared serializer;
    the batch tool returns a summary dict that carries previews inside it. Two
    render paths to one declared model, and a caller writing one parser for it
    meets any difference between them immediately.

    Values are compared, not only keys: the batch path assembles its dict by
    hand, so a dump there that forgets JSON mode leaves an enum member where
    the tool path puts a string -- same keys, different wire.
    """
    preview = _null_preview()

    direct = _serialize(preview)

    summary = IngestSummary()
    summary.dry_run = True
    summary.previews = [preview]
    nested = summary.to_dict()["previews"][0]

    assert set(direct) == set(nested), (
        "the same preview carries different keys depending on how it is "
        f"reached: direct-only {sorted(set(direct) - set(nested))}, "
        f"nested-only {sorted(set(nested) - set(direct))}"
    )
    assert direct == nested, (
        "the same preview carries different values depending on how it is "
        "reached:\n"
        + "\n".join(
            f"  {k}: direct={direct[k]!r} nested={nested[k]!r}"
            for k in sorted(direct)
            if direct[k] != nested[k]
        )
    )
    # Equality is not enough to catch a missing JSON mode. `SourceType` is a
    # `StrEnum`, so an enum member compares equal to its own value and
    # serializes through `json.dumps` -- the batch path could hand the
    # transport a dict with a live enum inside it and every assertion above
    # would still hold. The type is the only thing that shows it.
    for label, body in (("direct", direct), ("nested", nested)):
        assert type(body["resolved_source_type"]) is str, (
            f"the {label} path left resolved_source_type as "
            f"{type(body['resolved_source_type']).__name__}; a dict handed to "
            "the transport with an enum still in it is not a wire shape"
        )

    # And an optional field, which `IngestPreview` itself has none of: the
    # errors beside the previews in the same summary carry two, so the batch
    # path's own rendering of a nullable field is compared here too.
    error = BatchIngestFileError(
        file_index=0, filename="bad.md", source_path="/in/bad.md", message="boom"
    )
    summary.errors = [error]
    assert _serialize(error) == summary.to_dict()["errors"][0], (
        "a batch summary renders its nested error entries differently from "
        "the way the same model is rendered on its own"
    )


# ---------------------------------------------------------------------------
# W5 / W6: the two halves of the per-field rule
# ---------------------------------------------------------------------------


def test_required_nullable_key_is_present_and_null():
    """W5 -- a required key survives even when its value is null.

    The schema calls these required, which means the key always appears; the
    field descriptions rely on it. A null ``duplicate_of`` is the reported
    verdict *no document holds these bytes*, and a caller cannot read that
    verdict off a key that is not there.
    """
    body = _serialize(_null_preview())

    for field in ("source_content_hash", "duplicate_of", "predecessor_id"):
        assert field in body, (
            f"{field} is declared required, so its key must appear on the "
            "wire even when the value is null; the serializer deleted it"
        )
        assert body[field] is None

    assert "permitted_source_types" in body["requirements"], (
        "the rule must hold one model down as well: permitted_source_types "
        "is required on the nested requirements and null means unconstrained"
    )
    assert body["requirements"]["permitted_source_types"] is None


def test_optional_nullable_key_is_omitted():
    """W6 -- an optional key is still dropped when null.

    The economy half of the rule, and the half a careless fix loses. Simply
    removing the null-dropping rule outright satisfies every other test in
    this module while adding a key to two hundred-odd optional fields across
    the surface -- inflating every response and invalidating the inline-size
    budget the retrieval hints are fitted to.
    """
    from sage.models.schemas import TraverseResponse

    body = _serialize(TraverseResponse(start_id="0123abcd_sentinel", nodes=[]))

    assert "resolution_path" not in body, (
        "resolution_path is optional and null here, so the wire omits it; "
        "keeping it means the null rule stopped reading the declaration"
    )
    assert "start_id" in body and "nodes" in body


# ---------------------------------------------------------------------------
# W7: the rule reaches through nesting, lists, and the event stream
# ---------------------------------------------------------------------------


def test_rule_reaches_through_nested_models_and_lists_on_the_event_stream():
    """W7 -- nesting and list membership do not exempt a field.

    The rule the serializer replaced recursed into nested models, so its
    replacement must too, and by a path that is easy to get wrong: a preview
    reaches the event stream inside a *list* on a summary event, one model
    below the one being dumped.

    A shallow implementation passes W5 -- which reads a top-level model -- and
    fails only here.

    Both halves are asserted at depth, and the omission half is the one that
    matters: a prune that stops at the top level *keeps* every nested null, so
    a test that only checked required keys were present would pass a
    non-recursive implementation and a list-blind one alike. Checking that an
    optional nested null is gone is what distinguishes "the rule ran here"
    from "nothing ran here".
    """
    from sage.models.schemas import SummaryEvent
    from sage.services.batch_ingest_stream import _sse_event

    preview = _null_preview()
    # `code` and `detail` are optional on the error entry and left null, so
    # this list element carries a key the rule must remove -- the only shape
    # in the payload that a prune failing to descend would leave behind.
    error = BatchIngestFileError(
        file_index=0, filename="bad.md", source_path="/in/bad.md", message="boom"
    )
    event = SummaryEvent(
        event_type="summary",
        documents_created={"new": 0, "new_version": 0},
        metadata_pending=0,
        edges_created={},
        edges_staged={},
        edges_removed=0,
        edges_dropped=0,
        abstracts_generated=0,
        abstracts_deferred=0,
        error_count=1,
        errors=[error],
        dry_run=True,
        previews=[preview],
        edge_warnings=None,
    )

    payload = json.loads(_sse_event(event).removeprefix("data: ").strip())
    nested = payload["previews"][0]
    nested_error = payload["errors"][0]

    assert "duplicate_of" in nested and nested["duplicate_of"] is None, (
        "a required-and-nullable key lost its place inside a list of nested "
        "models; the rule is not recursing"
    )
    assert "permitted_source_types" in nested["requirements"], (
        "two models down, inside a list: the rule stopped short"
    )
    assert "code" not in nested_error and "detail" not in nested_error, (
        "an optional null survived inside a list element, so the rule did not "
        f"reach it: {sorted(nested_error)}"
    )
    assert "edge_warnings" not in payload, (
        "edge_warnings is optional and null, so the event still omits it"
    )


def test_the_dump_is_keyed_the_way_the_rule_reads_it():
    """W8 -- an aliased field is dumped and looked up under the same key.

    The rule reads each field's declaration under the key the dump wrote, so
    the two have to agree on what that key is. They do not agree by accident:
    the dump keys by field name unless told otherwise, while the lookup asks
    for the serialization alias. Mismatched, an aliased field's declaration is
    never found, and the key falls through to the pass-through for keys no
    field claims -- carrying its optional null onto the wire, the exact
    opposite of the rule.

    No response model declares an alias today, so nothing in the enrolled
    sweep can reach this and a local model is the only way to state it. That
    is the reason to state it rather than a reason to skip it: an invariant
    holding only because nothing exercises it is one a later simplification
    drops with the whole suite green.
    """
    from pydantic import Field

    class Aliased(BaseModel):
        absent: str | None = Field(default=None, serialization_alias="absent_alias")
        present: str | None = Field(default=None, serialization_alias="present_alias")
        required_null: str | None = Field(serialization_alias="required_alias")

    body = to_wire(Aliased(present="here", required_null=None))

    assert "absent_alias" not in body and "absent" not in body, (
        "an aliased optional null survived, so the dump and the lookup are "
        f"keyed differently: {sorted(body)}"
    )
    assert body["present_alias"] == "here", (
        "an aliased value must reach the wire under its alias, which is the "
        "property name the spec declares"
    )
    assert body["required_alias"] is None, (
        "an aliased required field keeps its key under the alias, null and all"
    )


def test_rule_reaches_models_held_in_a_mapping():
    """W7b -- a model reached as a dict *value* is not exempt either.

    The third way a response nests a model, after the nested field and the
    list: a mapping keyed by something the caller chose. One tool's response
    is shaped that way -- the pending-metadata listing, whose per-field
    entries carry optional alternates -- so the mapping branch is live
    production code rather than defensive breadth.

    Split from W7 because it excludes a different rival. W7's fixtures leave a
    mapping-blind prune entirely green: deleting that branch passes every
    other assertion in this module, since no other fixture routes a model
    through a dict. The branch was reachable, unexercised, and its removal
    was silent.
    """
    from sage.models.schemas import ExtractedField, PendingMetadataItem

    entry = PendingMetadataItem.model_construct(
        document=None,
        extracted_fields={
            "title": ExtractedField(
                value="A title", source="filename", alt_value=None, alt_source=None
            )
        },
    )
    body = _serialize(entry)
    field = body["extracted_fields"]["title"]

    assert "alt_value" not in field and "alt_source" not in field, (
        "optional nulls survived inside a mapping value, so the rule does not "
        f"reach a model held as a dict value: {sorted(field)}"
    )
    assert field["value"] == "A title", (
        "the mapping branch must prune the entry, not replace or empty it"
    )


# ---------------------------------------------------------------------------
# W9: the model and the spec agree on which properties admit null
# ---------------------------------------------------------------------------

# Properties whose nullability is allowed to differ between a model and its
# component, each with the reason. Empty by intent, like the wire-shape
# allowlist above: an entry is a property on which a client validating against
# the published contract and the surface disagree about a null.
KNOWN_NULLABILITY_DIVERGENCE: Final[dict[tuple[str, str, str], str]] = {}

# Vacuity floor for W9. Several hundred properties are compared across both
# specs today; a reflection helper returning nothing, or a flatten that stops
# resolving composition, falls far below it.
MIN_NULLABILITY_FIELDS_COMPARED: Final[int] = 600

# Components W9 must have reached, from both sides of the rule. The floor
# counts; this names. A comparison scoped to response-reachable components --
# the scope every other test in this module uses -- would pass every request
# divergence by never looking at one, and a count alone cannot tell.
_NULLABILITY_REQUIRED_REACH: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("sage_core", "IngestRequest"),
        ("sage_core", "RetrievalFilters"),
        ("sage_core", "UpdateMetadataRequest"),
        ("sage_core", "BulkMetadataItem"),
        ("sage_core", "IngestPreview"),
        ("cas_app", "IngestFileItem"),
    }
)


def _absolute_refs(node: Any) -> Any:
    """A copy of a spec fragment with its local refs addressed through the spec URI.

    A property lifted out of its component carries refs such as
    ``#/components/schemas/Edge``, which resolve against whatever document the
    validator is built on. Rewriting them to the registered spec URI lets a
    fragment be validated on its own.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/"):
                out[key] = f"{_SPEC_BASE_URI}{value}"
            else:
                out[key] = _absolute_refs(value)
        return out
    if isinstance(node, list):
        return [_absolute_refs(item) for item in node]
    return node


def _spec_admits_null(prop: dict, spec: dict) -> bool:
    """Whether a 3.1 client validating against this declaration accepts null.

    Asked of a JSON Schema validator rather than read off the keywords, so every
    way a declaration can admit null counts and nothing else does: a type list,
    an ``anyOf`` or ``oneOf`` arm, a ``$ref`` to a nullable component, or no type
    constraint at all. The 3.0 ``nullable`` keyword is not part of 3.1 and the
    validator ignores it, which is the reading a generated client gives it too.
    """
    validator = jsonschema.Draft202012Validator(_absolute_refs(prop), registry=_registry_for(spec))
    return validator.is_valid(None)


def _model_admits_null(field: Any) -> bool:
    """Whether the model field accepts an explicit null from a caller.

    Validated through the field's own annotation and metadata, so an
    ``Annotated`` alias, ``Optional``, ``Any`` and a constrained type all answer
    as the model would, rather than as a reading of the annotation's shape.
    """
    import pydantic

    annotation = field.annotation
    if field.metadata:
        annotation = typing.Annotated[(annotation, *field.metadata)]
    try:
        pydantic.TypeAdapter(annotation).validate_python(None)
    except pydantic.ValidationError:
        return False
    return True


def _field_key(name: str, field: Any, props: dict) -> str | None:
    """The spec property a model field corresponds to, if the component declares one."""
    for key in (field.serialization_alias, field.validation_alias, field.alias, name):
        if isinstance(key, str) and key in props:
            return key
    return None


def _surface_classes() -> tuple[tuple[str, Path, Any], ...]:
    return (
        ("sage_core", SAGE_CORE_SPEC_PATH, _sage_classes),
        ("cas_app", CAS_APP_SPEC_PATH, _cas_app_classes),
    )


def _nullability_divergences() -> tuple[dict[tuple[str, str, str], str], set[tuple[str, str]], int]:
    """Every property on which a model and its component disagree about null.

    Returns the divergences keyed ``(surface, component, property)`` with a
    rendering of which side admits null, the components reached, and the
    number of properties compared.
    """
    divergences: dict[tuple[str, str, str], str] = {}
    reached: set[tuple[str, str]] = set()
    compared = 0
    for label, path, classes_for in _surface_classes():
        spec = _load_spec(path)
        if spec is None:
            continue
        classes = classes_for()
        components = (spec.get("components") or {}).get("schemas") or {}
        for name in sorted(components):
            model = classes.get(name)
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                continue
            props = _properties_for(spec, name)
            for field_name, field in model.model_fields.items():
                key = _field_key(field_name, field, props)
                if key is None:
                    continue
                reached.add((label, name))
                compared += 1
                model_null = _model_admits_null(field)
                spec_null = _spec_admits_null(props[key], spec)
                if model_null != spec_null:
                    side = "model" if model_null else "spec"
                    divergences[(label, name, key)] = f"only the {side} admits null"
    return divergences, reached, compared


def test_model_and_spec_agree_on_nullability():
    """W9 -- a property admits null in the contract exactly when the surface does.

    Covers request and response components alike, which is the point: the
    serialized-shape sweep above reaches only what the server sends, so a
    request model accepting a null its published property refuses -- a body a
    spec-validating client will not build, served by a surface that accepts it
    -- was visible to no gate at all.

    Both directions are asserted. A model wider than the spec is a contract
    narrower than the surface; a spec wider than the model is a null a client
    is told it may send and is refused for. For an optional request property a
    null means the same as the property's absence, so the rule costs a caller
    nothing and removes a disagreement a generated client meets immediately.

    For a response property the same agreement is what keeps a nullable field
    declared nullable now that no transport sends an optional null: the value
    still reaches a caller on a required field, and a model is one declaration
    whichever way the body travels.
    """
    divergences, reached, compared = _nullability_divergences()

    assert compared >= MIN_NULLABILITY_FIELDS_COMPARED, (
        f"only {compared} properties were compared for nullability; the "
        "reflection helpers returned little or nothing, so this test passed "
        "over an almost-empty loop rather than finding agreement"
    )
    missing = _NULLABILITY_REQUIRED_REACH - reached
    assert not missing, (
        "components the nullability comparison must reach, from both the "
        f"request and the response side, were never compared: {sorted(missing)}"
    )
    unexplained = {k: v for k, v in divergences.items() if k not in KNOWN_NULLABILITY_DIVERGENCE}
    assert not unexplained, (
        "properties on which a model and its published component disagree "
        "about null -- add a null arm to the spec where the model admits one, "
        "or narrow whichever side is wrong:\n"
        + "\n".join(f"  {s}: {c}.{p} -> {why}" for (s, c, p), why in sorted(unexplained.items()))
    )


def test_nullability_divergence_allowlist_has_no_stale_entries():
    """W9b -- every allowlist entry names a property that still diverges.

    Inert while the allowlist is empty, like W2: the loop runs zero times. It is
    the guard for an entry that outlives the divergence it was admitted for.
    """
    divergences, _reached, _compared = _nullability_divergences()
    stale = sorted(k for k in KNOWN_NULLABILITY_DIVERGENCE if k not in divergences)
    assert not stale, f"stale KNOWN_NULLABILITY_DIVERGENCE entries: {stale}"


_NULL_TEST_SPEC: Final[dict] = {
    "components": {
        "schemas": {
            "Thing": {"type": "object", "properties": {"id": {"type": "string"}}},
            "MaybeThing": {"anyOf": [{"$ref": "#/components/schemas/Thing"}, {"type": "null"}]},
        }
    }
}


@pytest.mark.parametrize(
    ("declaration", "admits_null"),
    [
        ({"type": ["string", "null"]}, True),
        ({"anyOf": [{"$ref": "#/components/schemas/Thing"}, {"type": "null"}]}, True),
        ({"oneOf": [{"type": "integer"}, {"type": "null"}]}, True),
        ({"description": "Any value, including null."}, True),
        ({"$ref": "#/components/schemas/MaybeThing"}, True),
        ({"type": "string"}, False),
        ({"$ref": "#/components/schemas/Thing"}, False),
        ({"type": "string", "nullable": True}, False),
        ({"type": "array", "items": {"type": "string"}}, False),
    ],
    ids=[
        "type-list",
        "anyOf-ref-null",
        "oneOf-null",
        "untyped",
        "ref-to-nullable",
        "plain-type",
        "ref-to-object",
        "openapi-3.0-nullable-is-not-3.1",
        "array",
    ],
)
def test_spec_null_reader_classifies_each_form(declaration: dict, admits_null: bool):
    """W9a (spec side) -- each way a declaration admits null, and the 3.0 keyword that does not."""
    assert _spec_admits_null(declaration, _NULL_TEST_SPEC) is admits_null


def test_model_null_reader_classifies_each_form():
    """W9a (model side) -- each annotation shape answers as the model validates it."""
    from pydantic import BeforeValidator

    from sage.models.schemas import DocumentDateStr

    class Shapes(BaseModel):
        union: str | None = None
        optional: typing.Optional[int] = None  # noqa: UP045 -- the spelling under test
        aliased: DocumentDateStr = None
        anything: Any = None
        plain: str = "x"
        listed: list[str] = []
        literal: typing.Literal["a", "b"] = "a"
        refuses: typing.Annotated[str | None, BeforeValidator(_refuse_null)] = None

    expected = {
        "union": True,
        "optional": True,
        "aliased": True,
        "anything": True,
        "plain": False,
        "listed": False,
        "literal": False,
        "refuses": False,
    }
    actual = {name: _model_admits_null(field) for name, field in Shapes.model_fields.items()}
    assert actual == expected


def _refuse_null(value: Any) -> Any:
    if value is None:
        raise ValueError("null refused")
    return value


# ---------------------------------------------------------------------------
# W10 / W11: the Core API sends the same wire shape the MCP surface does
# ---------------------------------------------------------------------------


def _wire_app(router_setup: typing.Callable[[Any], None]):
    """An application over one router built on the Core API's route class."""
    from fastapi import APIRouter, FastAPI

    from sage.api.wire_route import WireRoute

    router = APIRouter(route_class=WireRoute)
    router_setup(router)
    app = FastAPI()
    app.include_router(router)
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_rest_route_keeps_a_required_null_and_omits_an_optional_one():
    """W10 -- a route renders its model under the per-field rule, at every depth.

    The same three shapes the MCP tests above pin, sent through a real route:
    a required-and-nullable key survives carrying null, an optional null is
    omitted, and the omission reaches into each element of a list. Blanket
    ``exclude_none`` passes the second and third and fails the first; FastAPI's
    own serialization passes the first and fails the others; a prune that does
    not descend passes the first two and fails the third.
    """
    from sage.models.schemas import IngestPreview, TraverseResponse

    def setup(router):
        @router.get("/preview", response_model=IngestPreview)
        async def preview():
            return _null_preview()

        @router.get("/traverse", response_model=TraverseResponse)
        async def traverse():
            return TraverseResponse(start_id="0123abcd_sentinel", nodes=[])

        @router.get("/errors", response_model=list[BatchIngestFileError])
        async def errors():
            return [
                BatchIngestFileError(
                    file_index=0, filename="bad.md", source_path="/in/bad.md", message="boom"
                )
            ]

    async with _client(_wire_app(setup)) as client:
        preview_body = (await client.get("/preview")).json()
        traverse_body = (await client.get("/traverse")).json()
        [error_body] = (await client.get("/errors")).json()

    assert "duplicate_of" in preview_body and preview_body["duplicate_of"] is None, (
        "a required-and-nullable key must reach the REST wire carrying null"
    )
    assert preview_body["requirements"]["permitted_source_types"] is None

    assert "resolution_path" not in traverse_body, (
        "an optional null must be omitted from a REST body as it is from MCP"
    )
    assert traverse_body["start_id"] == "0123abcd_sentinel"

    assert "code" not in error_body and "detail" not in error_body, (
        f"an optional null survived inside a list element: {sorted(error_body)}"
    )
    assert error_body["message"] == "boom"

    # One model, one wire shape: the REST body is the MCP body.
    assert preview_body == _serialize(_null_preview())


async def test_rest_route_keeps_status_sync_endpoints_and_returned_responses():
    """W10 -- rendering the body changes nothing else a route declares.

    A route class that builds its own response is where a declared status code,
    a status or header set on an injected ``Response``, a sync endpoint's
    threadpool dispatch, a response model declared only by the return
    annotation, or a handler's own ``Response`` is lost, and every shape
    assertion above still passes when they are.
    """
    import threading

    from fastapi.responses import PlainTextResponse

    from sage.models.schemas import TraverseResponse

    sync_threads: list[int] = []

    def setup(router):
        @router.post("/created", response_model=TraverseResponse, status_code=201)
        def created():  # sync on purpose
            sync_threads.append(threading.get_ident())
            return TraverseResponse(start_id="0123abcd_sentinel", nodes=[])

        @router.get("/inferred")
        async def inferred() -> TraverseResponse:
            return TraverseResponse(start_id="0123abcd_sentinel", nodes=[])

        @router.get("/raw", response_model=TraverseResponse)
        async def raw():
            return PlainTextResponse("verbatim", status_code=202)

        @router.get("/injected", response_model=TraverseResponse)
        async def injected(response: Response):
            response.status_code = 203
            response.headers["x-wire-probe"] = "kept"
            return TraverseResponse(start_id="0123abcd_sentinel", nodes=[])

    async with _client(_wire_app(setup)) as client:
        created = await client.post("/created")
        raw = await client.get("/raw")
        injected = await client.get("/injected")
        inferred = await client.get("/inferred")

    assert created.status_code == 201
    assert created.json() == {"start_id": "0123abcd_sentinel", "nodes": []}
    # A sync endpoint runs in the threadpool, not on the event loop serving
    # every other request: a route class calling it inline returns the same body.
    assert sync_threads and sync_threads[0] != threading.get_ident()

    # A model declared only by the return annotation is the route's response
    # model too; a route class reading the keyword alone would keep its nulls.
    assert inferred.json() == {"start_id": "0123abcd_sentinel", "nodes": []}

    assert raw.status_code == 202 and raw.text == "verbatim"

    # A status and a header set on an injected response reach the caller, as
    # FastAPI applies them to the response it builds itself.
    assert injected.status_code == 203
    assert injected.headers["x-wire-probe"] == "kept"
    assert injected.json() == {"start_id": "0123abcd_sentinel", "nodes": []}


def _stamp_response(response: Response) -> None:
    """A dependency that sets status, a header and a cookie on the injected response."""
    response.status_code = 203
    response.headers["x-dependency-probe"] = "set-by-dependency"
    response.set_cookie("probe", "1")


async def test_rest_route_keeps_what_a_dependency_sets_on_the_response():
    """W10 -- status, headers and cookies a dependency sets reach the caller.

    FastAPI gives every dependency and the endpoint one per-request response
    object and applies what was set on it to the response it builds. A route
    class that looks for that object among the endpoint's own arguments finds it
    only when the endpoint declares one, so a dependency's cookie is silently
    lost on an endpoint that does not -- the standard cookie-setting shape.
    """
    from fastapi import Depends

    def setup(router):
        @router.get(
            "/stamped", response_model=TraverseResponse, dependencies=[Depends(_stamp_response)]
        )
        async def stamped():
            return TraverseResponse(start_id="0123abcd_sentinel", nodes=[])

    async with _client(_wire_app(setup)) as client:
        response = await client.get("/stamped")

    assert response.status_code == 203
    assert response.headers["x-dependency-probe"] == "set-by-dependency"
    assert "probe=1" in response.headers["set-cookie"]
    assert response.json() == {"start_id": "0123abcd_sentinel", "nodes": []}


_UNIMPLEMENTED_OPTIONS: Final[list] = [
    pytest.param({"response_model_include": {"start_id"}}, id="include"),
    pytest.param({"response_model_exclude": {"nodes"}}, id="exclude"),
    pytest.param({"response_model_exclude_unset": True}, id="exclude_unset"),
    pytest.param({"response_model_exclude_defaults": True}, id="exclude_defaults"),
    pytest.param({"response_model_exclude_none": True}, id="exclude_none"),
    pytest.param({"response_model_by_alias": False}, id="by_alias_false"),
]


@pytest.mark.parametrize("option", _UNIMPLEMENTED_OPTIONS)
def test_rest_route_refuses_serialization_options_it_does_not_apply(option: dict):
    """W10 -- an option the route class would silently ignore is refused when declared.

    The route class renders a model's body itself, so FastAPI's own options for
    shaping that body never run. Accepting one would publish a route that
    behaves as though it were not there, which is the failure this class exists
    to remove; refusing it turns that into an error where the route is written.
    """
    from fastapi import APIRouter

    from sage.api.wire_route import WireRoute

    router = APIRouter(route_class=WireRoute)
    with pytest.raises(ValueError, match="WireRoute"):
        router.add_api_route(
            "/refused",
            lambda: None,
            methods=["GET"],
            response_model=TraverseResponse,
            **option,
        )


def test_rest_route_refuses_a_response_class_it_does_not_render():
    """W10 -- a non-JSON response class is refused, and the defaults are accepted."""
    from fastapi import APIRouter
    from fastapi.responses import PlainTextResponse

    from sage.api.wire_route import WireRoute

    router = APIRouter(route_class=WireRoute)
    with pytest.raises(ValueError, match="WireRoute"):
        router.add_api_route(
            "/refused",
            lambda: None,
            methods=["GET"],
            response_model=TraverseResponse,
            response_class=PlainTextResponse,
        )
    router.add_api_route(
        "/accepted", lambda: None, methods=["GET"], response_model=TraverseResponse
    )
    assert [route.path for route in router.routes] == ["/accepted"]


async def test_rest_route_renders_the_declared_type_and_documents_it():
    """W10 -- the body is the declared model's, and the OpenAPI document is unchanged.

    FastAPI serializes a returned value *as the declared response model*, so a
    wider object never leaks fields the contract does not declare. A route class
    that dumped the runtime object instead would publish them. And the declared
    model must still reach the generated OpenAPI document, which every
    spec-versus-app gate in the suite reads.
    """
    from pydantic import Field

    class Declared(BaseModel):
        name: str = Field(description="Declared.")
        note: str | None = Field(default=None, description="Optional.")

    class Wider(Declared):
        internal: str = Field(default="leak", description="Undeclared on the wire.")

    def setup(router):
        @router.get("/declared", response_model=Declared)
        async def declared():
            return Wider(name="n")

    app = _wire_app(setup)
    async with _client(app) as client:
        assert (await client.get("/declared")).json() == {"name": "n"}
    response_schema = app.openapi()["paths"]["/declared"]["get"]["responses"]["200"]
    ref = response_schema["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/Declared")


# Routes that do not render a model, each with the reason. Everything else a
# built app exposes must be on the wire route class, so a router added or
# edited without it is caught here rather than by a caller.
NON_WIRE_ROUTES: Final[dict[tuple[str, str], str]] = {
    ("sage_core", "/health"): "a constant liveness dict, outside the documented surface",
    ("bff", "/health"): "a constant liveness dict, outside the documented surface",
    ("bff", "/sage_vaults"): "relays the SAGE response bytes unchanged",
    ("bff", "/sage_vaults/{path:path}"): "relays the SAGE response bytes unchanged",
    ("bff", "/{spa_path:path}"): "serves the built single-page application's files",
}


def _built_app_routes(spa_dir: Path) -> dict[str, list]:
    """Every API route each application serves, the SPA catch-all included.

    The backend-for-frontend adds its SPA route only when a built bundle is
    present, so the directory is supplied rather than left to whatever a local
    build happened to leave behind.
    """
    from fastapi.routing import APIRoute

    from app.backend.asgi import create_bff_app
    from sage.app import create_app
    from sage.config import SageCoreConfig

    (spa_dir / "index.html").write_text("<!doctype html>")
    apps = {
        "sage_core": create_app(),
        "bff": create_bff_app(spa_dir=spa_dir, stack_config=SageCoreConfig(profile="cloud")),
    }
    return {
        label: [r for r in app.routes if isinstance(r, APIRoute)] for label, app in apps.items()
    }


def test_every_built_route_renders_through_the_wire_route(tmp_path: Path):
    """W11 -- no route on either built app keeps FastAPI's null-keeping serialization.

    W10 proves the route class; this proves it is where the routes are. The
    route class is chosen per router, so a router that omits it sends every
    optional null again with every other test green. This walk over the
    applications as they are built is the only place that shows.
    """
    from sage.api.wire_route import WireRoute

    routes = _built_app_routes(tmp_path)
    assert routes["sage_core"] and routes["bff"], "an application exposed no routes"

    offenders = sorted(
        f"  {label}: {sorted(route.methods)} {route.path}"
        for label, app_routes in routes.items()
        for route in app_routes
        if not isinstance(route, WireRoute) and (label, route.path) not in NON_WIRE_ROUTES
    )
    assert not offenders, (
        "routes that serialize a response without the wire route class, so "
        "their bodies keep every optional null:\n" + "\n".join(offenders)
    )

    present = {(label, route.path) for label, app_routes in routes.items() for route in app_routes}
    stale = sorted(k for k in NON_WIRE_ROUTES if k not in present)
    assert not stale, f"NON_WIRE_ROUTES entries naming no route: {stale}"
