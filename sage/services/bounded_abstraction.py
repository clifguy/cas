"""Authored evidence and conservative source-scoped generation allowances (ADR-059)."""

from dataclasses import dataclass

from sage.adapters.interfaces import NonRetryableAbstractionError
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
    text: str
    identity: ProcessingIdentity
    selected_words: int
    breadth: int
    target_tokens: int
    evidence: CoverageDimension
    observations: tuple[DerivedObservation, ...]


def abstraction_request(
    package: EvidencePackage, config: VaultAbstractionConfig, doc_type: str | None = None
) -> AbstractionRequest:
    """Describe supplied authored excerpts; derived observations stay outside input."""
    from sage.adapters.abstraction_prompt import _format_system_prompt, wrap_source_document
    from sage.services.discovery_selection import _spread

    def render(excerpts: tuple) -> str:
        # Native labels are supplied once per group; repeated observations are
        # not appended. Every excerpt is still a complete authored input.
        groups: dict[tuple[str, str], list[str]] = {}
        for excerpt in excerpts:
            groups.setdefault((excerpt.region, excerpt.field_path), []).append(excerpt.input_text)
        return "\n\n".join(
            f"Source region: {region}\nSource field: {field}\n" + "\n".join(inputs)
            for (region, field), inputs in groups.items()
        )

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
    while text and cost(text) > config.max_input_tokens:
        available = max(0, config.max_input_tokens - cost(""))
        used = max(1, cost(text) - cost(""))
        count = min(count - 1, int(count * available / used))
        selected = (
            tuple(package.selected[i] for i in _spread(len(package.selected), count))
            if count
            else ()
        )
        text = render(selected)
    prompt_limited = len(selected) < len(package.selected)
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
        + ([] if complete else ["selected_authored_evidence_only"]),
        unknown_remainder=not complete,
    )
    return AbstractionRequest(
        text, identity, words, breadth, target, evidence, package.observations
    )
