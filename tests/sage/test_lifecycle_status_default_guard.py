"""A non-nullable `lifecycle_status` carries no model-level default.

CAS-ADR-047 places lifecycle preconditions under vault configuration:
engine literals survive only as the defaults an unset declaration
resolves to. A model-level `lifecycle_status` default is not such a
literal. It is reachable without any declaration at all, so it shadows
the configured landing state on every construction path that omits the
field, and it does so silently — the document simply comes to rest in a
state the vault never routes ingest to, and no precondition rejects it
afterward because the state is a declared one, just not the declared
landing one.

`TransitionTable.ingest_landing_state` already states this thesis one
layer up: it raises rather than substituting, because a substituted
default could strand documents in a state the vault never declared.
This module holds the field-level counterpart.

Two properties are pinned:

* **Behaviour** — a `Document` neither invents a state when none is
  given (construction raises) nor overrides one that is (a supplied
  non-`active` state survives verbatim). Together those leave the
  configured landing state the sole determinant of where a freshly
  ingested document rests. Neither half consults a vault, because
  `Document` takes none; that a real ingest routes through the
  configured state is verified in
  `test_ingest_lands_in_configured_state_end_to_end`, where a vault
  actually is consulted.

* **Structure** — no non-nullable `lifecycle_status` field anywhere in
  `sage.models.schemas` carries a default. Stated over the whole module
  rather than the one class that carried the default, because a
  per-class fix is correct on the day it lands and silently incomplete
  the next time a state-carrying model is added.

Nullability is the discriminator rather than an allowlist. A
non-nullable `lifecycle_status` is a state a record is asserting; a
nullable one (`RetrievalFilters.lifecycle_status`) is a filter whose
absence means "any", and a default of `None` there is the correct
reading of an unset filter, not a substituted state.

What this does not reach: `model_construct`, which bypasses validation
entirely and so accepts a missing field regardless of how the model
declares it. That bypass is deliberate where it is used — a repair
workflow must be able to read legacy values the request-side validators
reject — so the gate raises the cost of omitting the state, it does not
make it impossible.
"""

import hashlib
import inspect
import typing
from collections.abc import Mapping, Set
from datetime import datetime, timezone
from typing import Final

import pytest
from pydantic import BaseModel, Field, ValidationError

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document

# Divergences pinned here rather than remediated. Keyed by
# (class name, field name), valued with the reason the field keeps a
# default. Empty: no non-nullable lifecycle_status field carries one.
#
# Allowlist contract, uniform with the repo's other structural gates.
# Three clauses, computed by `_contract_violations`:
# - `unpinned_default`: a defaulted non-nullable field with no entry.
#   This is also what fires when an entry is removed while the default
#   it pinned remains, so it carries two of the four stated behaviours.
# - `stale_entry`: an entry whose field is required after all.
# - `phantom_entry`: an entry naming no discovered field.
# With this dict empty only the first can fire against real data, so
# all three are exercised against synthetic input in
# `test_allowlist_contract_clauses_fire_on_synthetic_input` — a clause
# that never executes pins nothing, however carefully it is worded.
KNOWN_LIFECYCLE_STATUS_DEFAULTS: Final[dict[tuple[str, str], str]] = {}

_FIELD: Final[str] = "lifecycle_status"


# ---------------------------------------------------------------------------
# Fixture helpers — mirrored from the sibling suites rather than shared, so
# this module states the full shape it constructs.
# ---------------------------------------------------------------------------


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID."""
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name."""
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _document_kwargs_without_lifecycle_status(name: str = "guard") -> dict:
    """Every required `Document` field except `lifecycle_status`.

    Spelled out rather than derived from `model_fields` so the omission
    is the fixture's explicit subject. A derived version would drop the
    field automatically once it stopped being required and could not
    then distinguish "omitted" from "no longer exists".
    """
    now = datetime.now(timezone.utc)
    return dict(
        id=_id(name),
        title=f"Doc {name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{name}.md",
        source_content_hash=_sha(name),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


# ---------------------------------------------------------------------------
# Structural discovery
# ---------------------------------------------------------------------------


def _is_nullable(annotation: object) -> bool:
    """Whether `annotation` admits None.

    A nullable `lifecycle_status` is a filter — its absence means "any"
    — so it is outside the rule. A non-nullable one is a state the
    record asserts, and a default there substitutes one.
    """
    return type(None) in typing.get_args(annotation)


def _state_carrying_fields() -> list[tuple[type[BaseModel], str]]:
    """Every non-nullable `lifecycle_status` field in `sage.models.schemas`.

    Walks the module's own classes rather than a hand-kept roster, so a
    state-carrying model added later is covered without an edit here.
    Subclasses that only inherit the field are skipped: the declaration
    is what carries the default, and reporting the base and every
    subclass would name one divergence several times.
    """
    from sage.models import schemas

    found: list[tuple[type[BaseModel], str]] = []
    for _, cls in inspect.getmembers(schemas, inspect.isclass):
        if not (issubclass(cls, BaseModel) and cls.__module__ == schemas.__name__):
            continue
        if _FIELD not in cls.__annotations__:
            continue
        info = cls.model_fields.get(_FIELD)
        if info is None or _is_nullable(info.annotation):
            continue
        found.append((cls, _FIELD))
    return sorted(found, key=lambda pair: pair[0].__name__)


def _carries_a_default(cls: type[BaseModel], field_name: str) -> bool:
    """Whether `cls.field_name` resolves to a value when omitted."""
    return not cls.model_fields[field_name].is_required()


def _contract_violations(
    discovered: Set[tuple[str, str]],
    defaulted: Set[tuple[str, str]],
    allowlist: Mapping[tuple[str, str], str],
) -> dict[str, list[tuple[str, str]]]:
    """The allowlist contract's three clauses, over supplied sets.

    Takes its inputs as parameters rather than reading module state so
    every clause is reachable from a test. Against the real data the
    allowlist is empty, which makes `allowlisted` uniformly false and
    leaves the stale and phantom clauses unexecuted — worded but inert.
    Passing synthetic sets is what gives them teeth before an entry
    ever exists to exercise them for real.
    """
    pinned = set(allowlist)
    return {
        "unpinned_default": sorted(defaulted - pinned),
        "stale_entry": sorted(pinned & (discovered - defaulted)),
        "phantom_entry": sorted(pinned - discovered),
    }


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_document_construction_requires_lifecycle_status():
    """Omitting the state raises rather than resolving to one.

    Anti-coincidental-pass: the error is matched on the field location
    and the `missing` type, so a ValidationError raised for any other
    reason — a malformed id, a bad hash — does not satisfy it.
    """
    with pytest.raises(ValidationError) as excinfo:
        Document(**_document_kwargs_without_lifecycle_status())

    locations = {error["loc"] for error in excinfo.value.errors()}
    types = {error["type"] for error in excinfo.value.errors() if error["loc"] == (_FIELD,)}
    assert (_FIELD,) in locations, (
        f"construction failed, but not for the omitted {_FIELD}: {excinfo.value.errors()}"
    )
    assert types == {"missing"}, (
        f"{_FIELD} was rejected for {types} rather than being reported missing"
    )


def test_a_non_active_state_is_preserved_verbatim():
    """The model does not coerce a supplied state toward `active`.

    The pair this completes: `test_document_construction_requires_lifecycle_status`
    excludes a model that *substitutes* a state when none is given; this
    one excludes a model that *overrides* the state it was given. Only a
    model doing neither leaves the configured landing state as the sole
    determinant of where a freshly ingested document rests.

    Anti-coincidental-pass: the asserted state is deliberately not
    `active`, so a model that coerced every value to `active` — or that
    kept the removed default and ignored the argument — fails the
    round-trip. Its rival is a coercing model, not a defaulting one:
    supplying a value satisfies a default too, so this test alone does
    not detect the default's return. That is the other half's job.

    No vault config appears here, and that is deliberate. `Document`
    takes no vault, so nothing about a configured landing state can be
    in this assertion's causal path; an earlier revision built a
    divergent vault and read its landing state back, and the test passed
    unchanged when the whole config block was replaced by a string
    literal. Config machinery that cannot discriminate reads as a
    control while being decoration. The property that a real ingest
    routes through the configured state is verified where a vault is
    actually consulted, in
    `test_ingest_lands_in_configured_state_end_to_end`, which must stay
    untouched by any change to this field.
    """
    state = "draft"
    assert state != "active", "the asserted state must differ from the removed default"

    doc = Document(
        lifecycle_status=state,
        **_document_kwargs_without_lifecycle_status("supplied"),
    )

    assert doc.lifecycle_status == state, (
        f"the model resolved {state!r} to {doc.lifecycle_status!r}; a supplied "
        "state must survive construction unchanged, or a document can rest "
        "somewhere the vault never selected"
    )


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,field_name",
    [
        pytest.param(cls, name, id=f"{cls.__name__}.{name}")
        for cls, name in _state_carrying_fields()
    ],
)
def test_non_nullable_lifecycle_status_fields_are_required(cls: type[BaseModel], field_name: str):
    """No state-carrying field resolves to a state of the model's choosing.

    Anti-coincidental-pass: this test is green both when the module is
    clean and when the gate cannot see. Two companions separate those.
    `test_gate_discovers_the_state_carrying_models` pins that the
    discovery finds models at all, without which this parametrizes to
    zero cases; `test_gate_flags_a_defaulted_field` pins that the
    predicate answers True on a defaulted field, without which every
    case here passes whatever the module declares.
    """
    key = (cls.__name__, field_name)
    defaulted = {key} if _carries_a_default(cls, field_name) else set()
    violations = _contract_violations({key}, defaulted, KNOWN_LIFECYCLE_STATUS_DEFAULTS)

    if violations["stale_entry"]:
        pytest.fail(
            f"{cls.__name__}.{field_name} is required AND is pinned in "
            f"KNOWN_LIFECYCLE_STATUS_DEFAULTS. Remove the stale entry "
            f"({KNOWN_LIFECYCLE_STATUS_DEFAULTS[key]!r})."
        )
    if violations["unpinned_default"]:
        default = cls.model_fields[field_name].get_default(call_default_factory=False)
        pytest.fail(
            f"{cls.__name__}.{field_name} is non-nullable and defaults to "
            f"{default!r}, so a construction path that omits it lands the "
            f"record in that state whatever the vault declares. Drop the "
            f"default so every construction site supplies the state, or "
            f"pin the divergence in KNOWN_LIFECYCLE_STATUS_DEFAULTS with "
            f"its reason."
        )


def test_gate_discovers_the_state_carrying_models():
    """The discovery reaches the models it is meant to police.

    Trap: a discovery that returns nothing parametrizes the gate above
    into zero cases, which reports success forever over a module it
    never read. Naming the expected classes also pins that the
    nullability split keeps the filter out — `RetrievalFilters` carries
    `lifecycle_status` too, and sweeping it in would demand a state
    from a field whose absence correctly means "any".

    What this does not reach: a class added to `schemas` after this
    roster was written. The roster is a floor, asserted as a subset,
    so a new model widens the gate without reddening this test.
    """
    discovered = {cls.__name__ for cls, _ in _state_carrying_fields()}

    expected = {
        "ChainEntry",
        "Document",
        "DocumentSummary",
        "DocumentSummaryLight",
        "ReadProjectionResponse",
        "SourceFileIntegrityEntry",
    }
    assert expected <= discovered, (
        f"the discovery missed state-carrying models: {sorted(expected - discovered)}"
    )
    assert "RetrievalFilters" not in discovered, (
        "RetrievalFilters.lifecycle_status is a nullable filter, not a "
        "state a record asserts; sweeping it in would demand a value "
        "from a field whose absence means 'any'"
    )


def test_gate_flags_a_defaulted_field():
    """The predicate has teeth on both answers.

    Trap: a predicate that never returns True reports no divergence on
    any module at all, and the gate above passes vacuously. Exercised
    against synthetic models rather than the real ones so it keeps
    working once `schemas` is clean.
    """

    class Defaulted(BaseModel):
        lifecycle_status: str = Field(default="active", description="Synthetic.")

    class Required(BaseModel):
        lifecycle_status: str = Field(description="Synthetic.")

    assert _carries_a_default(Defaulted, _FIELD) is True
    assert _carries_a_default(Required, _FIELD) is False


def test_nullability_split_reads_both_answers():
    """`_is_nullable` separates the filter shape from the state shape.

    Trap: a nullability check that always answers False sweeps the
    filter into the gate; one that always answers True empties the gate
    of every model. Either way the discovery test above could still
    pass on a roster that happened to match.
    """

    class Stateful(BaseModel):
        lifecycle_status: str = Field(description="Synthetic.")

    class Filtering(BaseModel):
        lifecycle_status: str | None = Field(default=None, description="Synthetic.")

    assert _is_nullable(Stateful.model_fields[_FIELD].annotation) is False
    assert _is_nullable(Filtering.model_fields[_FIELD].annotation) is True


def test_known_lifecycle_status_defaults_reference_real_fields():
    """Every pinned divergence names a field the discovery finds.

    Trap: an entry naming a renamed or deleted class sits in the
    allowlist forever, reading as a known divergence while pinning
    nothing.
    """
    discovered = {(cls.__name__, name) for cls, name in _state_carrying_fields()}
    defaulted = {
        (cls.__name__, name)
        for cls, name in _state_carrying_fields()
        if _carries_a_default(cls, name)
    }
    phantom = _contract_violations(discovered, defaulted, KNOWN_LIFECYCLE_STATUS_DEFAULTS)[
        "phantom_entry"
    ]
    assert not phantom, (
        f"KNOWN_LIFECYCLE_STATUS_DEFAULTS pins entries that match no "
        f"non-nullable lifecycle_status field in sage.models.schemas: "
        f"{phantom}. Did the class get renamed or the field made "
        f"nullable? Remove the stale entry."
    )


def test_allowlist_contract_clauses_fire_on_synthetic_input():
    """All three clauses have teeth before a real entry exists.

    Trap, and the reason this test exists: `KNOWN_LIFECYCLE_STATUS_DEFAULTS`
    is empty, so against real data `pinned` is empty and both the stale
    and phantom clauses subtract from nothing. They would read as
    enforced while never executing, and would stay that way until the
    first entry is added — which is exactly when a reviewer would rely
    on them. Synthetic sets exercise each clause now.
    """
    real = ("Real", _FIELD)
    ghost = ("Ghost", _FIELD)

    # A defaulted field nobody pinned.
    assert _contract_violations({real}, {real}, {})["unpinned_default"] == [real]
    # ... and pinning it clears exactly that clause.
    assert _contract_violations({real}, {real}, {real: "why"})["unpinned_default"] == []

    # A pinned field that turned out to be required.
    assert _contract_violations({real}, set(), {real: "why"})["stale_entry"] == [real]
    # ... and an unpinned required field is not stale.
    assert _contract_violations({real}, set(), {})["stale_entry"] == []

    # A pin naming nothing the discovery found.
    assert _contract_violations({real}, {real}, {ghost: "why"})["phantom_entry"] == [ghost]
    # ... and a pin on a discovered field is not phantom.
    assert _contract_violations({real}, {real}, {real: "why"})["phantom_entry"] == []
