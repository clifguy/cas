"""Bounded synthetic hybrid trials; direct providers, no vault writes or promotion.

These are behavioral observations, not held-out calibration or hosted acceptance.
The stored/served path is exercised separately by the opt-in service regression.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from sage.adapters.abstraction_prompt import _format_system_prompt, wrap_source_document
from sage.adapters.abstraction_utils import (
    collapse_unattested_acronym_glosses,
    trim_to_sentence_boundary,
)
from sage.adapters.interfaces import BoundedEvidenceText
from sage.config import VaultAbstractionConfig
from sage.services.abstraction_publication import (
    GeneratedDescription,
    compose_publication,
    prepare_publication,
)
from sage.services.bounded_abstraction import abstraction_request
from scripts.evaluate_abstraction_faithfulness import cases


async def evaluate(provider, output: Path, repeats: int) -> None:
    records = []
    try:
        for name, old_text, package in cases(include_packages=True):
            request = (
                abstraction_request(package, VaultAbstractionConfig(), generation=1)
                if package
                else None
            )
            budget = prepare_publication(package, request, 1500) if package else None
            authored = (
                old_text.authored_text if isinstance(old_text, BoundedEvidenceText) else old_text
            )
            text = BoundedEvidenceText(authored, authored_text=authored) if package else authored
            target = min(225, budget.prose_tokens) if budget else 225
            for repeat in range(repeats):
                started = time.monotonic()
                raw = await provider.generate_abstract(text, target, None)
                repaired = collapse_unattested_acronym_glosses(
                    trim_to_sentence_boundary(raw), authored
                )
                publication = (
                    compose_publication(
                        package, request, GeneratedDescription(repaired, raw), budget, 1, {}
                    )
                    if package
                    else None
                )
                system = _format_system_prompt(None, bounded=package is not None)
                user = wrap_source_document(text)
                records.append(
                    {
                        "case": name,
                        "repeat": repeat + 1,
                        "provider": type(provider).__name__,
                        "model": provider._model_id,
                        "revision": getattr(provider, "_revision", None),
                        "system": system,
                        "user": user,
                        "authored": authored,
                        "raw": raw,
                        "gloss_repaired": repaired,
                        "composition": publication.text if publication else repaired,
                        "quality": publication.quality.model_dump(mode="json")
                        if publication
                        else None,
                        "reserved_input_token_bound": len((system + user).encode()) + 1024,
                        "output_allowance": target,
                        "seconds": time.monotonic() - started,
                        "limitation": (
                            "Direct provider and gloss-only repair; no held-out adjudication "
                            "or stored/served acceptance. No classes promoted."
                        ),
                    }
                )
                output.write_text(json.dumps(records, indent=2) + "\n")
                print(name, repeat + 1, json.dumps(raw), flush=True)
    finally:
        if hasattr(provider, "unload"):
            await provider.unload()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("qwen", "anthropic"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if args.provider == "qwen":
        import yaml

        from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider

        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "sage/config.yaml").read_text()
        )["abstraction"]
        provider = Qwen3AbstractionProvider(
            cfg["model"],
            revision=cfg["revision"],
            context_window=cfg["context_window"],
            opener_constraint=cfg["opener_constraint"],
        )
    else:
        from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

        provider = AnthropicAbstractionProvider("claude-haiku-5-5")
    asyncio.run(evaluate(provider, args.output, args.repeats))


if __name__ == "__main__":
    main()
