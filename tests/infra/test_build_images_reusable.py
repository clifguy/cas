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
whose own CI run concluded green, and that run built from the same
digest-pinned bases and the same locked dependency sets, then smoked the result
— so a third execution would re-prove a settled fact against the clock. The
pins do not make the two builds byte-identical (the apt layer floats, and is
equal only while the layer cache serves it, which is at most the same UTC
day); they make the recipe equal, which is what the skip rests on.

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

# DOTALL so a command continued over several lines still matches.
_SMOKE_RE: Final[re.Pattern[str]] = re.compile(r"pytest\b.*tests/deploy/", re.DOTALL)
_NEGATED_PUSH_RE: Final[re.Pattern[str]] = re.compile(r"!\s*inputs\.push|inputs\.push\s*==\s*false")
_IMAGE_ENV_RE: Final[re.Pattern[str]] = re.compile(r"SAGE_TEST_(?:BFF_)?IMAGE\b")
_TRIAL_SCRIPT: Final[str] = "scripts/embedding_contention_trial.py"


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
    concluded green, and that run built from the same digest-pinned bases and
    the same locked dependency sets, then smoked the result. Running the suite a
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


def _image_reading_suites() -> set[str]:
    """The deploy suites that read a built image's tag from the environment."""
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests" / "deploy").rglob("test_*.py"))
        if _IMAGE_ENV_RE.search(path.read_text(encoding="utf-8"))
    }


def test_container_smoke_selects_exactly_the_image_reading_suites() -> None:
    """The smoke step runs the suites that need the image, and only those.

    Every other deploy suite runs in the ordinary test job, which collects the
    whole of ``tests/``; running it a second time beside the image adds minutes
    and proves nothing new. The selection is derived from the suites themselves
    -- a file that reads the image tag is one the smoke step must run -- so a new
    image-exercising suite cannot be left out by an explicit list that predates
    it.
    """
    expected = _image_reading_suites()
    assert expected, "no deploy suite reads a built image; the derivation found nothing"

    smoke = [step for step in _steps() if _SMOKE_RE.search(_run_of(step))]
    assert len(smoke) == 1, f"expected exactly one container smoke step, found {len(smoke)}"
    tokens = _run_of(smoke[0]).split()
    selected = {token for token in tokens if token.startswith("tests/")}

    assert "tests/deploy/" not in selected and "tests/deploy" not in selected, (
        "the smoke step collects the whole deploy tree, re-running every suite the "
        "test job already ran"
    )
    assert selected == expected, (
        f"the smoke step selects {sorted(selected)}, but the suites that read a "
        f"built image are {sorted(expected)}"
    )


def _workflow_call_inputs() -> dict:
    return (_on_block(_load()).get("workflow_call") or {}).get("inputs") or {}


def test_opt_in_inputs_are_declared_off_by_default() -> None:
    """Cache writes and the embedding trial are opt-in per caller.

    A caller that names neither gets a build that reads the cache, writes
    nothing to it, and skips the trial. Defaulting either to on would make every
    new caller pay for it silently.
    """
    inputs = _workflow_call_inputs()
    for name in ("write_cache", "embedding_trial"):
        assert name in inputs, f"workflow_call must accept a `{name}` input"
        assert inputs[name].get("type") == "boolean", f"`{name}` must be a boolean input"
        assert inputs[name].get("default") is False, f"`{name}` must default to false"


def test_embedding_trial_runs_only_when_the_caller_asks() -> None:
    """The trial and its evidence upload run on the non-push arm, on request.

    The trial is a performance measurement taking several minutes, not a
    correctness gate, so it belongs to the callers that choose it rather than to
    every build. Its artifact step must follow the same condition: uploading
    from a trial that never ran fails the job on `if-no-files-found`.
    """
    steps = _steps()
    trial = [step for step in steps if _TRIAL_SCRIPT in _run_of(step)]
    assert len(trial) == 1, f"expected exactly one embedding trial step, found {len(trial)}"
    upload = [step for step in steps if step.get("name") == "Retain embedding trial measurements"]
    assert len(upload) == 1, "expected exactly one trial evidence upload step"

    for label, step in (("trial", trial[0]), ("upload", upload[0])):
        condition = str(step.get("if") or "")
        assert _NEGATED_PUSH_RE.search(condition), (
            f"the {label} step's condition ({condition!r}) does not negate `inputs.push`"
        )
        assert "inputs.embedding_trial" in condition, (
            f"the {label} step's condition ({condition!r}) does not require the "
            "caller's `embedding_trial` request"
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
        assert re.search(r'"\$\{PUSH_TO_REGISTRY\}"\s*=\s*"true"', run), (
            "the push flag must be compared against the lowercase `true` a GitHub "
            "expression renders. Compared against anything else -- `True`, `1` -- the "
            "branch is never taken, the deploy arm silently loads instead of pushing, "
            "and every other assertion here still holds"
        )
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


def test_the_layer_cache_is_read_everywhere_and_written_only_on_request() -> None:
    """Every arm reads the layer cache; only the deploy and named callers write it.

    A pull-request ref's cache scope is readable by nothing else and dies with
    the branch, so writing one spends several GiB of a fixed repository budget
    to warm a scope no other run will ever open. Writing from every default-branch
    push is the same mistake more slowly: each push exports a fresh copy of every
    layer its changes and the daily apt key touched, and the repository budget
    fills with superseded blobs. The eviction that buys comes out of the entries
    every other run reads, and does it quietly, because LRU eviction reports
    nothing.

    Writing is therefore gated on the deploy arm and on a caller's explicit
    `write_cache` request, which one scheduled caller makes; it is not keyed to
    any event or ref. Reading is unconditional. The two halves are
    asserted by position rather than by presence: both flags appear in the file
    either way, and what distinguishes the correct arrangement from the
    inverted one is which side of the gate each sits on.
    """
    build_steps = [step for step in _steps() if _is_buildx_step(step)]
    assert len(build_steps) == 2, "expected two image build steps"

    write_conditions: list[str] = []
    for step in build_steps:
        run = _run_of(step)
        env = step.get("env") or {}

        assert "WRITE_CACHE" in env, "the build step resolves no cache-write gate"
        write_conditions.append(_normalise_expr(str(env["WRITE_CACHE"])))

        gate = run.find('if [ "${WRITE_CACHE}" = "true" ]')
        assert gate != -1, "the cache write is not gated on the resolved flag"

        # Containment, not order. `--cache-to` placed after the closing `fi`
        # satisfies "later than the gate" while every ref writes again, so the
        # flag has to fall inside the branch the gate opens.
        closing = run.find("\nfi", gate)
        assert closing != -1, "the cache-write gate is never closed"

        cache_from = run.find("--cache-from")
        cache_to = run.find("--cache-to")
        assert cache_from != -1 and cache_to != -1, "both cache flags must be present"
        assert cache_from < gate, (
            "`--cache-from` sits inside the write gate; reading must be "
            "unconditional or the arms that do not write also do not read, "
            "which is the whole benefit"
        )
        assert gate < cache_to < closing, (
            "`--cache-to` sits outside the write gate's body, so every ref "
            "writes its own copy of the cache"
        )

    assert len(set(write_conditions)) == 1, (
        f"the cache-write gate has drifted between the build steps: {write_conditions}"
    )

    # Equality is not enough: two steps can agree on a condition that is wrong
    # in the same way. A constant `true` writes from every ref, and an inverted
    # expression writes from exactly the refs nothing reads -- both identical
    # across the pair, both correctly positioned, both defeating the policy.
    condition = write_conditions[0]
    assert re.fullmatch(r"inputs\.push \|\| inputs\.write_cache", condition), (
        "the write gate must admit exactly the deploy dispatch and an explicit "
        f"`write_cache` request, got {condition!r}; a term keyed to an event or "
        "ref writes from every run that matches it"
    )


def test_the_sage_build_refreshes_its_apt_layer_daily_on_every_arm() -> None:
    """The SAGE build feeds the current UTC date to the image's apt refresh key.

    The layer cache keys the runtime apt layer on the base digest and the RUN
    text alone, so without a moving input it serves the packages it first
    resolved until the base digest changes -- holding back Debian security
    fixes that are already published. A daily key bounds that staleness to a
    day while two same-day builds of one commit still share the layer.

    The argument has to be unconditional: placed inside the push or cache-write
    branch, one arm would keep reusing the stale layer, and the deploy arm runs
    no vulnerability scan to notice.
    """
    sage_steps = [
        step for step in _steps() if _is_buildx_step(step) and "Dockerfile.bff" not in _run_of(step)
    ]
    assert len(sage_steps) == 1, "expected exactly one SAGE image build step"
    run = _run_of(sage_steps[0])

    refresh = run.find('--build-arg "APT_REFRESH=$(date -u +%F)"')
    assert refresh != -1, "the SAGE build passes no daily APT_REFRESH build arg"
    first_branch = run.find("\nif ")
    assert first_branch == -1 or refresh < first_branch, (
        "APT_REFRESH is passed inside a conditional branch, so some arm builds without it"
    )
