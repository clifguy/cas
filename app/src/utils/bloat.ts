export type BloatState = 'ok' | 'warn' | 'red';

// Retained content-store versions rise monotonically with un-optimized write
// churn and are independent of corpus size, so fixed thresholds stay
// meaningful across vaults: a handful is healthy; tens signal accumulating
// drift; ~50+ matches the observed reclamation pathology.
const VERSION_WARN_THRESHOLD = 20;
const VERSION_RED_THRESHOLD = 50;

// Small (un-compacted) fragments are likewise corpus-size-independent: a
// healthy, recently-optimized store sits near zero. Thresholds are provisional
// (no empirical baseline yet) and set conservatively to avoid false alarms.
const SMALL_FRAGMENT_WARN_THRESHOLD = 10;
const SMALL_FRAGMENT_RED_THRESHOLD = 25;

const SEVERITY: Record<BloatState, number> = { ok: 0, warn: 1, red: 2 };

function versionState(versionCount: number): BloatState {
  if (versionCount >= VERSION_RED_THRESHOLD) return 'red';
  if (versionCount >= VERSION_WARN_THRESHOLD) return 'warn';
  return 'ok';
}

function smallFragmentState(smallFragmentCount: number): BloatState {
  if (smallFragmentCount >= SMALL_FRAGMENT_RED_THRESHOLD) return 'red';
  if (smallFragmentCount >= SMALL_FRAGMENT_WARN_THRESHOLD) return 'warn';
  return 'ok';
}

/**
 * Map the retained-version and small-fragment counts to a single
 * self-calibrating ok/warn/red state, taking the worse of the two signals so
 * either dimension can flag a drifting store.
 */
export function bloatState(versionCount: number, smallFragmentCount: number): BloatState {
  const vs = versionState(versionCount);
  const fs = smallFragmentState(smallFragmentCount);
  return SEVERITY[vs] >= SEVERITY[fs] ? vs : fs;
}
