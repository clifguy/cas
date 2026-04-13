import { useState } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { DiscoverHit, DiscoverRequest } from '../api/types';
import { discover } from '../api/discover';

const PAGE_SIZE = 50;

type SortColumn = 'title' | 'document_date' | 'lifecycle_status';
type SortDir = 'asc' | 'desc';

interface SortState {
  column: SortColumn;
  direction: SortDir;
}

export default function Search() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'hybrid' | 'semantic' | 'keyword' | 'browse'>('hybrid');
  const [showFilters, setShowFilters] = useState(false);
  const [docTypeFilter, setDocTypeFilter] = useState('');
  const [lifecycleFilter, setLifecycleFilter] = useState('');
  const [projectFilter, setProjectFilter] = useState('');
  const [results, setResults] = useState<DiscoverHit[]>([]);
  const [totalAvailable, setTotalAvailable] = useState(0);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);

  // Drill-down state
  const [filterResults, setFilterResults] = useState<DiscoverHit[] | null>(null);
  const [filterTotal, setFilterTotal] = useState(0);
  const [filterOffset, setFilterOffset] = useState(0);
  const [filterHeading, setFilterHeading] = useState('');
  const [filterSort, setFilterSort] = useState<SortState | null>(null);

  // Browse state
  const [browseOffset, setBrowseOffset] = useState(0);
  const [browseSort, setBrowseSort] = useState<SortState | null>(null);

  if (!vault) return <div>Vault not found.</div>;

  // Check for dashboard drill-down filter params
  const filterDefs: Record<string, { filterKey: string; heading: (v: string) => string }> = {
    pipeline_status: { filterKey: 'pipeline_status', heading: v => ({ abstraction_skipped: 'Deferred Abstracts', failed: 'Failed Ingestions' }[v] ?? v) },
    lifecycle_status: { filterKey: 'lifecycle_status', heading: v => `Lifecycle: ${v}` },
    doc_type: { filterKey: 'doc_type', heading: v => `Doc Type: ${v.replace(/_/g, ' ')}` },
  };

  // On first render with filter params, execute filtered catalog discover
  const filterParam = Object.keys(filterDefs).find(p => searchParams.has(p));
  if (filterParam && !filterResults && !searching) {
    const value = searchParams.get(filterParam)!;
    const def = filterDefs[filterParam];
    setSearching(true);
    setFilterHeading(def.heading(value));
    setFilterOffset(0);
    discover(vaultId, {
      mode: 'catalog',
      filters: { [def.filterKey]: value },
      limit: PAGE_SIZE,
      offset: 0,
    })
      .then(resp => {
        setFilterResults(resp.results);
        setFilterTotal(resp.total_available);
        setSearching(false);
      })
      .catch(() => {
        setFilterResults([]);
        setFilterTotal(0);
        setSearching(false);
      });
  }

  async function loadFilterPage(offset: number, sort?: SortState | null) {
    if (!filterParam) return;
    const value = searchParams.get(filterParam)!;
    const def = filterDefs[filterParam];
    const activeSort = sort !== undefined ? sort : filterSort;
    setSearching(true);
    try {
      const req: DiscoverRequest = {
        mode: 'catalog',
        filters: { [def.filterKey]: value },
        limit: PAGE_SIZE,
        offset,
      };
      if (activeSort) {
        req.sort_by = activeSort.column;
        req.sort_order = activeSort.direction;
      }
      const resp = await discover(vaultId, req);
      setFilterResults(resp.results);
      setFilterTotal(resp.total_available);
      setFilterOffset(offset);
    } catch {
      // Keep existing results on error
    }
    setSearching(false);
  }

  function handleFilterSort(column: SortColumn) {
    const newSort = toggleSort(filterSort, column);
    setFilterSort(newSort);
    loadFilterPage(0, newSort);
  }

  async function handleSearch(e?: React.FormEvent) {
    if (e) e.preventDefault();

    if (mode === 'browse') {
      setSearching(true);
      try {
        const filters = buildFilters();
        const req: DiscoverRequest = {
          mode: 'catalog',
          filters: Object.keys(filters).length > 0 ? filters : undefined,
          limit: PAGE_SIZE,
          offset: 0,
        };
        if (browseSort) {
          req.sort_by = browseSort.column;
          req.sort_order = browseSort.direction;
        }
        const resp = await discover(vaultId, req);
        setResults(resp.results);
        setTotalAvailable(resp.total_available);
        setBrowseOffset(0);
        setHasSearched(true);
      } catch {
        setResults([]);
        setTotalAvailable(0);
        setHasSearched(true);
      }
      setSearching(false);
      return;
    }

    // Semantic/keyword modes require a query
    if (!query.trim()) return;
    setSearching(true);
    try {
      const filters = buildFilters();
      const useHybrid = mode === 'hybrid';
      const resp = await discover(vaultId, {
        mode: mode === 'keyword' ? 'keyword' : 'semantic',
        query: query.trim(),
        filters: Object.keys(filters).length > 0 ? filters : undefined,
        use_hybrid: useHybrid,
        limit: 20,
      });
      setResults(resp.results);
      setTotalAvailable(resp.total_available);
      setHasSearched(true);
    } catch {
      setResults([]);
      setTotalAvailable(0);
      setHasSearched(true);
    }
    setSearching(false);
  }

  function buildFilters(): Record<string, string> {
    const filters: Record<string, string> = {};
    if (docTypeFilter) filters.doc_type = docTypeFilter;
    if (lifecycleFilter) filters.lifecycle_status = lifecycleFilter;
    if (projectFilter) filters.project = projectFilter;
    return filters;
  }

  async function loadBrowsePage(offset: number, sort?: SortState | null) {
    const activeSort = sort !== undefined ? sort : browseSort;
    setSearching(true);
    try {
      const filters = buildFilters();
      const req: DiscoverRequest = {
        mode: 'catalog',
        filters: Object.keys(filters).length > 0 ? filters : undefined,
        limit: PAGE_SIZE,
        offset,
      };
      if (activeSort) {
        req.sort_by = activeSort.column;
        req.sort_order = activeSort.direction;
      }
      const resp = await discover(vaultId, req);
      setResults(resp.results);
      setTotalAvailable(resp.total_available);
      setBrowseOffset(offset);
    } catch {
      // Keep existing results on error
    }
    setSearching(false);
  }

  function handleBrowseSort(column: SortColumn) {
    const newSort = toggleSort(browseSort, column);
    setBrowseSort(newSort);
    loadBrowsePage(0, newSort);
  }

  // Dashboard drill-down view
  if (filterResults !== null) {
    const hasNext = filterOffset + PAGE_SIZE < filterTotal;
    const hasPrev = filterOffset > 0;
    return (
      <div>
        <h1 style={{ margin: '0 0 4px', textTransform: 'capitalize' }}>{filterHeading}</h1>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: '#666' }}>
          {filterTotal} result{filterTotal !== 1 ? 's' : ''}
        </p>
        {filterResults.length === 0 ? (
          <div style={{ color: '#999' }}>No documents match this filter.</div>
        ) : (
          <>
            <CatalogTable
              hits={filterResults}
              sort={filterSort}
              onSort={handleFilterSort}
            />
            {(hasPrev || hasNext) && (
              <div style={paginationStyle}>
                {hasPrev && (
                  <button style={pageBtnStyle} onClick={() => loadFilterPage(filterOffset - PAGE_SIZE)}>
                    Previous
                  </button>
                )}
                <span style={{ fontSize: 12, color: '#666' }}>
                  {filterOffset + 1}&ndash;{Math.min(filterOffset + PAGE_SIZE, filterTotal)} of {filterTotal}
                </span>
                {hasNext && (
                  <button style={pageBtnStyle} onClick={() => loadFilterPage(filterOffset + PAGE_SIZE)}>
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
      </div>
    );
  }

  const isBrowse = mode === 'browse';
  const showBrowsePagination = isBrowse && hasSearched && totalAvailable > PAGE_SIZE;

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Search</h1>

      <form onSubmit={handleSearch} style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          {!isBrowse && (
            <input
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search documents..."
              style={{ flex: 1, padding: '8px 12px', fontSize: 14 }}
            />
          )}
          <select
            value={mode}
            onChange={e => setMode(e.target.value as 'hybrid' | 'semantic' | 'keyword' | 'browse')}
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

      {hasSearched && results.length === 0 && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>No results found.</div>
      )}

      {hasSearched && results.length > 0 && (
        <p style={{ margin: '0 0 8px', fontSize: 13, color: '#666' }}>
          {totalAvailable} result{totalAvailable !== 1 ? 's' : ''}
        </p>
      )}

      {/* Browse mode: sortable table */}
      {isBrowse && hasSearched && results.length > 0 && (
        <CatalogTable
          hits={results}
          sort={browseSort}
          onSort={handleBrowseSort}
        />
      )}

      {/* Semantic/keyword mode: card layout */}
      {!isBrowse && results.map((hit) => (
        <div key={hit.document.id} style={{ borderBottom: '1px solid #eee', padding: '16px 0' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
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
          {browseOffset > 0 && (
            <button style={pageBtnStyle} onClick={() => loadBrowsePage(browseOffset - PAGE_SIZE)}>
              Previous
            </button>
          )}
          <span style={{ fontSize: 12, color: '#666' }}>
            {browseOffset + 1}&ndash;{Math.min(browseOffset + PAGE_SIZE, totalAvailable)} of {totalAvailable}
          </span>
          {browseOffset + PAGE_SIZE < totalAvailable && (
            <button style={pageBtnStyle} onClick={() => loadBrowsePage(browseOffset + PAGE_SIZE)}>
              Next
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// -- Sortable catalog table --

function CatalogTable({
  hits,
  sort,
  onSort,
}: {
  hits: DiscoverHit[];
  sort: SortState | null;
  onSort: (column: SortColumn) => void;
}) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <SortableHeader label="Title" column="title" sort={sort} onSort={onSort} />
          <th style={thStyle}>Type</th>
          <SortableHeader label="Date" column="document_date" sort={sort} onSort={onSort} />
          <SortableHeader label="Status" column="lifecycle_status" sort={sort} onSort={onSort} />
        </tr>
      </thead>
      <tbody>
        {hits.map(hit => (
          <tr key={hit.document.id}>
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
  // Default direction: desc for date, asc for title and status
  const defaultDir: SortDir = column === 'document_date' ? 'desc' : 'asc';
  return { column, direction: defaultDir };
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return '-';
  return d.toLocaleDateString();
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
