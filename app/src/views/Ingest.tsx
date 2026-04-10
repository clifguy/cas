import { useState, useEffect, useRef } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { ScanResultItem, IngestProgressEvent, IngestSummaryEvent } from '../api/types';
import { scanDirectory, startIngestion } from '../api/ingest';

export default function Ingest() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [step, setStep] = useState(1);

  // Step 1 state
  const [directory, setDirectory] = useState('');
  const [maxDepth, setMaxDepth] = useState('');
  const [scanError, setScanError] = useState('');
  const [scanning, setScanning] = useState(false);

  // Step 2 state
  const [files, setFiles] = useState<(ScanResultItem & { selected: boolean })[]>([]);
  const [scanWarnings, setScanWarnings] = useState<string[]>([]);
  const [inferEdges, setInferEdges] = useState(false);

  // Step 3 state
  const [progress, setProgress] = useState({ current: 0, total: 0, filename: '', stage: '', status: '' });
  const [log, setLog] = useState<{ filename: string; status: string; error?: string }[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  // Step 4 state
  const [summary, setSummary] = useState<IngestSummaryEvent | null>(null);

  if (!vault) return <div>Vault not found.</div>;

  async function handleScan() {
    if (!directory.trim()) return;
    setScanError('');
    setScanning(true);

    try {
      const depth = maxDepth ? parseInt(maxDepth, 10) : undefined;
      const result = await scanDirectory(vaultId, directory.trim(), depth);
      const enriched = result.files.map(f => ({
        ...f,
        selected: f.sage_status === 'new' || f.sage_status === 'modified',
      }));
      setFiles(enriched);
      setScanWarnings(result.warnings);
      setStep(2);
    } catch (err) {
      setScanError(err instanceof Error ? err.message : 'Scan failed');
    } finally {
      setScanning(false);
    }
  }

  async function handleIngest() {
    const selected = files.filter(f => f.selected);
    setProgress({ current: 0, total: selected.length, filename: '', stage: '', status: '' });
    setLog([]);
    setSummary(null);
    setStep(3);

    const controller = new AbortController();
    abortRef.current = controller;

    const ingestFiles = selected
      .filter(f => f.adapter !== null)
      .map(f => ({
        file_path: f.file_path,
        adapter: f.adapter!,
        parsed_metadata: f.parsed_metadata,
      }));

    try {
      await startIngestion(vaultId, ingestFiles, (event) => {
        if (event.event_type === 'progress') {
          const pe = event as IngestProgressEvent;
          if (pe.status === 'completed' || pe.status === 'failed') {
            setProgress(_prev => ({
              current: pe.file_index + 1,
              total: pe.total_files,
              filename: pe.filename,
              stage: pe.stage,
              status: pe.status,
            }));
            setLog(prev => [...prev, {
              filename: pe.filename,
              status: pe.status,
              error: pe.error,
            }]);
          } else if (pe.status === 'started') {
            setProgress(prev => ({
              ...prev,
              filename: pe.filename,
              stage: pe.stage,
              status: pe.status,
            }));
          }
        } else if (event.event_type === 'summary') {
          setSummary(event as IngestSummaryEvent);
          setStep(4);
        }
      }, controller.signal, inferEdges);

      // Stream ended normally. If the summary event already advanced us to
      // step 4, this is a no-op. If the stream closed without emitting a
      // summary (unexpected), advance so the user isn't stuck on step 3.
      setSummary(prev => {
        if (!prev) setStep(4);
        return prev;
      });
    } catch (err) {
      if (controller.signal.aborted) {
        // Cancelled by user -- advance to results with whatever summary we have
        setStep(4);
      } else {
        setLog(prev => [...prev, { filename: 'ERROR', status: 'failed', error: String(err) }]);
        setStep(4);
      }
    }
  }

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  function handleCancel() {
    abortRef.current?.abort();
  }

  function handleReset() {
    setStep(1);
    setDirectory('');
    setMaxDepth('');
    setScanError('');
    setFiles([]);
    setScanWarnings([]);
    setLog([]);
    setSummary(null);
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
          <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
            <input
              type="text"
              value={directory}
              onChange={e => { setDirectory(e.target.value); setScanError(''); }}
              placeholder="/path/to/source/directory"
              style={{ flex: 1, padding: '6px 10px' }}
            />
            <button onClick={handleScan} style={btnStyle} disabled={scanning || !directory.trim()}>
              {scanning ? 'Scanning...' : 'Scan'}
            </button>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
            <label style={{ fontSize: 13, color: '#666' }}>Max depth</label>
            <input
              type="number"
              min={1}
              value={maxDepth}
              onChange={e => setMaxDepth(e.target.value)}
              placeholder="unlimited"
              style={{ width: 80, padding: '6px 10px', fontSize: 13 }}
            />
          </div>
          {scanError && (
            <div style={{ color: '#c62828', fontSize: 13, marginTop: 4 }}>{scanError}</div>
          )}
        </div>
      )}

      {/* Step 2: Scan Preview */}
      {step === 2 && (
        <div>
          <ScanSummaryBar files={files} />
          {scanWarnings.length > 0 && (
            <div style={{ color: '#f57f17', fontSize: 12, marginTop: 4 }}>
              {scanWarnings.map((w, i) => <div key={i}>{w}</div>)}
            </div>
          )}
          <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 12 }}>
            <thead>
              <tr>
                <th style={thStyle}></th>
                <th style={thStyle}>Filename</th>
                <th style={thStyle}>Adapter</th>
                <th style={thStyle}>Doc Type</th>
                <th style={thStyle}>Version</th>
                <th style={thStyle}>Project</th>
                <th style={thStyle}>Status</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f, i) => {
                const filename = f.file_path.split('/').pop() ?? f.file_path;
                return (
                  <tr key={f.file_path}>
                    <td style={tdStyle}>
                      <input
                        type="checkbox"
                        checked={f.selected}
                        disabled={f.sage_status === 'no_adapter'}
                        onChange={e => {
                          const updated = [...files];
                          updated[i] = { ...f, selected: e.target.checked };
                          setFiles(updated);
                        }}
                      />
                    </td>
                    <td style={tdStyle}>{filename}</td>
                    <td style={tdStyle}>{f.adapter ?? '-'}</td>
                    <td style={tdStyle}>{f.parsed_metadata.doc_type ?? '-'}</td>
                    <td style={tdStyle}>{f.parsed_metadata.version ?? '-'}</td>
                    <td style={tdStyle}>{f.parsed_metadata.project ?? '-'}</td>
                    <td style={tdStyle}><StatusBadge status={f.sage_status} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 16 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={inferEdges}
                onChange={e => setInferEdges(e.target.checked)}
              />
              Infer edges during ingest
            </label>
          </div>
          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
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
            <strong>{progress.filename || 'Starting...'}</strong>
            {progress.stage && <> - {progress.stage}</>}
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
            {log.map((entry, i) => (
              <div key={i} style={{ color: entry.status === 'failed' ? '#c62828' : '#333' }}>
                [{entry.status}] {entry.filename}
                {entry.error && <span style={{ color: '#c62828' }}> - {entry.error}</span>}
              </div>
            ))}
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
          {summary ? (
            <table style={{ borderCollapse: 'collapse' }}>
              <tbody>
                <tr>
                  <td style={tdStyle}>Documents created</td>
                  <td style={tdStyle}>{summary.documents_created.new + summary.documents_created.new_version} ({summary.documents_created.new} new, {summary.documents_created.new_version} new version)</td>
                </tr>
                <tr>
                  <td style={tdStyle}>Metadata pending</td>
                  <td style={tdStyle}>{summary.metadata_pending} documents</td>
                </tr>
                <tr>
                  <td style={tdStyle}>Edges (Tier 1 auto-created)</td>
                  <td style={tdStyle}>
                    {Object.keys(summary.edges_created).length > 0
                      ? Object.entries(summary.edges_created).map(([type, count]) => `${count} ${type}`).join(', ')
                      : 'None'}
                  </td>
                </tr>
                <tr>
                  <td style={tdStyle}>Edges (Tier 2 staged)</td>
                  <td style={tdStyle}>
                    {Object.keys(summary.edges_staged).length > 0
                      ? Object.entries(summary.edges_staged).map(([type, count]) => `${count} ${type}`).join(', ')
                      : 'None'}
                    {summary.edges_dropped > 0 && <span style={{ color: '#f57f17' }}> ({summary.edges_dropped} dropped)</span>}
                  </td>
                </tr>
                <tr>
                  <td style={tdStyle}>Abstracts</td>
                  <td style={tdStyle}>{summary.abstracts_generated} generated, {summary.abstracts_deferred} deferred</td>
                </tr>
                <tr>
                  <td style={tdStyle}>Errors</td>
                  <td style={tdStyle}>{summary.error_count}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p style={{ color: '#666' }}>Ingestion was cancelled or ended without a summary.</p>
          )}
          <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
            <Link to="/review?tab=metadata" style={{ ...btnStyle, textDecoration: 'none', textAlign: 'center' }}>
              Review Metadata
            </Link>
            <Link to="/review?tab=edges" style={{ ...btnStyle, textDecoration: 'none', textAlign: 'center' }}>
              Review Edges
            </Link>
            <button onClick={handleReset} style={{ ...btnStyle, background: '#eee', color: '#333' }}>
              Ingest More
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// -- Sub-components --

function ScanSummaryBar({ files }: { files: { sage_status: string }[] }) {
  const total = files.length;
  const withAdapter = files.filter(f => f.sage_status !== 'no_adapter').length;
  const noAdapter = total - withAdapter;
  const newCount = files.filter(f => f.sage_status === 'new').length;
  const modifiedCount = files.filter(f => f.sage_status === 'modified').length;
  return (
    <div style={{ display: 'flex', gap: 16, fontSize: 13, color: '#666' }}>
      <span>Total: <strong>{total}</strong></span>
      <span>New: <strong>{newCount}</strong></span>
      <span>Modified: <strong>{modifiedCount}</strong></span>
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
      background: `${colors[status] ?? '#999'}18`,
      color: colors[status] ?? '#999',
      textTransform: 'capitalize',
    }}>
      {status === 'no_adapter' ? 'No adapter' : status}
    </span>
  );
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
