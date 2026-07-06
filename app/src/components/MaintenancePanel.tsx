// Top-level Maintenance page.
//
// Structured as a list-of-operations component: the panel renders an
// outer wrapper + per-operation rows. Each row is its own state
// machine (idle → confirming → running → done). Operations:
//   - ReabstractOperation: SSE-streamed Qwen3 reabstract.
//   - OptimizeOperation: synchronous content-store VACUUM (Postgres).

import { useEffect, useState, useRef, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import type { VaultContext } from '../App';
import type {
  OptimizeContentStoreReport,
  ReabstractEvent,
  ReabstractProgressEvent,
  ReabstractSummaryEvent,
} from '../api/types';
import { formatBytes } from '../utils/format';
import {
  startReabstract,
  getDeferredCount,
  startOptimizeContentStore,
} from '../api/maintenance';
import { ApiError } from '../api/client';

export default function MaintenancePanel() {
  const { vaultId } = useOutletContext<VaultContext>();
  return (
    <div data-testid="maintenance-panel">
      <h1 style={{ margin: '0 0 16px' }}>Maintenance</h1>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: '#666' }}>
        Per-vault administrative operations. Scoped to the active vault.
      </p>
      <ReabstractOperation vaultId={vaultId} />
      <OptimizeOperation vaultId={vaultId} />
    </div>
  );
}

type Phase = 'idle' | 'confirming' | 'running' | 'done';

interface ProgressState {
  processed: number;
  total: number;
  current_title: string;
}

interface OpError {
  kind: 'conflict' | 'generic';
  message: string;
}

function ReabstractOperation({ vaultId }: { vaultId: string }) {
  const [count, setCount] = useState<number | null>(null);
  const [countError, setCountError] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [progress, setProgress] = useState<ProgressState>({
    processed: 0,
    total: 0,
    current_title: '',
  });
  const [summary, setSummary] = useState<ReabstractSummaryEvent | null>(null);
  const [opError, setOpError] = useState<OpError | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refreshCount = useCallback(async () => {
    try {
      const c = await getDeferredCount(vaultId);
      setCount(c);
      setCountError('');
    } catch (err) {
      setCountError(err instanceof Error ? err.message : 'Failed to load deferred count');
    }
  }, [vaultId]);

  // Initial fetch on mount + whenever the active vault changes.
  useEffect(() => {
    async function run() {
      await refreshCount();
    }
    run();
  }, [refreshCount]);

  // Refresh count when the operation completes. Decoupled from the
  // success handler so the same refresh applies whether the summary
  // arrives via the SSE summary event or via a stream-end fallback.
  useEffect(() => {
    if (phase !== 'done') return;
    async function run() {
      await refreshCount();
    }
    run();
  }, [phase, refreshCount]);

  // Abort an in-flight stream on unmount (prevents setState-after-unmount).
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const handleConfirmClick = () => {
    setPhase('confirming');
    setOpError(null);
  };

  const handleCancel = () => {
    setPhase('idle');
  };

  const handleStart = async () => {
    setPhase('running');
    setProgress({ processed: 0, total: count ?? 0, current_title: '' });
    setSummary(null);
    setOpError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await startReabstract(
        vaultId,
        (event: ReabstractEvent) => {
          if (event.event_type === 'progress') {
            const pe = event as ReabstractProgressEvent;
            setProgress({
              processed: pe.processed,
              total: pe.total,
              current_title: pe.current_title,
            });
          } else if (event.event_type === 'summary') {
            setSummary(event);
            setPhase('done');
          }
        },
        controller.signal,
        false,
      );
      // Stream ended. If a summary event arrived, phase is already 'done';
      // if not (degenerate case), fall through to 'done' so the user isn't
      // stuck staring at a running spinner.
      setPhase((prev) => (prev === 'running' ? 'done' : prev));
    } catch (err) {
      if (controller.signal.aborted) {
        // Caller cancelled (e.g., component unmount): swallow without surfacing.
        return;
      }
      if (err instanceof ApiError && err.code === 'reabstract_already_in_flight') {
        setOpError({ kind: 'conflict', message: err.message });
      } else {
        const msg = err instanceof Error ? err.message : 'Reabstract failed';
        setOpError({ kind: 'generic', message: msg });
      }
      setPhase('idle');
    }
  };

  const handleDismissSummary = () => {
    setSummary(null);
    setPhase('idle');
  };

  const buttonEnabled = phase === 'idle' && (count ?? 0) > 0;

  return (
    <section data-testid="reabstract-operation" style={sectionStyle}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <h3 style={{ margin: 0, fontSize: 15 }}>Reabstract deferred documents</h3>
        <span data-testid="reabstract-count" style={countStyle}>
          {count === null ? '…' : count} deferred
        </span>
      </div>
      <p style={{ margin: '0 0 12px', fontSize: 13, color: '#666' }}>
        Regenerate semantic abstracts for documents whose initial abstraction was
        skipped. Long-running: per-document Qwen3 inference dominates the wall-clock
        time.
      </p>

      {countError && (
        <div style={errorStyle}>Failed to load deferred count: {countError}</div>
      )}

      {opError?.kind === 'conflict' && (
        <div data-testid="reabstract-conflict" style={warnStyle}>
          Another reabstract is in progress on this vault.{' '}
          {opError.message ? <span>{opError.message}</span> : null}
        </div>
      )}

      {opError?.kind === 'generic' && (
        <div data-testid="reabstract-error" style={errorStyle}>
          Reabstract failed: {opError.message}
        </div>
      )}

      {phase === 'idle' && (
        <button
          data-testid="reabstract-button"
          onClick={handleConfirmClick}
          disabled={!buttonEnabled}
          style={buttonEnabled ? primaryBtnStyle : disabledBtnStyle}
        >
          Reabstract deferred documents
        </button>
      )}

      {phase === 'confirming' && (
        <div data-testid="reabstract-confirm" style={confirmPanelStyle}>
          <p style={{ margin: '0 0 8px', fontSize: 13 }}>
            Reabstract <strong>{count ?? 0}</strong> deferred document
            {count === 1 ? '' : 's'}? This may take several minutes.
          </p>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button onClick={handleCancel} style={secondaryBtnStyle}>
              Cancel
            </button>
            <button
              onClick={handleStart}
              style={primaryBtnStyle}
              data-testid="reabstract-confirm-apply"
            >
              Confirm and reabstract
            </button>
          </div>
        </div>
      )}

      {phase === 'running' && (
        <div data-testid="reabstract-running" style={runningStyle}>
          <div style={{ marginBottom: 8 }}>
            <strong>Reabstracting…</strong>
            {progress.current_title && (
              <>
                {' '}
                <span data-testid="reabstract-current-title">
                  {progress.current_title}
                </span>
              </>
            )}
          </div>
          <div
            data-testid="reabstract-progress-counts"
            style={{ fontSize: 13, color: '#666', marginBottom: 8 }}
          >
            {progress.processed} of {progress.total}
          </div>
          <div style={{ background: '#eee', borderRadius: 4, height: 8 }}>
            <div
              style={{
                background: '#333',
                borderRadius: 4,
                height: 8,
                width: `${
                  progress.total > 0 ? (progress.processed / progress.total) * 100 : 0
                }%`,
                transition: 'width 0.3s',
              }}
            />
          </div>
        </div>
      )}

      {phase === 'done' && summary && (
        <div data-testid="reabstract-summary" style={summaryStyle}>
          <div style={{ marginBottom: 8 }}>
            <strong>Reabstract complete.</strong>
          </div>
          <ul style={{ margin: '0 0 8px', paddingLeft: 18, fontSize: 13 }}>
            <li data-testid="reabstract-reabstracted-count">
              Reabstracted: <strong>{summary.reabstracted_count}</strong>
            </li>
            <li data-testid="reabstract-skipped-count">
              Skipped (PDF): <strong>{summary.skipped_pdf_count}</strong>
            </li>
            <li data-testid="reabstract-failed-count">
              Failed: <strong>{summary.failed_count}</strong>
            </li>
          </ul>
          {summary.failed_count > 0 && (
            <div style={{ marginBottom: 8 }}>
              <h4 style={{ margin: '0 0 4px', fontSize: 12, color: '#666' }}>
                Failures
              </h4>
              <ul
                data-testid="reabstract-failure-list"
                style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: '#c62828' }}
              >
                {summary.entries
                  .filter((e) => e.outcome === 'llm_failure')
                  .slice(0, 3)
                  .map((e) => (
                    <li key={e.document_id}>
                      <code>{e.document_id}</code>: {e.error_message ?? 'unknown error'}
                    </li>
                  ))}
              </ul>
            </div>
          )}
          <button
            onClick={handleDismissSummary}
            style={secondaryBtnStyle}
            data-testid="reabstract-dismiss"
          >
            Dismiss
          </button>
        </div>
      )}
    </section>
  );
}

type OptimizePhase = 'idle' | 'confirming' | 'running' | 'done';

function OptimizeOperation({ vaultId }: { vaultId: string }) {
  const [phase, setPhase] = useState<OptimizePhase>('idle');
  const [report, setReport] = useState<OptimizeContentStoreReport | null>(null);
  const [opError, setOpError] = useState<string | null>(null);

  const handleConfirmClick = () => {
    setPhase('confirming');
    setOpError(null);
  };

  const handleCancel = () => {
    setPhase('idle');
  };

  const handleStart = async () => {
    setPhase('running');
    setReport(null);
    setOpError(null);
    try {
      const result = await startOptimizeContentStore(vaultId);
      setReport(result);
      setPhase('done');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Optimize failed';
      setOpError(msg);
      setPhase('idle');
    }
  };

  const handleDismiss = () => {
    setReport(null);
    setPhase('idle');
  };

  return (
    <section data-testid="optimize-operation" style={sectionStyle}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <h3 style={{ margin: 0, fontSize: 15 }}>Optimize content store</h3>
      </div>
      <p style={{ margin: '0 0 12px', fontSize: 13, color: '#666' }}>
        Run VACUUM (FULL, ANALYZE) on the content-store table: removes dead row
        versions and returns free space to the OS.
      </p>

      {opError && (
        <div data-testid="optimize-error" style={errorStyle}>
          Optimize failed: {opError}
        </div>
      )}

      {phase === 'idle' && (
        <button
          data-testid="optimize-button"
          onClick={handleConfirmClick}
          style={primaryBtnStyle}
        >
          Optimize content store
        </button>
      )}

      {phase === 'confirming' && (
        <div data-testid="optimize-confirm" style={confirmPanelStyle}>
          <p style={{ margin: '0 0 8px', fontSize: 13 }}>
            Run <strong>VACUUM FULL</strong> on this vault's content-store table?
            It reclaims dead rows and returns free space to the OS, and holds an{' '}
            <strong>exclusive lock</strong> on the table while it runs.
          </p>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button onClick={handleCancel} style={secondaryBtnStyle}>
              Cancel
            </button>
            <button
              onClick={handleStart}
              style={primaryBtnStyle}
              data-testid="optimize-confirm-apply"
            >
              Confirm and optimize
            </button>
          </div>
        </div>
      )}

      {phase === 'running' && (
        <div data-testid="optimize-running" style={runningStyle}>
          <strong>Optimizing…</strong>
        </div>
      )}

      {phase === 'done' && report && (
        <div data-testid="optimize-summary" style={summaryStyle}>
          <div style={{ marginBottom: 8 }}>
            <strong>Optimize complete.</strong>
          </div>
          <ul style={{ margin: '0 0 8px', paddingLeft: 18, fontSize: 13 }}>
            <li data-testid="optimize-bytes-reclaimed">
              Reclaimed: <strong>{formatBytes(report.bytes_reclaimed)}</strong>
            </li>
            <li data-testid="optimize-dead-rows-removed">
              Dead rows removed:{' '}
              <strong>{report.pre_versions - report.post_versions}</strong>
            </li>
          </ul>
          <button
            onClick={handleDismiss}
            style={secondaryBtnStyle}
            data-testid="optimize-dismiss"
          >
            Dismiss
          </button>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const sectionStyle: React.CSSProperties = {
  padding: 16,
  border: '1px solid #ddd',
  borderRadius: 4,
  background: '#fff',
  marginBottom: 16,
};

const countStyle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: '#666',
};

const primaryBtnStyle: React.CSSProperties = {
  padding: '6px 16px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};

const secondaryBtnStyle: React.CSSProperties = {
  padding: '6px 16px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#fff',
  color: '#333',
  cursor: 'pointer',
  fontSize: 13,
};

const disabledBtnStyle: React.CSSProperties = {
  ...primaryBtnStyle,
  background: '#ccc',
  color: '#888',
  cursor: 'not-allowed',
};

const confirmPanelStyle: React.CSSProperties = {
  padding: 12,
  background: '#fff3e0',
  border: '1px solid #ffb74d',
  borderRadius: 4,
};

const runningStyle: React.CSSProperties = {
  padding: 12,
  background: '#f5f5f5',
  border: '1px solid #ddd',
  borderRadius: 4,
};

const summaryStyle: React.CSSProperties = {
  padding: 12,
  background: '#e8f5e9',
  border: '1px solid #a5d6a7',
  borderRadius: 4,
};

const warnStyle: React.CSSProperties = {
  padding: '8px 12px',
  marginBottom: 12,
  background: '#fff3e0',
  color: '#e65100',
  borderRadius: 4,
  fontSize: 13,
};

const errorStyle: React.CSSProperties = {
  padding: '8px 12px',
  marginBottom: 12,
  background: '#fce4ec',
  color: '#c62828',
  borderRadius: 4,
  fontSize: 13,
};
