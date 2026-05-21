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

# Maintenance panel e2e (T-0117) — one document in abstraction_skipped so the
# reabstract worklist is non-empty when the panel mounts.
MAINTENANCE_FIXTURE_TAG = "e2e-maintenance-fixture"
MAINTENANCE_FIXTURE_TITLE = "e2e-deferred-abstract-fixture"
TERMINAL_PIPELINE_STATUSES = frozenset({"abstraction_complete", "abstraction_skipped", "failed"})


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


def get_vault_config(backend: str) -> dict:
    status, body = http_request("GET", f"{backend}/sage_vaults/{VAULT_ID}/config")
    if status != 200 or not isinstance(body, dict):
        raise SystemExit(f"GET vault config failed: {status} {body!r}")
    return body


def set_abstraction_enabled(backend: str, enabled: bool) -> None:
    """Patch the cas vault config to toggle vault-scope abstraction.

    The ingestion pipeline reads ``vault_config.abstraction.enabled`` at
    Stage 3 (see sage/services/ingestion.py:730 — BH-025). When False,
    documents transition directly to ``abstraction_skipped`` without
    dispatching to the abstraction provider. This is the only HTTP-level
    seam that produces an abstraction_skipped document on a vault whose
    abstraction is otherwise enabled; pipeline_status itself is not a
    user-mutable field.
    """
    status, body = http_request(
        "PUT",
        f"{backend}/sage_vaults/{VAULT_ID}/config",
        {"abstraction": {"enabled": enabled}},
    )
    if status != 200:
        raise SystemExit(
            f"PUT vault config (abstraction.enabled={enabled}) failed: {status} {body!r}"
        )


def archive_prior_maintenance_fixtures(backend: str) -> int:
    """Archive any docs tagged ``e2e-maintenance-fixture`` so the seed is idempotent.

    Sweeps both ``active`` and ``completed`` lifecycle states; archived
    predecessors do not need re-archiving (they're already off the
    discover-filtered worklist).
    """
    archived = 0
    for lifecycle in ("active", "completed"):
        status, body = http_request(
            "POST",
            f"{backend}/sage_vaults/{VAULT_ID}/discover",
            {
                "mode": "catalog",
                "filters": {"tags": [MAINTENANCE_FIXTURE_TAG], "lifecycle_status": lifecycle},
                "limit": 100,
            },
        )
        if status != 200 or not isinstance(body, dict):
            continue
        results = body.get("results", [])
        if not results:
            continue
        items = [{"document_id": r["document"]["id"], "action": "archive"} for r in results]
        http_request(
            "POST",
            f"{backend}/sage_vaults/{VAULT_ID}/lifecycle/bulk",
            {"items": items},
        )
        archived += len(items)
    return archived


def wait_for_terminal(backend: str, document_id: str, timeout: float = 30.0) -> str | None:
    """Poll a document until its pipeline_status is terminal."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = http_request(
            "GET", f"{backend}/sage_vaults/{VAULT_ID}/documents/{document_id}"
        )
        if status == 200 and isinstance(body, dict):
            ps = body.get("pipeline_status")
            if ps in TERMINAL_PIPELINE_STATUSES:
                return ps
        time.sleep(0.5)
    return None


def ingest_maintenance_fixture(backend: str) -> str:
    workdir = Path(tempfile.mkdtemp(prefix="cas_e2e_maint_"))
    nonce = int(time.time())
    src_path = workdir / f"{MAINTENANCE_FIXTURE_TITLE}.md"
    src_path.write_text(
        f"# {MAINTENANCE_FIXTURE_TITLE}\n\n"
        f"Fixture document for the maintenance-panel e2e (T-0117).\n\n"
        f"seed-nonce: {nonce}\n"
    )
    payload = {
        "source": str(src_path),
        "adapter": "markdown",
        "metadata": {
            "title": MAINTENANCE_FIXTURE_TITLE,
            "doc_type": FIXTURE_DOC_TYPE,
            "tags": [MAINTENANCE_FIXTURE_TAG],
        },
        "needs_review": False,
    }
    status, body = http_request("POST", f"{backend}/sage_vaults/{VAULT_ID}/documents", payload)
    if status not in (200, 201) or not isinstance(body, dict):
        raise SystemExit(f"ingest of maintenance fixture failed: {status} {body!r}")
    # IngestResponse wraps the Document under the `document` key alongside
    # `pipeline_status` — the HTTP shape, not the bare-Document shape some
    # other endpoints use. Confirmed in sage/api/routers/ingestion.py:63-69.
    document = body.get("document")
    if not isinstance(document, dict) or "id" not in document:
        raise SystemExit(f"ingest response missing document.id: {body!r}")
    return document["id"]


def seed_maintenance_fixture(backend: str) -> None:
    """Seed at least one document with pipeline_status=abstraction_skipped.

    Approach: archive prior fixtures, temporarily disable vault-scope
    abstraction, ingest a marker document, wait for it to reach a terminal
    pipeline_status, then restore abstraction. The toggle window is bounded
    by the wait + a try/finally, so a crash mid-seed cannot leave the cas
    vault with abstraction disabled.

    Per sage/services/ingestion.py:730-740, ingestion with
    abstraction.enabled=False transitions documents directly to
    abstraction_skipped (BH-025), which is exactly the worklist the
    reabstract-deferred endpoint targets.
    """
    archived = archive_prior_maintenance_fixtures(backend)
    if archived:
        print(f"  archived {archived} prior maintenance fixture(s)")

    cfg_before = get_vault_config(backend)
    abstraction_was_enabled = bool(cfg_before.get("abstraction", {}).get("enabled"))
    if abstraction_was_enabled:
        print("  temporarily disabling vault-scope abstraction (BH-025)")
        set_abstraction_enabled(backend, False)
    try:
        print(f"  ingesting {MAINTENANCE_FIXTURE_TITLE} (expecting abstraction_skipped)")
        doc_id = ingest_maintenance_fixture(backend)
        terminal = wait_for_terminal(backend, doc_id)
        if terminal != "abstraction_skipped":
            raise SystemExit(
                f"maintenance fixture {doc_id} did not reach abstraction_skipped "
                f"(got {terminal!r}); the maintenance e2e cannot proceed"
            )
        print(f"  fixture {doc_id} reached abstraction_skipped")
    finally:
        if abstraction_was_enabled:
            print("  restoring vault-scope abstraction")
            set_abstraction_enabled(backend, True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://localhost:8000")
    args = parser.parse_args()
    backend = args.backend.rstrip("/")

    print(f"seeding {VAULT_ID} fixtures (tag={FIXTURE_TAG}) via {backend}")
    archive_prior_fixtures(backend)
    # Maintenance fixture goes first so its disable-ingest-wait-enable
    # window completes before the bulk ingests start; otherwise mid-flight
    # bulk docs could land in abstraction_skipped instead of abstraction_complete.
    seed_maintenance_fixture(backend)
    ingest_fresh_fixtures(backend)
    print(f"seed complete: 3 bulk fixtures + 1 deferred-abstract fixture in {VAULT_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
