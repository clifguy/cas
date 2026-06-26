import { useState, useEffect, useCallback } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { PendingMetadata, StagingEdge, UpdateMetadataRequest } from '../api/types';
import { listPendingMetadata, listStagingEdges, confirmStagingEdge, dismissStagingEdge } from '../api/review';
import { updateMetadata } from '../api/documents';
import { BulkActionBar } from '../components/BulkActionBar';
import { BulkLifecycleDialog } from '../components/BulkLifecycleDialog';
import { BulkMetadataDialog } from '../components/BulkMetadataDialog';

export default function Review() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('tab') === 'edges' ? 'edges' : 'metadata';

  const [pendingMeta, setPendingMeta] = useState<PendingMetadata[]>([]);
  const [stagingEdges, setStagingEdges] = useState<StagingEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState('');

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [lifecycleDialogOpen, setLifecycleDialogOpen] = useState(false);
  const [metadataDialogOpen, setMetadataDialogOpen] = useState(false);

  // Clear the selection when the active tab changes — the rows under the
  // user's fingers differ between tabs. Resetting during render (guarded by
  // the previous tab) avoids a setState-in-effect.
  const [syncedTab, setSyncedTab] = useState(activeTab);
  if (activeTab !== syncedTab) {
    setSyncedTab(activeTab);
    setSelectedIds(new Set());
  }

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

  useEffect(() => {
    async function run() {
      await fetchData();
    }
    run();
  }, [fetchData]);

  function toggleRow(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAllVisible(ids: string[]) {
    setSelectedIds((prev) => {
      const allSelected = ids.length > 0 && ids.every((id) => prev.has(id));
      if (allSelected) {
        const next = new Set(prev);
        for (const id of ids) next.delete(id);
        return next;
      }
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      return next;
    });
  }

  function handleBulkResolved(result: { succeeded: string[]; failed: string[] }) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const id of result.succeeded) next.delete(id);
      return next;
    });
  }

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
        <>
          {selectedIds.size > 0 && (
            <BulkActionBar
              count={selectedIds.size}
              onSetLifecycle={() => setLifecycleDialogOpen(true)}
              onUpdateMetadata={() => setMetadataDialogOpen(true)}
              onClear={() => setSelectedIds(new Set())}
            />
          )}
          <MetadataReview
            vaultId={vaultId}
            items={pendingMeta}
            selectedIds={selectedIds}
            onToggleRow={toggleRow}
            onToggleAll={toggleAllVisible}
          />
        </>
      ) : (
        <EdgeReview vaultId={vaultId} edges={stagingEdges} />
      )}

      {lifecycleDialogOpen && (
        <BulkLifecycleDialog
          vaultId={vaultId}
          selectedIds={Array.from(selectedIds)}
          onResolved={handleBulkResolved}
          onClose={() => setLifecycleDialogOpen(false)}
        />
      )}
      {metadataDialogOpen && (
        <BulkMetadataDialog
          vaultId={vaultId}
          selectedIds={Array.from(selectedIds)}
          onResolved={handleBulkResolved}
          onClose={() => setMetadataDialogOpen(false)}
        />
      )}
    </div>
  );
}

// Tier-1 scalar fields on UpdateMetadataRequest (CAS-ADR-028). Any other key
// surfaced by the metadata extractor is treated as tier3_metadata.
const TIER1_SCALAR_FIELDS: ReadonlySet<string> = new Set([
  'title',
  'version_label',
  'project',
  'doc_type',
  'authority_scope',
  'document_date',
]);

// -- Metadata Review --

export function MetadataReview({
  vaultId,
  items,
  selectedIds,
  onToggleRow,
  onToggleAll,
}: {
  vaultId: string;
  items: PendingMetadata[];
  selectedIds: Set<string>;
  onToggleRow: (id: string) => void;
  onToggleAll: (ids: string[]) => void;
}) {
  const [queue, setQueue] = useState(items);
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  // Re-seed the working queue when a fresh prop arrives (render-phase resync).
  const [syncedItems, setSyncedItems] = useState(items);
  if (items !== syncedItems) {
    setSyncedItems(items);
    setQueue(items);
  }

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
      const merged: Record<string, unknown> = { ...baseline, ...edits[docId] };

      // Partition into CAS-ADR-028 ops-object shape: Tier-1 scalars go on the
      // body root, `tags` becomes a ListFieldPatch.add, anything else becomes
      // a Tier3Patch.set entry. Empty patches are omitted because the backend
      // rejects ListFieldPatch / Tier3Patch carrying no actionable operation.
      const body: UpdateMetadataRequest = {};
      const tier3Set: Record<string, unknown> = {};
      for (const [field, value] of Object.entries(merged)) {
        if (TIER1_SCALAR_FIELDS.has(field)) {
          (body as Record<string, unknown>)[field] = value;
        } else if (field === 'tags') {
          const tagsArr =
            typeof value === 'string'
              ? value.split(',').map(t => t.trim()).filter(Boolean)
              : Array.isArray(value)
              ? value.filter((t): t is string => typeof t === 'string' && t.length > 0)
              : [];
          if (tagsArr.length > 0) body.tags = { add: tagsArr };
        } else {
          tier3Set[field] = value;
        }
      }
      if (Object.keys(tier3Set).length > 0) body.tier3_metadata = { set: tier3Set };

      await updateMetadata(vaultId, docId, body);
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
  }

  const visibleIds = queue.map((item) => item.document.id);
  const selectedVisible = visibleIds.filter((id) => selectedIds.has(id)).length;
  const allChecked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  const someChecked = selectedVisible > 0 && selectedVisible < visibleIds.length;

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={confirmAll} style={btnStyle}>Confirm All</button>
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={{ ...thStyle, width: 32 }}>
              <input
                type="checkbox"
                data-testid="bulk-select-all"
                aria-label="Select all visible"
                checked={allChecked}
                ref={(el) => {
                  if (el) el.indeterminate = someChecked;
                }}
                onChange={() => onToggleAll(visibleIds)}
              />
            </th>
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
                    <input
                      type="checkbox"
                      data-testid={`bulk-row-checkbox-${item.document.id}`}
                      aria-label={`Select ${item.document.title}`}
                      checked={selectedIds.has(item.document.id)}
                      onChange={() => onToggleRow(item.document.id)}
                    />
                  </td>
                )}
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

export function EdgeReview({ vaultId, edges }: { vaultId: string; edges: StagingEdge[] }) {
  const [staging, setStaging] = useState(edges);
  const [edgeErrors, setEdgeErrors] = useState<Record<string, string>>({});

  // Re-seed the working list when a fresh prop arrives (render-phase resync).
  const [syncedEdges, setSyncedEdges] = useState(edges);
  if (edges !== syncedEdges) {
    setSyncedEdges(edges);
    setStaging(edges);
  }

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
  }

  async function dismissGroup(edgeType: string) {
    const group = staging.filter(e => e.edge_type === edgeType);
    await Promise.all(group.map(e => handleDismiss(e.id)));
  }

  async function confirmAll() {
    await Promise.all(staging.map(e => handleConfirm(e.id)));
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
