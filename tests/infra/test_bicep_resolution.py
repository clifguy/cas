"""Which Bicep compiler the suite's local compile checks run.

Each case fixes ``PATH`` lookups and the Azure CLI's configuration directory, so
the resolution is exercised on its own rules rather than on whatever this host
has installed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers import bicep

SOURCE = Path("infra/main.bicep")
OUTFILE = Path("out/main.json")


def _which(found: dict[str, str]) -> Callable[[str], str | None]:
    return lambda name: found.get(name)


def _install_cli_copy(config_dir: Path, *, executable: bool = True) -> Path:
    binary = config_dir / "bin" / "bicep"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755 if executable else 0o644)
    return binary


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Azure CLI configuration directory, named by ``AZURE_CONFIG_DIR``."""
    directory = tmp_path / "azure"
    directory.mkdir()
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(directory))
    return directory


def test_bicep_on_path_is_used_directly(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_cli_copy(config_dir)
    monkeypatch.setattr(bicep.shutil, "which", _which({"bicep": "/opt/bicep", "az": "/opt/az"}))
    assert bicep.bicep_command("build", SOURCE, OUTFILE) == [
        "/opt/bicep",
        "build",
        str(SOURCE),
        "--outfile",
        str(OUTFILE),
    ]


def test_the_azure_cli_copy_is_found_off_path(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_cli_copy(config_dir)
    monkeypatch.setattr(bicep.shutil, "which", _which({"az": "/opt/az"}))
    assert bicep.bicep_command("build", SOURCE, OUTFILE) == [
        str(binary),
        "build",
        str(SOURCE),
        "--outfile",
        str(OUTFILE),
    ]
    assert not bicep.bicep_unavailable()


def test_az_bicep_is_the_fallback(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bicep.shutil, "which", _which({"az": "/opt/az"}))
    assert bicep.bicep_command("build-params", SOURCE, OUTFILE) == [
        "az",
        "bicep",
        "build-params",
        "--file",
        str(SOURCE),
        "--outfile",
        str(OUTFILE),
    ]


def test_a_non_executable_cli_copy_is_passed_over(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_cli_copy(config_dir, executable=False)
    monkeypatch.setattr(bicep.shutil, "which", _which({"az": "/opt/az"}))
    command = bicep.bicep_command("build", SOURCE, OUTFILE)
    assert command is not None
    assert command[:2] == ["az", "bicep"]


def test_no_compiler_yields_none(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bicep.shutil, "which", _which({}))
    assert bicep.bicep_command("build", SOURCE, OUTFILE) is None
    assert bicep.bicep_unavailable()


def test_no_outfile_writes_to_standard_output(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bicep.shutil, "which", _which({"bicep": "/opt/bicep"}))
    assert bicep.bicep_command("build", SOURCE, None) == [
        "/opt/bicep",
        "build",
        str(SOURCE),
        "--stdout",
    ]
