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

# The on-box, single-process deployment target. The only profile the resolver
# assembles today; additional targets (e.g. a hosted profile) register their
# bindings as the cloud adapters land.
LOCAL_PROFILE = "local"

# Seam name for the abstraction-provider binding -- the keystone seam
# (CAS-ADR-042): the only hard environmental coupling, and therefore the
# binding whose substitutability determines whether the rest of the server is
# portable at all.
ABSTRACTION_SEAM = "abstraction_provider"

# A binding factory assembles one seam's implementation from the stack config.
BindingFactory = Callable[[SageCoreConfig], object]


@dataclass(frozen=True)
class ResolvedProfile:
    """The assembled bindings for one resolved deployment profile.

    ``bindings`` maps a seam name to the object that seam's registered factory
    produced from the stack config. :meth:`binding` is the accessor a caller
    casts at the call site to the seam's known port type.
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
    config; the results are collected by seam name into a
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
    assembled = {seam: factory(stack_config) for seam, factory in seams.items()}
    return ResolvedProfile(profile=profile, bindings=assembled)
