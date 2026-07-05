"""API smoke sweep driver.

Runs the ordered check catalog from checks.py against one deployment,
enforces the structural coverage gate (every OpenAPI operation and every
live MCP tool must be claimed by a check or carry a written justification),
and writes the coverage-matrix report.

Usage:
    python driver.py --profile local --sage-url http://127.0.0.1:8765 \
        --report reports/local.md
    python driver.py --profile cloud --sage-url https://<edge> \
        --bff-url https://<bff> --token-cmd "az account get-access-token ..." \
        --report reports/cloud.md

Probe self-tests (anti-coincidental-pass) run between vault creation and
first ingest: each probe feeds the assertion machinery a case that MUST
fail; a probe that passes aborts the sweep, because it means the harness
can no longer detect that class of breakage.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

import yaml
from checks import (
    GROUPS,
    Ctx,
    group1_vault,
    group2_ingest,
    stage_retrieval_assertions,
    wait_for_health,
)
from client import CheckFailure, Recorder, SmokeClient

SMOKE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SMOKE_DIR.parent
OPENAPI_PATH = REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"

# Operations declared in the OpenAPI spec with no implementation behind
# them; the repository's own parity gate allowlists the same divergence.
SPEC_ONLY_OPS: dict[tuple[str, str], str] = {
    (
        "get",
        "/sage_vaults/{vault_id}/documents/{document_id}/editors",
    ): "spec-declared, unimplemented (allowlisted by test_openapi_conformance);"
    " live absence asserted by L-076",
    (
        "put",
        "/sage_vaults/{vault_id}/documents/{document_id}/editors",
    ): "spec-declared, unimplemented (allowlisted by test_openapi_conformance);"
    " live absence asserted by L-076",
}

# Surfaces outside the SAGE OpenAPI document that the sweep still owns.
EXTRA_OPS: list[tuple[str, str]] = [
    ("get", "/health"),
    ("post", "/app/scan"),
    ("post", "/app/ingest"),
    ("get", "/app/auth/login"),
    ("get", "/app/auth/callback"),
    ("get", "/app/auth/me"),
    ("post", "/app/auth/logout"),
]

UNEXERCISABLE_OPS: dict[tuple[str, str], str] = {
    (
        "get",
        "/app/auth/callback",
    ): "completing the code exchange needs an interactive identity-provider"
    " login; covered by the session-store unit suite and the BFF deploy smoke",
}


def load_openapi_ops() -> list[tuple[str, str]]:
    spec = yaml.safe_load(OPENAPI_PATH.read_text())
    ops: list[tuple[str, str]] = []
    for path, item in spec.get("paths", {}).items():
        for method in item:
            if method in ("get", "post", "put", "delete", "patch"):
                ops.append((method, path))
    return ops


def coverage_gate(rec: Recorder, mcp_tools: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Return (uncovered REST ops, uncovered MCP tools). Empty means covered."""
    claimed = rec.claimed_rest()
    uncovered_rest = []
    for op in load_openapi_ops() + EXTRA_OPS:
        if op in claimed or op in SPEC_ONLY_OPS or op in UNEXERCISABLE_OPS:
            continue
        uncovered_rest.append(f"{op[0].upper()} {op[1]}")
    claimed_tools = rec.claimed_tools()
    uncovered_tools = []
    for mount, tools in mcp_tools.items():
        for tool in tools:
            if tool not in claimed_tools:
                uncovered_tools.append(f"{tool} ({mount})")
    return sorted(set(uncovered_rest)), sorted(set(uncovered_tools))


def run_probes(c: SmokeClient, ctx: Ctx) -> list[str]:
    """Anti-coincidental-pass self-tests. Returns a list of probe failures
    (probes that PASSED when they must FAIL); empty list means the harness
    can still detect every class of breakage.
    """
    print("— probe self-tests (each must FAIL)")
    probe_rec = Recorder()
    bad: list[str] = []

    def p01() -> str:
        c.expect_status(c.req("GET", ctx.v("/documents/ffffffff_nonexistent_probe")), 200)
        return "read a document that does not exist"

    probe_rec.run(
        "P-01", "nonexistent document read", p01, surface="rest", db_class="relational", rw="read"
    )

    def p02() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/discover"),
                json={"mode": "semantic", "query": "heliograph dome shutter", "limit": 5},
            ),
            200,
        )
        hits = body["results"]
        if not hits:
            raise CheckFailure("semantic search over an empty vault returned nothing")
        return "found results in an empty vault"

    probe_rec.run(
        "P-02", "semantic assertion on empty vault", p02, surface="rest", db_class="both", rw="read"
    )

    def p03() -> str:
        c.sse("GET", "/health")
        return "consumed a JSON endpoint as an event stream"

    probe_rec.run(
        "P-03",
        "SSE consumer on non-stream endpoint",
        p03,
        surface="rest",
        db_class="none",
        rw="read",
    )

    def p04() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/metadata"),
                json={
                    "items": [
                        {
                            "document_id": "ffffffff_nonexistent_probe",
                            "tags": {"add": ["x"]},
                        }
                    ]
                },
            ),
            200,
        )
        c.expect_batch(body, success=1)
        return "accepted a batch whose only item errored"

    probe_rec.run(
        "P-04",
        "per-item error envelope detection",
        p04,
        surface="rest",
        db_class="relational",
        rw="write",
    )

    for r in probe_rec.results:
        if r.status == "PASS":
            bad.append(f"{r.check_id} unexpectedly PASSED: {r.detail}")

    # P-05: the coverage gate itself must flag a missing claim.
    ghost = Recorder()
    uncovered, _ = coverage_gate(ghost, {})
    if not uncovered:
        bad.append("P-05: coverage gate reported full coverage for an empty catalog")
    else:
        print(f"  [ok] P-05 gate detects gaps ({len(uncovered)} unclaimed ops on empty catalog)")
    return bad


def render_vault_config(vault_id: str, storage_root: str, brain_root: str) -> dict:
    text = (SMOKE_DIR / "vault_config_template.yaml").read_text()
    text = (
        text.replace("__VAULT_ID__", vault_id)
        .replace("__STORAGE_ROOT__", storage_root)
        .replace("__BRAIN_ROOT__", brain_root)
    )
    return yaml.safe_load(text)


def write_report(
    path: Path,
    *,
    profile: str,
    sage_url: str,
    rec: Recorder,
    ctx: Ctx,
    uncovered_rest: list[str],
    uncovered_tools: list[str],
    probe_failures: list[str],
) -> None:
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    counts = rec.counts()
    lines = [
        "# API smoke sweep report",
        "",
        f"- profile: **{profile}**  |  target: `{sage_url}`  |  run at: {now}",
        f"- vault: `{ctx.vault_id}`  |  documents ingested: "
        f"{len([k for k in ctx.ids if not k.startswith('_')])}",
        f"- results: {counts}",
        f"- probe self-tests: {'all failed as designed' if not probe_failures else probe_failures}",
        "",
        "## Checks",
        "",
        "| id | title | surface | db | r/w | result | detail |",
        "|---|---|---|---|---|---|---|",
    ]
    home = str(Path.home())
    for r in rec.results:
        # Check details can echo server-returned absolute paths; redact the
        # home prefix so the committed artifact carries no personal paths.
        detail = r.detail.replace(home, "~").replace("|", "\\|").replace("\n", " ")[:220]
        lines.append(
            f"| {r.check_id} | {r.title} | {r.surface} | {r.db_class} | {r.rw}"
            f" | {r.status} | {detail} |"
        )
    lines += ["", "## Coverage", ""]
    claimed = rec.claimed_rest()
    lines.append("### REST operations")
    lines.append("")
    lines.append("| op | covered by |")
    lines.append("|---|---|")
    per_op: dict[tuple[str, str], list[str]] = {}
    for r in rec.results:
        for op in r.covers_rest:
            per_op.setdefault(op, []).append(r.check_id)
    for op in load_openapi_ops() + EXTRA_OPS:
        key = f"{op[0].upper()} {op[1]}"
        if op in per_op:
            lines.append(f"| `{key}` | {', '.join(per_op[op])} |")
        elif op in SPEC_ONLY_OPS:
            lines.append(f"| `{key}` | excluded: {SPEC_ONLY_OPS[op]} |")
        elif op in UNEXERCISABLE_OPS:
            lines.append(f"| `{key}` | excluded: {UNEXERCISABLE_OPS[op]} |")
        else:
            lines.append(f"| `{key}` | **UNCOVERED** |")
    lines += ["", "### MCP tools", "", "| tool | mount | covered by |", "|---|---|---|"]
    per_tool: dict[str, list[str]] = {}
    for r in rec.results:
        for t in r.covers_tools:
            per_tool.setdefault(t, []).append(r.check_id)
    for mount, tools in ctx.mcp_tools.items():
        for tool in tools:
            covered = ", ".join(per_tool.get(tool, [])) or "**UNCOVERED**"
            lines.append(f"| `{tool}` | {mount} | {covered} |")
    if uncovered_rest or uncovered_tools:
        lines += ["", "## GATE FAILURES", ""]
        lines += [f"- `{u}`" for u in uncovered_rest + uncovered_tools]
    _ = claimed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CAS API smoke sweep")
    parser.add_argument("--profile", choices=("local", "cloud"), required=True)
    parser.add_argument("--sage-url", required=True)
    parser.add_argument(
        "--bff-url",
        default=None,
        help="Cloud only: standalone BFF base URL (local serves /app itself).",
    )
    parser.add_argument("--vault-id", default="cas_smoke")
    parser.add_argument(
        "--storage-root",
        default=None,
        help="Local only: server-visible scratch root for the smoke vault.",
    )
    parser.add_argument(
        "--expected-build", default=None, help="Substring the MCP build handshake must contain."
    )
    parser.add_argument(
        "--token-cmd", default=None, help="Cloud only: shell command printing a bearer token."
    )
    parser.add_argument("--report", default=str(SMOKE_DIR / "reports" / "run.md"))
    parser.add_argument("--skip-probes", action="store_true")
    args = parser.parse_args()

    if args.profile == "local" and not args.storage_root:
        parser.error("--storage-root is required under the local profile")

    def token_provider() -> str | None:
        if not args.token_cmd:
            return None
        # The command is the operator's own --token-cmd argument, executed
        # on the operator's machine; a shell is the point (pipes to jq etc.).
        return subprocess.run(  # noqa: S602
            args.token_cmd, shell=True, capture_output=True, text=True, check=True
        ).stdout.strip()

    storage_root = Path(args.storage_root).resolve() if args.storage_root else None
    vault_storage = (
        str(storage_root / args.vault_id / "sources")
        if storage_root
        else (f"/var/lib/sage/vaults/{args.vault_id}/sources")
    )
    vault_brain = (
        str(storage_root / args.vault_id / "brain")
        if storage_root
        else (f"/var/lib/sage/vaults/{args.vault_id}/brain")
    )
    aux_id = f"{args.vault_id}_aux"
    ctx = Ctx(
        profile=args.profile,
        vault_id=args.vault_id,
        aux_vault_id=aux_id,
        fixtures=SMOKE_DIR / "fixtures",
        vault_config=render_vault_config(args.vault_id, vault_storage, vault_brain),
        aux_vault_config=render_vault_config(
            aux_id,
            vault_storage.replace(args.vault_id, aux_id),
            vault_brain.replace(args.vault_id, aux_id),
        ),
        expected_build=args.expected_build,
    )

    c = SmokeClient(args.sage_url, token_provider=token_provider)
    bff = SmokeClient(args.bff_url, token_provider=token_provider) if args.bff_url else c
    rec = Recorder()

    print(f"waiting for {args.sage_url}/health ...")
    wait_for_health(c)

    probe_failures: list[str] = []
    for group in GROUPS:
        if group.__name__ == "group10_bff":
            group(bff, rec, ctx)
        else:
            group(c, rec, ctx)
        if group is group1_vault and not args.skip_probes:
            probe_failures = run_probes(c, ctx)
            if probe_failures:
                print("PROBE FAILURE — harness cannot detect breakage; aborting:")
                for p in probe_failures:
                    print(f"  {p}")
                return 2
        if group is group2_ingest and args.profile == "local":
            stage_retrieval_assertions(ctx, Path(vault_storage))

    uncovered_rest, uncovered_tools = coverage_gate(rec, ctx.mcp_tools)
    write_report(
        Path(args.report),
        profile=args.profile,
        sage_url=args.sage_url,
        rec=rec,
        ctx=ctx,
        uncovered_rest=uncovered_rest,
        uncovered_tools=uncovered_tools,
        probe_failures=probe_failures if not args.skip_probes else ["skipped"],
    )

    counts = rec.counts()
    failed = counts.get("FAIL", 0) + counts.get("ERROR", 0)
    if uncovered_rest or uncovered_tools:
        print(f"COVERAGE GATE FAILED: {uncovered_rest + uncovered_tools}")
        return 3
    print(f"sweep complete: {counts}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
