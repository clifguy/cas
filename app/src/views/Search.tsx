import { useState, useEffect } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router';
import type { VaultContext } from '../App';
import type { DiscoverHit, DiscoverRequest } from '../api/types';
import { discover } from '../api/discover';
import { BulkActionBar } from '../components/BulkActionBar';
import { BulkLifecycleDialog } from '../components/BulkLifecycleDialog';
import { BulkMetadataDialog } from '../components/BulkMetadataDialog';
import { formatDate } from '../utils/format';
import { describeConstraints, type Constraint } from '../utils/searchConstraints';

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
  const {
    query: urlQuery,
    mode: urlMode,
    docType: urlDocType,
    lifecycle: urlLifecycle,
    project: urlProject,
    pipelineStatus: urlPipelineStatus,
    excludeTerminal: urlExcludeTerminal,
    offset: urlOffset,
    sortBy: urlSortBy,
    sortOrder: urlSortOrder,
    isDrillDown,
  } = parseSearchUrl(searchParams);

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

  // --- Execute search whenever the URL changes ---
  const paramsKey = searchParams.toString();

  // The constraints named on screen are read from the same request the
  // effect below sends, so a filter cannot reach one without the other.
  const constraints = describeConstraints(buildSearchRequest(searchParams)?.filters);

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

    run(buildSearchRequest(searchParams));

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
    // Tags reach this view only through the URL -- no form control sets
    // them -- so a query string rebuilt from the form buffers alone
    // discards them with no way for the user to type them back. Carried
    // forward by name rather than by cloning the whole query string: the
    // other keys a drill-down URL holds either have a form control above
    // or should reset on a new search, and an unconditional clone would
    // pin a stale offset and sort onto every submit.
    const currentTags = searchParams.get('tags');
    if (currentTags) next.set('tags', currentTags);
    // `exclude_terminal_lifecycle` is deliberately not carried forward
    // beside it. It is a worklist affordance meaning "the open
    // population", not a filter the user chose, and a new search is the
    // user leaving that worklist -- so a submit is where it ends rather
    // than where it follows them into an unrelated query.
    setSearchParams(next);
  }

  function goToOffset(offset: number) {
    const next = new URLSearchParams(searchParams);
    next.set('offset', String(offset));
    setSearchParams(next);
  }

  // Filter keys and URL parameter names are the same spelling, so a
  // constraint named from the request is cleared by deleting its
  // parameter. The offset goes too: a page of the narrower result is not
  // a page of the wider one.
  function clearConstraint(key: string) {
    const next = new URLSearchParams(searchParams);
    next.delete(key);
    next.delete('offset');
    // A drill-down is recognised by its filters alone, so clearing its
    // last one leaves nothing to run and would blank the list instead of
    // widening it. Browse is the same catalog query with nothing applied.
    if (!next.has('mode') && buildSearchRequest(next) === null) {
      next.set('mode', 'browse');
    }
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
    const heading = drillDownHeading(
      urlPipelineStatus,
      urlLifecycle,
      urlDocType,
      urlExcludeTerminal,
    );
    const hasNext = urlOffset + PAGE_SIZE < totalAvailable;
    const hasPrev = urlOffset > 0;
    return (
      <div>
        <h1 style={{ margin: '0 0 4px', textTransform: 'capitalize' }}>{heading}</h1>
        <ActiveConstraints constraints={constraints} onClear={clearConstraint} />
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

      <ActiveConstraints constraints={constraints} onClear={clearConstraint} />

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

// -- Active constraints --

function ActiveConstraints({
  constraints,
  onClear,
}: {
  constraints: Constraint[];
  onClear: (key: string) => void;
}) {
  if (constraints.length === 0) return null;
  return (
    <ul aria-label="Active filters" style={constraintListStyle}>
      {constraints.map((c) => (
        <li key={c.key} data-constraint={c.key} style={constraintChipStyle}>
          <span>
            {c.label}
            {c.value && <>: <strong>{c.value}</strong></>}
          </span>
          <button
            type="button"
            aria-label={`Clear filter: ${c.label}`}
            onClick={() => onClear(c.key)}
            style={constraintClearStyle}
          >
            &times;
          </button>
        </li>
      ))}
    </ul>
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

// -- URL and request --

function parseSearchUrl(params: URLSearchParams) {
  const mode = params.get('mode') as Mode | null;
  const docType = params.get('doc_type') ?? '';
  const lifecycle = params.get('lifecycle_status') ?? '';
  const pipelineStatus = params.get('pipeline_status') ?? '';
  // A boolean carried in a URL, so only the affirmative spellings set it.
  // '0' and an absent param both mean "do not narrow", which keeps a
  // hand-edited link from silently hiding rows.
  const excludeTerminal = ['1', 'true'].includes(
    (params.get('exclude_terminal_lifecycle') ?? '').toLowerCase(),
  );
  return {
    query: params.get('q') ?? '',
    mode,
    docType,
    lifecycle,
    project: params.get('project') ?? '',
    pipelineStatus,
    excludeTerminal,
    tags: (params.get('tags') ?? '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    offset: Math.max(0, parseInt(params.get('offset') ?? '0', 10) || 0),
    sortBy: params.get('sort_by') as SortColumn | null,
    sortOrder: params.get('sort_order') as SortDir | null,
    // Drill-down: filter params present but no mode param (dashboard deep-link).
    isDrillDown: !mode && Boolean(pipelineStatus || lifecycle || docType || excludeTerminal),
  };
}

// The one place a request is built from the URL. The search effect sends
// what this returns and the constraint strip names its filters, so the two
// cannot disagree about what narrowed the result.
function buildSearchRequest(params: URLSearchParams): DiscoverRequest | null {
  const url = parseSearchUrl(params);
  let req: DiscoverRequest | null = null;
  if (url.isDrillDown) {
    // The same filters the other modes apply, plus the pipeline status
    // only a drill-down reads. A link carrying tags or a project narrows
    // here exactly as it would in browse.
    const filters: NonNullable<DiscoverRequest['filters']> = buildUrlFilters(url);
    if (url.pipelineStatus) filters.pipeline_status = url.pipelineStatus;
    req = {
      mode: 'catalog',
      filters,
      limit: PAGE_SIZE,
      offset: url.offset,
      response_mode: 'full',
    };
    if (url.sortBy && url.sortOrder) {
      req.sort_by = url.sortBy;
      req.sort_order = url.sortOrder;
    }
  } else if (url.mode === 'browse') {
    const filters = buildUrlFilters(url);
    req = {
      mode: 'catalog',
      filters: Object.keys(filters).length > 0 ? filters : undefined,
      limit: PAGE_SIZE,
      offset: url.offset,
      response_mode: 'full',
    };
    if (url.sortBy && url.sortOrder) {
      req.sort_by = url.sortBy;
      req.sort_order = url.sortOrder;
    }
  } else if (url.mode && url.query.trim()) {
    const filters = buildUrlFilters(url);
    req = {
      mode: url.mode === 'keyword' ? 'keyword' : 'semantic',
      query: url.query.trim(),
      filters: Object.keys(filters).length > 0 ? filters : undefined,
      use_hybrid: url.mode === 'hybrid',
      limit: 20,
      response_mode: 'full',
    };
  }
  return req;
}

// Filters for the browse and scored modes.
function buildUrlFilters(
  url: ReturnType<typeof parseSearchUrl>,
): NonNullable<DiscoverRequest['filters']> {
  const f: NonNullable<DiscoverRequest['filters']> = {};
  if (url.docType) f.doc_type = url.docType;
  if (url.lifecycle) f.lifecycle_status = url.lifecycle;
  if (url.project) f.project = url.project;
  if (url.tags.length) f.tags = url.tags;
  if (url.excludeTerminal) f.exclude_terminal_lifecycle = true;
  return f;
}

// -- Helpers --

function toggleSort(current: SortState | null, column: SortColumn): SortState {
  if (current?.column === column) {
    return { column, direction: current.direction === 'asc' ? 'desc' : 'asc' };
  }
  const defaultDir: SortDir = column === 'document_date' ? 'desc' : 'asc';
  return { column, direction: defaultDir };
}

function drillDownHeading(
  pipelineStatus: string,
  lifecycle: string,
  docType: string,
  excludeTerminal: boolean,
): string {
  if (pipelineStatus) {
    return ({
      abstraction_skipped: 'Deferred Abstracts',
      abstraction_interrupted: 'Interrupted Abstracts',
      failed: 'Failed Ingestions',
    } as Record<string, string>)[pipelineStatus] ?? pipelineStatus;
  }
  if (lifecycle) return `Lifecycle: ${lifecycle}`;
  if (docType) return `Doc Type: ${docType.replace(/_/g, ' ')}`;
  // The exclusion can stand alone as a drill-down, and every other
  // branch above names a value. This one names a population, so the
  // heading has to as well -- the alternative is an empty h1 over a
  // populated table.
  if (excludeTerminal) return 'Open Documents';
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
const constraintListStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 6, listStyle: 'none', margin: '0 0 12px', padding: 0 };
const constraintChipStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 4, padding: '2px 4px 2px 10px', border: '1px solid #bbdefb', borderRadius: 12, background: '#e3f2fd', color: '#0d47a1', fontSize: 12 };
const constraintClearStyle: React.CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', color: '#0d47a1', fontSize: 14, lineHeight: 1, padding: '0 4px' };
const sortBtnStyle: React.CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: '#666', fontWeight: 600, padding: 0 };
