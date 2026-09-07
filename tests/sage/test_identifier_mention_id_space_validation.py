"""Config-load validation: does a mention regex still span its target's id space?

An ``identifier_mention`` pattern names a regex that is matched against
document bodies, and a ``target_tier3`` filter that resolves the matched
literal to a document. The identifiers that filter can resolve are declared
somewhere else entirely -- in the target ``doc_type``'s ``metadata_schema``.
Nothing tied the two together, and the divergence is silent in both
directions: a body naming an identifier the regex cannot match produces no
``references`` edge, which is byte-identical to a body that cited nothing.

Spec (plain English):

TS1 -- A regex narrower than its target's declared id space warns.
  Inputs: doc_type whose ``failure_id`` schema admits an unbounded digit
           run; a mention regex bounded to two digits.
  Expect: one warning naming the regex, the schema pattern, and at least
           one identifier the schema admits and the regex misses.
  Why: the defect this check exists for.
  Two companions carry properties the headline case cannot. One drops the
           alternation, leaving digit-run width as the only thing that can
           expose the gap -- in the headline case the second branch is
           missed whatever run lengths were sampled, so it stays green
           against a sampler that never crosses the bound. The other uses a
           regex that matches a *prefix* of an admitted identifier, the one
           shape here where the narrow regex matches at all; without it, an
           implementation asking merely whether the regex matched, rather
           than whether it matched the whole identifier, agrees with the
           correct one everywhere in this module.

TS2 -- The corrected regex is silent.
  Inputs: same config, regex widened to the full id space.
  Expect: no warnings.
  Why: TS1 alone is passed by a check that warns on everything.

TS3 -- The canonical pattern set is silent.
  Inputs: the three patterns the suite pins to the vault they mirror,
           against doc_type schemas carrying those vaults' id spaces.
  Expect: no warnings, and constructing a whole config from them logs
           nothing.
  Why: a production config must not be reported as broken. The second arm
           is the silence half of TS9: a divergent config can only show
           that the loader reports a divergence, never that it stays quiet
           when there is none, and that arm has to read what construction
           logged rather than call the check again afterwards.

TS4 -- Absent declarations degrade silently.
  Inputs: no ``metadata_schema``; a schema with no ``pattern`` on the named
           key; no ``target_doc_type``; a ``target_doc_type`` the vault
           does not declare.
  Expect: no warnings, no exception, in every case.
  Why: that is most vaults, and it is not a defect.

TS5 -- A tier3 template other than the whole match is not measured.
  Inputs: a pattern whose regex matches a prefixed literal and whose tier3
           template carries the trailing numeric run.
  Expect: no warnings.
  Why: the mention text and the tier3 value differ by a transform with no
           inverse, so the comparison is not defined. A check that skipped
           this gate would report a correct pattern as broken.

TS6 -- Schema patterns outside the supported subset are not measured.
  Inputs: an unanchored pattern; a character class; a wildcard.
  Expect: no warnings.
  Why: guessing at an unsupported construct produces samples the schema
           does not actually admit, and a false warning off them.

TS7 -- The sample generator, directly.
  Inputs: supported and unsupported schema patterns.
  Expect: every sample is admitted by the pattern it came from; an
           unbounded digit run yields runs long enough to cross a
           two-digit bound; an unsupported construct yields None.
  Why: a generator that only ever emitted short runs would leave TS1
           green and the check inert.

TS8 -- A disabled pattern is not measured.
  Inputs: the TS1 config with ``enabled: false``.
  Expect: no warnings.
  Why: the engine's own reader skips disabled patterns, so warning about
           one describes behavior that will never occur.

TS9 -- The check is wired into the load path.
  Inputs: a divergent config written to disk and loaded.
  Expect: the warning is logged on the config logger; the vault still
           loads.
  Why: every other test calls the function directly, so nothing else
           would notice it being unreachable.

TS10 -- The division of labor with the formal substrate.
  Inputs: a divergent pattern validated against the edge-inference schema.
  Expect: the schema raises nothing.
  Why: the invariant joins two sibling config sections, and the schema for
           one cannot see the other. Recording that here keeps a later
           schema edit from quietly overlapping the loader check.
"""

from __future__ import annotations

import copy
import logging
import re

import jsonschema
import pytest
import yaml

from sage.config import (
    DocTypeEntry,
    VaultConfig,
    _sample_identifiers_from_schema_pattern,
    identifier_mention_id_space_warnings,
    load_vault_config,
)
from tests.app.test_edge_inference_identifier_mention import (
    CAS_IDENTIFIER_MENTION_PATTERNS,
    _edge_inference_block_with_pattern,
    _load_edge_inference_schema,
)

# ---------------------------------------------------------------------------
# Id spaces, as their doc_types declare them
#
# These mirror the metadata_schema fragments the canonical vault declares for
# the three doc_types its mention patterns target. They are the *other half*
# of CAS_IDENTIFIER_MENTION_PATTERNS: the patterns say what the engine will
# match, these say what it must be able to match. TS3 pairs them.
# ---------------------------------------------------------------------------

ADR_ID_PATTERN = r"^\d{3}$"
TICKET_ID_PATTERN = r"^T-\d{4}$"
FAILURE_ID_PATTERN = r"^(F\d+|BASELINE(-\d+)?)$"

# The bound that was outgrown: two digits, against an id space that had
# already been widened to an unbounded run.
NARROW_FAILURE_REGEX = r"\bF\d{1,2}\b"
WIDE_FAILURE_REGEX = r"\bF\d+\b|\bBASELINE(?:-\d+)?\b"


def _doc_type(value: str, id_key: str | None = None, pattern: str | None = None) -> DocTypeEntry:
    """A doc_type entry, optionally declaring one pattern-constrained key."""
    payload: dict = {"value": value, "label": value.replace("_", " ").title()}
    if id_key is not None:
        prop: dict = {"type": "string"}
        if pattern is not None:
            prop["pattern"] = pattern
        payload["metadata_schema"] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {id_key: prop},
        }
    return DocTypeEntry.model_validate(payload)


def _edge_inference(*patterns: dict) -> dict:
    """The ``references`` tier assignment carrying the given patterns."""
    return {
        "tier_assignments": [
            {
                "edge_type": "references",
                "tier": 1,
                "inference_rules": [{"method": "identifier_mention", "patterns": list(patterns)}],
            },
        ],
    }


def _failure_pattern(regex: str, **overrides: object) -> dict:
    pattern: dict = {
        "regex": regex,
        "target_doc_type": "failure_record",
        "target_tier3": {"failure_id": "{id}"},
    }
    pattern.update(overrides)
    return pattern


def test_ts1_narrow_regex_warns_naming_regex_schema_and_a_missed_identifier() -> None:
    """A regex that cannot match every identifier its target admits warns.

    The warning has to carry enough to act on without re-deriving the
    comparison: which regex, which schema pattern it was measured against,
    and at least one identifier that falls in the gap.
    """
    warnings = identifier_mention_id_space_warnings(
        _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX)),
        [_doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN)],
    )

    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    message = warnings[0]
    # Both are quoted with `repr`, matching the sibling pattern-warning pass,
    # so a regex full of backslashes reads unambiguously in a log line.
    assert repr(NARROW_FAILURE_REGEX) in message, f"warning does not name the regex: {message}"
    assert repr(FAILURE_ID_PATTERN) in message, (
        f"warning does not name the schema pattern: {message}"
    )
    missed = re.findall(r"F\d{3,}|BASELINE", message)
    assert missed, (
        f"warning names no identifier the schema admits and the regex misses, "
        f"so a reader cannot check the claim: {message}"
    )


def test_ts1_width_alone_warns_with_no_alternation_to_carry_it() -> None:
    """The digit-run width is load-bearing on its own.

    TS1's id space has a second branch, and a regex bounded to two digits
    misses that branch whatever run lengths were sampled -- so TS1 stays green
    even if the sampler never crosses the bound. Here the space is a single
    unbounded run, so nothing but run length can expose the gap, and a sampler
    that stopped at two digits would report this narrow regex as spanning it.
    """
    warnings = identifier_mention_id_space_warnings(
        _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX)),
        [_doc_type("failure_record", "failure_id", r"^F\d+$")],
    )
    assert len(warnings) == 1, f"a two-digit bound cannot span an unbounded run; got {warnings}"
    assert re.search(r"F\d{3,}", warnings[0]), (
        f"the warning must name an identifier past the bound: {warnings[0]}"
    )


def test_ts1_a_regex_matching_only_a_prefix_does_not_cover_the_identifier() -> None:
    """Matching part of an identifier is not covering it.

    The engine feeds each match's span to the resolver as the identifier, so a
    regex that matches ``ID-12`` inside ``ID-1234`` resolves a different,
    truncated identifier rather than the one the body named -- and the tier3
    lookup goes to a document that is not the cited one, or to none.

    This is the only case in the module where a narrow regex produces a match
    at all. Everywhere else the regex misses its samples outright, so an
    implementation that asked merely whether the regex matched -- rather than
    whether it matched the whole identifier -- would agree with the correct
    one on every other test here and diverge only on this shape.
    """
    warnings = identifier_mention_id_space_warnings(
        _edge_inference(
            {
                "regex": r"\bID-\d{2}",
                "target_doc_type": "record",
                "target_tier3": {"record_id": "{id}"},
            }
        ),
        [_doc_type("record", "record_id", r"^ID-\d{4}$")],
    )
    assert len(warnings) == 1, f"a prefix match leaves the identifier uncovered; got {warnings}"
    assert re.search(r"ID-\d{4}", warnings[0]), (
        f"the warning must name a full identifier the regex fails to cover: {warnings[0]}"
    )


def test_ts2_regex_spanning_the_id_space_is_silent() -> None:
    """The same config, corrected, produces nothing.

    Anti-coincidence for TS1: a check that appended a warning for every
    pattern it visited would pass TS1 and fail here.
    """
    assert (
        identifier_mention_id_space_warnings(
            _edge_inference(_failure_pattern(WIDE_FAILURE_REGEX)),
            [_doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN)],
        )
        == []
    )


def test_ts3_canonical_pattern_set_is_silent_against_its_id_spaces() -> None:
    """The pinned pattern set, against the id spaces its targets declare.

    The patterns are imported rather than re-listed: they are pinned to the
    vault they mirror elsewhere in the suite, and a second copy here would
    re-open the drift that pin closed.

    This is the strongest anti-coincidence check in the module. A check that
    compared the mention regex to the schema pattern without first asking
    whether the tier3 template is the whole match would fire on the ADR leg
    -- whose regex matches a prefixed literal while its schema constrains
    the bare numeric run -- and so would report a correct config broken.
    """
    doc_types = [
        _doc_type("adr", "adr_id", ADR_ID_PATTERN),
        _doc_type("ticket", "ticket_id", TICKET_ID_PATTERN),
        _doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN),
    ]
    warnings = identifier_mention_id_space_warnings(
        _edge_inference(*CAS_IDENTIFIER_MENTION_PATTERNS), doc_types
    )
    assert warnings == [], (
        f"the canonical pattern set must measure clean against the id spaces "
        f"its own targets declare; got {warnings}"
    )


@pytest.mark.parametrize(
    ("pattern", "doc_types", "case"),
    [
        pytest.param(
            _failure_pattern(NARROW_FAILURE_REGEX),
            [_doc_type("failure_record")],
            "no metadata_schema at all",
            id="no_metadata_schema",
        ),
        pytest.param(
            _failure_pattern(NARROW_FAILURE_REGEX),
            [_doc_type("failure_record", "failure_id", None)],
            "schema declares the key but constrains no pattern on it",
            id="no_pattern_on_key",
        ),
        pytest.param(
            {"regex": NARROW_FAILURE_REGEX, "target_tier3": {"failure_id": "{id}"}},
            [_doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN)],
            "pattern names no target_doc_type, so no id space is reachable",
            id="no_target_doc_type",
        ),
        pytest.param(
            _failure_pattern(NARROW_FAILURE_REGEX),
            [_doc_type("note")],
            "target_doc_type names a type the vault does not declare",
            id="undeclared_target_doc_type",
        ),
    ],
)
def test_ts4_absent_declarations_degrade_silently(
    pattern: dict, doc_types: list[DocTypeEntry], case: str
) -> None:
    """Nothing to measure against is not a defect, and must not raise.

    Most vaults declare no metadata_schema. A check that warned on absence
    would make every one of them noisy at load; a check that indexed blindly
    would raise on the same configs.
    """
    assert identifier_mention_id_space_warnings(_edge_inference(pattern), doc_types) == [], case


def test_ts5_tier3_template_other_than_the_whole_match_is_not_measured() -> None:
    """A prefixed-literal regex feeding a derived tier3 value is skipped.

    The regex matches ``CAS-ADR-042`` and the filter looks up ``042``. The
    transform from one to the other has no inverse, so there is no way to
    ask whether some string the regex matches would produce a given id --
    and a check that compared them directly would warn that a correct
    pattern misses every identifier its schema admits.
    """
    adr_pattern = {
        "regex": r"\bCAS-ADR-\d{3}\b",
        "target_doc_type": "adr",
        "target_tier3": {"adr_id": "{adr_num}"},
    }
    assert (
        identifier_mention_id_space_warnings(
            _edge_inference(adr_pattern),
            [_doc_type("adr", "adr_id", ADR_ID_PATTERN)],
        )
        == []
    )


@pytest.mark.parametrize(
    "schema_pattern",
    [
        pytest.param(r"\d+", id="unanchored"),
        pytest.param(r"^[A-Z]{2}-\d+$", id="character_class"),
        pytest.param(r"^.+$", id="wildcard"),
        pytest.param(r"^F\w*$", id="word_class"),
    ],
)
def test_ts6_unsupported_schema_patterns_are_not_measured(schema_pattern: str) -> None:
    """A schema pattern the generator cannot expand faithfully is skipped.

    A generator that treated ``[A-Z]`` as four literal characters would
    produce strings the schema does not admit, and every one of them would
    read as an identifier the regex misses.
    """
    assert (
        identifier_mention_id_space_warnings(
            _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX)),
            [_doc_type("failure_record", "failure_id", schema_pattern)],
        )
        == []
    )


@pytest.mark.parametrize(
    "schema_pattern",
    [FAILURE_ID_PATTERN, TICKET_ID_PATTERN, ADR_ID_PATTERN, r"^F\d{1,4}$", r"^X-\d?$"],
)
def test_ts7_every_generated_sample_is_admitted_by_its_source_pattern(
    schema_pattern: str,
) -> None:
    """The generator never emits a string its source pattern rejects.

    This is what keeps a generator bug from producing a false warning: a
    sample the schema would not accept is not evidence of anything.
    """
    samples = _sample_identifiers_from_schema_pattern(schema_pattern)
    assert samples, f"no samples generated for {schema_pattern}"
    for sample in samples:
        assert re.fullmatch(schema_pattern, sample), (
            f"generator emitted {sample!r} for {schema_pattern}, which does not admit it"
        )


def test_ts7_unbounded_digit_run_yields_runs_past_a_two_digit_bound() -> None:
    """An unbounded run must be sampled past the bound that outgrew it.

    A generator that stopped at two digits would leave the check inert
    against the exact defect it exists for: the narrow regex matches every
    one- and two-digit identifier, so only a longer run exposes the gap.
    """
    samples = _sample_identifiers_from_schema_pattern(FAILURE_ID_PATTERN)
    assert any(re.fullmatch(r"F\d{3,}", s) for s in samples), (
        f"no sample crosses the two-digit bound: {samples}"
    )
    assert any(s.startswith("BASELINE") for s in samples), (
        f"the alternation's second branch was not expanded: {samples}"
    )


@pytest.mark.parametrize(
    "schema_pattern",
    [r"\d+", r"^[A-Z]{2}-\d+$", r"^.+$", r"^F\w*$", r"^F\d*$"],
)
def test_ts7_unsupported_constructs_refuse_rather_than_guess(schema_pattern: str) -> None:
    """Outside the supported subset the generator returns nothing at all."""
    assert _sample_identifiers_from_schema_pattern(schema_pattern) is None


def test_ts8_disabled_pattern_is_not_measured() -> None:
    """A pattern the engine will never apply is not worth a warning.

    The engine's own reader filters on ``enabled`` before it applies
    anything, so a warning about a disabled pattern describes behavior that
    cannot occur.
    """
    assert (
        identifier_mention_id_space_warnings(
            _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX, enabled=False)),
            [_doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN)],
        )
        == []
    )


def test_ts9_divergence_is_surfaced_on_the_load_path(
    minimal_vault_config_dict, tmp_path, caplog
) -> None:
    """The check reaches the loader, and the vault still loads.

    Every other test in this module calls the function directly, so a
    correct function that nothing invokes would leave them all green. The
    second assertion carries the lenient-load posture: a divergent config is
    a fact to surface, not a request to refuse -- refusing would drop the
    vault from the registry and with it every surface that could repair the
    configuration.
    """
    mutated = copy.deepcopy(minimal_vault_config_dict)
    mutated["document_types"]["doc_types"].append(
        {
            "value": "failure_record",
            "label": "Failure Record",
            "metadata_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {"failure_id": {"type": "string", "pattern": FAILURE_ID_PATTERN}},
            },
        }
    )
    mutated["edge_inference"]["tier_assignments"].append(
        _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX))["tier_assignments"][0]
    )

    config_path = tmp_path / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(mutated), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = load_vault_config(config_path)

    assert config.vault.id == mutated["vault"]["id"], "the divergence must not block the load"
    assert any(repr(NARROW_FAILURE_REGEX) in record.getMessage() for record in caplog.records), (
        f"the load must surface the divergence; logged: {[r.getMessage() for r in caplog.records]}"
    )


def test_ts10_the_substrate_schema_cannot_express_this_invariant() -> None:
    """Id-space spanning is a loader invariant, and this records why.

    The relation joins an ``edge_inference`` pattern to the
    ``metadata_schema`` of a doc_type declared in a sibling section. The
    edge-inference schema validates its own section in isolation and has no
    way to reach the other, so it accepts the divergent pattern cleanly.
    Asserting that here keeps the division visible: a later schema edit that
    appeared to cover this would fail this test rather than silently
    duplicating -- or contradicting -- the loader check.
    """
    block = _edge_inference_block_with_pattern(_failure_pattern(NARROW_FAILURE_REGEX))
    validator = jsonschema.Draft202012Validator(_load_edge_inference_schema())
    schema_errors = list(validator.iter_errors(block))
    assert schema_errors == [], (
        "the structural schema must accept the divergent pattern; if it has "
        "started rejecting it, the invariant has moved and the loader check "
        "needs revisiting rather than both surfaces enforcing it"
    )
    assert identifier_mention_id_space_warnings(
        _edge_inference(_failure_pattern(NARROW_FAILURE_REGEX)),
        [_doc_type("failure_record", "failure_id", FAILURE_ID_PATTERN)],
    ), "the loader check is the surface that does catch it"


def test_ts3_constructing_a_canonical_config_logs_no_id_space_warning(caplog) -> None:
    """Construction is silent on a config whose patterns do span their spaces.

    TS9's config diverges, so it can only show that the loader reports a
    divergence; it cannot show that the loader stays quiet when there is none.
    This is that arm, and it has to observe construction rather than call the
    function again afterwards: a re-call would re-derive the answer from the
    same inputs and say nothing about what ``model_post_init`` did with them.
    So the assertion reads what construction logged, and a check that
    over-fired on the canonical pattern set -- the ADR leg in particular,
    whose regex matches a prefixed literal its schema never admits -- would
    put a record in here.
    """
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = VaultConfig.model_validate(
            {
                "vault": {
                    "id": "canonical_probe",
                    "name": "Canonical Probe",
                    "owner": "testuser",
                    "storage_root": "/tmp/sources",
                    "brain_root": "/tmp/brain",
                    "visibility": "personal",
                },
                "document_types": {
                    "doc_types": [
                        {
                            "value": "adr",
                            "label": "ADR",
                            "metadata_schema": {
                                "type": "object",
                                "properties": {
                                    "adr_id": {"type": "string", "pattern": ADR_ID_PATTERN}
                                },
                            },
                        },
                        {
                            "value": "ticket",
                            "label": "Ticket",
                            "metadata_schema": {
                                "type": "object",
                                "properties": {
                                    "ticket_id": {"type": "string", "pattern": TICKET_ID_PATTERN}
                                },
                            },
                        },
                        {
                            "value": "failure_record",
                            "label": "Failure Record",
                            "metadata_schema": {
                                "type": "object",
                                "properties": {
                                    "failure_id": {"type": "string", "pattern": FAILURE_ID_PATTERN}
                                },
                            },
                        },
                    ],
                },
                "lifecycle": {
                    "base_states_required": True,
                    "states": [
                        {"value": "active", "label": "Active"},
                        {"value": "completed", "label": "Completed"},
                        {"value": "archived", "label": "Archived", "is_terminal": True},
                    ],
                    "transitions": [
                        {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                        {
                            "from_state": "active",
                            "action": "supersede",
                            "to_state": "archived",
                            "creates_edge": "supersedes",
                        },
                        {"from_state": "active", "action": "complete", "to_state": "completed"},
                        {"from_state": "active", "action": "archive", "to_state": "archived"},
                        {"from_state": "archived", "action": "reactivate", "to_state": "active"},
                    ],
                },
                "metadata_extraction": {},
                "edge_inference": _edge_inference(*CAS_IDENTIFIER_MENTION_PATTERNS),
            }
        )
    assert config.vault.id == "canonical_probe"
    assert [record.getMessage() for record in caplog.records] == [], (
        "constructing a config whose patterns span their declared identifier "
        "spaces must log nothing"
    )
