"""Container boot smoke for the CAS BFF image (BSMK-001..004).

Opt-in Docker integration gate: skipped unless ``SAGE_TEST_DOCKER=1`` and the
``docker`` CLI is on PATH -- the same gate the SAGE infra-image smoke uses.
Building the image is slow and network-bound, so these never run in the default
suite; the structural assertions in ``test_bff_container_image`` are the
always-on guard.

The entrypoint is ``python -m app.backend``; checks that are not the server
(``id``, an ad-hoc ``python -c``) override it with ``--entrypoint``. The default
entrypoint+CMD is exercised by the boot and SPA-serving tests.
"""

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "Dockerfile.bff"
_IMAGE = "cas-bff:pytest-smoke"
#: Pre-built image supplied by CI (SAGE_TEST_BFF_IMAGE); when absent the
#: fixture builds locally with the sentinel version below.
_PREBUILT_IMAGE: str | None = os.environ.get("SAGE_TEST_BFF_IMAGE")
#: Version baked into the image. CI sets SAGE_TEST_BFF_IMAGE_VERSION to the
#: real release; local dev uses an unmistakable sentinel distinct from any release.
_VERSION: str = os.environ.get("SAGE_TEST_BFF_IMAGE_VERSION") or "9.9.9"
#: Production target arch. On Apple Silicon this builds under emulation; set
#: SAGE_TEST_DOCKER_PLATFORM="" to build a native image instead.
_PLATFORM = os.environ.get("SAGE_TEST_DOCKER_PLATFORM", "linux/amd64")

#: Exits 1 iff mlx is importable, so a leaked Apple-Silicon dep fails BSMK-004.
_MLX_PROBE = "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('mlx') else 0)"

pytestmark = pytest.mark.skipif(
    os.environ.get("SAGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="container smoke is opt-in: set SAGE_TEST_DOCKER=1 with docker on PATH",
)


def _platform_args() -> list[str]:
    return ["--platform", _PLATFORM] if _PLATFORM else []


@pytest.fixture(scope="module")
def image() -> str:
    """Return the image under test; build it with the sentinel version if not pre-built."""
    if _PREBUILT_IMAGE:
        return _PREBUILT_IMAGE
    subprocess.run(
        [
            "docker",
            "build",
            *_platform_args(),
            "-f",
            str(_DOCKERFILE),
            "--build-arg",
            f"SAGE_BUILD_VERSION={_VERSION}",
            "-t",
            _IMAGE,
            str(_REPO_ROOT),
        ],
        check=True,
    )
    return _IMAGE


def _run_detached(image: str, name: str, host_port: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            *_platform_args(),
            "-p",
            f"{host_port}:8001",
            image,
        ],
        check=True,
    )


def _get(url: str) -> tuple[int, str, str] | None:
    """Return (status, body, content-type) or None when unreachable."""
    with contextlib.suppress(Exception):
        # Fixed localhost http URL; scheme is not user-controlled.
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
            return (
                resp.status,
                resp.read().decode("utf-8", "replace"),
                resp.headers.get("content-type", ""),
            )
    return None


def _await_ready(port: str) -> bool:
    for _ in range(60):
        got = _get(f"http://127.0.0.1:{port}/health")
        if got is not None and got[0] == 200:
            return True
        time.sleep(2)
    return False


def test_bsmk_003_runs_as_non_root(image: str) -> None:
    out = subprocess.run(
        ["docker", "run", "--rm", *_platform_args(), "--entrypoint", "id", image, "-u"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # A forgotten USER directive would report uid 0.
    assert out != "0", f"container runs as root (uid {out})"


def test_bsmk_004_mlx_absent(image: str) -> None:
    # Non-zero exit -> CalledProcessError -> fail.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *_platform_args(),
            "--entrypoint",
            "python",
            image,
            "-c",
            _MLX_PROBE,
        ],
        check=True,
    )


def test_bsmk_001_boots_health_green_version_baked(image: str) -> None:
    port = "18098"
    name = "cas-bff-pytest-smoke"
    url = f"http://127.0.0.1:{port}/health"
    _run_detached(image, name, port)
    try:
        body = None
        for _ in range(60):
            got = _get(url)
            if got is not None and got[0] == 200:
                body = json.loads(got[1])
                break
            time.sleep(2)
        # Host reachability via the published port proves the non-loopback
        # (0.0.0.0) bind; the exact body proves health-green and the baked stamp.
        assert body is not None, "/health never became reachable on the published port"
        assert body == {"status": "ok", "version": _VERSION}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_bsmk_002_spa_index_and_deep_link_served(image: str) -> None:
    port = "18097"
    name = "cas-bff-pytest-spa"
    _run_detached(image, name, port)
    try:
        assert _await_ready(port), "the BFF never became reachable"

        # Root serves the built SPA shell (html, not the JSON health envelope).
        root = _get(f"http://127.0.0.1:{port}/")
        assert root is not None and root[0] == 200, "GET / did not return 200"
        _, html, ctype = root
        assert "text/html" in ctype, f"index content-type is not html: {ctype}"
        assert '<div id="root">' in html, "served index is not the SPA shell"
        # A bundled asset reference proves this is the Vite build output, not a
        # raw or stub index that happened to answer 200.
        assert "/assets/" in html, "served index lacks a built /assets reference"

        # A client-side deep link falls back to the same SPA shell (the catch-all
        # route), rather than 404-ing as a bare static mount would.
        deep = _get(f"http://127.0.0.1:{port}/graph/deadbeef")
        assert deep is not None and deep[0] == 200, "deep link did not return 200 (no SPA fallback)"
        assert '<div id="root">' in deep[1], "deep link did not fall back to the SPA shell"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
