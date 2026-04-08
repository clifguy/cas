import { useState } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import { vaults, type Document } from '../mock/data';

// Filter configurations for dashboard drill-down links
const filterDefs: Record<string, { field: keyof Document; heading: (v: string) => string }> = {
  pipeline_status: { field: 'pipeline_status', heading: v => ({ abstraction_skipped: 'Deferred Abstracts', failed: 'Failed Ingestions' }[v] ?? v) },
  lifecycle_status: { field: 'lifecycle_status', heading: v => `Lifecycle: ${v}` },
  doc_type: { field: 'doc_type', heading: v => `Doc Type: ${v.replace(/_/g, ' ')}` },
  source_type: { field: 'source_type', heading: v => `Source Adapter: ${v}` },
};

export default function Search() {
  const { vaultId } = useOutletContext<{ vaultId: string }>();
  const vault = vaults[vaultId];
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'hybrid' | 'semantic' | 'keyword'>('hybrid');
  const [showFilters, setShowFilters] = useState(false);
  const [docTypeFilter, setDocTypeFilter] = useState('');
  const [lifecycleFilters, setLifecycleFilters] = useState<string[]>([]);
  const [projectFilter, setProjectFilter] = useState('');
  const [hasSearched, setHasSearched] = useState(false);

  if (!vault) return <div>Vault not found.</div>;

  // Check for any dashboard drill-down filter
  let filteredDocuments: Document[] | null = null;
  let filterHeading = '';
  for (const [param, def] of Object.entries(filterDefs)) {
    const value = searchParams.get(param);
    if (value) {
      filteredDocuments = vault.documents.filter(d => String(d[def.field]) === value);
      filterHeading = def.heading(value);
      break;
    }
  }

  const results = hasSearched ? vault.search_results : [];

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setHasSearched(true);
  }

  // If showing a filtered document list, render that instead of search
  if (filteredDocuments) {
    const showErrorColumn = searchParams.has('pipeline_status');
    return (
      <div>
        <h1 style={{ margin: '0 0 4px', textTransform: 'capitalize' }}>{filterHeading}</h1>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: '#666' }}>
          {filteredDocuments.length} document{filteredDocuments.length !== 1 ? 's' : ''}
        </p>
        {filteredDocuments.length === 0 ? (
          <div style={{ color: '#999' }}>No documents match this filter.</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Title</th>
                <th style={thStyle}>Type</th>
                <th style={thStyle}>Status</th>
                <th style={thStyle}>Pipeline</th>
                {showErrorColumn && <th style={thStyle}>Error</th>}
              </tr>
            </thead>
            <tbody>
              {filteredDocuments.map(doc => (
                <tr key={doc.id}>
                  <td style={tdStyle}>
                    <Link to={`/documents/${doc.id}`} style={{ color: '#1565c0', textDecoration: 'none' }}>
                      {doc.title}
                    </Link>
                  </td>
                  <td style={tdStyle}>{doc.doc_type?.replace(/_/g, ' ') ?? '-'}</td>
                  <td style={tdStyle}>{doc.lifecycle_status}</td>
                  <td style={tdStyle}>{doc.pipeline_status.replace(/_/g, ' ')}</td>
                  {showErrorColumn && <td style={tdStyle}>{doc.pipeline_error ?? '-'}</td>}
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

      {/* Query interface */}
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
          <button type="submit" style={btnStyle}>Search</button>
        </div>

        {/* Filter toggle */}
        <button
          type="button"
          onClick={() => setShowFilters(!showFilters)}
          style={{ background: 'none', border: 'none', color: '#1565c0', cursor: 'pointer', fontSize: 12, padding: 0 }}
        >
          {showFilters ? 'Hide filters' : 'Show filters'}
        </button>

        {/* Filters */}
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
                <div style={{ display: 'flex', gap: 8 }}>
                  {vault.lifecycle_states.map(ls => (
                    <label key={ls.value} style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 3 }}>
                      <input
                        type="checkbox"
                        checked={lifecycleFilters.includes(ls.value)}
                        onChange={e => {
                          if (e.target.checked) {
                            setLifecycleFilters([...lifecycleFilters, ls.value]);
                          } else {
                            setLifecycleFilters(lifecycleFilters.filter(v => v !== ls.value));
                          }
                        }}
                      />
                      {ls.label}
                    </label>
                  ))}
                </div>
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

      {/* Results */}
      {!hasSearched && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>Enter a query to search.</div>
      )}

      {hasSearched && results.length === 0 && (
        <div style={{ color: '#999', marginTop: 32, textAlign: 'center' }}>No results found.</div>
      )}

      {results.map((hit) => (
        <div key={hit.document.id} style={{
          borderBottom: '1px solid #eee',
          padding: '16px 0',
        }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
            <Link to={`/documents/${hit.document.id}`} style={{ fontSize: 15, fontWeight: 600, color: '#1565c0', textDecoration: 'none' }}>
              {hit.document.title}
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

          {/* Show semantic abstract from the full document list if available */}
          {vault.documents.find(d => d.id === hit.document.id)?.semantic_abstract && (
            <div style={{ fontSize: 12, color: '#888', fontStyle: 'italic', marginTop: 4 }}>
              {vault.documents.find(d => d.id === hit.document.id)!.semantic_abstract}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  padding: '8px 20px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 14,
};

const filterLabelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  color: '#666',
  marginBottom: 4,
  fontWeight: 500,
};

const badgeStyle: React.CSSProperties = {
  padding: '1px 8px',
  borderRadius: 3,
  fontSize: 10,
  fontWeight: 600,
  background: '#e3f2fd',
  color: '#1565c0',
  textTransform: 'capitalize',
};

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '6px 10px',
  borderBottom: '2px solid #ddd',
  fontSize: 12,
  color: '#666',
};

const tdStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderBottom: '1px solid #eee',
  fontSize: 13,
};
