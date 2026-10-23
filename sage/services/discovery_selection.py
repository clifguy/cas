"""Deterministic bounded authored evidence selection (CAS-ADR-059).

New projection attempts inspect bounded source facts before inference. Selection
leaves readable projection, keyword indexing and inference inputs untouched.
"""

import hashlib
import heapq
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime

from pydantic import TypeAdapter

from sage.models.coverage import CoverageHash
from sage.models.discovery import (
    AuthoredExcerpt,
    DerivedObservation,
    EvidencePackage,
    OccurrenceRun,
    SelectionConfig,
    SelectionIdentity,
    check_configuration_tree,
)

POLICY_VERSION = "bounded-discovery:1"
_SOURCE_HASH_ADAPTER = TypeAdapter(CoverageHash)


def selection_identity(
    *,
    source_hash: str,
    adapter_version: str,
    tokenizer_identity: str,
    config: SelectionConfig,
    adapter_configuration: dict | None = None,
    fact_configuration: dict | None = None,
    inspected_source_hash: str | None = None,
) -> SelectionIdentity:
    """Capture effective immutable configuration; reject an unusable identity early."""
    from sage.source_adapters.structure import FactLimits

    source_hash = _SOURCE_HASH_ADAPTER.validate_python(
        source_hash if source_hash.startswith("sha256:") else "sha256:" + source_hash
    )
    inspected_source_hash = source_hash if inspected_source_hash is None else inspected_source_hash
    inspected_source_hash = _SOURCE_HASH_ADAPTER.validate_python(
        inspected_source_hash
        if inspected_source_hash.startswith("sha256:")
        else "sha256:" + inspected_source_hash
    )
    configuration = {
        "inspected_source_hash": inspected_source_hash,
        "selection": asdict(config),
        "adapter": adapter_configuration or {},
        "facts": fact_configuration if fact_configuration is not None else asdict(FactLimits()),
    }

    check_configuration_tree(configuration)
    configuration_json = json.dumps(
        configuration, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(configuration_json.encode("utf-8")) > 65536:
        raise ValueError("effective configuration exceeds 65536 bytes")
    identity = SelectionIdentity(
        source_hash if source_hash.startswith("sha256:") else "sha256:" + source_hash,
        adapter_version,
        POLICY_VERSION,
        configuration_json,
        tokenizer_identity,
    )
    identity.processing_identity()
    return identity


_NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def classify(value: object) -> str:
    """Recognize native primitives and strict lexical syntax without field semantics."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (date, datetime)):
        return "date"
    if not isinstance(value, str):
        return "text"
    if value in ("true", "false"):
        return "boolean"
    if _NUMBER.fullmatch(value):
        return "number"
    if _UUID.fullmatch(value):
        return "identifier"
    if _DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
            return "date"
        except ValueError:
            pass
    return "text"


@dataclass
class _Candidate:
    fact: object
    text: str
    classification: str
    runs: list[OccurrenceRun] = field(default_factory=list)
    repetitions: int = 0
    tokens: int = 0
    retained_bytes: int = 0
    group_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)


def _spread(length: int, count: int) -> list[int]:
    if count >= length:
        return list(range(length))
    if count == 1:
        return [length // 2]
    return [i * (length - 1) // (count - 1) for i in range(count)]


def select_discovery(
    facts: Iterable,
    *,
    source_hash: str,
    adapter_version: str,
    tokenizer: Callable[[str], int],
    tokenizer_identity: str,
    config: SelectionConfig = SelectionConfig(),
    cancelled: Callable[[], bool] | None = None,
    adapter_configuration: dict | None = None,
    fact_configuration: dict | None = None,
    inspected_source_hash: str | None = None,
) -> EvidencePackage:
    """Inspect bounded facts, deduplicate full inputs, then fairly select authored text.

    ``tokenizer`` returns a nonnegative integer count and has no inference role.
    A reached inspection ceiling leaves the remainder unknown without another read.
    Exceptions propagate as failed inspection, never completed evidence.
    """
    actual_limits = getattr(facts, "limits", None)
    narrative_input_bytes = getattr(facts, "narrative_input_bytes", None)
    if actual_limits is not None:
        base_configuration = asdict(actual_limits)
        actual_configuration = dict(base_configuration)
        if narrative_input_bytes is not None:
            actual_configuration["narrative_input_bytes"] = narrative_input_bytes
        if fact_configuration is not None and fact_configuration not in (
            base_configuration,
            actual_configuration,
        ):
            raise ValueError("fact configuration differs from effective stream limits")
        fact_configuration = actual_configuration
    elif narrative_input_bytes is not None:
        fact_configuration = dict(fact_configuration or {})
        fact_configuration["narrative_input_bytes"] = narrative_input_bytes
    identity = selection_identity(
        source_hash=source_hash,
        adapter_version=adapter_version,
        tokenizer_identity=tokenizer_identity,
        config=config,
        adapter_configuration=adapter_configuration,
        fact_configuration=fact_configuration,
        inspected_source_hash=inspected_source_hash,
    )
    candidates: dict[str, _Candidate] = {}
    priorities: list[tuple[int, str]] = []
    inventory: dict[tuple[str, str, str], int] = defaultdict(int)
    regions: set[str] = set()
    fields: set[tuple[str, str]] = set()
    limitations: set[str] = set()
    observed = characters = retained = runs = 0
    complete = interrupted = False
    associations_complete = True
    boundary = None
    iterator = iter(facts)
    try:
        while observed < config.max_values:
            if cancelled and cancelled():
                interrupted = True
                limitations.add("interrupted")
                break
            try:
                fact = next(iterator)
            except StopIteration:
                complete = True
                break
            # Structural text is bounded before storing keys or constructing inputs.
            if any(len(x) > 512 for x in (fact.region, fact.field_path, fact.location)):
                limitations.add("location_characters")
                break
            if fact.region not in regions and len(regions) >= config.max_regions:
                limitations.add("regions")
                break
            group = (fact.region, fact.field_path)
            if group not in fields and len(fields) >= config.max_fields:
                limitations.add("fields")
                break
            size = len(fact.rendering)
            if characters + size > config.max_characters:
                limitations.add("inspection_characters")
                break
            observed += 1
            characters += size
            boundary = fact.location
            regions.add(fact.region)
            fields.add(group)
            classification = classify(fact.value)
            inventory[(*group, classification)] += 1
            # Machine-shaped scalar values contribute observations. Their authored
            # field paths remain attached to retained text; no domain meaning applies.
            inventory_key = (*group, classification)
            if classification != "text":
                labels = getattr(fact, "authored_labels", ())
                if not labels:
                    continue
                from sage.source_adapters.structure import SourceFact

                fact = SourceFact(
                    fact.region,
                    fact.field_path,
                    fact.location,
                    fact.order,
                    fact.record,
                    labels[-1],
                    labels[-1],
                    "\n".join(labels[:-1]),
                )
                classification = "structural_label"
            if not fact.rendering.strip():
                continue
            length = len(fact.rendering) + len(fact.context) + (2 if fact.context else 0)
            if length > config.max_candidate_characters:
                limitations.add("candidate_characters")
                labels = getattr(fact, "authored_labels", ())
                if not labels:
                    continue
                from sage.source_adapters.structure import SourceFact

                fact = SourceFact(
                    fact.region,
                    fact.field_path,
                    fact.location,
                    fact.order,
                    fact.record,
                    labels[-1],
                    labels[-1],
                    "\n".join(labels[:-1]),
                )
                classification = "structural_label"
                if len(fact.rendering) + len(fact.context) + 2 > config.max_candidate_characters:
                    continue
            text = f"{fact.context}\n\n{fact.rendering}" if fact.context else fact.rendering
            candidate = candidates.get(text)
            if candidate is None:
                # A fixed-size dictionary retains lexicographically lowest SHA-256
                # priorities. This deterministic reservoir admits late source text.
                encoded = text.encode("utf-8")
                byte_size = len(encoded)
                priority = int.from_bytes(hashlib.sha256(encoded).digest())
                if byte_size > config.max_retained_bytes:
                    limitations.add("candidate_retention")
                    associations_complete = False
                    continue
                while (
                    len(candidates) >= config.max_candidates
                    or retained + byte_size > config.max_retained_bytes
                ):
                    limitations.add("candidate_retention")
                    associations_complete = False
                    if not priorities or priority >= -priorities[0][0]:
                        break
                    _, worst = heapq.heappop(priorities)
                    removed = candidates.pop(worst)
                    retained -= removed.retained_bytes
                    runs -= len(removed.runs)
                if (
                    len(candidates) >= config.max_candidates
                    or retained + byte_size > config.max_retained_bytes
                ):
                    continue
                candidate = _Candidate(fact, text, classification, retained_bytes=byte_size)
                candidates[text] = candidate
                heapq.heappush(priorities, (-priority, text))
                retained += byte_size
            else:
                candidate.repetitions += 1
            if inventory_key in candidate.group_counts:
                candidate.group_counts[inventory_key] += 1
            elif retained + 4224 <= config.max_retained_bytes:
                candidate.group_counts[inventory_key] = 1
                candidate.retained_bytes += 4224
                retained += 4224
            else:
                associations_complete = False
                limitations.add("association_groups")
            if candidate.runs and candidate.runs[-1].end + 1 == fact.order:
                old = candidate.runs[-1]
                candidate.runs[-1] = replace(
                    old, end=fact.order, last_location=fact.location, count=old.count + 1
                )
            elif (
                runs < config.max_association_runs
                and retained + 4096 + 64 <= config.max_retained_bytes
            ):
                candidate.runs.append(
                    OccurrenceRun(fact.order, fact.order, fact.location, fact.location, 1)
                )
                association_bytes = 4096 + 64
                retained += association_bytes
                candidate.retained_bytes += association_bytes
                runs += 1
            else:
                associations_complete = False
                limitations.add("association_runs")
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    if observed >= config.max_values and not complete:
        limitations.add("inspection_values")
    stream_limitations = tuple(getattr(facts, "limitations", ()))
    limitations.update(stream_limitations)
    if stream_limitations:
        complete = False
    if interrupted:
        selected_candidates = []
    else:
        groups: dict[tuple[str, str], list[_Candidate]] = defaultdict(list)
        for candidate in candidates.values():
            count = tokenizer(candidate.text)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("tokenizer must return a nonnegative integer token count")
            candidate.tokens = count
            groups[(candidate.fact.region, candidate.fact.field_path)].append(candidate)
        for group_values in groups.values():
            group_values.sort(key=lambda c: (c.fact.order, c.fact.location, c.text))
        all_candidates = sorted(
            candidates.values(), key=lambda c: (c.fact.order, c.fact.location, c.text)
        )
        if (
            len(all_candidates) <= config.max_excerpts
            and sum(c.tokens for c in all_candidates) <= config.max_tokens
        ):
            selected_candidates = all_candidates
        else:
            limitations.add("selection_budget")
            keys = sorted(groups)
            allocations = dict.fromkeys(keys, 0)
            slots = min(config.max_excerpts, len(all_candidates))
            while slots:
                progressed = False
                for key in keys:
                    if slots and allocations[key] < len(groups[key]):
                        allocations[key] += 1
                        slots -= 1
                        progressed = True
                if not progressed:
                    break
            selected_candidates = []
            remaining_tokens = config.max_tokens
            # Equal group token shares prevent an early large field spending all
            # tokens. An unused share is redistributed in a second bounded pass.
            queues = {
                k: [groups[k][i] for i in _spread(len(groups[k]), allocations[k])] for k in keys
            }
            quota = config.max_tokens // max(len(keys), 1)
            pending = []
            for key in keys:
                spent = 0
                for candidate in queues[key]:
                    if candidate.tokens <= quota - spent:
                        selected_candidates.append(candidate)
                        spent += candidate.tokens
                        remaining_tokens -= candidate.tokens
                    else:
                        pending.append(candidate)
            for candidate in pending:
                if candidate.tokens <= remaining_tokens:
                    selected_candidates.append(candidate)
                    remaining_tokens -= candidate.tokens
            selected_candidates.sort(key=lambda c: (c.fact.order, c.fact.location, c.text))
    retained_counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for candidate in candidates.values():
        for key, count in candidate.group_counts.items():
            retained_counts[key][0] += 1
            retained_counts[key][1] += count - 1
    observations = tuple(
        DerivedObservation(*key, populated, *retained_counts[key])
        for key, populated in sorted(inventory.items())
    )
    selected = tuple(
        AuthoredExcerpt(
            c.text,
            c.fact.rendering,
            c.fact.context,
            c.fact.region,
            c.fact.field_path,
            c.fact.location,
            c.fact.order,
            c.tokens,
            c.classification,
            tuple(c.runs),
        )
        for c in selected_candidates
    )
    return EvidencePackage(
        identity,
        observed,
        characters,
        selected,
        tuple(observations),
        tuple(sorted(limitations)),
        complete,
        interrupted,
        associations_complete,
        sum(c.token_count for c in selected),
        boundary,
    )
