import { useState, useEffect } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { DiscoverHit, DiscoverRequest } from '../api/types';
import { discover } from '../api/discover';
import { BulkActionBar } from '../components/BulkActionBar';
import { BulkLifecycleDialog } from '../components/BulkLifecycleDialog';
import { BulkMetadataDialog } from '../components/BulkMetadataDialog';
import { formatDate } from '../utils/format';

const PAGE_SIZE = 50;

type Mode = 'hybrid' | 'semantic' | 'keyword' | 'browse';
type SortColumn = 'title' | 'doc_type' | 'document_date' | 'lifecycle_status';
type SortDir = 'asc' | 'desc';

interface SortState {
  column: SortColumn;
  direction: SortDir;
}

export default function Search() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [searchParams, setSearchParams] = useSearchParams();

  // --- URL-derived state ---
  const urlQuery = searchParams.get('q') ?? '';
  const urlMode = searchParams.get('mode') as Mode | null;
  const urlDocType = searchParams.get('doc_type') ?? '';
  const urlLifecycle = searchParams.get('lifecycle_status') ?? '';
  const urlProject = searchParams.get('project') ?? '';
  const urlPipelineStatus = searchParams.get('pipeline_status') ?? '';
  const urlTags = (searchParams.get('tags') ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const urlOffset = Math.max(0, parseInt(searchParams.get('offset') ?? '0', 10) || 0);
  const urlSortBy = searchParams.get('sort_by') as SortColumn | null;
  const urlSortOrder = searchParams.get('sort_order') as SortDir | null;

  // Drill-down: filter params present but no mode param (dashboard deep-link).
  const isDrillDown = !urlMode && Boolean(urlPipelineStatus || urlLifecycle || urlDocType);

  // --- Form buffer state (syncs from URL so back/forward restores inputs) ---
  const [queryInput, setQueryInput] = useState(urlQuery);
  const [modeInput, setModeInput] = useState<Mode>(urlMode ?? 'hybrid');
  const [docTypeFilter, setDocTypeFilter] = useState(urlDocType);
  const [lifecycleFilter, setLifecycleFilter] = useState(urlLifecycle);
  const [projectFilter, setProjectFilter] = useState(urlProject);
  const [showFilters, setShowFilters] = useState(false);

  // Re-seed the form buffers from the URL so back/forward restores inputs.
  // Adjusting during render — guarded by the previous URL slice — avoids a
  // setState-in-effect.
  const formSyncKey = JSON.stringify([urlQuery, urlMode ?? 'hybrid', urlDocType, urlLifecycle, urlProject]);
  const [syncedFormKey, setSyncedFormKey] = useState(formSyncKey);
  if (formSyncKey !== syncedFormKey) {
    setSyncedFormKey(formSyncKey);
    setQueryInput(urlQuery);
    setModeInput(urlMode ?? 'hybrid');
    setDocTypeFilter(urlDocType);
    setLifecycleFilter(urlLifecycle);
    setProjectFilter(urlProject);
  }

  // --- Result state ---
  const [results, setResults] = useState<DiscoverHit[]>([]);
  const [totalAvailable, setTotalAvailable] = useState(0);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');

  // --- Selection state (bulk actions) ---
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [lifecycleDialogOpen, setLifecycleDialogOpen] = useState(false);
  const [metadataDialogOpen, setMetadataDialogOpen] = useState(false);

  // Filters derived from the URL, shared by the search effect and handlers.
  function buildUrlFilters(): NonNullable<DiscoverRequest['filters']> {
    const f: NonNullable<DiscoverRequest['filters']> = {};
    if (urlDocType) f.doc_type = urlDocType;
    if (urlLifecycle) f.lifecycle_status = urlLifecycle;
    if (urlProject) f.project = urlProject;
    if (urlTags.length) f.tags = urlTags;
    return f;
  }

  // --- Execute search whenever the URL changes ---
  const paramsKey = searchParams.toString();

  // Selection is bound to the current filter result set; clear it whenever the
  // URL (hence the rows under the user's fingers) changes. Resetting during
  // render — guarded by the previous key — avoids a setState-in-effect.
  const selectionResetKey = JSON.stringify([vaultId ?? '', paramsKey]);
  const [syncedSelectionKey, setSyncedSelectionKey] = useState(selectionResetKey);
  if (selectionResetKey !== syncedSelectionKey) {
    setSyncedSelectionKey(selectionResetKey);
    setSelectedIds(new Set());
  }

  useEffect(() => {
    if (!vaultId) return;
    let cancelled = false;

    async function run(req: DiscoverRequest | null) {
      setError('');
      if (!req) {
        // No actionable URL state — clear to the empty landing view.
        setResults([]);
        setTotalAvailable(0);
        setHasSearched(false);
        setSearching(false);
        return;
      }
      setSearching(true);
      try {
        const resp = await discover(vaultId, req);
        if (cancelled) return;
        setResults(resp.results);
        setTotalAvailable(resp.total_available);
        setHasSearched(true);
      } catch (err) {
        if (cancelled) return;
        // Capture the failure so it renders as a visible error rather than an
        // empty result set indistinguishable from a genuine no-match.
        setError(err instanceof Error ? err.message : 'Search failed');
        setResults([]);
        setTotalAvailable(0);
        setHasSearched(true);
      }
      if (!cancelled) setSearching(false);
    }

    let req: DiscoverRequest | null = null;
    if (isDrillDown) {
      const filters: Record<string, string> = {};
      if (urlPipelineStatus) filters.pipeline_status = urlPipelineStatus;
      if (urlLifecycle) filters.lifecycle_status = urlLifecycle;
      if (urlDocType) filters.doc_type = urlDocType;
      req = {
        mode: 'catalog',
        filters,
        limit: PAGE_SIZE,
        offset: urlOffset,
      };
      if (urlSortBy && urlSortOrder) {
        req.sort_by = urlSortBy;
        req.sort_order = urlSortOrder;
      }
    } else if (urlMode === 'browse') {
      const filters = buildUrlFilters();
      req = {
        mode: 'catalog',
        filters: Object.keys(filters).length > 0 ? filters : undefined,
        limit: PAGE_SIZE,
        offset: urlOffset,
      };
      if (urlSortBy && urlSortOrder) {
        req.sort_by = urlSortBy;
        req.sort_order = urlSortOrder;
      }
    } else if (urlMode && urlQuery.trim()) {
      const filters = buildUrlFilters();
      req = {
        mode: urlMode === 'keyword' ? 'keyword' : 'semantic',
        query: urlQuery.trim(),
        filters: Object.keys(filters).length > 0 ? filters : undefined,
        use_hybrid: urlMode === 'hybrid',
        limit: 20,
      };
    }
    run(req);

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey, vaultId]);

  function handleSearch(e?: React.FormEvent) {
    if (e) e.preventDefault();
    // Semantic/keyword/hybrid need a query. Browse can run with no query.
    if (modeInput !== 'browse' && !queryInput.trim()) return;

    const next = new URLSearchParams();
    next.set('mode', modeInput);
    if (queryInput.trim()) next.set('q', queryInput.trim());
    if (docTypeFilter) next.set('doc_type', docTypeFilter);
    if (lifecycleFilter) next.set('lifecycle_status', lifecycleFilter);
    if (projectFilter) next.set('project', projectFilter);
    setSearchParams(next);
  }

  function goToOffset(offset: number) {
    const next = new URLSearchParams(searchParams);
    next.set('offset', String(offset));
    setSearchParams(next);
  }

  function handleSort(column: SortColumn) {
    const current = urlSortBy && urlSortOrder
      ? { column: urlSortBy, direction: urlSortOrder }
      : null;
    const newSort = toggleSort(current, column);
    const next = new URLSearchParams(searchParams);
    next.set('sort_by', newSort.column);
    next.set('sort_order', newSort.direction);
    next.set('offset', '0');
    setSearchParams(next);
  }

  function toggleRow(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAllVisible() {
    setSelectedIds((prev) => {
      const visibleIds = results.map((h) => h.document.id);
      const allSelected = visibleIds.every((id) => prev.has(id)) && visibleIds.length > 0;
      if (allSelected) {
        const next = new Set(prev);
        for (const id of visibleIds) next.delete(id);
        return next;
      }
      const next = new Set(prev);
      for (const id of visibleIds) next.add(id);
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

  // Shared error affordance, rendered in both the drill-down and main branches.
  const errorBanner = error ? (
    <div style={{ color: '#c62828', marginTop: 32, textAlign: 'center' }}>Error: {error}</div>
  ) : null;

  const currentSort: SortState | null = urlSortBy && urlSortOrder
    ? { column: urlSortBy, direction: urlSortOrder }
    : null;

  // --- Dashboard drill-down view ---
  if (isDrillDown) {
    const heading = drillDownHeading(urlPipelineStatus, urlLifecycle, urlDocType);
    const hasNext = urlOffset + PAGE_SIZE < totalAvailable;
    const hasPrev = urlOffset > 0;
    return (
      <div>
        <h1 style={{ margin: '0 0 4px', textTransform: 'capitalize' }}>{heading}</h1>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: '#666' }}>
          {totalAvailable} result{totalAvailable !== 1 ? 's' : ''}
        </p>
        {error ? (
          errorBanner
        ) : results.length === 0 && !searching ? (
          <div style={{ color: '#999' }}>No documents match this filter.</div>
        ) : (
          <>
            {selectedIds.size > 0 && (
              <BulkActionBar
                count={selectedIds.size}
                onSetLifecycle={() => setLifecycleDialogOpen(true)}
                onUpdateMetadata={() => setMetadataDialogOpen(true)}
                onClear={() => setSelectedIds(new Set())}
              />
            )}
            <CatalogTable
              hits={results}
              sort={currentSort}
              onSort={handleSort}
              selectedIds={selectedIds}
              onToggleRow={toggleRow}
              onToggleAll={toggleAllVisible}
            />
            {(hasPrev || hasNext) && (
              <div style={paginationStyle}>
                {hasPrev && (
                  <button style={pageBtnStyle} onClick={() => goToOffset(urlOffset - PAGE_SIZE)}>
                    Previous
                  </button>
                )}
                <span style={{ fontSize: 12, color: '#666' }}>
                  {urlOffset + 1}&ndash;{Math.min(urlOffset + PAGE_SIZE, totalAvailable)} of {totalAvailable}
                </span>
                {hasNext && (
                  <button style={pageBtnStyle} onClick={() => goToOffset(urlOffset + PAGE_SIZE)}>
                    Next
                  </button>
                )}
              </div>
            )}
          </>
        )}
        <div style={{ marginTop: 16 }}>
          <Link to="/dashboard" style={{ fontSize: 12, color: '#666' }}>&larr; Back to dashboard</Link>
        </div>

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

  const isBrowse = modeInput === 'browse';
  const showBrowsePagination = urlMode === 'browse' && hasSearched && totalAvailable > PAGE_SIZE;

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Search</h1>

      <form onSubmit={handleSearch} style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          {!isBrowse && (
            <input
              type="text"
              value={queryInput}
              onChange={e => setQueryInput(e.target.value)}
              placeholder="Search documents..."
              style={{ flex: 1, padding: '8px 12px', fontSize: 14 }}
            />
          )}
          <select
            value={modeInput}
            onChange={e => setModeInput(e.target.value as Mode)}
            style={{ padding: '8px 12px' }}
          >
            <option value="hybrid">Hybrid</option>
            <option value="semantic">Semantic</option>
            <option value="keyword">Keyword</option>
            <option value="browse">Browse</option>
          </select>
          <button type="submit" style={btnStyle} disabled={searching}>
            {searching ? (isBrowse ? 'Loading...' : 'Searching...') : (isBrowse ? 'Browse' : 'Search')}
          </button>
        </div>

        <button
          type="button"
          onClick={() => setShowFilters(!showFilters)}
          style={{ background: 'none', border: 'none', color: '#1565c0', cursor: 'pointer', fontSize: 12, padding: 0 }}
        >
          {showFilters ? 'Hide filters' : 'Show filters'}
        </button>

        {showFilters && (
          <div style={{ marginTop: 8, padding: 12, background: '#f9f9f9', border: '1px solid #eee', borderRadius: 4 }}>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
              <div>
                <label style={filterLabelStyle}>Document type</label>
                <select
                  value={docTypeFilter}
                  onChange={e => setDocTypeFilter(e.target.value)}
                  style={{ padding: '4px 8px', fontSize: 12 }}
                >
                  <option value="">All</option>
                  {vault.doc_types.map(dt => (
                    <option key={dt.value} value={dt.value}>{dt.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label style={filterLabelStyle}>Lifecycle state</label>
                <select
                  value={lifecycleFilter}
                  onChange={e => setLifecycleFilter(e.target.value)}
                  style={{ padding: '4px 8px', fontSize: 12 }}
                >
                  <option value="">All</option>
                  {vault.lifecycle_states.map(ls => (
                    <option key={ls.value} value={ls.value}>{ls.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label style={filterLabelStyle}>Project</label>
                <select
                  value={projectFilter}
                  onChange={e => setProjectFilter(e.target.value)}
                  style={{ padding: '4px 8px', fontSize: 12 }}
                >
                  <option value="">All</option>
                  {vault.projects.map(p => (
                    <option key={p} value={p}>{p.replace(/_/g, ' ')}</option>
                  ))}
                </select>
              </div>

              {hasSearched && (
                <button
                  type="button"
                  onClick={() => handleSearch()}
                  style={{ ...btnStyle, padding: '4px 14px', fontSize: 12 }}
                >
                  Update
                </button>
              )}
            </div>
          </div>
        )}
      </form>

      {!hasSearched && !searching && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>
          {isBrowse ? 'Click Browse to list documents.' : 'Enter a query to search.'}
        </div>
      )}

      {errorBanner}

      {hasSearched && results.length === 0 && !error && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>No results found.</div>
      )}

      {hasSearched && results.length > 0 && (
        <p style={{ margin: '0 0 8px', fontSize: 13, color: '#666' }}>
          {totalAvailable} result{totalAvailable !== 1 ? 's' : ''}
        </p>
      )}

      {hasSearched && results.length > 0 && selectedIds.size > 0 && (
        <BulkActionBar
          count={selectedIds.size}
          onSetLifecycle={() => setLifecycleDialogOpen(true)}
          onUpdateMetadata={() => setMetadataDialogOpen(true)}
          onClear={() => setSelectedIds(new Set())}
        />
      )}

      {/* Browse mode: sortable table */}
      {urlMode === 'browse' && hasSearched && results.length > 0 && (
        <CatalogTable
          hits={results}
          sort={currentSort}
          onSort={handleSort}
          selectedIds={selectedIds}
          onToggleRow={toggleRow}
          onToggleAll={toggleAllVisible}
        />
      )}

      {/* Semantic/keyword/hybrid: card layout */}
      {urlMode && urlMode !== 'browse' && results.map((hit) => (
        <div key={hit.document.id} style={{ borderBottom: '1px solid #eee', padding: '16px 0' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
            <input
              type="checkbox"
              data-testid={`bulk-row-checkbox-${hit.document.id}`}
              aria-label={`Select ${hit.document.title}`}
              checked={selectedIds.has(hit.document.id)}
              onChange={() => toggleRow(hit.document.id)}
            />
            <Link to={`/documents/${hit.document.id}`} style={{ fontSize: 15, fontWeight: 600, color: '#1565c0', textDecoration: 'none' }}>
              {sourceFilename(hit.document.source_path) ?? hit.document.title}
            </Link>
            {hit.document.doc_type && (
              <span style={badgeStyle}>{hit.document.doc_type.replace(/_/g, ' ')}</span>
            )}
            <span style={{ ...badgeStyle, background: '#e8f5e9', color: '#2e7d32' }}>
              {hit.document.lifecycle_status}
            </span>
          </div>

          {hit.relevance_score !== null && (
            <div style={{ fontSize: 11, color: '#999', marginBottom: 4 }}>
              Relevance: {(hit.relevance_score * 100).toFixed(0)}%
              {hit.heading_path && <span> &middot; {hit.heading_path}</span>}
            </div>
          )}

          {hit.chunk_content && (
            <div style={{ fontSize: 13, color: '#444', marginBottom: 4, lineHeight: 1.5 }}>
              {truncateContent(hit.chunk_content)}
            </div>
          )}
        </div>
      ))}

      {showBrowsePagination && (
        <div style={paginationStyle}>
          {urlOffset > 0 && (
            <button style={pageBtnStyle} onClick={() => goToOffset(urlOffset - PAGE_SIZE)}>
              Previous
            </button>
          )}
          <span style={{ fontSize: 12, color: '#666' }}>
            {urlOffset + 1}&ndash;{Math.min(urlOffset + PAGE_SIZE, totalAvailable)} of {totalAvailable}
          </span>
          {urlOffset + PAGE_SIZE < totalAvailable && (
            <button style={pageBtnStyle} onClick={() => goToOffset(urlOffset + PAGE_SIZE)}>
              Next
            </button>
          )}
        </div>
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

// -- Sortable catalog table --

function CatalogTable({
  hits,
  sort,
  onSort,
  selectedIds,
  onToggleRow,
  onToggleAll,
}: {
  hits: DiscoverHit[];
  sort: SortState | null;
  onSort: (column: SortColumn) => void;
  selectedIds: Set<string>;
  onToggleRow: (id: string) => void;
  onToggleAll: () => void;
}) {
  const visibleIds = hits.map((h) => h.document.id);
  const selectedVisible = visibleIds.filter((id) => selectedIds.has(id)).length;
  const allChecked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  const someChecked = selectedVisible > 0 && selectedVisible < visibleIds.length;

  return (
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
              onChange={onToggleAll}
            />
          </th>
          <SortableHeader label="Title" column="title" sort={sort} onSort={onSort} />
          <SortableHeader label="Type" column="doc_type" sort={sort} onSort={onSort} />
          <SortableHeader label="Date" column="document_date" sort={sort} onSort={onSort} />
          <SortableHeader label="Status" column="lifecycle_status" sort={sort} onSort={onSort} />
        </tr>
      </thead>
      <tbody>
        {hits.map(hit => (
          <tr key={hit.document.id}>
            <td style={tdStyle}>
              <input
                type="checkbox"
                data-testid={`bulk-row-checkbox-${hit.document.id}`}
                aria-label={`Select ${hit.document.title}`}
                checked={selectedIds.has(hit.document.id)}
                onChange={() => onToggleRow(hit.document.id)}
              />
            </td>
            <td style={tdStyle}>
              <Link to={`/documents/${hit.document.id}`} style={{ color: '#1565c0', textDecoration: 'none' }}>
                {sourceFilename(hit.document.source_path) ?? hit.document.title}
              </Link>
            </td>
            <td style={tdStyle}>{hit.document.doc_type?.replace(/_/g, ' ') ?? '-'}</td>
            <td style={tdStyle}>{formatDate(hit.document.document_date)}</td>
            <td style={tdStyle}>{hit.document.lifecycle_status}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SortableHeader({
  label,
  column,
  sort,
  onSort,
}: {
  label: string;
  column: SortColumn;
  sort: SortState | null;
  onSort: (column: SortColumn) => void;
}) {
  const isActive = sort?.column === column;
  const arrow = isActive ? (sort.direction === 'asc' ? ' \u25B2' : ' \u25BC') : '';
  return (
    <th style={thStyle}>
      <button
        onClick={() => onSort(column)}
        style={sortBtnStyle}
      >
        {label}{arrow}
      </button>
    </th>
  );
}

// -- Helpers --

function toggleSort(current: SortState | null, column: SortColumn): SortState {
  if (current?.column === column) {
    return { column, direction: current.direction === 'asc' ? 'desc' : 'asc' };
  }
  const defaultDir: SortDir = column === 'document_date' ? 'desc' : 'asc';
  return { column, direction: defaultDir };
}

function drillDownHeading(pipelineStatus: string, lifecycle: string, docType: string): string {
  if (pipelineStatus) {
    return ({
      abstraction_skipped: 'Deferred Abstracts',
      failed: 'Failed Ingestions',
    } as Record<string, string>)[pipelineStatus] ?? pipelineStatus;
  }
  if (lifecycle) return `Lifecycle: ${lifecycle}`;
  if (docType) return `Doc Type: ${docType.replace(/_/g, ' ')}`;
  return '';
}

function truncateContent(text: string): string {
  const words = text.split(/\s+/);
  if (words.length <= 200) return text;
  const head = words.slice(0, 100).join(' ');
  const tail = words.slice(-100).join(' ');
  return `${head} \u2026 ${tail}`;
}

function sourceFilename(sourcePath: string | null | undefined): string | null {
  if (!sourcePath) return null;
  const filename = sourcePath.includes('/') ? sourcePath.split('/').pop()! : sourcePath;
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.substring(0, dot) : filename;
}

// -- Styles --

const btnStyle: React.CSSProperties = { padding: '8px 20px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 14 };
const filterLabelStyle: React.CSSProperties = { display: 'block', fontSize: 11, color: '#666', marginBottom: 4, fontWeight: 500 };
const badgeStyle: React.CSSProperties = { padding: '1px 8px', borderRadius: 3, fontSize: 10, fontWeight: 600, background: '#e3f2fd', color: '#1565c0', textTransform: 'capitalize' };
const thStyle: React.CSSProperties = { textAlign: 'left', padding: '6px 10px', borderBottom: '2px solid #ddd', fontSize: 12, color: '#666' };
const tdStyle: React.CSSProperties = { padding: '6px 10px', borderBottom: '1px solid #eee', fontSize: 13 };
const paginationStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 12, marginTop: 16, justifyContent: 'center' };
const pageBtnStyle: React.CSSProperties = { padding: '4px 14px', border: '1px solid #ccc', borderRadius: 4, background: '#fff', cursor: 'pointer', fontSize: 12 };
const sortBtnStyle: React.CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: '#666', fontWeight: 600, padding: 0 };
