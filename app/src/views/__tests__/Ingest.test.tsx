import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router';
import Ingest from '../Ingest';
import * as ingestApi from '../../api/ingest';
import type { VaultContext } from '../../App';
import type { VaultSummary, BatchIngestEvent } from '../../api/types';

// Mock the ingest API surface but keep sourceTypeForFilename real so the
// view's extension-based source-type derivation (E5) is genuinely exercised.
vi.mock('../../api/ingest', async () => {
  const actual = await vi.importActual<typeof import('../../api/ingest')>('../../api/ingest');
  return {
    ...actual,
    detectIngestProfile: vi.fn(),
    uploadBatchIngest: vi.fn(),
    scanDirectory: vi.fn(),
    startIngestion: vi.fn(),
  };
});

const detectIngestProfileMock = vi.mocked(ingestApi.detectIngestProfile);
const uploadBatchIngestMock = vi.mocked(ingestApi.uploadBatchIngest);

const mockVault: VaultSummary = {
  id: 'example_vault',
  name: 'Example Vault',
  description: 'Test vault',
  storage_root: '/tmp/test',
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function TestWrapper({ vaultId, vault }: { vaultId: string; vault: VaultSummary | null }) {
  const ctx: VaultContext = { vaultId, vault, vaults: vault ? [vault] : [] };
  return (
    <MemoryRouter initialEntries={['/ingest']}>
      <Routes>
        <Route element={<WrapperWithContext ctx={ctx} />}>
          <Route path="ingest" element={<Ingest />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

function WrapperWithContext({ ctx }: { ctx: VaultContext }) {
  return <Outlet context={ctx} />;
}

beforeEach(() => {
  detectIngestProfileMock.mockReset();
  uploadBatchIngestMock.mockReset();
});

describe('Ingest view — co-located profile', () => {
  beforeEach(() => {
    detectIngestProfileMock.mockResolvedValue('co-located');
  });

  it('renders all three step labels', () => {
    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    expect(screen.getByText(/1\. Directory Input/)).toBeInTheDocument();
    expect(screen.getByText(/2\. Scan Preview/)).toBeInTheDocument();
    expect(screen.getByText(/3\. Ingestion/)).toBeInTheDocument();
  });

  it('E1: shows the directory input and Scan button, not the file picker', async () => {
    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    expect(await screen.findByPlaceholderText('/path/to/source/directory')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /scan/i })).toBeInTheDocument();
    expect(screen.queryByTestId('upload-file-input')).not.toBeInTheDocument();
  });

  it('shows vault not found for unknown vault', () => {
    render(<TestWrapper vaultId="nonexistent" vault={null} />);
    expect(screen.getByText('Vault not found.')).toBeInTheDocument();
  });
});

describe('Ingest view — hosted profile', () => {
  beforeEach(() => {
    detectIngestProfileMock.mockResolvedValue('hosted');
  });

  it('E2: shows the file-upload affordance, not the directory input', async () => {
    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    expect(await screen.findByTestId('upload-file-input')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('/path/to/source/directory')).not.toBeInTheDocument();
  });

  it('E3: selecting files and uploading drives the Step 3 log and summary', async () => {
    const user = userEvent.setup();
    let captured: ((e: BatchIngestEvent) => void) | null = null;
    uploadBatchIngestMock.mockImplementation((_vaultId, _items, onEvent) => {
      captured = onEvent;
      return new Promise<void>(() => {}); // keep the stream open
    });

    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    const input = await screen.findByTestId('upload-file-input');

    await user.upload(input, [
      new File(['one'], 'one.md', { type: 'text/markdown' }),
      new File(['two'], 'two.pdf', { type: 'application/pdf' }),
    ]);

    await user.click(await screen.findByRole('button', { name: /upload selected \(2\)/i }));

    expect(uploadBatchIngestMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(captured).not.toBeNull());

    act(() => {
      captured!({
        event_type: 'progress',
        file_index: 0,
        total_files: 2,
        filename: 'one.md',
        stage: 'projection',
        status: 'completed',
        document_id: 'd1',
      });
    });
    expect(screen.getByText(/\[completed\] one\.md/)).toBeInTheDocument();

    act(() => {
      captured!({
        event_type: 'summary',
        documents_created: { new: 2, new_version: 0 },
        metadata_pending: 2,
        edges_created: {},
        edges_staged: {},
        edges_removed: 0,
        edges_dropped: 0,
        abstracts_generated: 2,
        abstracts_deferred: 0,
        error_count: 0,
        errors: [],
      });
    });
    expect(screen.getByText('Results Summary')).toBeInTheDocument();
    expect(screen.getByText(/2 \(2 new, 0 new version\)/)).toBeInTheDocument();
  });

  it('E4: a failed progress event and a SAGE summary with edges render in the reused UI', async () => {
    const user = userEvent.setup();
    let captured: ((e: BatchIngestEvent) => void) | null = null;
    uploadBatchIngestMock.mockImplementation((_vaultId, _items, onEvent) => {
      captured = onEvent;
      return new Promise<void>(() => {});
    });

    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    const input = await screen.findByTestId('upload-file-input');
    await user.upload(input, [new File(['bad'], 'bad.pdf', { type: 'application/pdf' })]);
    await user.click(await screen.findByRole('button', { name: /upload selected \(1\)/i }));
    await waitFor(() => expect(captured).not.toBeNull());

    act(() => {
      captured!({
        event_type: 'progress',
        file_index: 0,
        total_files: 1,
        filename: 'bad.pdf',
        stage: 'projection',
        status: 'failed',
        error: 'Unsupported PDF',
      });
    });
    expect(screen.getByText(/\[failed\] bad\.pdf/)).toBeInTheDocument();
    expect(screen.getByText(/Unsupported PDF/)).toBeInTheDocument();

    act(() => {
      captured!({
        event_type: 'summary',
        documents_created: { new: 0, new_version: 0 },
        metadata_pending: 0,
        edges_created: { supersedes: 1 },
        edges_staged: {},
        edges_removed: 0,
        edges_dropped: 0,
        abstracts_generated: 0,
        abstracts_deferred: 0,
        error_count: 1,
        errors: [{ filename: 'bad.pdf', error: 'Unsupported PDF' }],
      });
    });
    // The Tier-1 edges row renders the SAGE summary's edges_created map.
    expect(screen.getByText('1 supersedes')).toBeInTheDocument();
  });

  it('E6: edge warnings render next to the drop count with reason and endpoints', async () => {
    const user = userEvent.setup();
    let captured: ((e: BatchIngestEvent) => void) | null = null;
    uploadBatchIngestMock.mockImplementation((_vaultId, _items, onEvent) => {
      captured = onEvent;
      return new Promise<void>(() => {});
    });

    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    const input = await screen.findByTestId('upload-file-input');
    await user.upload(input, [new File(['one'], 'one.md', { type: 'text/markdown' })]);
    await user.click(await screen.findByRole('button', { name: /upload selected \(1\)/i }));
    await waitFor(() => expect(captured).not.toBeNull());

    act(() => {
      captured!({
        event_type: 'summary',
        documents_created: { new: 1, new_version: 0 },
        metadata_pending: 0,
        edges_created: {},
        edges_staged: {},
        edges_removed: 0,
        edges_dropped: 2,
        abstracts_generated: 1,
        abstracts_deferred: 0,
        error_count: 0,
        errors: [],
        edge_warnings: [
          {
            source: 'doc_v2',
            target: 'doc_v1',
            edge_type: 'supersedes',
            reason: 'supersede_target_not_transitionable',
            detail: "observed state 'completed' does not permit supersede",
          },
          {
            source: 'imports/new.md',
            target: 'imports/old.md',
            edge_type: 'supersedes',
            reason: 'ingestion_failed',
            detail: 'Target file failed ingestion: imports/old.md',
          },
        ],
      });
    });

    expect(screen.getByText(/\(2 dropped\)/)).toBeInTheDocument();
    expect(screen.getByText(/Edge warnings \(2\)/)).toBeInTheDocument();
    expect(screen.getByText(/supersede_target_not_transitionable/)).toBeInTheDocument();
    expect(screen.getByText(/doc_v2/)).toBeInTheDocument();
    expect(screen.getByText(/doc_v1/)).toBeInTheDocument();
    expect(screen.getByText(/ingestion_failed/)).toBeInTheDocument();
    expect(screen.getByText(/does not permit supersede/)).toBeInTheDocument();
  });

  it('E7: a summary without edge warnings renders no warnings block', async () => {
    const user = userEvent.setup();
    let captured: ((e: BatchIngestEvent) => void) | null = null;
    uploadBatchIngestMock.mockImplementation((_vaultId, _items, onEvent) => {
      captured = onEvent;
      return new Promise<void>(() => {});
    });

    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    const input = await screen.findByTestId('upload-file-input');
    await user.upload(input, [new File(['one'], 'one.md', { type: 'text/markdown' })]);
    await user.click(await screen.findByRole('button', { name: /upload selected \(1\)/i }));
    await waitFor(() => expect(captured).not.toBeNull());

    act(() => {
      captured!({
        event_type: 'summary',
        documents_created: { new: 1, new_version: 0 },
        metadata_pending: 0,
        edges_created: {},
        edges_staged: {},
        edges_removed: 0,
        edges_dropped: 0,
        abstracts_generated: 1,
        abstracts_deferred: 0,
        error_count: 0,
        errors: [],
      });
    });

    expect(screen.getByText('Results Summary')).toBeInTheDocument();
    expect(screen.queryByText(/Edge warnings/)).not.toBeInTheDocument();
  });

  it('E5: an unsupported file is flagged and excluded from the upload payload', async () => {
    const user = userEvent.setup();
    uploadBatchIngestMock.mockResolvedValue();

    render(<TestWrapper vaultId="example_vault" vault={mockVault} />);
    const input = await screen.findByTestId('upload-file-input');
    await user.upload(input, [
      new File(['ok'], 'good.md', { type: 'text/markdown' }),
      new File(['no'], 'notes.txt', { type: 'text/plain' }),
    ]);

    // Step 2 preview: the .txt is marked Unsupported; only one file is selectable.
    expect(await screen.findByText('Unsupported')).toBeInTheDocument();
    const uploadBtn = await screen.findByRole('button', { name: /upload selected \(1\)/i });
    await user.click(uploadBtn);

    expect(uploadBatchIngestMock).toHaveBeenCalledTimes(1);
    const items = uploadBatchIngestMock.mock.calls[0][1];
    expect(items).toHaveLength(1);
    expect(items[0].file.name).toBe('good.md');
    expect(items[0].source_type).toBe('markdown');
  });
});
