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
# Trailing version: v-prefix with optional sub-components separated by _ or.
# An optional trailing annotation group (e.g. _FIXED, _FINAL) is captured
# separately so it can be re-attached to the residual stem as title content.
_TRAILING_VERSION_RE = re.compile(
    r"(?:^|[_ ])((?i:v)\d+(?:[._]\d+)*)"
    r"(_[A-Z]+(?:_[A-Z]+)*)?"
    r"$"
)
# Finder-style duplication noise at end of stem: " copy", " copy 2", " (1)".
# Stripped from the stem before version extraction; not preserved.
_TRAILING_FINDER_NOISE_RE = re.compile(r"(?:[_ ]+(?:copy(?:\s+\d+)?|\(\d+\)))+\s*$")
# Post-split date: a segment that is exactly YYYY-MM-DD. Catches dates
# that appear after a project prefix (e.g. EXAMPLE_2026-01-06_Title).
_SEGMENT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_version(version_str: str) -> tuple[int, int, int]:
    """Normalize a version string to a (major, minor, patch) tuple.

    Accepts v7, v10_2, v8_4_1, v1.3, v6a etc. Missing components default
    to 0. Trailing alpha suffixes on parts are stripped (e.g. '6a' -> 6).
    """
    stripped = version_str.lstrip("vV")
    parts = re.split(r"[._]", stripped)
    ints: list[int] = []
    for p in parts[:3]:
        m = re.match(r"(\d+)", p)
        ints.append(int(m.group(1)) if m else 0)
    while len(ints) < 3:
        ints.append(0)
    return (ints[0], ints[1], ints[2])


def format_version(version_str: str) -> str:
    """Canonical version string: 'v{major}.{minor}' or 'v{major}.{minor}.{patch}'.

    Preserves the originally-supplied number of numeric components so a
    trailing-zero patch is not dropped: v8.2.0 stays v8.2.0, not v8.2.

      v1_0 -> v1.0, V3_2 -> v3.2, v8_4_1 -> v8.4.1, v7 -> v7.0, v6a -> v6.0,
      v8.2.0 -> v8.2.0, v9.1.0 -> v9.1.0, v3.0.0 -> v3.0.0.
    """
    major, minor, patch = normalize_version(version_str)
    parts = re.split(r"[._]", version_str.lstrip("vV"))
    supplied = 0
    for p in parts[:3]:
        if re.match(r"\d", p):
            supplied += 1
        else:
            break
    if supplied >= 3:
        return f"v{major}.{minor}.{patch}"
    return f"v{major}.{minor}"


class FilenameParser:
    """Parse filenames using vault metadata_extraction config."""

    def __init__(
        self,
        metadata_extraction: dict,
        doc_types: list[dict] | None = None,
    ) -> None:
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

        # Explicit project identifier (e.g. "EXAMPLE") for disambiguating
        # the first segment from codes during parsing.
        self._project_id: str | None = fe.get("project_identifier")

        # Source type constraints from doc_type definitions. Maps
        # doc_type value -> allowed adapter list (or None if unconstrained).
        self._source_type_constraints: dict[str, list[str] | None] = {}
        if doc_types:
            for dt in doc_types:
                self._source_type_constraints[dt["value"]] = dt.get("source_types")

    def parse(self, filename_stem: str, adapter: str | None = None) -> ParsedMetadata:
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

        # Strip Finder-style duplication noise before version extraction
        noise_m = _TRAILING_FINDER_NOISE_RE.search(stem)
        if noise_m:
            stem = stem[: noise_m.start()]

        # Trailing version: v-prefix, may span multiple separator-
        # delimited segments (e.g. v2_3, v10.4.1, V3_2). An optional
        # uppercase-alpha annotation (e.g. _FIXED) is held aside and
        # appended to the resolved title after Phase 2, so it does not
        # get misclassified as a code or project segment.
        trailing_annotation = ""
        ver_m = _TRAILING_VERSION_RE.search(stem)
        if ver_m:
            version = format_version(ver_m.group(1))
            trailing_annotation = ver_m.group(2) or ""
            stem = stem[: ver_m.start(1)].rstrip("_ ")

        # -- Phase 2: post-split segment classification --

        segments = stem.split(self._separator) if stem else []
        if not segments:
            return ParsedMetadata(title=filename_stem, date=date, version=version)

        remaining = list(segments)
        project: str | None = None
        codes: list[str] = []

        # Project vs code: the first uppercase segment could be either
        # a project identifier or a code (e.g., PV07). When
        # project_identifier is configured, match it exactly regardless
        # of length. Otherwise fall back to the heuristic that a short
        # uppercase segment is the project (or a code if it matches a
        # configured code pattern).
        if remaining and remaining[0].isupper():
            first = remaining[0]
            if self._project_id and first.upper() == self._project_id.upper():
                project = first
                remaining = remaining[1:]
            elif len(first) <= 5:
                if self._is_code(first):
                    codes.append(first)
                else:
                    project = first
                remaining = remaining[1:]

        # Codes and dates: via known_code_patterns and YYYY-MM-DD pattern
        still_remaining: list[str] = []
        for seg in remaining:
            if self._is_code(seg):
                codes.append(seg)
            elif date is None and _SEGMENT_DATE_RE.match(seg):
                date = seg
            else:
                still_remaining.append(seg)

        # Whatever remains is the title. When no title segments remain
        # but codes were extracted, use the first code as the title (e.g.
        # "2026-01-02_EXAMPLE_TD04_v1_1" -> title "TD04"). Fall back to the
        # raw filename stem only when nothing was extracted at all.
        if still_remaining:
            title = self._separator.join(still_remaining)
        elif codes:
            title = codes[0]
        else:
            title = filename_stem
        if trailing_annotation:
            title = title + trailing_annotation

        # Resolve doc_type
        doc_type = self._resolve_doc_type(title, codes, adapter)

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

    def _is_allowed_doc_type(self, doc_type: str, adapter: str | None) -> bool:
        """Check if doc_type is allowed for the given adapter.

        Returns True if no constraints are configured for this doc_type.
        When constraints exist, the adapter must be in the allowed list.
        A None adapter (no adapter matched) fails any constraint check,
        since files without a recognized adapter cannot satisfy a
        source_type requirement.
        """
        allowed = self._source_type_constraints.get(doc_type)
        if allowed is None:
            return True
        if adapter is None:
            return False
        return adapter in allowed

    @staticmethod
    def _keyword_matches(keyword: str, title: str) -> bool:
        """Word-boundary keyword match against a title.

        Matches the keyword as a whole word, using underscores, hyphens,
        and string boundaries as delimiters. This prevents substring
        matches inside compound words (e.g. "Plan" must not match
        "PlanPortability").
        """
        pattern = r"(?:^|[_\-])" + re.escape(keyword) + r"(?:$|[_\-])"
        return re.search(pattern, title, re.IGNORECASE) is not None

    def _resolve_doc_type(
        self, title: str, codes: list[str], adapter: str | None = None
    ) -> str | None:
        """Resolve doc_type: keyword_to_doc_type first, then code_to_doc_type."""
        # Phase 1: keyword_to_doc_type (EI-009)
        for rule in self._keyword_rules:
            keyword = rule.get("keyword", "")
            segment_name = rule.get("segment", "title")
            if segment_name == "title" and keyword:
                if self._keyword_matches(keyword, title):
                    candidate = rule["doc_type"]
                    if self._is_allowed_doc_type(candidate, adapter):
                        return candidate

        # Phase 2: code_to_doc_type (EI-010)
        # Compound keys first (rules with title_contains), then code-only
        # rules. Since rules are ordered and first-match wins, we evaluate
        # in order and compound keys naturally take precedence if the config
        # lists them first.
        for rule in self._code_rules:
            rule_code = rule.get("code")
            if rule_code is None:
                continue

            # Prefix match: rule code "PV" matches extracted codes PV01,
            # PV07, etc. Exact-match rules (REF, PVMaster) naturally
            # work because startswith covers equality. Order in the
            # config controls precedence (PVMaster before PV).
            matching_codes = [c for c in codes if c.upper().startswith(rule_code.upper())]
            if not matching_codes:
                continue

            # Check compound conditions
            title_contains = rule.get("title_contains")
            if title_contains and title_contains.lower() not in title.lower():
                continue

            candidate = rule["doc_type"]
            if self._is_allowed_doc_type(candidate, adapter):
                return candidate

        return None
