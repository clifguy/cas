import { useMemo, useState } from 'react';
import { Dialog } from './Dialog';
import { bulkUpdateMetadata } from '../api/bulk';
import type { BulkMetadataItem, BulkMetadataResponse, ListFieldPatch, Tier3Patch } from '../api/types';

const BULK_CONFIRM_THRESHOLD = 10;

interface Props {
  vaultId: string;
  selectedIds: string[];
  onResolved: (result: { succeeded: string[]; failed: string[] }) => void;
  onClose: () => void;
}

type Phase = 'idle' | 'confirming' | 'submitting' | 'results' | 'error';

interface Tier3Row {
  key: string;
  value: string;
}

function splitCSV(s: string): string[] {
  return s
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
}

export function BulkMetadataDialog({ vaultId, selectedIds, onResolved, onClose }: Props) {
  const [tagsAdd, setTagsAdd] = useState('');
  const [tagsRemove, setTagsRemove] = useState('');
  const [tier3Set, setTier3Set] = useState<Tier3Row[]>([{ key: '', value: '' }]);
  const [tier3Unset, setTier3Unset] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [response, setResponse] = useState<BulkMetadataResponse | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const addList = splitCSV(tagsAdd);
  const removeList = splitCSV(tagsRemove);
  const setEntries = tier3Set.filter((r) => r.key.trim() !== '');
  const unsetList = splitCSV(tier3Unset);

  // Derive the source lists inside each memo so the dependency arrays stay
  // exhaustive on the underlying state (the outer addList/removeList/etc. are
  // fresh arrays each render and would defeat memoization if listed).
  const tagsOverlap = useMemo(() => {
    const adds = splitCSV(tagsAdd);
    const removes = splitCSV(tagsRemove);
    return adds.filter((t) => removes.includes(t));
  }, [tagsAdd, tagsRemove]);
  const tier3Overlap = useMemo(() => {
    const keys = tier3Set.filter((r) => r.key.trim() !== '').map((r) => r.key.trim());
    const unsets = splitCSV(tier3Unset);
    return keys.filter((k) => unsets.includes(k));
  }, [tier3Set, tier3Unset]);

  const hasAnyOp = addList.length > 0 || removeList.length > 0 || setEntries.length > 0 || unsetList.length > 0;
  const hasOverlap = tagsOverlap.length > 0 || tier3Overlap.length > 0;
  const applyDisabled = !hasAnyOp || hasOverlap || phase === 'submitting';

  function buildItems(): BulkMetadataItem[] {
    const tags: ListFieldPatch | undefined =
      addList.length > 0 || removeList.length > 0
        ? {
            ...(addList.length > 0 ? { add: addList } : {}),
            ...(removeList.length > 0 ? { remove: removeList } : {}),
          }
        : undefined;
    const tier3SetObj: Record<string, string> = {};
    for (const row of setEntries) {
      tier3SetObj[row.key.trim()] = row.value;
    }
    const tier3_metadata: Tier3Patch | undefined =
      setEntries.length > 0 || unsetList.length > 0
        ? {
            ...(setEntries.length > 0 ? { set: tier3SetObj } : {}),
            ...(unsetList.length > 0 ? { unset: unsetList } : {}),
          }
        : undefined;
    return selectedIds.map((id) => ({
      document_id: id,
      ...(tags ? { tags } : {}),
      ...(tier3_metadata ? { tier3_metadata } : {}),
    }));
  }

  async function submit() {
    setPhase('submitting');
    setSubmitError(null);
    try {
      const resp = await bulkUpdateMetadata(vaultId, buildItems());
      setResponse(resp);
      setPhase('results');
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Bulk metadata request failed');
      setPhase('error');
    }
  }

  function handleApply() {
    if (selectedIds.length > BULK_CONFIRM_THRESHOLD) {
      setPhase('confirming');
    } else {
      submit();
    }
  }

  function handleResultsClose() {
    if (!response) {
      onResolved({ succeeded: [], failed: [] });
      onClose();
      return;
    }
    const succeeded = response.results.filter((r) => r.status === 'success').map((r) => r.document_id);
    const failed = response.results.filter((r) => r.status === 'error').map((r) => r.document_id);
    onResolved({ succeeded, failed });
    onClose();
  }

  function updateTier3Row(idx: number, field: 'key' | 'value', val: string) {
    setTier3Set((prev) => prev.map((r, i) => (i === idx ? { ...r, [field]: val } : r)));
  }
  function addTier3Row() {
    setTier3Set((prev) => [...prev, { key: '', value: '' }]);
  }
  function removeTier3Row(idx: number) {
    setTier3Set((prev) => prev.filter((_, i) => i !== idx));
  }

  return (
    <Dialog title="Bulk update metadata" onClose={onClose} width={560}>
      {phase === 'results' && response ? (
        <ResultsPanel response={response} onClose={handleResultsClose} />
      ) : phase === 'error' ? (
        <div>
          <p style={{ color: '#c62828', margin: '0 0 12px' }}>Bulk request failed: {submitError}</p>
          <button type="button" onClick={onClose} style={primaryBtnStyle}>Close</button>
        </div>
      ) : (
        <>
          <p style={{ margin: '0 0 12px', fontSize: 13 }}>
            Update metadata on <strong>{selectedIds.length}</strong> selected document{selectedIds.length === 1 ? '' : 's'}.
            Empty lanes are skipped; the four ops mirror CAS-ADR-028.
          </p>

          <div style={lanesGrid}>
            <Lane title="Tags > Add" testId="lane-tags-add" help="Comma-separated tags to add">
              <input type="text" value={tagsAdd} onChange={(e) => setTagsAdd(e.target.value)} style={inputStyle} aria-label="Tags to add" />
            </Lane>
            <Lane title="Tags > Remove" testId="lane-tags-remove" help="Comma-separated tags to remove">
              <input type="text" value={tagsRemove} onChange={(e) => setTagsRemove(e.target.value)} style={inputStyle} aria-label="Tags to remove" />
            </Lane>
            <Lane title="Tier3 > Set" testId="lane-tier3-set" help="Key-value pairs to set (overwrites existing keys)">
              {tier3Set.map((row, i) => (
                <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 4, alignItems: 'center' }}>
                  <input type="text" value={row.key} onChange={(e) => updateTier3Row(i, 'key', e.target.value)} placeholder="key" style={{ ...inputStyle, flex: 1 }} />
                  <input type="text" value={row.value} onChange={(e) => updateTier3Row(i, 'value', e.target.value)} placeholder="value" style={{ ...inputStyle, flex: 1 }} />
                  {tier3Set.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeTier3Row(i)}
                      aria-label={`Remove row ${i + 1}`}
                      style={removeRowBtnStyle}
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
              <button type="button" onClick={addTier3Row} style={secondaryBtnStyle}>Add row</button>
            </Lane>
            <Lane title="Tier3 > Unset" testId="lane-tier3-unset" help="Comma-separated keys to remove (must be present)">
              <input type="text" value={tier3Unset} onChange={(e) => setTier3Unset(e.target.value)} style={inputStyle} aria-label="Tier3 keys to unset" />
            </Lane>
          </div>

          {tagsOverlap.length > 0 && (
            <div style={errStyle}>tags add and remove must be disjoint; overlap: {tagsOverlap.join(', ')}</div>
          )}
          {tier3Overlap.length > 0 && (
            <div style={errStyle}>tier3 set and unset must be disjoint; overlap: {tier3Overlap.join(', ')}</div>
          )}

          {phase === 'confirming' && (
            <div data-testid="bulk-metadata-confirm" style={confirmPanelStyle}>
              <p style={{ margin: '0 0 8px', fontSize: 13 }}>
                Update metadata on <strong>{selectedIds.length}</strong> documents?
              </p>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" onClick={() => setPhase('idle')} style={secondaryBtnStyle}>Cancel</button>
                <button type="button" onClick={submit} style={primaryBtnStyle} data-testid="bulk-metadata-confirm-apply">
                  Confirm and apply
                </button>
              </div>
            </div>
          )}

          {phase !== 'confirming' && (
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
              <button type="button" onClick={onClose} style={secondaryBtnStyle}>Cancel</button>
              <button
                type="button"
                onClick={handleApply}
                disabled={applyDisabled}
                style={primaryBtnStyle}
                data-testid="bulk-metadata-apply"
              >
                {phase === 'submitting' ? 'Applying…' : 'Apply'}
              </button>
            </div>
          )}
        </>
      )}
    </Dialog>
  );
}

function Lane({
  title,
  testId,
  help,
  children,
}: {
  title: string;
  testId: string;
  help: string;
  children: React.ReactNode;
}) {
  return (
    <div data-testid={testId} style={laneStyle}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 2 }}>{title}</div>
      <div style={{ fontSize: 11, color: '#888', marginBottom: 6 }}>{help}</div>
      {children}
    </div>
  );
}

function ResultsPanel({ response, onClose }: { response: BulkMetadataResponse; onClose: () => void }) {
  const failed = response.results.filter((r) => r.status === 'error');
  return (
    <div>
      <p data-testid="bulk-metadata-results-summary" style={{ margin: '0 0 12px', fontSize: 13 }}>
        <strong>{response.success_count} succeeded, {response.error_count} failed</strong> of {response.total}.
      </p>
      {failed.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <h3 style={{ fontSize: 13, margin: '0 0 6px', fontWeight: 600 }}>Failures</h3>
          <ul style={{ margin: 0, padding: '0 0 0 16px', fontSize: 12, color: '#c62828' }}>
            {failed.map((r) => (
              <li key={r.document_id}>
                <code>{r.document_id}</code>: {r.error?.message ?? 'unknown error'}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" onClick={onClose} style={primaryBtnStyle}>Close</button>
      </div>
    </div>
  );
}

const lanesGrid: React.CSSProperties = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 };
const laneStyle: React.CSSProperties = { background: '#f9f9f9', border: '1px solid #eee', borderRadius: 4, padding: 10 };
const inputStyle: React.CSSProperties = { padding: '4px 8px', fontSize: 12, boxSizing: 'border-box', width: '100%' };
const errStyle: React.CSSProperties = { color: '#c62828', fontSize: 12, marginBottom: 8 };
const primaryBtnStyle: React.CSSProperties = { padding: '6px 16px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 13 };
const secondaryBtnStyle: React.CSSProperties = { padding: '4px 12px', border: '1px solid #ccc', borderRadius: 4, background: '#fff', color: '#333', cursor: 'pointer', fontSize: 12 };
const confirmPanelStyle: React.CSSProperties = { padding: 12, background: '#fff3e0', border: '1px solid #ffb74d', borderRadius: 4, marginTop: 12 };
const removeRowBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  fontSize: 16,
  lineHeight: 1,
  cursor: 'pointer',
  color: '#666',
  padding: '0 4px',
};
