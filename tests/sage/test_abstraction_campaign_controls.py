"""Frozen controls must exercise the admitted value/context distinction they claim."""

import hashlib
import json
from pathlib import Path

import pytest

from sage.config import VaultAbstractionConfig
from sage.models.abstraction_evidence import decode_package, encode_package
from sage.services.bounded_abstraction import abstraction_request
from sage.services.discovery_selection import select_discovery
from sage.source_adapters.structure import FactLimits, source_facts

SPEC = Path(__file__).resolve().parents[2] / "docs/development/abstraction-regression-campaign.json"


def frozen_case(name):
    # Keep this probe on the frozen shared package/request path. Do not repair or
    # reconstruct a classification inside the assertion helper.
    import base64

    case = next(c for c in json.loads(SPEC.read_text())["cases"] if c["id"] == name)
    source = base64.b64decode(case["source"]["base64"], validate=True)
    evidence = base64.b64decode(case["evidence"]["base64"], validate=True)
    return case, source, decode_package(json.loads(evidence))


@pytest.mark.parametrize("hybrid", [False, True])
def test_authored_string_is_admitted_text_with_matched_numeric_context(hybrid):
    _, text_source, text_package = frozen_case("authored_string")
    _, numeric_source, numeric_package = frozen_case("numeric_context")
    config = json.loads(SPEC.read_text())["request_config"]
    text_request = abstraction_request(
        text_package, VaultAbstractionConfig(**config, hybrid_description=hybrid), generation=1
    )
    numeric_request = abstraction_request(
        numeric_package, VaultAbstractionConfig(**config, hybrid_description=hybrid), generation=1
    )
    amount = next(x for x in text_package.selected if x.field_path == '$["amount"]')
    assert amount.classification == "text", "the authored-string control must supply admitted text"
    assert amount.rendering == '"rows"'
    assert amount.context == '"amount"'
    assert '"rows"' in text_request.provider_text.authored_text
    assert "987654321" not in numeric_request.provider_text.authored_text
    assert (
        next(x for x in numeric_package.selected if x.field_path == '$["amount"]').classification
        == "structural_label"
    )
    assert (
        next(x for x in text_package.observations if x.field_path == '$["amount"]').classification
        == "text"
    )
    assert (
        next(
            x for x in numeric_package.observations if x.field_path == '$["amount"]'
        ).classification
        == "number"
    )
    text_fields, numeric_fields = json.loads(text_source), json.loads(numeric_source)
    assert text_fields.pop("amount") == "rows"
    assert numeric_fields.pop("amount") == 987654321
    assert text_fields == numeric_fields  # Only the tested value differs.
    if hybrid:
        rows = [json.loads(line) for line in text_request.provider_text.splitlines()[1:]]
        supplied = next(row for row in rows if row["field"] == '$["amount"]')
        assert supplied["classification"] == "text"
        assert supplied["authored_value"] == '"rows"'
        assert supplied["context"] == '"amount"'
        assert "structural_label" not in supplied


@pytest.mark.parametrize("name", ["numeric_context", "authored_string"])
def test_control_frozen_evidence_matches_native_source_discovery(name, tmp_path):
    case, source, package = frozen_case(name)
    native = tmp_path / "source.json"
    native.write_bytes(source)
    source_hash = "sha256:" + hashlib.sha256(source).hexdigest()
    rediscovered = select_discovery(
        source_facts(native, None, FactLimits()),
        source_hash=source_hash,
        adapter_version="diagnostic-synthetic:1",
        tokenizer=lambda text: len(text.encode()),
        tokenizer_identity="utf8-byte-tokens:1",
    )
    assert encode_package(package) == encode_package(rediscovered)
    assert case["source_generation"] == source_hash
    evidence = json.dumps(
        encode_package(rediscovered), sort_keys=True, separators=(",", ":")
    ).encode()
    assert case["selection_id"] == hashlib.sha256(evidence).hexdigest()
    assert case["evidence"]["sha256"] == hashlib.sha256(evidence).hexdigest()
