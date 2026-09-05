"""The vault-source store's refusal is typed at the binding (CAS-ADR-043).

The translation is applied by wrapping the binding where it is resolved rather
than by opening it at each call site. The claim that buys -- every source-byte
call is covered -- is the claim a previous placement made and did not hold, so
it is demonstrated here rather than asserted.

The demonstration is driven from the *port*, never from the wrapper's own
notion of which methods it covers. A test parameterized over the wrapper's set
would silently skip a method missing from it, which is exactly the failure the
wrapper exists to end. ``test_vsst_003`` closes the loop by exhibiting an
implementation where the claim is false and showing the probe fails against it.

Test IDs follow VSST-NNN (Vault-Source Store Translation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.api.errors import VaultSourceStoreRefusedError, VaultSourceStoreUnavailableError
from sage.config import StackDocumentStoreConfig
from sage.services.vault_source_errors import (
    CONFIG_METHOD_NAMES,
    SOURCE_BYTE_METHOD_NAMES,
    wrap_vault_source_store,
)
from sage.vault_source_binding import (
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    SupportsSourceDownloadUrl,
    VaultSourceStore,
)
from sage.vault_source_document_store import SourceStoreRefusalError
from tests.helpers.store_refusal import STORE_BODY, store_refusal

#: The half the wrapper is expected to cover, derived from the port rather than
#: from the wrapper. A source-byte method the wrapper failed to install still
#: gets a case here and fails on the untranslated exception.
SOURCE_BYTE_NAMES = sorted(set(VaultSourceStore.__abstractmethods__) - CONFIG_METHOD_NAMES)

_STORAGE_ROOT = Path("/vault/v/sources")
_STAGED = Path("/tmp/staged/in.md")
_RETAINED = "imports/r.md"

#: One valid call per source-byte operation. Kept beside the parameterization
#: rather than derived, because "a valid call" is not something a signature
#: says; VSST-000 pins the two against each other so a port addition fails by
#: name here instead of as a lookup error inside a case.
CALL_ARGS: dict[str, tuple] = {
    "planned_source_path": ("v", _STORAGE_ROOT, _STAGED),
    "retain_source": ("v", _STORAGE_ROOT, _STAGED),
    "write_source": ("v", _STORAGE_ROOT, _RETAINED, _STAGED),
    "source_exists": ("v", _STORAGE_ROOT, _RETAINED),
    "source_is_symlink": ("v", _STORAGE_ROOT, _RETAINED),
    "source_is_out_of_root": ("v", _STORAGE_ROOT, _RETAINED),
    "source_size": ("v", _STORAGE_ROOT, _RETAINED),
    "read_source": ("v", _STORAGE_ROOT, _RETAINED),
    "iter_source": ("v", _STORAGE_ROOT, _RETAINED),
    "hash_source": ("v", _STORAGE_ROOT, _RETAINED),
    "delete_source_tree": ("v", _STORAGE_ROOT),
}

#: The path each call above is expected to report. Stated outright rather than
#: derived from CALL_ARGS, which would restate the production rule and pass
#: against any consistent wrong answer. Every source-byte operation names the
#: source it was called with; the tree teardown has no source argument and
#: names the vault instead, that being what its caller can act on.
EXPECTED_REPORTED_PATH: dict[str, str] = {
    "planned_source_path": str(_STAGED),
    "retain_source": str(_STAGED),
    "write_source": _RETAINED,
    "source_exists": _RETAINED,
    "source_is_symlink": _RETAINED,
    "source_is_out_of_root": _RETAINED,
    "source_size": _RETAINED,
    "read_source": _RETAINED,
    "iter_source": _RETAINED,
    "hash_source": _RETAINED,
    "delete_source_tree": "v",
}

#: Operations returning an iterator, which have to be drained for the store
#: call to happen at all.
_ITERATING = {"iter_source"}


def _refusing_binding(refusal: Exception) -> VaultSourceStore:
    """A binding that refuses every port operation with ``refusal``.

    Built from the port's own abstract set so it cannot fall behind it. The
    streaming read is a generator function, as the real bindings' is: it must
    refuse on the first pull rather than at the call, or a delegate that wrapped
    only the call would appear to work.
    """

    def _refuse(self, *args, **kwargs):
        raise refusal

    def _refuse_on_pull(self, *args, **kwargs):
        raise refusal
        yield  # pragma: no cover -- unreachable; makes this a generator function

    namespace: dict = dict.fromkeys(VaultSourceStore.__abstractmethods__, _refuse)
    namespace["iter_source"] = _refuse_on_pull
    return type("_RefusingBinding", (VaultSourceStore,), namespace)()


def _call(store: VaultSourceStore, name: str):
    """Invoke ``name`` on ``store``, draining it if it returns an iterator."""
    result = getattr(store, name)(*CALL_ARGS[name])
    if name in _ITERATING:
        return list(result)
    return result


# --------------------------------------------------------------------------- #
# VSST-000: the call table keeps pace with the port
# --------------------------------------------------------------------------- #


def test_vsst_000_call_table_covers_every_source_byte_method():
    """Every source-byte port method has a call recorded for it.

    Trap: without this, a method added to the port would fail the probe below
    with a ``KeyError`` from the table lookup -- a red that names the test
    harness rather than the untranslated method, and one a reader would be
    tempted to fix by adding an entry rather than by covering the method.
    """
    assert set(CALL_ARGS) == set(SOURCE_BYTE_NAMES)
    assert set(EXPECTED_REPORTED_PATH) == set(SOURCE_BYTE_NAMES)


# --------------------------------------------------------------------------- #
# VSST-001/002: the source-byte half is typed, both codes reachable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method_name", SOURCE_BYTE_NAMES)
def test_vsst_001_every_source_byte_call_is_typed(method_name):
    """A refusal on any source-byte operation reaches the caller typed.

    The completeness claim, demonstrated rather than asserted. Parameterized
    over the port's source-byte half, so a method the wrapper does not cover
    fails here by name instead of going untested.

    Trap: the store's own response body naming tenant coordinates travelling
    onto the public error. Asserted absent, the sentinel body being one no
    paraphrase reproduces.

    Trap: a wrapper that types every operation but names the wrong path in the
    result. The detail exists so a caller can tell which of the paths it named
    was refused, and deriving it is the only non-mechanical thing the wrapper
    does -- a delegate reading a fixed argument position, or falling back to an
    empty string, types the error just as this asserts while telling the caller
    nothing. So the reported path is pinned per operation against a table
    stated independently of the production rule.
    """
    refusal = store_refusal(403, retryable=False, operation="read source")
    store = wrap_vault_source_store(_refusing_binding(refusal))

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        _call(store, method_name)

    assert excinfo.value.detail["source_path"] == EXPECTED_REPORTED_PATH[method_name]
    assert excinfo.value.detail["operation"] == "read source"
    assert excinfo.value.detail["store_status"] == 403
    assert STORE_BODY not in str(excinfo.value)
    assert excinfo.value.__cause__ is refusal


@pytest.mark.parametrize("method_name", SOURCE_BYTE_NAMES)
def test_vsst_002_a_transient_refusal_picks_the_other_code(method_name):
    """The binding's transience decision picks between the two codes.

    Anti-coincidental-pass for VSST-001: a wrapper that raised one code
    unconditionally would satisfy that test on every method and fail every
    case here. Run across the whole half rather than one representative, so a
    delegate that hard-coded a code on one method cannot hide behind the rest.
    """
    store = wrap_vault_source_store(
        _refusing_binding(store_refusal(429, retryable=True, operation="stat source"))
    )

    with pytest.raises(VaultSourceStoreUnavailableError) as excinfo:
        _call(store, method_name)

    assert excinfo.value.status_code == 503


# --------------------------------------------------------------------------- #
# VSST-003: the probe fails against an implementation where the claim is false
# --------------------------------------------------------------------------- #


def test_vsst_003_the_probe_detects_an_uncovered_method():
    """An uncovered source-byte method is caught, not passed over.

    VSST-001's assertion is only worth its claim if a wrapper that missed a
    method fails it. This exhibits that wrapper -- a subclass restoring plain,
    untranslated delegation on one operation -- and shows the raw refusal
    escapes, which is what VSST-001 would report.

    The two prior placements of this translation each shipped a completeness
    claim a probe falsified afterwards. This is that probe, run in advance.
    """
    refusal = store_refusal(403, retryable=False)
    binding = _refusing_binding(refusal)
    covered = wrap_vault_source_store(binding)

    class _Uncovered(type(covered)):
        def read_source(self, vault_id, storage_root, source_path):
            return self.binding.read_source(vault_id, storage_root, source_path)

    with pytest.raises(SourceStoreRefusalError):
        _call(_Uncovered(binding), "read_source")

    # The control: the same call through the real wrapper is typed.
    with pytest.raises(VaultSourceStoreRefusedError):
        _call(covered, "read_source")


# --------------------------------------------------------------------------- #
# VSST-004: the config half stays untranslated
# --------------------------------------------------------------------------- #


_CONFIG_CALL_ARGS: dict[str, tuple] = {
    "discover": (),
    "load_config": (object(),),
    "config_locator": ("v",),
    "write_config": ("v", {}),
    "delete_config": ("v",),
}


def test_vsst_004_config_call_table_covers_every_config_method():
    """The config-half call table keeps pace with the named config set."""
    assert set(_CONFIG_CALL_ARGS) == set(CONFIG_METHOD_NAMES)


@pytest.mark.parametrize("method_name", sorted(CONFIG_METHOD_NAMES))
def test_vsst_005_the_config_half_is_not_typed(method_name):
    """A refused config operation still reaches its caller as the raw refusal.

    The scope decision, pinned. What a refusal means for a half-written config
    -- and whether teardown should report or absorb one -- is unsettled, and a
    wrapper spanning the whole port would settle it silently by giving those
    operations a status code nobody chose for them.

    Trap: asserting only that the typed errors are absent would also pass if
    the call never reached the store at all. The raw refusal is asserted
    positively, so the operation demonstrably ran and was declined.
    """
    refusal = store_refusal(403, retryable=False, operation="read config")
    store = wrap_vault_source_store(_refusing_binding(refusal))

    with pytest.raises(SourceStoreRefusalError) as excinfo:
        getattr(store, method_name)(*_CONFIG_CALL_ARGS[method_name])

    assert excinfo.value is refusal
    assert not isinstance(
        excinfo.value, VaultSourceStoreRefusedError | VaultSourceStoreUnavailableError
    )


# --------------------------------------------------------------------------- #
# VSST-006: streaming refusals are typed on the pull, not at the call
# --------------------------------------------------------------------------- #


def test_vsst_006_a_streaming_refusal_is_typed_mid_iteration():
    """A refusal raised after the first chunk is typed when it is pulled.

    The binding's streaming read opens no request until the first pull, so a
    translation around the *call* sees nothing. Where the iterator is drained
    inside a service call -- write-to-path delivery -- that difference is the
    difference between a typed error and a bare internal one.

    Anti-coincidental-pass: the refusal fires after a chunk has already been
    yielded, so a delegate that only translated a generator refusing on its
    very first pull would fail here. Constructing the iterator raises nothing,
    which the unguarded call below would surface -- the point being that the
    call itself is silent and only the pull carries the refusal.
    """
    refusal = store_refusal(503, retryable=True, operation="stream source")

    class _RefusingMidStream(FilesystemVaultSourceStore):
        def iter_source(self, vault_id, storage_root, source_path):
            yield b"first chunk"
            raise refusal

    store = wrap_vault_source_store(_RefusingMidStream(Path("/unused")))
    chunks = store.iter_source("v", _STORAGE_ROOT, _RETAINED)

    assert next(chunks) == b"first chunk"
    with pytest.raises(VaultSourceStoreUnavailableError):
        next(chunks)


# --------------------------------------------------------------------------- #
# VSST-007: the download-URL capability stays binding-scoped
# --------------------------------------------------------------------------- #


def test_vsst_007_wrapping_does_not_confer_the_download_url_capability():
    """The wrapper claims the optional capability only for a binding that has it.

    Trap, and the reason the wrapper is not a ``__getattr__`` proxy: the
    capability is probed with ``isinstance`` against a runtime-checkable
    protocol, which asks only whether the attribute resolves. A proxy resolves
    every attribute, so it would claim the capability on behalf of the
    filesystem binding and the probe at the delivery boundary would stop
    discriminating -- turning a structured 501 into an ``AttributeError``.

    The negative case is the load-bearing one; the positive case alone passes
    against the defect.
    """
    filesystem = wrap_vault_source_store(FilesystemVaultSourceStore(Path("/unused")))
    document_store = wrap_vault_source_store(
        DocumentStoreVaultSourceStore(StackDocumentStoreConfig())
    )

    assert not isinstance(filesystem, SupportsSourceDownloadUrl)
    assert not hasattr(filesystem, "download_url")
    assert isinstance(document_store, SupportsSourceDownloadUrl)


def test_vsst_008_a_refused_download_url_is_typed():
    """The optional capability's refusal is typed like the port's own."""
    refusal = store_refusal(403, retryable=False, operation="mint download url")

    class _RefusingUrl(DocumentStoreVaultSourceStore):
        def download_url(self, vault_id, storage_root, source_path):
            raise refusal

    store = wrap_vault_source_store(_RefusingUrl(StackDocumentStoreConfig()))

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        store.download_url("v", _STORAGE_ROOT, _RETAINED)

    assert excinfo.value.detail["source_path"] == _RETAINED


# --------------------------------------------------------------------------- #
# VSST-009: wrapping is idempotent
# --------------------------------------------------------------------------- #


def test_vsst_009_wrapping_an_already_wrapped_binding_is_a_no_op():
    """A second wrap returns the first rather than stacking a translation.

    Resolution happens per call rather than once, and a test may hand the
    resolver a binding it wrapped itself. A stacked wrapper would still type a
    refusal, so the cost is not visible in an error -- it is a second frame
    that re-reads a path already reported, which is how a re-label at the ingest
    would come to correct the wrong layer.
    """
    once = wrap_vault_source_store(FilesystemVaultSourceStore(Path("/unused")))
    assert wrap_vault_source_store(once) is once


# --------------------------------------------------------------------------- #
# VSST-010: the wrapper's covered set matches the port
# --------------------------------------------------------------------------- #


def test_vsst_010_the_covered_set_is_the_ports_source_byte_half():
    """The wrapper's set and the port's agree, with no overlap between halves.

    The seam contract pins the wrapper's *surface* against the port; this pins
    the *split*. Trap: a config method drifting into the source-byte set would
    type the config half silently, which VSST-005 catches only for the five
    names it knows about today.
    """
    assert SOURCE_BYTE_METHOD_NAMES | CONFIG_METHOD_NAMES == set(
        VaultSourceStore.__abstractmethods__
    )
    assert not SOURCE_BYTE_METHOD_NAMES & CONFIG_METHOD_NAMES
