#!/usr/bin/env python3
"""Record, check, and release changes to the Formal Substrate (CAS-ADR-008).

CAS-ADR-008 makes every change a patch unless it crosses one of three minor
boundaries, and has each change record its own classification in a numberless
change record that a release step later folds into the manifest. This script is
that machinery:

``check``
    The change-record gate. A change that touches a manifest-listed substrate
    file, either OpenAPI specification, or the MCP tool catalog must add a
    record under ``docs/fs/changes/unreleased/``. Every record must validate
    against ``docs/fs/changes/change_record.schema.json``. The contract
    comparison in ``scripts/contract_diff.py`` runs over the change, and a
    ``patch`` claim fails against a minor-level finding unless the record
    carries the owner's ``detector_override``. A change that is not a release
    may not move an artifact version, a specification's ``info.version``, or
    the revision history, and may not delete another change's record: those
    edits are what made concurrent changes conflict, so only the release step
    makes them.

``status``
    What is waiting to be released: the unreleased records, whether a minor
    release is due, the contract comparison against the last release tag, and
    whether a landed release still lacks its tag.

``release-prepare``
    Run on a release branch. Folds every unreleased record into a new release
    entry at the head of the manifest's revision history, sets the version of
    every artifact changed since the last release, sets both specifications'
    ``info.version``, and deletes the records.

``release-tag``
    Run once the release change has landed. Creates the annotated ``vMAJOR.MINOR.0``
    tag on the commit that introduced the release entry.

**Tag sequencing.** The contract-version gate holds both specifications to the
version ``git describe`` derives from the tags. A release change declares the
next version before its tag can exist, because the tag belongs on the commit
that change becomes once it lands. ``contract_version_ok`` resolves this: the
specifications may declare the manifest's newest release while that release's
tag does not yet exist, and at no other time. Between releases the check is as
strict as it was.

**The base of a comparison.** ``check`` compares a change against its merge base
with the base revision, so the records and findings it reports are the change's
own. ``status`` and ``release-prepare`` compare against the last release tag,
because what they report is everything the next release publishes.

Usage::

    python -m scripts.substrate_changes check                    # working tree vs origin/main
    python -m scripts.substrate_changes check --base REF --head REF
    python -m scripts.substrate_changes check --format json
    python -m scripts.substrate_changes status
    python -m scripts.substrate_changes release-prepare [--major]
    python -m scripts.substrate_changes release-tag [--push]

``check`` exits 1 when the gate fails. ``release-prepare`` and ``release-tag``
exit 1 when they refuse, having changed nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import jsonschema
import yaml

from scripts.contract_diff import Contract, Finding, diff_contracts

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

SUBSTRATE_DIR: Final[str] = "docs/fs"
MANIFEST: Final[str] = "docs/fs/manifest.json"
CORE_SPEC: Final[str] = "docs/fs/sage/sage_core_api.openapi.yaml"
APP_SPEC: Final[str] = "docs/fs/cas_app_api.openapi.yaml"
SPEC_PATHS: Final[tuple[str, ...]] = (CORE_SPEC, APP_SPEC)
CATALOG_PATH: Final[str] = "docs/fs/sage/sage_mcp_tools.catalog.json"
RECORDS_DIR: Final[str] = "docs/fs/changes/unreleased"
RECORD_SCHEMA: Final[str] = "docs/fs/changes/change_record.schema.json"

# Files the records directory holds that are not records.
_NON_RECORD_FILES: Final[frozenset[str]] = frozenset({".gitkeep"})
_RECORD_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.yaml$")

# The counter CAS-ADR-008 v2 retires. Its presence marks a manifest from before
# the migration, which is the one change allowed to rewrite artifact versions
# outside a release.
LEGACY_COUNTER: Final[str] = "substrate_version"

RELEASE_TAG_MATCH: Final[str] = "v*.*.0"
DEFAULT_BRANCHES: Final[frozenset[str]] = frozenset({"main", "master"})
CATEGORY_ORDER: Final[tuple[str, ...]] = ("capability", "caller-adaptation", "operator-adaptation")


class ReleaseRefused(RuntimeError):
    """A release step declined to act; nothing was changed."""


class GitError(RuntimeError):
    """A git command failed."""


# ---------------------------------------------------------------------------
# Git and trees
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _git_ok(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


class _Tree:
    """Read access to the files of one revision, or of the working tree."""

    def read(self, rel: str) -> str | None:
        raise NotImplementedError

    def record_names(self) -> list[str]:
        raise NotImplementedError


class _RevisionTree(_Tree):
    def __init__(self, root: Path, revision: str) -> None:
        self.root = root
        self.revision = revision

    def read(self, rel: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{self.revision}:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def record_names(self) -> list[str]:
        listing = _git(self.root, "ls-tree", "--name-only", self.revision, f"{RECORDS_DIR}/")
        names = (Path(line).name for line in listing.splitlines() if line.strip())
        return sorted(name for name in names if name not in _NON_RECORD_FILES)


class _WorkingTree(_Tree):
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def record_names(self) -> list[str]:
        directory = self.root / RECORDS_DIR
        if not directory.is_dir():
            return []
        return sorted(
            p.name for p in directory.iterdir() if p.is_file() and p.name not in _NON_RECORD_FILES
        )


def _tree(root: Path, revision: str | None) -> _Tree:
    return _WorkingTree(root) if revision is None else _RevisionTree(root, revision)


def tag_exists(repo_root: Path, release: str) -> bool | None:
    """Whether ``v<release>.0`` exists, or ``None`` when git cannot say."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/v{release}.0"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode not in (0, 1):
        return None
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Manifest, records, contract
# ---------------------------------------------------------------------------


def render_manifest(manifest: Mapping[str, Any]) -> str:
    """The manifest's canonical committed text."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def head_release(manifest: Mapping[str, Any] | None) -> str | None:
    """The newest release entry's version, or ``None`` before the first release."""
    history = (manifest or {}).get("revision_history") or []
    if history and isinstance(history[0], dict) and "release" in history[0]:
        return str(history[0]["release"])
    return None


def release_entries(manifest: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    history = (manifest or {}).get("revision_history") or []
    return [entry for entry in history if isinstance(entry, dict) and "release" in entry]


def _listed(manifest: Mapping[str, Any] | None) -> set[str]:
    return {f"{SUBSTRATE_DIR}/{entry['path']}" for entry in (manifest or {}).get("schemas") or []}


def _versions(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {entry["path"]: entry.get("version") for entry in manifest.get("schemas") or []}


def _load_json(tree: _Tree, rel: str) -> Any:
    text = tree.read(rel)
    return None if text is None else json.loads(text)


def _load_yaml(tree: _Tree, rel: str) -> Any:
    text = tree.read(rel)
    return None if text is None else yaml.safe_load(text)


def _load_contract(tree: _Tree) -> Contract:
    return Contract(
        sage_core_api=_load_yaml(tree, CORE_SPEC),
        cas_app_api=_load_yaml(tree, APP_SPEC),
        mcp_catalog=_load_json(tree, CATALOG_PATH),
    )


def _info_version(spec: Any) -> Any:
    return ((spec or {}).get("info") or {}).get("version")


def _validator(tree: _Tree) -> jsonschema.Draft202012Validator:
    text = tree.read(RECORD_SCHEMA)
    if text is None:
        text = (REPO_ROOT / RECORD_SCHEMA).read_text(encoding="utf-8")
    return jsonschema.Draft202012Validator(json.loads(text))


def _load_records(tree: _Tree) -> tuple[dict[str, Any], list[str]]:
    """Every record in a tree, by file name, and the errors that make any invalid."""
    validator = _validator(tree)
    records: dict[str, Any] = {}
    errors: list[str] = []
    for name in tree.record_names():
        rel = f"{RECORDS_DIR}/{name}"
        if not _RECORD_NAME.match(name):
            errors.append(f"{rel}: a record's file name is a kebab-case slug ending in .yaml")
        try:
            data = yaml.safe_load(tree.read(rel) or "")
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: not valid YAML ({exc})")
            continue
        records[name] = data
        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            location = "/".join(str(part) for part in error.absolute_path) or "record"
            errors.append(f"{rel}: {location}: {error.message}")
    return records, errors


def _categories(records: list[Any]) -> list[str]:
    named = {c for r in records if isinstance(r, dict) for c in r.get("category") or []}
    return [c for c in CATEGORY_ORDER if c in named]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """The gate's verdict on one change."""

    base: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    records_added: list[str] = field(default_factory=list)
    records: list[Any] = field(default_factory=list)
    classification: str = "patch"
    categories: list[str] = field(default_factory=list)
    release: str | None = None

    def classification_line(self) -> str:
        if self.release is not None:
            return f"Release classification: release {self.release}"
        if self.categories:
            return f"Release classification: {self.classification} ({', '.join(self.categories)})"
        return f"Release classification: {self.classification}"

    def to_json(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "classification": self.classification,
            "categories": self.categories,
            "release": self.release,
            "records": [
                {"path": path, "record": record}
                for path, record in zip(self.records_added, self.records, strict=True)
            ],
            "findings": [f.__dict__ for f in self.findings],
            "errors": self.errors,
            "warnings": self.warnings,
            "notes": self.notes,
        }

    def render(self) -> str:
        lines = [self.classification_line()]
        for record in self.records:
            if isinstance(record, dict) and record.get("for_callers"):
                lines.append(f"  for callers: {record['for_callers']}")
        lines.extend(f"record added: {path}" for path in self.records_added)
        lines.extend(f"contract finding: {f.render()}" for f in self.findings)
        lines.extend(f"note: {note}" for note in self.notes)
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        lines.extend(f"error: {error}" for error in self.errors)
        return "\n".join(lines)


def _default_base(root: Path) -> str:
    for candidate in ("origin/main", "main"):
        if _git_ok(root, "rev-parse", "-q", "--verify", f"{candidate}^{{commit}}"):
            return candidate
    raise GitError("no base revision given and neither origin/main nor main exists")


def _changed_paths(root: Path, base: str, head: str | None) -> dict[str, str]:
    """Each path the change touches, with its status letter (A, M, D, T)."""
    args = ["diff", "--name-status", "--no-renames", base]
    if head is not None:
        args.append(head)
    changed: dict[str, str] = {}
    for line in _git(root, *args).splitlines():
        status, _, path = line.partition("\t")
        if path:
            changed[path] = status[:1]
    if head is None:
        for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
            if path:
                changed[path] = "A"
    return changed


def run_check(
    repo_root: Path | str, base: str | None = None, head: str | None = None
) -> CheckResult:
    """The change-record gate over ``head`` (the working tree when ``None``)."""
    root = Path(repo_root)
    base_commit = _git(root, "merge-base", base or _default_base(root), head or "HEAD").strip()
    result = CheckResult(base=base_commit)
    base_tree, head_tree = _tree(root, base_commit), _tree(root, head)

    try:
        base_manifest = _load_json(base_tree, MANIFEST)
        head_manifest = _load_json(head_tree, MANIFEST)
        base_contract, head_contract = _load_contract(base_tree), _load_contract(head_tree)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        result.errors.append(f"a substrate file does not parse: {exc}")
        return result
    if head_manifest is None:
        result.errors.append(f"{MANIFEST} is missing")
        return result

    changed = _changed_paths(root, base_commit, head)
    head_records, record_errors = _load_records(head_tree)
    result.errors.extend(record_errors)
    result.findings, notes = diff_contracts(base_contract, head_contract)
    result.notes.extend(notes)

    record_paths = {
        path: status
        for path, status in changed.items()
        if path.startswith(f"{RECORDS_DIR}/") and Path(path).name not in _NON_RECORD_FILES
    }
    added = sorted(path for path, status in record_paths.items() if status == "A")
    deleted = sorted(path for path, status in record_paths.items() if status == "D")

    known_releases = {str(entry["release"]) for entry in release_entries(base_manifest)}
    newest = head_release(head_manifest)
    if newest is not None and newest not in known_releases:
        _check_release(
            result, base_tree, base_manifest, head_manifest, head_contract, head_records, newest
        )
        return result

    result.records_added = added
    result.records = [head_records.get(Path(path).name) for path in added]
    _check_change(result, base_manifest, head_manifest, base_contract, head_contract)
    for path in deleted:
        result.errors.append(f"{path}: a record is deleted only by the release that folds it")

    triggers = sorted(
        path
        for path in changed
        if path in _listed(base_manifest) | _listed(head_manifest) | set(SPEC_PATHS)
    )
    if triggers and not added:
        result.errors.append(
            "change record required: this change touches the published contract ("
            + ", ".join(triggers)
            + f") but adds no change record under {RECORDS_DIR}/; see "
            "docs/fs/changes/README.md"
        )

    valid = [r for r in result.records if isinstance(r, dict)]
    minor = any(r.get("classification") == "minor" for r in valid)
    result.classification = "minor" if minor else "patch"
    result.categories = _categories(valid)

    if result.findings and valid:
        if not minor:
            if any(r.get("detector_override") for r in valid):
                result.notes.append(
                    "contract findings are downgraded to patch by the owner's detector_override"
                )
            else:
                result.errors.append(
                    "the contract comparison finds a minor-level change, but every added record "
                    "classifies it patch; classify it minor, or record the owner's "
                    "detector_override with a reason:\n"
                    + "\n".join(f"  {f.render()}" for f in result.findings)
                )
        else:
            missing = sorted({f.category for f in result.findings} - set(result.categories))
            if missing:
                result.warnings.append(
                    "the contract comparison finds "
                    + " and ".join(missing)
                    + " change that no added record names in its category"
                )
    return result


def _check_change(
    result: CheckResult,
    base_manifest: dict[str, Any] | None,
    head_manifest: dict[str, Any],
    base_contract: Contract,
    head_contract: Contract,
) -> None:
    """A change that is not a release leaves every version and the history alone."""
    legacy = base_manifest is not None and LEGACY_COUNTER in base_manifest
    if legacy or base_manifest is None:
        return
    if LEGACY_COUNTER in head_manifest:
        result.errors.append(
            f"{MANIFEST}: {LEGACY_COUNTER} is retired (CAS-ADR-008); "
            "versions move only in a release"
        )
    base_versions, head_versions = _versions(base_manifest), _versions(head_manifest)
    for path in sorted(set(base_versions) & set(head_versions)):
        if base_versions[path] != head_versions[path]:
            result.errors.append(
                f"{SUBSTRATE_DIR}/{path}: manifest version moved from {base_versions[path]!r} to "
                f"{head_versions[path]!r}; only a release moves versions"
            )
    for path in sorted(set(head_versions) - set(base_versions)):
        if head_versions[path] is not None:
            result.errors.append(
                f"{SUBSTRATE_DIR}/{path}: a newly listed artifact carries version null until a "
                "release publishes it"
            )
    for rel, old_spec, new_spec in (
        (CORE_SPEC, base_contract.sage_core_api, head_contract.sage_core_api),
        (APP_SPEC, base_contract.cas_app_api, head_contract.cas_app_api),
    ):
        if old_spec is not None and new_spec is not None:
            if _info_version(old_spec) != _info_version(new_spec):
                result.errors.append(
                    f"{rel}: info.version moved from {_info_version(old_spec)!r} to "
                    f"{_info_version(new_spec)!r}; only a release moves the contract version"
                )
    if base_manifest.get("revision_history") != head_manifest.get("revision_history"):
        result.errors.append(
            f"{MANIFEST}: revision_history changed; only a release adds to it, and existing "
            "entries are never rewritten"
        )


def _check_release(
    result: CheckResult,
    base_tree: _Tree,
    base_manifest: dict[str, Any] | None,
    head_manifest: dict[str, Any],
    head_contract: Contract,
    head_records: Mapping[str, Any],
    release: str,
) -> None:
    """A release folds exactly the records it deletes and changes no contract."""
    result.classification = "release"
    result.release = release
    base_records, _ = _load_records(base_tree)

    if head_records:
        result.errors.append(
            f"release {release} leaves unreleased records behind: "
            + ", ".join(sorted(head_records))
        )
    head_entry = head_manifest["revision_history"][0]
    expected = [base_records[name] for name in sorted(base_records)]
    if head_entry.get("changes") != expected:
        result.errors.append(
            f"release {release} folds changes that differ from the unreleased records it deletes"
        )
    if (head_manifest.get("revision_history") or [])[1:] != (
        (base_manifest or {}).get("revision_history") or []
    ):
        result.errors.append(
            f"release {release} rewrites revision_history beyond adding its own entry"
        )
    if result.findings:
        result.errors.append(
            f"release {release} changes the contract; a release moves only versions and history:\n"
            + "\n".join(f"  {f.render()}" for f in result.findings)
        )
    for rel, spec in (
        (CORE_SPEC, head_contract.sage_core_api),
        (APP_SPEC, head_contract.cas_app_api),
    ):
        if spec is not None and _info_version(spec) != release:
            result.errors.append(
                f"{rel}: info.version is {_info_version(spec)!r}; release {release} declares it"
            )


# ---------------------------------------------------------------------------
# The contract-version rule
# ---------------------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def contract_version_ok(
    api_version: str,
    spec_versions: Mapping[str, Any],
    release: str | None,
    *,
    tag_exists: bool,
) -> list[str]:
    """Why the declared contract versions disagree with the build, if they do.

    Between releases every specification declares the version the build derives
    from its tags. Once a release change has landed, the specifications declare
    that release, and the build may still derive the previous version -- but only
    until the release's tag exists. A tag that exists yet is not what the build
    derives, or a release behind the derived version, means the release step and
    the tags have come apart.
    """
    expected = release or api_version
    errors = [
        f"{label}.openapi.yaml declares info.version {version!r}; expected {expected!r}"
        for label, version in spec_versions.items()
        if version != expected
    ]
    if release is not None and release != api_version:
        if tag_exists:
            errors.append(
                f"tag v{release}.0 exists, but this build derives {api_version!r} from its tags; "
                "the release commit is not an ancestor of this build"
            )
        elif _version_tuple(release) <= _version_tuple(api_version):
            errors.append(
                f"the manifest's newest release {release!r} is behind the derived version "
                f"{api_version!r}; a release was tagged without the release step"
            )
    return errors


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@dataclass
class ReleaseStatus:
    """What the next release would publish."""

    latest_tag: str | None
    pending_release: str | None
    records: dict[str, Any]
    record_errors: list[str]
    minor_due: bool
    findings: list[Finding]
    notes: list[str]

    def render(self) -> str:
        lines = [f"last release tag: {self.latest_tag or 'none'}"]
        if self.pending_release:
            lines.append(
                f"release {self.pending_release} has landed without its tag; run release-tag"
            )
        lines.append(f"unreleased records: {len(self.records)}")
        for name, record in self.records.items():
            classification = record.get("classification") if isinstance(record, dict) else "?"
            lines.append(f"  {name}: {classification}")
        lines.append("minor release due: " + ("yes" if self.minor_due else "no"))
        lines.extend(
            f"contract finding since {self.latest_tag}: {f.render()}" for f in self.findings
        )
        lines.extend(f"note: {note}" for note in self.notes)
        lines.extend(f"error: {error}" for error in self.record_errors)
        return "\n".join(lines)


def _latest_tag(root: Path, revision: str) -> str | None:
    try:
        return _git(
            root, "describe", "--abbrev=0", "--tags", "--match", RELEASE_TAG_MATCH, revision
        ).strip()
    except GitError:
        return None


def release_status(repo_root: Path | str, ref: str | None = None) -> ReleaseStatus:
    root = Path(repo_root)
    tree = _tree(root, ref)
    latest = _latest_tag(root, ref or "HEAD")
    manifest = _load_json(tree, MANIFEST)
    newest = head_release(manifest)
    pending = newest if newest is not None and tag_exists(root, newest) is False else None
    records, record_errors = _load_records(tree)
    minor_due = any(
        isinstance(r, dict) and r.get("classification") == "minor" for r in records.values()
    )
    findings: list[Finding] = []
    notes: list[str] = []
    if latest is not None:
        findings, notes = diff_contracts(_load_contract(_tree(root, latest)), _load_contract(tree))
    return ReleaseStatus(latest, pending, records, record_errors, minor_due, findings, notes)


# ---------------------------------------------------------------------------
# release-prepare
# ---------------------------------------------------------------------------

_INFO_KEY: Final[re.Pattern[str]] = re.compile(r"^info:\s*$")
_TOP_LEVEL_KEY: Final[re.Pattern[str]] = re.compile(r"^[^\s#]")
_VERSION_LINE: Final[re.Pattern[str]] = re.compile(
    r"""^(?P<lead>\s+version:\s*)(?P<quote>["']?)(?P<value>[^"'\s#]+)(?P=quote)(?P<trail>\s*)$"""
)


def set_info_version(text: str, version: str) -> str:
    """The specification text with ``info.version`` replaced and nothing else.

    A targeted line edit rather than a re-dump, so comments, quoting, and line
    layout survive the release untouched.
    """
    lines = text.splitlines(keepends=True)
    in_info = False
    for index, line in enumerate(lines):
        if _INFO_KEY.match(line):
            in_info = True
            continue
        if in_info and _TOP_LEVEL_KEY.match(line):
            break
        if in_info:
            match = _VERSION_LINE.match(line.rstrip("\r\n"))
            if match:
                quote = match.group("quote") or '"'
                ending = line[len(line.rstrip("\r\n")) :]
                lines[index] = f"{match.group('lead')}{quote}{version}{quote}{ending}"
                updated = "".join(lines)
                before, after = yaml.safe_load(text), yaml.safe_load(updated)
                after_info = dict(after.get("info") or {})
                if after_info.get("version") != version:
                    break
                after_info["version"] = before["info"]["version"]
                if {**after, "info": after_info} != before:
                    break
                return updated
    raise ReleaseRefused("could not locate a single info.version line to update")


def _tag_version(tag: str) -> tuple[int, int]:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.0", tag)
    if match is None:
        raise ReleaseRefused(f"the last release tag {tag!r} is not of the form vMAJOR.MINOR.0")
    return int(match.group(1)), int(match.group(2))


def release_prepare(
    repo_root: Path | str, *, today: dt.date | None = None, major: bool = False
) -> str:
    """Fold the unreleased records into a new release; returns its version."""
    root = Path(repo_root)
    date = (today or dt.date.today()).isoformat()

    branch = _git(root, "branch", "--show-current").strip()
    if not branch or branch in DEFAULT_BRANCHES:
        raise ReleaseRefused(
            f"release-prepare runs on a release branch, not on {branch or 'a detached HEAD'}"
        )
    if _git(root, "status", "--porcelain").strip():
        raise ReleaseRefused("the working tree has uncommitted changes; commit or stash them first")

    status = release_status(root)
    if status.latest_tag is None:
        raise ReleaseRefused(f"no {RELEASE_TAG_MATCH} release tag is reachable from HEAD")
    if status.pending_release is not None:
        raise ReleaseRefused(
            f"release {status.pending_release} has landed without its tag "
            f"v{status.pending_release}.0; run release-tag before preparing another"
        )
    if status.record_errors:
        raise ReleaseRefused("unreleased records are invalid:\n" + "\n".join(status.record_errors))
    if not major and not status.minor_due:
        if status.findings:
            raise ReleaseRefused(
                f"the contract comparison against {status.latest_tag} finds minor-level change, "
                "but no unreleased record is classified minor:\n"
                + "\n".join(f"  {f.render()}" for f in status.findings)
            )
        raise ReleaseRefused("no minor release is due: no unreleased record is classified minor")

    last_major, last_minor = _tag_version(status.latest_tag)
    release = f"{last_major + 1}.0" if major else f"{last_major}.{last_minor + 1}"

    changed = set(
        _git(root, "diff", "--name-only", status.latest_tag, "HEAD", "--", SUBSTRATE_DIR).split()
    )
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    for entry in manifest["schemas"]:
        rel = f"{SUBSTRATE_DIR}/{entry['path']}"
        if rel in changed or rel in SPEC_PATHS:
            entry["version"] = release
    manifest["revision_history"].insert(
        0,
        {
            "release": release,
            "date": date,
            "changes": [status.records[name] for name in sorted(status.records)],
        },
    )
    manifest["manifest_date"] = date

    spec_texts = {
        rel: set_info_version((root / rel).read_text(encoding="utf-8"), release)
        for rel in SPEC_PATHS
    }

    (root / MANIFEST).write_text(render_manifest(manifest), encoding="utf-8")
    for rel, text in spec_texts.items():
        (root / rel).write_text(text, encoding="utf-8")
    for name in status.records:
        (root / RECORDS_DIR / name).unlink()
    return release


# ---------------------------------------------------------------------------
# release-tag
# ---------------------------------------------------------------------------


def _tag_message(release: str, entry: Mapping[str, Any]) -> str:
    changes = [c for c in entry.get("changes") or [] if isinstance(c, dict)]
    notes = [c["for_callers"] for c in changes if c.get("for_callers")]
    lines = [f"CAS {release}.0", ""]
    if notes:
        lines.append("For callers and operators:")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
    lines.append(
        f"{len(changes)} change(s); the release's entry in {MANIFEST} revision_history lists each."
    )
    return "\n".join(lines) + "\n"


def release_tag(repo_root: Path | str, *, ref: str = "HEAD", push: bool = False) -> str:
    """Tag the commit that introduced the newest release entry; returns the tag."""
    root = Path(repo_root)
    manifest = _load_json(_RevisionTree(root, ref), MANIFEST)
    release = head_release(manifest)
    if release is None:
        raise ReleaseRefused("no release entry heads revision_history; there is nothing to tag")
    tag = f"v{release}.0"
    if tag_exists(root, release):
        raise ReleaseRefused(f"{tag} already exists")

    introducing: str | None = None
    for commit in _git(root, "rev-list", "--first-parent", ref, "--", MANIFEST).split():
        if head_release(_load_json(_RevisionTree(root, commit), MANIFEST)) != release:
            break
        introducing = commit
    if introducing is None:
        raise ReleaseRefused(
            f"no commit on the first-parent history of {ref} introduced release {release}"
        )

    _git(
        root,
        "tag",
        "-a",
        tag,
        "-m",
        _tag_message(release, manifest["revision_history"][0]),
        introducing,
    )
    if push:
        _git(root, "push", "origin", tag)
    return tag


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT, help="repository to operate on"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="the change-record gate")
    check.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    check.add_argument("--base", help="base revision; the comparison uses its merge base with head")
    check.add_argument("--head", help="head revision; the working tree when omitted")
    check.add_argument("--format", choices=("text", "json"), default="text")

    status = commands.add_parser("status", help="what the next release would publish")
    status.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    status.add_argument("--ref", help="revision to report on; the working tree when omitted")
    status.add_argument("--format", choices=("text", "json"), default="text")

    prepare = commands.add_parser("release-prepare", help="fold records into a new release")
    prepare.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    prepare.add_argument("--major", action="store_true", help="release a new major version")

    tag = commands.add_parser("release-tag", help="tag a landed release")
    tag.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    tag.add_argument("--ref", default="HEAD", help="revision whose newest release is tagged")
    tag.add_argument("--push", action="store_true", help="push the tag to origin")

    args = parser.parse_args(argv)
    root: Path = args.repo_root

    if args.command == "check":
        result = run_check(root, base=args.base, head=args.head)
        if args.format == "json":
            print(json.dumps(result.to_json(), indent=2, ensure_ascii=False))
        else:
            print(result.render())
        return 1 if result.errors else 0

    if args.command == "status":
        report = release_status(root, ref=args.ref)
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "latest_tag": report.latest_tag,
                        "pending_release": report.pending_release,
                        "records": report.records,
                        "record_errors": report.record_errors,
                        "minor_due": report.minor_due,
                        "findings": [f.__dict__ for f in report.findings],
                        "notes": report.notes,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(report.render())
        return 0

    try:
        if args.command == "release-prepare":
            release = release_prepare(root, major=args.major)
            print(
                f"prepared release {release}: commit this change on the release branch and land "
                "it; then run release-tag on the default branch"
            )
        else:
            print(f"created {release_tag(root, ref=args.ref, push=args.push)}")
    except ReleaseRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
