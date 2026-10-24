"""Class-bounded hybrid publication (ADR-020 v11); no classes initially promoted."""

import hashlib
import json
import re
from dataclasses import dataclass

from sage.abstraction_calibrations import ACCEPTED_CALIBRATIONS as ACCEPTED_CALIBRATIONS
from sage.abstraction_policy import RULES
from sage.adapters.abstraction_prompt import neutralize_source_text
from sage.models.coverage import AbstractionQuality
from sage.models.discovery import EvidencePackage

POLICY_VERSION = "hybrid-1"
# Only a reviewed change binding an accepted frozen calibration report may add
# entries. Configuration cannot promote a detector or assert an arbitrary receipt.
_IDENTIFIER = re.compile(r"\b(?:item\d{4,12}|[A-Z]{1,10}-\d{2,12})\b", re.IGNORECASE)
_STRUCTURE = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:(?:sampled|inspected)\s+)?(?:records?|rows?|columns?|fields?|departments?|sentences?)\b"
    r"|\b(?:column|field)\s*:?\s*\d+\b",
    re.IGNORECASE,
)
_RANGE = re.compile(r"\bitem\d{4,12}\s+(?:through|to)\s+([A-Za-z][A-Za-z0-9_-]*)\b", re.IGNORECASE)


def detect_publication_findings(candidate: str, authored: str) -> tuple[str, ...]:
    """Narrow recording-only signatures; neither arbitrary entailment nor world truth."""
    found = set()
    authored_ids = set(_IDENTIFIER.findall(authored))
    if any(value not in authored_ids for value in _IDENTIFIER.findall(candidate)):
        found.add("unsupported_identifier")
    if _STRUCTURE.search(candidate):
        found.add("bounded_structure")
    for match in _RANGE.finditer(candidate):
        if match.group(0) not in authored and not _IDENTIFIER.fullmatch(match.group(1)):
            found.add("range_role")
    return tuple(rule for rule in RULES if rule in found)


class GeneratedDescription(str):
    """Repaired text plus bounded raw-generation provenance, kept off public models."""

    def __new__(cls, text: str, raw: str) -> "GeneratedDescription":
        value = super().__new__(cls, text)
        value.raw = raw
        return value


@dataclass(frozen=True)
class PublicationBudget:
    measurements: tuple[str, ...]
    measurements_limited: bool
    prose_tokens: int
    final_bytes: int


def prepare_publication(package: EvidencePackage, request, maximum: int) -> PublicationBudget:
    """UTF-8 bytes conservatively upper-bound final token use, including labels."""
    maximum = min(maximum, 1500)
    retained = []
    used = 0
    # Preserve room for authored prose or a complete fallback unit. Complete
    # observations alone are selected; oversized labels are never clipped.
    fact_limit = max(0, maximum - min(256, maximum // 2))
    for obs in request.observations[:8]:

        def quote(value: str) -> str:
            return json.dumps(neutralize_source_text(value), ensure_ascii=False)

        inspection = (
            "complete" if package.inspection_complete and not package.limitations else "incomplete"
        )
        text = (
            f"SAGE counted {obs.populated} {quote(obs.classification)} source values "
            f"in region {quote(obs.region)}, field {quote(obs.field_path)}, exactly over "
            f"the inspected scope through {quote(package.boundary or 'unknown boundary')}; "
            f"inspection {inspection}."
        )
        extra = len(("\n\n" if retained else "").encode()) + len(text.encode())
        if used + extra <= fact_limit and len(text) <= 2048:
            retained.append(text)
            used += extra
    return PublicationBudget(
        tuple(retained),
        len(retained) < len(package.observations),
        max(1, min(request.target_tokens, maximum - used - (2 if retained else 0))),
        maximum,
    )


def extractive_fallback(package: EvidencePackage, limit: int) -> str | None:
    """Quote complete contextual units in source order; no sentence truncation."""
    prefix = "Selected source excerpts (quoted; limited):"
    result = prefix
    count = 0
    # No scan beyond the already bounded selected package. Byte bounds also
    # bound tokens, and remain stricter than the separate 2,048-byte ceiling.
    limit = min(limit, 256, 2048)
    for excerpt in sorted(package.selected, key=lambda x: (x.order, x.location)):
        label = json.dumps(
            {"region": excerpt.region, "field": excerpt.field_path, "location": excerpt.location},
            ensure_ascii=False,
        )
        block = (
            "\n"
            + neutralize_source_text(label)
            + "\n"
            + json.dumps(
                neutralize_source_text(excerpt.input_text),
                ensure_ascii=False,
            )
        )
        if len((result + block).encode()) <= limit:
            result += block
            count += 1
            if count == 3:
                break
    return result if count else None


@dataclass(frozen=True)
class Publication:
    text: str | None
    quality: AbstractionQuality
    candidate: dict


def compose_publication(
    package: EvidencePackage,
    request,
    candidate: str,
    budget: PublicationBudget,
    generation: int,
    calibration_refs: dict[str, str],
) -> Publication:
    """Choose generated/fallback/unavailable; promotion authority is caller-validated."""
    if set(calibration_refs) - set(RULES):
        raise ValueError("unknown publication rule")
    findings = detect_publication_findings(candidate, request.text.authored_text)
    facts = "\n\n".join(budget.measurements)
    remaining = budget.final_bytes - len(facts.encode()) - (2 if facts else 0)
    withheld = bool(set(findings) & calibration_refs.keys())
    oversized = len(candidate.encode()) > remaining
    body = str(candidate) or None
    mode = "screened_generated" if calibration_refs else "recording_generated"
    if withheld or oversized or body is None:
        body = extractive_fallback(package, remaining)
        mode = "extractive_fallback" if body else "unavailable"
    measurements = list(budget.measurements) if body else []
    text = body + ("\n\n" + facts if facts else "") if body else None
    raw = getattr(candidate, "raw", str(candidate))
    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    quality = AbstractionQuality(
        mode=mode,
        policy=POLICY_VERSION,
        generation=generation,
        identity=request.identity,
        checks=list(RULES),
        findings=list(findings),
        calibration_refs=calibration_refs,
        measurements=measurements,
        measurements_limited=budget.measurements_limited
        or (bool(budget.measurements) and not body),
        candidate_sha256=digest,
    )
    if text is not None and len(text.encode()) > budget.final_bytes:
        raise ValueError("hybrid final description exceeds reserved bound")
    return Publication(
        text,
        quality,
        {
            "raw": raw[:65536],
            "truncated": len(raw) > 65536,
            "sha256": digest,
            "generation": generation,
        },
    )
