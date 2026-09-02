"""The opt-in real-model tier: gate predicate, wiring, model id, lock, and release.

The tests that load real model weights (the Qwen3 MLX abstraction model and the
nomic embedding model) are the only expensive tests in the suite: minutes of
wall-clock and most of the machine's memory. ``tests/helpers/real_models.py``
keeps them out of the default run behind ``SAGE_TEST_REAL_MODELS=1``,
serializes them machine-wide with a lock file, and releases the model at
fixture teardown. These tests exercise that helper with fakes -- no weights are
loaded here, so they run in the default tier.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
import yaml

from tests.helpers.real_models import (
    REAL_MODEL_LOCK_PATH,
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


@pytest.mark.parametrize(
    "class_name",
    ["TestQwen3AbstractionProvider", "TestQwen3LazyLoading", "TestNomicEmbeddingProvider"],
)
def test_rm_003_real_model_classes_carry_the_gate(class_name):
    from tests.sage import test_adapters

    cls = getattr(test_adapters, class_name)
    assert REAL_MODELS_SKIP_REASON in _skipif_reasons(cls), (
        f"{class_name} loads real weights but is not gated on {REAL_MODELS_ENV}=1"
    )


def test_rm_004_qwen3_test_model_is_the_committed_default():
    """The real-model tier exercises the model SAGE ships, read from the one
    place it is declared; a literal pin drifts the moment the default moves."""
    from tests.sage import test_adapters

    committed = yaml.safe_load((PROJECT_ROOT / "sage" / "config.yaml").read_text())
    expected = committed["abstraction"]["model"]
    assert expected, "sage/config.yaml declares no abstraction.model"
    assert test_adapters.QWEN3_MODEL_ID == expected


# ---------------------------------------------------------------------------
# RM-005 / RM-007: the machine-wide lock
# ---------------------------------------------------------------------------


def _lock_is_free() -> bool:
    """Probe the lock from a second file description (flock contends across
    descriptions, so one process is enough to observe exclusion)."""
    fd = os.open(REAL_MODEL_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_rm_005_real_model_lock_excludes_a_second_holder():
    assert _lock_is_free()
    with real_model_lock():
        assert not _lock_is_free()
    assert _lock_is_free()


class _FakeProvider:
    def __init__(self) -> None:
        self.unload_calls = 0

    async def unload(self) -> bool:
        self.unload_calls += 1
        return True


def test_rm_007_loaded_provider_holds_the_lock_only_while_alive():
    with loaded_provider(_FakeProvider) as provider:
        assert isinstance(provider, _FakeProvider)
        assert not _lock_is_free()
    assert _lock_is_free()


# ---------------------------------------------------------------------------
# RM-006: release on exit, including the error path
# ---------------------------------------------------------------------------


def test_rm_006_loaded_provider_releases_on_clean_exit():
    factory_calls: list[int] = []

    def factory() -> _FakeProvider:
        factory_calls.append(1)
        return _FakeProvider()

    with loaded_provider(factory) as provider:
        assert provider.unload_calls == 0
    assert len(factory_calls) == 1
    assert provider.unload_calls == 1


def test_rm_006_loaded_provider_releases_when_the_body_raises():
    factory_calls: list[int] = []

    def factory() -> _FakeProvider:
        factory_calls.append(1)
        return _FakeProvider()

    with pytest.raises(RuntimeError, match="boom"):
        with loaded_provider(factory) as provider:
            raise RuntimeError("boom")
    assert len(factory_calls) == 1
    assert provider.unload_calls == 1
    assert _lock_is_free()


def test_rm_006_release_provider_awaits_unload_and_reports_it():
    provider = _FakeProvider()
    assert release_provider(provider) is True
    assert provider.unload_calls == 1
