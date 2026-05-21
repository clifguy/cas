"""Seed the cas vault with three tag-scoped fixture documents for the
Playwright bulk-actions spec.

Usage:
    python app/e2e/fixtures/seed_e2e_fixtures.py [--backend http://localhost:8000]

Idempotency: this script discovers any cas-vault documents tagged
`e2e-bulk-fixture` and archives them before ingesting three fresh
markdown documents with the same tag. A timestamp nonce in each body
keeps the content hash fresh across runs so the ingest does not collide
with archived predecessors under `duplicate_content`.

Replaces seed_bulk_e2e.py — see T-0130 for the rationale (drop the
dedicated bulk_e2e vault; run fixtures alongside real cas content,
scoped by tag).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VAULT_ID = "cas"
FIXTURE_TAG = "e2e-bulk-fixture"
FIXTURE_DOC_TYPE = "misc"
FIXTURE_TITLES = ("e2e-doc-1", "e2e-doc-2", "e2e-doc-3")


def http_request(
    method: str, url: str, payload: dict | None = None
) -> tuple[int, dict | list | None]:
    # URL is constructed from the --backend argument plus hardcoded path
    # fragments inside this script; no user-supplied scheme reaches urlopen.
    # S310 audit-only lint is therefore not actionable here.
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode("utf-8", "replace")}


def archive_prior_fixtures(backend: str) -> int:
    """Best-effort: archive every active doc tagged FIXTURE_TAG in the cas vault."""
    status, body = http_request(
        "POST",
        f"{backend}/sage_vaults/{VAULT_ID}/discover",
        {
            "mode": "catalog",
            "filters": {"tags": [FIXTURE_TAG], "lifecycle_status": "active"},
            "limit": 100,
        },
    )
    if status != 200 or not isinstance(body, dict):
        print(f"  warning: discover failed ({status}); skipping cleanup")
        return 0
    results = body.get("results", [])
    if not results:
        return 0
    items = [{"document_id": r["document"]["id"], "action": "archive"} for r in results]
    print(f"  archiving {len(items)} pre-existing fixture documents")
    http_request(
        "POST",
        f"{backend}/sage_vaults/{VAULT_ID}/lifecycle/bulk",
        {"items": items},
    )
    return len(items)


def ingest_one(backend: str, source_path: str, title: str) -> None:
    payload = {
        "source": source_path,
        "adapter": "markdown",
        "metadata": {
            "title": title,
            "doc_type": FIXTURE_DOC_TYPE,
            "tags": [FIXTURE_TAG],
        },
        "needs_review": False,
    }
    status, body = http_request("POST", f"{backend}/sage_vaults/{VAULT_ID}/documents", payload)
    if status not in (200, 201):
        raise SystemExit(f"ingest failed for {title}: {status} {body!r}")


def ingest_fresh_fixtures(backend: str) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="cas_e2e_fixtures_"))
    nonce = int(time.time())
    for title in FIXTURE_TITLES:
        src_path = workdir / f"{title}.md"
        # Nonce in the body keeps the content hash fresh on re-runs so the ingest
        # does not collide with the archived predecessor under duplicate_content.
        src_path.write_text(
            f"# {title}\n\nTrivial fixture content for the bulk-actions e2e (T-0130).\n\n"
            f"seed-nonce: {nonce}\n"
        )
        print(f"  ingesting {title}")
        ingest_one(backend, str(src_path), title)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://localhost:8000")
    args = parser.parse_args()
    backend = args.backend.rstrip("/")

    print(f"seeding {VAULT_ID} fixtures (tag={FIXTURE_TAG}) via {backend}")
    archive_prior_fixtures(backend)
    ingest_fresh_fixtures(backend)
    print(f"seed complete: 3 active fixture documents in {VAULT_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
