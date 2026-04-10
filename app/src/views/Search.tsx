import { useState } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { DiscoverHit } from '../api/types';
import { discover } from '../api/discover';

export default function Search() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'hybrid' | 'semantic' | 'keyword'>('hybrid');
  const [showFilters, setShowFilters] = useState(false);
  const [docTypeFilter, setDocTypeFilter] = useState('');
  const [lifecycleFilter, setLifecycleFilter] = useState('');
  const [projectFilter, setProjectFilter] = useState('');
  const [results, setResults] = useState<DiscoverHit[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [filterResults, setFilterResults] = useState<DiscoverHit[] | null>(null);
  const [filterHeading, setFilterHeading] = useState('');

  if (!vault) return <div>Vault not found.</div>;

  // Check for dashboard drill-down filter params
  const filterDefs: Record<string, { filterKey: string; heading: (v: string) => string }> = {
    pipeline_status: { filterKey: 'pipeline_status', heading: v => ({ abstraction_skipped: 'Deferred Abstracts', failed: 'Failed Ingestions' }[v] ?? v) },
    lifecycle_status: { filterKey: 'lifecycle_status', heading: v => `Lifecycle: ${v}` },
    doc_type: { filterKey: 'doc_type', heading: v => `Doc Type: ${v.replace(/_/g, ' ')}` },
    source_type: { filterKey: 'doc_type', heading: v => `Source Adapter: ${v}` }, // approximate via discover
  };

  // On first render with filter params, execute filtered discover
  const filterParam = Object.keys(filterDefs).find(p => searchParams.has(p));
  if (filterParam && !filterResults && !searching) {
    const value = searchParams.get(filterParam)!;
    const def = filterDefs[filterParam];
    setSearching(true);
    setFilterHeading(def.heading(value));
    discover(vaultId, {
      mode: 'keyword',
      query: '*',
      filters: { [def.filterKey]: value },
      limit: 100,
    })
      .then(resp => {
        setFilterResults(resp.results);
        setSearching(false);
      })
      .catch(() => {
        setFilterResults([]);
        setSearching(false);
      });
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    try {
      const filters: Record<string, string> = {};
      if (docTypeFilter) filters.doc_type = docTypeFilter;
      if (lifecycleFilter) filters.lifecycle_status = lifecycleFilter;
      if (projectFilter) filters.project = projectFilter;

      const useHybrid = mode === 'hybrid';
      const resp = await discover(vaultId, {
        mode: mode === 'keyword' ? 'keyword' : 'semantic',
        query: query.trim(),
        filters: Object.keys(filters).length > 0 ? filters : undefined,
        use_hybrid: useHybrid,
        limit: 20,
      });
      setResults(resp.results);
      setHasSearched(true);
    } catch {
      setResults([]);
      setHasSearched(true);
    }
    setSearching(false);
  }

  // Dashboard drill-down view
  if (filterResults !== null) {
    return (
      <div>
        <h1 style={{ margin: '0 0 4px', textTransform: 'capitalize' }}>{filterHeading}</h1>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: '#666' }}>
          {filterResults.length} result{filterResults.length !== 1 ? 's' : ''}
        </p>
        {filterResults.length === 0 ? (
          <div style={{ color: '#999' }}>No documents match this filter.</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Title</th>
                <th style={thStyle}>Type</th>
                <th style={thStyle}>Status</th>
              </tr>
            </thead>
            <tbody>
              {filterResults.map(hit => (
                <tr key={hit.document.id}>
                  <td style={tdStyle}>
                    <Link to={`/documents/${hit.document.id}`} style={{ color: '#1565c0', textDecoration: 'none' }}>
                      {sourceFilename(hit.document.source_path) ?? hit.document.title}
                    </Link>
                  </td>
                  <td style={tdStyle}>{hit.document.doc_type?.replace(/_/g, ' ') ?? '-'}</td>
                  <td style={tdStyle}>{hit.document.lifecycle_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div style={{ marginTop: 16 }}>
          <Link to="/dashboard" style={{ fontSize: 12, color: '#666' }}>&larr; Back to dashboard</Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Search</h1>

      <form onSubmit={handleSearch} style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search documents..."
            style={{ flex: 1, padding: '8px 12px', fontSize: 14 }}
          />
          <select
            value={mode}
            onChange={e => setMode(e.target.value as 'hybrid' | 'semantic' | 'keyword')}
            style={{ padding: '8px 12px' }}
          >
            <option value="hybrid">Hybrid</option>
            <option value="semantic">Semantic</option>
            <option value="keyword">Keyword</option>
          </select>
          <button type="submit" style={btnStyle} disabled={searching}>
            {searching ? 'Searching...' : 'Search'}
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
            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
              <div>
                <label style={filterLabelStyle}>Document type</label>
                <select
                  value={docTypeFilter}
                  onChange={e => setDocTypeFilter(e.target.value)}
                  style={{ padding: '4px 8px' }}
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
                  style={{ padding: '4px 8px' }}
                >
                  <option value="">All</option>
                  {vault.lifecycle_states.map(ls => (
                    <option key={ls.value} value={ls.value}>{ls.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label style={filterLabelStyle}>Project</label>
                <input
                  type="text"
                  value={projectFilter}
                  onChange={e => setProjectFilter(e.target.value)}
                  placeholder="e.g. pim_health"
                  style={{ padding: '4px 8px' }}
                />
              </div>
            </div>
          </div>
        )}
      </form>

      {!hasSearched && !searching && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>Enter a query to search.</div>
      )}

      {hasSearched && results.length === 0 && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>No results found.</div>
      )}

      {results.map((hit) => (
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
              {hit.chunk_content}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function sourceFilename(sourcePath: string | null | undefined): string | null {
  if (!sourcePath) return null;
  const filename = sourcePath.includes('/') ? sourcePath.split('/').pop()! : sourcePath;
  // Strip extension
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.substring(0, dot) : filename;
}

const btnStyle: React.CSSProperties = { padding: '8px 20px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 14 };
const filterLabelStyle: React.CSSProperties = { display: 'block', fontSize: 11, color: '#666', marginBottom: 4, fontWeight: 500 };
const badgeStyle: React.CSSProperties = { padding: '1px 8px', borderRadius: 3, fontSize: 10, fontWeight: 600, background: '#e3f2fd', color: '#1565c0', textTransform: 'capitalize' };
const thStyle: React.CSSProperties = { textAlign: 'left', padding: '6px 10px', borderBottom: '2px solid #ddd', fontSize: 12, color: '#666' };
const tdStyle: React.CSSProperties = { padding: '6px 10px', borderBottom: '1px solid #eee', fontSize: 13 };
