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
}));

const startReabstractMock = vi.mocked(maintenanceApi.startReabstract);
const getDeferredCountMock = vi.mocked(maintenanceApi.getDeferredCount);

beforeEach(() => {
  startReabstractMock.mockReset();
  getDeferredCountMock.mockReset();
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
