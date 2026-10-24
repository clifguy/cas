"""Opt-in real-provider calls with local disposable Postgres stored/served readback.

Not hosted acceptance. Run serially with SAGE_TEST_HYBRID_PROVIDER=qwen|anthropic;
only these synthetic source strings are transmitted. No classes are promoted.
"""

import json
import os
from pathlib import Path

import pytest

PROVIDER = os.environ.get("SAGE_TEST_HYBRID_PROVIDER")


@pytest.mark.skipif(
    PROVIDER not in ("qwen", "anthropic"), reason="explicit synthetic real-provider tier"
)
@pytest.mark.parametrize(
    "text",
    [
        "CAS coordinates document work and retains project records.",
        "The orchard team documents pruning and maintains community resources.",
    ],
)
async def test_real_hybrid_candidate_is_persisted_and_served_locally(
    ingestion_service, tmp_vault_dir, pg_pool, monkeypatch, text
):
    from sage.models.schemas import IngestRequest
    from sage.models.wire import to_wire
    from sage.services.documents import DocumentsService

    if PROVIDER == "qwen":
        import yaml

        from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider

        cfg = yaml.safe_load((Path(__file__).parents[2] / "sage/config.yaml").read_text())[
            "abstraction"
        ]
        provider = Qwen3AbstractionProvider(
            cfg["model"],
            revision=cfg["revision"],
            context_window=cfg["context_window"],
            opener_constraint=cfg["opener_constraint"],
        )
    else:
        from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

        provider = AnthropicAbstractionProvider("claude-haiku-5-5")
    service = ingestion_service
    service._config.abstraction = service._config.abstraction.model_copy(
        update={"hybrid_description": True}
    )
    service._abstraction = provider
    source = tmp_vault_dir / "synthetic-real-hybrid.md"
    source.write_text(text)
    seen = []
    actual = provider.generate_abstract

    async def generate(value, max_tokens, doc_type):
        assert "SAGE-computed observations (data)" not in value
        seen.append(str(value))
        return await actual(value, max_tokens, doc_type)

    monkeypatch.setattr(provider, "generate_abstract", generate)
    try:
        doc = (await service.ingest(IngestRequest(source=str(source)))).document
        current = await service._store.get_document(doc.id)
        assert current.pipeline_status.value == "abstraction_complete"
        quality = current.processing_coverage.abstraction_quality
        assert quality.mode in ("recording_generated", "extractive_fallback", "unavailable")
        assert quality.generation == current.processing_coverage.attempt
        assert not quality.calibration_refs
        served = await DocumentsService(service._store, service._config).get_document_with_content(
            doc.id, False, None
        )
        public = to_wire(served)
        assert public["processing_coverage"]["abstraction_quality"]["mode"] == quality.mode
        assert public.get("semantic_abstract") == current.semantic_abstract
        async with pg_pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT semantic_abstract, abstraction_state->'candidate', "
                    "processing_coverage->'abstraction_quality' FROM documents WHERE id=%s",
                    (doc.id,),
                )
            ).fetchone()
        assert row[0] == current.semantic_abstract
        assert row[1]["sha256"] == quality.candidate_sha256
        assert row[2] == quality.model_dump(mode="json")
        assert "raw" not in public["processing_coverage"]["abstraction_quality"]
        output = Path(
            os.environ.get(
                "SAGE_TEST_HYBRID_EVIDENCE", f"/private/tmp/cas-hybrid-real-{PROVIDER}.json"
            )
        )
        records = json.loads(output.read_text()) if output.exists() else []
        records.append(
            {
                "provider": PROVIDER,
                "model": provider._model_id,
                "source": text,
                "provider_inputs": seen,
                "raw": row[1],
                "stored": row[0],
                "served": public.get("semantic_abstract"),
                "quality": row[2],
                "limitation": (
                    "Real provider plus shared ingestion and local disposable "
                    "Postgres/wire readback; not a hosted deployment or calibration."
                ),
            }
        )
        output.write_text(json.dumps(records, indent=2) + "\n")
    finally:
        await service.stop_worker()
        if hasattr(provider, "unload"):
            await provider.unload()
