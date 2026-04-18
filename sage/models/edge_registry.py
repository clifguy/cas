"""Edge-type resolution-policy registry (CAS-ADR-017).

Each vault has a registry assigning a ResolutionPolicy to every edge type
the vault uses. The policy is consulted at edge creation time (write-time
invariant enforcement) and frozen onto the edge row; later registry edits
do not retroactively change resolution behavior for existing edges.

Chunk 2 provides a built-in default registry matching the 11-row table
frozen in CAS-ADR-017. Vault-config-driven loading is tracked for a
later chunk.
"""

from sage.models.enums import EdgeType, ResolutionPolicy


_DEFAULT_POLICIES: dict[EdgeType, ResolutionPolicy] = {
    EdgeType.SUPERSEDES: ResolutionPolicy.NONE,
    EdgeType.RETRACTS: ResolutionPolicy.NONE,
    EdgeType.MERGED_FROM: ResolutionPolicy.NONE,
    EdgeType.DERIVED_FROM: ResolutionPolicy.TRANSITIVE_SOURCE,
    EdgeType.INSTANTIATED_FROM: ResolutionPolicy.TRANSITIVE_BOTH,
    EdgeType.REFERENCES: ResolutionPolicy.TRANSITIVE_BOTH,
    EdgeType.COVERS: ResolutionPolicy.TRANSITIVE_BOTH,
    EdgeType.BUNDLES_WITH: ResolutionPolicy.TRANSITIVE_BOTH,
    EdgeType.DEPENDS_ON: ResolutionPolicy.TRANSITIVE_BOTH,
    EdgeType.AUTHORITATIVE_FOR: ResolutionPolicy.TBD,
    EdgeType.SYNC_TARGET: ResolutionPolicy.TBD,
}


class EdgeTypeRegistry:
    def __init__(self, policies: dict[EdgeType, ResolutionPolicy]) -> None:
        self._policies = dict(policies)

    @classmethod
    def default(cls) -> "EdgeTypeRegistry":
        return cls(_DEFAULT_POLICIES)

    def policy_for(self, edge_type: EdgeType) -> ResolutionPolicy:
        try:
            return self._policies[edge_type]
        except KeyError as e:
            raise KeyError(
                f"No resolution_policy registered for edge_type {edge_type.value!r}"
            ) from e
