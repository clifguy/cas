import { Link } from 'react-router-dom';

export type BloatState = 'ok' | 'warn' | 'red';

// Retained content-store versions rise monotonically with un-optimized write
// churn and are independent of corpus size, so fixed thresholds stay
// meaningful across vaults: a handful is healthy; tens signal accumulating
// drift; ~50+ matches the observed reclamation pathology.
const WARN_THRESHOLD = 20;
const RED_THRESHOLD = 50;

/** Map a retained-version count to a self-calibrating ok/warn/red state. */
export function bloatState(versionCount: number): BloatState {
  if (versionCount >= RED_THRESHOLD) return 'red';
  if (versionCount >= WARN_THRESHOLD) return 'warn';
  return 'ok';
}

const STATE_STYLE: Record<BloatState, React.CSSProperties> = {
  ok: { background: '#f5f5f5', color: '#333' },
  warn: { background: '#fff3e0', color: '#e65100' },
  red: { background: '#fce4ec', color: '#c62828' },
};

/**
 * Vault-health card for LanceDB content-store bloat. Surfaces the retained
 * dataset-version count as a thresholded state rather than a bare number, and
 * points a drifting store at the Maintenance-page optimize action.
 */
export default function BloatIndicator({ versionCount }: { versionCount: number }) {
  const state = bloatState(versionCount);
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
      <div data-testid="bloat-count" style={{ fontSize: 24, fontWeight: 700 }}>
        {versionCount}
      </div>
      <div style={{ fontSize: 12, color: '#666' }}>Content-store versions</div>
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
