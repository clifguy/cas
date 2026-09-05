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
    #
    # Read from the runtime stage, not the whole file. Pairing a whole-file
    # version check with the sibling gate's unversioned runtime-stage check
    # leaves a gap between them: a matching major installed in the builder and a
    # stale one in the runtime stage satisfies both, and the shipped image is the
    # runtime stage.
    major = _flexible_server_major()
    assert f"postgresql-client-{major}" in _runtime_stage_text(), (
        f"the runtime stage must install postgresql-client-{major} to match the Flexible "
        f"Server major {major} (infra/modules/postgres.bicep)"
    )


def test_pg_client_installed_in_runtime_stage() -> None:
    # A builder-only install would never reach the shipped image: the runtime
    # stage copies the venv and source but not the builder's apt binaries.
    assert "postgresql-client-" in _runtime_stage_text(), (
        "the Postgres client must be installed in the runtime stage, not only the builder"
    )


def test_installs_ocr_extra() -> None:
    # The scanned-PDF OCR pre-pass needs the ocrmypdf Python package, which lives
    # in the optional [ocr] extra. A base-only sync ships an image that raises on
    # the lazy `import ocrmypdf` and surfaces a generic internal_error.
    text = _dockerfile_text()
    assert "--extra ocr" in text, (
        "the runtime image must `uv sync` the [ocr] extra so ocrmypdf is present"
    )


def test_installs_ocr_binaries_in_runtime_stage() -> None:
    # ocrmypdf shells out to tesseract (OCR engine + English data) and ghostscript
    # (gs). They must be apt-installed in the RUNTIME stage: a builder-only install
    # never reaches the shipped image (the runtime copies the venv/source, not the
    # builder's apt binaries), exactly like the postgresql-client gate above.
    runtime = _runtime_stage_text()
    for pkg in ("tesseract-ocr", "tesseract-ocr-eng", "ghostscript"):
        assert pkg in runtime, (
            f"{pkg} is not installed in the runtime stage; scanned-PDF OCR would fail on cloud"
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


# The docs/fs files the running process reads. Each must be COPYed into the
# runtime stage AND re-included in the build context: .dockerignore excludes
# docs/ wholesale. The two halves fail differently -- a missing re-include
# breaks the build, a missing COPY breaks startup -- so both are asserted.
_RUNTIME_DOCS_FS_FILES = (
    "docs/fs/sage/sage_core_config.schema.json",
    "docs/fs/sage/sage_core_api.openapi.yaml",
    "docs/fs/cas_app_api.openapi.yaml",
)


def test_runtime_docs_fs_files_are_copied() -> None:
    """Every docs/fs file the process reads is COPYed to the path it reads
    from: <repo-root>/docs/fs/..., which is /opt/sage in the image.
    """
    runtime = _runtime_stage_text()
    for relative in _RUNTIME_DOCS_FS_FILES:
        assert f"COPY {relative} /opt/sage/{relative}" in runtime, (
            f"{relative} is not COPYed into the runtime stage; the process reads it at startup"
        )


def test_runtime_docs_fs_files_survive_the_dockerignore() -> None:
    """Each COPYed docs/fs file has a matching re-include in .dockerignore.

    A drift guard, not a correctness proof: this asserts the two source files
    agree, so a pattern that is wrong in both stays green. Whether the build
    context actually admits the file is decided by Docker's own ignore matcher
    at build time -- the gate of record is the image build (a COPY of an
    excluded path fails it outright), with SMK-007 proving the file resolves
    inside the built image.
    """
    ignore_lines = [ln.strip() for ln in (_REPO_ROOT / ".dockerignore").read_text().splitlines()]
    for relative in _RUNTIME_DOCS_FS_FILES:
        assert f"!{relative}" in ignore_lines, (
            f"{relative} is COPYed but not re-included in .dockerignore; the build would fail"
        )
