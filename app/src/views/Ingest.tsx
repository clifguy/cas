import { useState, useEffect, useRef } from 'react';
import { Link, useOutletContext } from 'react-router';
import type { VaultContext } from '../App';
import type {
  ScanResultItem,
  IngestProgressEvent,
  IngestSummaryEvent,
  BatchProgressEvent,
  BatchSummaryEvent,
} from '../api/types';
import {
  scanDirectory,
  startIngestion,
  detectIngestProfile,
  uploadBatchIngest,
  sourceTypeForFilename,
  type IngestProfile,
} from '../api/ingest';

type StreamEvent =
  | IngestProgressEvent
  | IngestSummaryEvent
  | BatchProgressEvent
  | BatchSummaryEvent;

interface UploadCandidate {
  file: File;
  source_type: string | null;
  selected: boolean;
}

export default function Ingest() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [step, setStep] = useState(1);

  // Deployment profile gate: co-located keeps the directory-path scan; hosted
  // shows the file-upload affordance (the browser holds the files and shares no
  // filesystem with the server). 'detecting' is the brief pre-resolution state.
  const [profile, setProfile] = useState<'detecting' | IngestProfile>('detecting');

  // Step 1 state (co-located)
  const [directory, setDirectory] = useState('');
  const [maxDepth, setMaxDepth] = useState('');
  const [scanError, setScanError] = useState('');
  const [scanning, setScanning] = useState(false);

  // Step 1 state (hosted)
  const [uploadFiles, setUploadFiles] = useState<UploadCandidate[]>([]);

  // Step 2 state (co-located)
  const [files, setFiles] = useState<(ScanResultItem & { selected: boolean })[]>([]);
  const [scanWarnings, setScanWarnings] = useState<string[]>([]);
  const [inferEdges, setInferEdges] = useState(false);

  // Step 3 state
  const [progress, setProgress] = useState({ current: 0, total: 0, filename: '', stage: '', status: '' });
  const [log, setLog] = useState<{ filename: string; status: string; error?: string }[]>([]);
  const [runningCounts, setRunningCounts] = useState({ completed: 0, failed: 0 });
  const [summary, setSummary] = useState<IngestSummaryEvent | BatchSummaryEvent | null>(null);
  const [ingestionDone, setIngestionDone] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    detectIngestProfile(vaultId).then((p) => {
      if (!cancelled) setProfile(p);
    });
    return () => {
      cancelled = true;
    };
  }, [vaultId]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  if (!vault) return <div>Vault not found.</div>;

  // Shared SSE handler for both the co-located ingest stream and the hosted
  // upload stream. The two endpoints emit the same progress/summary discriminator;
  // the hosted summary is a superset, rendered by the same table below.
  function onStreamEvent(event: StreamEvent) {
    if (event.event_type === 'progress') {
      const pe = event;
      if (pe.status === 'completed' || pe.status === 'failed') {
        setProgress(() => ({
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
        setRunningCounts(prev => ({
          completed: prev.completed + (pe.status === 'completed' ? 1 : 0),
          failed: prev.failed + (pe.status === 'failed' ? 1 : 0),
        }));
      } else if (pe.status === 'started') {
        setProgress(prev => ({
          ...prev,
          filename: pe.filename,
          stage: pe.stage,
          status: pe.status,
        }));
      }
    } else if (event.event_type === 'summary') {
      setSummary(event);
      setIngestionDone(true);
    }
  }

  function resetStreamState(total: number) {
    setProgress({ current: 0, total, filename: '', stage: '', status: '' });
    setLog([]);
    setRunningCounts({ completed: 0, failed: 0 });
    setSummary(null);
    setIngestionDone(false);
    setStep(3);
  }

  async function handleScan() {
    if (!directory.trim()) return;
    setScanError('');
    setScanning(true);

    try {
      const depth = maxDepth ? parseInt(maxDepth, 10) - 1 : undefined;
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
    resetStreamState(selected.length);

    const controller = new AbortController();
    abortRef.current = controller;

    const ingestFiles = selected
      .filter(f => f.source_type !== null)
      .map(f => ({
        file_path: f.file_path,
        source_type: f.source_type!,
        parsed_metadata: f.parsed_metadata,
      }));

    try {
      await startIngestion(vaultId, ingestFiles, onStreamEvent, controller.signal, inferEdges);
      // Stream ended normally. If the summary event already set ingestionDone,
      // this is a no-op. If the stream closed without emitting a summary
      // (unexpected), mark done so the user isn't stuck without action buttons.
      setIngestionDone(true);
    } catch (err) {
      if (controller.signal.aborted) {
        setIngestionDone(true);
      } else {
        setLog(prev => [...prev, { filename: 'ERROR', status: 'failed', error: String(err) }]);
        setIngestionDone(true);
      }
    }
  }

  function handleFilesSelected(fileList: FileList | File[]) {
    const arr = Array.from(fileList);
    if (arr.length === 0) return;
    const mapped: UploadCandidate[] = arr.map(file => {
      const sourceType = sourceTypeForFilename(file.name);
      return { file, source_type: sourceType, selected: sourceType !== null };
    });
    setUploadFiles(mapped);
    setStep(2);
  }

  async function handleUpload() {
    const selected = uploadFiles.filter(f => f.selected && f.source_type !== null);
    if (selected.length === 0) return;
    resetStreamState(selected.length);

    const controller = new AbortController();
    abortRef.current = controller;

    const items = selected.map(f => ({ file: f.file, source_type: f.source_type! }));

    try {
      await uploadBatchIngest(vaultId, items, onStreamEvent, controller.signal, { inferEdges });
      setIngestionDone(true);
    } catch (err) {
      if (controller.signal.aborted) {
        setIngestionDone(true);
      } else {
        setLog(prev => [...prev, { filename: 'ERROR', status: 'failed', error: String(err) }]);
        setIngestionDone(true);
      }
    }
  }

  function handleCancel() {
    abortRef.current?.abort();
  }

  function handleReset() {
    setStep(1);
    setDirectory('');
    setMaxDepth('');
    setScanError('');
    setFiles([]);
    setUploadFiles([]);
    setScanWarnings([]);
    setLog([]);
    setRunningCounts({ completed: 0, failed: 0 });
    setSummary(null);
    setIngestionDone(false);
  }

  const stepLabels = profile === 'hosted'
    ? ['Select Files', 'File Preview', 'Ingestion']
    : ['Directory Input', 'Scan Preview', 'Ingestion'];

  const hostedSelectedCount = uploadFiles.filter(f => f.selected && f.source_type !== null).length;
  const hostedUnsupportedCount = uploadFiles.filter(f => f.source_type === null).length;

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Ingest</h1>

      {/* Step indicator */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        {stepLabels.map((label, i) => (
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

      {/* Step 1: detecting profile */}
      {step === 1 && profile === 'detecting' && (
        <div style={{ fontSize: 13, color: '#666' }}>Detecting deployment profile…</div>
      )}

      {/* Step 1 (co-located): Directory Input */}
      {step === 1 && profile === 'co-located' && (
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

      {/* Step 1 (hosted): File upload picker + drop zone */}
      {step === 1 && profile === 'hosted' && (
        <div>
          <label style={{ display: 'block', marginBottom: 4, fontWeight: 500 }}>Select files to ingest</label>
          <div
            onDrop={e => { e.preventDefault(); handleFilesSelected(e.dataTransfer.files); }}
            onDragOver={e => e.preventDefault()}
            style={{
              border: '2px dashed #ccc',
              borderRadius: 6,
              padding: 24,
              textAlign: 'center',
              color: '#666',
              fontSize: 13,
            }}
          >
            <div style={{ marginBottom: 8 }}>Drag and drop files here, or</div>
            <input
              data-testid="upload-file-input"
              aria-label="Upload files to ingest"
              type="file"
              multiple
              onChange={e => { if (e.target.files) handleFilesSelected(e.target.files); }}
            />
            <div style={{ marginTop: 8, color: '#999', fontSize: 12 }}>
              Supported types: markdown (.md), docx, xlsx, pptx, pdf
            </div>
          </div>
        </div>
      )}

      {/* Step 2 (hosted): File preview + select */}
      {step === 2 && profile === 'hosted' && (
        <div>
          <div style={{ display: 'flex', gap: 16, fontSize: 13, color: '#666' }}>
            <span>Total: <strong>{uploadFiles.length}</strong></span>
            <span>Supported: <strong>{uploadFiles.length - hostedUnsupportedCount}</strong></span>
            <span>Unsupported: <strong>{hostedUnsupportedCount}</strong></span>
          </div>
          {hostedUnsupportedCount > 0 && (
            <div style={{ color: '#f57f17', fontSize: 12, marginTop: 4 }}>
              {hostedUnsupportedCount} file(s) have no supported adapter and will be skipped.
            </div>
          )}
          <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 12 }}>
            <thead>
              <tr>
                <th style={thStyle}></th>
                <th style={thStyle}>Filename</th>
                <th style={thStyle}>Source type</th>
                <th style={thStyle}>Status</th>
              </tr>
            </thead>
            <tbody>
              {uploadFiles.map((f, i) => {
                const unsupported = f.source_type === null;
                return (
                  <tr key={`${f.file.name}-${i}`}>
                    <td style={tdStyle}>
                      <input
                        type="checkbox"
                        checked={f.selected}
                        disabled={unsupported}
                        onChange={e => {
                          const updated = [...uploadFiles];
                          updated[i] = { ...f, selected: e.target.checked };
                          setUploadFiles(updated);
                        }}
                      />
                    </td>
                    <td style={tdStyle}>{f.file.name}</td>
                    <td style={tdStyle}>{f.source_type ?? '-'}</td>
                    <td style={tdStyle}><UploadStatusBadge unsupported={unsupported} /></td>
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
            <button onClick={handleUpload} style={btnStyle} disabled={hostedSelectedCount === 0}>
              Upload Selected ({hostedSelectedCount})
            </button>
          </div>
        </div>
      )}

      {/* Step 2 (co-located): Scan Preview */}
      {step === 2 && profile !== 'hosted' && (
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
                    <td style={tdStyle}>{f.source_type ?? '-'}</td>
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

      {/* Step 3: Ingestion Progress + Running Summary */}
      {step === 3 && (
        <div>
          <div style={{ marginBottom: 12 }}>
            <strong>
              {ingestionDone
                ? 'Ingestion complete'
                : (progress.current > 0
                    ? (progress.filename || 'Working...')
                    : (profile === 'hosted' ? 'Uploading & ingesting…' : 'Starting...'))}
            </strong>
            {!ingestionDone && progress.stage && <> - {progress.stage}</>}
          </div>
          <div style={{ marginBottom: 8, fontSize: 13, color: '#666' }}>
            {progress.current} of {progress.total} files
          </div>
          <div style={{ background: '#eee', borderRadius: 4, height: 8, marginBottom: 16 }}>
            <div style={{
              background: ingestionDone ? '#2e7d32' : '#333',
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
          {!ingestionDone && (
            <button onClick={handleCancel} style={{ ...btnStyle, marginTop: 12, background: '#c62828', color: '#fff' }}>
              Cancel
            </button>
          )}

          {/* Running / Final Summary */}
          {(progress.current > 0 || summary) && (
            <div style={{ marginTop: 20 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8, color: ingestionDone ? '#333' : '#666' }}>
                {ingestionDone ? 'Results Summary' : 'Running Summary'}
              </h3>
              <table style={{ borderCollapse: 'collapse' }}>
                <tbody>
                  <tr>
                    <td style={tdStyle}>Documents created</td>
                    <td style={tdStyle}>
                      {summary
                        ? <>{summary.documents_created.new + summary.documents_created.new_version} ({summary.documents_created.new} new, {summary.documents_created.new_version} new version)</>
                        : <>{runningCounts.completed}</>}
                    </td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Metadata pending</td>
                    <td style={tdStyle}>{summary ? `${summary.metadata_pending} documents` : '-'}</td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Edges (Tier 1 auto-created)</td>
                    <td style={tdStyle}>
                      {summary
                        ? (Object.keys(summary.edges_created).length > 0
                            ? Object.entries(summary.edges_created).map(([type, count]) => `${count} ${type}`).join(', ')
                            : 'None')
                        : '-'}
                    </td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Edges (Tier 2 staged)</td>
                    <td style={tdStyle}>
                      {summary
                        ? (<>
                            {Object.keys(summary.edges_staged).length > 0
                              ? Object.entries(summary.edges_staged).map(([type, count]) => `${count} ${type}`).join(', ')
                              : 'None'}
                            {summary.edges_dropped > 0 && <span style={{ color: '#f57f17' }}> ({summary.edges_dropped} dropped)</span>}
                          </>)
                        : '-'}
                    </td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Abstracts</td>
                    <td style={tdStyle}>
                      {summary ? `${summary.abstracts_generated} generated, ${summary.abstracts_deferred} deferred` : '-'}
                    </td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Errors</td>
                    <td style={tdStyle}>{summary ? summary.error_count : runningCounts.failed}</td>
                  </tr>
                </tbody>
              </table>
              {summary?.edge_warnings && summary.edge_warnings.length > 0 && (
                <div style={{ marginTop: 8, padding: 8, border: '1px solid #f57f17', borderRadius: 4, fontSize: 12 }}>
                  <strong style={{ color: '#f57f17' }}>Edge warnings ({summary.edge_warnings.length})</strong>
                  {summary.edge_warnings.map((w, i) => (
                    <div key={i} style={{ marginTop: 4 }}>
                      {w.reason}: <span style={{ fontFamily: 'monospace' }}>{w.source}</span> →{' '}
                      <span style={{ fontFamily: 'monospace' }}>{w.target}</span>
                      <div style={{ color: '#666', fontSize: 11 }}>{w.detail}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Action buttons after completion */}
          {ingestionDone && (
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
          )}
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

function UploadStatusBadge({ unsupported }: { unsupported: boolean }) {
  const color = unsupported ? '#c62828' : '#2e7d32';
  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: 3,
      fontSize: 11,
      fontWeight: 600,
      background: `${color}18`,
      color,
    }}>
      {unsupported ? 'Unsupported' : 'Supported'}
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
