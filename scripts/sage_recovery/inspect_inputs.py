"""Read-only local tokenizer/chunker diagnostic; no model inference or vault writes."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

from .config import settings
from .recovery_checks import atomic, originals, require


async def inspect():
    from transformers import AutoTokenizer

    from sage.adapters.embedding_nomic import MAX_INPUT_TOKENS, MODEL_PINS, NOMIC_MODEL_NAME
    from sage.adapters.tokenization import require_bounded_tokenizer_input
    from sage.services.ingestion import IngestionService
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    class TokenizerOnly:
        max_input_tokens = MAX_INPUT_TOKENS

        def count_tokens(self, text):
            require_bounded_tokenizer_input(text)
            return len(self.tokenizer(text)["input_ids"])

    pin = MODEL_PINS[NOMIC_MODEL_NAME]
    tokenizer = AutoTokenizer.from_pretrained(
        NOMIC_MODEL_NAME, revision=pin.revision, local_files_only=True
    )
    service = IngestionService.__new__(IngestionService)
    service._embedding = TokenizerOnly()
    service._embedding.tokenizer = tokenizer
    rows = []
    for s in originals()[0]:
        began = time.monotonic()
        p = await StructuredDataAdapter().project(Path(s["source"]))
        require(
            hashlib.sha256(p.text.encode()).hexdigest() == s["expected_projection_sha256"],
            "projection differs",
        )
        chunks = service._chunk_projection("00000001_local_diagnostic", p)
        require("".join(c.content for c in chunks) == p.text, "reconstruction differs")
        tokens = [service._embedding.count_tokens(c.content) for c in chunks]
        require(max(tokens) <= MAX_INPUT_TOKENS, "token budget exceeded")
        row = {
            "filename": s["filename"],
            "input_bytes": s["bytes"],
            "projection_characters": len(p.text),
            "bounded_passages": len(chunks),
            "summed_input_tokens_including_special_tokens": sum(tokens),
            "max_input_tokens": max(tokens),
            "p95_input_tokens": sorted(tokens)[int(0.95 * (len(tokens) - 1))],
            "exact_reconstruction": True,
            "diagnostic_seconds": time.monotonic() - began,
            "scope": "local pinned-tokenizer and actual chunker; no model throughput measurement",
        }
        rows.append(row)
        path = settings().root / "input-workload.json"
        atomic(
            path, {"tokenizer_model": NOMIC_MODEL_NAME, "revision": pin.revision, "records": rows}
        )
        print(json.dumps(row), flush=True)


def main():
    asyncio.run(inspect())
