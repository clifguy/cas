"""Locate a Bicep compiler for the suite's local compile checks.

A local compile is a fast check; the infrastructure workflow's ``validate`` job
is the authoritative one. The standalone ``bicep`` binary is preferred because
``az bicep`` starts the whole Azure CLI before it compiles anything. The Azure
CLI installs its own copy of that binary under its configuration directory
without putting it on ``PATH``, so that copy is looked for too before falling
back to ``az bicep``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

UNAVAILABLE_REASON: Final[str] = (
    "bicep/az CLI absent; the infra workflow validate job is authoritative"
)


def standalone_bicep() -> str | None:
    """The standalone ``bicep`` binary: on ``PATH``, else the Azure CLI's copy."""
    on_path = shutil.which("bicep")
    if on_path is not None:
        return on_path
    config_dir = os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")
    installed = Path(config_dir) / "bin" / "bicep"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return None


def bicep_command(verb: str, source: Path, outfile: Path | None) -> list[str] | None:
    """The command running Bicep ``verb`` on ``source``, or None with no compiler.

    ``verb`` is a Bicep subcommand taking one input file, such as ``build`` or
    ``build-params``. The output goes to ``outfile``, or to standard output
    when it is None.
    """
    output = ["--outfile", str(outfile)] if outfile is not None else ["--stdout"]
    binary = standalone_bicep()
    if binary is not None:
        return [binary, verb, str(source), *output]
    if shutil.which("az") is not None:
        return ["az", "bicep", verb, "--file", str(source), *output]
    return None


def bicep_unavailable() -> bool:
    """True when no compiler is reachable, for a ``skipif`` condition."""
    return standalone_bicep() is None and shutil.which("az") is None
