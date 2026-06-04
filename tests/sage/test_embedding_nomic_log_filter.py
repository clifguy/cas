"""Tests for the nomic embedding-load log filter.

nomic-embed-text's remote modeling code emits a cosmetic
``<All keys matched successfully>`` WARNING (PyTorch's ``_IncompatibleKeys``
success repr) on a clean state-dict load. The filter swallows exactly that
record at WARNING level while leaving every other record -- including nomic's
genuinely-useful ``scaled_dot_product_attention not available`` note and any
real key-mismatch report -- visible.

The emission travels through a *named* child logger (its name embeds the HF
snapshot hash), so the filter must attach to the root logger's *handlers*: an
ancestor logger's own filters are not consulted for records propagated up from
a named child logger. ``test_install_suppresses_named_child_logger_at_root_handler``
exercises that real propagation path and is the guard against a logger-level
(``root.addFilter``) regression that would silently fail to suppress.

None of these tests load nomic-embed-text or construct the provider; they build
``LogRecord`` objects directly or emit through a scratch logger.
"""

import io
import logging

from sage.adapters.embedding_nomic import (
    _KEYS_MATCHED_MESSAGE,
    _install_nomic_keys_matched_filter,
    _NomicKeysMatchedFilter,
)

_SDPA_MESSAGE = "scaled_dot_product_attention not available, using torch.matmul instead"


def _make_record(msg: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        name="transformers_modules.deadbeef.modeling_hf_nomic_bert",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Unit level: _NomicKeysMatchedFilter.filter() in isolation
# ---------------------------------------------------------------------------


def test_filter_swallows_keys_matched_warning():
    f = _NomicKeysMatchedFilter()
    assert f.filter(_make_record(_KEYS_MATCHED_MESSAGE)) is False


def test_filter_passes_scaled_dot_product_warning():
    f = _NomicKeysMatchedFilter()
    assert f.filter(_make_record(_SDPA_MESSAGE)) is True


def test_filter_passes_partial_match():
    # A real key mismatch uses the longer _IncompatibleKeys repr, not the
    # exact success repr -- it must stay visible.
    f = _NomicKeysMatchedFilter()
    mismatch = "_IncompatibleKeys(missing_keys=['embeddings.weight'], unexpected_keys=[])"
    assert f.filter(_make_record(mismatch)) is True


def test_filter_passes_non_warning_records():
    # Even text that matches the suppression pattern is passed through at
    # non-WARNING levels -- the filter only acts on WARNING.
    f = _NomicKeysMatchedFilter()
    assert f.filter(_make_record(_KEYS_MATCHED_MESSAGE, level=logging.ERROR)) is True
    assert f.filter(_make_record(_KEYS_MATCHED_MESSAGE, level=logging.INFO)) is True


# ---------------------------------------------------------------------------
# Mechanism level: handler-level install over the real propagation path
# ---------------------------------------------------------------------------


def _strip_keys_matched_filters(handler: logging.Handler) -> None:
    for existing in list(handler.filters):
        if isinstance(existing, _NomicKeysMatchedFilter):
            handler.removeFilter(existing)


def test_install_suppresses_named_child_logger_at_root_handler():
    """A WARNING emitted by a *named* child logger is suppressed at the root
    handler after install; an unrelated WARNING from the same logger is not.

    This is the trap for a logger-level (``root.addFilter``) implementation:
    that approach would leave the root handler unfiltered for propagated
    child-logger records, so the success message would reach the StringIO and
    this test would fail. Only a handler-level install passes it.
    """
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    child = logging.getLogger("transformers_modules.deadbeef.modeling_hf_nomic_bert")
    child.setLevel(logging.WARNING)
    try:
        _install_nomic_keys_matched_filter()
        child.warning(_KEYS_MATCHED_MESSAGE)
        child.warning(_SDPA_MESSAGE)
        out = stream.getvalue()
        assert _KEYS_MATCHED_MESSAGE not in out
        assert "scaled_dot_product_attention not available" in out
    finally:
        root.removeHandler(handler)
        # The filter is a module-level singleton; install may have attached it
        # to pre-existing root handlers too. Clean it off everything so the
        # mutation does not leak into other tests.
        _strip_keys_matched_filters(handler)
        for h in root.handlers:
            _strip_keys_matched_filters(h)
        if logging.lastResort is not None:
            _strip_keys_matched_filters(logging.lastResort)


def test_install_is_idempotent():
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        _install_nomic_keys_matched_filter()
        _install_nomic_keys_matched_filter()
        matched = [f for f in handler.filters if isinstance(f, _NomicKeysMatchedFilter)]
        assert len(matched) == 1
    finally:
        root.removeHandler(handler)
        for h in root.handlers:
            _strip_keys_matched_filters(h)
        if logging.lastResort is not None:
            _strip_keys_matched_filters(logging.lastResort)


def test_install_falls_back_to_lastResort_when_root_unconfigured():
    """With no handlers on the root logger, install attaches to lastResort so
    the unconfigured-logging fallback path is still suppressed."""
    root = logging.getLogger()
    saved = root.handlers
    root.handlers = []
    try:
        _install_nomic_keys_matched_filter()
        assert any(isinstance(f, _NomicKeysMatchedFilter) for f in logging.lastResort.filters)
    finally:
        root.handlers = saved
        if logging.lastResort is not None:
            _strip_keys_matched_filters(logging.lastResort)
