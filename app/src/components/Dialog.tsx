import { useEffect, type ReactNode } from 'react';

interface Props {
  title: string;
  onClose: () => void;
  children: ReactNode;
  width?: number;
}

export function Dialog({ title, onClose, children, width = 480 }: Props) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      role="presentation"
      style={backdropStyle}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div role="dialog" aria-label={title} style={{ ...cardStyle, width }}>
        <div style={headerStyle}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            style={closeBtnStyle}
          >
            ×
          </button>
        </div>
        <div style={bodyStyle}>{children}</div>
      </div>
    </div>
  );
}

const backdropStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.4)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
};
const cardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 6,
  boxShadow: '0 4px 20px rgba(0, 0, 0, 0.2)',
  maxHeight: '90vh',
  display: 'flex',
  flexDirection: 'column',
};
const headerStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  padding: '12px 16px',
  borderBottom: '1px solid #eee',
};
const closeBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  fontSize: 22,
  lineHeight: 1,
  cursor: 'pointer',
  color: '#666',
  padding: 0,
};
const bodyStyle: React.CSSProperties = {
  padding: 16,
  overflowY: 'auto',
};
