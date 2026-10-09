"""Revision pinning and code refusal for the local MLX abstraction model.

mlx-lm downloads a repository's Python files and executes the one a model's
``config.json`` names in ``model_file``, with no trust flag. The provider
therefore fetches a pinned commit, inspects the snapshot, and loads only a
snapshot that carries no Python code. No real weights are loaded here: a fake
``mlx_lm`` records what it is asked to load.
"""

import json
import sys
import types
from pathlib import Path

import pytest

from sage.adapters import abstraction_qwen3
from sage.adapters.abstraction_qwen3 import (
    Qwen3AbstractionProvider,
    _reset_qwen3_singleton,
    get_qwen3_abstraction_provider,
)

REVISION = "8b2b98c00a6b4d291155e4890773ca8f769aee53"
MODEL_ID = "mlx-community/Example-4bit"


class _FakeTokenizer:
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
        return "prompt"

    def encode(self, text):
        return [0] * len(text.split())


class _FakeResponse:
    text = "STUB ABSTRACT"
    prompt_tokens = 10
    prompt_tps = 100.0
    generation_tokens = 5
    generation_tps = 50.0


@pytest.fixture
def loads(monkeypatch) -> list[tuple[tuple, dict]]:
    """Install a fake ``mlx_lm`` and record every ``load`` call."""
    calls: list[tuple[tuple, dict]] = []

    def fake_load(*args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(), _FakeTokenizer()

    def fake_stream_generate(model, tokenizer, prompt, **kwargs):
        yield _FakeResponse()

    mlx_lm = types.ModuleType("mlx_lm")
    mlx_lm.load = fake_load
    mlx_lm.stream_generate = fake_stream_generate
    sample_utils = types.ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = lambda temp=0.0: object()
    mlx_lm.sample_utils = sample_utils
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    return calls


def _snapshot(root: Path, config: dict | None = None, extra: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(config or {"model_type": "qwen3"}))
    (root / "model.safetensors").write_bytes(b"\0")
    for name, body in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """Replace the Hub snapshot fetch; record its arguments and serve *tmp_path/snap*."""
    import huggingface_hub

    calls: list[tuple[tuple, dict]] = []
    snapshot = tmp_path / "snap"

    def fake_snapshot_download(*args, **kwargs):
        calls.append((args, kwargs))
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    return types.SimpleNamespace(calls=calls, snapshot=snapshot)


def test_pinned_revision_reaches_the_fetch_and_the_inspected_snapshot_is_loaded(loads, hub):
    """The configured revision is passed to the Hub fetch, and the load reads
    exactly the snapshot directory that was inspected."""
    _snapshot(hub.snapshot)
    provider = Qwen3AbstractionProvider(model_id=MODEL_ID, revision=REVISION)
    provider._ensure_loaded()
    assert [(a, k.get("revision")) for a, k in hub.calls] == [((MODEL_ID,), REVISION)]
    assert loads == [((str(hub.snapshot),), {})]


@pytest.mark.parametrize(
    ("config", "extra"),
    [
        pytest.param(None, {"modeling_example.py": "print('x')\n"}, id="python-file"),
        pytest.param(None, {"sub/helper.py": "pass\n"}, id="nested-python-file"),
        pytest.param({"model_type": "qwen3", "model_file": "anything"}, None, id="model-file-key"),
    ],
)
def test_snapshot_carrying_code_is_refused_before_load(loads, hub, config, extra):
    """A snapshot that could make the loader execute repository code is refused."""
    _snapshot(hub.snapshot, config=config, extra=extra)
    provider = Qwen3AbstractionProvider(model_id=MODEL_ID, revision=REVISION)
    with pytest.raises(RuntimeError, match=MODEL_ID):
        provider._ensure_loaded()
    assert loads == []


def test_hub_model_without_a_revision_is_refused_without_a_fetch(loads, hub):
    """A Hub model id with no pinned revision is refused; nothing is fetched."""
    provider = Qwen3AbstractionProvider(model_id=MODEL_ID)
    with pytest.raises(RuntimeError, match="revision"):
        provider._ensure_loaded()
    assert hub.calls == []
    assert loads == []


def test_local_directory_model_is_inspected_and_loaded_without_a_fetch(loads, hub, tmp_path):
    """A model id naming a local directory is inspected in place; no revision applies."""
    local = _snapshot(tmp_path / "local-model")
    provider = Qwen3AbstractionProvider(model_id=str(local))
    provider._ensure_loaded()
    assert hub.calls == []
    assert loads == [((str(local),), {})]


def test_local_directory_carrying_code_is_refused(loads, hub, tmp_path):
    local = _snapshot(tmp_path / "local-model", extra={"modeling.py": "pass\n"})
    provider = Qwen3AbstractionProvider(model_id=str(local))
    with pytest.raises(RuntimeError, match="Python"):
        provider._ensure_loaded()
    assert loads == []


def test_singleton_refuses_a_different_revision():
    """The process-wide provider will not silently serve a second revision."""
    _reset_qwen3_singleton()
    try:
        get_qwen3_abstraction_provider(model_id=MODEL_ID, revision=REVISION)
        with pytest.raises(RuntimeError, match="revision"):
            get_qwen3_abstraction_provider(model_id=MODEL_ID, revision="0" * 40)
    finally:
        _reset_qwen3_singleton()


def test_mlx_fetch_patterns_cover_python_files():
    """The fetch downloads Python files so the inspection can see them.

    Leaving them out of the fetch would let a later loader fetch of the same
    commit bring them in uninspected.
    """
    assert "*.py" in abstraction_qwen3._MLX_SNAPSHOT_PATTERNS
