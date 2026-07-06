"""Container boot smoke for the SAGE infra-server image (SMK-001..004).

Opt-in Docker integration gate: skipped unless ``SAGE_TEST_DOCKER=1`` and the
``docker`` CLI is on PATH (the same shape as the Postgres tests gated on
``SAGE_TEST_PG_DSN``). Building the image is slow and network-bound, so these
never run in the default suite.

The entrypoint is ``python -m sage``; commands that are not the server
(``id``, ad-hoc ``python -c``) override it with ``--entrypoint``. The default
entrypoint+CMD is exercised only by the boot test (SMK-001).
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
_IMAGE = "cas-sage:pytest-smoke"
#: Pre-built image supplied by CI (SAGE_TEST_IMAGE); when absent the fixture
#: builds locally with the sentinel version below.
_PREBUILT_IMAGE: str | None = os.environ.get("SAGE_TEST_IMAGE")
#: Version baked into the image. CI sets SAGE_TEST_IMAGE_VERSION to the real
#: release; local dev uses an unmistakable sentinel distinct from any release.
_VERSION: str = os.environ.get("SAGE_TEST_IMAGE_VERSION") or "9.9.9"
#: Build identity baked into the image. CI sets SAGE_TEST_IMAGE_IDENTITY to the
#: real short SHA; local dev uses an unmistakable sentinel.
_IDENTITY: str = os.environ.get("SAGE_TEST_IMAGE_IDENTITY") or "cafe123"
#: Production target arch. On Apple Silicon this builds under emulation; set
#: SAGE_TEST_DOCKER_PLATFORM="" to build a native image instead.
_PLATFORM = os.environ.get("SAGE_TEST_DOCKER_PLATFORM", "linux/amd64")

#: Exits 1 iff mlx is importable, so a leaked Apple-Silicon dep fails SMK-004.
_MLX_PROBE = "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('mlx') else 0)"
#: Constructs the real embedder; raises if the baked weights are absent offline.
_EMBED_PROBE = (
    "from sage.adapters.embedding_nomic import NomicEmbeddingProvider; NomicEmbeddingProvider()"
)

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
            "--build-arg",
            f"SAGE_BUILD_VERSION={_VERSION}",
            "--build-arg",
            f"SAGE_BUILD_IDENTITY={_IDENTITY}",
            "-t",
            _IMAGE,
            str(_REPO_ROOT),
        ],
        check=True,
    )
    return _IMAGE


def test_smk_003_runs_as_non_root(image: str) -> None:
    out = subprocess.run(
        ["docker", "run", "--rm", *_platform_args(), "--entrypoint", "id", image, "-u"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # A forgotten USER directive would report uid 0.
    assert out != "0", f"container runs as root (uid {out})"


def test_smk_004_mlx_absent(image: str) -> None:
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


def test_smk_005_pg_dump_on_path(image: str) -> None:
    # The maintenance job's snapshot-before-destroy step shells out to a bare
    # ``pg_dump`` on PATH; a missing binary raises FileNotFoundError and aborts the
    # teardown. This is the runtime proof the structural gate cannot give: the
    # client actually resolves, at the major that matches the Flexible Server (16).
    out = subprocess.run(
        ["docker", "run", "--rm", *_platform_args(), "--entrypoint", "pg_dump", image, "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "(PostgreSQL) 16" in out, f"pg_dump is not the expected major 16: {out!r}"


def test_smk_002_nomic_weights_load_offline(image: str) -> None:
    # If weights were not baked, offline construction raises -> non-zero exit.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *_platform_args(),
            "-e",
            "HF_HUB_OFFLINE=1",
            "-e",
            "TRANSFORMERS_OFFLINE=1",
            "--entrypoint",
            "python",
            image,
            "-c",
            _EMBED_PROBE,
        ],
        check=True,
    )


def test_smk_001_boots_health_green_version_baked(image: str) -> None:
    port = "18099"
    name = "sage-pytest-smoke"
    url = f"http://127.0.0.1:{port}/health"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, *_platform_args(), "-p", f"{port}:8000", image],
        check=True,
    )
    try:
        body = None
        for _ in range(60):
            with contextlib.suppress(Exception):
                # Fixed localhost http health URL; scheme is not user-controlled.
                with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                    if resp.status == 200:
                        body = json.loads(resp.read())
                        break
            time.sleep(2)
        # Host reachability via the published port proves the non-loopback
        # (0.0.0.0) bind; the exact body proves health-green and the baked stamp.
        assert body is not None, "/health never became reachable on the published port"
        assert body == {"status": "ok", "version": _VERSION}
        # A baked version of 0.0.0/unknown means build_info resolved no v* tag at
        # image-build time and fell back to the setuptools-scm sentinel -- the mode
        # a tagless out-of-repo build degrades to. Refuse it explicitly so a
        # degraded stamp fails the gate instead of passing whenever the expected
        # value is itself degraded (CAS-ADR-042).
        assert body["version"] not in ("0.0.0", "unknown", ""), (
            f"image reports a degraded baked version {body['version']!r}; build_info "
            "found no v* tag at build time and baked the setuptools-scm fallback"
        )

        # SMK-001b: the baked build identity reaches the startup banner (docker
        # logs), proving the ARG -> ENV -> build_info -> banner path end-to-end
        # -- not just that the Dockerfile declares the ARG.
        logs_result = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        logs = logs_result.stdout + logs_result.stderr
        assert f"build {_IDENTITY}" in logs, (
            f"startup banner does not report baked identity {_IDENTITY!r}"
        )
        assert "build identity unavailable" not in logs, (
            "banner reports identity unavailable despite a baked SAGE_BUILD_IDENTITY stamp"
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
