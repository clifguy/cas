import { useEffect, useState } from 'react';
import { Dialog } from './Dialog';
import { getVaultConfig } from '../api/vaults';
import { bulkSetLifecycle } from '../api/bulk';
import type { BulkLifecycleResponse, VaultConfig } from '../api/types';

const BULK_CONFIRM_THRESHOLD = 10;

interface Props {
  vaultId: string;
  selectedIds: string[];
  onResolved: (result: { succeeded: string[]; failed: string[] }) => void;
  onClose: () => void;
}

type Phase = 'idle' | 'confirming' | 'submitting' | 'results' | 'error';

export function BulkLifecycleDialog({ vaultId, selectedIds, onResolved, onClose }: Props) {
  const [actions, setActions] = useState<string[] | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [action, setAction] = useState<string>('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [response, setResponse] = useState<BulkLifecycleResponse | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getVaultConfig(vaultId)
      .then((cfg: VaultConfig) => {
        if (cancelled) return;
        const distinct = Array.from(
          new Set(cfg.lifecycle.transitions.map((t) => t.action).filter((a) => a !== 'supersede')),
        );
        setActions(distinct);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setConfigError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [vaultId]);

  async function submit() {
    setPhase('submitting');
    setSubmitError(null);
    try {
      const resp = await bulkSetLifecycle(
        vaultId,
        selectedIds.map((id) => ({ document_id: id, action })),
      );
      setResponse(resp);
      setPhase('results');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Bulk lifecycle request failed';
      setSubmitError(msg);
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

  return (
    <Dialog title="Bulk set lifecycle" onClose={onClose}>
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
            Apply a lifecycle action to <strong data-testid="bulk-lifecycle-count">{selectedIds.length}</strong> selected document{selectedIds.length === 1 ? '' : 's'}.
          </p>
          {configError && (
            <div style={{ color: '#c62828', fontSize: 12, marginBottom: 8 }}>Failed to load vault config: {configError}</div>
          )}
          <label style={labelStyle}>
            Action
            <select
              data-testid="bulk-lifecycle-action"
              value={action}
              onChange={(e) => setAction(e.target.value)}
              disabled={!actions}
              style={selectStyle}
              aria-label="Action"
            >
              <option value="">Select an action&hellip;</option>
              {actions?.map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
          </label>

          {phase === 'confirming' && (
            <div data-testid="bulk-lifecycle-confirm" style={confirmPanelStyle}>
              <p style={{ margin: '0 0 8px', fontSize: 13 }}>
                Apply <strong>{action}</strong> to <strong>{selectedIds.length}</strong> documents?
              </p>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" onClick={() => setPhase('idle')} style={secondaryBtnStyle}>Cancel</button>
                <button type="button" onClick={submit} style={primaryBtnStyle} data-testid="bulk-lifecycle-confirm-apply">
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
                disabled={!action || phase === 'submitting'}
                style={primaryBtnStyle}
                data-testid="bulk-lifecycle-apply"
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

function ResultsPanel({ response, onClose }: { response: BulkLifecycleResponse; onClose: () => void }) {
  const failed = response.results.filter((r) => r.status === 'error');
  return (
    <div>
      <p data-testid="bulk-lifecycle-results-summary" style={{ margin: '0 0 12px', fontSize: 13 }}>
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

const labelStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: '#666', marginBottom: 12 };
const selectStyle: React.CSSProperties = { padding: '6px 8px', fontSize: 13 };
const primaryBtnStyle: React.CSSProperties = { padding: '6px 16px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 13 };
const secondaryBtnStyle: React.CSSProperties = { padding: '6px 16px', border: '1px solid #ccc', borderRadius: 4, background: '#fff', color: '#333', cursor: 'pointer', fontSize: 13 };
const confirmPanelStyle: React.CSSProperties = { padding: 12, background: '#fff3e0', border: '1px solid #ffb74d', borderRadius: 4, marginTop: 12 };
