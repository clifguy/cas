import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The views hand the bulk lifecycle dialog the doc_types of the selected rows,
 * which decide the actions it offers. The dialog is stubbed to record its
 * props: an empty list would offer every action in the vault, and a list read
 * from all visible rows rather than the selected ones would narrow it wrongly,
 * so each test selects a subset of rows whose doc_types differ from the rest.
 */

vi.mock('../../components/BulkLifecycleDialog', () => ({
  BulkLifecycleDialog: vi.fn(() => null),
}));
vi.mock('../../api/discover', () => ({ discover: vi.fn() }));
vi.mock('../../api/review', () => ({
  listPendingMetadata: vi.fn(),
  listStagingEdges: vi.fn(),
  confirmStagingEdge: vi.fn(),
  dismissStagingEdge: vi.fn(),
}));

import Search from '../Search';
import Review from '../Review';
import { BulkLifecycleDialog } from '../../components/BulkLifecycleDialog';
import { discover } from '../../api/discover';
import { listPendingMetadata, listStagingEdges } from '../../api/review';
import type { VaultContext } from '../../App';
import type { Document, DiscoverHit, VaultSummary } from '../../api/types';

const vault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: null,
  document_count: 0,
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function makeDocument(id: string, doc_type: string | null): Document {
  return {
    id,
    title: `Doc ${id}`,
    lifecycle_status: 'active',
    source_type: 'markdown',
    source_path: `${id}.md`,
    version_label: null,
    project: null,
    tags: [],
    authority_scope: null,
    doc_type,
    source_content_hash: 'sha',
    stored_content_hash: null,
    adapter_version: '0',
    created_by: 'system',
    created_at: '2026-05-21',
    last_modified_by: 'system',
    updated_at: '2026-05-21',
    projected_at: null,
    indexed_at: null,
    source_modified_at: null,
    document_date: null,
    semantic_abstract: null,
    pipeline_status: 'projection_complete',
    pipeline_error: null,
    tier3_metadata: null,
    relocated_from: null,
    relocated_to: null,
  };
}

function renderAt(path: string, element: React.ReactNode) {
  const ctx: VaultContext = { vaultId: 'test_vault', vault, vaults: [vault] };
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Outlet context={ctx} />}>
          <Route path="search" element={element} />
          <Route path="review" element={element} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function lastDocTypes(): (string | null)[] {
  const calls = vi.mocked(BulkLifecycleDialog).mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1][0].selectedDocTypes;
}

// A, B and C carry distinct doc_types; B is left unselected, so its type must
// not appear, and C carries none.
const docs = [makeDocument('A', 'ticket'), makeDocument('B', 'adr'), makeDocument('C', null)];

beforeEach(() => {
  vi.mocked(BulkLifecycleDialog).mockClear();
  vi.mocked(discover).mockReset();
  vi.mocked(listPendingMetadata).mockReset();
  vi.mocked(listStagingEdges).mockReset();
});

describe('bulk lifecycle dialog receives the selection doc_types', () => {
  it('from Search', async () => {
    const results: DiscoverHit[] = docs.map((document) => ({
      document,
      chunk_content: null,
      heading_path: null,
      relevance_score: null,
    }));
    vi.mocked(discover).mockResolvedValue({
      mode: 'catalog',
      results,
      total_available: results.length,
    });
    const user = userEvent.setup();
    renderAt('/search?lifecycle_status=active', <Search />);

    await user.click(await screen.findByLabelText('Select Doc A'));
    await user.click(screen.getByLabelText('Select Doc C'));
    await user.click(screen.getByRole('button', { name: /set lifecycle/i }));

    expect(lastDocTypes()).toEqual(['ticket', null]);
  });

  it('from Review', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue({
      items: docs.map((document) => ({
        document,
        extracted_fields: { title: { value: document.title, source: 'filename' } },
      })),
      total_available: docs.length,
      limit: 100,
      offset: 0,
      response_mode: 'full',
    });
    vi.mocked(listStagingEdges).mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt('/review', <Review />);

    await user.click(await screen.findByLabelText('Select Doc A'));
    await user.click(screen.getByLabelText('Select Doc C'));
    await user.click(screen.getByRole('button', { name: /set lifecycle/i }));

    expect(lastDocTypes()).toEqual(['ticket', null]);
  });
});
