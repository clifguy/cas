interface Props {
  count: number;
  onSetLifecycle: () => void;
  onUpdateMetadata: () => void;
  onClear: () => void;
}

export function BulkActionBar({ count, onSetLifecycle, onUpdateMetadata, onClear }: Props) {
  return (
    <div data-testid="bulk-action-bar" style={barStyle}>
      <span data-testid="bulk-action-bar-count" style={{ fontWeight: 600, fontSize: 13 }}>
        {count} selected
      </span>
      <div style={{ display: 'flex', gap: 8, marginLeft: 'auto' }}>
        <button type="button" onClick={onSetLifecycle} style={primaryBtnStyle}>
          Set lifecycle&hellip;
        </button>
        <button type="button" onClick={onUpdateMetadata} style={primaryBtnStyle}>
          Update metadata&hellip;
        </button>
        <button type="button" onClick={onClear} style={secondaryBtnStyle} aria-label="Clear selection">
          Clear
        </button>
      </div>
    </div>
  );
}

const barStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '8px 12px',
  marginBottom: 8,
  background: '#fff8e1',
  border: '1px solid #ffe082',
  borderRadius: 4,
};
const primaryBtnStyle: React.CSSProperties = {
  padding: '4px 14px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 12,
};
const secondaryBtnStyle: React.CSSProperties = {
  padding: '4px 14px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#fff',
  color: '#333',
  cursor: 'pointer',
  fontSize: 12,
};
