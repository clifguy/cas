"""Check security-tool locks by regenerating temporary copies with existing pins."""

from __future__ import annotations

import argparse
import difflib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def check(root: Path) -> int:
    directory = Path("requirements/security")
    inputs = {path.stem for path in (root / directory).glob("*.in")}
    outputs = {path.stem for path in (root / directory).glob("*.txt")}
    if not inputs and not outputs:
        print("No security lockfile pairs found in requirements/security", file=sys.stderr)
        return 1
    if inputs != outputs:
        missing = [str(directory / f"{name}.txt") for name in sorted(inputs - outputs)]
        missing += [str(directory / f"{name}.in") for name in sorted(outputs - inputs)]
        print(f"Missing security lockfile pair: {', '.join(missing)}", file=sys.stderr)
        return 1
    version = json.loads((root / "versions.json").read_text())["python"]["version"]
    failed = False
    with tempfile.TemporaryDirectory(prefix="security-lockfiles-") as temporary:
        workspace = Path(temporary)
        shutil.copytree(root / directory, workspace / directory)
        for name in sorted(inputs):
            source = directory / f"{name}.in"
            output = directory / f"{name}.txt"
            command = [
                "uv",
                "pip",
                "compile",
                source.as_posix(),
                "--generate-hashes",
                "--universal",
                "--python-version",
                version,
                "-o",
                output.as_posix(),
            ]
            # Relative paths preserve the documented header and # via annotations.
            # The copied output seeds uv's existing pins; no upgrade flag is used.
            result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
            if result.returncode:
                print(f"Compilation failed for {source}:\n{result.stderr}", file=sys.stderr)
                failed = True
            else:
                expected = (root / output).read_bytes()
                actual = (workspace / output).read_bytes()
                if expected == actual:
                    print(f"OK: {output}")
                    continue
                failed = True
                print(f"Security lockfile drift: {output}", file=sys.stderr)
                diff = difflib.unified_diff(
                    expected.decode().splitlines(keepends=True),
                    actual.decode().splitlines(keepends=True),
                    fromfile=str(output),
                    tofile=f"{output} (regenerated)",
                )
                sys.stderr.writelines(diff)
            print(f"Regenerate with: {shlex.join(command)}", file=sys.stderr)
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        return check(args.repo.resolve())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Security lockfile verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
