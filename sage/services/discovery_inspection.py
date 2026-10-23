"""Attempt-fenced publication of deterministic discovery inspection.

Callers capture the effective identity before starting selection. This seam
publishes only established inspection counts; selecting excerpts establishes no
embedding or abstraction achievement. Ingestion publishes as inspection settles.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore
from sage.models.coverage import CoverageDimension, ProcessingIdentity, SelectionSummary

if TYPE_CHECKING:
    from sage.services.discovery_selection import EvidencePackage, SelectionConfig


@dataclass(frozen=True)
class InspectionAttempt:
    """Immutable caller-local binding to a durable attempt and effective inputs."""

    document_id: str
    generation: int
    identity_json: str


def capture_inspection_attempt(
    document_id: str,
    generation: int,
    *,
    source_hash: str,
    adapter_version: str,
    tokenizer_identity: str,
    config: "SelectionConfig | None" = None,
    adapter_configuration: dict | None = None,
    fact_configuration: dict | None = None,
) -> InspectionAttempt:
    """Freeze configuration through the same identity authority as selection."""
    from sage.services.discovery_selection import SelectionConfig, selection_identity

    if generation < 1 or not document_id:
        raise ValueError("inspection requires a document and durable processing attempt")
    identity = selection_identity(
        source_hash=source_hash,
        adapter_version=adapter_version,
        tokenizer_identity=tokenizer_identity,
        config=config if config is not None else SelectionConfig(),
        adapter_configuration=adapter_configuration,
        fact_configuration=fact_configuration,
    ).processing_identity()
    return InspectionAttempt(document_id, generation, identity.model_dump_json())


async def publish_inspection(
    store: ContentStore, attempt: InspectionAttempt, package: "EvidencePackage"
) -> bool:
    """Reconcile evidence before publishing once through the durable attempt fence.

    A false result means the durable attempt was superseded. An identity mismatch
    raises before storage work; it must never restamp another selection's results
    with the current defaults. The existing publication merges only inspection,
    preserving unrelated dimensions and their coherently served artifacts.
    """
    identity = ProcessingIdentity.model_validate_json(attempt.identity_json)
    if package.identity.processing_identity() != identity:
        raise ValueError("inspection package differs from captured attempt identity")
    if attempt.generation < 1:
        raise ValueError("inspection requires a durable processing attempt")
    if package.interrupted and package.inspection_complete:
        raise ValueError("interrupted inspection cannot claim complete scope")
    if package.interrupted and (package.selected or package.selected_tokens):
        raise ValueError("interrupted inspection cannot retain selected evidence")
    selection_summary = SelectionSummary(
        outcome=(
            "interrupted"
            if package.interrupted
            else "complete"
            if package.inspection_complete
            and not package.limitations
            and package.associations_complete
            else "limited"
        ),
        selected_inputs=len(package.selected),
        selected_tokens=package.selected_tokens,
        established_occurrences=sum(
            run.count for excerpt in package.selected for run in excerpt.associations
        ),
        associations_complete=package.associations_complete,
        boundary=package.boundary,
    )
    limitations = list(dict.fromkeys(package.limitations))
    if package.interrupted and "inspection_interrupted" not in limitations:
        limitations.append("inspection_interrupted")
    if len(limitations) > 16 or any(not item or len(item) > 512 for item in limitations):
        # Detailed selection inventory stays in the package. This is only the
        # deliberately bounded coverage summary, never an inventory manifest.
        limitations = [item for item in limitations if item and len(item) <= 512][:15]
        limitations.append("additional_inspection_limitations_omitted")
    complete = package.inspection_complete
    counts = [
        {
            "unit": unit,
            "observed": value,
            "represented": value,
            "total": value if complete else None,
        }
        for unit, value in (
            ("occurrences", package.observed_values),
            ("characters", package.observed_characters),
        )
    ]
    evidence = CoverageDimension(
        scope="complete" if complete else "limited",
        identity=identity,
        counts=counts,
        limitations=limitations,
        unknown_remainder=not complete,
        selection_summary=selection_summary,
    )
    return await store.publish_processing(
        attempt.document_id, attempt.generation, {"inspection": evidence}
    )
