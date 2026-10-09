"""The local launcher runs the Hugging Face client offline once pinned models are cached.

Every pinned model and remote-code snapshot the stack will load is looked up
in the local Hub cache. When all are present the launcher sets
``HF_HUB_OFFLINE`` before any Hub client is imported, so an established
installation makes no network request at startup; a first run, with something
missing, stays online to fetch it.
"""

import sys

import pytest

from sage.adapters import hub_cache
from sage.adapters.embedding_nomic import MODEL_PINS, NOMIC_MODEL_NAME
from sage.config import SageCoreConfig, StackAbstractionConfig

PIN = MODEL_PINS[NOMIC_MODEL_NAME]
MLX_MODEL = "mlx-community/Example-4bit"
MLX_REVISION = "8b2b98c00a6b4d291155e4890773ca8f769aee53"

_NOMIC_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "modules.json",
    "1_Pooling/config.json",
)
_MLX_FILES = ("config.json", "model.safetensors", "tokenizer.json")


def _snapshot(cache, repo: str, revision: str, files) -> None:
    root = cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def _populate(cache, *, mlx: bool = True, code: bool = True, nomic: bool = True) -> None:
    if nomic:
        _snapshot(cache, NOMIC_MODEL_NAME, PIN.revision, _NOMIC_FILES)
    if code:
        _snapshot(cache, PIN.code_repo, PIN.code_revision, tuple(PIN.code_sha256))
    if mlx:
        _snapshot(cache, MLX_MODEL, MLX_REVISION, _MLX_FILES)


def _mlx_stack(**overrides) -> SageCoreConfig:
    fields = {"provider": "local-mlx", "model": MLX_MODEL, "revision": MLX_REVISION}
    fields.update(overrides)
    return SageCoreConfig(abstraction=StackAbstractionConfig(**fields))


def _clear(monkeypatch, *names: str) -> None:
    """Unset *names*, registering each for restoration even if it was unset,
    since the helper under test writes ``HF_HUB_OFFLINE`` directly."""
    for name in names:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def cache(monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    _clear(monkeypatch, "HF_HUB_OFFLINE", "HF_HOME", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME")
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    return hub


def test_all_pinned_snapshots_cached_sets_offline(monkeypatch, cache):
    _populate(cache)
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is True
    assert hub_cache.os.environ["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param({"nomic": False}, id="embedding-weights"),
        pytest.param({"code": False}, id="remote-code"),
        pytest.param({"mlx": False}, id="abstraction-weights"),
    ],
)
def test_any_pinned_snapshot_missing_stays_online(monkeypatch, cache, missing):
    _populate(cache, **missing)
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is False
    assert "HF_HUB_OFFLINE" not in hub_cache.os.environ


def test_a_snapshot_at_another_revision_does_not_count(monkeypatch, cache):
    """Only the pinned commit satisfies the check; a cached older commit does not."""
    _populate(cache, mlx=False)
    _snapshot(cache, MLX_MODEL, "0" * 40, _MLX_FILES)
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is False


def test_a_partial_snapshot_does_not_count(monkeypatch, cache):
    """A snapshot directory missing a file the load needs is not cached, even
    with its weights present."""
    _populate(cache, mlx=False)
    _snapshot(cache, MLX_MODEL, MLX_REVISION, ("config.json", "model.safetensors"))
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is False


def test_a_snapshot_without_weights_does_not_count(monkeypatch, cache):
    """Configuration and tokenizer without any weight shard is not cached."""
    _populate(cache, mlx=False)
    _snapshot(cache, MLX_MODEL, MLX_REVISION, ("config.json", "tokenizer.json"))
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is False


def test_explicit_setting_is_never_overridden(monkeypatch, cache):
    _populate(cache)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack())
    assert hub_cache.os.environ["HF_HUB_OFFLINE"] == "0"


def test_stub_abstraction_needs_only_the_embedding_snapshots(monkeypatch, cache):
    _populate(cache, mlx=False)
    stack = SageCoreConfig(abstraction=StackAbstractionConfig(provider="stub"))
    assert hub_cache.prefer_offline_when_pinned_models_cached(stack) is True


def test_local_directory_abstraction_model_is_not_looked_up(monkeypatch, cache, tmp_path):
    _populate(cache, mlx=False)
    assert hub_cache.prefer_offline_when_pinned_models_cached(
        _mlx_stack(model=str(tmp_path), revision=None)
    )


def test_hf_home_locates_the_cache_when_hub_cache_is_unset(monkeypatch, tmp_path):
    _clear(monkeypatch, "HF_HUB_OFFLINE", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    _populate(tmp_path / "home" / "hub")
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is True


def test_cache_location_expands_environment_references(monkeypatch, tmp_path):
    """A cache location written with an environment reference resolves as the
    Hub client resolves it."""
    _clear(monkeypatch, "HF_HUB_OFFLINE", "HF_HOME", "HUGGINGFACE_HUB_CACHE")
    monkeypatch.setenv("CACHE_PARENT", str(tmp_path))
    monkeypatch.setenv("HF_HUB_CACHE", "$CACHE_PARENT/hub")
    _populate(tmp_path / "hub")
    assert hub_cache.prefer_offline_when_pinned_models_cached(_mlx_stack()) is True


def test_launcher_decides_before_building_the_app(monkeypatch, cache):
    """The launcher sets offline mode before the app (and any Hub client) is built."""
    import sage.__main__ as launcher

    _populate(cache)
    seen: list[str | None] = []

    def _create_app(**kwargs):
        seen.append(hub_cache.os.environ.get("HF_HUB_OFFLINE"))
        raise SystemExit(0)

    monkeypatch.setattr("sys.argv", ["sage", "--vault-root", "/tmp/x"])
    monkeypatch.setattr(launcher, "load_stack_config_or_default", _mlx_stack)
    monkeypatch.setattr(launcher, "create_app", _create_app)
    with pytest.raises(SystemExit):
        launcher.main()
    assert seen == ["1"]


def test_launcher_module_imports_no_hub_client():
    """Offline mode is read when the Hub client is imported, so the launcher
    must not import one before it decides."""
    import subprocess

    hub_clients = ("huggingface_hub", "transformers", "sentence_transformers", "mlx_lm")
    code = (
        "import sys, sage.__main__; "
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {hub_clients!r}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
