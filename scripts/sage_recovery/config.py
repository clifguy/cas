"""Explicit private configuration. Importing the package reads no configuration."""

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


@dataclasses.dataclass(frozen=True)
class Settings:
    config_path: Path
    base_url: str
    audience: str
    subscription: str
    resource_group: str
    app: str
    revision: str
    image: str
    vault_id: str
    indexed_document: str
    expected_ids: tuple[str, ...]
    six_ids: tuple[str, ...]
    root: Path
    evidence_out: Path
    token_file: Path
    manifest: Path
    ledger: Path
    manifest_sha256: str
    ledger_sha256: str
    control_enabled: bool = False

    @property
    def fingerprint(self):
        """Bind all effective target/source/path settings; execution consent is transient."""
        values = dataclasses.asdict(self)
        values.pop("control_enabled")
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def native_scope(self):
        return f"{self.subscription}/{self.resource_group}/{self.app}"


_current: Settings | None = None


def settings():
    if _current is None:
        raise ValueError("Explicit recovery configuration is required")
    return _current


def configure(value):
    """Install an already validated configuration for this process only."""
    global _current
    _current = value


def private_path(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Private paths must be absolute and must not be symlinks")
    path = path.resolve()
    if any((parent / ".git").exists() for parent in [path, *path.parents]):
        raise ValueError("Credentials, manifests and runtime evidence must be outside Git")
    return path


def load(path, *, control=False):
    """Validate all target and source pins before allowing any external operation."""
    path = private_path(path)
    raw = json.loads(path.read_text())
    required = {f.name for f in dataclasses.fields(Settings)} - {
        "config_path",
        "control_enabled",
    }
    if set(raw) != required:
        raise ValueError("Recovery configuration has missing or unknown fields")
    origin = urlsplit(raw["base_url"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path
        or origin.query
        or origin.fragment
    ):
        raise ValueError("base_url must be one explicit HTTPS origin without a trailing slash")
    for key in [
        "subscription",
        "resource_group",
        "app",
        "revision",
        "vault_id",
        "indexed_document",
    ]:
        if not isinstance(raw[key], str) or not re.fullmatch(
            r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", raw[key]
        ):
            raise ValueError("Invalid target identifier: " + key)
    if not raw["revision"].startswith(raw["app"] + "--"):
        raise ValueError("Pinned revision does not belong to the configured app")
    if not isinstance(raw["audience"], str) or not raw["audience"] or not raw["image"]:
        raise ValueError("Explicit audience and pinned image are required")
    for field, count in [("expected_ids", 16), ("six_ids", 6)]:
        values = raw[field]
        if (
            not isinstance(values, list)
            or len(values) != count
            or any(not isinstance(i, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", i) for i in values)
            or len(set(values)) != count
        ):
            raise ValueError("Invalid exact document population: " + field)
        raw[field] = tuple(values)
    if not set(raw["six_ids"]).issubset(raw["expected_ids"]):
        raise ValueError("The six source records must belong to the original backlog")
    for key in ["root", "evidence_out", "token_file", "manifest", "ledger"]:
        raw[key] = private_path(raw[key])
    roots = [raw["root"], raw["evidence_out"]]
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        raise ValueError("State and export directories must be disjoint")
    inputs = [path, raw["token_file"], raw["manifest"], raw["ledger"]]
    if len(set(inputs)) != len(inputs):
        raise ValueError("Private input files must have distinct paths")
    if any(item.is_relative_to(root) for item in inputs for root in roots):
        raise ValueError("Private inputs must be outside state and export directories")
    for key in ["manifest", "ledger"]:
        digest = raw[key + "_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid approved digest: " + key)
        if hashlib.sha256(raw[key].read_bytes()).hexdigest() != digest:
            raise ValueError("Original source pin mismatch: " + key)
    for entry in json.loads(raw["manifest"].read_text()):
        if isinstance(entry, dict) and entry.get("source"):
            source = private_path(entry["source"])
            if any(source.is_relative_to(root) for root in roots) or source in inputs:
                raise ValueError(
                    "Original CSV inputs must be outside state/export and other inputs"
                )
    if not raw["root"].is_dir() or not raw["evidence_out"].is_dir():
        raise ValueError("Create private work and export directories explicitly before use")
    return Settings(config_path=path, control_enabled=control, **raw)


def bound(value):
    """Stamp an observation with its effective immutable configuration identity."""
    return {**value, "configuration_sha256": settings().fingerprint}


def require_binding(value):
    if value.get("configuration_sha256") != settings().fingerprint:
        raise ValueError("Observation belongs to another recovery configuration")
