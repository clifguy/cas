"""Exercise the verifier against real canonical Postgres tables, outside timing."""

import hashlib
import json
import math
import os
import sys
from types import SimpleNamespace

import psycopg
import pytest
import yaml
from psycopg import sql


@pytest.mark.asyncio
async def test_verifier_reads_real_canonical_tables_and_rejects_wrong_vectors(
    tmp_path, monkeypatch, _provision_isolated_test_database
):
    import azure.identity

    from deploy import performance_reconcile
    from sage.adapters import embedding_nomic
    from sage.services.ingestion import IngestionService
    from sage.source_adapters.markdown_adapter import MarkdownAdapter
    from sage.storage.postgres.schema import CHUNKS_TABLE, DOCUMENT_SURFACE_TABLE, DOCUMENTS_TABLE

    class Provider:
        max_input_tokens = 8192

        def count_tokens(self, text):
            return len(text.encode())

        async def embed(self, texts):
            result = []
            for text in texts:
                values = [float(x + 1) for x in hashlib.sha256(text.encode()).digest()]
                norm = math.sqrt(sum(x * x for x in values))
                result.append([x / norm for x in values] + [0.0] * 736)
            return result

    run_id, vault, doc_id = "trial-one", "perf_trial_one_a0", "synthetic"
    raw = b"# Witness\n\nA small representative source.\n"
    digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / "witness.md"
    source.write_bytes(raw)
    provider = Provider()
    projection = await MarkdownAdapter().project(source)
    builder = object.__new__(IngestionService)
    builder._embedding = provider
    expected = builder._chunk_projection(doc_id, projection)
    request = {
        "vaults": [
            {
                "id": vault,
                "expected_terminal": "abstraction_complete",
                "sources": [{"name": source.name, "sha256": digest}],
            }
        ]
    }
    output = []

    class Evidence:
        def __init__(self, *args):
            pass

        def request(self, method, name, **kwargs):
            if method == "PUT":
                output.append(json.loads(kwargs["content"]))
                return SimpleNamespace(raise_for_status=lambda: None)
            if name == "reconcile-request.json":
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: request)
            assert name == "sources/witness.md"
            return SimpleNamespace(raise_for_status=lambda: None, content=raw)

    original_connect = psycopg.connect
    dsn = os.environ["SAGE_TEST_PG_DSN"]
    connect_calls = []

    def verifier_connection(**kwargs):
        connect_calls.append(kwargs)
        assert kwargs["host"] == "psql-perf-trial-one-isolated"
        return original_connect(dsn)

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "document_store": {"root_path": "performance/trial-one"},
                "postgres": {
                    "host": "psql-perf-trial-one-isolated",
                    "database": "sage",
                    "user": "trial",
                },
            }
        )
    )
    monkeypatch.setenv("SAGE_CONFIG_PATH", str(config))
    monkeypatch.setenv("PERF_RUN_ID", run_id)
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic")
    monkeypatch.setenv("PERF_EXPORT_URL", "unused-in-this-transport")
    monkeypatch.setitem(sys.modules, "performance_runtime", SimpleNamespace(Evidence=Evidence))
    monkeypatch.setattr(embedding_nomic, "NomicEmbeddingProvider", Provider)
    monkeypatch.setattr(
        azure.identity,
        "ManagedIdentityCredential",
        lambda **kw: SimpleNamespace(get_token=lambda *a: SimpleNamespace(token="synthetic")),
    )
    monkeypatch.setattr(psycopg, "connect", verifier_connection)
    with original_connect(dsn, autocommit=True) as admin:
        schema = sql.Identifier(vault)
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            admin.execute(sql.SQL("SET search_path TO {}, public").format(schema))
            for ddl in (DOCUMENTS_TABLE, CHUNKS_TABLE, DOCUMENT_SURFACE_TABLE):
                admin.execute(ddl)
            admin.execute(
                """INSERT INTO documents
                (id,title,source_type,source_path,source_content_hash,adapter_version,
                 created_by,created_at,last_modified_by,updated_at,pipeline_status)
                VALUES (%s,'Witness','markdown','witness.md',%s,'1','trial','now',
                        'trial','now','abstraction_complete')""",
                (doc_id, digest),
            )
            from sage.services.passage_split import embedding_input

            for chunk in expected:
                vector = (
                    await provider.embed([embedding_input(chunk.heading_path, chunk.content)])
                )[0]
                admin.execute(
                    """INSERT INTO chunks
                    (document_id,chunk_index,heading_path,content,section_index,embedding)
                    VALUES (%s,%s,%s,%s,%s,%s::vector)""",
                    (
                        doc_id,
                        chunk.chunk_index,
                        chunk.heading_path,
                        chunk.content,
                        chunk.section_index,
                        json.dumps(vector),
                    ),
                )
            vector = (await provider.embed(["Witness\n\nwitness"]))[0]
            admin.execute(
                """INSERT INTO document_surface
                (document_id,matchable,orienting,embedding)
                VALUES (%s,'Witness','witness',%s::vector)""",
                (doc_id, json.dumps(vector)),
            )
            await performance_reconcile.run()
            assert len(connect_calls) == 1
            assert output[0]["documents"][0]["passages"] == len(expected)
            assert output[0]["documents"][0]["all_vectors_associated"] is True
            # A different normalized vector must fail even though count/norm pass.
            admin.execute(
                "UPDATE chunks SET embedding=%s::vector", (json.dumps([1.0] + [0.0] * 767),)
            )
            with pytest.raises(ValueError, match="does not match its passage"):
                await performance_reconcile.run()
            assert len(output) == 1
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
