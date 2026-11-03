"""Check that every security-tool uv project's lock is current with its pyproject."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

DIRECTORY = Path("requirements/security")


def check(root: Path) -> int:
    directory = root / DIRECTORY
    # Hidden entries are local artifacts (.DS_Store, a venv) that git never ships.
    entries = (
        sorted(e for e in directory.iterdir() if not e.name.startswith("."))
        if directory.is_dir()
        else []
    )
    projects = [entry for entry in entries if (entry / "pyproject.toml").is_file()]
    if not projects:
        print(f"No security tool projects found in {DIRECTORY}", file=sys.stderr)
        return 1
    failed = False
    # Anything else here is a requirements source no gate would verify.
    for entry in entries:
        if entry not in projects:
            print(f"Not a security tool project: {DIRECTORY / entry.name}", file=sys.stderr)
            failed = True
    for project in projects:
        relative = DIRECTORY / project.name
        lock = relative / "uv.lock"
        fix = shlex.join(["uv", "lock", "--project", relative.as_posix()])
        if not (root / lock).is_file():
            print(f"Missing security lockfile: {lock}\nCreate it with: {fix}", file=sys.stderr)
            failed = True
            continue
        # --check resolves against the committed lock and writes nothing.
        command = ["uv", "lock", "--check", "--project", relative.as_posix()]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        if result.returncode:
            print(f"Security lockfile drift: {lock}\n{result.stderr}", file=sys.stderr)
            print(f"Regenerate with: {fix}", file=sys.stderr)
            failed = True
        else:
            print(f"OK: {lock}")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        return check(args.repo.resolve())
    except OSError as exc:
        print(f"Security lockfile verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
