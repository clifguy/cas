import { useState } from 'react';
import { Link, useParams, useOutletContext } from 'react-router-dom';
import { vaults, getDocument, getEdgesForDocument, getDocumentTitle } from '../mock/data';

export default function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const { vaultId } = useOutletContext<{ vaultId: string }>();
  const doc = id ? getDocument(vaultId, id) : undefined;
  const edges = id ? getEdgesForDocument(vaultId, id) : [];
  const vault = vaults[vaultId];
  const [showEdgeDialog, setShowEdgeDialog] = useState(false);

  if (!doc) return <div>Document not found.</div>;

  // Group edges by type
  const edgeGroups: Record<string, typeof edges> = {};
  for (const e of edges) {
    if (!edgeGroups[e.edge_type]) edgeGroups[e.edge_type] = [];
    edgeGroups[e.edge_type].push(e);
  }

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <Link to="/search" style={{ fontSize: 12, color: '#666' }}>&larr; Back to search</Link>
      </div>

      <h1 style={{ margin: '0 0 4px' }}>{doc.title}</h1>
      <div style={{ display: 'flex', gap: 8, marginBottom: 20 }}>
        {doc.doc_type && <Badge label={doc.doc_type.replace(/_/g, ' ')} color="#1565c0" />}
        <Badge label={doc.lifecycle_status} color="#2e7d32" />
        <Badge label={doc.pipeline_status.replace(/_/g, ' ')} color="#6a1b9a" />
      </div>

      {/* Metadata Panel */}
      <Section title="Metadata - Tier 1 (Core)">
        <MetaTable rows={[
          ['Document ID', doc.id],
          ['Title', doc.title],
          ['Lifecycle status', doc.lifecycle_status],
          ['Created at', new Date(doc.created_at).toLocaleString()],
          ['Updated at', new Date(doc.updated_at).toLocaleString()],
        ]} />
      </Section>

      <Section title="Metadata - Tier 2 (Vault-Configured)">
        <MetaTable rows={[
          ['Doc type', doc.doc_type ?? '-'],
          ['Version label', doc.version_label ?? '-'],
          ['Project', doc.project ?? '-'],
          ['Tags', doc.tags.length > 0 ? doc.tags.join(', ') : '-'],
          ['Authority scope', doc.authority_scope ?? '-'],
        ]} />
      </Section>

      {doc.tier3_metadata && (
        <Section title="Metadata - Tier 3 (Source-Specific)">
          <MetaTable rows={Object.entries(doc.tier3_metadata).map(([k, v]) => [
            k,
            typeof v === 'object' ? JSON.stringify(v) : String(v),
          ])} />
        </Section>
      )}

      <Section title="Provenance">
        <MetaTable rows={[
          ['Source type', doc.source_type],
          ['Source path', doc.source_path],
          ['Content hash', doc.source_content_hash],
          ['Adapter version', doc.adapter_version],
          ['Projected at', doc.projected_at ? new Date(doc.projected_at).toLocaleString() : '-'],
          ['Indexed at', doc.indexed_at ? new Date(doc.indexed_at).toLocaleString() : '-'],
          ['Source modified at', doc.source_modified_at ? new Date(doc.source_modified_at).toLocaleString() : '-'],
          ['Created by', doc.created_by],
        ]} />
      </Section>

      {/* Semantic Abstract */}
      {doc.semantic_abstract && (
        <Section title="Semantic Abstract">
          <div style={{ fontSize: 13, lineHeight: 1.6, color: '#444', fontStyle: 'italic' }}>
            {doc.semantic_abstract}
          </div>
        </Section>
      )}

      {/* Projection Preview */}
      {doc.projection_text && (
        <Section title="Projection Preview">
          <div style={{
            background: '#f9f9f9',
            border: '1px solid #eee',
            borderRadius: 4,
            padding: 16,
            fontSize: 13,
            lineHeight: 1.6,
            fontFamily: 'system-ui',
            whiteSpace: 'pre-wrap',
          }}>
            {doc.projection_text}
          </div>
        </Section>
      )}

      <button style={{ ...btnStyle, marginBottom: 16, background: '#eee', color: '#333' }} disabled>
        Open Source File
      </button>

      {/* Edge List */}
      <Section title="Edges">
        {Object.keys(edgeGroups).length === 0 ? (
          <div style={{ color: '#666' }}>No edges for this document.</div>
        ) : (
          Object.entries(edgeGroups).map(([edgeType, group]) => (
            <div key={edgeType} style={{ marginBottom: 12 }}>
              <h4 style={{ margin: '0 0 4px', fontSize: 13, textTransform: 'capitalize', color: '#666' }}>
                {edgeType.replace(/_/g, ' ')}
              </h4>
              {group.map(e => {
                const relatedId = e.source_id === id ? e.target_id : e.source_id;
                const direction = e.source_id === id ? 'to' : 'from';
                return (
                  <div key={e.id} style={{ fontSize: 13, marginBottom: 4, paddingLeft: 12 }}>
                    {direction}{' '}
                    <Link to={`/documents/${relatedId}`} style={{ color: '#1565c0' }}>
                      {getDocumentTitle(vaultId, relatedId)}
                    </Link>
                    {e.notes && <span style={{ color: '#999', fontSize: 11 }}> - {e.notes}</span>}
                  </div>
                );
              })}
            </div>
          ))
        )}

        <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
          <Link to={`/documents/${id}/graph`} style={{ ...btnStyle, textDecoration: 'none', textAlign: 'center' }}>
            View in Graph
          </Link>
          <button onClick={() => setShowEdgeDialog(!showEdgeDialog)} style={{ ...btnStyle, background: '#eee', color: '#333' }}>
            Add Edge
          </button>
        </div>
      </Section>

      {/* Manual Edge Creation Dialog */}
      {showEdgeDialog && (
        <div style={{
          border: '1px solid #ddd',
          borderRadius: 4,
          padding: 16,
          marginTop: 8,
          background: '#fafafa',
        }}>
          <h4 style={{ margin: '0 0 12px', fontSize: 14 }}>Create Edge</h4>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <div>
              <label style={filterLabelStyle}>Edge type</label>
              <select style={{ padding: '4px 8px' }}>
                <option value="authoritative_for">authoritative_for</option>
                <option value="depends_on">depends_on</option>
                <option value="sync_target">sync_target</option>
                <option value="covers">covers</option>
                <option value="derived_from">derived_from</option>
                <option value="bundles_with">bundles_with</option>
                <option value="references">references</option>
              </select>
            </div>
            <div>
              <label style={filterLabelStyle}>Target document</label>
              <select style={{ padding: '4px 8px' }}>
                <option value="">Select...</option>
                {vault?.documents.filter(d => d.id !== id).map(d => (
                  <option key={d.id} value={d.id}>{d.title}</option>
                ))}
              </select>
            </div>
            <button style={btnStyle} onClick={() => setShowEdgeDialog(false)}>Create</button>
            <button style={{ ...btnStyle, background: '#eee', color: '#333' }} onClick={() => setShowEdgeDialog(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

// -- Sub-components --

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h2 style={{ fontSize: 15, borderBottom: '1px solid #ddd', paddingBottom: 4, marginBottom: 10 }}>{title}</h2>
      {children}
    </div>
  );
}

function MetaTable({ rows }: { rows: [string, string][] }) {
  return (
    <table style={{ borderCollapse: 'collapse', width: '100%' }}>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <td style={{ ...tdStyle, fontWeight: 500, width: '30%', color: '#555' }}>{label}</td>
            <td style={tdStyle}>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span style={{
      padding: '2px 10px',
      borderRadius: 3,
      fontSize: 11,
      fontWeight: 600,
      background: `${color}18`,
      color,
      textTransform: 'capitalize',
    }}>
      {label}
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

const filterLabelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  color: '#666',
  marginBottom: 4,
  fontWeight: 500,
};

const tdStyle: React.CSSProperties = {
  padding: '5px 10px',
  borderBottom: '1px solid #eee',
  fontSize: 13,
};
