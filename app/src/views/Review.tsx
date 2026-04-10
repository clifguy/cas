import { useState, useEffect, useCallback } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { PendingMetadata, StagingEdge } from '../api/types';
import { listPendingMetadata, listStagingEdges, confirmStagingEdge, dismissStagingEdge } from '../api/review';
import { updateMetadata } from '../api/documents';

export default function Review() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('tab') === 'edges' ? 'edges' : 'metadata';

  const [pendingMeta, setPendingMeta] = useState<PendingMetadata[]>([]);
  const [stagingEdges, setStagingEdges] = useState<StagingEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState('');

  const fetchData = useCallback(async () => {
    setLoading(true);
    setFetchError('');
    try {
      const [meta, edges] = await Promise.all([
        listPendingMetadata(vaultId),
        listStagingEdges(vaultId),
      ]);
      setPendingMeta(meta);
      setStagingEdges(edges);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load review data';
      setFetchError(msg);
    }
    setLoading(false);
  }, [vaultId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  if (!vault) return <div>Vault not found.</div>;
  if (loading) return <div>Loading review data...</div>;
  if (fetchError) return <div style={{ color: '#c62828' }}>Error: {fetchError}</div>;

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Review</h1>

      <div style={{ display: 'flex', gap: 0, borderBottom: '2px solid #ddd', marginBottom: 20 }}>
        <TabButton
          label={`Metadata Review (${pendingMeta.length})`}
          active={activeTab === 'metadata'}
          onClick={() => setSearchParams({ tab: 'metadata' })}
        />
        <TabButton
          label={`Edge Review (${stagingEdges.length})`}
          active={activeTab === 'edges'}
          onClick={() => setSearchParams({ tab: 'edges' })}
        />
      </div>

      {activeTab === 'metadata' ? (
        <MetadataReview vaultId={vaultId} items={pendingMeta} onRefresh={fetchData} />
      ) : (
        <EdgeReview vaultId={vaultId} edges={stagingEdges} onRefresh={fetchData} />
      )}
    </div>
  );
}

// -- Metadata Review --

function MetadataReview({ vaultId, items, onRefresh }: { vaultId: string; items: PendingMetadata[]; onRefresh: () => void }) {
  const [queue, setQueue] = useState(items);
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => { setQueue(items); }, [items]);

  function setFieldEdit(docId: string, field: string, value: string) {
    setEdits(prev => ({
      ...prev,
      [docId]: { ...prev[docId], [field]: value },
    }));
  }

  if (queue.length === 0) {
    return <div style={{ color: '#666' }}>No metadata pending review.</div>;
  }

  async function confirmOne(docId: string) {
    setErrors(prev => { const next = { ...prev }; delete next[docId]; return next; });
    try {
      // Start with extracted field values as baseline, then overlay user edits.
      // Without this, clicking "Confirm" without editing sends an empty body
      // and no metadata values are actually persisted.
      const item = queue.find(i => i.document.id === docId);
      const baseline: Record<string, string> = {};
      if (item) {
        for (const [field, info] of Object.entries(item.extracted_fields)) {
          if (info.value != null) baseline[field] = info.value;
        }
      }
      const docEdits = { ...baseline, ...edits[docId] };
      await updateMetadata(vaultId, docId, docEdits);
      setQueue(q => q.filter(item => item.document.id !== docId));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Confirmation failed';
      setErrors(prev => ({ ...prev, [docId]: msg }));
    }
  }

  async function confirmAll() {
    for (const item of queue) {
      await confirmOne(item.document.id);
    }
    onRefresh();
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={confirmAll} style={btnStyle}>Confirm All</button>
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={thStyle}>Document</th>
            <th style={thStyle}>Field</th>
            <th style={thStyle}>Value</th>
            <th style={thStyle}>Source</th>
            <th style={thStyle}>Alternate</th>
            <th style={thStyle}></th>
          </tr>
        </thead>
        <tbody>
          {queue.map(item => {
            const fields = Object.entries(item.extracted_fields);
            const docError = errors[item.document.id];
            return fields.map(([field, info], fi) => (
              <tr key={`${item.document.id}-${field}`} style={docError ? { background: '#fff3f3' } : undefined}>
                {fi === 0 && (
                  <td style={{ ...tdStyle, verticalAlign: 'top' }} rowSpan={fields.length}>
                    <Link to={`/documents/${item.document.id}`} style={{ color: '#1565c0' }}>
                      {item.document.title}
                    </Link>
                    {docError && <div style={{ color: '#c62828', fontSize: 11, marginTop: 4 }}>{docError}</div>}
                  </td>
                )}
                <td style={tdStyle}>{field}</td>
                <td style={tdStyle}>
                  <input
                    type="text"
                    defaultValue={info.value ?? ''}
                    onChange={e => setFieldEdit(item.document.id, field, e.target.value)}
                    style={{ padding: '2px 6px', width: '100%', boxSizing: 'border-box' }}
                  />
                </td>
                <td style={tdStyle}>
                  <SourceBadge source={info.source} />
                </td>
                <td style={tdStyle}>
                  {info.alt_value ? (
                    <span style={{ fontSize: 12, color: '#888' }}>
                      {info.alt_value} <SourceBadge source={info.alt_source!} />
                    </span>
                  ) : '-'}
                </td>
                {fi === 0 && (
                  <td style={{ ...tdStyle, verticalAlign: 'top' }} rowSpan={fields.length}>
                    <button onClick={() => confirmOne(item.document.id)} style={btnSmallStyle}>
                      Confirm
                    </button>
                  </td>
                )}
              </tr>
            ));
          })}
        </tbody>
      </table>
    </div>
  );
}

// -- Edge Review --

function EdgeReview({ vaultId, edges, onRefresh }: { vaultId: string; edges: StagingEdge[]; onRefresh: () => void }) {
  const [staging, setStaging] = useState(edges);
  const [edgeErrors, setEdgeErrors] = useState<Record<string, string>>({});

  useEffect(() => { setStaging(edges); }, [edges]);

  if (staging.length === 0) {
    return <div style={{ color: '#666' }}>No edges pending review.</div>;
  }

  const groups: Record<string, StagingEdge[]> = {};
  for (const e of staging) {
    if (!groups[e.edge_type]) groups[e.edge_type] = [];
    groups[e.edge_type].push(e);
  }

  async function handleConfirm(id: string) {
    setEdgeErrors(prev => { const next = { ...prev }; delete next[id]; return next; });
    try {
      await confirmStagingEdge(vaultId, id);
      setStaging(s => s.filter(e => e.id !== id));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Confirm failed';
      setEdgeErrors(prev => ({ ...prev, [id]: msg }));
    }
  }

  async function handleDismiss(id: string) {
    setEdgeErrors(prev => { const next = { ...prev }; delete next[id]; return next; });
    try {
      await dismissStagingEdge(vaultId, id);
      setStaging(s => s.filter(e => e.id !== id));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Dismiss failed';
      setEdgeErrors(prev => ({ ...prev, [id]: msg }));
    }
  }

  async function confirmGroup(edgeType: string) {
    const group = staging.filter(e => e.edge_type === edgeType);
    await Promise.all(group.map(e => handleConfirm(e.id)));
    onRefresh();
  }

  async function dismissGroup(edgeType: string) {
    const group = staging.filter(e => e.edge_type === edgeType);
    await Promise.all(group.map(e => handleDismiss(e.id)));
    onRefresh();
  }

  async function confirmAll() {
    await Promise.all(staging.map(e => handleConfirm(e.id)));
    onRefresh();
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={confirmAll} style={btnStyle}>Confirm All</button>
      </div>

      {Object.entries(groups).map(([edgeType, groupEdges]) => (
        <div key={edgeType} style={{ marginBottom: 24 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 14, textTransform: 'capitalize' }}>{edgeType.replace(/_/g, ' ')}</h3>
            <div style={{ display: 'flex', gap: 4 }}>
              <button onClick={() => confirmGroup(edgeType)} style={btnSmallStyle}>Confirm All</button>
              <button onClick={() => dismissGroup(edgeType)} style={{ ...btnSmallStyle, background: '#eee', color: '#333' }}>
                Dismiss All
              </button>
            </div>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Source</th>
                <th style={thStyle}>Target</th>
                <th style={thStyle}>Evidence</th>
                <th style={thStyle}>Tier</th>
                <th style={thStyle}></th>
              </tr>
            </thead>
            <tbody>
              {groupEdges.map(e => (
                <tr key={e.id} style={edgeErrors[e.id] ? { background: '#fff3f3' } : undefined}>
                  <td style={tdStyle}>
                    <Link to={`/documents/${e.source_id}`} style={{ color: '#1565c0' }}>{e.source_id}</Link>
                  </td>
                  <td style={tdStyle}>
                    <Link to={`/documents/${e.target_id}`} style={{ color: '#1565c0' }}>{e.target_id}</Link>
                  </td>
                  <td style={tdStyle}><span style={{ fontSize: 12, color: '#666' }}>{e.inference_evidence}</span></td>
                  <td style={tdStyle}>Tier {e.confidence_tier}</td>
                  <td style={tdStyle}>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <button onClick={() => handleConfirm(e.id)} style={btnSmallStyle}>Confirm</button>
                      <button onClick={() => handleDismiss(e.id)} style={{ ...btnSmallStyle, background: '#eee', color: '#333' }}>
                        Dismiss
                      </button>
                    </div>
                    {edgeErrors[e.id] && <div style={{ color: '#c62828', fontSize: 11, marginTop: 4 }}>{edgeErrors[e.id]}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

// -- Sub-components --

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '8px 16px',
        border: 'none',
        borderBottom: active ? '2px solid #333' : '2px solid transparent',
        background: 'none',
        fontWeight: active ? 600 : 400,
        color: active ? '#000' : '#666',
        cursor: 'pointer',
        fontSize: 13,
        marginBottom: -2,
      }}
    >
      {label}
    </button>
  );
}

function SourceBadge({ source }: { source: string }) {
  const colors: Record<string, string> = { filename: '#1565c0', content: '#2e7d32', default: '#999' };
  return (
    <span style={{
      padding: '1px 6px',
      borderRadius: 3,
      fontSize: 10,
      fontWeight: 600,
      background: `${colors[source] ?? '#999'}18`,
      color: colors[source] ?? '#999',
    }}>
      {source}
    </span>
  );
}

const btnStyle: React.CSSProperties = { padding: '6px 16px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 13 };
const btnSmallStyle: React.CSSProperties = { padding: '3px 10px', border: '1px solid #ccc', borderRadius: 3, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 11 };
const thStyle: React.CSSProperties = { textAlign: 'left', padding: '6px 10px', borderBottom: '2px solid #ddd', fontSize: 12, color: '#666' };
const tdStyle: React.CSSProperties = { padding: '6px 10px', borderBottom: '1px solid #eee' };
