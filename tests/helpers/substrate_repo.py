"""A disposable git repository carrying a Formal Substrate, for release tooling tests.

The change-record gate, the release step, and the conflict property are all
claims about git history: what a change adds relative to its base, which commit
introduced a release, whether two branches merge. They can only be exercised
against a real repository, and never against this one, whose tags and history the
tests must not touch. ``make_substrate_repo`` builds a small repository with the
layout ``scripts/substrate_changes.py`` reads -- a manifest, both OpenAPI
specifications, an MCP catalog, one further schema, and the change-record schema
copied from the real substrate -- commits it on ``main``, and tags it ``v1.0.0``.

Git identity and signing are configured on the repository itself, so a
developer's global configuration (a signing key, a hook path) cannot change what
a test observes.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REAL_SUBSTRATE: Final[Path] = REPO_ROOT / "docs" / "fs"

MANIFEST: Final[str] = "docs/fs/manifest.json"
CORE_SPEC: Final[str] = "docs/fs/sage/sage_core_api.openapi.yaml"
APP_SPEC: Final[str] = "docs/fs/cas_app_api.openapi.yaml"
CATALOG: Final[str] = "docs/fs/sage/sage_mcp_tools.catalog.json"
OTHER_SCHEMA: Final[str] = "docs/fs/sage/other.schema.json"
RECORD_SCHEMA: Final[str] = "docs/fs/changes/change_record.schema.json"
RECORDS_DIR: Final[str] = "docs/fs/changes/unreleased"


def _spec(title: str, path: str) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0", "description": f"{title} fixture."},
        "paths": {
            path: {
                "get": {
                    "summary": "List things.",
                    "operationId": f"list_{title.lower().replace(' ', '_')}",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "description": "Page size.",
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "OK.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Thing"}
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Thing": {
                    "type": "object",
                    "description": "A thing.",
                    "properties": {"id": {"type": "string", "description": "Identifier."}},
                    "required": ["id"],
                }
            }
        },
    }


FIXTURE_CATALOG: Final[dict[str, Any]] = {
    "title": "Fixture catalog",
    "description": "Fixture.",
    "surfaces": {
        "sage": [
            {
                "name": "search",
                "title": None,
                "description": "Search.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "outputSchema": None,
                "annotations": None,
            }
        ],
        "sage_maint": [],
    },
}

FIXTURE_OTHER_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Other",
    "description": "Another substrate schema.",
    "type": "object",
    "properties": {"name": {"type": "string", "description": "Name."}},
}


def _fixture_manifest(*, legacy: bool) -> dict[str, Any]:
    version = "0.1" if legacy else "1.0"
    manifest: dict[str, Any] = {
        "title": "Fixture manifest",
        "description": "Fixture substrate manifest.",
        "manifest_version": "1.0",
        "manifest_date": "2026-01-01",
        "schemas": [
            {"path": "sage/sage_core_api.openapi.yaml", "version": version},
            {"path": "cas_app_api.openapi.yaml", "version": version},
            {"path": "sage/sage_mcp_tools.catalog.json", "version": version},
            {"path": "sage/other.schema.json", "version": version},
            {"path": "changes/change_record.schema.json", "version": version},
        ],
        "deferred": [],
    }
    if legacy:
        manifest["substrate_version"] = "0.1"
    manifest["revision_history"] = [
        {"version": "0.1", "date": "2026-01-01", "summary": "Legacy entry."}
    ]
    return manifest


def render_manifest(manifest: dict[str, Any]) -> str:
    """The manifest's canonical committed text."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


class SubstrateRepo:
    """A scratch repository and the operations the release tooling tests need."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed ({result.returncode}):\n"
                f"{result.stdout}{result.stderr}"
            )
        return result

    def rev(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read_text(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def write_text(self, rel: str, text: str) -> None:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read_json(self, rel: str) -> Any:
        return json.loads(self.read_text(rel))

    def write_json(self, rel: str, data: Any) -> None:
        self.write_text(rel, render_manifest(data))

    def read_yaml(self, rel: str) -> Any:
        return yaml.safe_load(self.read_text(rel))

    def write_yaml(self, rel: str, data: Any) -> None:
        self.write_text(rel, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def delete(self, rel: str) -> None:
        self.path(rel).unlink()

    def edit_yaml(self, rel: str, mutate: Callable[[Any], None]) -> None:
        data = copy.deepcopy(self.read_yaml(rel))
        mutate(data)
        self.write_yaml(rel, data)

    def edit_json(self, rel: str, mutate: Callable[[Any], None]) -> None:
        data = copy.deepcopy(self.read_json(rel))
        mutate(data)
        self.write_json(rel, data)

    def add_record(self, slug: str, **fields: Any) -> str:
        rel = f"{RECORDS_DIR}/{slug}.yaml"
        self.write_yaml(rel, fields)
        return rel

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.rev()

    def checkout(self, branch: str, *, create: bool = False, start: str | None = None) -> None:
        args = ["checkout", "-q"]
        if create:
            args.append("-b")
        args.append(branch)
        if start is not None:
            args.append(start)
        self.git(*args)

    def squash_merge(self, branch: str, message: str) -> str:
        """Land ``branch`` on ``main`` as one commit, as the forge's squash merge does."""
        self.checkout("main")
        self.git("merge", "--squash", "-q", branch)
        return self.commit(message)


def _init(root: Path) -> SubstrateRepo:
    root.mkdir(parents=True, exist_ok=True)
    repo = SubstrateRepo(root)
    repo.git("init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ):
        repo.git("config", key, value)
    return repo


def make_substrate_repo(root: Path, *, legacy: bool = False) -> SubstrateRepo:
    """A repository holding a minimal substrate, committed on ``main`` and tagged ``v1.0.0``."""
    repo = _init(root)
    repo.write_json(MANIFEST, _fixture_manifest(legacy=legacy))
    repo.write_yaml(CORE_SPEC, _spec("Core API", "/things"))
    repo.write_yaml(APP_SPEC, _spec("App API", "/app/things"))
    repo.write_text(CATALOG, json.dumps(FIXTURE_CATALOG, indent=2, sort_keys=True) + "\n")
    repo.write_text(OTHER_SCHEMA, json.dumps(FIXTURE_OTHER_SCHEMA, indent=2) + "\n")
    repo.write_text(
        RECORD_SCHEMA, (REAL_SUBSTRATE / "changes" / "change_record.schema.json").read_text()
    )
    repo.write_text(f"{RECORDS_DIR}/.gitkeep", "")
    repo.write_text("sage/app.py", "VALUE = 1\n")
    repo.commit("initial")
    repo.git("tag", "-a", "v1.0.0", "-m", "Fixture 1.0.0")
    return repo


def make_real_substrate_repo(root: Path, *, legacy: bool = False) -> SubstrateRepo:
    """A repository seeded with the real manifest, specifications, and catalog.

    ``legacy`` reintroduces the retired substrate-wide counter, so the same
    content can be exercised under the scheme it replaced.
    """
    repo = _init(root)
    for rel in (
        "manifest.json",
        "sage/sage_core_api.openapi.yaml",
        "cas_app_api.openapi.yaml",
        "sage/sage_mcp_tools.catalog.json",
        "changes/change_record.schema.json",
    ):
        target = repo.path(f"docs/fs/{rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REAL_SUBSTRATE / rel, target)
    if legacy:
        manifest = repo.read_json(MANIFEST)
        manifest["substrate_version"] = "1.0"
        repo.write_json(MANIFEST, manifest)
    repo.write_text(f"{RECORDS_DIR}/.gitkeep", "")
    repo.commit("initial")
    repo.git("tag", "-a", "v1.0.0", "-m", "Fixture 1.0.0")
    return repo
