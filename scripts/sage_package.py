"""Build and verify the standalone SAGE distribution; transaction code is upstream.

This module uses only the standard library so a verified export needs no checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True


class PackageError(ValueError):
    """An untrusted or incomplete package must not be installed."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise PackageError(f"Non-regular package path: {rel}")
        if path.is_file():
            result[rel] = {"sha256": digest(path), "mode": stat.S_IMODE(mode)}
        else:
            result[rel] = {"directory": True, "mode": stat.S_IMODE(mode)}
    return result


def validate_skill(root: Path) -> None:
    entry = root / "SKILL.md"
    if not entry.is_file() or entry.is_symlink():
        raise PackageError("Missing SAGE entry point")
    text = entry.read_text()
    if not re.search(r"^name: sage$", text, re.M):
        raise PackageError("Expected skill name sage")
    # Both Markdown links and the inherited backtick reference style are used.
    for path in root.rglob("*.md"):
        text = path.read_text()
        refs = re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text)
        refs += re.findall(r"`((?:reference/)?[\w./-]+\.md)`", text)
        for ref in refs:
            if ref.startswith(("https://", "http://", "#")):
                continue
            target = (path.parent / ref).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise PackageError(f"Unresolved/escaping reference in {path.name}: {ref}")


def verify_package(
    root: Path, *, installed: bool = False, manifest_path: Path | None = None
) -> dict:
    root = Path(root)
    mp = Path(manifest_path) if manifest_path else root / "manifest.json"
    if mp.is_symlink():
        raise PackageError("Symlink manifest")
    try:
        manifest = json.loads(mp.read_text())
        if (
            manifest["format"] != "cas-sage-v1"
            or manifest["skills"] != ["sage"]
            or not re.fullmatch("[0-9a-f]{40}", manifest["source_commit"])
        ):
            raise PackageError("Unsupported package identity or ownership")
        expected = manifest["files"]
        if not isinstance(expected, dict) or not expected:
            raise PackageError("Empty inventory")
        for name in expected:
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or p.as_posix() != name:
                raise PackageError("Unsafe manifest path")
        if installed:
            actual = {
                "sage": {"directory": True, "mode": stat.S_IMODE((root / "sage").stat().st_mode)}
            }
            actual.update({"sage/" + k: v for k, v in inventory(root / "sage").items()})
            expected = {k: v for k, v in expected.items() if k == "sage" or k.startswith("sage/")}
        else:
            actual = inventory(root)
            actual.pop("manifest.json", None)
        if actual != expected:
            raise PackageError("Package inventory/hash/mode mismatch")
        validate_skill(root / "sage")
        return manifest
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"Invalid SAGE package: {exc}") from exc


def verify_engine(root: Path) -> dict:
    """Check the immutable upstream export before including executable code."""
    manifest = json.loads((root / "runtime-manifest.json").read_text())
    names = {"transaction_engine.py", "package_safety.py", "standalone_layout.py"}
    if (
        manifest.get("format") != 1
        or set(manifest.get("files", {})) != names
        or not re.fullmatch("[0-9a-f]{40}", manifest["source"]["revision"])
    ):
        raise PackageError("Invalid shared-engine pin")
    actual = inventory(root)
    actual.pop("runtime-manifest.json", None)
    if actual != manifest["files"]:
        raise PackageError("Shared-engine export differs from pinned hashes/modes")
    return manifest


def build_package(source: Path, output: Path, commit: str, release: str) -> dict:
    source, output = Path(source), Path(output)
    if not re.fullmatch("[0-9a-f]{40}", commit) or not release.strip():
        raise PackageError("Exact canonical commit and CAS release identity required")
    if os.path.lexists(output):
        raise PackageError("Output already exists")
    validate_skill(source / "skills/sage")
    inventory(source / "skills/sage")
    verify_engine(source / "scripts/sage_installer")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sage-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        shutil.copytree(source / "skills/sage", stage / "sage")
        shutil.copytree(source / "scripts/sage_installer", stage / "support")
        # The adapter is part of the verified distribution, not the installed skill.
        shutil.copy2(source / "scripts/sage_package.py", stage / "support/sage_package.py")
        shutil.copy2(source / "scripts/install_sage_skill.py", stage / "installer.py")
        for p in stage.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        manifest = {
            "format": "cas-sage-v1",
            "skills": ["sage"],
            "source_commit": commit,
            "cas_release": release,
            "files": inventory(stage),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        verify_package(stage)
        # Fail rather than replacing an output created during the build.
        output.mkdir()
        try:
            for p in stage.iterdir():
                shutil.move(str(p), output / p.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    return manifest


class SageVerifier:
    def verify(self, bundle, installed=False, manifest_path=None):
        return verify_package(Path(bundle), installed=installed, manifest_path=manifest_path)

    def validate_target(self, manifest, target_kind):
        if target_kind not in {"codex-personal", "claude-personal"}:
            raise PackageError("SAGE supports explicit personal targets only")
        if manifest.get("skills") != ["sage"]:
            raise PackageError("Unexpected owned skill")

    def root_names(self, manifest, target_kind):
        self.validate_target(manifest, target_kind)
        return ["sage", "manifest.json"]


def export_sources(repository: Path, revision: str, destination: Path) -> None:
    """Materialize committed blobs, never a checkout that can change during build."""
    paths = [
        "skills/sage",
        "scripts/sage_installer",
        "scripts/sage_package.py",
        "scripts/install_sage_skill.py",
    ]
    listing = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-rz", revision, "--", *paths]
    )
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        relative = Path(raw_path.decode())
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise PackageError("Only regular committed package sources are supported")
        if relative.is_absolute() or ".." in relative.parts:
            raise PackageError("Unsafe committed source path")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.check_output(["git", "-C", str(repository), "cat-file", "blob", oid])
        )
        target.chmod(int(mode[-3:], 8))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", type=Path, required=True)
    b.add_argument("--output", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("package", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            manifest = verify_package(args.package)
        else:
            # CLI seals only clean committed sources; tests can use fixture identities.
            status = subprocess.check_output(
                ["git", "-C", str(args.source), "status", "--porcelain", "--untracked-files=all"],
                text=True,
            )
            if status.strip():
                raise PackageError("Commit the candidate before sealing a release package")
            commit = subprocess.check_output(
                ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
            ).strip()
            release = subprocess.check_output(
                ["git", "-C", str(args.source), "describe", "--tags", "--always"], text=True
            ).strip()
            with tempfile.TemporaryDirectory(prefix="sage-source-") as temporary:
                export = Path(temporary)
                export_sources(args.source, commit, export)
                manifest = build_package(export, args.output, commit, release)
        print(
            json.dumps(
                {
                    "status": "verified",
                    "source_commit": manifest["source_commit"],
                    "cas_release": manifest["cas_release"],
                }
            )
        )
    except (PackageError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
