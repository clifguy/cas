"""Read-only, in-VNet verification of trial passages and stored vector associations.

Mounted into the baseline image after measurement; no live service or second
pipeline owner is started. Re-encoding references is outside the measured interval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


def verify_rows(expected, actual):
    """Compare every passage's identity/order/content plus vector norm."""
    if not expected or len(expected) != len(actual):
        raise ValueError("passage coverage differs")
    for want, got in zip(expected, actual, strict=True):
        if (want.chunk_index, want.heading_path, want.content, want.section_index) != tuple(
            got[:4]
        ):
            raise ValueError("passage order/content/association differs")
        vector = got[4]
        if (
            len(vector) != 768
            or not all(math.isfinite(x) for x in vector)
            or abs(sum(x * x for x in vector) - 1) > 0.001
        ):
            raise ValueError("missing or invalid normalized vector")


async def run():
    import numpy as np
    import psycopg
    import yaml
    from azure.identity import ManagedIdentityCredential
    from performance_runtime import Evidence
    from psycopg import sql

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider
    from sage.services.ingestion import IngestionService
    from sage.services.passage_split import embedding_input, join_passages
    from sage.source_adapters.markdown_adapter import MarkdownAdapter
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    run_id = os.environ["PERF_RUN_ID"]
    cfg = yaml.safe_load(Path(os.environ["SAGE_CONFIG_PATH"]).read_text())
    if cfg["document_store"]["root_path"] != "performance/" + run_id or not cfg["postgres"][
        "host"
    ].startswith("psql-perf-" + run_id + "-"):
        raise ValueError("refusing non-isolated backend")
    store = Evidence(os.environ["PERF_EXPORT_URL"], run_id)
    request = store.request("GET", "reconcile-request.json")
    request.raise_for_status()
    manifest = request.json()
    provider = NomicEmbeddingProvider()
    # The pure canonical section/passage builder consumes only this provider;
    # no ingestion service constructor, queue or background worker is started.
    builder = object.__new__(IngestionService)
    builder._embedding = provider
    token = (
        ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
        .get_token("https://ossrdbms-aad.database.windows.net/.default")
        .token
    )
    reports = []
    with psycopg.connect(
        host=cfg["postgres"]["host"],
        dbname=cfg["postgres"]["database"],
        user=cfg["postgres"]["user"],
        password=token,
        sslmode="require",
        connect_timeout=15,
    ) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        for vault in manifest["vaults"]:
            if not vault["id"].startswith("perf_" + run_id.replace("-", "_") + "_"):
                raise ValueError("unexpected schema")
            schema = sql.Identifier(vault["id"])
            docs = conn.execute(
                sql.SQL(
                    "SELECT id,source_content_hash,source_type,pipeline_status FROM"
                    " {}.documents ORDER BY id"
                ).format(schema)
            ).fetchall()
            expected_hashes = {x["sha256"]: x for x in vault["sources"]}
            if {x[1].removeprefix("sha256:") for x in docs} != set(expected_hashes) or len(
                docs
            ) != len(expected_hashes):
                raise ValueError("source inventory differs; no blind retry")
            for doc_id, digest, source_type, status in docs:
                if status != vault["expected_terminal"]:
                    raise ValueError("partial pipeline cannot pass reconciliation")
                source = expected_hashes[digest.removeprefix("sha256:")]
                response = store.request("GET", "sources/" + source["name"])
                response.raise_for_status()
                raw = response.content
                if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                    raise ValueError("frozen source hash differs")
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / source["name"]
                    path.write_bytes(raw)
                    adapter = (
                        MarkdownAdapter() if source_type == "markdown" else StructuredDataAdapter()
                    )
                    projection = await adapter.project(path)
                expected = builder._chunk_projection(doc_id, projection)
                actual = conn.execute(
                    sql.SQL(
                        "SELECT chunk_index,heading_path,content,section_index,embedding::text"
                        " FROM {}.chunks WHERE document_id=%s"
                        " AND chunk_index>=0 ORDER BY chunk_index"
                    ).format(schema),
                    (doc_id,),
                ).fetchall()
                actual = [(*x[:4], json.loads(x[4]) if x[4] else []) for x in actual]
                verify_rows(expected, actual)
                for want, got in zip(expected, actual, strict=True):
                    reference = (
                        await provider.embed([embedding_input(want.heading_path, want.content)])
                    )[0]
                    if not np.allclose(reference, got[4], atol=1e-4, rtol=1e-3):
                        raise ValueError("stored vector does not match its passage")
                surface = conn.execute(
                    sql.SQL(
                        "SELECT matchable,orienting,embedding::text FROM {}.document_surface WHERE"
                        " document_id=%s"
                    ).format(schema),
                    (doc_id,),
                ).fetchall()
                if len(surface) != 1 or not surface[0][2]:
                    raise ValueError("document surface absent")
                reference = (await provider.embed([surface[0][0] + "\n\n" + surface[0][1]]))[0]
                if not np.allclose(reference, json.loads(surface[0][2]), atol=1e-4, rtol=1e-3):
                    raise ValueError("document surface association differs")
                reports.append(
                    {
                        "vault": vault["id"],
                        "document_id": doc_id,
                        "source_sha256": source["sha256"],
                        "source_bytes": len(raw),
                        "projected_characters": len(projection.text),
                        "reconstructed_projection_sha256": hashlib.sha256(
                            join_passages(expected).encode()
                        ).hexdigest(),
                        "passages": len(actual),
                        "all_vectors_associated": True,
                        "pipeline_status": status,
                    }
                )
    result = {
        "kind": "reconciliation",
        "outcome": "complete",
        "documents": reports,
        "reference_inference": "outside_measured_interval",
    }
    response = store.request(
        "PUT",
        "reconciliation.json",
        content=json.dumps(result).encode(),
        headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"},
    )
    response.raise_for_status()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(run())
