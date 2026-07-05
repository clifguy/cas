"""Graph-store port value types: edge-query rows and link-read context.

Return shapes shared between the ``GraphStore`` port
(``sage.adapters.interfaces``) and its concrete implementations
(Postgres today). They live in the models leaf so the port can reference
them without importing any concrete store module — keeping the port's
dependency direction one-way (port -> models) and substitution clean.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sage.models.schemas import Edge


@dataclass(frozen=True)
class EdgeQueryRow:
    """Edge enumeration result row with computed retraction envelope.

    Wraps a hydrated ``Edge`` and adds two fields computed via LEFT JOIN
    against the earliest ``retracts``-type edge that disclaims this row:
    ``retracted_at`` carries the timestamp of that disclaiming edge,
    ``retracted_by_edge_id`` its id. Both are ``None`` when this row is
    still live (no disclaiming retracts edge exists). For rows that are
    themselves ``retracts`` edges, these fields are likewise ``None``
    (a retracts edge isn't itself subject to retraction); the row's own
    ``edge.retracted_edge_id`` carries the id of the edge it disclaims.
    """

    edge: Edge
    retracted_at: datetime | None
    retracted_by_edge_id: str | None


OnConflict = Literal["raise", "noop"]


@dataclass(frozen=True)
class LinkReadContext:
    """Pre-fetched state needed to validate and execute a LinkRequest.

    Populated by the graph store's ``read_link_context`` in a single
    executor submission so the service layer can validate without issuing
    further per-query round-trips. Fields that are not applicable to the
    request's edge type are left at their default (empty / False / None).
    """

    source_exists: bool
    target_exists: bool
    retracted_edge: Edge | None = None
    source_lineage: frozenset[str] = field(default_factory=frozenset)
    target_lineage: frozenset[str] = field(default_factory=frozenset)
    source_anchor_exists: bool = True
    target_anchor_exists: bool = True
    has_sup_predecessor: bool = False
    has_sup_successor: bool = False
    tombstone_candidates: tuple[str, ...] = ()
