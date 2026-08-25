"""Shared SAGE service initialization for FastAPI and MCP entry points."""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sage import profiles
from sage.adapters.embedding_nomic import get_nomic_embedding_provider
from sage.adapters.interfaces import (
    AbstractionProvider,
    ContentStore,
    EmbeddingProvider,
    GraphStore,
)
from sage.adapters.stubs import StubAbstractionProvider
from sage.auth import TokenValidator, build_auth_validator
from sage.config import SageCoreConfig, StackAbstractionConfig, VaultConfig
from sage.instrumentation.timing import (
    NULL_QUERY_TIMER,
    NullQueryTimer,
    QueryTimer,
    TimingConfig,
    VaultTimingThread,
)
from sage.models.enums import SourceType
from sage.services.documents import DocumentsService
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.maintenance import MaintenanceService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.staging_edges import StagingEdgesService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.services.vault_config import VaultConfigService
from sage.source_adapters.base import SourceAdapter
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.pptx_adapter import PptxAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from sage.storage.locks import DocumentLockManager
from sage.storage_binding import (
    VaultStorageHandle,
    VaultStorageProvisioner,
    build_stack_storage_provisioner,
)
from sage.vault_source_binding import (
    VaultSourceStore,
    build_stack_vault_source_store,
)

_logger = logging.getLogger(__name__)

_TIMING_LOGGER_NAMES = (
    "sage.storage.timing",
    "sage.content.timing",
    "sage.retrieval.timing",
    "sage.abstraction.timing",
)


def build_source_adapter_registry() -> dict[SourceType, SourceAdapter]:
    """Build the process-wide source-adapter registry.

    Adapter selection during ingestion resolves against this mapping, so a
    source type absent here raises ``adapter_not_found``. Vault
    configuration declares no adapters at all (CAS-ADR-046); availability
    is process-wide capability fixed by the installed implementations.
    """
    return {
        SourceType.MARKDOWN: MarkdownAdapter(),
        SourceType.DOCX: DocxAdapter(),
        SourceType.XLSX: XlsxAdapter(),
        SourceType.PDF: PdfAdapter(),
        SourceType.PPTX: PptxAdapter(),
    }


@dataclass
class _TimingHandlerRef:
    """Reference-count entry for a process-global timing-log handler.

    The timing loggers are process-global, but the
    ``RotatingFileHandler`` behind them is per-vault — keyed by the resolved
    ``timing.log`` path. Several ``SAGEServices`` can map to one path (a vault
    reload reuses its ``brain_root``), so the handler outlives any single
    services instance. Reference counting closes the open file handle exactly
    once, when the last holder of the path tears down.
    """

    handler: logging.handlers.RotatingFileHandler
    refcount: int


# Guards ``_timing_handlers``; install/release run inside synchronous critical
# sections so concurrent vault initialization (startup fan-out) stays correct.
_timing_handler_lock = threading.Lock()
# Reference-counted timing-log handlers, keyed by resolved (str) log path. The
# handlers attach to the process-global timing loggers, so their lifecycle
# spans more than one SAGEServices and needs cross-call coordination; this
# registry is that coordination point. Entries are created on first install
# and removed when the last reference is released.
_timing_handlers: dict[str, _TimingHandlerRef] = {}
# Resting ``propagate`` state of the timing loggers, captured when the first
# handler in the process attaches and restored when the last one detaches.
# Installing disables propagation, so without this the loggers would stay
# non-propagating for the life of the process even after every handler is
# gone. Keyed by logger name; empty whenever ``_timing_handlers`` is empty.
_saved_propagate: dict[str, bool] = {}


def _install_timing_handler(
    log_path: Path,
) -> logging.handlers.RotatingFileHandler:
    """Attach (or reuse) the per-vault timing handler under a reference count.

    The first caller for ``log_path`` opens a ``RotatingFileHandler`` on the
    file and attaches it to the timing loggers; later callers reuse that
    handler and bump its reference count. ``propagate`` is disabled on the
    timing loggers so records don't bubble up to the root logger (which would
    mix them into the normal app stream); the state it displaces is saved on
    the process's first install and restored by its last release. That
    snapshot is process-global rather than per-path because the loggers are
    shared: restoring when one vault detaches would re-enable propagation
    while another vault's handler is still attached.

    The open file handle is released only by the matching
    ``_release_timing_handler`` call that drops the count to zero, so a vault
    reload (which reuses the path) keeps logging across the swap and a failed
    reload leaves the surviving vault's handler intact.
    """
    key = str(log_path)
    with _timing_handler_lock:
        if not _timing_handlers:
            _saved_propagate.clear()
            _saved_propagate.update(
                {name: logging.getLogger(name).propagate for name in _TIMING_LOGGER_NAMES}
            )
        ref = _timing_handlers.get(key)
        if ref is None:
            handler = logging.handlers.RotatingFileHandler(
                key,
                maxBytes=50_000_000,
                backupCount=3,
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            for name in _TIMING_LOGGER_NAMES:
                logger = logging.getLogger(name)
                logger.setLevel(logging.DEBUG)
                logger.addHandler(handler)
                logger.propagate = False
            ref = _TimingHandlerRef(handler=handler, refcount=0)
            _timing_handlers[key] = ref
        ref.refcount += 1
        return ref.handler


def _release_timing_handler(handler: logging.Handler | None) -> None:
    """Drop one reference to a per-vault timing handler; close it on the last.

    Inverse of ``_install_timing_handler``. Decrements the reference count for
    the entry holding ``handler``; on reaching zero it detaches the handler
    from the timing loggers and closes it, releasing the ``timing.log`` file
    handle. Detaching the process's last handler also restores the propagation
    state the first install displaced, so a logger does not stay
    non-propagating once nothing is writing it to a file any more.

    A ``None`` handler (timing disabled) or one already fully released is a
    no-op, so this is safe on any teardown path and safe to call more than
    once.
    """
    if handler is None:
        return
    with _timing_handler_lock:
        key = next(
            (k for k, ref in _timing_handlers.items() if ref.handler is handler),
            None,
        )
        if key is None:
            return
        ref = _timing_handlers[key]
        ref.refcount -= 1
        if ref.refcount > 0:
            return
        del _timing_handlers[key]
        for name in _TIMING_LOGGER_NAMES:
            logging.getLogger(name).removeHandler(handler)
        handler.close()
        if not _timing_handlers:
            for name, propagate in _saved_propagate.items():
                logging.getLogger(name).propagate = propagate
            _saved_propagate.clear()


def _build_vault_timers(
    timing: TimingConfig,
    brain_root: Path,
) -> tuple[
    QueryTimer | NullQueryTimer,
    QueryTimer | NullQueryTimer,
    QueryTimer | NullQueryTimer,
    VaultTimingThread | None,
    logging.handlers.RotatingFileHandler | None,
]:
    """Construct the three per-layer QueryTimers and the flusher thread.

    Returns ``(storage, content, retrieval, thread, handler)``. When
    ``timing.enabled`` is False, all three are ``NULL_QUERY_TIMER`` and
    the thread/handler are ``None``.
    """
    if not timing.enabled:
        return NULL_QUERY_TIMER, NULL_QUERY_TIMER, NULL_QUERY_TIMER, None, None

    log_path = Path(timing.log_path) if timing.log_path else brain_root / "timing.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = _install_timing_handler(log_path)

    storage_timer = QueryTimer("sage.storage.timing", timing, "storage")
    content_timer = QueryTimer("sage.content.timing", timing, "content")
    retrieval_timer = QueryTimer("sage.retrieval.timing", timing, "retrieval")
    flusher = VaultTimingThread(
        timers=[storage_timer, content_timer, retrieval_timer],
        interval_seconds=timing.summary_interval_seconds,
    )
    flusher.start()
    return storage_timer, content_timer, retrieval_timer, flusher, handler


if TYPE_CHECKING:
    from sage.services.vault_registry import VaultRegistryService


@dataclass
class SAGEServices:
    """All initialized SAGE services for a single vault."""

    config: VaultConfig
    graph_store: GraphStore
    content_store: ContentStore
    lock_manager: DocumentLockManager
    user_service: UserService
    lifecycle_service: LifecycleService
    metadata_service: MetadataService
    documents_service: DocumentsService
    ingestion_service: IngestionService
    graph_ops_service: GraphOpsService
    retrieval_service: RetrievalService
    utilities_service: UtilitiesService
    staging_edges_service: StagingEdgesService
    vault_config_service: VaultConfigService
    # CAS-ADR-029 pilot maintenance operation. None when initialize_services
    # was called without a registry_service (test paths that bypass the
    # lifespan); the production lifespan always supplies one.
    maintenance_service: MaintenanceService | None = None
    config_path: Path | None = None
    # Test-only hook: when set, reload paths (reload_vault,
    # reload_vault_in_registry) re-invoke this factory with the vault's
    # brain_root instead of consulting the storage provisioner. Carried on
    # the services tuple so the factory survives across reloads without
    # adding module-level mutable state. None in production.
    content_store_factory: Callable[[Path], ContentStore] | None = None
    # Mirror of content_store_factory for the graph store: when set, reload
    # paths re-invoke this factory with the vault's brain_root instead of
    # consulting the storage provisioner. Carried on the services tuple so
    # the factory survives across reloads without module-level mutable state.
    # None in production.
    graph_store_factory: Callable[[Path], GraphStore] | None = None
    # Per-vault background flusher for query-timing summary records.
    # None when timing is disabled or when the content store was injected
    # without going through _build_vault_timers (test paths).
    timing_thread: VaultTimingThread | None = None
    # Per-vault rotating file handler behind the timing loggers. Held so
    # close_timing() can release the per-path reference and let the last holder
    # close the timing.log file handle on teardown. Shares the timing_thread's
    # lifecycle (both built by _build_vault_timers); None when timing is
    # disabled.
    timing_handler: logging.Handler | None = None
    # Handle for the durable-storage pair the storage provisioner opened for
    # this vault (CAS-ADR-042). Owns the backing resource — the per-vault
    # Postgres connection pool. None when the caller filled both store slots
    # itself, in which case the provisioner was never consulted.
    storage: "VaultStorageHandle | None" = None

    async def close_storage(self) -> None:
        """Close the graph store, then release the storage handle's resource.

        The graph-store close preserves the pre-seam teardown contract
        (injected stores included — every teardown path closed
        ``graph_store`` unconditionally); the handle close then releases the
        backing resource the provisioner opened (the pool, on the Postgres
        binding). Both closes are idempotent. Invoked from every vault
        teardown path in place of the former bare ``graph_store.close()``.
        """
        await self.graph_store.close()
        if self.storage is not None:
            await self.storage.close()

    def close_timing(self) -> None:
        """Stop the timing flusher and release this vault's timing.log handle.

        Stops the per-vault ``VaultTimingThread`` (when running) and drops this
        vault's reference to the shared per-path timing handler, which closes
        the underlying ``timing.log`` file once the last referencing vault
        tears down. Idempotent: the handler reference is cleared after release,
        so a second call is a no-op. Invoked from every vault-teardown path —
        production lifespan shutdown, registry reload and create-rollback, the
        migration CLI, and the test context manager — so the file handle never
        outlives the services that opened it.
        """
        if self.timing_thread is not None:
            self.timing_thread.stop(timeout=1.0)
        _release_timing_handler(self.timing_handler)
        self.timing_handler = None


# Stack-wide SAGE Core API config (CAS-ADR-030). Loaded once at
# lifespan startup; nullable to support callers (tests, in-process FastAPI
# mounts) that construct services without invoking the standalone lifespan.
_stack_config: SageCoreConfig | None = None


def get_stack_config() -> SageCoreConfig:
    """Return the loaded stack config, or an empty default if none is set.

    Used by `get_stack_config` (MCP) and by FastAPI lifespan paths
    that need to thread the stack config into per-vault initialization.
    Returns the module-level singleton when set; otherwise a default
    SageCoreConfig (defaults are stack-stub-effective when combined with
    `SAGE_TEST_STUB_PROVIDERS=1`).
    """
    return _stack_config if _stack_config is not None else SageCoreConfig()


def set_stack_config(cfg: SageCoreConfig | None) -> None:
    """Set or clear the module-level stack config. Used by lifespan paths."""
    global _stack_config
    _stack_config = cfg


def caller_local_filesystem_reachable() -> bool:
    """Whether the active profile lets the server see the caller's filesystem.

    Reads the active deployment profile and defers to
    :func:`sage.profiles.caller_local_filesystem_available`. Path-bearing tools
    consult this to decide whether a caller-supplied local path can be honored
    (local profile) or must be refused in favor of the in-request byte channel
    (cloud profile).
    """
    return profiles.caller_local_filesystem_available(get_stack_config().profile)


def require_caller_local_filesystem(operation: str, remedy: str) -> None:
    """Refuse a caller-path operation the running server cannot honor.

    Under a profile where SAGE cannot see the calling client's filesystem (the
    cloud profile: a remote container), a caller-supplied local path would
    silently resolve against the server's own tree -- the usability gap and the
    container-walk disclosure in one. Path-bearing tools call this before
    touching such a path so the operation is refused with a structured error
    naming the sanctioned in-request mechanism, instead of reading, writing, or
    enumerating the container. A no-op under the local profile.

    ``operation`` names the refused affordance and ``remedy`` names the
    in-request mechanism to use instead; both land in the error envelope.
    """
    if not caller_local_filesystem_reachable():
        from sage.api.errors import CallerFilesystemUnavailableError

        raise CallerFilesystemUnavailableError(operation, remedy)


# Process-bound vault root (CAS-ADR-043). The transport lifespans resolve it from
# ``--vault-root`` / ``SAGE_VAULT_ROOT`` / the default and publish it here, so the
# vault-config write paths resolve the same filesystem binding discovery used.
# Nullable: mirrors ``_stack_config`` for callers (in-process FastAPI mounts,
# injected-config test paths) that construct services without a transport lifespan.
_vault_root: Path | None = None


def get_vault_root() -> Path | None:
    """Return the process-bound vault root the active transport lifespan resolved.

    The mirror of ``get_stack_config`` for the filesystem vault-source binding's
    root: the standalone MCP lifespan and the FastAPI lifespan publish the root
    they resolved from ``--vault-root`` / ``SAGE_VAULT_ROOT`` / the default so the
    config write paths (``create_vault``, ``update_config``) resolve the same
    binding discovery used. ``None`` when no lifespan has run (injected-config or
    in-process test paths) or when the active profile's vault-source backend is
    not the filesystem (the cloud document store publishes no filesystem root); in
    both cases the write paths fall through to the profile seam, unchanged.
    """
    return _vault_root


def set_vault_root(root: Path | None) -> None:
    """Set or clear the process-bound vault root. Used by lifespan paths."""
    global _vault_root
    _vault_root = root


_DEFAULT_STACK_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

#: Environment variable that overrides the stack-config source path when the
#: caller passes no explicit ``path``. A repo-less deployment (e.g. a container
#: image) sets this to a Linux-safe stack config so the lifespan loads it
#: instead of the packaged ``sage/config.yaml`` -- no edit to the committed
#: file required.
_STACK_CONFIG_PATH_ENV: str = "SAGE_CONFIG_PATH"


def load_stack_config_or_default(path: Path | None = None) -> SageCoreConfig:
    """Load the stack config, resolving the source path by precedence.

    Source precedence: an explicit ``path`` argument (test injection) -> the
    ``SAGE_CONFIG_PATH`` environment variable (the repo-less / container
    override seam) -> the packaged ``sage/config.yaml``
    (:data:`_DEFAULT_STACK_CONFIG_PATH`).

    A missing *default* file returns a default :class:`SageCoreConfig` so test
    fixtures that exercise the lifespan without writing the file (and rely on
    ``SAGE_TEST_STUB_PROVIDERS=1`` to short-circuit the provider build) keep
    working. A missing file named by an *explicit override* (the ``path``
    argument or ``SAGE_CONFIG_PATH``) fails loud with ``FileNotFoundError``
    rather than silently degrading to defaults: a typo'd override must not
    boot a deployment on the wrong configuration unnoticed. The provider
    builder remains the gate that fails loudly when a real Qwen3 dispatch is
    requested without a model identifier.
    """
    from sage.config import load_sage_core_config

    if path is not None:
        resolved, is_override = path, True
    else:
        env_value = os.environ.get(_STACK_CONFIG_PATH_ENV)
        if env_value:
            resolved, is_override = Path(env_value), True
        else:
            resolved, is_override = _DEFAULT_STACK_CONFIG_PATH, False

    if not resolved.exists():
        if is_override:
            raise FileNotFoundError(
                f"{_STACK_CONFIG_PATH_ENV} (or an explicit config path) points at "
                f"{resolved}, which does not exist."
            )
        return SageCoreConfig()
    return load_sage_core_config(resolved)


def _reject_context_window_for_non_local(abstraction: StackAbstractionConfig) -> None:
    """Fail loud when ``context_window`` is set against a non-local provider.

    The field is consumed only by the local MLX provider, which truncates its
    prompt to fit a window it owns. A hosted provider derives its input limit
    from its own configured model and has nothing to do with the field, so a
    configuration that sets it against one is rejected rather than silently
    ignored -- the difference between a visible misconfiguration and a knob
    that appears to work and does nothing.
    """
    if abstraction.context_window is None or abstraction.provider == "local-mlx":
        return
    raise ValueError(
        "sage_core_config.abstraction.context_window is consumed only by the "
        "'local-mlx' provider, but abstraction.provider is "
        f"{abstraction.provider!r} (CAS-ADR-030). A hosted provider derives its "
        "input limit from its own configured model. Remove the context_window "
        "field from the stack config, or set abstraction.provider to 'local-mlx'."
    )


def build_stack_abstraction_provider(stack_config: SageCoreConfig) -> AbstractionProvider:
    """Construct the SAGE-stack-wide abstraction provider (CAS-ADR-030).

    Called once at SAGE process startup, before any vault is registered.
    The resulting provider is shared across every vault that does not
    opt out via ``vault.abstraction.enabled = False``.

    Dispatch contract:
      1. SAGE_TEST_STUB_PROVIDERS=1 -> Stub (env override)
      2. stack.abstraction.provider == "stub" -> Stub (explicit opt-out)
      3. stack.abstraction.provider == "local-mlx"
         and stack.abstraction.model is None -> raise ValueError
      4. stack.abstraction.provider == "local-mlx"
         and stack.abstraction.model is not None -> local MLX provider (factory)
      5. stack.abstraction.provider == "anthropic"
         and stack.abstraction.model is None -> raise ValueError
      6. stack.abstraction.provider == "anthropic"
         and stack.abstraction.model is not None -> hosted Claude provider
      7. stack.abstraction.context_window is not None
         and provider is not "local-mlx" -> raise ValueError

    The env override remains the topmost short-circuit so that tests
    cannot load the local MLX model alongside the running MCP server (F-8).
    Provider/model live at stack scope because the local MLX provider is a
    process-wide singleton; co-locating the config with the resource
    boundary resolves the layering contradiction (ADR-030). The hosted
    'anthropic' provider is constructed without loading any local model and
    carries no unified-memory budget, so it is not serialized behind the
    local provider's generation lock.
    """
    if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1":
        return StubAbstractionProvider()

    abstraction = stack_config.abstraction
    _reject_context_window_for_non_local(abstraction)
    if abstraction.provider == "stub":
        return StubAbstractionProvider()
    if abstraction.provider == "local-mlx":
        if abstraction.model is None:
            raise ValueError(
                "sage_core_config.abstraction.model is required when "
                "abstraction.provider is 'local-mlx' (CAS-ADR-030). Set "
                "the model identifier in sage/config.yaml, or set "
                "abstraction.provider to 'stub' to opt the whole stack "
                "out of semantic abstract generation."
            )
        from sage.adapters.abstraction_qwen3 import get_qwen3_abstraction_provider

        return get_qwen3_abstraction_provider(
            model_id=abstraction.model,
            context_window=abstraction.context_window,
        )
    if abstraction.provider == "anthropic":
        if abstraction.model is None:
            raise ValueError(
                "sage_core_config.abstraction.model is required when "
                "abstraction.provider is 'anthropic' (CAS-ADR-030). Set "
                "the Claude model identifier in sage/config.yaml, or set "
                "abstraction.provider to 'stub' to opt the whole stack "
                "out of semantic abstract generation."
            )
        from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

        return AnthropicAbstractionProvider(model_id=abstraction.model)
    raise ValueError(  # pragma: no cover - schema-validated upstream
        f"Unknown stack abstraction provider: {abstraction.provider!r}"
    )


def _local_abstraction_binding(stack_config: SageCoreConfig) -> AbstractionProvider:
    """Late-binding factory for the local profile's abstraction seam.

    Delegates to the module-level ``build_stack_abstraction_provider`` by name
    rather than by captured reference, so a test that monkeypatches
    ``sage.mcp_init.build_stack_abstraction_provider`` is honored through the
    resolver path (the registry would otherwise pin the original function object
    captured at import and silently bypass the patch).
    """
    return build_stack_abstraction_provider(stack_config)


# Register the abstraction-provider binding for the local deployment profile
# (CAS-ADR-042). The abstraction provider is the keystone seam, and its binding
# is the whole stack-provider factory above: a future profile attaches its own
# abstraction binding by registering a different factory here, not by branching
# this dispatch. ``sage.profiles`` imports no SAGE runtime wiring, so the
# dependency is one-directional (mcp_init -> profiles) with no import cycle.
profiles.register_binding(
    profiles.LOCAL_PROFILE,
    profiles.ABSTRACTION_SEAM,
    _local_abstraction_binding,
)


def _local_storage_binding(stack_config: SageCoreConfig) -> VaultStorageProvisioner:
    """Late-binding factory for the local profile's durable-storage seam.

    Delegates to ``build_stack_storage_provisioner`` by module-global name
    rather than by captured reference, for the same reason as
    ``_local_abstraction_binding``: a test that monkeypatches
    ``sage.mcp_init.build_stack_storage_provisioner`` must be honored through
    the resolver path.
    """
    return build_stack_storage_provisioner(stack_config)


# Register the durable-storage binding for the local deployment profile
# (CAS-ADR-042). The binding is the Postgres provisioner in
# sage.storage_binding, authenticating from the environment or a peer-
# authenticated unix socket. A future profile attaches its own storage
# binding by registering a different factory here.
profiles.register_binding(
    profiles.LOCAL_PROFILE,
    profiles.STORAGE_SEAM,
    _local_storage_binding,
)


def _local_vault_source_binding(stack_config: SageCoreConfig) -> VaultSourceStore:
    """Late-binding factory for the local profile's vault-source seam.

    Delegates to ``build_stack_vault_source_store`` by module-global name (the
    same late-binding reason as ``_local_storage_binding``: a test that
    monkeypatches the builder must be honored through the resolver path). With
    no explicit root the builder defaults to ``default_vault_root()`` -- the
    on-box ``~/sage_vaults`` (or ``$SAGE_VAULT_ROOT``); the transport lifespans
    inject the root they already resolved (CAS-ADR-043).
    """
    return build_stack_vault_source_store(stack_config)


# Register the vault-source-store binding for the local deployment profile
# (CAS-ADR-043). The binding is the backend dispatch in
# sage.vault_source_binding: the filesystem store under the vault root by
# default, with the tenant document store selectable as the cloud-oriented
# alternative. A future profile attaches its own binding by registering a
# different factory here.
profiles.register_binding(
    profiles.LOCAL_PROFILE,
    profiles.VAULT_SOURCE_SEAM,
    _local_vault_source_binding,
)


def _local_auth_binding(stack_config: SageCoreConfig) -> TokenValidator:
    """Late-binding factory for the local profile's auth seam.

    Delegates to ``build_auth_validator`` by module-global name rather than
    by captured reference, for the same reason as ``_local_storage_binding``:
    a test that monkeypatches ``sage.mcp_init.build_auth_validator`` must be
    honored through the resolver path.
    """
    return build_auth_validator(stack_config.auth)


# Register the OAuth resource-server binding for the local deployment profile
# (CAS-ADR-042). The binding is the validator dispatch in sage.auth: a
# pass-through validator when the auth block is absent or disabled (the on-box
# default authenticates no one), an issuer/audience-bound JWT validator when
# it is enabled. A future profile attaches its own auth binding by registering
# a different factory here.
profiles.register_binding(
    profiles.LOCAL_PROFILE,
    profiles.AUTH_SEAM,
    _local_auth_binding,
)


def _cloud_abstraction_binding(stack_config: SageCoreConfig) -> AbstractionProvider:
    """Cloud-profile abstraction binding: hosted Claude with a Key-Vault key.

    Same dispatch as the local binding -- the ``SAGE_TEST_STUB_PROVIDERS`` env
    override stays topmost (the F-8 guard, so the suite never loads a real
    provider or reaches the secret store), then the explicit ``stub`` opt-out --
    except the hosted ``anthropic`` provider receives its API key from the
    managed secret store via managed identity rather than from the environment.
    The key is passed to the provider directly so it never transits the process
    environment. The hosted target ships no local MLX runtime, so a non-hosted
    provider fails closed with a clear error.
    """
    if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1":
        return StubAbstractionProvider()
    abstraction = stack_config.abstraction
    _reject_context_window_for_non_local(abstraction)
    if abstraction.provider == "stub":
        return StubAbstractionProvider()
    if abstraction.provider == "anthropic":
        if abstraction.model is None:
            raise ValueError(
                "sage_core_config.abstraction.model is required for the cloud "
                "profile's hosted abstraction provider (CAS-ADR-030). Set the "
                "Claude model identifier in the cloud stack config."
            )
        from sage.secrets.key_vault import (
            ANTHROPIC_SECRET_NAME,
            fetch_secret,
            resolve_vault_uri,
        )

        api_key = fetch_secret(resolve_vault_uri(), ANTHROPIC_SECRET_NAME)
        from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

        return AnthropicAbstractionProvider(model_id=abstraction.model, api_key=api_key)
    raise ValueError(
        "the cloud profile's abstraction binding supports the hosted "
        f"'anthropic' provider (or 'stub'), not {abstraction.provider!r}: the "
        "hosted target ships no local MLX runtime."
    )


def _cloud_storage_binding(stack_config: SageCoreConfig) -> VaultStorageProvisioner:
    """Cloud-profile durable-storage binding: Postgres over managed-identity auth.

    Delegates to ``build_stack_storage_provisioner`` (by module-global name, the
    same late-binding reason as the local bindings) with ``managed_identity=True``
    so the per-vault pool authenticates with a managed-identity Entra token: the
    cloud endpoint is Entra-only, with password auth disabled.
    """
    return build_stack_storage_provisioner(stack_config, managed_identity=True)


def _cloud_vault_source_binding(stack_config: SageCoreConfig) -> VaultSourceStore:
    """Cloud-profile vault-source binding: the document store under managed identity.

    Delegates to ``build_stack_vault_source_store`` (by module-global name, the
    same late-binding reason as the local bindings) with
    ``managed_identity=True`` so the document-store binding authenticates with
    the workload's managed identity. The backend dispatch still honors the
    config: a cloud stack that has not yet flipped ``vault_source_backend`` to
    ``document_store`` resolves the filesystem binding and keeps booting, so this
    registration does not by itself force the (stubbed) document store on a cloud
    deployment (CAS-ADR-043).
    """
    return build_stack_vault_source_store(stack_config, managed_identity=True)


def _cloud_auth_binding(stack_config: SageCoreConfig) -> TokenValidator:
    """Cloud-profile auth binding: the issuer/audience-bound JWT validator.

    The cloud profile authenticates every caller with an Entra-issued bearer
    token. The validator is the same one the local binding builds from the auth
    block -- non-secret issuer/audience coordinates only, no secret material --
    so this delegates to ``build_auth_validator`` by module-global name.
    """
    return build_auth_validator(stack_config.auth)


# Register the cloud deployment profile's three bindings (CAS-ADR-042). The
# cloud profile differs from the local profile only in where its secrets come
# from: the abstraction key is read from the managed secret store and the
# Postgres pool authenticates by managed-identity token, while the auth seam
# reuses the same JWT validator. The roster lives in the SAGE Deployment Profile
# Bindings steering document.
profiles.register_binding(
    profiles.CLOUD_PROFILE,
    profiles.ABSTRACTION_SEAM,
    _cloud_abstraction_binding,
)
profiles.register_binding(
    profiles.CLOUD_PROFILE,
    profiles.STORAGE_SEAM,
    _cloud_storage_binding,
)
profiles.register_binding(
    profiles.CLOUD_PROFILE,
    profiles.VAULT_SOURCE_SEAM,
    _cloud_vault_source_binding,
)
profiles.register_binding(
    profiles.CLOUD_PROFILE,
    profiles.AUTH_SEAM,
    _cloud_auth_binding,
)


def resolve_stack_profile(stack_config: SageCoreConfig) -> profiles.ResolvedProfile:
    """Resolve the active deployment profile from the stack config.

    Reads ``stack_config.profile`` and assembles its registered bindings once.
    The schema enum has already rejected an out-of-range profile value at
    config load, so the unknown-profile guard inside ``resolve_profile`` is the
    second line of defense.
    """
    return profiles.resolve_profile(stack_config.profile, stack_config)


def resolve_stack_abstraction_provider(stack_config: SageCoreConfig) -> AbstractionProvider:
    """Resolve the abstraction provider for the active deployment profile.

    Thin typed accessor over ``resolve_stack_profile``: returns the binding the
    active profile assembles for the abstraction seam. For the ``local`` profile
    that binding is ``build_stack_abstraction_provider``, so the result matches
    the direct-call behavior exactly while routing the construction path through
    the profile seam (CAS-ADR-042).
    """
    resolved = resolve_stack_profile(stack_config)
    return cast(AbstractionProvider, resolved.binding(profiles.ABSTRACTION_SEAM))


def resolve_stack_storage_provisioner(stack_config: SageCoreConfig) -> VaultStorageProvisioner:
    """Resolve the durable-storage provisioner for the active deployment profile.

    Thin typed accessor over ``resolve_stack_profile``, mirroring
    ``resolve_stack_abstraction_provider``: returns the binding the active
    profile assembles for the storage seam. For the ``local`` profile that
    binding is ``build_stack_storage_provisioner``'s backend dispatch
    (CAS-ADR-042).
    """
    resolved = resolve_stack_profile(stack_config)
    return cast(VaultStorageProvisioner, resolved.binding(profiles.STORAGE_SEAM))


def resolve_stack_vault_source_store(
    stack_config: SageCoreConfig, *, vault_root: Path | None = None
) -> VaultSourceStore:
    """Resolve the vault-source store for the active deployment profile (CAS-ADR-043).

    Thin typed accessor over ``resolve_stack_profile``, mirroring
    ``resolve_stack_storage_provisioner`` -- with one addition the storage seam
    does not need: a ``vault_root`` the transport lifespans inject. Vault
    discovery is a stack-level operation that runs before any per-vault call, and
    the filesystem root the lifespans resolved (from ``--vault-root`` /
    ``SAGE_VAULT_ROOT`` / the default) is a filesystem-binding-specific input the
    profile seam's ``(SageCoreConfig) -> binding`` factory signature cannot
    carry. When a root is injected, the binding is built directly so the
    explicitly-resolved root is honored (the filesystem path the lifespans take);
    the backend is still chosen by the config/env dispatch. The lifespans publish
    the resolved root to :func:`get_vault_root`, and every caller that needs the
    process-bound root -- discovery and the create-vault / update-config write
    paths alike -- injects it here. When no root is injected (in-process /
    injected-config paths, or a non-filesystem backend such as the cloud document
    store, which has no filesystem root), the binding resolves through the profile
    seam with the default root.
    """
    if vault_root is not None:
        return build_stack_vault_source_store(stack_config, vault_root=vault_root)
    resolved = resolve_stack_profile(stack_config)
    return cast(VaultSourceStore, resolved.binding(profiles.VAULT_SOURCE_SEAM))


def resolve_stack_auth_validator(stack_config: SageCoreConfig) -> TokenValidator:
    """Resolve the token validator for the active deployment profile.

    Thin typed accessor over ``resolve_stack_profile``, mirroring
    ``resolve_stack_abstraction_provider``: returns the binding the active
    profile assembles for the auth seam. For the ``local`` profile that
    binding is ``build_auth_validator``'s dispatch -- a pass-through
    validator unless the stack config's auth block is enabled (CAS-ADR-042).
    """
    resolved = resolve_stack_profile(stack_config)
    return cast(TokenValidator, resolved.binding(profiles.AUTH_SEAM))


# Closure-pair invariant: the canonical declaration of kwargs that
# every transport-reachable production call site of ``initialize_services``
# must thread. ``tests/sage/test_initialize_services_conformance.py`` walks
# every call site (MCP standalone lifespan + reload tool in sage/mcp_server.py,
# FastAPI lifespan in sage/app.py, standalone CLI in sage/migrate.py, the
# create-vault and reload feature-operation paths in sage/services/vault_registry.py
# / sage/mcp_init.reload_vault_in_registry) and asserts each captured kwargs
# dict has every key listed here. Adding a new must-thread kwarg is a
# deliberate one-line edit to this set, not an automatic consequence of
# growing the signature -- most signature kwargs are test-injection defaults
# that no transport-reachable code must override. Presence is the contract:
# ``registry_service=None`` (as in sage/migrate.py) satisfies the gate
# because the key is present; silent omission does not.
REQUIRED_TRANSPORT_KWARGS: frozenset[str] = frozenset({"config_path", "registry_service"})


async def initialize_services(
    config: VaultConfig,
    *,
    graph_store: GraphStore | None = None,
    graph_store_factory: Callable[[Path], GraphStore] | None = None,
    content_store: ContentStore | None = None,
    content_store_factory: Callable[[Path], ContentStore] | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    abstraction_provider: AbstractionProvider | None = None,
    migrate: bool = False,
    config_path: Path | None = None,
    registry_service: "VaultRegistryService | None" = None,
) -> SAGEServices:
    """Initialize all SAGE services for a vault configuration.

    Transactional: if any construction step raises, partially-allocated
    resources (timing thread, graph store, internally-constructed content
    store) are released on a best-effort basis before the original
    exception propagates. Cleanup-time exceptions are logged but never
    re-raised — the caller sees only the original failure. This is the
    structural counterpart to AC2's atomicity guarantee in
    ``reload_vault_in_registry``.

    Args:
        config: Loaded and validated vault configuration.
        graph_store: Optional override (default: built by the active
            profile's storage provisioner per the stack config's
            storage_backend key). When supplied, the caller owns the
            lifecycle and the store is assumed already initialized —
            cleanup on failure does NOT close it.
        graph_store_factory: Optional callable invoked with the vault's
            ``brain_root`` to build a GraphStore. Used by hermetic tests to
            substitute ``StubGraphStore`` without mutating module state.
            Ignored if ``graph_store`` is also passed; stored on the returned
            ``SAGEServices`` so reload paths reuse the same factory. When this
            factory builds the graph store, the caller owns the lifecycle —
            cleanup on failure does NOT close it.
        content_store: Optional override (default: built by the active
            profile's storage provisioner per the stack config's
            storage_backend key). When supplied, the caller owns the
            lifecycle — cleanup on failure does NOT close it.
        content_store_factory: Optional callable invoked with the vault's
            ``brain_root`` to build a ContentStore. Used by hermetic
            lifespan tests to substitute ``StubContentStore`` without
            mutating module state. Ignored if ``content_store`` is also
            passed; stored on the returned ``SAGEServices`` so reload
            paths reuse the same factory. When this factory builds the
            content store, the caller owns the lifecycle — cleanup on
            failure does NOT close it.
        embedding_provider: Optional override (default: NomicEmbeddingProvider).
        abstraction_provider: Optional override (default: from config).
        migrate: Threaded into the graph and content store initializers for
            port symmetry; the Postgres bindings provision their schema
            externally and treat every initialization statement as
            replace-or-create, so the flag does not change behavior.
        config_path: Source path of the vault_config.yaml file. Stored on
            the returned ``SAGEServices`` so that ``reload_vault`` can
            re-read the file from disk to pick up edits made externally.
        registry_service: Singleton VaultRegistryService used by
            VaultConfigService.update_config to perform the registry-mutation
            step of a config reload. Optional in test fixtures that never
            mutate the registry; required in normal app startup.

    Returns:
        SAGEServices dataclass with all services ready to use.
    """
    # Transactional cleanup handles for the build-fails-midway path.
    # Only resources THIS function constructed are tracked here; resources
    # passed in by the caller (explicit content_store, factory-built
    # content_store) are owned by the caller and not closed on cleanup.
    timing_thread: VaultTimingThread | None = None
    timing_handler: logging.Handler | None = None
    graph_store_owned_here: GraphStore | None = None
    content_store_owned_here: ContentStore | None = None
    storage_handle: VaultStorageHandle | None = None

    try:
        brain_root = Path(config.vault.brain_root).expanduser()
        brain_root.mkdir(parents=True, exist_ok=True)

        storage_timer, content_timer, retrieval_timer, timing_thread, timing_handler = (
            _build_vault_timers(config.timing, brain_root)
        )

        # Durable stores: explicit instance > factory > the provisioner the
        # active deployment profile binds for the storage seam (CAS-ADR-042) —
        # Postgres adapters over a per-vault connection pool.
        # Only provisioner-built stores (no external handle) are tracked for
        # cleanup; caller-supplied and factory-supplied stores remain the
        # caller's responsibility and are assumed ready for use. The
        # provisioner is consulted only for the slots injection leaves empty,
        # so a fully-injected call never pays a backend connection.
        if graph_store is None and graph_store_factory is not None:
            graph_store = graph_store_factory(brain_root)
        if content_store is None and content_store_factory is not None:
            content_store = content_store_factory(brain_root)

        need_graph = graph_store is None
        need_content = content_store is None
        if need_graph or need_content:
            provisioner = resolve_stack_storage_provisioner(get_stack_config())
            storage_handle = await provisioner.open_vault_storage(
                config.vault.id,
                brain_root,
                need_graph=need_graph,
                need_content=need_content,
                storage_timer=storage_timer,
                content_timer=content_timer,
                migrate=migrate,
            )
            if need_graph:
                graph_store = storage_handle.graph_store
                graph_store_owned_here = graph_store
            if need_content:
                content_store = storage_handle.content_store
                content_store_owned_here = content_store
        if graph_store is None or content_store is None:
            raise RuntimeError(
                "storage provisioner returned no store for a requested slot "
                f"(graph={graph_store!r}, content={content_store!r})"
            )

        lock_manager = DocumentLockManager()

        # Embedding provider: injected or production Nomic. CI sets
        # SAGE_TEST_STUB_PROVIDERS=1 so the ~700 tests that construct
        # services via this path don't each load the ~270MB nomic model
        # into a 7 GB Linux runner. Tests that exercise the real
        # adapter (@requires_embedding in test_adapters.py) construct
        # NomicEmbeddingProvider directly and are unaffected.
        if embedding_provider is None:
            if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1":
                from sage.adapters.stubs import StubEmbeddingProvider

                embedding_provider = StubEmbeddingProvider()
            else:
                embedding_provider = get_nomic_embedding_provider()

        # Abstraction provider: post CAS-ADR-030 /, the factory dispatch
        # lives in build_stack_abstraction_provider (stack scope). The per-vault
        # path here only consults the vault-scope opt-out. Precedence:
        # 1. vault.abstraction.enabled is False -> Stub (ADR-011 opt-in)
        # 2. abstraction_provider injected -> use injection
        # 3. SAGE_TEST_STUB_PROVIDERS=1 -> Stub (belt-and-suspenders
        # for tests that don't go through stack startup)
        # 4. no injection, env var unset -> raise (production path
        # must thread the stack-built provider through)
        if not config.abstraction.enabled:
            abstraction_provider = StubAbstractionProvider()
        elif abstraction_provider is None:
            if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1":
                abstraction_provider = StubAbstractionProvider()
            else:
                raise ValueError(
                    "initialize_services requires an `abstraction_provider` "
                    "injection in production (post CAS-ADR-030: the provider "
                    "is built once at SAGE process startup via "
                    "build_stack_abstraction_provider and passed into every "
                    "vault's initialize_services call). Tests that want a "
                    "Stub may set SAGE_TEST_STUB_PROVIDERS=1 or pass "
                    "StubAbstractionProvider() explicitly."
                )

        # Source adapters
        source_adapters = build_source_adapter_registry()

        # Services
        user_service = UserService(graph_store, config)
        lifecycle_service = LifecycleService(graph_store, lock_manager, config, content_store)
        metadata_service = MetadataService(graph_store, lock_manager, config, content_store)
        documents_service = DocumentsService(graph_store, config)
        # GraphOpsService is constructed before IngestionService so the
        # ingestion pipeline can run identifier_mention inference (which writes
        # edges via _create_edge) inside its Stage-2 → Stage-3 transition.
        graph_ops_service = GraphOpsService(graph_store, config)
        ingestion_service = IngestionService(
            graph_store=graph_store,
            lock_manager=lock_manager,
            content_store=content_store,
            embedding_provider=embedding_provider,
            abstraction_provider=abstraction_provider,
            config=config,
            source_adapters=source_adapters,
            lifecycle_service=lifecycle_service,
            graph_ops_service=graph_ops_service,
        )
        retrieval_service = RetrievalService(
            graph_store=graph_store,
            content_store=content_store,
            embedding_provider=embedding_provider,
            config=config,
            query_timer=retrieval_timer,
        )
        utilities_service = UtilitiesService(
            graph_store=graph_store,
            content_store=content_store,
            embedding_provider=embedding_provider,
            config=config,
        )
        staging_edges_service = StagingEdgesService(graph_store)
        vault_config_service = VaultConfigService(
            graph_store, content_store, config, registry_service
        )
        # CAS-ADR-029: only construct the maintenance service when a registry
        # service is available, since migrate_vault closes-and-reopens via
        # registry_service.reload(...).
        # Ingestion_service is wired through so reabstract_deferred can
        # reuse the in-process AbstractionProvider (F-8 budget rule); ordering
        # matters -- ingestion_service is constructed above this block.
        maintenance_service: MaintenanceService | None = None
        if registry_service is not None:
            maintenance_service = MaintenanceService(
                vault_id=config.vault.id,
                graph_store=graph_store,
                config=config,
                registry_service=registry_service,
                content_store=content_store,
                ingestion_service=ingestion_service,
            )

        # Bootstrap vault owner
        await user_service.bootstrap_owner()

        return SAGEServices(
            config=config,
            graph_store=graph_store,
            content_store=content_store,
            lock_manager=lock_manager,
            user_service=user_service,
            lifecycle_service=lifecycle_service,
            metadata_service=metadata_service,
            documents_service=documents_service,
            ingestion_service=ingestion_service,
            graph_ops_service=graph_ops_service,
            retrieval_service=retrieval_service,
            utilities_service=utilities_service,
            staging_edges_service=staging_edges_service,
            vault_config_service=vault_config_service,
            maintenance_service=maintenance_service,
            config_path=config_path,
            content_store_factory=content_store_factory,
            graph_store_factory=graph_store_factory,
            timing_thread=timing_thread,
            timing_handler=timing_handler,
            storage=storage_handle,
        )
    except BaseException:
        # AC2 + Risk: release partially-allocated resources without
        # masking the original exception. BaseException (not Exception) is
        # deliberate — KeyboardInterrupt and asyncio.CancelledError between
        # _build_vault_timers and the return statement would otherwise leak
        # the timing thread and graph store. Each cleanup is wrapped in its
        # own try/except so a cleanup-time failure does not skip the rest
        # nor mask the original. The original exception propagates via the
        # bare `raise` at the end.
        if timing_thread is not None:
            try:
                timing_thread.stop(timeout=1.0)
            except Exception:
                _logger.exception("initialize_services cleanup: failed to stop timing_thread")
        try:
            _release_timing_handler(timing_handler)
        except Exception:
            _logger.exception("initialize_services cleanup: failed to release timing handler")
        if graph_store_owned_here is not None:
            try:
                await graph_store_owned_here.close()
            except Exception:
                _logger.exception("initialize_services cleanup: failed to close graph_store")
        if content_store_owned_here is not None:
            try:
                close = getattr(content_store_owned_here, "close", None)
                if close is not None:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
            except Exception:
                _logger.exception("initialize_services cleanup: failed to close content_store")
        if storage_handle is not None:
            try:
                await storage_handle.close()
            except Exception:
                _logger.exception("initialize_services cleanup: failed to close storage handle")
        raise


async def reload_vault_in_registry(
    registry: dict[str, SAGEServices],
    vault_id: str,
    config: VaultConfig,
    config_path: Path | None = None,
    registry_service: "VaultRegistryService | None" = None,
) -> SAGEServices:
    """Atomically swap a vault's services in the registry.

    Build-new-first ordering: constructs the new services completely, then
    closes the old services and installs the new ones in the registry. If
    new-service construction raises, the registry is left unchanged — the
    old services remain installed and functional (graph store still open,
    timing thread still running). The caller can retry the reload after
    addressing the underlying cause; the exception propagates with no
    rollback ceremony required.

    Partial-allocation cleanup inside ``initialize_services`` is best-effort
    (see ``initialize_services``'s transactional cleanup block).

    Used by:
    - ``VaultRegistryService.reload`` (FastAPI PUT-config endpoint).
    - ``reload_vault`` MCP tool (via delegation).

    Carries the predecessor's ``content_store_factory`` / ``graph_store_factory``
    and (when the caller does not supply one) ``config_path`` forward so
    hermetic-lifespan-test setups survive reload and on-disk YAML edits
    round-trip correctly.
    """
    old = registry.get(vault_id)
    content_store_factory = None
    graph_store_factory = None
    if old is not None:
        if config_path is None:
            config_path = old.config_path
        content_store_factory = old.content_store_factory
        graph_store_factory = old.graph_store_factory

    # CAS-ADR-042: resolve the active deployment profile and thread its
    # abstraction binding through. For the local profile this is the same
    # stack-built provider as before (CAS-ADR-030). Falls back to the default
    # stack config when no lifespan has run (test paths that exercise reload
    # directly).
    stack_provider = resolve_stack_abstraction_provider(get_stack_config())

    # Build new BEFORE touching old. If initialize_services raises,
    # ``old`` remains installed in the registry and fully functional. The
    # exception propagates; partial-allocation cleanup inside
    # initialize_services releases timing_thread / graph_store /
    # internally-constructed content_store on the failed path.
    new_services = await initialize_services(
        config,
        config_path=config_path,
        registry_service=registry_service,
        graph_store_factory=graph_store_factory,
        content_store_factory=content_store_factory,
        abstraction_provider=stack_provider,
    )

    # New services built successfully — safe to tear down old and install new.
    if old is not None:
        await old.ingestion_service.stop_worker()
        old.close_timing()
        await old.close_storage()
    registry[vault_id] = new_services
    return new_services
