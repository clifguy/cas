"""Structured-data source adapter: JSON, JSON Lines, YAML and TOML.

The four formats share one data model -- a tree of mappings, sequences and
scalars -- so one adapter reads them all, choosing the parser by extension.

The projection has no headings. A data file's keys are not a document's
sections: a heading per key multiplies passages without giving a search anything
the passage text does not already carry. The whole file is one section, which
the ingestion service divides to fit the embedder at blank lines first, then at
line breaks. The adapter's one structural act is to put a blank line between
records, so that a division falls between records rather than inside one.

A record is a member of a container whose members are all containers: an
object in an array of objects, a table in a map of tables. A container with any
scalar member is a record's own body and stays together.

Parsing the projection yields the data parsing the source does. JSON is
re-emitted, since it carries no comments to lose, which also gives minified
JSON the line structure a division needs. YAML and TOML keep their text,
comments included, and gain only blank lines; the result is re-parsed, and if a
blank line would change the data the source text is kept as written.

Computes SHA-256 of source file bytes for content_hash.
"""

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sage.source_adapters.base import (
    ProjectionResult,
    SourceAdapter,
    SourceReadError,
    extract_adr_id_from_filename,
)

_JSON = ".json"
_JSON_LINES = ".jsonl"
_YAML = (".yaml", ".yml")
_TOML = ".toml"

# A TOML table or array-of-tables header line, optionally followed by a comment.
_TOML_HEADER = re.compile(r"[ \t]*\[\[?[^\[\]\n]+\]\]?[ \t]*(?:#.*)?\r?")
_COMMENT = re.compile(r"[ \t]*#")
_INDENT = "  "


class StructuredDataAdapter(SourceAdapter):
    VERSION = "0.1.0"
    EXTENSIONS = [_JSON, _JSON_LINES, *_YAML, _TOML]

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        suffix = source_path.suffix.lower()
        if suffix not in self.EXTENSIONS:
            raise SourceReadError(
                f"Structured-data source has none of the extensions "
                f"{', '.join(self.EXTENSIONS)}, which select its parser: {source_path}"
            )
        try:
            source = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceReadError(
                f"Structured-data source is not valid UTF-8: {source_path}: {exc}"
            ) from exc

        try:
            data, text = _project(suffix, source)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
            raise SourceReadError(
                f"Structured-data source does not parse as {suffix[1:]}: {source_path}: {exc}"
            ) from exc
        except RecursionError as exc:
            raise SourceReadError(
                f"Structured-data source is nested too deeply to read: {source_path}"
            ) from exc

        source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)
        metadata: dict = {"source_modified_at": source_mtime.isoformat()}
        adapter_tier3 = extract_adr_id_from_filename(source_path.stem)
        if adapter_tier3 is not None:
            metadata["adapter_tier3_metadata"] = adapter_tier3

        return ProjectionResult(
            text=text,
            headings=[],
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=_title(data, source_path.stem),
            metadata=metadata,
        )


def _project(suffix: str, source: str) -> tuple[object, str]:
    """The parsed data of ``source`` and its projection text."""
    if suffix == _JSON:
        data = json.loads(source)
        return data, _emit_json(data, 0) + "\n"
    if suffix == _JSON_LINES:
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
        return records, "\n\n".join(lines) + "\n"
    if suffix in _YAML:
        documents = list(yaml.safe_load_all(source))
        return (documents[0] if len(documents) == 1 else documents), _yaml_text(source)
    data = tomllib.loads(source)
    return data, _toml_text(source, data)


def _separated(members: list) -> bool:
    """Whether a blank line goes between ``members``: every one is a container."""
    return bool(members) and all(isinstance(member, (dict, list)) for member in members)


def _emit_json(value: object, depth: int) -> str:
    """``value`` as indented JSON, with a blank line between the members of a
    container whose members are all containers."""
    if isinstance(value, dict) and value:
        opener, closer = "{", "}"
        members = list(value.values())
        rendered = [
            f"{json.dumps(key, ensure_ascii=False)}: {_emit_json(item, depth + 1)}"
            for key, item in value.items()
        ]
    elif isinstance(value, list) and value:
        opener, closer = "[", "]"
        members = value
        rendered = [_emit_json(item, depth + 1) for item in value]
    else:
        return json.dumps(value, ensure_ascii=False)
    inner = _INDENT * (depth + 1)
    separator = (",\n\n" if _separated(members) else ",\n") + inner
    return f"{opener}\n{inner}{separator.join(rendered)}\n{_INDENT * depth}{closer}"


def _yaml_text(source: str) -> str:
    """``source`` with a blank line above each record of every separated node.

    Positions come from the composed node graph, which records where each node
    starts. A mapping member starts at its key; a sequence member at its item.
    A member on the same line as the one before it -- flow style -- gets none.
    """
    lines = source.split("\n")
    starts: set[int] = set()
    seen: set[int] = set()
    pending = list(yaml.compose_all(source))
    while pending:
        node = pending.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            members = [value for _key, value in node.value]
            anchors = [key.start_mark.line for key, _value in node.value]
        elif isinstance(node, yaml.SequenceNode):
            members = node.value
            anchors = [item.start_mark.line for item in node.value]
        else:
            continue
        pending.extend(members)
        if all(isinstance(member, (yaml.MappingNode, yaml.SequenceNode)) for member in members):
            starts.update(line for previous, line in zip(anchors, anchors[1:]) if line > previous)
    separated = _insert_blank_lines(lines, starts, indent_bound=True)
    return separated if _yaml_events(separated) == _yaml_events(source) else source


def _yaml_events(text: str) -> list[tuple]:
    """The YAML event stream of ``text``, without positions.

    Compared in place of the loaded data, because an alias is one event here
    and a whole copied subtree there: comparing loaded data walks every alias
    in full, which a small file can make exponentially large.
    """
    return [
        (
            type(event).__name__,
            getattr(event, "anchor", None),
            getattr(event, "tag", None),
            getattr(event, "value", None),
            getattr(event, "implicit", None),
        )
        for event in yaml.parse(text)
    ]


def _toml_text(source: str, data: dict) -> str:
    """``source`` with a blank line above each table header."""
    lines = source.split("\n")
    starts = {index for index, line in enumerate(lines) if _TOML_HEADER.fullmatch(line)}
    separated = _insert_blank_lines(lines, starts, indent_bound=False)
    return separated if tomllib.loads(separated) == data else source


def _insert_blank_lines(lines: list[str], starts: set[int], *, indent_bound: bool) -> str:
    """``lines`` joined, with a blank line above each line in ``starts``.

    The blank line goes above any comment lines immediately preceding the start,
    so a comment stays with what it describes. Where indentation is structure
    (``indent_bound``), only comments indented no deeper than the start count:
    a deeper ``#`` line belongs to the content above. No blank line is added at
    the top of the text or where one is already present.
    """
    targets: set[int] = set()
    for start in starts:
        depth = len(lines[start]) - len(lines[start].lstrip(" \t"))
        index = start
        while (
            index > 0
            and _COMMENT.match(lines[index - 1])
            and not (
                indent_bound and len(lines[index - 1]) - len(lines[index - 1].lstrip(" \t")) > depth
            )
        ):
            index -= 1
        if index > 0 and lines[index - 1].strip():
            targets.add(index)
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in targets:
            out.append("\r" if lines[index - 1].endswith("\r") else "")
        out.append(line)
    return "\n".join(out)


def _title(data: object, stem: str) -> str:
    """A top-level ``title``, ``name`` or ``info.title`` string, else ``stem``."""
    if isinstance(data, dict):
        info = data.get("info")
        for candidate in (
            data.get("title"),
            data.get("name"),
            info.get("title") if isinstance(info, dict) else None,
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return stem
