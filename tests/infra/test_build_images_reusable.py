"""Structural gate for the reusable container-image build workflow.

Locks the shape of ``.github/workflows/build-images.yml`` — the
``workflow_call`` reusable workflow that builds the SAGE and CAS BFF images,
runs the container smoke tests on the non-push arm, and (when invoked with push
enabled) pushes the immutable ``{version}-{short-sha}`` tag to a tenant's
container registry. Both the CI workflow (build + smoke, push off) and the
deploy pipeline (build + push to the selected tenant's registry) call it, so its
input/output contract is the single source the two callers share. The push
authenticates through the OIDC deploy identity (no stored secret) and stays
dormant until the registry coordinate is configured.

The deploy arm does not re-run the smoke tests. It is reached only for a commit
whose own CI run concluded green, and that run smoked the same build from the
same digest-pinned bases — so a third execution would re-prove a settled fact
against the clock.

These checks read the tracked workflow YAML only — no Actions runner or Azure
tooling — so they run in the ordinary Python test job. The deployment-profile
model this build path serves is recorded in CAS-ADR-042.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BUILD_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "build-images.yml"


def _load() -> dict:
    return yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    """Return the workflow trigger mapping.

    PyYAML parses the bare ``on:`` key as the boolean ``True`` under YAML 1.1
    truthy-token rules, so the trigger block is keyed by ``True`` rather than the
    string ``"on"``.
    """
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def test_reusable_workflow_exists_and_is_callable() -> None:
    """The reusable build workflow exists and is invocable via workflow_call."""
    assert BUILD_WORKFLOW.is_file(), ".github/workflows/build-images.yml missing"
    on = _on_block(_load())
    assert "workflow_call" in on, "build-images.yml must be a reusable workflow_call workflow"


def test_declares_push_and_environment_inputs() -> None:
    """The call contract takes a push toggle and a target environment name — the
    two knobs that distinguish the CI caller (build only) from the deploy caller
    (build + push to the tenant's registry).
    """
    call = _on_block(_load()).get("workflow_call") or {}
    inputs = call.get("inputs") or {}
    assert "push" in inputs, "workflow_call must accept a `push` input"
    assert "environment" in inputs, "workflow_call must accept an `environment` input"


def test_outputs_image_tag() -> None:
    """The workflow exports the resolved image tag and build version the deploy
    pipeline pins the provisioned containers to.
    """
    call = _on_block(_load()).get("workflow_call") or {}
    outputs = call.get("outputs") or {}
    assert "image_tag" in outputs, "workflow_call must output image_tag"
    assert "sage_build_version" in outputs, "workflow_call must output sage_build_version"


def test_builds_both_images_and_bakes_version() -> None:
    """Both images are built, with the release version baked into each.

    Whether the container smoke tests run, and on which arm, is asserted by
    ``test_container_smoke_runs_only_on_the_non_push_arm`` below -- a raw-text
    search cannot tell a step that runs from a step that merely exists.
    """
    raw = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert len(re.findall(r"docker buildx build", raw)) >= 2, (
        "must build both the SAGE and BFF images"
    )
    assert "-f Dockerfile.bff" in raw, "must build the BFF image from Dockerfile.bff"
    assert "--build-arg" in raw and "SAGE_BUILD_VERSION=" in raw, (
        "the image version must be baked via the SAGE_BUILD_VERSION build arg"
    )
    assert "--build-arg" in raw and "SAGE_BUILD_IDENTITY=" in raw, (
        "the image build identity must be baked via the SAGE_BUILD_IDENTITY build arg"
    )
    assert "build_info" in raw, "the version must be single-sourced from sage.build_info"


def test_push_is_oidc_and_dormant_until_registry_set() -> None:
    """The push authenticates via OIDC (no stored secret) and is gated on the
    registry login-server coordinate, so it stays dormant until configured.
    """
    raw = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert "azure/login" in raw, "the push must authenticate via the OIDC azure/login action"
    assert "ACR_LOGIN_SERVER" in raw, "the push must be gated on the ACR_LOGIN_SERVER coordinate"
    lowered = raw.lower()
    for forbidden in ("client-secret", "client_secret", "azure_client_secret", "creds:"):
        assert forbidden not in lowered, (
            f"the deploy identity must be OIDC-federated, not a stored secret ({forbidden!r})"
        )


# ---------------------------------------------------------------------------
# Step-level structure
#
# The assertions above read the workflow as raw text, which cannot distinguish
# "the step exists" from "the step runs on this arm". The gates below parse the
# step list instead, so a conditional step is testable as conditional.
# ---------------------------------------------------------------------------

_SMOKE_RE: Final[re.Pattern[str]] = re.compile(r"pytest\b.*tests/deploy/")
_NEGATED_PUSH_RE: Final[re.Pattern[str]] = re.compile(r"!\s*inputs\.push|inputs\.push\s*==\s*false")


def _steps() -> list[dict]:
    """The build job's step list, in declaration order."""
    build = (_load().get("jobs") or {}).get("build") or {}
    return list(build.get("steps") or [])


def _run_of(step: dict) -> str:
    return str(step.get("run") or "")


def _uses_of(step: dict) -> str:
    return str(step.get("uses") or "")


def _is_buildx_step(step: dict) -> bool:
    return "docker buildx build" in _run_of(step)


def _first_index(steps: list[dict], predicate) -> int | None:
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    return None


def test_gha_cache_flags_require_exposed_runtime_credentials() -> None:
    """The GitHub Actions cache backend needs credentials a `run:` step lacks.

    ``type=gha`` authenticates with ``ACTIONS_RUNTIME_TOKEN`` and
    ``ACTIONS_RESULTS_URL``, which the forge injects into JavaScript actions
    only. A bare ``run: docker buildx build`` never receives them, so the cache
    exporter produces nothing and reports no error -- the flags read as
    protection while caching silently does not happen. Whatever exposes those
    variables must therefore precede the first build step.
    """
    steps = _steps()
    build_indices = [i for i, step in enumerate(steps) if _is_buildx_step(step)]
    assert len(build_indices) == 2, f"expected two image build steps, found {len(build_indices)}"

    # Non-vacuity: without this, deleting the cache flags *and* the exporter
    # would satisfy the implication below while abandoning caching entirely.
    for index in build_indices:
        assert "type=gha" in _run_of(steps[index]), (
            f"build step {index} no longer uses the GitHub Actions cache backend; "
            "if that is deliberate, this gate should be removed rather than left "
            "asserting a protection that is gone"
        )

    exporter = _first_index(steps, lambda step: "ghaction-github-runtime" in _uses_of(step))
    assert exporter is not None, (
        "the workflow uses `--cache-to type=gha` from a `run:` step but never "
        "exposes the Actions runtime credentials; add the "
        "crazy-max/ghaction-github-runtime action ahead of the builds"
    )
    assert exporter < build_indices[0], (
        f"the runtime-credential export (step {exporter}) must precede the first "
        f"image build (step {build_indices[0]}); the build cannot use credentials "
        "exported after it"
    )
    assert not str(steps[exporter].get("if") or ""), (
        "the credential export is conditional, but the cache flags above are not. "
        "On any arm where that condition is false the build runs with an "
        "unauthenticated exporter -- which writes nothing, reads nothing, and "
        "reports no error, exactly the silent no-op this gate exists to catch"
    )


def test_container_smoke_runs_only_on_the_non_push_arm() -> None:
    """The smoke step exists, and is gated off the deploy arm.

    The deploy caller reaches this workflow only for a commit whose own CI run
    concluded green, and that run smoked the same build. Running the suite a
    third time on the push arm re-proves it against the clock.
    """
    smoke = [step for step in _steps() if _SMOKE_RE.search(_run_of(step))]
    assert len(smoke) == 1, (
        f"expected exactly one container smoke step, found {len(smoke)}; the "
        "regression gate must not be dropped, only scoped"
    )

    condition = str(smoke[0].get("if") or "")
    assert condition, "the container smoke step is unconditional; it must be gated off the push arm"
    assert _NEGATED_PUSH_RE.search(condition), (
        f"the smoke step's condition ({condition!r}) does not negate `inputs.push`"
    )


def test_images_are_pushed_from_the_build_step() -> None:
    """Each build writes its own output; nothing re-exports the image afterwards.

    Loading a built image into the local daemon so a later step can push it
    costs a full serialise-and-reimport of every layer, twice over. The build
    can write straight to the registry instead, and does so on the arm that
    has one.
    """
    steps = _steps()
    for index, step in enumerate(steps):
        assert "docker push" not in _run_of(step), (
            f"step {index} pushes an image separately; the build step should "
            "write to the registry directly"
        )

    build_steps = [step for step in steps if _is_buildx_step(step)]
    assert len(build_steps) == 2, "expected two image build steps"
    for step in build_steps:
        run = _run_of(step)
        assert "--push" in run and "--load" in run, (
            "each build step must select its own output mode -- `--push` on the "
            "registry arm, `--load` where the smoke tests need a local image"
        )
        assert "PUSH_TO_REGISTRY" in run, "the output mode must branch on the resolved push flag"
        fallback = re.search(r"^\s*else\s*$", run, re.MULTILINE)
        assert fallback is not None, (
            "the output mode has no fallback branch; a caller with no registry "
            "configured would get no image at all"
        )
        assert run.index("--push") < fallback.start() < run.index("--load"), (
            "the output modes are inverted: `--push` belongs in the branch the "
            "push flag selects and `--load` in the fallback. Swapped, this pushes "
            "CI's throwaway images to the tenant registry and leaves the deploy "
            "with no artifact -- while satisfying a check that only asks whether "
            "both flags appear"
        )

    joined = "\n".join(_run_of(step) for step in build_steps)
    assert "IMAGE_TAG" in joined, "the immutable {version}-{short-sha} tag must be applied"
    assert ":latest" in joined, (
        "the moving `latest` tag is part of the published contract and must "
        "survive the move to a direct push"
    )


def test_registry_login_precedes_the_image_builds() -> None:
    """A build that writes to the registry must be authenticated before it runs."""
    steps = _steps()
    login = _first_index(steps, lambda step: "azure/login" in _uses_of(step))
    assert login is not None, "the workflow no longer authenticates to the registry"

    first_build = _first_index(steps, _is_buildx_step)
    assert first_build is not None, "no image build step found"
    assert login < first_build, (
        f"Azure login (step {login}) must precede the first image build "
        f"(step {first_build}); a build that pushes cannot authenticate afterwards"
    )

    # `azure/login` yields a management-plane token, not a registry one. Dropping
    # the exchange below leaves the push unauthenticated while every other
    # assertion in this test still holds.
    acr_login = _first_index(steps, lambda step: "az acr login" in _run_of(step))
    assert acr_login is not None, (
        "nothing exchanges the workload-identity token for a registry token"
    )
    assert acr_login < first_build, (
        f"the registry token exchange (step {acr_login}) must precede the first "
        f"image build (step {first_build})"
    )


def _normalise_expr(raw: str) -> str:
    """Strip expression delimiters and collapse whitespace for comparison."""
    return " ".join(raw.replace("${{", " ").replace("}}", " ").split())


def test_the_registry_gate_is_expressed_identically_everywhere() -> None:
    """One decision, spelled out at four sites, must stay one decision.

    Whether this run pushes is decided by the same conjunction in four places:
    the two login steps' conditions and the resolved flag on each build step.
    They are independent copies, so they can drift -- and drift is worse than a
    hard failure in one direction. A build whose flag says no while the caller
    expects a push succeeds, loads its images into a daemon nobody reads, and
    reports green having produced no artifact; the miss surfaces later, at the
    app tier, as an image that cannot be pulled.
    """
    conditions: dict[str, str] = {}
    for index, step in enumerate(_steps()):
        condition = str(step.get("if") or "")
        if "ACR_LOGIN_SERVER" in condition:
            conditions[f"step {index} if"] = _normalise_expr(condition)
        env = step.get("env") or {}
        if "PUSH_TO_REGISTRY" in env:
            conditions[f"step {index} env.PUSH_TO_REGISTRY"] = _normalise_expr(
                str(env["PUSH_TO_REGISTRY"])
            )

    assert len(conditions) == 4, (
        "expected the registry gate at four sites -- two login conditions and "
        f"one resolved flag per build step -- found {sorted(conditions)}"
    )
    assert len(set(conditions.values())) == 1, (
        f"the registry gate has drifted between its copies: {conditions}"
    )
