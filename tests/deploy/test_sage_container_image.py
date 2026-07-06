"""Structural gate for the SAGE infra-server container image (root ``Dockerfile``).

Fast text assertions over the committed root ``Dockerfile`` -- the always-on guard
that runs in the default suite with no Docker daemon. The heavyweight build/boot
checks (including that ``pg_dump`` actually resolves on ``PATH``) live in the
opt-in ``test_container_smoke`` module.

The runtime image carries the Postgres client (``pg_dump``) because the in-VNet
maintenance job runs this image and its whole-vault teardown takes a
``pg_dump`` schema snapshot before destroying anything. The client major must
track the Flexible Server major (``infra/modules/postgres.bicep``): an older
client refuses to dump a newer server, which would fail the snapshot and abort
the teardown. ``test_pg_client_major_matches_flexible_server`` binds the two so a
future server-version bump that forgets the image is caught here.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_POSTGRES_BICEP = _REPO_ROOT / "infra" / "modules" / "postgres.bicep"


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text()


def _runtime_stage_text() -> str:
    """The Dockerfile text from the runtime ``FROM ... AS runtime`` onward.

    The runtime image copies only the venv, model cache, and source from the
    builder -- never the builder's apt-installed system binaries -- so a package
    the runtime job needs must be installed in this segment, not the builder.
    """
    text = _dockerfile_text()
    marker = "AS runtime"
    idx = text.find(marker)
    assert idx != -1, "no `AS runtime` stage in the Dockerfile"
    return text[idx:]


def _flexible_server_major() -> str:
    """The Postgres Flexible Server major version, read from the infra module."""
    match = re.search(r"postgresVersion\s+string\s*=\s*'(\d+)'", _POSTGRES_BICEP.read_text())
    assert match is not None, "could not read postgresVersion default from postgres.bicep"
    return match.group(1)


def test_dockerfile_sage_exists() -> None:
    assert _DOCKERFILE.is_file(), "Dockerfile is missing at the repository root"


def test_installs_postgresql_client() -> None:
    text = _dockerfile_text()
    assert "apt-get install" in text, "no apt package install layer (pg_dump would be absent)"
    assert "postgresql-client-" in text, "the Postgres client (pg_dump) is not installed"


def test_pg_client_major_matches_flexible_server() -> None:
    # Anti-drift: the client major must equal the Flexible Server major so pg_dump
    # never refuses a newer-server dump. A future server bump that forgets the
    # image, or a client pinned to the older Debian stock major, reds this gate.
    major = _flexible_server_major()
    assert f"postgresql-client-{major}" in _dockerfile_text(), (
        f"the image must install postgresql-client-{major} to match the Flexible "
        f"Server major {major} (infra/modules/postgres.bicep)"
    )


def test_pg_client_installed_in_runtime_stage() -> None:
    # A builder-only install would never reach the shipped image: the runtime
    # stage copies the venv and source but not the builder's apt binaries.
    assert "postgresql-client-" in _runtime_stage_text(), (
        "the Postgres client must be installed in the runtime stage, not only the builder"
    )


def test_runtime_nonroot_user() -> None:
    text = _dockerfile_text()
    assert "useradd" in text, "no non-root user is created"
    user_directives = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("USER ")]
    # A forgotten USER directive, or a trailing escalation back to root, defeats
    # the non-root guarantee -- and would silently let the apt layer's root
    # context persist into the running process.
    assert user_directives, "no USER directive"
    assert user_directives[-1] == "USER sage", f"last USER directive is not sage: {user_directives}"


def test_entrypoint_is_sage() -> None:
    assert '"sage"' in _dockerfile_text(), "entrypoint is not python -m sage"
