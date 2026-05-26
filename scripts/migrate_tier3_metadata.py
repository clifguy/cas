#!/usr/bin/env python3
"""Backfill tier3_metadata on every ticket and failure_record version.

Phase 1 (commit cedfce6) wired the tier3_metadata substrate: a typed
per-doc_type metadata field validated against each doc_type's metadata_schema
declared in vault config. Phase 2 migrates existing data into it.

This script:

- Enumerates all versions of ``ticket`` and ``failure_record`` in the cas vault
  (heads and superseded — typically ~14 failure_record rows and ~49 ticket rows
  across supersedes chains).
- For each row:
  - Parses tags to derive the tier3 fields the tag grammar carried:
    - ``ticket``: ``id:T-NNNN`` -> ``ticket_id``, ``type:<v>`` -> ``ticket_type``,
      ``priority:<v>`` -> ``ticket_priority``.
    - ``failure_record``: ``id:F<N>`` -> ``failure_id``, ``class:<v>`` -> ``failure_class``,
      ``severity:<v>`` -> ``severity``, ``observed_by:<v>`` -> ``observed_by``.
  - For ``failure_record`` rows, additionally reads the ``Metadata`` chunk from
    LanceDB and parses the structured key-value body header to recover
    ``caught_by_gate``, ``introduction_commit``, ``discovery_commit``,
    ``fix_commit``, and ``session_link``. Tag-derived values win on overlap so
    the ``tags`` array remains authoritative for the four fields it carries.
  - Strips the migrated tag prefixes from the row's ``tags`` array. The retained
    prefix surfaces (``phase-NN``, content topic tags, ``follow-up-*``, and the
    bare doc_type keyword ``ticket`` / ``failure-log``) are preserved verbatim.
  - Validates the computed payload against the doc_type's ``metadata_schema``
    via jsonschema. Any failure is a stop-the-line error; the script exits
    non-zero without writing anything.

Idempotence: rows whose ``tier3_metadata`` is already non-null are logged and
skipped.

Dry-run is the default. Pass ``--execute`` to write.

Usage::

    .venv/bin/python scripts/migrate_tier3_metadata.py
    .venv/bin/python scripts/migrate_tier3_metadata.py --execute
    .venv/bin/python scripts/migrate_tier3_metadata.py --doc-type ticket
    .venv/bin/python scripts/migrate_tier3_metadata.py --vault other_vault
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import lancedb
import yaml

TICKET_TAG_PREFIXES = ("id:T-", "type:", "priority:")
FAILURE_TAG_PREFIXES = ("id:", "class:", "severity:", "observed_by:")

FAILURE_BODY_FIELDS = (
    "failure_id",
    "failure_class",
    "severity",
    "observed_by",
    "caught_by_gate",
    "introduction_commit",
    "discovery_commit",
    "fix_commit",
    "session_link",
)


@dataclass
class DocumentMigration:
    document_id: str
    doc_type: str
    title: str
    original_tags: list[str]
    new_tags: list[str]
    tier3_payload: dict
    body_fields_found: bool
    skipped_reason: str | None = None


@dataclass
class MigrationPlan:
    migrations: list[DocumentMigration] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (doc_id, reason)
    validation_errors: list[tuple[str, str]] = field(default_factory=list)


def load_vault_paths(vault_id: str) -> tuple[Path, Path, dict]:
    config_path = Path.home() / "sage_vaults" / vault_id / "vault_config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Vault config not found: {config_path}")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    brain_root = Path(config["vault"]["brain_root"]).expanduser().resolve()
    db_path = brain_root / "graph.db"
    lance_path = brain_root / "lancedb"
    if not db_path.exists():
        raise FileNotFoundError(f"Graph DB not found: {db_path}")
    if not lance_path.exists():
        raise FileNotFoundError(f"LanceDB not found: {lance_path}")
    return db_path, lance_path, config


def build_validators(config: dict) -> dict[str, jsonschema.protocols.Validator]:
    """Build per-doc_type jsonschema validators from vault config.

    Returns a mapping of doc_type value -> Draft202012Validator. Doc_types
    without a metadata_schema are not included in the result.
    """
    validators: dict[str, jsonschema.protocols.Validator] = {}
    for entry in config["document_types"]["doc_types"]:
        schema = entry.get("metadata_schema")
        if schema is None:
            continue
        validators[entry["value"]] = jsonschema.Draft202012Validator(schema)
    return validators


def parse_ticket_tags(tags: list[str]) -> tuple[dict, list[str]]:
    """Extract ticket_id, ticket_type, ticket_priority from a tag list.

    Returns ``(payload, stripped_tags)``.
    """
    payload: dict = {}
    stripped: list[str] = []
    for tag in tags:
        if tag.startswith("id:T-"):
            payload["ticket_id"] = tag[len("id:") :]
        elif tag.startswith("type:"):
            payload["ticket_type"] = tag[len("type:") :]
        elif tag.startswith("priority:"):
            payload["ticket_priority"] = tag[len("priority:") :]
        else:
            stripped.append(tag)
    return payload, stripped


def parse_failure_tags(tags: list[str]) -> tuple[dict, list[str]]:
    """Extract failure_id, failure_class, severity, observed_by from a tag list.

    Returns ``(payload, stripped_tags)``. Only the four tag-derived fields are
    populated here; body fields are layered in by parse_failure_body.
    """
    payload: dict = {}
    stripped: list[str] = []
    for tag in tags:
        if tag.startswith("id:") and not tag.startswith("id:T-"):
            # Failure-record id tags: id:F1, id:BASELINE, etc.
            payload["failure_id"] = tag[len("id:") :]
        elif tag.startswith("class:"):
            payload["failure_class"] = tag[len("class:") :]
        elif tag.startswith("severity:"):
            payload["severity"] = tag[len("severity:") :]
        elif tag.startswith("observed_by:"):
            payload["observed_by"] = tag[len("observed_by:") :]
        else:
            stripped.append(tag)
    return payload, stripped


_BODY_LINE_RE = re.compile(r"^- (?P<key>[a-z_]+):\s*(?P<value>.*?)\s*$")


def parse_failure_body(metadata_chunk_text: str) -> dict:
    """Parse the ``## Metadata`` body section of a failure_record.

    The chunk content has the convention::

        - failure_id: F1
        - failure_class: api_drift
        - caught_by_gate: false
        - introduction_commit: null
        ...

    Returns a dict containing only fields named in FAILURE_BODY_FIELDS that
    were present in the chunk. ``null`` -> None; ``true``/``false`` (only for
    caught_by_gate) -> bool; everything else -> str.
    """
    fields: dict = {}
    for line in metadata_chunk_text.splitlines():
        m = _BODY_LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        key = m.group("key")
        raw = m.group("value")
        if key not in FAILURE_BODY_FIELDS:
            continue
        if raw.lower() == "null":
            fields[key] = None
        elif key == "caught_by_gate":
            fields[key] = raw.lower() == "true"
        else:
            fields[key] = raw
    return fields


def fetch_failure_metadata_chunks(lance_path: Path, document_ids: list[str]) -> dict[str, str]:
    """Return {document_id: metadata_chunk_text} for failure_record docs.

    The chunk's heading_path is either ``Metadata`` (top-level) or
    ``<Title> > Metadata`` (nested under the title heading). We match both.
    Docs without a Metadata chunk are absent from the result.
    """
    if not document_ids:
        return {}
    db = lancedb.connect(str(lance_path))
    tbl = db.open_table("chunks")
    # SQL-injection guard: ids contain only [a-zA-Z0-9_], no quoting needed,
    # but build the IN-list with single-quoted strings explicitly.
    quoted_ids = ", ".join(f"'{did}'" for did in document_ids)
    where = (
        f"document_id IN ({quoted_ids}) AND "
        "(heading_path = 'Metadata' OR heading_path LIKE '% > Metadata')"
    )
    rows = tbl.search().where(where).to_list()
    result: dict[str, str] = {}
    for row in rows:
        # When multiple chunks match (some legacy docs have both), prefer the
        # nested heading form; fall back to the top-level.
        existing = result.get(row["document_id"])
        if existing is None or " > Metadata" in row["heading_path"]:
            result[row["document_id"]] = row["content"]
    return result


def strip_chunk_synthetic_header(content: str) -> str:
    """Drop the Title/Source/Tags synthetic header from a chunk's content.

    The chunker prepends a synthetic header to each chunk for hybrid search.
    The actual body content starts after the first blank line.
    """
    parts = content.split("\n\n", 1)
    if len(parts) == 2 and parts[0].startswith("Title:"):
        return parts[1]
    return content


def build_plan(
    conn: sqlite3.Connection,
    lance_path: Path,
    validators: dict[str, jsonschema.protocols.Validator],
    doc_types: list[str],
) -> MigrationPlan:
    plan = MigrationPlan()
    placeholders = ", ".join("?" for _ in doc_types)
    rows = conn.execute(
        f"SELECT id, doc_type, title, tags, tier3_metadata "  # noqa: S608 -- placeholders only
        f"FROM documents WHERE doc_type IN ({placeholders}) "
        "ORDER BY doc_type, id",
        doc_types,
    ).fetchall()

    failure_ids = [row[0] for row in rows if row[1] == "failure_record" and row[4] is None]
    metadata_chunks = fetch_failure_metadata_chunks(lance_path, failure_ids)

    for doc_id, doc_type, title, tags_json, tier3_json in rows:
        if tier3_json is not None:
            plan.skipped.append((doc_id, "tier3_metadata already set"))
            continue

        tags = json.loads(tags_json) if tags_json else []
        if doc_type == "ticket":
            payload, stripped = parse_ticket_tags(tags)
            body_found = False
        elif doc_type == "failure_record":
            payload, stripped = parse_failure_tags(tags)
            chunk = metadata_chunks.get(doc_id)
            if chunk is not None:
                body = strip_chunk_synthetic_header(chunk)
                body_payload = parse_failure_body(body)
                # Tag-derived values win on overlap so tags remain authoritative
                # for the four fields they carry.
                for k, v in body_payload.items():
                    payload.setdefault(k, v)
                body_found = True
            else:
                body_found = False
        else:
            plan.skipped.append((doc_id, f"unhandled doc_type: {doc_type}"))
            continue

        if not payload:
            plan.skipped.append((doc_id, "no migratable tags or body fields"))
            continue

        validator = validators.get(doc_type)
        if validator is None:
            plan.skipped.append((doc_id, f"no metadata_schema for doc_type '{doc_type}'"))
            continue

        try:
            validator.validate(payload)
        except jsonschema.ValidationError as exc:
            plan.validation_errors.append(
                (
                    doc_id,
                    f"path={exc.json_path} message={exc.message} payload={payload!r}",
                )
            )
            continue

        plan.migrations.append(
            DocumentMigration(
                document_id=doc_id,
                doc_type=doc_type,
                title=title,
                original_tags=tags,
                new_tags=stripped,
                tier3_payload=payload,
                body_fields_found=body_found,
            )
        )

    return plan


def apply_migration(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    modified_by: str,
) -> None:
    """Apply the migration. Caller must have verified plan.validation_errors is empty."""
    if plan.validation_errors:
        raise RuntimeError(
            "Refusing to apply migration: validation errors present. "
            f"{len(plan.validation_errors)} document(s) failed schema validation."
        )
    now = datetime.now(timezone.utc).isoformat()
    for m in plan.migrations:
        conn.execute(
            "UPDATE documents "
            "SET tags = ?, tier3_metadata = ?, metadata_confirmed = 1, "
            "    last_modified_by = ?, updated_at = ? "
            "WHERE id = ?",
            (
                json.dumps(m.new_tags),
                json.dumps(m.tier3_payload),
                modified_by,
                now,
                m.document_id,
            ),
        )


def print_plan(plan: MigrationPlan, *, out, verbose: bool) -> None:
    print(f"Documents to migrate:  {len(plan.migrations)}", file=out)
    by_type: dict[str, int] = {}
    for m in plan.migrations:
        by_type[m.doc_type] = by_type.get(m.doc_type, 0) + 1
    for dt, count in sorted(by_type.items()):
        print(f"  {dt:18s}      {count}", file=out)
    print(f"Skipped:               {len(plan.skipped)}", file=out)
    if plan.skipped and verbose:
        for did, reason in plan.skipped:
            print(f"  [skip] {did}: {reason}", file=out)
    print(f"Validation errors:     {len(plan.validation_errors)}", file=out)
    if plan.validation_errors:
        for did, msg in plan.validation_errors:
            print(f"  [error] {did}: {msg}", file=out)
    if verbose:
        for m in plan.migrations:
            print(f"\n  {m.document_id}", file=out)
            print(f"    title:        {m.title}", file=out)
            print(f"    payload:      {m.tier3_payload}", file=out)
            print(f"    stripped tags: {m.original_tags} -> {m.new_tags}", file=out)


def run(
    vault_id: str,
    doc_types: list[str],
    execute: bool,
    verbose: bool,
    modified_by: str,
    *,
    out=None,
    err=None,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    db_path, lance_path, config = load_vault_paths(vault_id)
    validators = build_validators(config)
    if not validators:
        print("ERROR: no metadata_schema declared in vault config.", file=err)
        return 2

    print(f"Vault:        {vault_id}", file=out)
    print(f"DB:           {db_path}", file=out)
    print(f"LanceDB:      {lance_path}", file=out)
    print(f"Doc types:    {', '.join(doc_types)}", file=out)
    print(f"Mode:         {'EXECUTE' if execute else 'DRY-RUN'}", file=out)
    print("", file=out)

    conn = sqlite3.connect(str(db_path))
    try:
        plan = build_plan(conn, lance_path, validators, doc_types)
        print_plan(plan, out=out, verbose=verbose)
        if plan.validation_errors:
            print(
                "\nABORT: schema validation failed. No writes performed.",
                file=err,
            )
            return 2
        if not execute:
            print(
                f"\nDry-run. Use --execute to apply migration to "
                f"{len(plan.migrations)} document(s).",
                file=out,
            )
            return 0
        if not plan.migrations:
            print("\nNothing to migrate.", file=out)
            return 0
        apply_migration(conn, plan, modified_by)
        conn.commit()
        print(f"\nMigrated {len(plan.migrations)} document(s).", file=out)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill tier3_metadata on tickets and failure_records (T-0039).",
    )
    parser.add_argument("--vault", default="cas", help="Vault id (default: cas)")
    parser.add_argument(
        "--doc-type",
        choices=["ticket", "failure_record", "both"],
        default="both",
        help="Limit migration scope to a single doc_type (default: both)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply writes (default: dry-run)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-document payload and tag transitions",
    )
    parser.add_argument(
        "--modified-by",
        default="migration_t_0039",
        help="Value written into documents.last_modified_by (default: migration_t_0039)",
    )
    args = parser.parse_args(argv)
    doc_types = ["ticket", "failure_record"] if args.doc_type == "both" else [args.doc_type]
    return run(
        vault_id=args.vault,
        doc_types=doc_types,
        execute=args.execute,
        verbose=args.verbose,
        modified_by=args.modified_by,
    )


if __name__ == "__main__":
    sys.exit(main())
