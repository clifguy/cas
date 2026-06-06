import { Link } from 'react-router-dom';

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

const STATE_STYLE: Record<BloatState, React.CSSProperties> = {
  ok: { background: '#f5f5f5', color: '#333' },
  warn: { background: '#fff3e0', color: '#e65100' },
  red: { background: '#fce4ec', color: '#c62828' },
};

/**
 * Vault-health card for LanceDB content-store bloat. Surfaces the retained
 * dataset-version count and the small (un-compacted) fragment count as a
 * thresholded state rather than bare numbers, and points a drifting store at
 * the Maintenance-page optimize action.
 */
export default function BloatIndicator({
  versionCount,
  smallFragmentCount,
}: {
  versionCount: number;
  smallFragmentCount: number;
}) {
  const state = bloatState(versionCount, smallFragmentCount);
  const flagged = state !== 'ok';
  return (
    <div
      data-testid="bloat-card"
      data-bloat-state={state}
      style={{
        border: '1px solid #ddd',
        borderRadius: 4,
        padding: '12px 16px',
        height: '100%',
        boxSizing: 'border-box',
        ...STATE_STYLE[state],
      }}
    >
      <div style={{ fontSize: 24, fontWeight: 700 }}>
        <span data-testid="bloat-version-count">{versionCount}</span>
        <span style={{ color: '#999' }}> / </span>
        <span data-testid="bloat-small-fragment-count">{smallFragmentCount}</span>
      </div>
      <div style={{ fontSize: 12, color: '#666' }}>Content-store versions / small fragments</div>
      {flagged && (
        <Link
          to="/maintenance"
          style={{ display: 'inline-block', marginTop: 4, fontSize: 12, color: '#1565c0', textDecoration: 'none' }}
        >
          Optimize content store
        </Link>
      )}
    </div>
  );
}
