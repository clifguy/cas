"""Translation of vault-source store refusals into typed API errors (CAS-ADR-043).

The vault-source store binding refuses an operation with its own exception:
it sits below the API layer and may not import the error hierarchy. Left to
propagate, that refusal reaches an MCP caller as a generic internal error and
an HTTP caller as a bare 500 against a spec declaring neither -- describing a
server fault rather than the upstream refusal it actually is.

More than one service writes through that binding -- an ingest retains a
source, a restore writes one back -- and both owe a caller the same answer for
the same upstream fact. The translation lives here rather than in either of
them so the two cannot drift apart on which refusals are worth retrying or on
how much of the store's own text reaches a caller.

The translation is applied by wrapping the binding where it is resolved, not
by opening it at each call site. Hand placement covers the calls someone
remembered; the wrapper covers the source-byte half of the port because that
half is what it is derived from, so a method added to the port is either
wrapped or fails the seam contract that pins the wrapper's surface against it.
The alternative -- a wrap per call site and a gate that walks the source
looking for them -- was tried, and the gate proved able to miss a site and then
to miss a spelling.

Scope is the source-byte half. The config half (discovery, and the vault-config
read, write and delete) reaches the same client and can raise the same refusal,
but what a refusal means for a half-written config, and whether teardown should
report or absorb one, is a separate question this module does not answer. A
wrapper spanning the whole port would answer it silently.
"""

from __future__ import annotations

import abc
import contextlib
import functools
import inspect
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sage.api.errors import VaultSourceStoreRefusedError, VaultSourceStoreUnavailableError
from sage.config import VaultConfig
from sage.vault_source_binding import (
    DiscoveredVault,
    SupportsSourceDownloadUrl,
    VaultSourceStore,
)

logger = logging.getLogger(__name__)

#: The config half of the port: the operations that decide whether a vault
#: exists at all. Named rather than derived because the split is a judgement
#: about what a refusal *means*, not a property the port exposes.
CONFIG_METHOD_NAMES = frozenset(
    {"discover", "load_config", "config_locator", "write_config", "delete_config"}
)

#: The half the wrapper translates, derived from the port so a source-byte
#: method added there joins without an edit here.
SOURCE_BYTE_METHOD_NAMES = frozenset(VaultSourceStore.__abstractmethods__) - CONFIG_METHOD_NAMES

#: Source-byte methods that return an iterator rather than a value. The store
#: call happens on the first pull, not at the call, so wrapping the call
#: catches nothing and the delegate has to wrap the iteration instead.
_STREAMING_METHOD_NAMES = frozenset({"iter_source"})


@contextlib.contextmanager
def translate_store_refusal(source_path: str) -> Iterator[None]:
    """Surface the vault-source store's refusal as a typed API error.

    The binding has already decided whether waiting could change the answer,
    which is the one thing a caller needs and the one thing a message cannot
    convey reliably; that decision picks between the two codes.

    The store's own response body is logged rather than forwarded. It names
    the cause far better than anything composed here could, which is why it is
    kept at all -- but it is the store's text rather than this API's, it can
    carry tenant coordinates, and forwarding it would make an upstream
    service's wording a declared part of this surface.

    ``source_path`` is whichever path the caller can act on. The wrapper below
    supplies the one the operation was called with, which is the caller's
    concern for every source-byte method but retention: a retain is called with
    a staged upload's path under this process's temp tree, which names nothing
    the caller has. :func:`report_refusal_as` is how that one call corrects it.
    """
    from sage.vault_source_document_store import SourceStoreRefusalError

    try:
        yield
    except SourceStoreRefusalError as exc:
        logger.warning("vault-source store refused %s for %s: %s", exc.operation, source_path, exc)
        error = VaultSourceStoreUnavailableError if exc.retryable else VaultSourceStoreRefusedError
        raise error(source_path, exc.operation, exc.status) from exc


@contextlib.contextmanager
def report_refusal_as(source_path: str) -> Iterator[None]:
    """Re-label an already-translated store refusal with a caller's own spelling.

    Not a second translation: the wrapper has already chosen the code and the
    operation off the binding's decision, and this changes only which path the
    error names. It exists for the one caller whose own spelling of the source
    is not an argument the binding ever sees -- an ingest hands retention a
    staged copy, so the path the wrapper reports names a location under this
    process's temp tree instead of the source the caller asked to ingest.

    The chain is preserved through to the binding's original refusal rather
    than to the typed error being replaced, so a log walking ``__cause__``
    still reaches the store's own text in one hop.
    """
    try:
        yield
    except (VaultSourceStoreRefusedError, VaultSourceStoreUnavailableError) as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise type(exc)(
            source_path, detail.get("operation", "reach"), detail.get("store_status")
        ) from exc.__cause__


def _reported_path(signature: inspect.Signature, args: tuple, kwargs: dict) -> str:
    """The path this call names, read off the port's own signature.

    ``source_path`` where the operation has one -- every source-byte method
    but the tree teardown, which addresses a whole vault and so reports the
    vault id instead. Reading it from the bound signature rather than a fixed
    argument position keeps the delegate indifferent to how a caller spelled
    the call.
    """
    bound = signature.bind(None, *args, **kwargs)
    bound.apply_defaults()
    value = bound.arguments.get("source_path")
    if value is None:
        value = bound.arguments.get("vault_id", "")
    return str(value)


def _make_delegate(name: str) -> Callable[..., Any]:
    """A delegate that translates a refusal raised by the call itself.

    ``functools.wraps`` copies the port method's ``__dict__``, and
    ``@abstractmethod`` records itself there as ``__isabstractmethod__``. Left
    copied, the delegate would claim to be abstract while being the concrete
    implementation, so the flag is cleared rather than left for the installer
    to subtract around.
    """
    port_method = getattr(VaultSourceStore, name)
    signature = inspect.signature(port_method)

    @functools.wraps(port_method)
    def delegate(self, *args: Any, **kwargs: Any) -> Any:
        with translate_store_refusal(_reported_path(signature, args, kwargs)):
            return getattr(self.binding, name)(*args, **kwargs)

    delegate.__isabstractmethod__ = False
    return delegate


def _make_streaming_delegate(name: str) -> Callable[..., Any]:
    """A delegate that translates a refusal raised while the iterator is pulled.

    The binding's streaming read is a generator function: it opens no request
    until the first pull, so a translation around the call sees nothing. The
    delegate returns an iterator carrying the translation with it. A refusal
    reaching a caller that has already sent response headers cannot become a
    status code -- the truncated body against the promised length is what tells
    that caller it was cut short -- but the same iterator is also drained
    inside a service call, where the typed error is the answer.
    """
    port_method = getattr(VaultSourceStore, name)
    signature = inspect.signature(port_method)

    @functools.wraps(port_method)
    def delegate(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        source_path = _reported_path(signature, args, kwargs)

        def stream() -> Iterator[bytes]:
            with translate_store_refusal(source_path):
                yield from getattr(self.binding, name)(*args, **kwargs)

        return stream()

    delegate.__isabstractmethod__ = False
    return delegate


def _install_source_byte_delegates(cls: type) -> type:
    """Install a translating delegate for every source-byte method of the port.

    Generated rather than written out one by one. The set comes from the port,
    so a source-byte method added there is wrapped without an edit here, and
    the seam contract that pins this class's surface against the port turns
    that into a failure by name rather than a silently untranslated call. Hand
    placement is what this replaces; hand-placing it a second time inside the
    wrapper would keep the defect and only move it.

    ``ABCMeta`` computes ``__abstractmethods__`` when the class body closes,
    which is before these delegates land, so the set is recomputed afterwards --
    otherwise the class stays uninstantiable despite implementing everything.
    The recompute is ``abc``'s own rather than a subtraction performed here: the
    factories clear the abstract flag their delegates inherit, so the standard
    walk reaches the right answer, and anything that recomputes later reaches it
    too. Subtracting the names by hand would leave the delegates still claiming
    to be abstract, and the next recompute -- ``abc.update_abstractmethods``,
    which ``dataclasses`` calls when it decorates a class -- would undo the
    subtraction and take the class back to uninstantiable.
    """
    for name in sorted(SOURCE_BYTE_METHOD_NAMES):
        factory = _make_streaming_delegate if name in _STREAMING_METHOD_NAMES else _make_delegate
        setattr(cls, name, factory(name))
    abc.update_abstractmethods(cls)
    return cls


@_install_source_byte_delegates
class _TranslatingVaultSourceStore(VaultSourceStore):
    """A binding whose source-byte refusals arrive as typed API errors.

    Delegates the whole port. The config half passes through untouched, which
    is the scope decision recorded in this module's docstring and is written
    out here rather than derived so the omission reads as deliberate. The
    source-byte half is installed by the decorator above.
    """

    def __init__(self, binding: VaultSourceStore) -> None:
        self.binding = binding

    # -- Config half: delegated, deliberately untranslated -------------------

    def discover(self) -> list[DiscoveredVault]:
        return self.binding.discover()

    def load_config(self, discovered: DiscoveredVault) -> VaultConfig:
        return self.binding.load_config(discovered)

    def config_locator(self, vault_id: str) -> Path | None:
        return self.binding.config_locator(vault_id)

    def write_config(self, vault_id: str, config_dict: dict) -> None:
        self.binding.write_config(vault_id, config_dict)

    def delete_config(self, vault_id: str) -> None:
        self.binding.delete_config(vault_id)

    def close(self) -> None:
        self.binding.close()


class _TranslatingVaultSourceStoreWithDownloadUrl(_TranslatingVaultSourceStore):
    """The wrapper for a binding that mints download URLs (CAS-ADR-043 s5).

    The capability is probed with ``isinstance`` against a runtime-checkable
    protocol, which asks only whether the attribute is present. A single
    wrapper class defining ``download_url`` unconditionally would therefore
    claim the capability on behalf of a binding that has none, and the probe
    would stop discriminating. Two classes keep the claim honest.
    """

    def download_url(self, vault_id: str, storage_root: Path, source_path: str) -> str | None:
        with translate_store_refusal(source_path):
            return self.binding.download_url(vault_id, storage_root, source_path)  # type: ignore[attr-defined]


def wrap_vault_source_store(binding: VaultSourceStore) -> VaultSourceStore:
    """Return ``binding`` with its source-byte refusals translated.

    Applied where the binding is resolved, so every caller that resolves one
    gets the translation without asking for it. Idempotent: wrapping an
    already-wrapped binding returns it unchanged, so a caller that resolves
    twice, or a test that wraps a binding the resolver would have wrapped,
    does not stack two translations.
    """
    if isinstance(binding, _TranslatingVaultSourceStore):
        return binding
    wrapper = (
        _TranslatingVaultSourceStoreWithDownloadUrl
        if isinstance(binding, SupportsSourceDownloadUrl)
        else _TranslatingVaultSourceStore
    )
    return wrapper(binding)
