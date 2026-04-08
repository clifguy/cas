import { useState, useEffect, useRef } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import { vaults, type ScanFile } from '../mock/data';

export default function Ingest() {
  const { vaultId } = useOutletContext<{ vaultId: string }>();
  const vault = vaults[vaultId];
  const [step, setStep] = useState(1);
  const [directory, setDirectory] = useState('');
  const [files, setFiles] = useState<(ScanFile & { selected: boolean })[]>([]);
  const [progress, setProgress] = useState({ current: 0, total: 0, currentFile: '', stage: '' });
  const [log, setLog] = useState<string[]>([]);
  const timerRef = useRef<number | null>(null);

  if (!vault) return <div>Vault not found.</div>;

  function handleScan() {
    if (!directory.trim()) return;
    const enriched = vault.scan_files.map(f => ({
      ...f,
      selected: f.status === 'new' || f.status === 'modified',
    }));
    setFiles(enriched);
    setStep(2);
  }

  function handleIngest() {
    const selected = files.filter(f => f.selected);
    setProgress({ current: 0, total: selected.length, currentFile: '', stage: '' });
    setLog([]);
    setStep(3);

    const stages = ['projection', 'indexing', 'abstraction'];
    let idx = 0;
    let stageIdx = 0;

    timerRef.current = window.setInterval(() => {
      if (idx >= selected.length) {
        if (timerRef.current) clearInterval(timerRef.current);
        setStep(4);
        return;
      }
      const file = selected[idx];
      const stage = stages[stageIdx];
      setProgress({ current: idx + 1, total: selected.length, currentFile: file.filename, stage });
      setLog(prev => [...prev, `[${stage}] ${file.filename}`]);
      stageIdx++;
      if (stageIdx >= stages.length) {
        stageIdx = 0;
        idx++;
      }
    }, 400);
  }

  useEffect(() => {
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, []);

  function handleCancel() {
    if (timerRef.current) clearInterval(timerRef.current);
    setStep(4);
  }

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Ingest</h1>

      {/* Step indicator */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        {['Directory Input', 'Scan Preview', 'Ingestion Progress', 'Results Summary'].map((label, i) => (
          <div key={i} style={{
            padding: '4px 12px',
            borderRadius: 4,
            background: step === i + 1 ? '#333' : '#eee',
            color: step === i + 1 ? '#fff' : '#999',
            fontSize: 12,
            fontWeight: step === i + 1 ? 600 : 400,
          }}>
            {i + 1}. {label}
          </div>
        ))}
      </div>

      {/* Step 1: Directory Input */}
      {step === 1 && (
        <div>
          <label style={{ display: 'block', marginBottom: 4, fontWeight: 500 }}>Directory path</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={directory}
              onChange={e => setDirectory(e.target.value)}
              placeholder="/path/to/source/directory"
              style={{ flex: 1, padding: '6px 10px' }}
            />
            <button onClick={handleScan} style={btnStyle}>Scan</button>
          </div>
        </div>
      )}

      {/* Step 2: Scan Preview */}
      {step === 2 && (
        <div>
          <SummaryBar files={files} />
          <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 12 }}>
            <thead>
              <tr>
                <th style={thStyle}></th>
                <th style={thStyle}>Filename</th>
                <th style={thStyle}>Size</th>
                <th style={thStyle}>Adapter</th>
                <th style={thStyle}>Status</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f, i) => (
                <tr key={f.path}>
                  <td style={tdStyle}>
                    <input
                      type="checkbox"
                      checked={f.selected}
                      disabled={f.status === 'no_adapter'}
                      onChange={e => {
                        const updated = [...files];
                        updated[i] = { ...f, selected: e.target.checked };
                        setFiles(updated);
                      }}
                    />
                  </td>
                  <td style={tdStyle}>{f.filename}</td>
                  <td style={tdStyle}>{formatSize(f.size)}</td>
                  <td style={tdStyle}>{f.detected_adapter ?? '-'}</td>
                  <td style={tdStyle}>
                    <StatusBadge status={f.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <button onClick={() => setStep(1)} style={{ ...btnStyle, background: '#eee', color: '#333' }}>Back</button>
            <button onClick={handleIngest} style={btnStyle} disabled={!files.some(f => f.selected)}>
              Ingest Selected ({files.filter(f => f.selected).length})
            </button>
          </div>
        </div>
      )}

      {/* Step 3: Ingestion Progress */}
      {step === 3 && (
        <div>
          <div style={{ marginBottom: 12 }}>
            <strong>{progress.currentFile}</strong> - {progress.stage}
          </div>
          <div style={{ marginBottom: 8, fontSize: 13, color: '#666' }}>
            {progress.current} of {progress.total} files
          </div>
          <div style={{ background: '#eee', borderRadius: 4, height: 8, marginBottom: 16 }}>
            <div style={{
              background: '#333',
              borderRadius: 4,
              height: 8,
              width: `${progress.total > 0 ? (progress.current / progress.total) * 100 : 0}%`,
              transition: 'width 0.3s',
            }} />
          </div>
          <div style={{
            background: '#f5f5f5',
            border: '1px solid #ddd',
            borderRadius: 4,
            padding: 12,
            maxHeight: 200,
            overflow: 'auto',
            fontSize: 12,
            fontFamily: 'monospace',
          }}>
            {log.map((line, i) => <div key={i}>{line}</div>)}
          </div>
          <button onClick={handleCancel} style={{ ...btnStyle, marginTop: 12, background: '#c62828', color: '#fff' }}>
            Cancel
          </button>
        </div>
      )}

      {/* Step 4: Results Summary */}
      {step === 4 && (
        <div>
          <h2 style={{ fontSize: 16, marginBottom: 12 }}>Ingestion Complete</h2>
          <table style={{ borderCollapse: 'collapse' }}>
            <tbody>
              <tr><td style={tdStyle}>Documents created</td><td style={tdStyle}>3 (2 new, 1 new version)</td></tr>
              <tr><td style={tdStyle}>Metadata extracted</td><td style={tdStyle}>2 pending confirmation</td></tr>
              <tr><td style={tdStyle}>Edges inferred</td><td style={tdStyle}>Tier 1 auto-created: 1 supersedes. Tier 2 staged: 2 covers</td></tr>
              <tr><td style={tdStyle}>Abstracts</td><td style={tdStyle}>2 generated, 1 deferred</td></tr>
              <tr><td style={tdStyle}>Errors</td><td style={tdStyle}>0</td></tr>
            </tbody>
          </table>
          <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
            <Link to="/review?tab=metadata" style={{ ...btnStyle, textDecoration: 'none', textAlign: 'center' }}>
              Review Metadata
            </Link>
            <Link to="/review?tab=edges" style={{ ...btnStyle, textDecoration: 'none', textAlign: 'center' }}>
              Review Edges
            </Link>
            <button onClick={() => { setStep(1); setDirectory(''); setFiles([]); setLog([]); }} style={{ ...btnStyle, background: '#eee', color: '#333' }}>
              Ingest More
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// -- Sub-components --

function SummaryBar({ files }: { files: { status: string }[] }) {
  const total = files.length;
  const withAdapter = files.filter(f => f.status !== 'no_adapter').length;
  const noAdapter = total - withAdapter;
  return (
    <div style={{ display: 'flex', gap: 16, fontSize: 13, color: '#666' }}>
      <span>Total files: <strong>{total}</strong></span>
      <span>With adapter: <strong>{withAdapter}</strong></span>
      <span>No adapter: <strong>{noAdapter}</strong></span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    new: '#2e7d32',
    modified: '#f57f17',
    unchanged: '#999',
    no_adapter: '#c62828',
  };
  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: 3,
      fontSize: 11,
      fontWeight: 600,
      background: `${colors[status]}18`,
      color: colors[status],
      textTransform: 'capitalize',
    }}>
      {status === 'no_adapter' ? 'No adapter' : status}
    </span>
  );
}

function formatSize(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(1)} KB`;
  return `${bytes} B`;
}

const btnStyle: React.CSSProperties = {
  padding: '6px 16px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '6px 10px',
  borderBottom: '2px solid #ddd',
  fontSize: 12,
  color: '#666',
};

const tdStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderBottom: '1px solid #eee',
};
