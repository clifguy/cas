"""Deployment-profile seam for the SAGE stack (CAS-ADR-042).

A deployment profile is the single stack-scope selection that co-binds the
adapter-port implementations for one deployment target. This module is the
generic registration-and-resolution mechanism: each adapter binding registers
itself against a profile via :func:`register_binding`, and
:func:`resolve_profile` assembles the registered bindings for the active
profile once at stack startup.

The mechanism is deliberately thin (CAS-ADR-042 implementation-discipline
clause): registration plus single-binding-per-seam assembly, with no selection
machinery among competing bindings -- that lands only once a seam has a second
binding. The module is dependency-light (it imports no SAGE runtime wiring),
so the construction path it sits on carries no platform coupling and the
binding factories stay the sole owners of any environment-specific import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sage.config import SageCoreConfig

# The on-box, single-process deployment target. Its bindings read their secrets
# from the environment (the abstraction key, the Postgres password).
LOCAL_PROFILE = "local"

# The hosted, containerized deployment target. Its bindings resolve their secrets
# from the managed secret store via the workload's managed identity -- no secret
# value lives in the environment, the image, or the repository -- and the
# Postgres endpoint authenticates by managed-identity Entra token.
CLOUD_PROFILE = "cloud"

# Seam name for the abstraction-provider binding -- the keystone seam
# (CAS-ADR-042): the only hard environmental coupling, and therefore the
# binding whose substitutability determines whether the rest of the server is
# portable at all.
ABSTRACTION_SEAM = "abstraction_provider"

# Seam name for the durable-storage binding (CAS-ADR-042): the provisioner
# that opens a vault's graph and content stores as one co-varying pair --
# Postgres adapters over a per-vault pool, or the embedded SQLite/LanceDB
# fallback. One seam rather than two because the stores share their backing
# resource (the pool) and the embedded pair is one coherent fallback binding.
STORAGE_SEAM = "storage_provisioner"

# Seam name for the vault-source-store binding (CAS-ADR-043): the store that
# persists the vault configuration declaration and the retained ingest source
# files -- the durable seam CAS-ADR-042 left on the local filesystem. The
# filesystem binding (today's behavior) or a tenant-native document-store
# binding, selected by the stack config's ``vault_source_backend`` key, on the
# same swappable-per-profile footing as the storage seam.
VAULT_SOURCE_SEAM = "vault_source_store"

# Seam name for the OAuth resource-server binding (CAS-ADR-042): the bearer
# token validator that authorizes calls to the HTTP surfaces. A pass-through
# validator where the deployment authenticates no one (the on-box default),
# an issuer/audience-bound JWT validator where it does -- selected by the
# auth block of the stack config, with the same validator enforcing the
# policy on the REST and MCP surfaces so authorization is uniform.
AUTH_SEAM = "auth_validator"

# A binding factory assembles one seam's implementation from the stack config.
BindingFactory = Callable[[SageCoreConfig], object]


class _LazyBindings(Mapping[str, object]):
    """Seam-name-to-binding mapping that runs each factory once, on demand.

    Assembly is per-seam lazy so that asking for one seam's binding can never
    fail (or pay a construction cost) on an unrelated seam's account -- a
    caller resolving the storage binding must not trip over an abstraction
    misconfiguration, and vice versa. Membership checks (``seam in bindings``)
    consult the factory registry without building anything.
    """

    def __init__(self, factories: Mapping[str, "BindingFactory"], stack_config) -> None:
        self._factories = dict(factories)
        self._stack_config = stack_config
        self._built: dict[str, object] = {}

    def __getitem__(self, seam: str) -> object:
        if seam not in self._factories:
            raise KeyError(seam)
        if seam not in self._built:
            self._built[seam] = self._factories[seam](self._stack_config)
        return self._built[seam]

    def __iter__(self):
        return iter(self._factories)

    def __len__(self) -> int:
        return len(self._factories)

    def __contains__(self, seam: object) -> bool:
        return seam in self._factories


@dataclass(frozen=True)
class ResolvedProfile:
    """The assembled bindings for one resolved deployment profile.

    ``bindings`` maps a seam name to the object that seam's registered factory
    produces from the stack config -- lazily, once per seam, on first access.
    :meth:`binding` is the accessor a caller casts at the call site to the
    seam's known port type.
    """

    profile: str
    bindings: Mapping[str, object]

    def binding(self, seam: str) -> object:
        """Return the assembled binding for ``seam``.

        Raises ``KeyError`` when the resolved profile carries no binding for
        the seam -- a programming error, since a caller asks only for the
        seams it knows the active profile populates.
        """
        return self.bindings[seam]


# profile name -> seam name -> factory. Module-level so a binding registers
# once at import time and every resolver call sees it.
_REGISTRY: dict[str, dict[str, BindingFactory]] = {}


def register_binding(profile: str, seam: str, factory: BindingFactory) -> None:
    """Attach an adapter ``factory`` to one ``seam`` of one ``profile``.

    This is the contract by which a new binding joins a profile: register it,
    rather than branching selection logic across the config consumers.
    Re-registering the same ``(profile, seam)`` replaces the factory, which
    keeps the call idempotent under module re-import.
    """
    _REGISTRY.setdefault(profile, {})[seam] = factory


def registered_profiles() -> frozenset[str]:
    """Return the profile names that currently have at least one binding."""
    return frozenset(_REGISTRY)


def resolve_profile(profile: str, stack_config: SageCoreConfig) -> ResolvedProfile:
    """Assemble the registered bindings for ``profile`` from ``stack_config``.

    Each factory registered for the profile is called once with the stack
    config, lazily on the seam's first access (see :class:`_LazyBindings`),
    and the results are reachable by seam name on the returned
    :class:`ResolvedProfile`. An unregistered profile fails loud here -- the
    resolver never silently returns an empty assembly -- which is the second
    line of defense behind the schema-level enum that rejects an unknown
    profile value at config load.
    """
    seams = _REGISTRY.get(profile)
    if seams is None:
        raise ValueError(
            f"Unknown deployment profile {profile!r}; no bindings registered. "
            f"Registered profiles: {sorted(_REGISTRY)}."
        )
    return ResolvedProfile(profile=profile, bindings=_LazyBindings(seams, stack_config))
