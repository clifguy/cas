import { Link } from 'react-router';
import { bloatState, deadTupleRatio, reclaimableBytes, type BloatState } from '../utils/bloat';
import { formatBytes } from '../utils/format';

const STATE_STYLE: Record<BloatState, React.CSSProperties> = {
  ok: { background: '#f5f5f5', color: '#333' },
  warn: { background: '#fff3e0', color: '#e65100' },
  red: { background: '#fce4ec', color: '#c62828' },
};

/**
 * Vault-health card for Postgres content-store bloat. Surfaces the dead-tuple
 * fraction of the content store as a thresholded ok/warn/red state (anchored
 * to autovacuum's 20% trigger), with the reclaimable free space shown as an
 * informational figure. Points a genuinely bloated store at the Maintenance-page
 * VACUUM action.
 */
export default function BloatIndicator({
  deadTuples,
  liveRows,
  freePages,
}: {
  deadTuples: number;
  liveRows: number;
  freePages: number;
}) {
  const state = bloatState(deadTuples, liveRows);
  const flagged = state !== 'ok';
  const pct = deadTupleRatio(deadTuples, liveRows) * 100;
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
        <span data-testid="bloat-dead-pct">{pct.toFixed(1)}%</span>
      </div>
      <div style={{ fontSize: 12, color: '#666' }}>
        Dead-row bloat ({deadTuples} dead / {liveRows} live)
      </div>
      <div data-testid="bloat-reclaimable" style={{ fontSize: 12, color: '#666' }}>
        ~{formatBytes(reclaimableBytes(freePages))} reclaimable
      </div>
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
