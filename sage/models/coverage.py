"""Models derived from sage_core_api.openapi.yaml coverage components (CAS-ADR-059)."""

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BoundedText = Annotated[str, Field(min_length=1, max_length=512)]
CoverageHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


PROCESSING_CONFIGURATION_MAX_BYTES = 4096
PROCESSING_CONFIGURATION_MAX_KEYS = 32


def processing_configuration_fits(configuration: dict) -> bool:
    """Use the coverage model's actual default JSON spacing and ASCII escaping."""
    if len(configuration) > PROCESSING_CONFIGURATION_MAX_KEYS:
        return False
    size = 0
    for chunk in json.JSONEncoder().iterencode(configuration):
        size += len(chunk.encode("utf-8"))
        if size > PROCESSING_CONFIGURATION_MAX_BYTES:
            return False
    return True


def canonical_configuration_hash(configuration: dict) -> str:
    """Identify the full canonical configuration, independently of summary omissions."""
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(configuration):
        digest.update(chunk.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def bounded_processing_configuration(
    configuration: dict,
    *,
    preserve_fields: tuple[str, ...] = (),
    omission_details: dict | None = None,
) -> dict:
    """Retain exact small settings or a bounded, explicitly omitted full identity.

    Optional stage details are retained only when the same wire bound admits
    them. The full configuration's canonical digest is always present after
    omission, including when an individual retained field cannot fit.
    """
    if processing_configuration_fits(configuration):
        return deepcopy(configuration)
    summary = {
        "effective_configuration_sha256": canonical_configuration_hash(configuration),
        "effective_configuration_omitted": True,
    }
    extras = dict(omission_details or {})
    extras.update({key: configuration[key] for key in preserve_fields if key in configuration})
    for key, value in extras.items():
        if key not in summary and processing_configuration_fits({**summary, key: value}):
            summary[key] = deepcopy(value)
    return summary


class CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProcessingIdentity(CoverageModel):
    source_hash: CoverageHash = Field(description="Source hash.")
    adapter: BoundedText = Field(description="Adapter.")
    policy: BoundedText = Field(description="Policy.")
    configuration: dict = Field(
        max_length=PROCESSING_CONFIGURATION_MAX_KEYS, description="Configuration."
    )
    tokenization: BoundedText = Field(description="Tokenization.")

    @classmethod
    def from_effective_configuration(
        cls,
        *,
        source_hash: str,
        adapter: str,
        policy: str,
        configuration: dict,
        tokenization: str,
        preserve_fields: tuple[str, ...] = (),
        omission_details: dict | None = None,
    ) -> "ProcessingIdentity":
        """Construct an identity from the same bounded representation for every stage."""
        return cls(
            source_hash=source_hash,
            adapter=adapter,
            policy=policy,
            configuration=bounded_processing_configuration(
                configuration, preserve_fields=preserve_fields, omission_details=omission_details
            ),
            tokenization=tokenization,
        )

    @model_validator(mode="after")
    def bounded_configuration(self) -> "ProcessingIdentity":
        if not processing_configuration_fits(self.configuration):
            raise ValueError("effective configuration exceeds 4096 bytes")
        return self


class CoverageCount(CoverageModel):
    unit: Literal[
        "bytes", "characters", "records", "passages", "tokens", "regions", "fields", "occurrences"
    ] = Field(description="Unit.")
    observed: int = Field(ge=0, description="Observed.")
    represented: int = Field(ge=0, description="Represented.")
    total: int | None = Field(ge=0, description="Total.")

    @model_validator(mode="after")
    def consistent_counts(self) -> "CoverageCount":
        if self.represented > self.observed or (
            self.total is not None and self.observed > self.total
        ):
            raise ValueError("represented <= observed <= known total is required")
        return self


class CoverageRange(CoverageModel):
    location: BoundedText = Field(description="Location.")
    start: int = Field(ge=0, description="Start.")
    end: int = Field(ge=0, description="End.")

    @model_validator(mode="after")
    def ordered(self) -> "CoverageRange":
        if self.end < self.start:
            raise ValueError("range end precedes start")
        return self


class CoverageManifest(CoverageModel):
    generation: int = Field(ge=1, description="Generation.")
    source_hash: CoverageHash = Field(description="Source hash.")
    content_hash: CoverageHash = Field(description="Content hash.")
    entries: list[CoverageRange] = Field(max_length=32, description="Entries.")
    unknown_remainder: bool = Field(description="Unknown remainder.")

    @model_validator(mode="after")
    def identified(self) -> "CoverageManifest":
        payload = json.dumps(
            {
                "entries": [x.model_dump() for x in self.entries],
                "unknown_remainder": self.unknown_remainder,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if (
            len(payload) > 16384
            or self.content_hash != "sha256:" + hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("manifest content hash or size mismatch")
        return self


class SelectionSummary(CoverageModel):
    outcome: Literal["complete", "limited", "interrupted"] = Field(
        description=(
            "Selection outcome independent of inspection scope; interrupted retains no "
            "selected inputs, tokens or occurrences."
        )
    )
    selected_inputs: int = Field(
        strict=True, ge=0, le=4096, description="Exact number of selected distinct complete inputs."
    )
    selected_tokens: int = Field(
        strict=True,
        ge=0,
        le=32768,
        description=(
            "Exact selected input token count under the parent processing identity "
            "tokenization rule."
        ),
    )
    established_occurrences: int = Field(
        strict=True,
        ge=0,
        le=400000,
        description=(
            "Established occurrences associated with selected inputs; a lower bound when "
            "associations_complete is false."
        ),
    )
    associations_complete: bool = Field(
        description=(
            "Whether all established selected-input occurrence associations were retained; "
            "complete outcome requires true."
        )
    )
    boundary: BoundedText | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Last inspected source location, absent when no location was established.",
    )

    @model_validator(mode="after")
    def consistent_selection(self) -> "SelectionSummary":
        if (self.outcome == "interrupted" or self.selected_inputs == 0) and (
            self.selected_inputs != 0
            or self.selected_tokens != 0
            or self.established_occurrences != 0
        ):
            raise ValueError("interrupted or empty selection cannot assert selected evidence")
        if self.outcome == "complete" and not self.associations_complete:
            raise ValueError("complete selection requires complete associations")
        return self


class CoverageDimension(CoverageModel):
    scope: Literal["unknown", "pending", "not_applicable", "limited", "complete"] = Field(
        default="unknown", description="Scope."
    )
    generation: int | None = Field(
        default=None, ge=1, description="Generation.", exclude_if=lambda value: value is None
    )
    identity: ProcessingIdentity | None = Field(
        default=None, description="Identity.", exclude_if=lambda value: value is None
    )
    counts: list[CoverageCount] = Field(
        default_factory=list,
        max_length=8,
        description="Counts.",
        exclude_if=lambda value: not value,
    )
    limitations: list[BoundedText] = Field(
        default_factory=list,
        max_length=16,
        description="Limitations.",
        exclude_if=lambda value: not value,
    )
    ranges: list[CoverageRange] = Field(
        default_factory=list,
        max_length=64,
        description="Ranges.",
        exclude_if=lambda value: not value,
    )
    unknown_remainder: bool = Field(
        default=True, description="Unknown remainder.", exclude_if=lambda value: value is True
    )
    manifest: CoverageManifest | None = Field(
        default=None, description="Manifest.", exclude_if=lambda value: value is None
    )

    selection_summary: SelectionSummary | None = Field(
        default=None,
        description=(
            "Optional discovery selection evidence only on established inspection scope, "
            "sharing its identity and generation."
        ),
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def established(self) -> "CoverageDimension":
        if len({c.unit for c in self.counts}) != len(self.counts):
            raise ValueError("each count unit appears once")
        if self.scope in ("limited", "complete") and self.identity is None:
            raise ValueError("established coverage requires processing identity")
        if self.scope == "complete" and (
            self.unknown_remainder
            or any(c.total is None or c.represented != c.total for c in self.counts)
        ):
            raise ValueError("complete coverage requires exact totals and no unknown remainder")
        if self.scope in ("unknown", "pending", "not_applicable") and (
            self.counts or self.ranges or self.selection_summary is not None
        ):
            raise ValueError(
                "unestablished coverage cannot assert counts, ranges or selection summary"
            )
        if self.manifest and (
            self.generation != self.manifest.generation
            or self.identity is None
            or self.identity.source_hash != self.manifest.source_hash
            or self.unknown_remainder != self.manifest.unknown_remainder
        ):
            raise ValueError("manifest identity differs from summary")
        return self


DIMENSIONS = (
    "source_retention",
    "inspection",
    "projection",
    "keyword_indexing",
    "passage_embeddings",
    "abstraction_evidence",
)


class AbstractionQuality(CoverageModel):
    """Derived from the Core API AbstractionQuality component."""

    mode: Literal[
        "unknown", "recording_generated", "screened_generated", "extractive_fallback", "unavailable"
    ] = Field(
        default="unknown",
        description=(
            "Publication disposition; screened means only the declared "
            "calibrated checks, never universal verification."
        ),
    )
    policy: str | None = Field(
        default=None,
        max_length=64,
        description="Version of the applied publication policy; null for historical unknown state.",
    )
    generation: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Processing attempt that published this quality disposition; null for unknown history."
        ),
    )
    identity: ProcessingIdentity | None = Field(
        default=None,
        description=(
            "Source and selection identity bound to the quality disposition; null when unknown."
        ),
    )
    checks: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list,
        max_length=3,
        description="Versioned policy check classes applied to the generated candidate.",
    )
    findings: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Named class findings, including recording-only findings that do "
            "not authorize withholding."
        ),
    )
    calibration_refs: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(
        default_factory=dict,
        max_length=3,
        description="Accepted calibration report SHA-256 values for the promoted subset of checks.",
    )
    measurements: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Complete deterministic rendered inspected-scope measurements, "
            "separate from model prose."
        ),
    )
    measurements_limited: bool = Field(
        default=False,
        description="True when deterministic observations were omitted from the final composition.",
    )
    candidate_sha256: CoverageHash | None = Field(
        default=None,
        description="Hash of raw model output; raw text remains private diagnostic data.",
    )

    @model_validator(mode="after")
    def attributable(self) -> "AbstractionQuality":
        if self.mode != "unknown" and (
            self.identity is None or self.generation is None or not self.policy
        ):
            raise ValueError(
                "established abstract quality requires identity, policy and generation"
            )
        if self.mode == "screened_generated" and not self.calibration_refs:
            raise ValueError("screened prose requires calibrated check references")
        if not set(self.calibration_refs) <= set(self.checks):
            raise ValueError("calibration references must name applied checks")
        return self


class ProcessingCoverage(CoverageModel):
    attempt: int = Field(default=0, ge=0, description="Attempt.")
    source_retention: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Source retention."
    )
    inspection: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Inspection."
    )
    projection: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Projection."
    )
    keyword_indexing: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Keyword indexing."
    )
    passage_embeddings: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Passage embeddings."
    )
    abstraction_evidence: CoverageDimension = Field(
        default_factory=CoverageDimension, description="Abstraction evidence."
    )

    abstraction_quality: AbstractionQuality = Field(
        default_factory=AbstractionQuality,
        exclude_if=lambda value: value.mode == "unknown",
        description="Class-bounded abstract publication quality, independent of coverage. "
        "Omitted means unknown historical quality, never screened.",
    )

    served_abstraction_generation: int | None = Field(
        default=None,
        ge=1,
        description="Generation of the served abstract; null means historical identity "
        "unknown. Evidence may describe a newer failed replacement attempt.",
    )

    @model_validator(mode="after")
    def bounded_summary(self) -> "ProcessingCoverage":
        if any(
            getattr(self, dimension).selection_summary is not None
            for dimension in DIMENSIONS
            if dimension != "inspection"
        ):
            raise ValueError("selection summary belongs only to inspection")
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("coverage summary exceeds 65536 bytes")
        return self


# The abstraction-evidence limitation recorded when a document's projection holds
# no readable text, so no abstract can be generated from it.
EMPTY_READABLE_PROJECTION = "empty_readable_projection"


def has_empty_readable_projection(coverage: ProcessingCoverage) -> bool:
    """Whether recorded coverage says the projection holds no readable text."""
    evidence = coverage.abstraction_evidence
    return evidence.scope == "not_applicable" and EMPTY_READABLE_PROJECTION in evidence.limitations
