"""Behavioral checks for the shipped standalone SAGE package adapter."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.sage_package import PackageError, build_package, verify_package

ROOT = Path(__file__).resolve().parents[2]
REV = "a" * 40


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(ROOT / "skills/sage", root / "skills/sage")
    shutil.copytree(ROOT / "scripts/sage_installer", root / "scripts/sage_installer")
    shutil.copy2(ROOT / "scripts/install_sage_skill.py", root / "scripts/install_sage_skill.py")
    shutil.copy2(ROOT / "scripts/sage_package.py", root / "scripts/sage_package.py")
    return root


def test_complete_package_is_reproducible_and_detached(source, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    a = build_package(source, one, REV, "3.0.2")
    b = build_package(source, two, REV, "3.0.2")
    assert a == b
    shutil.rmtree(source)
    assert not source.exists()
    assert verify_package(one) == a
    assert (one / "sage/reference/transfer.md").is_file()
    assert set(a["skills"]) == {"sage"}
    assert not (one / "sage/project-policy").exists()


@pytest.mark.parametrize("damage", ["delete", "extra", "modify", "mode", "symlink"])
def test_payload_inventory_detects_all_drift(source, tmp_path, damage):
    out = tmp_path / "package"
    build_package(source, out, REV, "3.0.2")
    p = out / "sage/reference/runtime.md"
    if damage == "delete":
        p.unlink()
    elif damage == "extra":
        (out / "sage/__pycache__").mkdir()
        (out / "sage/__pycache__/bad.pyc").write_bytes(b"bytecode")
    elif damage == "modify":
        p.write_text("changed")
    elif damage == "mode":
        p.chmod(0o755)
    else:
        p.unlink()
        p.symlink_to("/etc/hosts")
    with pytest.raises(PackageError):
        verify_package(out)


@pytest.mark.parametrize(
    "reference",
    [
        "reference/missing.md",
        "../../outside.md",
        "/tmp/source-only.md",
        "file:///tmp/source-only.md",
    ],
)
def test_missing_or_escaping_reference_refused_before_publication(source, tmp_path, reference):
    p = source / "skills/sage/SKILL.md"
    p.write_text(p.read_text() + "\nRead [required](" + reference + ").\n")
    out = tmp_path / "package"
    with pytest.raises(PackageError):
        build_package(source, out, REV, "3.0.2")
    assert not out.exists()


def test_existing_destination_is_preserved(source, tmp_path):
    out = tmp_path / "package"
    out.mkdir()
    (out / "owner").write_text("retain")
    with pytest.raises(PackageError):
        build_package(source, out, REV, "3.0.2")
    assert (out / "owner").read_text() == "retain"


def test_manifest_cannot_broaden_skill_ownership(source, tmp_path):
    out = tmp_path / "package"
    build_package(source, out, REV, "3.0.2")
    p = out / "manifest.json"
    manifest = json.loads(p.read_text())
    manifest["skills"].append("unrelated")
    p.write_text(json.dumps(manifest))
    with pytest.raises(PackageError):
        verify_package(out)


@pytest.mark.parametrize("host", ["codex-personal", "claude-personal"])
def test_detached_cli_install_verify_noop_and_rollback(source, tmp_path, host):
    import subprocess
    import sys

    out = tmp_path / "package"
    build_package(source, out, REV, "3.0.2")
    shutil.rmtree(source)
    assert not source.exists()
    root = tmp_path / "personal"
    root.mkdir()
    (root / "skills").mkdir()
    (root / "skills/manifest.json").write_text("host reserved")
    (root / "skills/unrelated").mkdir()
    (root / "skills/unrelated/keep").write_bytes(b"owner")

    def run(*args):
        process = subprocess.run(
            [sys.executable, str(out / "installer.py"), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    plan = run("plan", "--root", str(root), "--target-kind", host, "--bundle", str(out))
    saved = tmp_path / "plan.json"
    saved.write_text(json.dumps(plan))
    applied = run("apply", "--plan", str(saved))
    assert (root / "skills/sage/SKILL.md").read_bytes() == (out / "sage/SKILL.md").read_bytes()
    assert run("verify", "--root", str(root), "--target-kind", host)["status"] == "verified"
    plan = run("plan", "--root", str(root), "--target-kind", host, "--bundle", str(out))
    assert plan["action"] == "already-current"
    assert (root / "skills/manifest.json").read_text() == "host reserved"
    assert (root / "skills/unrelated/keep").read_bytes() == b"owner"
    assert verify_package(out)["source_commit"] == REV
    run(
        "rollback",
        "--root",
        str(root),
        "--target-kind",
        host,
        "--transaction",
        applied["transaction"],
    )
    assert not (root / "skills/sage").exists()
    assert (root / "skills/unrelated/keep").read_bytes() == b"owner"


def test_changed_upstream_engine_is_not_packaged(source, tmp_path):
    p = source / "scripts/sage_installer/transaction_engine.py"
    p.write_text(p.read_text() + "\n# changed export\n")
    out = tmp_path / "package"
    with pytest.raises(PackageError):
        build_package(source, out, REV, "3.0.2")
    assert not out.exists()


def test_export_reads_committed_blobs_after_checkout_changes(source, tmp_path):
    import subprocess

    from scripts.sage_package import export_sources

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "fixture",
    )
    revision = git("rev-parse", "HEAD")
    expected = (source / "skills/sage/SKILL.md").read_bytes()
    (source / "skills/sage/SKILL.md").write_text("concurrent checkout change")
    export = tmp_path / "export"
    export_sources(source, revision, export)
    assert (export / "skills/sage/SKILL.md").read_bytes() == expected
    package = tmp_path / "package"
    assert build_package(export, package, revision, "fixture")["source_commit"] == revision


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


@pytest.mark.parametrize("release", ["v3.9.0-2-g5d8b8a21", "v3.9.0: odd #release"])
def test_packaged_skill_reports_build_version(source, tmp_path, release):
    """The built entry point names the CAS release and commit it was sealed from."""
    out = tmp_path / "package"
    manifest = build_package(source, out, REV, release)
    built = _frontmatter(out / "sage/SKILL.md")
    original = _frontmatter(source / "skills/sage/SKILL.md")
    assert built["metadata"] == {"version": release, "source_commit": REV}
    assert {k: v for k, v in built.items() if k != "metadata"} == original
    assert verify_package(out) == manifest


def test_build_leaves_source_skill_unversioned(source, tmp_path):
    """The stamp lands in the package only; the repository source stays unversioned."""
    entry = source / "skills/sage/SKILL.md"
    before = entry.read_bytes()
    build_package(source, tmp_path / "package", REV, "v3.9.0")
    assert entry.read_bytes() == before
    assert "metadata" not in _frontmatter(entry)


@pytest.mark.parametrize(
    "authored",
    [
        "metadata:\n  version: 1.0.0",
        "version: 1.0.0",
        '"metadata":\n  version: 1.0.0',
        "metadata :\n  version: 1.0.0",
    ],
)
def test_hand_versioned_source_refused(source, tmp_path, authored):
    """A version written by hand into the source would compete with the build stamp."""
    entry = source / "skills/sage/SKILL.md"
    head, body = entry.read_text().split("\n---\n", 1)
    entry.write_text(head + "\n" + authored + "\n---\n" + body)
    out = tmp_path / "package"
    with pytest.raises(PackageError, match="only name and description"):
        build_package(source, out, REV, "v3.9.0")
    assert not out.exists()


def test_source_without_frontmatter_refused(source, tmp_path):
    """The version stamp has nowhere to go in an entry point without frontmatter."""
    entry = source / "skills/sage/SKILL.md"
    entry.write_text(entry.read_text().split("\n---\n", 1)[1].replace("\n", "\nname: sage\n", 1))
    out = tmp_path / "package"
    with pytest.raises(PackageError, match="frontmatter"):
        build_package(source, out, REV, "v3.9.0")
    assert not out.exists()
