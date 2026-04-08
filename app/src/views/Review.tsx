import { useState } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import { vaults, getDocumentTitle, type PendingMetadata, type StagingEdge } from '../mock/data';

export default function Review() {
  const { vaultId } = useOutletContext<{ vaultId: string }>();
  const vault = vaults[vaultId];
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('tab') === 'edges' ? 'edges' : 'metadata';

  if (!vault) return <div>Vault not found.</div>;

  return (
    <div>
      <h1 style={{ margin: '0 0 16px' }}>Review</h1>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 0, borderBottom: '2px solid #ddd', marginBottom: 20 }}>
        <TabButton
          label={`Metadata Review (${vault.pending_metadata.length})`}
          active={activeTab === 'metadata'}
          onClick={() => setSearchParams({ tab: 'metadata' })}
        />
        <TabButton
          label={`Edge Review (${vault.staging_edges.length})`}
          active={activeTab === 'edges'}
          onClick={() => setSearchParams({ tab: 'edges' })}
        />
      </div>

      {activeTab === 'metadata' ? (
        <MetadataReview items={vault.pending_metadata} />
      ) : (
        <EdgeReview edges={vault.staging_edges} vaultId={vaultId} />
      )}
    </div>
  );
}

// -- Metadata Review --

function MetadataReview({ items }: { items: PendingMetadata[] }) {
  const [queue, setQueue] = useState(items);

  if (queue.length === 0) {
    return <div style={{ color: '#666' }}>No metadata pending review.</div>;
  }

  function confirmOne(docId: string) {
    setQueue(q => q.filter(item => item.document.id !== docId));
  }

  function confirmAll() {
    setQueue([]);
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={confirmAll} style={btnStyle}>Confirm All</button>
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={thStyle}>Document</th>
            <th style={thStyle}>Field</th>
            <th style={thStyle}>Value</th>
            <th style={thStyle}>Source</th>
            <th style={thStyle}>Alternate</th>
            <th style={thStyle}></th>
          </tr>
        </thead>
        <tbody>
          {queue.map(item => {
            const fields = Object.entries(item.extracted_fields);
            return fields.map(([field, info], fi) => (
              <tr key={`${item.document.id}-${field}`}>
                {fi === 0 && (
                  <td style={{ ...tdStyle, verticalAlign: 'top' }} rowSpan={fields.length}>
                    <Link to={`/documents/${item.document.id}`} style={{ color: '#1565c0' }}>
                      {item.document.title}
                    </Link>
                  </td>
                )}
                <td style={tdStyle}>{field}</td>
                <td style={tdStyle}>
                  <input
                    type="text"
                    defaultValue={info.value}
                    style={{ padding: '2px 6px', width: '100%', boxSizing: 'border-box' }}
                  />
                </td>
                <td style={tdStyle}>
                  <SourceBadge source={info.source} />
                </td>
                <td style={tdStyle}>
                  {info.alt_value ? (
                    <span style={{ fontSize: 12, color: '#888' }}>
                      {info.alt_value} <SourceBadge source={info.alt_source!} />
                    </span>
                  ) : '-'}
                </td>
                {fi === 0 && (
                  <td style={{ ...tdStyle, verticalAlign: 'top' }} rowSpan={fields.length}>
                    <button onClick={() => confirmOne(item.document.id)} style={btnSmallStyle}>
                      Confirm
                    </button>
                  </td>
                )}
              </tr>
            ));
          })}
        </tbody>
      </table>
    </div>
  );
}

// -- Edge Review --

function EdgeReview({ edges, vaultId }: { edges: StagingEdge[]; vaultId: string }) {
  const [staging, setStaging] = useState(edges);

  if (staging.length === 0) {
    return <div style={{ color: '#666' }}>No edges pending review.</div>;
  }

  // Group by edge_type
  const groups: Record<string, StagingEdge[]> = {};
  for (const e of staging) {
    if (!groups[e.edge_type]) groups[e.edge_type] = [];
    groups[e.edge_type].push(e);
  }

  function confirmEdge(id: string) {
    setStaging(s => s.filter(e => e.id !== id));
  }

  function dismissEdge(id: string) {
    setStaging(s => s.filter(e => e.id !== id));
  }

  function confirmGroup(edgeType: string) {
    setStaging(s => s.filter(e => e.edge_type !== edgeType));
  }

  function dismissGroup(edgeType: string) {
    setStaging(s => s.filter(e => e.edge_type !== edgeType));
  }

  function confirmAll() {
    setStaging([]);
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={confirmAll} style={btnStyle}>Confirm All</button>
      </div>

      {Object.entries(groups).map(([edgeType, groupEdges]) => (
        <div key={edgeType} style={{ marginBottom: 24 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 14, textTransform: 'capitalize' }}>{edgeType.replace(/_/g, ' ')}</h3>
            <div style={{ display: 'flex', gap: 4 }}>
              <button onClick={() => confirmGroup(edgeType)} style={btnSmallStyle}>Confirm All</button>
              <button onClick={() => dismissGroup(edgeType)} style={{ ...btnSmallStyle, background: '#eee', color: '#333' }}>
                Dismiss All
              </button>
            </div>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle}>Source</th>
                <th style={thStyle}>Target</th>
                <th style={thStyle}>Evidence</th>
                <th style={thStyle}>Tier</th>
                <th style={thStyle}></th>
              </tr>
            </thead>
            <tbody>
              {groupEdges.map(e => (
                <tr key={e.id}>
                  <td style={tdStyle}>
                    <Link to={`/documents/${e.source_id}`} style={{ color: '#1565c0' }}>
                      {getDocumentTitle(vaultId, e.source_id)}
                    </Link>
                  </td>
                  <td style={tdStyle}>
                    <Link to={`/documents/${e.target_id}`} style={{ color: '#1565c0' }}>
                      {getDocumentTitle(vaultId, e.target_id)}
                    </Link>
                  </td>
                  <td style={tdStyle}><span style={{ fontSize: 12, color: '#666' }}>{e.inference_evidence}</span></td>
                  <td style={tdStyle}>Tier {e.confidence_tier}</td>
                  <td style={tdStyle}>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <button onClick={() => confirmEdge(e.id)} style={btnSmallStyle}>Confirm</button>
                      <button onClick={() => dismissEdge(e.id)} style={{ ...btnSmallStyle, background: '#eee', color: '#333' }}>
                        Dismiss
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

// -- Sub-components --

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '8px 16px',
        border: 'none',
        borderBottom: active ? '2px solid #333' : '2px solid transparent',
        background: 'none',
        fontWeight: active ? 600 : 400,
        color: active ? '#000' : '#666',
        cursor: 'pointer',
        fontSize: 13,
        marginBottom: -2,
      }}
    >
      {label}
    </button>
  );
}

function SourceBadge({ source }: { source: string }) {
  const colors: Record<string, string> = { filename: '#1565c0', content: '#2e7d32', default: '#999' };
  return (
    <span style={{
      padding: '1px 6px',
      borderRadius: 3,
      fontSize: 10,
      fontWeight: 600,
      background: `${colors[source] ?? '#999'}18`,
      color: colors[source] ?? '#999',
    }}>
      {source}
    </span>
  );
}

const btnStyle: React.CSSProperties = {
  padding: '6px 16px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};

const btnSmallStyle: React.CSSProperties = {
  padding: '3px 10px',
  border: '1px solid #ccc',
  borderRadius: 3,
  background: '#333',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 11,
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
};
