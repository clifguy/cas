import { Link } from 'react-router-dom';
import { bloatState, type BloatState } from '../utils/bloat';

const STATE_STYLE: Record<BloatState, React.CSSProperties> = {
  ok: { background: '#f5f5f5', color: '#333' },
  warn: { background: '#fff3e0', color: '#e65100' },
  red: { background: '#fce4ec', color: '#c62828' },
};

/**
 * Vault-health card for vector-database content-store bloat. Surfaces the retained
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
