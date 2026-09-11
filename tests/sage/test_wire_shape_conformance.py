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

W5 and W6 are the two halves of the per-field rule, and they fail under
opposite mistakes. An implementation that keeps every key passes W5 and fails
W6; one that keeps none passes W6 and fails W5. Neither half is redundant with
W1, which reports a schema violation several layers below the cause.

**A third rival passes both of them, and W7 is the only thing that excludes
it**: a rule that reads each field's declaration correctly but stops at the top
level. W5 and W6 both read a top-level model, so a prune that never descends --
or one that descends into a nested model but not into a list of them -- satisfies
each of them while leaving every nested optional null on the wire. This was
measured, not reasoned: both rivals were written and both passed the whole
module until W7 gained an assertion that a nested optional key is *gone*.
Checking only that required keys survive at depth is not enough, because a rule
that does nothing at depth leaves them there too.

Read the pairing as the gate and this paragraph as a claim to re-audit. The
inventory above was accurate and incomplete once already.

One test here is inert by construction: the divergence allowlist is empty, so
its staleness check loops zero times and passes against any implementation. It
is a guard for entries that do not exist yet, in the manner of the other empty
allowlists in this suite, and it is named here rather than left to look like
coverage.
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
from pydantic import BaseModel

from sage.mcp_server import _serialize
from sage.models.schemas import BatchIngestFileError
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


# ---------------------------------------------------------------------------
# W1 / W2: serialized bodies validate against their declared components
# ---------------------------------------------------------------------------


def test_serialized_response_validates_against_declared_schema(
    sage_core_spec: dict | None, cas_app_spec: dict | None
):
    """W1 -- what the surface sends satisfies what the surface published.

    For each component with a same-named model, an instance whose nullable
    fields are all null is pushed through the real MCP serializer and
    validated against the component. A required key the serializer dropped
    surfaces here as a missing-property violation.

    This is the check the ticket that motivated this module asks for, and the
    one no other gate performs: every sibling compares a declaration to a
    declaration and cannot see the wire at all.
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
            body = _serialize(instance)
            errors = sorted(
                _validator_for(spec, name).iter_errors(body),
                key=lambda e: list(e.absolute_path),
            )
            for error in errors:
                where = "/".join(str(p) for p in error.absolute_path) or "(root)"
                violations.append(f"  {label}: {name} at {where} -> {error.message}")

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
                body = _serialize(_build_sentinel(model, _properties_for(spec, name), spec=spec))
            except UnbuildableModel:
                continue
            if _validator_for(spec, name).is_valid(body):
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
