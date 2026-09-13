"""The substrate manifest follows the CAS-ADR-008 versioning format.

CAS-ADR-008 v2 retired the manifest's two hand-assigned counters. The
substrate-wide ``substrate_version`` is gone. Each artifact's ``version`` is the
release ``MAJOR.MINOR`` in which the artifact last changed. The revision history
grows one entry per release, folded from change records, above the entries
written under the old scheme, which the ADR keeps verbatim.

These checks hold that format on the committed manifest. The release tooling
reads the manifest and trusts its shape. The gate over individual changes can
only check what a change moves, not whether the file it starts from is right.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import jsonschema
import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MANIFEST_PATH: Final[Path] = REPO_ROOT / "docs" / "fs" / "manifest.json"
RECORD_SCHEMA_PATH: Final[Path] = (
    REPO_ROOT / "docs" / "fs" / "changes" / "change_record.schema.json"
)

# sha256 of the revision-history entries written under the counter scheme,
# serialized with sorted keys and no whitespace. CAS-ADR-008 keeps them verbatim,
# so this value never changes.
LEGACY_HISTORY_SHA256: Final[str] = (
    "85d99d08850ed3120116c4fed29a416e7a07b597b8febbbc2f032b41bbda4f85"
)
LEGACY_HISTORY_LENGTH: Final[int] = 101

_RELEASE_VERSION: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+$")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )


def _latest_release_tag() -> str | None:
    result = _git("describe", "--abbrev=0", "--tags", "--match", "v*.*.0", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _legacy_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in manifest["revision_history"] if "release" not in entry]


def test_substrate_counter_is_retired(manifest: dict[str, Any]) -> None:
    assert "substrate_version" not in manifest


def test_manifest_description_states_the_release_format(manifest: dict[str, Any]) -> None:
    description = manifest["description"]

    assert "per release" in description
    assert "last changed" in description
    assert "substrate_version" not in description


def test_artifact_versions_are_releases(manifest: dict[str, Any]) -> None:
    """Each version names a release that exists, or is null for an artifact no
    release has published yet."""
    latest = _latest_release_tag()
    if latest is None:
        pytest.skip("no release tag is reachable; this checkout has no tag history")
    tags = set(_git("tag", "--list", "v*.*.0").stdout.split())
    history = manifest["revision_history"]
    pending = history[0]["release"] if history and "release" in history[0] else None

    problems: list[str] = []
    for entry in manifest["schemas"]:
        path, version = entry["path"], entry["version"]
        present_at_latest = _git("cat-file", "-e", f"{latest}:docs/fs/{path}").returncode == 0
        if version is None:
            if present_at_latest:
                problems.append(f"{path}: null, but {latest} already published it")
        elif not isinstance(version, str) or not _RELEASE_VERSION.match(version):
            problems.append(f"{path}: {version!r} is not a MAJOR.MINOR release")
        elif f"v{version}.0" not in tags and version != pending:
            problems.append(f"{path}: {version!r} names no release tag and no pending release")

    assert not problems, "\n".join(problems)


def test_release_entries_precede_legacy_entries_and_hold_valid_records(
    manifest: dict[str, Any],
) -> None:
    history = manifest["revision_history"]
    kinds = ["release" if "release" in entry else "legacy" for entry in history]
    validator = jsonschema.Draft202012Validator(json.loads(RECORD_SCHEMA_PATH.read_text()))

    assert kinds == sorted(kinds, key=lambda kind: kind != "release"), (
        "a release entry sits below an entry written under the counter scheme"
    )
    for entry in history:
        if "release" not in entry:
            continue
        assert set(entry) == {"release", "date", "changes"}, entry.keys()
        assert _RELEASE_VERSION.match(entry["release"]), entry["release"]
        assert entry["changes"], f"release {entry['release']} folds no changes"
        for change in entry["changes"]:
            errors = [e.message for e in validator.iter_errors(change)]
            assert not errors, f"release {entry['release']}: {errors}"


def test_legacy_history_is_verbatim(manifest: dict[str, Any]) -> None:
    legacy = _legacy_entries(manifest)
    serialized = json.dumps(legacy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    assert len(legacy) == LEGACY_HISTORY_LENGTH
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == LEGACY_HISTORY_SHA256, (
        "an entry written under the counter scheme was edited, reordered, or removed; "
        "CAS-ADR-008 keeps them verbatim"
    )


def test_manifest_is_canonically_formatted() -> None:
    """The release step rewrites the manifest, and a rewrite of a canonically
    formatted file changes only what the release changed."""
    text = MANIFEST_PATH.read_text(encoding="utf-8")

    assert json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n" == text
