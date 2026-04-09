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


# Pre-split patterns: operate on the full stem before separator splitting.
# Leading date: YYYY-MM-DD followed by a space or underscore.
_LEADING_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[_ ](.*)")
# Trailing version: v-prefix with optional sub-components separated by _ or .
# Anchored at $ so it captures the entire trailing version span (e.g. v2_3_1).
_TRAILING_VERSION_RE = re.compile(
    r"(?:^|[_ ])(v\d+(?:[._]\d+)*)$", re.IGNORECASE
)


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

        # Explicit project identifier (e.g. "PIM") for disambiguating
        # the first segment from codes during parsing.
        self._project_id: str | None = fe.get("project_identifier")

    def parse(self, filename_stem: str) -> ParsedMetadata:
        """Parse a filename stem (no extension) into structured metadata.

        Extraction proceeds in two phases:

        Phase 1 (pre-split): regex extraction on the full stem for fields
        whose delimiters may differ from the configured separator. This
        handles dates followed by spaces and multi-part versions whose
        sub-components use the same character as the separator (e.g. v2_3
        when the separator is '_').

        Phase 2 (post-split): segment classification for project, codes,
        and title, which are reliably delimited by the configured separator.
        """
        stem = filename_stem
        date: str | None = None
        version: str | None = None

        # -- Phase 1: pre-split extraction on full stem --

        # Leading date: YYYY-MM-DD followed by space or separator
        date_m = _LEADING_DATE_RE.match(stem)
        if date_m:
            date = date_m.group(1)
            stem = date_m.group(2)

        # Trailing version: v-prefix, may span multiple separator-
        # delimited segments (e.g. v2_3, v10.4.1, V3_2)
        ver_m = _TRAILING_VERSION_RE.search(stem)
        if ver_m:
            version = ver_m.group(1)
            stem = stem[: ver_m.start(1)].rstrip("_ ")

        # -- Phase 2: post-split segment classification --

        segments = stem.split(self._separator) if stem else []
        if not segments:
            return ParsedMetadata(
                title=filename_stem, date=date, version=version
            )

        remaining = list(segments)
        project: str | None = None
        codes: list[str] = []

        # Project vs code: the first short uppercase segment could be
        # either a project identifier (PIM) or a code (PV07).  When
        # project_identifier is configured, use it for an exact match.
        # Otherwise fall back to the original heuristic (first short
        # uppercase segment that is not a code).
        if remaining and len(remaining[0]) <= 5 and remaining[0].isupper():
            if (self._project_id
                    and remaining[0].upper() == self._project_id.upper()):
                project = remaining[0]
            elif self._is_code(remaining[0]):
                codes.append(remaining[0])
            else:
                project = remaining[0]
            remaining = remaining[1:]

        # Codes: via known_code_patterns (remaining segments)
        still_remaining: list[str] = []
        for seg in remaining:
            if self._is_code(seg):
                codes.append(seg)
            else:
                still_remaining.append(seg)

        # Whatever remains is the title
        title = (
            self._separator.join(still_remaining)
            if still_remaining
            else filename_stem
        )

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

            # Prefix match: rule code "PV" matches extracted codes PV01,
            # PV07, etc.  Exact-match rules (REF, PVMaster) naturally
            # work because startswith covers equality.  Order in the
            # config controls precedence (PVMaster before PV).
            matching_codes = [
                c for c in codes
                if c.upper().startswith(rule_code.upper())
            ]
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
