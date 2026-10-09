"""Local Hugging Face cache lookup for the pinned models a stack loads.

Every model SAGE loads from the Hugging Face Hub is pinned to a commit, so
once each pinned snapshot is in the local cache there is nothing left to
fetch. The launcher uses this to run the Hub client offline, closing the
network path entirely on an established installation, while a first run,
with a snapshot missing, stays online to fetch it.

Standard library only: the Hub client reads its offline setting when it is
imported, so this has to decide before anything imports it.
"""

import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from sage.adapters.embedding_nomic import MODEL_PINS
from sage.config import SageCoreConfig

logger = logging.getLogger(__name__)

OFFLINE_ENV = "HF_HUB_OFFLINE"

# Files each embedding model snapshot must hold for the module-built load.
_EMBEDDING_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "modules.json",
    "1_Pooling/config.json",
)
# Files an MLX model snapshot must hold beyond its weights.
_MLX_FILES = ("config.json", "tokenizer.json")


def hub_cache_dir() -> Path:
    """The Hub cache directory, resolved as the Hub client resolves it."""
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(name)
        if value:
            return Path(os.path.expandvars(value)).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(os.path.expandvars(home)).expanduser() / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "huggingface" / "hub"


def _snapshot_dir(repo: str, revision: str) -> Path:
    return hub_cache_dir() / f"models--{repo.replace('/', '--')}" / "snapshots" / revision


def _holds(snapshot: Path, files: Iterable[str]) -> bool:
    return all((snapshot / name).is_file() for name in files)


def _pinned_snapshots_cached(stack_config: SageCoreConfig) -> bool:
    for model_name, pin in MODEL_PINS.items():
        if not _holds(_snapshot_dir(model_name, pin.revision), _EMBEDDING_FILES):
            return False
        if not _holds(_snapshot_dir(pin.code_repo, pin.code_revision), pin.code_sha256):
            return False
    abstraction = stack_config.abstraction
    if abstraction.provider == "local-mlx" and abstraction.model is not None:
        if Path(abstraction.model).is_dir():
            return True
        if abstraction.revision is None:
            return False
        snapshot = _snapshot_dir(abstraction.model, abstraction.revision)
        if not _holds(snapshot, _MLX_FILES) or not any(snapshot.glob("model*.safetensors")):
            return False
    return True


def prefer_offline_when_pinned_models_cached(stack_config: SageCoreConfig) -> bool:
    """Set ``HF_HUB_OFFLINE`` when every pinned snapshot is already cached.

    An explicit setting is never overridden. Returns whether the Hub client
    will run offline on this account. Call it before anything imports the
    Hub client, which reads the setting once at import.
    """
    if OFFLINE_ENV in os.environ:
        return os.environ[OFFLINE_ENV].strip().lower() in {"1", "true", "yes", "on"}
    if not _pinned_snapshots_cached(stack_config):
        logger.info("Pinned models not all cached; Hugging Face Hub access stays online")
        return False
    if "huggingface_hub" in sys.modules:
        logger.warning(
            "Pinned models are cached, but the Hugging Face Hub client is already "
            "imported, so offline mode cannot take effect in this process"
        )
    os.environ[OFFLINE_ENV] = "1"
    logger.info("Pinned models cached; Hugging Face Hub access runs offline")
    return True


def pinned_cache_key() -> str:
    """A cache key naming every pinned embedding snapshot.

    A build cache keyed on it is reused only while the pins are unchanged; a
    new pin misses and fetches the new commit rather than restoring a cache
    built for an older one.
    """
    return "-".join(
        f"{name.replace('/', '--')}-{pin.revision}-{pin.code_revision}"
        for name, pin in sorted(MODEL_PINS.items())
    )
