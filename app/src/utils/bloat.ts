export type BloatState = 'ok' | 'warn' | 'red';

// Postgres default heap block size. The content store reports reclaimable free
// space as a whole-page count (free_space / block_size), so multiplying back by
// this reconstructs the byte figure exactly.
export const PG_BLOCK_SIZE = 8192;

// Dead tuples accumulate with write churn (each re-index is a delete + insert)
// and scale with table size, so the meaningful signal is the dead-tuple
// fraction, not an absolute count. Autovacuum's default trigger is 20% of live
// rows (autovacuum_vacuum_scale_factor); below that Postgres reclaims routinely
// on its own. Warn at that line, red at twice it — a store that stays there is a
// sign autovacuum is disabled or falling behind.
const DEAD_TUPLE_WARN_RATIO = 0.2;
const DEAD_TUPLE_RED_RATIO = 0.4;

/**
 * Dead-tuple fraction of the chunks relation: dead / (dead + live). Returns 0
 * for an empty table so the ratio is well-defined before any rows exist.
 */
export function deadTupleRatio(deadTuples: number, liveChunks: number): number {
  const total = deadTuples + liveChunks;
  return total === 0 ? 0 : deadTuples / total;
}

/**
 * Map the dead-tuple fraction to a single ok/warn/red state anchored to
 * autovacuum's default 20% trigger. Free space is deliberately excluded: it is
 * reused by future inserts and is not itself a health problem, so it must not
 * drive the alarm.
 */
export function bloatState(deadTuples: number, liveChunks: number): BloatState {
  const ratio = deadTupleRatio(deadTuples, liveChunks);
  if (ratio >= DEAD_TUPLE_RED_RATIO) return 'red';
  if (ratio >= DEAD_TUPLE_WARN_RATIO) return 'warn';
  return 'ok';
}

/**
 * Approximate the disk a VACUUM FULL would return to the OS, from the store's
 * reclaimable free-page count. Informational only.
 */
export function reclaimableBytes(freePages: number): number {
  return freePages * PG_BLOCK_SIZE;
}
