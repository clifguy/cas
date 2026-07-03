"""Structural gate for the reusable container-image build workflow.

Locks the shape of ``.github/workflows/build-images.yml`` — the
``workflow_call`` reusable workflow that builds the SAGE and CAS BFF images,
runs the container smoke tests, and (when invoked with push enabled) pushes the
immutable ``{version}-{short-sha}`` tag to a tenant's container registry. Both
the CI workflow (build + smoke, push off) and the deploy pipeline (build + push
to the selected tenant's registry) call it, so its input/output contract is the
single source the two callers share. The push authenticates through the OIDC
deploy identity (no stored secret) and stays dormant until the registry
coordinate is configured.

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


def test_builds_both_images_bakes_version_and_smokes() -> None:
    """Both images are built with the version baked in, and the container smoke
    tests run — the image surface the CI container job had is preserved.
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
    assert re.search(r"pytest\b.*tests/deploy/", raw), (
        "the container smoke tests (tests/deploy/) must run as part of the build"
    )


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
