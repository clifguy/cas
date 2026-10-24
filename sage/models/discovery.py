"""Internal immutable authored and derived discovery evidence contracts."""

import json
from dataclasses import asdict, dataclass

from sage.models.coverage import ProcessingIdentity, canonical_configuration_hash


@dataclass(frozen=True)
class SelectionIdentity:
    source_hash: str
    adapter_version: str
    policy: str
    configuration_json: str
    tokenizer_identity: str

    def processing_identity(self) -> ProcessingIdentity:
        configuration = json.loads(self.configuration_json)
        return ProcessingIdentity.from_effective_configuration(
            source_hash=self.source_hash,
            adapter=self.adapter_version,
            policy=self.policy,
            configuration=configuration,
            tokenization=self.tokenizer_identity,
            preserve_fields=("selection", "facts", "inspected_source_hash"),
            omission_details={
                "adapter_configuration_sha256": canonical_configuration_hash(
                    configuration.get("adapter", {})
                ),
                "adapter_configuration_omitted": True,
            },
        )


@dataclass(frozen=True)
class OccurrenceRun:
    """Inclusive adjacent source-order positions with exact endpoint locations."""

    start: int
    end: int
    first_location: str
    last_location: str
    count: int


@dataclass(frozen=True)
class AuthoredExcerpt:
    input_text: str
    rendering: str
    context: str
    region: str
    field_path: str
    location: str
    order: int
    token_count: int
    classification: str
    associations: tuple[OccurrenceRun, ...]


@dataclass(frozen=True)
class DerivedObservation:
    """Counts established only in the inspected prefix, never authored statements."""

    region: str
    field_path: str
    classification: str
    populated: int
    retained_distinct: int
    known_repetitions: int

    def __post_init__(self) -> None:
        counts = (self.populated, self.retained_distinct, self.known_repetitions)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("observation counts must be nonnegative integers")
        if self.retained_distinct + self.known_repetitions > self.populated:
            raise ValueError("retained observation counts exceed inspected values")
        if any(
            not isinstance(value, str) or len(value) > 512
            for value in (self.region, self.field_path, self.classification)
        ):
            raise ValueError("observation labels exceed their established bounds")


@dataclass(frozen=True)
class EvidencePackage:
    identity: SelectionIdentity
    observed_values: int
    observed_characters: int
    selected: tuple[AuthoredExcerpt, ...]
    observations: tuple[DerivedObservation, ...]
    limitations: tuple[str, ...]
    inspection_complete: bool
    interrupted: bool
    associations_complete: bool
    selected_tokens: int
    boundary: str | None


@dataclass(frozen=True)
class SelectionConfig:
    """Limits in scalar facts, Unicode characters, UTF-8 bytes, runs and tokens."""

    max_values: int = 100_000
    max_regions: int = 256
    max_fields: int = 1024
    max_characters: int = 16 * 1024 * 1024
    max_candidates: int = 8192
    max_tokens: int = 8192
    max_excerpts: int = 1024
    max_candidate_characters: int = 32768
    max_retained_bytes: int = 16 * 1024 * 1024
    max_association_runs: int = 16384
    max_keyword_characters: int | None = None
    max_in_flight: int = 8

    @classmethod
    def hard_limits(cls) -> dict[str, int]:
        """Single policy authority for selection hard maxima."""
        return {
            name: 67108864
            if name == "max_keyword_characters"
            else 32768
            if name == "max_candidate_characters"
            else item.default * 4
            for name, item in cls.__dataclass_fields__.items()
        }

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name == "max_keyword_characters" and value is None:
                continue
            maximum = self.hard_limits()[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer from 1 through {maximum}")


def check_configuration_tree(configuration: object) -> None:
    """Bound JSON-shaped configuration before copying, merging or encoding it."""
    import math

    nodes = text_characters = 0

    def validate(item: object, depth: int = 0) -> None:
        nonlocal nodes, text_characters
        nodes += 1
        if nodes > 4096 or depth > 32:
            raise ValueError("effective configuration exceeds node/depth limits")
        if isinstance(item, str):
            text_characters += len(item)
            if text_characters > 65536:
                raise ValueError("effective configuration exceeds text limit")
        elif isinstance(item, dict):
            if len(item) > 4096:
                raise ValueError("effective configuration exceeds node limit")
            for key, value in item.items():
                if not isinstance(key, str):
                    raise ValueError("effective configuration keys must be strings")
                validate(key, depth + 1)
                validate(value, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 4096:
                raise ValueError("effective configuration exceeds node limit")
            for value in item:
                validate(value, depth + 1)
        elif isinstance(item, bool) or item is None:
            pass
        elif isinstance(item, int):
            if item.bit_length() > 65536:
                raise ValueError("effective configuration integer exceeds text limit")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("effective configuration numbers must be finite")
        else:
            raise ValueError("effective configuration must contain JSON values")

    validate(configuration)
