"""Behavioral specifications for shared deterministic evidence selection."""

from types import SimpleNamespace

import pytest

from sage.services.discovery_selection import SelectionConfig, select_discovery


def fact(value, order, field="body", region="document", context=""):
    return SimpleNamespace(
        value=value,
        rendering=str(value),
        order=order,
        field_path=field,
        region=region,
        context=context,
        location=f"row:{order}",
        record=str(order),
    )


def select(facts, **kwargs):
    return select_discovery(
        facts,
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer=lambda text: len(text.split()),
        tokenizer_identity="words:1",
        **kwargs,
    )


def test_fitting_preserves_every_authored_candidate_and_exact_inputs():
    package = select([fact("alpha", 0), fact("beta", 1), fact("alpha", 2), fact("alpha!", 3)])
    assert package is not None, "selection must return established evidence"
    assert [x.input_text for x in package.selected] == ["alpha", "beta", "alpha!"]
    assert package.selected[0].associations[1].start == 2
    assert package.inspection_complete
    assert package.observed_values == 4
    assert package.selected_tokens == 3


def test_fair_source_spread_has_explicit_endpoints_and_later_field():
    facts = [fact(f"v{i}", i, field="first") for i in range(6)]
    facts += [fact("later text", 6, field="second")]
    package = select(facts, config=SelectionConfig(max_excerpts=3))
    assert package is not None, "selection must return established evidence"
    assert [(x.field_path, x.order) for x in package.selected] == [
        ("first", 0),
        ("first", 5),
        ("second", 6),
    ]
    assert "selection_budget" in package.limitations


def test_cutoff_does_not_advance_iterator_and_cancellation_is_interruption():
    seen = []

    def values():
        for i in range(10):
            seen.append(i)
            yield fact("text", i)

    package = select(values(), config=SelectionConfig(max_values=2))
    assert package is not None, "selection must return established evidence"
    assert seen == [0, 1]
    assert not package.inspection_complete
    assert package.observed_values == 2
    assert package.boundary == "row:1"
    interrupted = select([fact("text", 0)], cancelled=lambda: True)
    assert interrupted.interrupted
    assert not interrupted.inspection_complete
    assert interrupted.selected == ()


def test_uncertainty_and_context_prevent_false_deduplication():
    package = select(
        [
            fact("2026-13-40", 0, field="severity"),
            fact("same", 1, context="one"),
            fact("same", 2, context="two"),
            fact(12, 3),
            fact(True, 4),
        ]
    )
    assert package is not None, "selection must return established evidence"
    assert any(x.rendering == "2026-13-40" for x in package.selected)
    assert package.selected[0].classification == "text"
    assert len(package.selected) == 3
    assert {x.classification for x in package.observations} >= {"number", "boolean", "text"}


def test_associations_and_tokenizer_work_are_bounded_before_calls():
    calls = []
    package = select_discovery(
        [fact("x" * 40, 0), fact("short", 1), fact("short", 3)],
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer_identity="count:1",
        tokenizer=lambda text: calls.append(len(text)) or 1,
        config=SelectionConfig(max_candidate_characters=8, max_association_runs=1),
    )
    assert package is not None, "selection must return established evidence"
    assert calls == [5]
    assert not package.associations_complete
    assert "candidate_characters" in package.limitations
    assert package.selected[0].input_text == "short"


@pytest.mark.parametrize("kwargs", [{"max_values": 0}, {"max_tokens": 32769}, {"max_values": True}])
def test_configuration_refuses_invalid_or_over_hard_limits(kwargs):
    with pytest.raises(ValueError):
        SelectionConfig(**kwargs)


def test_effective_identity_freezes_adapter_and_fact_configuration():
    adapter = {"preview_rows": 5}
    facts = {"max_rows": 9}
    package = select_discovery(
        [fact("text", 0)],
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer=len,
        tokenizer_identity="characters:1",
        adapter_configuration=adapter,
        fact_configuration=facts,
    )
    adapter["preview_rows"] = 99
    facts["max_rows"] = 100
    import json

    captured = json.loads(package.identity.configuration_json)
    assert captured["adapter"] == {"preview_rows": 5}
    assert captured["facts"] == {"max_rows": 9}
    assert captured["selection"]["max_values"] == 100000


def test_retention_admits_late_inputs_and_payload_exhaustion_is_honest():
    import hashlib

    values = sorted(
        ["alpha", "beta", "gamma", "delta"],
        key=lambda x: hashlib.sha256(x.encode()).digest(),
        reverse=True,
    )
    package = select(
        [fact(v, i) for i, v in enumerate(values)], config=SelectionConfig(max_candidates=1)
    )
    assert [x.rendering for x in package.selected] == [values[-1]]
    assert package.inspection_complete
    assert not package.associations_complete
    assert "candidate_retention" in package.limitations
    bounded = select([fact("alpha", 0)], config=SelectionConfig(max_retained_bytes=5))
    assert not bounded.associations_complete
    assert bounded.selected[0].associations == ()


def test_duplicate_counts_are_attributed_to_each_observed_field():
    package = select(
        [fact("same", 0, field="a"), fact("same", 1, field="b"), fact("same", 2, field="b")]
    )
    observed = {
        x.field_path: (x.populated, x.retained_distinct, x.known_repetitions)
        for x in package.observations
    }
    assert observed == {"a": (1, 1, 0), "b": (2, 1, 1)}
    assert len(package.selected) == 1
    assert package.selected[0].associations[0].count == 3


def test_native_numeric_map_preserves_authored_key_labels():
    from sage.source_adapters.structure import SourceFact

    package = select(
        [
            SourceFact(
                "document",
                '$["measure"]',
                '$["measure"]',
                0,
                "document",
                42,
                "42",
                authored_labels=("measure",),
            )
        ]
    )
    assert [(x.rendering, x.classification) for x in package.selected] == [
        ("measure", "structural_label")
    ]
    assert package.observations[0].classification == "number"


def test_oversized_scalar_retains_authored_label_without_tokenizing_value():
    from sage.source_adapters.structure import SourceFact

    calls = []
    package = select_discovery(
        [
            SourceFact(
                "document",
                '$["note"]',
                '$["note"]',
                0,
                "document",
                "x" * 100,
                "x" * 100,
                authored_labels=("note",),
            )
        ],
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer=lambda text: calls.append(text) or len(text),
        tokenizer_identity="characters:1",
        config=SelectionConfig(max_candidate_characters=8),
    )
    assert calls == ["note"]
    assert package.selected[0].rendering == "note"
    assert package.selected[0].classification == "structural_label"
    assert "candidate_characters" in package.limitations
    assert package.observed_characters == 100


def test_large_effective_configuration_has_frozen_bounded_coverage_digest():
    from sage.services.discovery_selection import selection_identity

    config = {"heading_style_map": {"A" * 5000: 1}}
    identity = selection_identity(
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer_identity="bytes:1",
        config=SelectionConfig(),
        adapter_configuration=config,
    )
    coverage = identity.processing_identity()
    config["heading_style_map"].clear()
    assert coverage.configuration["adapter_configuration_omitted"] is True
    assert coverage.configuration["adapter_configuration_sha256"].startswith("sha256:")
    assert identity.processing_identity() == coverage
    assert "A" * 5000 in identity.configuration_json
    assert len(coverage.model_dump_json().encode()) < 4096
    changed = selection_identity(
        source_hash="0" * 64,
        adapter_version="fixture:1",
        tokenizer_identity="bytes:1",
        config=SelectionConfig(),
        adapter_configuration={"heading_style_map": {"B" * 5000: 1}},
    )
    assert changed.processing_identity() != coverage


@pytest.mark.parametrize("adapter", [{"x": "a" * 65537}, {"x": [None] * 4097}])
def test_effective_configuration_is_bounded_before_serialization(adapter, monkeypatch):
    import sage.services.discovery_selection as module

    monkeypatch.setattr(module.json, "dumps", lambda *a, **k: pytest.fail("encoder reached"))
    with pytest.raises(ValueError, match="configuration exceeds"):
        module.selection_identity(
            source_hash="0" * 64,
            adapter_version="fixture:1",
            tokenizer_identity="bytes:1",
            config=SelectionConfig(),
            adapter_configuration=adapter,
        )


@pytest.mark.parametrize("kind", ["discovery_selection", "discovery_facts"])
def test_published_policy_schema_matches_actual_configuration_boundaries(kind):
    import json
    from dataclasses import asdict
    from pathlib import Path

    import jsonschema

    from sage.source_adapters.structure import FactLimits

    runtime = SelectionConfig if kind == "discovery_selection" else FactLimits
    schema = json.loads(
        (Path(__file__).parents[2] / "docs/fs/sage/vault_config.schema.json").read_text()
    )
    fragment = dict(schema["properties"]["adapter_defaults"]["additionalProperties"])
    fragment["$defs"] = schema.get("$defs", {})
    validator = jsonschema.Draft202012Validator(fragment)
    for name, maximum in runtime.hard_limits().items():
        for invalid in (0, maximum + 1, True):
            with pytest.raises(jsonschema.ValidationError):
                validator.validate({kind: {name: invalid}})
            with pytest.raises(ValueError):
                runtime(**{name: invalid})
        validator.validate({kind: {name: maximum}})
        runtime(**{name: maximum})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({kind: {"unknown_option": 1}})
    published = schema["$defs"][kind]["properties"]
    assert {k: v["default"] for k, v in published.items()} == asdict(runtime())


@pytest.mark.parametrize("unit,budget", [("a", None), ("🙂", None), ("a", 8), ("🙂", 8)])
def test_actual_narrative_factory_selects_usable_default_and_small_budget(tmp_path, unit, budget):
    from sage.source_adapters.base import ProjectionResult
    from sage.source_adapters.structure import FactLimits, source_facts

    text = unit * 32768
    config = SelectionConfig() if budget is None else SelectionConfig(max_tokens=budget)
    projection = ProjectionResult(text, [], "hash", "1", "title")
    facts = source_facts(
        tmp_path / "source.md", projection, FactLimits(), narrative_input_bytes=budget
    )
    package = select_discovery(
        facts,
        source_hash="0" * 64,
        adapter_version="markdown:1",
        tokenizer=lambda value: len(value.encode()),
        tokenizer_identity="utf8-byte-tokens:1",
        config=config,
    )
    assert package.selected
    assert all(len(item.input_text.encode()) <= config.max_tokens for item in package.selected)
    assert package.selected_tokens <= config.max_tokens
    assert package.inspection_complete


def test_narrative_context_alone_over_budget_is_honestly_excluded(tmp_path):
    from sage.source_adapters.base import HeadingNode, ProjectionResult
    from sage.source_adapters.structure import FactLimits, source_facts

    context = "Authored context"
    projection = ProjectionResult(
        "body", [HeadingNode(1, context, context, "body")], "hash", "1", "title"
    )
    facts = source_facts(tmp_path / "source.md", projection, FactLimits(), narrative_input_bytes=4)
    values = list(facts)
    assert values and all(fact.context == context for fact in values)
    assert "".join(fact.value for fact in values) == "body"
    package = select_discovery(
        values,
        source_hash="0" * 64,
        adapter_version="markdown:1",
        tokenizer=lambda value: len(value.encode()),
        tokenizer_identity="utf8-byte-tokens:1",
        config=SelectionConfig(max_tokens=4),
    )
    assert package.selected == ()
    assert "selection_budget" in package.limitations


def test_narrative_effective_segmentation_budget_changes_captured_identity(tmp_path):
    from sage.source_adapters.base import ProjectionResult
    from sage.source_adapters.structure import FactLimits, source_facts

    projection = ProjectionResult("x" * 256, [], "hash", "1", "title")
    identities = []
    for budget in (64, 128):
        facts = source_facts(
            tmp_path / "source.md", projection, FactLimits(), narrative_input_bytes=budget
        )
        package = select_discovery(
            facts,
            source_hash="0" * 64,
            adapter_version="markdown:1",
            tokenizer=lambda value: len(value.encode()),
            tokenizer_identity="utf8-byte-tokens:1",
        )
        actual = package.identity.processing_identity().configuration["facts"]
        assert actual["narrative_input_bytes"] == budget
        identities.append(package.identity)
    assert identities[0] != identities[1]


def test_narrative_declared_budget_must_match_actual_stream(tmp_path):
    from dataclasses import asdict

    from sage.source_adapters.base import ProjectionResult
    from sage.source_adapters.structure import FactLimits, source_facts

    limits = FactLimits()
    projection = ProjectionResult("x" * 256, [], "hash", "1", "title")
    facts = source_facts(tmp_path / "source.md", projection, limits, narrative_input_bytes=64)
    declared = {**asdict(limits), "narrative_input_bytes": 128}
    with pytest.raises(ValueError, match="fact configuration differs"):
        select_discovery(
            facts,
            source_hash="0" * 64,
            adapter_version="markdown:1",
            tokenizer=lambda value: len(value.encode()),
            tokenizer_identity="utf8-byte-tokens:1",
            fact_configuration=declared,
        )
