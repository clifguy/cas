"""Durable package/accounting shapes derived from abstraction_evidence.schema.json."""

import json
from dataclasses import asdict

from sage.models.coverage import ProcessingIdentity
from sage.models.discovery import (
    AuthoredExcerpt,
    DerivedObservation,
    EvidencePackage,
    OccurrenceRun,
    SelectionIdentity,
)


def encode_package(package: EvidencePackage) -> dict:
    """Bound serialization independently of source size before persisting."""
    data = asdict(package)
    if len(json.dumps(data).encode()) > 32 * 1024 * 1024:
        raise ValueError("abstraction evidence exceeds durable package bound")
    return data


def decode_package(data: dict) -> EvidencePackage:
    """Restore only typed authored evidence, never arbitrary discovery prose."""
    selected = tuple(
        AuthoredExcerpt(
            **{**x, "associations": tuple(OccurrenceRun(**v) for v in x["associations"])}
        )
        for x in data["selected"]
    )
    package = EvidencePackage(
        **{
            **data,
            "identity": SelectionIdentity(**data["identity"]),
            "selected": selected,
            "observations": tuple(DerivedObservation(**x) for x in data["observations"]),
            "limitations": tuple(data["limitations"]),
        }
    )
    encode_package(package)
    # Validate retained identity and bounds without any source-wide tokenization.
    package.identity.processing_identity()
    captured = json.loads(package.identity.configuration_json).get("selection", {})
    ceiling = captured.get("max_tokens", 8192)
    if package.selected_tokens > ceiling or len(selected) > captured.get("max_excerpts", 1024):
        raise ValueError("stored abstraction evidence exceeds selected allowance")
    if sum(x.token_count for x in selected) != package.selected_tokens:
        raise ValueError("stored abstraction token accounting mismatch")
    return package


def reserve_generation(
    previous: dict | None,
    identity: ProcessingIdentity,
    input_tokens: int,
    output_tokens: int,
    *,
    max_calls: int,
    max_input: int,
    max_output: int,
) -> tuple[str, dict]:
    """Reserve upper bounds atomically; uncertain dispatch remains consumed."""
    from sage.models.coverage import canonical_configuration_hash

    if (
        not 1 <= max_calls <= 3
        or not 1 <= max_input <= 98304
        or not 1 <= max_output <= 4500
        or min(input_tokens, output_tokens) < 0
    ):
        raise ValueError("invalid abstraction allowance")
    key = canonical_configuration_hash(identity.model_dump(mode="json"))
    ledger = dict(previous or {})
    if ledger.get("source") not in (None, identity.source_hash):
        ledger = {}
    limits = {"max_calls": max_calls, "max_input": max_input, "max_output": max_output}
    if ledger and (ledger["identity"] != key or ledger.get("limits") != limits):
        return "scope_changed", ledger
    if not ledger:
        ledger = dict(
            source=identity.source_hash,
            identity=key,
            calls=0,
            input_tokens=0,
            output_tokens=0,
            limits=limits,
        )
    if (
        ledger["calls"] >= max_calls
        or ledger["input_tokens"] + input_tokens > max_input
        or ledger["output_tokens"] + output_tokens > max_output
    ):
        return "limited", ledger
    ledger.update(
        calls=ledger["calls"] + 1,
        input_tokens=ledger["input_tokens"] + input_tokens,
        output_tokens=ledger["output_tokens"] + output_tokens,
    )
    return "admitted", ledger
