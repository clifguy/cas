// Vitest specs for MaintenancePanel (T-0117).
//
// Mocks:
// - react-router-dom's useOutletContext → injects a stub VaultContext.
// - ../api/maintenance exports → controllable spies for startReabstract
//   and getDeferredCount.
// - ApiError is imported from ../api/client (real class) so the panel's
//   `err instanceof ApiError && err.code === 'reabstract_already_in_flight'`
//   discriminator runs against the real prototype.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MaintenancePanel from './MaintenancePanel';
import * as maintenanceApi from '../api/maintenance';
import { ApiError } from '../api/client';
import type { VaultContext } from '../App';
import type {
  OptimizeContentStoreReport,
  ReabstractEvent,
  ReabstractProgressEvent,
  ReabstractSummaryEvent,
} from '../api/types';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useOutletContext: () => ({ vaultId: 'v1', vault: null, vaults: [] } as VaultContext),
  };
});

vi.mock('../api/maintenance', () => ({
  startReabstract: vi.fn(),
  getDeferredCount: vi.fn(),
  startOptimizeContentStore: vi.fn(),
}));

const startReabstractMock = vi.mocked(maintenanceApi.startReabstract);
const getDeferredCountMock = vi.mocked(maintenanceApi.getDeferredCount);
const startOptimizeContentStoreMock = vi.mocked(
  maintenanceApi.startOptimizeContentStore,
);

beforeEach(() => {
  startReabstractMock.mockReset();
  getDeferredCountMock.mockReset();
  startOptimizeContentStoreMock.mockReset();
  // Default the deferred-count stub so the optimize-only tests don't
  // wedge waiting for ReabstractOperation's mount-time fetch.
  getDeferredCountMock.mockResolvedValue(0);
});

// ---------------------------------------------------------------------------
// Idle state (B1, B2)
// ---------------------------------------------------------------------------

describe('MaintenancePanel — idle state', () => {
  it('B1: renders the deferred count and enables the button when count > 0', async () => {
    getDeferredCountMock.mockResolvedValue(7);
    render(<MaintenancePanel />);

    await waitFor(() => {
      expect(screen.getByTestId('reabstract-count')).toHaveTextContent('7');
    });
    expect(screen.getByTestId('reabstract-button')).not.toBeDisabled();
  });

  it('B2: disables the button when count is 0', async () => {
    getDeferredCountMock.mockResolvedValue(0);
    render(<MaintenancePanel />);

    await waitFor(() => {
      expect(screen.getByTestId('reabstract-count')).toHaveTextContent('0');
    });
    expect(screen.getByTestId('reabstract-button')).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Confirmation flow (B3, B4)
// ---------------------------------------------------------------------------

describe('MaintenancePanel — confirmation flow', () => {
  it('B3: clicking the button shows the confirm panel and does NOT fire the API', async () => {
    getDeferredCountMock.mockResolvedValue(5);
    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('5'));

    await userEvent.click(screen.getByTestId('reabstract-button'));

    expect(screen.getByTestId('reabstract-confirm')).toBeInTheDocument();
    expect(startReabstractMock).not.toHaveBeenCalled();
  });

  it('B4: confirm fires startReabstract with the active vault id', async () => {
    getDeferredCountMock.mockResolvedValue(5);
    // Promise never resolves — pins the panel in 'running' so the
    // call-args assertion isn't racing the post-completion teardown.
    startReabstractMock.mockImplementation(() => new Promise<void>(() => {}));
    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('5'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    expect(startReabstractMock).toHaveBeenCalledTimes(1);
    const [vaultId, , , includePdf] = startReabstractMock.mock.calls[0];
    expect(vaultId).toBe('v1');
    expect(includePdf).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Running state (B5, B6)
// ---------------------------------------------------------------------------

describe('MaintenancePanel — running state', () => {
  it('B5: in-flight state hides the reabstract button and shows the running panel', async () => {
    getDeferredCountMock.mockResolvedValue(5);
    startReabstractMock.mockImplementation(() => new Promise<void>(() => {}));
    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('5'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    expect(screen.getByTestId('reabstract-running')).toBeInTheDocument();
    expect(screen.queryByTestId('reabstract-button')).not.toBeInTheDocument();
  });

  it('B6: progress events drive the current-title and processed/total counters', async () => {
    getDeferredCountMock.mockResolvedValue(2);

    let capturedOnEvent: ((event: ReabstractEvent) => void) | null = null;
    startReabstractMock.mockImplementation(async (_vaultId, onEvent) => {
      capturedOnEvent = onEvent;
      await new Promise<void>(() => {}); // keep stream open
    });

    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('2'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    // 'started' for the first document — title appears, processed still 0.
    act(() => {
      capturedOnEvent!({
        event_type: 'progress',
        processed: 0,
        total: 2,
        current_document_id: 'aaaaaaaa_alpha',
        current_title: 'Alpha doc',
        status: 'started',
      } satisfies ReabstractProgressEvent);
    });
    expect(screen.getByTestId('reabstract-current-title')).toHaveTextContent('Alpha doc');
    expect(screen.getByTestId('reabstract-progress-counts')).toHaveTextContent('0 of 2');

    // 'completed' for the first doc — processed increments.
    act(() => {
      capturedOnEvent!({
        event_type: 'progress',
        processed: 1,
        total: 2,
        current_document_id: 'aaaaaaaa_alpha',
        current_title: 'Alpha doc',
        status: 'completed',
        outcome: 'success',
        elapsed_seconds: 1.0,
      } satisfies ReabstractProgressEvent);
    });
    expect(screen.getByTestId('reabstract-progress-counts')).toHaveTextContent('1 of 2');

    // 'started' for the second doc — title rolls over.
    act(() => {
      capturedOnEvent!({
        event_type: 'progress',
        processed: 1,
        total: 2,
        current_document_id: 'bbbbbbbb_beta',
        current_title: 'Beta doc',
        status: 'started',
      } satisfies ReabstractProgressEvent);
    });
    expect(screen.getByTestId('reabstract-current-title')).toHaveTextContent('Beta doc');
  });
});

// ---------------------------------------------------------------------------
// Completion (B7, B8)
// ---------------------------------------------------------------------------

describe('MaintenancePanel — completion', () => {
  it('B7/B8: summary renders all three counts, refetches count, and returns to idle on dismiss', async () => {
    // First call: 3 docs deferred. Second call (after reabstract): 0.
    getDeferredCountMock.mockResolvedValueOnce(3).mockResolvedValueOnce(0);

    startReabstractMock.mockImplementation(async (_vaultId, onEvent) => {
      onEvent({
        event_type: 'summary',
        vault_id: 'v1',
        reabstracted_count: 2,
        skipped_pdf_count: 1,
        failed_count: 0,
        entries: [],
      } satisfies ReabstractSummaryEvent);
    });

    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('3'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    // Summary appears with all three counts.
    await waitFor(() =>
      expect(screen.getByTestId('reabstract-summary')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('reabstract-reabstracted-count')).toHaveTextContent('2');
    expect(screen.getByTestId('reabstract-skipped-count')).toHaveTextContent('1');
    expect(screen.getByTestId('reabstract-failed-count')).toHaveTextContent('0');

    // Count was refetched on phase=done transition.
    await waitFor(() =>
      expect(getDeferredCountMock.mock.calls.length).toBeGreaterThanOrEqual(2),
    );

    // Dismiss → idle. New count (0) makes the button disabled.
    await userEvent.click(screen.getByTestId('reabstract-dismiss'));
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('0'));
    expect(screen.getByTestId('reabstract-button')).toBeDisabled();
  });

  it('B7b: summary with failures renders the failure list (up to 3 entries)', async () => {
    getDeferredCountMock.mockResolvedValue(2);

    startReabstractMock.mockImplementation(async (_vaultId, onEvent) => {
      onEvent({
        event_type: 'summary',
        vault_id: 'v1',
        reabstracted_count: 1,
        skipped_pdf_count: 0,
        failed_count: 1,
        entries: [
          {
            document_id: 'cccccccc_gamma',
            outcome: 'success',
            error_message: null,
            elapsed_seconds: 1.0,
          },
          {
            document_id: 'dddddddd_delta',
            outcome: 'llm_failure',
            error_message: 'oom',
            elapsed_seconds: 0.5,
          },
        ],
      } satisfies ReabstractSummaryEvent);
    });

    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('2'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    await waitFor(() =>
      expect(screen.getByTestId('reabstract-failure-list')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('reabstract-failure-list')).toHaveTextContent(
      'dddddddd_delta',
    );
    expect(screen.getByTestId('reabstract-failure-list')).toHaveTextContent('oom');
  });
});

// ---------------------------------------------------------------------------
// Error handling (B9, B10)
// ---------------------------------------------------------------------------

describe('MaintenancePanel — error handling', () => {
  it('B9: 409 reabstract_already_in_flight surfaces the conflict alert (not the generic error)', async () => {
    getDeferredCountMock.mockResolvedValue(3);
    const err = new ApiError(
      'reabstract_already_in_flight',
      'A reabstract is already running on this vault.',
    );
    startReabstractMock.mockRejectedValue(err);

    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('3'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    await waitFor(() =>
      expect(screen.getByTestId('reabstract-conflict')).toBeInTheDocument(),
    );
    // Specifically NOT the generic error path.
    expect(screen.queryByTestId('reabstract-error')).not.toBeInTheDocument();
    // Panel returns to idle (button is back).
    expect(screen.getByTestId('reabstract-button')).toBeInTheDocument();
  });

  it('B10: generic ApiError surfaces the generic error alert', async () => {
    getDeferredCountMock.mockResolvedValue(3);
    const err = new ApiError('internal_error', 'Something broke');
    startReabstractMock.mockRejectedValue(err);

    render(<MaintenancePanel />);
    await waitFor(() => expect(screen.getByTestId('reabstract-count')).toHaveTextContent('3'));

    await userEvent.click(screen.getByTestId('reabstract-button'));
    await userEvent.click(screen.getByTestId('reabstract-confirm-apply'));

    await waitFor(() =>
      expect(screen.getByTestId('reabstract-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('reabstract-error')).toHaveTextContent('Something broke');
    expect(screen.queryByTestId('reabstract-conflict')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// OptimizeOperation
// ===========================================================================

// Helper: build an OptimizeContentStoreReport fixture with distinct values
// so a JSX field-swap in the summary produces a visible miss.
function makeReport(
  overrides: Partial<OptimizeContentStoreReport> = {},
): OptimizeContentStoreReport {
  return {
    vault_id: 'v1',
    cleanup_older_than_days: 7,
    started_at: '2026-05-28T12:00:00Z',
    finished_at: '2026-05-28T12:00:05Z',
    pre_bytes: 10_000,
    post_bytes: 8_766,
    bytes_reclaimed: 1234,
    pre_versions: 10,
    post_versions: 5,
    pre_fragments: 12,
    post_fragments: 4,
    pre_small_fragments: 6,
    post_small_fragments: 2,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Idle (O1)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — idle', () => {
  it('O1: idle shows an enabled optimize button and no days input', async () => {
    render(<MaintenancePanel />);

    expect(await screen.findByTestId('optimize-button')).not.toBeDisabled();
    // The LanceDB-era age-prune control is gone — VACUUM has no age threshold.
    expect(screen.queryByTestId('optimize-days-input')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Confirmation flow (O5, O6)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — confirmation flow', () => {
  it('O5: clicking the button shows the confirm panel with Postgres-accurate copy and does NOT fire the API', async () => {
    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));

    const confirm = screen.getByTestId('optimize-confirm');
    expect(confirm).toBeInTheDocument();
    // VACUUM + exclusive-lock caveat, not LanceDB dataset-version pruning.
    expect(confirm).toHaveTextContent(/VACUUM FULL/);
    expect(confirm).toHaveTextContent(/exclusive lock/i);
    expect(confirm).not.toHaveTextContent(/not undoable/i);
    expect(confirm).not.toHaveTextContent(/dataset version/i);
    expect(startOptimizeContentStoreMock).not.toHaveBeenCalled();
  });

  it('O6: cancel from confirming returns to idle with no API call', async () => {
    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(screen.queryByTestId('optimize-confirm')).not.toBeInTheDocument();
    expect(screen.getByTestId('optimize-button')).toBeInTheDocument();
    expect(startOptimizeContentStoreMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Confirm-apply fires the API with the typed value (O7)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — apply', () => {
  it('O7: confirm-apply fires startOptimizeContentStore with just the vault id (no days argument)', async () => {
    // Promise never resolves — pins the panel in 'running' so the
    // call-args assertion isn't racing teardown.
    startOptimizeContentStoreMock.mockImplementation(
      () => new Promise<OptimizeContentStoreReport>(() => {}),
    );

    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));
    await userEvent.click(screen.getByTestId('optimize-confirm-apply'));

    expect(startOptimizeContentStoreMock).toHaveBeenCalledTimes(1);
    expect(startOptimizeContentStoreMock).toHaveBeenCalledWith('v1');
    // The dropped age-prune knob must not sneak back as a second argument.
    expect(startOptimizeContentStoreMock.mock.calls[0]).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Running state (O8)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — running state', () => {
  it('O8: in-flight state hides the optimize button and shows the running panel', async () => {
    startOptimizeContentStoreMock.mockImplementation(
      () => new Promise<OptimizeContentStoreReport>(() => {}),
    );

    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));
    await userEvent.click(screen.getByTestId('optimize-confirm-apply'));

    expect(screen.getByTestId('optimize-running')).toBeInTheDocument();
    expect(screen.queryByTestId('optimize-button')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Completion (O9)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — completion', () => {
  it('O9: summary renders humanized reclaimed bytes and dead rows removed, with no fragments row', async () => {
    startOptimizeContentStoreMock.mockResolvedValue(
      makeReport({
        bytes_reclaimed: 1234,
        pre_versions: 10,
        post_versions: 5, // → 5 dead rows removed
      }),
    );

    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));
    await userEvent.click(screen.getByTestId('optimize-confirm-apply'));

    await waitFor(() =>
      expect(screen.getByTestId('optimize-summary')).toBeInTheDocument(),
    );
    // bytes_reclaimed=1234 renders humanized (1.2 KB), not as a raw integer.
    expect(screen.getByTestId('optimize-bytes-reclaimed')).toHaveTextContent('1.2 KB');
    expect(screen.getByTestId('optimize-bytes-reclaimed')).not.toHaveTextContent('1234');
    expect(screen.getByTestId('optimize-dead-rows-removed')).toHaveTextContent('5');
    // The LanceDB versions/fragments rows are gone.
    expect(screen.queryByTestId('optimize-fragments-merged')).not.toBeInTheDocument();
    expect(screen.queryByTestId('optimize-versions-cleaned')).not.toBeInTheDocument();

    // Dismiss → back to idle.
    await userEvent.click(screen.getByTestId('optimize-dismiss'));
    expect(screen.queryByTestId('optimize-summary')).not.toBeInTheDocument();
    expect(screen.getByTestId('optimize-button')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Error handling (O10)
// ---------------------------------------------------------------------------

describe('OptimizeOperation — error handling', () => {
  it('O10: generic ApiError surfaces the error alert and returns to idle', async () => {
    const err = new ApiError('internal_error', 'compaction broke');
    startOptimizeContentStoreMock.mockRejectedValue(err);

    render(<MaintenancePanel />);
    await userEvent.click(await screen.findByTestId('optimize-button'));
    await userEvent.click(screen.getByTestId('optimize-confirm-apply'));

    await waitFor(() =>
      expect(screen.getByTestId('optimize-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('optimize-error')).toHaveTextContent('compaction broke');
    expect(screen.getByTestId('optimize-button')).toBeInTheDocument();
  });
});
