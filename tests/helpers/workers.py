"""Size the parallel test run from the machine and the Postgres connection budget."""

from __future__ import annotations

# Connections left for anything that is not a pytest worker: the developer's
# own sessions, a running SAGE server, autovacuum and other background
# processes.
CONNECTION_RESERVE = 20

# Connections one worker can hold at once: a storage pool at its default
# ``max_pool_size`` of 10, the session hygiene connection, and the keep-alive
# the provisioner holds on the throwaway database.
CONNECTIONS_PER_WORKER = 12


def worker_budget(
    cpu_count: int | None,
    max_connections: int | None,
    *,
    reserve: int = CONNECTION_RESERVE,
    per_worker: int = CONNECTIONS_PER_WORKER,
) -> int:
    """Worker count for ``-n auto``: the smaller of the CPU count and what the
    Postgres connection ceiling can carry, never below one.

    The connection ceiling is the binding constraint on a workstation with
    more cores than a default ``max_connections`` of 100 can serve; without a
    server to ask, the CPU count stands alone.
    """
    cpus = max(1, cpu_count or 1)
    if max_connections is None:
        return cpus
    by_connections = (max_connections - reserve) // per_worker
    return max(1, min(cpus, by_connections))
