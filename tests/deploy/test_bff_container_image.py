"""Structural gate for the CAS BFF container image (``Dockerfile.bff``).

Fast text assertions over the committed ``Dockerfile.bff`` and its companion
``Dockerfile.bff.dockerignore`` -- the always-on guard that runs in the default
suite with no Docker daemon. The heavyweight build/boot checks live in the
opt-in ``test_bff_container_smoke`` module.

The BFF image is a distinct artifact from the SAGE infra image (root
``Dockerfile``): it adds a Node stage that builds the SPA bundle, serves that
bundle from the Python runtime, runs ``python -m app.backend``, and omits the
embedder weight pre-bake the SAGE image carries. These assertions pin those
differences so a verbatim copy of the SAGE Dockerfile would not satisfy them.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "Dockerfile.bff"
_IGNORE = _REPO_ROOT / "Dockerfile.bff.dockerignore"


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text()


def _ignore_exclusions(text: str) -> set[str]:
    """Excluded patterns from a dockerignore: non-comment, non-negated lines."""
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        out.add(line)
    return out


def test_dockerfile_bff_exists() -> None:
    assert _DOCKERFILE.is_file(), "Dockerfile.bff is missing at the repository root"
    assert _IGNORE.is_file(), "Dockerfile.bff.dockerignore is missing at the repository root"


def test_multistage_node_spa_build() -> None:
    text = _dockerfile_text()
    assert "AS spa-builder" in text, "no Node SPA build stage"
    assert "npm ci" in text, "SPA stage does not install with npm ci"
    assert "npm run build" in text, "SPA stage does not run the production build"


def test_python_runtime_stage() -> None:
    text = _dockerfile_text()
    assert "uv sync --locked --no-dev" in text, "deps are not installed from the lockfile"
    assert "COPY sage/" in text, "the sage package is not copied into the image"
    assert "app/backend" in text, "the application backend is not copied into the image"
    # The built SPA is carried from the Node stage into the served dist dir.
    assert "--from=spa-builder" in text, "the built SPA is not copied from the Node stage"
    assert "app/dist" in text, "the served SPA dist directory is not staged"


def test_no_embedder_prebake() -> None:
    # The BFF constructs no embedder (it owns no vault registry), so unlike the
    # SAGE image it must not pre-bake the Nomic weights -- the minimization delta.
    assert "NomicEmbeddingProvider" not in _dockerfile_text()


def test_nonroot_user() -> None:
    text = _dockerfile_text()
    assert "useradd" in text, "no non-root user is created"
    user_directives = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("USER ")]
    # A forgotten USER directive, or a trailing escalation back to root, defeats
    # the non-root guarantee.
    assert user_directives, "no USER directive"
    assert user_directives[-1] == "USER bff", f"last USER directive is not bff: {user_directives}"


def test_version_arg_baked() -> None:
    text = _dockerfile_text()
    assert "ARG SAGE_BUILD_VERSION" in text, "no version build arg"
    assert "ENV SAGE_BUILD_VERSION=${SAGE_BUILD_VERSION}" in text, "version arg not exported to env"


def test_healthcheck_probes_bff_port() -> None:
    text = _dockerfile_text()
    assert "HEALTHCHECK" in text, "no container health check"
    assert "/health" in text, "health check does not probe /health"
    assert "8001" in text, "health check does not target the BFF port"


def test_nonloopback_bind_entrypoint() -> None:
    text = _dockerfile_text()
    assert '"app.backend"' in text, "entrypoint is not python -m app.backend"
    # S104 (bind-all-interfaces) is a false positive here: this asserts the
    # Dockerfile text contains the bind address, it does not open a socket.
    assert "0.0.0.0" in text, "the process does not bind a non-loopback interface"  # noqa: S104
    assert "EXPOSE 8001" in text, "the BFF port is not exposed"


def test_ignore_inverts_sage_for_frontend() -> None:
    text = _IGNORE.read_text()
    excl = _ignore_exclusions(text)
    # .git exclusion is load-bearing: it forces the repo-less version fallback.
    assert ".git" in excl, ".git must be excluded (load-bearing for the version fallback)"
    assert "app/node_modules" in excl, "node_modules belongs out of the build context"
    assert "app/dist" in excl, "a stale prebuilt dist must not shadow the in-image build"
    # The SPA sources MUST reach the build context (the SAGE ignore excludes them).
    assert "app/src" not in excl, "app/src must not be excluded -- the Node stage needs it"
    # The stack-config schema the runtime validator reads is re-included.
    assert "sage_core_config.schema.json" in text, "the config schema must be re-included"
