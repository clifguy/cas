"""Tests for the T-0039 tier3_metadata backfill migration script.

Unit tests cover the pure tag/body parsers and the strip-tags logic.
Integration tests exercise the end-to-end migration against a fabricated
SQLite + LanceDB fixture so the dry-run/execute split and the
idempotence/validation-error stop-the-line behavior are verified without
touching the real cas vault.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from scripts.migrate_tier3_metadata import (
    build_plan,
    build_validators,
    parse_failure_body,
    parse_failure_tags,
    parse_ticket_tags,
    run,
    strip_chunk_synthetic_header,
)

# ---------------------------------------------------------------------------
# Pure-parser tests
# ---------------------------------------------------------------------------


def test_parse_ticket_tags_extracts_three_fields() -> None:
    tags = ["ticket", "id:T-0042", "type:methodology", "priority:high", "phase-2"]
    payload, stripped = parse_ticket_tags(tags)
    assert payload == {
        "ticket_id": "T-0042",
        "ticket_type": "methodology",
        "ticket_priority": "high",
    }
    assert stripped == ["ticket", "phase-2"]


def test_parse_ticket_tags_skips_absent_prefixes() -> None:
    """Schema has no required fields; partial payloads are valid."""
    tags = ["ticket", "id:T-0001", "phase-2"]
    payload, stripped = parse_ticket_tags(tags)
    assert payload == {"ticket_id": "T-0001"}
    assert stripped == ["ticket", "phase-2"]


def test_parse_ticket_tags_retains_followup_and_topic_tags() -> None:
    tags = [
        "ticket",
        "id:T-0010",
        "type:feature",
        "priority:medium",
        "phase-2",
        "sage",
        "follow-up-T-0003",
    ]
    _, stripped = parse_ticket_tags(tags)
    assert stripped == ["ticket", "phase-2", "sage", "follow-up-T-0003"]


def test_parse_failure_tags_extracts_four_fields() -> None:
    tags = [
        "failure-log",
        "id:F1",
        "class:api_drift",
        "severity:high",
        "observed_by:audit",
        "phase-2",
    ]
    payload, stripped = parse_failure_tags(tags)
    assert payload == {
        "failure_id": "F1",
        "failure_class": "api_drift",
        "severity": "high",
        "observed_by": "audit",
    }
    assert stripped == ["failure-log", "phase-2"]


def test_parse_failure_tags_handles_baseline_id() -> None:
    tags = ["failure-log", "id:BASELINE", "class:other", "severity:medium"]
    payload, _ = parse_failure_tags(tags)
    assert payload["failure_id"] == "BASELINE"


def test_parse_failure_tags_does_not_match_ticket_id_prefix() -> None:
    """``id:T-...`` must not be treated as a failure id."""
    tags = ["failure-log", "id:T-0001", "severity:low"]
    payload, stripped = parse_failure_tags(tags)
    assert "failure_id" not in payload
    # id:T-0001 falls through to stripped because it is not a recognized
    # failure-id pattern.
    assert "id:T-0001" in stripped


def test_parse_failure_body_extracts_all_fields() -> None:
    body = """- failure_id: F1
- failure_class: api_drift
- severity: medium
- observed_by: audit
- caught_by_gate: false
- introduction_commit: edeb1ca
- discovery_commit: 268786c
- fix_commit: null
- session_link: null
"""
    payload = parse_failure_body(body)
    assert payload["failure_id"] == "F1"
    assert payload["failure_class"] == "api_drift"
    assert payload["severity"] == "medium"
    assert payload["observed_by"] == "audit"
    assert payload["caught_by_gate"] is False
    assert payload["introduction_commit"] == "edeb1ca"
    assert payload["discovery_commit"] == "268786c"
    assert payload["fix_commit"] is None
    assert payload["session_link"] is None


def test_parse_failure_body_handles_multiword_string_value() -> None:
    """F1's introduction_commit is "(multiple, Apr 13–May 1)" — keep as string."""
    body = "- introduction_commit: (multiple, Apr 13)\n- fix_commit: null\n"
    payload = parse_failure_body(body)
    assert payload["introduction_commit"] == "(multiple, Apr 13)"
    assert payload["fix_commit"] is None


def test_parse_failure_body_ignores_unknown_keys() -> None:
    body = "- failure_id: F2\n- rumor_status: gossip\n"
    payload = parse_failure_body(body)
    assert payload == {"failure_id": "F2"}


def test_strip_chunk_synthetic_header_drops_title_block() -> None:
    chunk = (
        "Title: F1: API convention drift\n"
        "Source: F-1_api-convention-drift\n"
        "Tags: failure-log, id:F1\n"
        "\n"
        "- failure_id: F1\n- severity: medium\n"
    )
    body = strip_chunk_synthetic_header(chunk)
    assert body.startswith("- failure_id: F1")


def test_strip_chunk_synthetic_header_passes_through_when_no_header() -> None:
    chunk = "- failure_id: F1\n- severity: medium\n"
    assert strip_chunk_synthetic_header(chunk) == chunk


# ---------------------------------------------------------------------------
# Integration tests against a fabricated vault
# ---------------------------------------------------------------------------


VAULT_CONFIG_SOURCE: dict = {
    "vault": {
        "id": "test_migrate",
        "name": "Test migrate vault",
        "owner": "tester",
        "visibility": "personal",
        "members": None,
        "timezone": "UTC",
    },
    "document_types": {
        "doc_types": [
            {
                "value": "ticket",
                "label": "Ticket",
                "description": "test",
                "source_types": ["markdown"],
                "metadata_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "ticket_id": {"type": "string", "pattern": r"^T-\d{4}$"},
                        "ticket_type": {
                            "type": "string",
                            "enum": ["feature", "fix", "methodology", "spike", "doc"],
                        },
                        "ticket_priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                    },
                },
            },
            {
                "value": "failure_record",
                "label": "Failure",
                "description": "test",
                "source_types": ["markdown"],
                "metadata_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "failure_id": {"type": "string"},
                        "failure_class": {
                            "type": "string",
                            "enum": [
                                "api_drift",
                                "tests_as_chronicle",
                                "boundary_validation_gap",
                                "remediation_scope_gap",
                                "validation_bypass",
                                "other",
                            ],
                        },
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "observed_by": {
                            "type": "string",
                            "enum": [
                                "audit",
                                "test_suite",
                                "user_report",
                                "static_gate",
                                "review_gate",
                            ],
                        },
                        "caught_by_gate": {"type": "boolean"},
                        "introduction_commit": {"type": ["string", "null"]},
                        "discovery_commit": {"type": ["string", "null"]},
                        "fix_commit": {"type": ["string", "null"]},
                        "session_link": {"type": ["string", "null"]},
                    },
                },
            },
        ],
    },
}


def _seed_documents(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            version_label TEXT,
            project TEXT,
            tags TEXT,
            authority_scope TEXT,
            doc_type TEXT,
            source_content_hash TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_modified_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            projected_at TEXT,
            indexed_at TEXT,
            source_modified_at TEXT,
            document_date TEXT,
            semantic_abstract TEXT,
            pipeline_status TEXT NOT NULL DEFAULT 'projection_complete',
            pipeline_error TEXT,
            tier3_metadata TEXT,
            metadata_confirmed INTEGER NOT NULL DEFAULT 0
        )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO documents (id, title, source_type, source_path, "
            "lifecycle_status, project, tags, doc_type, source_content_hash, "
            "adapter_version, created_by, created_at, last_modified_by, "
            "updated_at, tier3_metadata, metadata_confirmed) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"],
                r["title"],
                "markdown",
                f"imports/{r['id']}.md",
                r.get("lifecycle_status", "active"),
                "TEST",
                json.dumps(r["tags"]),
                r["doc_type"],
                "sha256:test",
                "0.1.0",
                "test",
                "2026-01-01T00:00:00Z",
                "test",
                "2026-01-01T00:00:00Z",
                r.get("tier3_metadata"),
                0,
            ),
        )
    conn.commit()
    conn.close()


def _seed_chunks(lance_path: Path, chunks: list[dict]) -> None:
    lance_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lance_path))
    schema = pa.schema(
        [
            ("document_id", pa.string()),
            ("heading_path", pa.string()),
            ("content", pa.string()),
            ("chunk_index", pa.int32()),
            ("vector", pa.list_(pa.float32(), 8)),
            ("doc_type", pa.string()),
        ]
    )
    if chunks:
        data = [
            {
                "document_id": c["document_id"],
                "heading_path": c["heading_path"],
                "content": c["content"],
                "chunk_index": c.get("chunk_index", 0),
                "vector": [0.0] * 8,
                "doc_type": c["doc_type"],
            }
            for c in chunks
        ]
        db.create_table("chunks", data=data, schema=schema)
    else:
        db.create_table("chunks", schema=schema)


@pytest.fixture
def fabricated_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stand up a temp vault with vault_config.yaml, graph.db, and LanceDB.

    Patches Path.home() to point at tmp_path so load_vault_paths resolves
    to the fabricated tree.
    """
    home = tmp_path / "home"
    vault_dir = home / "sage_vaults" / "test_migrate"
    vault_dir.mkdir(parents=True)
    brain_dir = vault_dir / "brain"
    brain_dir.mkdir()
    (vault_dir / "sources").mkdir()

    import copy

    import yaml

    cfg = copy.deepcopy(VAULT_CONFIG_SOURCE)
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(brain_dir)
    (vault_dir / "vault_config.yaml").write_text(yaml.safe_dump(cfg))

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    return {
        "vault_id": "test_migrate",
        "db_path": brain_dir / "graph.db",
        "lance_path": brain_dir / "lancedb",
        "config": cfg,
    }


def test_run_dry_run_does_not_write(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "t_0001_alpha",
                "title": "T-0001: Alpha",
                "doc_type": "ticket",
                "tags": ["ticket", "id:T-0001", "type:feature", "priority:high"],
            }
        ],
    )
    _seed_chunks(fabricated_vault["lance_path"], [])

    exit_code = run(
        vault_id="test_migrate",
        doc_types=["ticket", "failure_record"],
        execute=False,
        verbose=False,
        modified_by="test",
    )
    assert exit_code == 0
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    row = conn.execute(
        "SELECT tags, tier3_metadata FROM documents WHERE id = 't_0001_alpha'"
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) == ["ticket", "id:T-0001", "type:feature", "priority:high"]
    assert row[1] is None


def test_run_execute_writes_payload_and_strips_tags(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "t_0001_alpha",
                "title": "T-0001: Alpha",
                "doc_type": "ticket",
                "tags": [
                    "ticket",
                    "id:T-0001",
                    "type:feature",
                    "priority:high",
                    "phase-2",
                    "sage",
                    "follow-up-T-0000",
                ],
            }
        ],
    )
    _seed_chunks(fabricated_vault["lance_path"], [])

    exit_code = run(
        vault_id="test_migrate",
        doc_types=["ticket", "failure_record"],
        execute=True,
        verbose=False,
        modified_by="test_writer",
    )
    assert exit_code == 0
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    row = conn.execute(
        "SELECT tags, tier3_metadata, last_modified_by, metadata_confirmed "
        "FROM documents WHERE id = 't_0001_alpha'"
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) == ["ticket", "phase-2", "sage", "follow-up-T-0000"]
    assert json.loads(row[1]) == {
        "ticket_id": "T-0001",
        "ticket_type": "feature",
        "ticket_priority": "high",
    }
    assert row[2] == "test_writer"
    assert row[3] == 1


def test_run_execute_failure_record_with_body(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "ea_f1",
                "title": "F1: Drift",
                "doc_type": "failure_record",
                "tags": [
                    "failure-log",
                    "id:F1",
                    "class:api_drift",
                    "severity:medium",
                    "observed_by:audit",
                    "phase-2",
                ],
            }
        ],
    )
    _seed_chunks(
        fabricated_vault["lance_path"],
        [
            {
                "document_id": "ea_f1",
                "heading_path": "Metadata",
                "doc_type": "failure_record",
                "content": (
                    "Title: F1: Drift\nSource: F-1\nTags: failure-log\n\n"
                    "- failure_id: F1\n"
                    "- failure_class: api_drift\n"
                    "- severity: medium\n"
                    "- observed_by: audit\n"
                    "- caught_by_gate: false\n"
                    "- introduction_commit: abc1234\n"
                    "- discovery_commit: def5678\n"
                    "- fix_commit: null\n"
                    "- session_link: null\n"
                ),
            }
        ],
    )

    exit_code = run(
        vault_id="test_migrate",
        doc_types=["failure_record"],
        execute=True,
        verbose=False,
        modified_by="test",
    )
    assert exit_code == 0
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    row = conn.execute("SELECT tags, tier3_metadata FROM documents WHERE id = 'ea_f1'").fetchone()
    conn.close()
    assert json.loads(row[0]) == ["failure-log", "phase-2"]
    payload = json.loads(row[1])
    assert payload == {
        "failure_id": "F1",
        "failure_class": "api_drift",
        "severity": "medium",
        "observed_by": "audit",
        "caught_by_gate": False,
        "introduction_commit": "abc1234",
        "discovery_commit": "def5678",
        "fix_commit": None,
        "session_link": None,
    }


def test_run_is_idempotent(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "t_already",
                "title": "T-0002: Already migrated",
                "doc_type": "ticket",
                "tags": ["ticket", "phase-2"],
                "tier3_metadata": json.dumps(
                    {
                        "ticket_id": "T-0002",
                        "ticket_type": "fix",
                        "ticket_priority": "low",
                    }
                ),
            }
        ],
    )
    _seed_chunks(fabricated_vault["lance_path"], [])

    exit_code = run(
        vault_id="test_migrate",
        doc_types=["ticket"],
        execute=True,
        verbose=False,
        modified_by="test",
    )
    assert exit_code == 0
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    row = conn.execute(
        "SELECT tags, tier3_metadata, last_modified_by FROM documents WHERE id = 't_already'"
    ).fetchone()
    conn.close()
    # Tags and tier3_metadata unchanged; last_modified_by untouched.
    assert json.loads(row[0]) == ["ticket", "phase-2"]
    assert json.loads(row[1])["ticket_id"] == "T-0002"
    assert row[2] == "test"  # default created_by; not the migration writer


def test_run_aborts_on_schema_violation(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "t_bad_priority",
                "title": "T-0003: bad",
                "doc_type": "ticket",
                "tags": ["ticket", "id:T-0003", "type:feature", "priority:urgent"],
            },
            {
                "id": "t_good",
                "title": "T-0004: good",
                "doc_type": "ticket",
                "tags": ["ticket", "id:T-0004", "type:feature", "priority:high"],
            },
        ],
    )
    _seed_chunks(fabricated_vault["lance_path"], [])

    exit_code = run(
        vault_id="test_migrate",
        doc_types=["ticket"],
        execute=True,
        verbose=False,
        modified_by="test",
    )
    assert exit_code == 2
    # No row was modified — even the good ticket stays untouched because the
    # abort is global.
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    rows = conn.execute("SELECT id, tags, tier3_metadata FROM documents ORDER BY id").fetchall()
    conn.close()
    for _id, tags_json, tier3_json in rows:
        assert tier3_json is None
        # Original tags retained.
        assert "id:T-" in tags_json


def test_build_validators_skips_doc_types_without_schema() -> None:
    config = {
        "document_types": {
            "doc_types": [
                {"value": "ticket", "metadata_schema": {"type": "object"}},
                {"value": "misc"},  # no metadata_schema
            ]
        }
    }
    validators = build_validators(config)
    assert set(validators) == {"ticket"}


def test_build_plan_skips_doc_with_no_migratable_data(fabricated_vault: dict) -> None:
    _seed_documents(
        fabricated_vault["db_path"],
        [
            {
                "id": "t_empty",
                "title": "T-0005: empty tags",
                "doc_type": "ticket",
                "tags": ["ticket", "phase-2"],
            }
        ],
    )
    _seed_chunks(fabricated_vault["lance_path"], [])

    validators = build_validators(fabricated_vault["config"])
    conn = sqlite3.connect(str(fabricated_vault["db_path"]))
    try:
        plan = build_plan(conn, fabricated_vault["lance_path"], validators, ["ticket"])
    finally:
        conn.close()
    assert plan.migrations == []
    assert plan.skipped[0][0] == "t_empty"
    assert "no migratable" in plan.skipped[0][1]
