"""Activation and write-time enforcement of declared tier3 uniqueness.

A ``unique_keys`` declaration is enforced by a per-(doc_type, field) partial
UNIQUE index (CAS-ADR-031). The declaration lives in vault configuration and
the index in the store, so the two can disagree: a configuration can arrive
before its index exists, and the index cannot be built while the portfolio
already collides. This module keeps them in step from the service layer:

* activation builds the index for every clean declaration and reports each
  declaration whose portfolio collides, without building it;
* the write gate refuses a write governed by a declaration whose index is
  absent, so a declared constraint is never silently unenforced.

Every caller -- vault load, the config write paths, ``migrate_vault``,
ingest and metadata update -- goes through these functions, so the rules
are stated once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from sage.adapters.interfaces import GraphStore
from sage.api.errors import Tier3UniqueConstraintInactiveError
from sage.config import VaultConfig
from sage.models.schemas import Tier3UniquenessActivation, Tier3UniquenessCollision
from sage.storage.tier3_uniqueness import Tier3UniqueIndexBlockedError, tier3_unique_index_name

logger = logging.getLogger(__name__)

UniquePair = tuple[str, str]


def declared_unique_pairs(config: VaultConfig) -> list[UniquePair]:
    """Every (doc_type, field) pair the configuration declares unique, in
    declaration order."""
    return [
        (dt.value, field)
        for dt in config.document_types.doc_types
        for field in dt.unique_keys or []
    ]


def declared_unique_fields(config: VaultConfig, doc_type: str | None) -> list[str]:
    """The fields ``doc_type`` declares under ``unique_keys``."""
    if doc_type is None:
        return []
    entry = next((dt for dt in config.document_types.doc_types if dt.value == doc_type), None)
    return list(entry.unique_keys or []) if entry is not None else []


async def scan_unique_collisions(
    store: GraphStore, doc_type: str, field: str
) -> list[Tier3UniquenessCollision]:
    """Values of ``field`` held by more than one chain head of ``doc_type``.

    A supersession chain is one logical artifact (CAS-ADR-031 §3), so only
    chain heads are compared; an archived head counts, a superseded
    predecessor does not.
    """
    groups = await store.find_chain_heads_with_tier3_value(doc_type, field)
    return [
        Tier3UniquenessCollision(
            doc_type=doc_type, field=field, value=value, document_ids=sorted(doc_ids)
        )
        for value, doc_ids in groups
        if len(doc_ids) > 1
    ]


@dataclass
class Activation:
    """What one activation pass found and did."""

    activations: list[Tier3UniquenessActivation] = dataclass_field(default_factory=list)
    collisions: list[Tier3UniquenessCollision] = dataclass_field(default_factory=list)
    # Pairs whose index this pass built, as distinct from one it confirmed.
    built: list[UniquePair] = dataclass_field(default_factory=list)


async def activate_unique_pairs(
    store: GraphStore, pairs: Iterable[UniquePair], *, tolerate_errors: bool = False
) -> Activation:
    """Build the index for each clean pair; report each colliding one.

    A pair whose index already exists is confirmed without a scan. A
    colliding pair is left without an index (CAS-ADR-031 §5), and an index
    that already exists is never dropped here. The index build is itself
    the authority: a duplicate written between the scan and the build fails
    the build, which is reported as a collision rather than raised.

    With ``tolerate_errors`` any other failure on a pair -- a lock wait
    that timed out, a lost connection -- is logged and the pair left
    unenforced, so its writes are refused while everything else proceeds.
    Vault load uses it: an activation that cannot complete must not take
    the vault's reads down with it.
    """
    result = Activation()
    for doc_type, unique_field in pairs:
        try:
            await _activate_pair(store, doc_type, unique_field, result)
        except Exception:
            if not tolerate_errors:
                raise
            logger.exception(
                "unique_keys %s.%s could not be activated; writes setting it are refused",
                doc_type,
                unique_field,
            )
    return result


async def _activate_pair(store: GraphStore, doc_type: str, field: str, result: Activation) -> None:
    """Activate one pair, recording the outcome in ``result``."""
    if not await store.tier3_unique_index_exists(doc_type, field):
        found = await scan_unique_collisions(store, doc_type, field)
        if found:
            result.collisions.extend(found)
            return
        try:
            await store.ensure_tier3_unique_index(doc_type, field)
        except Tier3UniqueIndexBlockedError as exc:
            rescanned = await scan_unique_collisions(store, exc.doc_type, exc.field)
            result.collisions.extend(
                rescanned
                or [
                    Tier3UniquenessCollision(
                        doc_type=exc.doc_type,
                        field=exc.field,
                        value="<reported by the store, value not recovered>",
                        document_ids=[],
                    )
                ]
            )
            return
        result.built.append((doc_type, field))
    result.activations.append(Tier3UniquenessActivation(doc_type=doc_type, field=field))


async def drop_undeclared_unique_indexes(store: GraphStore, config: VaultConfig) -> list[str]:
    """Drop every tier3 unique index the configuration no longer declares."""
    return await store.drop_tier3_unique_indexes_except(
        {tier3_unique_index_name(*pair) for pair in declared_unique_pairs(config)}
    )


async def inactive_unique_pairs(store: GraphStore, config: VaultConfig) -> list[UniquePair]:
    """Declared pairs whose index is absent."""
    return [
        (doc_type, field)
        for doc_type, field in declared_unique_pairs(config)
        if not await store.tier3_unique_index_exists(doc_type, field)
    ]


async def active_unique_fields(store: GraphStore, config: VaultConfig, doc_type: str) -> list[str]:
    """Sorted fields of ``doc_type`` whose declared uniqueness is enforced."""
    return sorted(
        [
            field
            for field in declared_unique_fields(config, doc_type)
            if await store.tier3_unique_index_exists(doc_type, field)
        ]
    )


async def require_active_unique_keys(
    store: GraphStore,
    config: VaultConfig,
    doc_type: str | None,
    fields: Iterable[str],
) -> None:
    """Refuse a write that sets a declared-unique field whose index is absent.

    ``fields`` are the tier3 keys the write sets to a non-null value. A
    declared field among them whose index does not exist raises
    ``tier3_unique_constraint_inactive``: the substrate cannot promise the
    uniqueness the declaration states, and accepting the write would let a
    duplicate land silently. The index is probed on every call, so an index
    dropped out of band is seen at once.
    """
    carried = set(fields)
    for field in declared_unique_fields(config, doc_type):
        if field in carried and not await store.tier3_unique_index_exists(doc_type, field):
            raise Tier3UniqueConstraintInactiveError(doc_type=doc_type, field=field)


def set_fields(tier3: dict | None) -> list[str]:
    """The keys a tier3 payload sets to a non-null value."""
    return [key for key, value in (tier3 or {}).items() if value is not None]
