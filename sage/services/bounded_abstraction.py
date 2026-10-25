"""Authored evidence and conservative source-scoped generation allowances (ADR-059)."""

import json
from dataclasses import dataclass

from sage.adapters.interfaces import BoundedEvidenceText, NonRetryableAbstractionError
from sage.config import VaultAbstractionConfig
from sage.models.coverage import CoverageDimension, ProcessingIdentity
from sage.models.discovery import (
    DerivedObservation,
    EvidencePackage,
)


class AbstractionAllowanceExhausted(NonRetryableAbstractionError):
    """A deterministic allowance cannot be recovered by transient retries."""


@dataclass(frozen=True)
class AbstractionRequest:
    text: BoundedEvidenceText
    provider_text: BoundedEvidenceText
    identity: ProcessingIdentity
    selected_words: int
    breadth: int
    target_tokens: int
    evidence: CoverageDimension
    observations: tuple[DerivedObservation, ...]


def abstraction_request(
    package: EvidencePackage,
    config: VaultAbstractionConfig,
    doc_type: str | None = None,
    *,
    generation: int | None = None,
) -> AbstractionRequest:
    """Supply authored evidence and scoped deterministic observations as data.

    Observations come only from the internal selection package. The caller
    admits its source/selection identity against the current processing attempt
    before dispatch. No source string or caller metadata is parsed as a fact.
    """
    from sage.adapters.abstraction_prompt import _format_system_prompt, wrap_source_document
    from sage.services.discovery_selection import _spread

    def render(excerpts: tuple) -> str:
        # Native labels are supplied once per group; repeated observations are
        # not appended. Every excerpt is still a complete authored input.
        groups: dict[tuple[str, str], list[str]] = {}
        for excerpt in excerpts:
            groups.setdefault((excerpt.region, excerpt.field_path), []).append(excerpt.input_text)
        return (
            "Authored excerpts (data)\n"
            + "\n\n".join(
                f"Source region: {region}\nSource field: {field}\n" + "\n".join(inputs)
                for (region, field), inputs in groups.items()
            )
            if excerpts
            else ""
        )

    def role_text(excerpts: tuple) -> str:
        # Keep structural labels distinct from authored values and their context.
        # These records describe retained excerpts, not native numeric values or
        # computed observations. JSON quoting keeps source-authored fake labels
        # inside their own data values.
        rows = [
            json.dumps(
                {
                    "region": x.region,
                    "field": x.field_path,
                    "location": x.location,
                    "classification": x.classification,
                    "context": x.context,
                    "structural_label"
                    if x.classification == "structural_label"
                    else "authored_value": x.rendering,
                },
                ensure_ascii=False,
            )
            for x in excerpts
        ]
        return (
            "Selected source excerpts (data; roles preserved)\n" + "\n".join(rows) if rows else ""
        )

    def observations_text(observations: tuple[DerivedObservation, ...]) -> str:
        rows = []
        for observation in observations:
            # Selection's distinct/repetition counters concern retained rendered
            # candidates, which may be field labels or context-bearing inputs.
            # Only the independently inventoried inspected-value count has the
            # source-value meaning required for supplied computed evidence.
            rows.append(
                json.dumps(
                    {
                        "operation": "count_inspected_values",
                        "unit": "source values",
                        "value": observation.populated,
                        "region": observation.region,
                        "field": observation.field_path,
                        "classification": observation.classification,
                        "scope": "inspected region and field",
                        "exactness": "exact over inspected scope",
                        "inspection_complete": package.inspection_complete,
                        "inspection_boundary": package.boundary,
                        "source_hash": package.identity.source_hash,
                        "processing_generation": generation,
                    },
                    ensure_ascii=False,
                )
            )
        return "\n\nSAGE-computed observations (data)\n" + "\n".join(rows) if rows else ""

    def cost(value: str) -> int:
        return (
            len(
                (
                    _format_system_prompt(doc_type, bounded=True) + wrap_source_document(value)
                ).encode()
            )
            + 1024
        )

    selected = package.selected
    text = render(selected)
    # Fit complete excerpts with stable source-spread positions. This operates
    # only on the already bounded package, never on the full source/projection.
    count = len(selected)
    while (
        text
        and max(cost(text), cost(role_text(selected)) if config.hybrid_description else 0)
        > config.max_input_tokens
    ):
        available = max(0, config.max_input_tokens - cost(""))
        used = max(
            1,
            max(cost(text), cost(role_text(selected)) if config.hybrid_description else 0)
            - cost(""),
        )
        count = min(count - 1, int(count * available / used))
        selected = (
            tuple(package.selected[i] for i in _spread(len(package.selected), count))
            if count
            else ()
        )
        text = render(selected)
    prompt_limited = len(selected) < len(package.selected)
    # Preserve authored inputs first; fit whole observation groups into the
    # remaining complete-prompt allowance. Neither fitting pass scans a source.
    observations = package.observations if selected else ()
    count = len(observations)
    while observations and cost(text + observations_text(observations)) > config.max_input_tokens:
        count //= 2
        observations = (
            tuple(package.observations[i] for i in _spread(len(package.observations), count))
            if count
            else ()
        )
    observation_limited = len(observations) < len(package.observations)
    # Keep the pre-observation check input, including its existing context
    # labels, unchanged. Admitting observations does not recalibrate attestation.
    authored_text = text.removeprefix("Authored excerpts (data)\n")
    provider_text = BoundedEvidenceText(role_text(selected), authored_text=authored_text)
    text = BoundedEvidenceText(text + observations_text(observations), authored_text=authored_text)
    represented_tokens = sum(x.token_count for x in selected)
    words = sum(len(x.input_text.split()) for x in selected)
    breadth = min(32, len({(x.region, x.field_path) for x in selected}))
    target = max(
        1,
        min(
            min(1500, config.max_abstract_tokens),
            int(
                min(1500, config.base_abstract_tokens)
                + words * min(1, config.tokens_per_word)
                + breadth * config.tokens_per_breadth
            ),
        ),
    )
    identity = package.identity.processing_identity()
    complete = (
        package.inspection_complete
        and not package.limitations
        and package.associations_complete
        and not prompt_limited
        and not observation_limited
    )
    evidence = CoverageDimension(
        scope="complete" if complete else "limited",
        identity=identity,
        counts=[
            {
                "unit": "tokens",
                "observed": package.selected_tokens,
                "represented": represented_tokens,
                "total": package.selected_tokens,
            }
        ],
        limitations=list(package.limitations)[:14]
        + (["abstraction_prompt_budget"] if prompt_limited else [])
        + (["abstraction_observation_budget"] if observation_limited else [])
        + (
            []
            if complete
            else [
                "selected_authored_and_scoped_computed_evidence_only"
                if observations
                else "selected_authored_evidence_only"
            ]
        ),
        unknown_remainder=not complete,
    )
    return AbstractionRequest(
        text, provider_text, identity, words, breadth, target, evidence, observations
    )
