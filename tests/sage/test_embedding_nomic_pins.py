"""Revision and remote-code pinning for the nomic embedding provider.

The model's modeling code is executed from a second repository named in its
``auto_map``, so pinning the model revision alone leaves that code floating.
These tests assert what was actually loaded and resolved, not merely what the
provider passed: a construction that hands ``code_revision`` to a loader that
discards it must fail here.
"""

import dataclasses
import hashlib
import inspect
import re
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("sentence_transformers")

import huggingface_hub  # noqa: E402
import transformers.dynamic_module_utils as dynamic_module_utils  # noqa: E402

from sage.adapters import embedding_nomic  # noqa: E402
from sage.adapters.embedding_nomic import (  # noqa: E402
    MODEL_PINS,
    NOMIC_MODEL_NAME,
    NomicEmbeddingProvider,
)

PIN = MODEL_PINS[NOMIC_MODEL_NAME]
MODELING_FILE = "modeling_hf_nomic_bert.py"
CONFIGURATION_FILE = "configuration_hf_nomic_bert.py"


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def resolution_spy(monkeypatch):
    """Record every remote-code file resolution and the revision it used.

    ``get_cached_module_file`` is the step that fetches a remote-code file
    for import; ``get_class_from_dynamic_module`` reaches it through the
    module global, so patching the module attribute intercepts every path.
    """
    calls: list[tuple[str, str, str | None]] = []
    original = dynamic_module_utils.get_cached_module_file

    def spy(repo, module_file, *args, **kwargs):
        calls.append((str(repo), module_file, kwargs.get("revision")))
        return original(repo, module_file, *args, **kwargs)

    monkeypatch.setattr(dynamic_module_utils, "get_cached_module_file", spy)
    return calls


def _pin_with(monkeypatch, **changes) -> None:
    monkeypatch.setitem(MODEL_PINS, NOMIC_MODEL_NAME, dataclasses.replace(PIN, **changes))


# ── The pins themselves ─────────────────────────────────────────────


def test_pins_are_full_commit_hashes_and_digests():
    """Every pin is an immutable identifier: a full commit hash, a full digest."""
    commit = re.compile(r"[0-9a-f]{40}")
    digest = re.compile(r"[0-9a-f]{64}")
    for pin in MODEL_PINS.values():
        assert commit.fullmatch(pin.revision)
        assert commit.fullmatch(pin.code_revision)
        assert set(pin.code_sha256) == {CONFIGURATION_FILE, MODELING_FILE}
        assert all(digest.fullmatch(value) for value in pin.code_sha256.values())


# ── What was actually loaded ────────────────────────────────────────


@pytest.fixture(scope="module")
def provider():
    return NomicEmbeddingProvider()


def test_loaded_modeling_code_is_the_pinned_commit(provider):
    """The executed model and config classes come from the pinned code commit.

    The dynamic module path names the commit the code was fetched at, and the
    file it was imported from hashes to the pinned digest.
    """
    model_cls = type(provider._model[0].auto_model)
    config_cls = type(provider._model[0].auto_model.config)
    for cls, filename in ((model_cls, MODELING_FILE), (config_cls, CONFIGURATION_FILE)):
        assert PIN.code_revision in cls.__module__.split("."), cls.__module__
        assert _sha256(inspect.getfile(cls)) == PIN.code_sha256[filename]


def test_every_remote_code_resolution_uses_the_pinned_code_revision(resolution_spy):
    """Each remote-code file is resolved at the pinned code revision.

    Passing ``code_revision`` through a loader that drops it before the model
    load leaves the modeling file resolved at the code repository's default
    branch while the configuration file is pinned. This test sees that.
    """
    NomicEmbeddingProvider()
    resolved = [c for c in resolution_spy if c[0] == PIN.code_repo]
    assert {c[1] for c in resolved} >= {CONFIGURATION_FILE, MODELING_FILE}
    assert all(c[2] == PIN.code_revision for c in resolved), resolved


def test_pooling_matches_the_pinned_snapshot(provider):
    """The pipeline is the pinned snapshot's Transformer + mean Pooling, nothing else."""
    modules = list(provider._model)
    assert [type(m).__name__ for m in modules] == ["Transformer", "Pooling"]
    assert modules[1].get_config_dict()["pooling_mode"] == "mean"
    assert provider._model.max_seq_length == embedding_nomic.MAX_INPUT_TOKENS


# ── Refusals ────────────────────────────────────────────────────────


def test_unavailable_code_revision_refuses_the_load(monkeypatch, resolution_spy):
    """A code revision that cannot be fetched refuses before the modeling file is
    resolved for import. The digest comparison itself is held by the tampered-
    digest and tampered-copy tests below."""
    _pin_with(monkeypatch, code_revision="0" * 40)
    with pytest.raises(RuntimeError, match=NOMIC_MODEL_NAME):
        NomicEmbeddingProvider()
    assert not [c for c in resolution_spy if c[1] == MODELING_FILE]


def test_tampered_code_digest_refuses_before_import(monkeypatch, resolution_spy):
    """A remote-code file whose digest differs from the pin is never imported."""
    digests = dict(PIN.code_sha256, **{MODELING_FILE: "0" * 64})
    _pin_with(monkeypatch, code_sha256=digests)
    with pytest.raises(RuntimeError, match="SHA-256"):
        NomicEmbeddingProvider()
    assert resolution_spy == []


def test_missing_code_file_refuses_before_import(monkeypatch, resolution_spy):
    """A pinned remote-code file that cannot be found refuses the load."""
    digests = dict(PIN.code_sha256, **{"absent_module.py": "0" * 64})
    _pin_with(monkeypatch, code_sha256=digests)
    with pytest.raises(RuntimeError, match="absent_module.py"):
        NomicEmbeddingProvider()
    assert resolution_spy == []


def test_tampered_imported_copy_refuses_before_import(monkeypatch, tmp_path, resolution_spy):
    """A modified copy in the dynamic-modules cache is refused, not imported.

    For a Hub repository the loader reuses an existing copy at the pinned
    commit without comparing it to the snapshot, so the copy is what runs.
    """
    import transformers.utils.hub as transformers_hub

    copy_dir = (
        tmp_path
        / "transformers_modules"
        / "nomic_hyphen_ai"
        / "nomic_hyphen_bert_hyphen_2048"
        / PIN.code_revision
    )
    copy_dir.mkdir(parents=True)
    (copy_dir / MODELING_FILE).write_text("# not the pinned code\n")
    monkeypatch.setattr(transformers_hub, "HF_MODULES_CACHE", str(tmp_path))
    with pytest.raises(RuntimeError, match="SHA-256"):
        NomicEmbeddingProvider()
    assert resolution_spy == []


def test_unpinned_model_name_refuses_before_any_hub_access(monkeypatch):
    """A model with no pin is refused without contacting the Hub."""
    hub_calls: list[object] = []
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *a, **k: hub_calls.append(a))
    with pytest.raises(RuntimeError, match="some-org/unpinned-model"):
        NomicEmbeddingProvider(model_name="some-org/unpinned-model")
    assert hub_calls == []


def _pipeline_with_classes_from(module_name: str) -> list[object]:
    class Config:
        pass

    class Model:
        config = Config()

    for cls in (Model, Config):
        cls.__module__ = module_name
    return [type("Module", (), {"auto_model": Model()})()]


def test_post_load_check_refuses_genuine_code_imported_under_another_commit(monkeypatch):
    """The pinned file's bytes imported under a different commit's module path are refused."""
    module_name = "transformers_modules.org.repo.main.modeling_hf_nomic_bert"
    genuine = types.ModuleType(module_name)
    genuine.__file__ = huggingface_hub.hf_hub_download(
        PIN.code_repo, MODELING_FILE, revision=PIN.code_revision
    )
    monkeypatch.setitem(sys.modules, module_name, genuine)
    with pytest.raises(RuntimeError, match="is not from"):
        embedding_nomic._verify_loaded_code(_pipeline_with_classes_from(module_name), PIN)


def test_post_load_check_refuses_a_pinned_file_name_with_other_bytes(monkeypatch, tmp_path):
    """A class imported from a file carrying a pinned name and the pinned commit
    in its module path, but other bytes, is refused after load."""
    module_name = f"transformers_modules.org.repo.{PIN.code_revision}.modeling_hf_nomic_bert"
    impostor = tmp_path / MODELING_FILE
    impostor.write_text("# not the pinned code\n")
    module = types.ModuleType(module_name)
    module.__file__ = str(impostor)
    monkeypatch.setitem(sys.modules, module_name, module)
    with pytest.raises(RuntimeError, match="SHA-256"):
        embedding_nomic._verify_loaded_code(_pipeline_with_classes_from(module_name), PIN)


def test_post_load_check_refuses_an_unpinned_file_under_the_pinned_commit(monkeypatch):
    """A class from a file that is not a pinned remote-code file is refused after load."""
    module_name = f"transformers_modules.org.repo.{PIN.code_revision}.unpinned"
    monkeypatch.setitem(sys.modules, module_name, sys.modules[__name__])
    with pytest.raises(RuntimeError, match="is not from"):
        embedding_nomic._verify_loaded_code(_pipeline_with_classes_from(module_name), PIN)
