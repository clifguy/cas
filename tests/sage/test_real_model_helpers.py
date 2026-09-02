"""The opt-in real-model tier: gate predicate, wiring, model id, lock, and release.

The tests that load the real Qwen3 MLX model are the only expensive tests in
the suite: minutes of wall-clock and most of the machine's memory.
``tests/helpers/real_models.py`` keeps them out of the default run behind
``SAGE_TEST_REAL_MODELS=1``, serializes them machine-wide with a lock file,
and releases the model at fixture teardown. These tests exercise that helper
with fakes and a private lock file -- no weights are loaded and the real lock
is never touched, so they run in the default tier alongside a real-model run.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sage.config import load_sage_core_config
from tests.helpers.real_models import (
    REAL_MODELS_ENV,
    REAL_MODELS_SKIP_REASON,
    loaded_provider,
    real_model_lock,
    real_models_enabled,
    release_provider,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# RM-001 / RM-002: the gate predicate and its skip reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({REAL_MODELS_ENV: "1"}, True),
        ({REAL_MODELS_ENV: "0"}, False),
        ({REAL_MODELS_ENV: ""}, False),
        ({REAL_MODELS_ENV: "true"}, False),
    ],
)
def test_rm_001_real_models_enabled_requires_literal_one(environ, expected):
    assert real_models_enabled(environ) is expected


def test_rm_002_skip_reason_names_the_variable():
    assert f"{REAL_MODELS_ENV}=1" in REAL_MODELS_SKIP_REASON
    assert "opt-in" in REAL_MODELS_SKIP_REASON


# ---------------------------------------------------------------------------
# RM-003 / RM-004: wiring into tests/sage/test_adapters.py
# ---------------------------------------------------------------------------


def _skipif_reasons(cls) -> list[str]:
    return [m.kwargs.get("reason") for m in getattr(cls, "pytestmark", []) if m.name == "skipif"]


@pytest.mark.parametrize("class_name", ["TestQwen3AbstractionProvider", "TestQwen3LazyLoading"])
def test_rm_003_qwen3_classes_carry_the_gate(class_name):
    from tests.sage import test_adapters

    cls = getattr(test_adapters, class_name)
    assert REAL_MODELS_SKIP_REASON in _skipif_reasons(cls), (
        f"{class_name} loads the MLX model but is not gated on {REAL_MODELS_ENV}=1"
    )


def test_rm_003_nomic_class_is_not_gated():
    """The nomic embedding tests are seconds and well under a gigabyte, and CI
    pre-caches the model for them; gating them would take them out of CI
    entirely, since the tier is local-only."""
    from tests.sage import test_adapters

    assert REAL_MODELS_SKIP_REASON not in _skipif_reasons(test_adapters.TestNomicEmbeddingProvider)


def test_rm_004_qwen3_test_model_is_the_committed_default():
    """The real-model tier exercises the model SAGE ships, read through the
    validated stack-config loader; a literal pin drifts the moment the default
    moves."""
    from tests.sage import test_adapters

    committed = load_sage_core_config(PROJECT_ROOT / "sage" / "config.yaml")
    assert committed.abstraction.provider == "local-mlx"
    assert committed.abstraction.model
    assert test_adapters.committed_qwen3_model_id() == committed.abstraction.model


def test_rm_004_non_mlx_provider_is_refused(monkeypatch):
    """A committed switch to a hosted provider must not hand that model id to MLX."""
    from tests.sage import test_adapters

    hosted = SimpleNamespace(abstraction=SimpleNamespace(provider="anthropic", model="claude-x"))
    monkeypatch.setattr(test_adapters, "load_sage_core_config", lambda _path: hosted)
    with pytest.raises(RuntimeError, match="local-mlx"):
        test_adapters.committed_qwen3_model_id()


# ---------------------------------------------------------------------------
# RM-005 / RM-007 / RM-008: the machine-wide lock, on a private lock file
# ---------------------------------------------------------------------------


def _lock_is_free(path: Path) -> bool:
    """Probe the lock from a second file description (flock contends across
    descriptions, so one process is enough to observe exclusion)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_rm_005_real_model_lock_excludes_a_second_holder(tmp_path):
    lock = tmp_path / "lock"
    assert _lock_is_free(lock)
    with real_model_lock(lock):
        assert not _lock_is_free(lock)
    assert _lock_is_free(lock)


class _FakeProvider:
    def __init__(self) -> None:
        self.unload_calls = 0

    async def unload(self) -> bool:
        self.unload_calls += 1
        return True


def test_rm_007_loaded_provider_holds_the_lock_only_while_alive(tmp_path):
    lock = tmp_path / "lock"
    with loaded_provider(_FakeProvider, lock_path=lock) as provider:
        assert isinstance(provider, _FakeProvider)
        assert not _lock_is_free(lock)
    assert _lock_is_free(lock)


def test_rm_008_in_process_reentry_raises_instead_of_blocking(tmp_path):
    """Two real-model fixtures alive in one process would block on the flock
    forever; the guard refuses the second acquire immediately, and the first
    holder keeps its lock."""
    lock = tmp_path / "lock"
    with real_model_lock(lock):
        with pytest.raises(RuntimeError, match="re-entered in-process"):
            with real_model_lock(tmp_path / "other"):
                pass
        assert not _lock_is_free(lock)
    assert _lock_is_free(lock)


# ---------------------------------------------------------------------------
# RM-006: release on exit, including the error path
# ---------------------------------------------------------------------------


def test_rm_006_loaded_provider_releases_on_clean_exit(tmp_path):
    factory_calls: list[int] = []

    def factory() -> _FakeProvider:
        factory_calls.append(1)
        return _FakeProvider()

    with loaded_provider(factory, lock_path=tmp_path / "lock") as provider:
        assert provider.unload_calls == 0
    assert len(factory_calls) == 1
    assert provider.unload_calls == 1


def test_rm_006_loaded_provider_releases_when_the_body_raises(tmp_path):
    factory_calls: list[int] = []
    lock = tmp_path / "lock"

    def factory() -> _FakeProvider:
        factory_calls.append(1)
        return _FakeProvider()

    with pytest.raises(RuntimeError, match="boom"):
        with loaded_provider(factory, lock_path=lock) as provider:
            raise RuntimeError("boom")
    assert len(factory_calls) == 1
    assert provider.unload_calls == 1
    assert _lock_is_free(lock)


def test_rm_006_release_provider_awaits_unload_and_reports_it():
    provider = _FakeProvider()
    assert release_provider(provider) is True
    assert provider.unload_calls == 1
