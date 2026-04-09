"""Tolerant, content-aware filename parser (EI-001 through EI-012).

Parses filenames into structured metadata using the vault's
metadata_extraction configuration. All segments are nullable except title.

Recognition rules:
  - Date: YYYY-MM-DD pattern
  - Version: v-prefix, scanned from right
  - Codes: matched against known_code_patterns (case-insensitive regexes)
  - Title: remaining segments after date/version/code extraction

Doc type resolution order:
  1. keyword_to_doc_type (case-insensitive substring on title)
  2. code_to_doc_type (compound keys first, code-only second)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedMetadata:
    title: str
    date: str | None = None
    project: str | None = None
    codes: list[str] = field(default_factory=list)
    version: str | None = None
    doc_type: str | None = None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VERSION_RE = re.compile(r"^v\d+([._]\d+)*$", re.IGNORECASE)


def normalize_version(version_str: str) -> tuple[int, int, int]:
    """Normalize a version string to a (major, minor, patch) tuple.

    Accepts v7, v10_2, v8_4_1, v1.3 etc.  Missing components default to 0.
    """
    stripped = version_str.lstrip("vV")
    parts = re.split(r"[._]", stripped)
    ints = [int(p) for p in parts[:3]]
    while len(ints) < 3:
        ints.append(0)
    return (ints[0], ints[1], ints[2])


class FilenameParser:
    """Parse filenames using vault metadata_extraction config."""

    def __init__(self, metadata_extraction: dict) -> None:
        fe = metadata_extraction.get("filename_extraction", {})
        self._separator = fe.get("separator", "_")

        # Compile known_code_patterns (case-sensitive: patterns define
        # their own character classes, e.g. [A-Z] means uppercase only)
        raw_patterns = fe.get("known_code_patterns", [])
        self._code_patterns = [re.compile(p) for p in raw_patterns]

        # keyword_to_doc_type rules (evaluated first)
        self._keyword_rules = fe.get("keyword_to_doc_type", [])

        # code_to_doc_type rules (evaluated second)
        self._code_rules = fe.get("code_to_doc_type", [])

        # Known project identifiers (from segment_fields if configured)
        self._project_id: str | None = fe.get("segment_fields", {}).get(
            "project"
        )

    def parse(self, filename_stem: str) -> ParsedMetadata:
        """Parse a filename stem (no extension) into structured metadata."""
        segments = filename_stem.split(self._separator)
        if not segments:
            return ParsedMetadata(title=filename_stem)

        date: str | None = None
        version: str | None = None
        codes: list[str] = []
        title_parts: list[str] = []
        project: str | None = None

        # Pass 1: identify date (first matching segment)
        remaining: list[str] = []
        for seg in segments:
            if date is None and _DATE_RE.match(seg):
                date = seg
            else:
                remaining.append(seg)

        # Pass 2: identify version (rightmost v-prefixed segment)
        version_idx: int | None = None
        for i in range(len(remaining) - 1, -1, -1):
            if _VERSION_RE.match(remaining[i]):
                version = remaining[i]
                version_idx = i
                break

        if version_idx is not None:
            remaining = remaining[:version_idx] + remaining[version_idx + 1 :]

        # Pass 3: identify project (first short uppercase segment).
        # Project comes before codes in the naming convention. We
        # extract it positionally before code detection so that "PIM"
        # is not consumed as a code.
        if remaining and len(remaining[0]) <= 5 and remaining[0].isupper():
            project = remaining[0]
            remaining = remaining[1:]

        # Pass 4: identify codes via known_code_patterns
        still_remaining: list[str] = []
        for seg in remaining:
            if self._is_code(seg):
                codes.append(seg)
            else:
                still_remaining.append(seg)

        # Whatever remains is the title
        title = self._separator.join(still_remaining) if still_remaining else filename_stem

        # Resolve doc_type
        doc_type = self._resolve_doc_type(title, codes)

        return ParsedMetadata(
            title=title,
            date=date,
            project=project,
            codes=codes,
            version=version,
            doc_type=doc_type,
        )

    def _is_code(self, segment: str) -> bool:
        """Check if a segment matches any known_code_pattern."""
        return any(p.match(segment) for p in self._code_patterns)

    def _resolve_doc_type(
        self, title: str, codes: list[str]
    ) -> str | None:
        """Resolve doc_type: keyword_to_doc_type first, then code_to_doc_type."""
        # Phase 1: keyword_to_doc_type (EI-009)
        for rule in self._keyword_rules:
            keyword = rule.get("keyword", "")
            segment_name = rule.get("segment", "title")
            if segment_name == "title" and keyword:
                if keyword.lower() in title.lower():
                    return rule["doc_type"]

        # Phase 2: code_to_doc_type (EI-010)
        # Compound keys first (rules with title_contains or segment_match),
        # then code-only rules. Since rules are ordered and first-match wins,
        # we evaluate in order and compound keys naturally take precedence
        # if the config lists them first.
        for rule in self._code_rules:
            rule_code = rule.get("code")
            if rule_code is None:
                continue

            matching_codes = [c for c in codes if c.upper() == rule_code.upper()]
            if not matching_codes:
                continue

            # Check compound conditions
            title_contains = rule.get("title_contains")
            if title_contains and title_contains.lower() not in title.lower():
                continue

            segment_match = rule.get("segment_match")
            if segment_match:
                # For now, segment_match is not evaluated against parsed
                # segments (would require passing all segments). Skip if
                # segment_match is present but we can't evaluate it.
                continue

            return rule["doc_type"]

        return None
