"""Structural gate for the SharePoint vault-source live-validation CI harness.

Locks the shape of ``.github/workflows/sharepoint-validate.yml`` as the authorized
execution path for the SharePoint vault-source live validation. The authenticated
SAGE edge is machine-to-machine (CAS-ADR-043): only the OIDC-federated deploy
identity holds the ``Sage.Access`` grant, so the validation driver cannot run from a
workstation. This manually-dispatched workflow gives it a home — it mints an edge
token as the deploy identity, runs the driver's pre-restart phase against the
deployed disposable ``cloud_validation`` vault, rolls the SAGE revision, waits for
liveness, then
runs the post-restart phase, proving the vault sources survive a restart with no
local copy.

The driver itself (``deploy/sharepoint_validate.py``) is gated separately by
``tests/deploy/test_sharepoint_validate.py``. These checks read the tracked workflow
YAML only; they need no GitHub or Azure tooling and run in the ordinary Python test
job. Command-present assertions anchor on the command line that does the work, never
on prose, so a paraphrasing comment cannot satisfy a stage gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "sharepoint-validate.yml"

# A subscription / tenant / client id is a GUID. None of these identity coordinates
# may be hardcoded into the harness — they arrive as environment-scoped GitHub
# variables at dispatch time.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    """The workflow trigger mapping (PyYAML keys bare ``on:`` as ``True``)."""
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def _job_environment(job: dict) -> str | None:
    """The environment a job binds, whether given as a string or a mapping."""
    env = job.get("environment")
    if isinstance(env, str):
        return env
    if isinstance(env, dict):
        return env.get("name")
    return None


def _job_run_text(job: dict) -> str:
    """All ``run:`` script of a job, joined in step order."""
    steps = job.get("steps") or []
    return "\n".join(s.get("run", "") for s in steps if isinstance(s, dict))


def _uncommented_run_text(job: dict) -> str:
    """``_job_run_text`` with shell ``#`` comment lines removed, so a command named
    only in a comment cannot be mistaken for a real invocation by a token-mint gate.
    """
    return "\n".join(
        line for line in _job_run_text(job).splitlines() if not line.lstrip().startswith("#")
    )


def _validation_job(workflow: dict) -> dict:
    """The job that runs the validator driver — it invokes ``sharepoint_validate.py``."""
    for job in (workflow.get("jobs") or {}).values():
        if "sharepoint_validate.py" in _uncommented_run_text(job):
            return job
    raise AssertionError("no job runs deploy/sharepoint_validate.py")


def test_dispatch_only_never_on_push() -> None:
    """The harness is manually dispatched only — never on ``push`` or ``pull_request``.

    The job mutates: it ingests a probe document and restarts the container. Wiring it
    to every push/PR would ingest into and recycle the deployed tenant on routine CI,
    so the trigger set must be ``workflow_dispatch`` alone.
    """
    on = _on_block(_load())
    assert "workflow_dispatch" in on, "harness must be manually dispatched"
    assert "push" not in on, "harness must not run on push (it mutates the deployed vault)"
    assert "pull_request" not in on, (
        "harness must not run on pull_request (it mutates the deployed vault)"
    )


def test_environment_input_bound() -> None:
    """The dispatch selects the tenant's Environment by input, and the job binds it.

    The per-tenant GitHub Environment carries that tenant's federated deploy identity
    and parameter set; binding ``inputs.environment`` (not a literal) keeps the harness
    tenant-agnostic, the same posture as the deploy workflow.
    """
    workflow = _load()
    dispatch = _on_block(workflow).get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") or {}
    assert "environment" in inputs, "workflow_dispatch must take an `environment` input"
    env = _job_environment(_validation_job(workflow))
    assert env is not None, "the validation job must bind a GitHub Environment"
    assert "inputs.environment" in env, (
        f"the validation environment must be the dispatch-selected tenant, not a literal: {env!r}"
    )


def test_oidc_posture() -> None:
    """The harness requests the OIDC token, reads its identity from a variable, and
    carries no stored client secret.
    """
    workflow = _load()
    raw = WORKFLOW.read_text(encoding="utf-8")
    permissions = workflow.get("permissions") or {}
    assert permissions.get("id-token") == "write", "harness must request id-token: write for OIDC"
    assert permissions.get("contents") == "read", "harness needs only contents: read"
    lowered = raw.lower()
    for forbidden in ("client-secret", "client_secret", "azure_client_secret", "creds:"):
        assert forbidden not in lowered, (
            f"deploy identity must be OIDC-federated, not a stored secret ({forbidden!r})"
        )
    assert "azure/login@" in raw, "harness must authenticate with azure/login"
    assert "vars.AZURE_CLIENT_ID" in raw, (
        "harness must read the deploy identity from a GitHub variable"
    )


def test_no_hardcoded_identity_guid() -> None:
    """No subscription/tenant/client GUID is baked into the harness."""
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert not _GUID_RE.search(raw), "no identity GUID may be hardcoded in the harness"


def test_all_token_mints_are_v2_scoped() -> None:
    """Every bearer-token mint uses the v2 *scope* endpoint (``--scope
    <audience>/.default``), never the v1 ``--resource`` endpoint.

    ``--resource`` always returns a token whose issuer is ``sts.windows.net``
    (``ver=1.0``); the APIM ``validate-jwt`` policy and the SAGE backend both expect a
    ``…/v2.0`` issuer, so a v1 token is rejected with 401 at the edge before the
    request reaches the backend. The harness mints once per phase, so *both* mints must
    be v2.
    """
    runs = _uncommented_run_text(_validation_job(_load()))
    # Strip shell comments (above), then collapse line-continuations so the flag and its
    # audience read as one logical command line — a comment naming the mint command must
    # not be counted as an invocation.
    collapsed = re.sub(r"\s+", " ", runs.replace("\\\n", " "))
    mints = re.findall(r"az account get-access-token.*?\)", collapsed)
    assert mints, "the harness must mint a bearer token for the SAGE audience"
    for mint in mints:
        assert '--scope "$SAGE_AUDIENCE/.default"' in mint, (
            "every token mint must use the v2 scope endpoint (--scope <audience>/.default), "
            f"whose issuer matches APIM and the SAGE backend; got: {mint!r}"
        )
        assert "--resource" not in mint, (
            "no token mint may use --resource (the v1 endpoint; it always returns "
            f"iss=sts.windows.net, rejected at the edge); got: {mint!r}"
        )


def test_uncommented_run_text_strips_comment_lines() -> None:
    """A command named only in a shell comment is absent from the scanned text, so a
    comment cannot be mistaken for a real mint invocation by the gate above.
    """
    job = {
        "steps": [
            {
                "run": (
                    "# mint with az account get-access-token --scope ... (v2), not --resource\n"
                    'AUTH_TOKEN="$(az account get-access-token '
                    '--scope "$SAGE_AUDIENCE/.default" -o tsv)"\n'
                )
            }
        ]
    }
    text = _uncommented_run_text(job)
    assert "# mint with" not in text, "shell comment lines must be stripped"
    assert text.count("az account get-access-token") == 1, (
        "only the real invocation survives; the comment's phantom mention is gone"
    )
    assert "not --resource" not in text, "the comment's --resource mention must not survive"


def test_phases_run_around_restart_with_liveness_in_order() -> None:
    """The harness runs the pre-restart phase, rolls the SAGE revision, waits for
    liveness, then runs the post-restart phase — in that order.

    Order is load-bearing: the post-restart phase proves the sources survived a fresh
    container with a wiped ephemeral filesystem, so it must run *after* the restart and
    *after* the container is live again. Anchored on the command lines, not prose.
    """
    runs = _uncommented_run_text(_validation_job(_load()))
    anchors = [
        "--phase pre-restart",
        "az containerapp revision restart",
        "/health",
        "--phase post-restart",
    ]
    positions: list[int] = []
    for anchor in anchors:
        idx = runs.find(anchor)
        assert idx != -1, f"harness missing stage: {anchor!r}"
        positions.append(idx)
    assert positions == sorted(positions), (
        f"harness stages out of order: {list(zip(anchors, positions))}"
    )


def test_targets_disposable_validation_vault() -> None:
    """The harness targets ``cloud_validation``, never the canonical ``cas``.

    The driver ingests a probe and runs the source-file audit — it mutates the vault it
    runs against. It must therefore target the throwaway cloud validation vault, never
    the canonical corpus.
    """
    job = _validation_job(_load())
    env = job.get("env") or {}
    vault = str(env.get("SP_VALIDATE_VAULT_ID", ""))
    if not vault:
        match = re.search(r"--vault-id\s+(\S+)", _uncommented_run_text(job))
        vault = match.group(1) if match else ""
    assert vault == "cloud_validation", (
        "the harness must target the disposable `cloud_validation` vault (it mutates), "
        f"not {vault!r}"
    )


def test_phase_steps_fail_closed() -> None:
    """Each phase step ``set -euo pipefail`` so the driver's non-zero exit fails the
    job. The driver exits 1 on any failed check; without ``set -e`` a failed phase
    inside a multi-line step could be swallowed and the run report green.
    """
    job = _validation_job(_load())
    phase_steps = [
        step
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and "sharepoint_validate.py" in step.get("run", "")
    ]
    assert len(phase_steps) >= 2, "expected a pre-restart and a post-restart phase step"
    for step in phase_steps:
        assert "set -euo pipefail" in step["run"], (
            "each phase step must `set -euo pipefail` so the driver's non-zero exit fails the job"
        )


def test_phase_steps_declare_the_rewrite_expectation() -> None:
    """Each phase step passes ``--expect-rewritten yes``.

    Several of the driver's provenance assertions only bite where the store
    actually rewrote the retained copy, and the driver is deliberately
    binding-agnostic: it observes whether a rewrite happened and reports it,
    which means a tenant that stopped rewriting would leave every verdict green
    while most of that check's value quietly went away. The workflow is the only
    place that knows which binding is behind the edge, so the expectation lives
    here — and it has to be asserted here too, or dropping it costs nothing that
    anything notices. Anchored on the command text, not on prose.

    This is a drift guard, not a correctness proof. It establishes that the
    harness *asks* for the expectation; whether the tenant actually rewrites is a
    live fact, and the only thing that establishes it is a real dispatch whose
    ``check=provenance`` line reads ``rewritten=yes``. Read a green run here as
    "the flag is still wired", never as "the store still rewrites".
    """
    job = _validation_job(_load())
    phase_steps = [
        step
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and "sharepoint_validate.py" in step.get("run", "")
    ]
    assert len(phase_steps) >= 2, "expected a pre-restart and a post-restart phase step"
    for step in phase_steps:
        uncommented = "\n".join(
            line for line in step["run"].splitlines() if not line.lstrip().startswith("#")
        )
        assert "--expect-rewritten yes" in uncommented, (
            "each phase step must pass `--expect-rewritten yes`: the tenant store rewrites "
            "Office packages at rest, and without the expectation a store that stopped "
            "doing so would pass the run instead of failing it"
        )


def test_least_privilege_probe_deferral_documented() -> None:
    """The least-privilege probe is explicitly deferred with a note surfaced in the job
    summary, pointing at the manual runbook procedure.

    Automating it needs an un-granted site plus a throwaway vault config staged into
    the library, so it stays a manual runbook step; the harness records the deferral so
    the gap is auditable rather than silent.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "GITHUB_STEP_SUMMARY" in raw, "the deferral note must be surfaced in the job summary"
    assert re.search(r"least-privilege", raw, re.IGNORECASE), (
        "the harness must note the deferred least-privilege probe"
    )
    assert "sharepoint-vault-source.md" in raw, (
        "the deferral note must point at the runbook with the manual procedure"
    )
