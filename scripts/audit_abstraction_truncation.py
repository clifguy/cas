#!/usr/bin/env python3
"""Read-only audit of which documents the abstraction provider must truncate.

A document longer than the prompt window is cut before the model sees it, and
the abstract that results describes only the retained prefix -- while being
stored and served as though it described the whole document. Nothing in the
document record says this happened: the provider reports it to the timing log
at generation time and keeps no per-document flag. This audit recovers the
set after the fact by measuring each document against the same budget the
provider spends.

The audit only reads: no document is modified, no abstract is regenerated,
and no inference runtime is loaded. Services initialize with a stub
abstraction provider and the tokenizer is loaded on its own, without the
model weights that accompany it in production. That matters operationally --
a server configured for a large window can claim a substantial share of
accelerator memory during a single generation, and a survey that loaded a
second copy of the model to count tokens would contend with it for no reason.

The threshold is per-document, not the window. The provider divides the
window three ways -- the generated abstract's budget, the chat template's
overhead, and whatever remains for the document -- so the cut-off varies with
the document's length (which sets the abstract budget) and its type (which
sets the template overhead). Both come from the vault's own configuration, so
two vaults can classify the same text differently. The budget arithmetic is
imported from the provider rather than restated here.

With ``--manifest``, the audit writes the truncated ids to a file that
``scripts.reabstract_deferred --ids-file`` reads back. That pairing is the
point: a pass over "whatever is truncated right now" is not reproducible,
while a pass over a written-down set can be re-run, reviewed, and checked
afterwards against what it claimed to cover.

Usage::

    # Survey a vault at its configured window
    .venv/bin/python scripts/audit_abstraction_truncation.py VAULT_ID

    # Survey at a specific window, to compare regimes
    .venv/bin/python scripts/audit_abstraction_truncation.py VAULT_ID --window 32768

    # Freeze the truncated set for a later reabstract pass
    .venv/bin/python scripts/audit_abstraction_truncation.py VAULT_ID --manifest ids.txt

    # Restrict the survey to documents in a given lifecycle state
    .venv/bin/python scripts/audit_abstraction_truncation.py VAULT_ID --lifecycle active

Exit code 0 on a completed audit (findings or none); 2 when the vault config
cannot be found or the tokenizer cannot be loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.abstraction_prompt import _format_system_prompt
from sage.adapters.abstraction_qwen3 import DEFAULT_CONTEXT_WINDOW, available_input_tokens
from sage.adapters.abstraction_utils import compute_max_tokens
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import VaultAbstractionConfig, load_vault_config
from sage.mcp_init import initialize_services, load_stack_config_or_default
from sage.vault_management import config_path_for_vault


@dataclass(frozen=True)
class TruncationRecord:
    """One document measured against the budget available to its text.

    ``body_tokens`` is the document's own length; ``available_tokens`` is
    what the window leaves for it once the abstract budget and template
    overhead are subtracted. The remaining fields are carried so a reader
    can see how the threshold was arrived at rather than having to trust it.
    """

    doc_id: str
    doc_type: str | None
    lifecycle_status: str
    body_tokens: int
    available_tokens: int
    max_tokens: int
    overhead_tokens: int

    @property
    def truncated(self) -> bool:
        """Whether the provider would discard part of this document.

        Strictly greater than: a document exactly filling the budget is sent
        whole, matching the provider's own fit check.
        """
        return self.body_tokens > self.available_tokens

    @property
    def lost_tokens(self) -> int:
        """Tokens of the document the model would never see."""
        return max(self.body_tokens - self.available_tokens, 0)

    @property
    def lost_fraction(self) -> float:
        """Share of the document discarded, as a fraction of its length.

        The absolute count says how much was lost; this says whether what
        survived is most of the document or a small opening slice. A
        document with no body text loses nothing, and reports zero rather
        than dividing by it.
        """
        if self.body_tokens <= 0:
            return 0.0
        return self.lost_tokens / self.body_tokens


def classify_truncation(
    *,
    doc_id: str,
    doc_type: str | None,
    lifecycle_status: str,
    body_text: str,
    effective_window: int,
    abstraction_config: VaultAbstractionConfig,
    count_tokens: Callable[[str], int],
    overhead_for: Callable[[str | None], int],
) -> TruncationRecord:
    """Measure one document against the input budget its abstraction gets.

    Reproduces the provider's own sizing: the density-proportional output
    budget via the shared ``compute_max_tokens``, the template overhead for
    this document's type, and the remainder via the shared
    ``available_input_tokens``. Nothing about the threshold is restated
    locally, so the audit cannot drift from what production enforces.

    Args:
        doc_id: Document identifier, carried through to the record.
        doc_type: Type whose chat template sets the overhead.
        lifecycle_status: Carried so a caller can scope findings; a
            superseded version is rarely worth reabstracting.
        body_text: The document text as the provider would receive it.
        effective_window: Window in force -- the smaller of the configured
            value and the model's native prompt length.
        abstraction_config: The vault's abstraction settings, which set the
            output budget.
        count_tokens: Token counter for the model in question.
        overhead_for: Template overhead in tokens, by document type.
    """
    max_tokens = compute_max_tokens(len(body_text.split()), abstraction_config)
    overhead_tokens = overhead_for(doc_type)
    return TruncationRecord(
        doc_id=doc_id,
        doc_type=doc_type,
        lifecycle_status=lifecycle_status,
        body_tokens=count_tokens(body_text),
        available_tokens=available_input_tokens(effective_window, max_tokens, overhead_tokens),
        max_tokens=max_tokens,
        overhead_tokens=overhead_tokens,
    )


def render_manifest(
    records: list[TruncationRecord],
    *,
    vault_id: str,
    effective_window: int,
    model_id: str,
    measured_at: str,
) -> str:
    """Render the truncated ids as a manifest, one id per line.

    Ordered by how much each document loses, largest first, so the head of
    the file is where the cost of leaving the set alone is concentrated.
    Ties break on id, so the rendering depends only on the records and not
    on the order the vault happened to enumerate them -- a manifest that
    reordered itself between runs could not be diffed against an earlier one.

    The header names the vault, the threshold, and the model that set it.
    Read later, a bare list of ids says nothing about what selected them, and
    the same ids measured against a different model or window are a different
    set. ``scripts.reabstract_deferred --ids-file`` skips these lines.
    """
    truncated = sorted(
        (record for record in records if record.truncated),
        key=lambda record: (-record.lost_tokens, record.doc_id),
    )
    lines = [
        f"# vault: {vault_id}",
        f"# effective_window: {effective_window}",
        f"# model: {model_id}",
        f"# measured_at: {measured_at}",
        f"# truncated_documents: {len(truncated)}",
        f"# tokens_discarded: {sum(record.lost_tokens for record in truncated)}",
        "",
    ]
    lines.extend(record.doc_id for record in truncated)
    return "\n".join(lines) + "\n"


def _load_tokenizer(model_id: str):
    """Load the tokenizer for a model without its weights.

    The audit needs to count tokens exactly as the provider would, but has
    no use for the model itself. Loading the tokenizer alone keeps the
    survey's memory cost negligible and lets it run alongside a server that
    already holds the weights.
    """
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("transformers is required to count tokens for the audit") from exc
    return AutoTokenizer.from_pretrained(model_id)


def _overhead_lookup(tokenizer) -> Callable[[str | None], int]:
    """Build a per-doc_type template-overhead counter, memoized.

    The overhead is the token cost of the chat template and system prompt
    with no document in it -- constant for a given type, so it is measured
    once per type rather than once per document.
    """
    cache: dict[str | None, int] = {}

    def overhead_for(doc_type: str | None) -> int:
        cached = cache.get(doc_type)
        if cached is not None:
            return cached
        messages = [
            {"role": "system", "content": _format_system_prompt(doc_type)},
            {"role": "user", "content": ""},
        ]
        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        overhead = len(tokenizer.encode(prompt))
        cache[doc_type] = overhead
        return overhead

    return overhead_for


async def _body_text(services, doc_id: str) -> str:
    """Reassemble a document's text from its stored chunks.

    Excludes the synthetic header chunk, which carries metadata rather than
    document content and is not part of what the provider is handed.
    """
    chunks = await services.content_store.get_all_chunks(doc_id)
    return "\n\n".join(
        chunk.content for chunk in chunks if chunk.heading_path != SYNTHETIC_HEADER_HEADING_PATH
    )


def _render_report(records: list[TruncationRecord], *, effective_window: int) -> list[str]:
    """Console summary: the counts first, then the worst offenders."""
    truncated = sorted(records, key=lambda r: (-r.lost_tokens, r.doc_id))
    truncated = [record for record in truncated if record.truncated]

    lines = [
        f"Documents measured:  {len(records)}",
        f"Effective window:    {effective_window}",
        f"Truncated:           {len(truncated)}",
    ]
    if not truncated:
        lines.append("No document exceeds the budget available to its text.")
        return lines

    discarded = sum(record.lost_tokens for record in truncated)
    lines.append(f"Tokens discarded:    {discarded}")
    lines.append("")
    lines.append(f"{'lost%':>7}  {'lost':>9}  {'length':>9}  {'status':<12}  document")
    for record in truncated:
        lines.append(
            f"{record.lost_fraction * 100:6.1f}%  "
            f"{record.lost_tokens:9d}  "
            f"{record.body_tokens:9d}  "
            f"{record.lifecycle_status:<12}  "
            f"{record.doc_id}"
        )
    return lines


async def run(
    vault_id: str,
    *,
    window: int | None,
    manifest: Path | None,
    lifecycle: str | None,
) -> int:
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    stack = load_stack_config_or_default()
    model_id = stack.abstraction.model
    if not model_id:
        print(
            "the stack names no abstraction model, so there is no tokenizer "
            "to measure with; configure one or run against a stack that has one",
            file=sys.stderr,
        )
        return 2

    # The survey's window is a deliberate input, not a fact about the running
    # stack: comparing regimes means measuring the same corpus at a window
    # other than the one currently configured. Taken as given -- in
    # production the provider clamps to the model's native prompt length,
    # which is knowable only from loaded weights, so a window above what the
    # model supports would survey a threshold production would not honor.
    effective_window = window
    if effective_window is None:
        effective_window = stack.abstraction.context_window or DEFAULT_CONTEXT_WINDOW

    try:
        tokenizer = _load_tokenizer(model_id)
    except Exception as exc:
        print(f"cannot load tokenizer for {model_id!r}: {exc}", file=sys.stderr)
        return 2
    overhead_for = _overhead_lookup(tokenizer)

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text))

    print(f"Loading SAGE services for vault {vault_id!r}...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        records: list[TruncationRecord] = []
        for doc in await services.graph_store.list_all_documents():
            if lifecycle is not None and doc.lifecycle_status != lifecycle:
                continue
            body = await _body_text(services, doc.id)
            if not body:
                continue
            records.append(
                classify_truncation(
                    doc_id=doc.id,
                    doc_type=doc.doc_type,
                    lifecycle_status=doc.lifecycle_status,
                    body_text=body,
                    effective_window=effective_window,
                    abstraction_config=config.abstraction,
                    count_tokens=count_tokens,
                    overhead_for=overhead_for,
                )
            )

        print()
        print(f"Vault: {vault_id}")
        for line in _render_report(records, effective_window=effective_window):
            print(line)

        if manifest is not None:
            manifest.write_text(
                render_manifest(
                    records,
                    vault_id=vault_id,
                    effective_window=effective_window,
                    model_id=model_id,
                    measured_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            print()
            print(f"Manifest written: {manifest}")
        return 0
    finally:
        await services.graph_store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report which documents the abstraction provider must truncate, "
            "and optionally write the set to a manifest. Read-only; loads no "
            "model weights. See script docstring."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. example_vault)")
    parser.add_argument(
        "--window",
        type=int,
        metavar="TOKENS",
        help=(
            "Measure against this context window instead of the configured "
            "one. Use to compare regimes -- for instance, to recover the set "
            "that was truncated under an earlier setting."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help=(
            "Write the truncated document ids here, largest loss first, with "
            "a provenance header. Consumed by "
            "'scripts.reabstract_deferred --ids-file'."
        ),
    )
    parser.add_argument(
        "--lifecycle",
        metavar="STATUS",
        help=(
            "Measure only documents in this lifecycle status. Superseded "
            "versions are retained but not served, and are rarely worth "
            "including in a pass."
        ),
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    rc = asyncio.run(
        run(
            args.vault_id,
            window=args.window,
            manifest=args.manifest,
            lifecycle=args.lifecycle,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
