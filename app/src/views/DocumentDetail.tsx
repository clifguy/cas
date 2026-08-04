import { useState, useEffect } from 'react';
import { Link, useParams, useOutletContext, useNavigate } from 'react-router';
import type { VaultContext } from '../App';
import type { Document, Edge, EdgeType, LinkRequest, ResolutionPolicy } from '../api/types';
import { DEFAULT_EDGE_POLICIES } from '../api/types';
import {
  getDocument,
  openDocument,
  getDocumentDownloadUrl,
  documentContentUrl,
  reabstractDocument,
} from '../api/documents';
import { detectIngestProfile } from '../api/ingest';
import { traverse } from '../api/graph';
import { createEdge } from '../api/graph';
import { ApiError } from '../api/client';

// Poll cadence for observing a fire-and-forget re-abstraction to completion.
const REABSTRACT_POLL_INTERVAL_MS = 1000;
const REABSTRACT_MAX_POLLS = 120;

// Edge types in the order we render them in the form dropdown and edge list.
// Mirrors the SAGE EdgeType enum and the registry order.
const EDGE_TYPES_ORDERED: EdgeType[] = [
  'supersedes',
  'derived_from',
  'instantiated_from',
  'covers',
  'references',
  'bundles_with',
  'depends_on',
  'authoritative_for',
  'sync_target',
  'retracts',
  'merged_from',
];

// Anchor-field requirements derived from resolution_policy (CAS-ADR-017).
// `retracts` is policy=none but carries a one-sided source anchor.
function anchorRequirements(edgeType: EdgeType, policy: ResolutionPolicy): {
  needsSourceAnchor: boolean;
  needsTargetAnchor: boolean;
  needsTarget: boolean;
  needsRetractedEdge: boolean;
} {
  if (edgeType === 'retracts') {
    return { needsSourceAnchor: true, needsTargetAnchor: false, needsTarget: false, needsRetractedEdge: true };
  }
  switch (policy) {
    case 'transitive_source':
      return { needsSourceAnchor: true, needsTargetAnchor: false, needsTarget: true, needsRetractedEdge: false };
    case 'transitive_target':
      return { needsSourceAnchor: false, needsTargetAnchor: true, needsTarget: true, needsRetractedEdge: false };
    case 'transitive_both':
      return { needsSourceAnchor: true, needsTargetAnchor: true, needsTarget: true, needsRetractedEdge: false };
    default:
      // 'none' (non-retracts) and 'TBD' (form rejects submission separately)
      return { needsSourceAnchor: false, needsTargetAnchor: false, needsTarget: true, needsRetractedEdge: false };
  }
}

export default function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const { vaultId } = useOutletContext<VaultContext>();
  const navigate = useNavigate();
  const [doc, setDoc] = useState<Document | null>(null);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showEdgeDialog, setShowEdgeDialog] = useState(false);
  const [newEdgeType, setNewEdgeType] = useState<EdgeType>('covers');
  const [newTargetId, setNewTargetId] = useState('');
  const [newEdgeNotes, setNewEdgeNotes] = useState('');
  const [newEdgeRationale, setNewEdgeRationale] = useState('');
  const [newSourceAnchor, setNewSourceAnchor] = useState('');
  const [newTargetAnchor, setNewTargetAnchor] = useState('');
  const [newRetractedEdgeId, setNewRetractedEdgeId] = useState('');
  const [edgeError, setEdgeError] = useState('');
  const [neighborTitles, setNeighborTitles] = useState<Record<string, string>>({});
  const [openStatus, setOpenStatus] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);
  const [reabstractPhase, setReabstractPhase] = useState<'idle' | 'running'>('idle');
  const [reabstractMsg, setReabstractMsg] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);

  async function handleReabstract() {
    if (!id) return;
    setReabstractMsg(null);
    setReabstractPhase('running');
    try {
      await reabstractDocument(vaultId, id);
      // Fire-and-forget: the abstract is written by a background job, so poll
      // the document until its pipeline_status leaves 'abstraction_in_progress'.
      let refreshed = await getDocument(vaultId, id);
      for (
        let i = 0;
        i < REABSTRACT_MAX_POLLS && refreshed.pipeline_status === 'abstraction_in_progress';
        i++
      ) {
        await new Promise(resolve => setTimeout(resolve, REABSTRACT_POLL_INTERVAL_MS));
        refreshed = await getDocument(vaultId, id);
      }
      setDoc(refreshed);
      setReabstractPhase('idle');
      setReabstractMsg({ kind: 'ok', message: 'Abstract regenerated.' });
    } catch (err) {
      setReabstractPhase('idle');
      if (err instanceof ApiError && err.code === 'reabstract_document_already_in_flight') {
        setReabstractMsg({ kind: 'err', message: 'A regeneration is already running for this document.' });
      } else {
        const msg = err instanceof Error ? err.message : 'Failed to regenerate abstract';
        setReabstractMsg({ kind: 'err', message: msg });
      }
    }
  }

  async function handleOpen() {
    if (!id) return;
    setOpenStatus(null);
    try {
      // "Open" means different things by deployment profile: co-located, the
      // browser and SAGE share a machine, so SAGE opens the file with the host
      // OS opener; hosted (cloud), SAGE is headless, so it mints a short-lived
      // download URL the browser fetches directly from the backing store.
      const profile = await detectIngestProfile(vaultId);
      if (profile === 'hosted') {
        try {
          const { download_url } = await getDocumentDownloadUrl(vaultId, id);
          window.open(download_url, '_blank', 'noopener');
        } catch (err) {
          // The filesystem-backed binding cannot presign and answers 501
          // download_url_unavailable; fall back to the same-origin streaming
          // content route, which the BFF proxies chunk-by-chunk (CAS-ADR-043).
          if (err instanceof ApiError && err.code === 'download_url_unavailable') {
            window.open(documentContentUrl(vaultId, id), '_blank', 'noopener');
          } else {
            throw err;
          }
        }
        setOpenStatus({ kind: 'ok', message: 'Opened in browser' });
      } else {
        await openDocument(vaultId, id);
        setOpenStatus({ kind: 'ok', message: 'Opened' });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to open';
      setOpenStatus({ kind: 'err', message: msg });
    }
  }

  useEffect(() => {
    async function load() {
      if (!id) return;
      setLoading(true);
      setError('');
      try {
        const [document, traverseResp] = await Promise.all([
          getDocument(vaultId, id),
          traverse(vaultId, { start_id: id, direction: 'both', depth: 1 }),
        ]);
        setDoc(document);
        const edgeList = traverseResp.nodes.map(n => n.edge);
        setEdges(edgeList);
        // Build a title map from traversal results
        const titles: Record<string, string> = {};
        for (const node of traverseResp.nodes) {
          titles[node.document.id] = node.document.title;
        }
        setNeighborTitles(titles);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load document');
        setLoading(false);
      }
    }
    load();
  }, [vaultId, id]);

  if (loading) return <div>Loading document...</div>;
  if (error) return <div style={{ color: '#c62828' }}>Error: {error}</div>;
  if (!doc) return <div>Document not found.</div>;

  // Group edges by type
  const edgeGroups: Record<string, Edge[]> = {};
  for (const e of edges) {
    if (!edgeGroups[e.edge_type]) edgeGroups[e.edge_type] = [];
    edgeGroups[e.edge_type].push(e);
  }

  function resolveTitle(docId: string): string {
    if (docId === id) return doc?.title ?? docId;
    return neighborTitles[docId] ?? docId;
  }

  async function handleCreateEdge() {
    if (!id) return;
    const policy = DEFAULT_EDGE_POLICIES[newEdgeType] ?? 'none';
    if (policy === 'TBD') {
      setEdgeError(
        `Edge type '${newEdgeType}' has resolution_policy=TBD; freeze the policy in the registry before use.`,
      );
      return;
    }
    const reqs = anchorRequirements(newEdgeType, policy);
    if (reqs.needsTarget && !newTargetId.trim()) {
      setEdgeError('Target document ID is required for this edge type.');
      return;
    }
    if (reqs.needsRetractedEdge && !newRetractedEdgeId.trim()) {
      setEdgeError('Retracted edge ID is required for `retracts` edges.');
      return;
    }
    if (reqs.needsSourceAnchor && !newSourceAnchor.trim()) {
      setEdgeError('Source anchor (source_valid_from_version) is required for this edge type.');
      return;
    }
    if (reqs.needsTargetAnchor && !newTargetAnchor.trim()) {
      setEdgeError('Target anchor (target_valid_from_version) is required for this edge type.');
      return;
    }

    setEdgeError('');
    const payload: LinkRequest = {
      source_id: id,
      edge_type: newEdgeType,
      ...(reqs.needsTarget && { target_id: newTargetId.trim() }),
      ...(reqs.needsRetractedEdge && { retracted_edge_id: newRetractedEdgeId.trim() }),
      ...(reqs.needsSourceAnchor && { source_valid_from_version: newSourceAnchor.trim() }),
      ...(reqs.needsTargetAnchor && { target_valid_from_version: newTargetAnchor.trim() }),
      ...(newEdgeNotes.trim() && { notes: newEdgeNotes.trim() }),
      ...(newEdgeRationale.trim() && { rationale: newEdgeRationale.trim() }),
    };
    try {
      const newEdge = await createEdge(vaultId, payload);
      setEdges(prev => [...prev, newEdge]);
      setShowEdgeDialog(false);
      setNewTargetId('');
      setNewEdgeNotes('');
      setNewEdgeRationale('');
      setNewSourceAnchor('');
      setNewTargetAnchor('');
      setNewRetractedEdgeId('');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to create edge';
      setEdgeError(msg);
    }
  }

  // Pre-fill anchor defaults to the current source/target ids when the user
  // changes edge type or target. This makes "attach at current heads" one click.
  function onChangeEdgeType(t: EdgeType) {
    setNewEdgeType(t);
    setEdgeError('');
    if (id) setNewSourceAnchor(prev => (prev ? prev : id));
    if (newTargetId) setNewTargetAnchor(prev => (prev ? prev : newTargetId));
  }

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <button
          type="button"
          onClick={() => navigate(-1)}
          style={{ background: 'none', border: 'none', padding: 0, fontSize: 12, color: '#666', cursor: 'pointer', textDecoration: 'underline' }}
        >
          &larr; Back to search
        </button>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12, margin: '0 0 4px' }}>
        <h1 style={{ margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{doc.title}</h1>
        <button type="button" onClick={handleOpen} style={btnStyle}>
          Open
        </button>
        {openStatus && (
          <span style={{ fontSize: 12, color: openStatus.kind === 'ok' ? '#2e7d32' : '#c62828' }}>
            {openStatus.message}
          </span>
        )}
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 20 }}>
        {doc.doc_type && <Badge label={doc.doc_type.replace(/_/g, ' ')} color="#1565c0" />}
        <Badge label={doc.lifecycle_status} color="#2e7d32" />
        <Badge label={doc.pipeline_status.replace(/_/g, ' ')} color="#6a1b9a" />
      </div>

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

      <Section title="Semantic Abstract">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
          <button
            type="button"
            onClick={handleReabstract}
            disabled={reabstractPhase === 'running'}
            style={{
              ...btnStyle,
              background: '#eee',
              color: '#333',
              ...(reabstractPhase === 'running' ? { cursor: 'not-allowed', opacity: 0.6 } : {}),
            }}
          >
            {reabstractPhase === 'running' ? 'Regenerating…' : 'Regenerate abstract'}
          </button>
          {reabstractMsg && (
            <span style={{ fontSize: 12, color: reabstractMsg.kind === 'ok' ? '#2e7d32' : '#c62828' }}>
              {reabstractMsg.message}
            </span>
          )}
        </div>
        {doc.semantic_abstract ? (
          <div style={{ fontSize: 13, lineHeight: 1.6, color: '#444', fontStyle: 'italic' }}>
            {doc.semantic_abstract}
          </div>
        ) : (
          <div style={{ fontSize: 13, color: '#888' }}>No abstract generated yet.</div>
        )}
      </Section>

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
                const tombstoned = e.valid_until_version != null;
                const isRetracts = e.edge_type === 'retracts';
                // `retracts` has a null target_id; surface the retracted edge id instead.
                const relatedId = isRetracts
                  ? null
                  : e.source_id === id ? e.target_id : e.source_id;
                const direction = e.source_id === id ? 'to' : 'from';
                const rowStyle: React.CSSProperties = {
                  fontSize: 13,
                  marginBottom: 4,
                  paddingLeft: 12,
                  opacity: tombstoned ? 0.5 : 1,
                };
                return (
                  <div key={e.id} style={rowStyle}>
                    {isRetracts ? (
                      <span>retracts edge <code style={{ fontSize: 11 }}>{e.retracted_edge_id ?? '?'}</code></span>
                    ) : (
                      <>
                        {direction}{' '}
                        {relatedId ? (
                          <Link to={`/documents/${relatedId}`} style={{ color: '#1565c0' }}>
                            {resolveTitle(relatedId)}
                          </Link>
                        ) : (
                          <span style={{ color: '#999' }}>(no target)</span>
                        )}
                      </>
                    )}
                    <AnchorBadges edge={e} />
                    {tombstoned && (
                      <span style={tombstoneBadgeStyle} title={`Tombstoned at ${e.valid_until_version}`}>
                        tombstoned
                      </span>
                    )}
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

      {showEdgeDialog && (() => {
        const policy = DEFAULT_EDGE_POLICIES[newEdgeType] ?? 'none';
        const reqs = anchorRequirements(newEdgeType, policy);
        const isTBD = policy === 'TBD';
        return (
          <div style={{ border: '1px solid #ddd', borderRadius: 4, padding: 16, marginTop: 8, background: '#fafafa' }}>
            <h4 style={{ margin: '0 0 12px', fontSize: 14 }}>Create Edge</h4>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
              <div>
                <label style={filterLabelStyle}>Edge type</label>
                <select
                  value={newEdgeType}
                  onChange={e => onChangeEdgeType(e.target.value as EdgeType)}
                  style={{ padding: '4px 8px' }}
                >
                  {EDGE_TYPES_ORDERED.map(t => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                <div style={policyHintStyle}>
                  resolution_policy: <code>{policy}</code>
                </div>
              </div>
              {reqs.needsTarget && (
                <div>
                  <label style={filterLabelStyle}>Target document ID</label>
                  <input
                    type="text"
                    value={newTargetId}
                    onChange={e => setNewTargetId(e.target.value)}
                    placeholder="Enter document ID"
                    style={{ padding: '4px 8px' }}
                  />
                </div>
              )}
              {reqs.needsRetractedEdge && (
                <div>
                  <label style={filterLabelStyle}>Retracted edge ID</label>
                  <input
                    type="text"
                    value={newRetractedEdgeId}
                    onChange={e => setNewRetractedEdgeId(e.target.value)}
                    placeholder="Edge id to retract"
                    style={{ padding: '4px 8px' }}
                  />
                </div>
              )}
              {reqs.needsSourceAnchor && (
                <div>
                  <label style={filterLabelStyle}>
                    Source anchor (chain member)
                  </label>
                  <input
                    type="text"
                    value={newSourceAnchor}
                    onChange={e => setNewSourceAnchor(e.target.value)}
                    placeholder={id ?? 'document_id on source chain'}
                    style={{ padding: '4px 8px' }}
                  />
                </div>
              )}
              {reqs.needsTargetAnchor && (
                <div>
                  <label style={filterLabelStyle}>
                    Target anchor (chain member)
                  </label>
                  <input
                    type="text"
                    value={newTargetAnchor}
                    onChange={e => setNewTargetAnchor(e.target.value)}
                    placeholder="document_id on target chain"
                    style={{ padding: '4px 8px' }}
                  />
                </div>
              )}
              <div>
                <label style={filterLabelStyle}>Notes (optional)</label>
                <input
                  type="text"
                  value={newEdgeNotes}
                  onChange={e => setNewEdgeNotes(e.target.value)}
                  placeholder="Brief description"
                  style={{ padding: '4px 8px' }}
                />
              </div>
              <div>
                <label style={filterLabelStyle}>Rationale (optional)</label>
                <input
                  type="text"
                  value={newEdgeRationale}
                  onChange={e => setNewEdgeRationale(e.target.value)}
                  placeholder="Why this edge exists"
                  style={{ padding: '4px 8px' }}
                />
              </div>
              <button
                style={{ ...btnStyle, ...(isTBD ? { background: '#aaa', cursor: 'not-allowed' } : {}) }}
                onClick={handleCreateEdge}
                disabled={isTBD}
                title={isTBD ? 'Resolution policy is TBD; freeze it in the registry first.' : undefined}
              >
                Create
              </button>
              <button style={{ ...btnStyle, background: '#eee', color: '#333' }} onClick={() => { setShowEdgeDialog(false); setEdgeError(''); }}>Cancel</button>
            </div>
            {isTBD && (
              <div style={{ color: '#bf360c', fontSize: 12, marginTop: 8 }}>
                Edge type <code>{newEdgeType}</code> has resolution_policy=TBD.
                The SAGE registry rejects creation until the policy is frozen.
              </div>
            )}
            {edgeError && <div style={{ color: '#c62828', fontSize: 12, marginTop: 8 }}>{edgeError}</div>}
          </div>
        );
      })()}
    </div>
  );
}

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
    <span style={{ padding: '2px 10px', borderRadius: 3, fontSize: 11, fontWeight: 600, background: `${color}18`, color, textTransform: 'capitalize' }}>
      {label}
    </span>
  );
}

function AnchorBadges({ edge }: { edge: Edge }) {
  const parts: string[] = [];
  if (edge.source_valid_from_version) parts.push(`src@${shortId(edge.source_valid_from_version)}`);
  if (edge.target_valid_from_version) parts.push(`tgt@${shortId(edge.target_valid_from_version)}`);
  if (parts.length === 0) return null;
  return (
    <span style={anchorBadgeStyle} title="Anchor: chain member where this edge becomes applicable">
      {parts.join(' ')}
    </span>
  );
}

function shortId(docId: string): string {
  // Heuristic short form for inline display: keep last 8 chars after a hyphen,
  // or first 8 chars if shorter than 12. Hover already shows full id via title.
  if (docId.length <= 12) return docId;
  const tail = docId.split('-').pop();
  if (tail && tail.length <= 12) return tail;
  return docId.slice(0, 8) + '...';
}

const btnStyle: React.CSSProperties = { padding: '6px 16px', border: '1px solid #ccc', borderRadius: 4, background: '#333', color: '#fff', cursor: 'pointer', fontSize: 13 };
const filterLabelStyle: React.CSSProperties = { display: 'block', fontSize: 11, color: '#666', marginBottom: 4, fontWeight: 500 };
const tdStyle: React.CSSProperties = { padding: '5px 10px', borderBottom: '1px solid #eee', fontSize: 13 };
const anchorBadgeStyle: React.CSSProperties = {
  display: 'inline-block',
  marginLeft: 8,
  padding: '0 6px',
  fontSize: 10,
  fontFamily: 'ui-monospace, monospace',
  color: '#1565c0',
  background: '#1565c018',
  borderRadius: 3,
};
const tombstoneBadgeStyle: React.CSSProperties = {
  display: 'inline-block',
  marginLeft: 8,
  padding: '0 6px',
  fontSize: 10,
  fontWeight: 600,
  color: '#6d4c41',
  background: '#6d4c4118',
  borderRadius: 3,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
};
const policyHintStyle: React.CSSProperties = {
  marginTop: 4,
  fontSize: 10,
  color: '#888',
  fontFamily: 'ui-monospace, monospace',
};
