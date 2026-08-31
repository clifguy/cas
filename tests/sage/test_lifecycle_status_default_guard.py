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

* **Behaviour** — a `Document` constructed without `lifecycle_status`
  raises rather than resolving to a state of the model's choosing, and
  it does so under a vault whose configured landing state is not the
  one the removed default named. That second half is what separates
  this from a test that merely notices a field moved.

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

import copy
import hashlib
import inspect
import typing
from datetime import datetime, timezone
from typing import Final

import pytest
from pydantic import BaseModel, Field, ValidationError

from sage.config import VaultConfig, build_transition_table
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document

# Divergences pinned here rather than remediated. Keyed by
# (class name, field name), valued with the reason the field keeps a
# default. Empty: no non-nullable lifecycle_status field carries one.
#
# Allowlist contract, uniform with the repo's other structural gates:
# - a new defaulted non-nullable field without an entry fails the suite;
# - removing an entry without remediating fails the suite;
# - making an entry's field required without removing the entry fails
#   the suite (stale-allowlist);
# - an entry naming no discovered field fails the suite (phantom-entry).
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


def _config_landing_ingest_in(config_dict: dict, state: str) -> VaultConfig:
    """Return a config whose `(new)` row lands ingest in `state`.

    Declares the state, rewrites the `(new)` row's target, and adds a
    transition out of it so the state is not a dead end.
    """
    mutated = copy.deepcopy(config_dict)
    mutated["lifecycle"]["states"].append({"value": state, "label": state.title()})
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "(new)":
            transition["to_state"] = state
    mutated["lifecycle"]["transitions"].append(
        {"from_state": state, "action": "activate", "to_state": "active"}
    )
    return VaultConfig.model_validate(mutated)


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


def test_omitted_state_cannot_substitute_one_the_vault_never_routes_ingest_to(
    minimal_vault_config_dict,
):
    """The hazard itself: a vault landing ingest outside `active`.

    Under a vault whose `(new)` row lands ingest in `draft`, a document
    constructed without a state must not come to rest in `active` — a
    declared state, but not one this vault's ingest ever produces, and
    so one no subsequent precondition would flag as wrong.

    Anti-coincidental-pass: the landing state is read back from the
    configured table and asserted to differ from the state the removed
    default named, so the test cannot pass by the vault happening to
    land in `active`. Both halves fail independently — a config that
    stopped being read fails the first assertion, a model that resumed
    substituting fails the second.

    What this does not exclude: a model that requires the field while
    ingestion hardcodes a state of its own. Only omission is asserted
    here, not that a supplied state is the configured one; that is
    `test_ingest_lands_in_configured_state_end_to_end`, which drives a
    real ingest under a divergent lifecycle and must stay untouched by
    any change to this field for the pair to keep its meaning.
    """
    config = _config_landing_ingest_in(minimal_vault_config_dict, "draft")
    landing_state = build_transition_table(config).ingest_landing_state()

    assert landing_state == "draft", (
        "the configured (new) row is not being read, so the divergence "
        "this test depends on does not exist"
    )
    assert landing_state != "active", (
        "the landing state coincides with the state the removed default "
        "named; the test would pass without exercising the hazard"
    )

    with pytest.raises(ValidationError):
        Document(**_document_kwargs_without_lifecycle_status("landing"))


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
    defaulted = _carries_a_default(cls, field_name)
    allowlisted = key in KNOWN_LIFECYCLE_STATUS_DEFAULTS

    if not defaulted and allowlisted:
        pytest.fail(
            f"{cls.__name__}.{field_name} is required AND is pinned in "
            f"KNOWN_LIFECYCLE_STATUS_DEFAULTS. Remove the stale entry "
            f"({KNOWN_LIFECYCLE_STATUS_DEFAULTS[key]!r})."
        )
    if defaulted and not allowlisted:
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
    phantom = sorted(set(KNOWN_LIFECYCLE_STATUS_DEFAULTS) - discovered)
    assert not phantom, (
        f"KNOWN_LIFECYCLE_STATUS_DEFAULTS pins entries that match no "
        f"non-nullable lifecycle_status field in sage.models.schemas: "
        f"{phantom}. Did the class get renamed or the field made "
        f"nullable? Remove the stale entry."
    )
