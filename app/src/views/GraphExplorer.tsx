import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, useNavigate, useOutletContext } from 'react-router-dom';
import { Network } from 'vis-network';
import { DataSet } from 'vis-data';
import type { VaultContext } from '../App';
import type { DocumentSummary, TraversalNode, ResolutionPathEntry } from '../api/types';
import { traverse } from '../api/graph';
import { getDocument as fetchDocument } from '../api/documents';

// Shape mapping for doc_type
const docTypeShapes: Record<string, string> = {
  design_spec: 'diamond',
  technical_disclosure: 'box',
  reference: 'ellipse',
  status_report: 'triangle',
  meeting_notes: 'star',
  note: 'dot',
  article: 'square',
  bookmark: 'triangleDown',
};

// Edge dash patterns by type. retracts and merged_from are CAS-ADR-017
// meta-edges: shown but visually distinct from semantic relationships.
const edgeStyles: Record<string, { dashes: boolean | number[]; color: string }> = {
  supersedes: { dashes: false, color: '#c62828' },
  derived_from: { dashes: [10, 5], color: '#1565c0' },
  instantiated_from: { dashes: [12, 3, 3, 3], color: '#0277bd' },
  covers: { dashes: [5, 5], color: '#2e7d32' },
  bundles_with: { dashes: [2, 4], color: '#f57f17' },
  references: { dashes: [8, 3, 2, 3], color: '#6a1b9a' },
  authoritative_for: { dashes: false, color: '#00695c' },
  depends_on: { dashes: [15, 5], color: '#e65100' },
  sync_target: { dashes: [4, 4], color: '#37474f' },
  retracts: { dashes: [3, 3], color: '#b71c1c' },
  merged_from: { dashes: [12, 6], color: '#4527a0' },
};

const lifecycleOpacity: Record<string, number> = {
  active: 1.0,
  draft: 0.8,
  archived: 0.3,
};

// Decide which endpoint becomes the new center when the user clicks an edge.
// If the edge touches the current center, return the opposite endpoint.
// Otherwise default to the target, preserving the arrow direction as a cue.
export function pickEdgeEndpoint(
  edge: { from: string; to: string },
  currentCenterId: string | undefined,
): string {
  if (edge.from === currentCenterId) return edge.to;
  if (edge.to === currentCenterId) return edge.from;
  return edge.to;
}

export default function GraphExplorer() {
  const { id } = useParams<{ id: string }>();
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement>(null);
  const networkRef = useRef<Network | null>(null);

  const [depth, setDepth] = useState(2);
  const [layout, setLayout] = useState<'hierarchical' | 'force'>('hierarchical');
  const [edgeTypeFilters, setEdgeTypeFilters] = useState<Record<string, boolean>>(() => {
    const filters: Record<string, boolean> = {};
    for (const t of Object.keys(edgeStyles)) filters[t] = true;
    return filters;
  });
  const [lifecycleFilters, setLifecycleFilters] = useState<Record<string, boolean>>({});

  // Sync lifecycle filters when vault loads (initializer runs before vault is available)
  useEffect(() => {
    if (!vault) return;
    setLifecycleFilters(prev => {
      if (Object.keys(prev).length > 0) return prev;
      const filters: Record<string, boolean> = {};
      for (const s of vault.lifecycle_states) filters[s.value] = true;
      return filters;
    });
  }, [vault]);
  const [selectedNode, setSelectedNode] = useState<DocumentSummary | null>(null);
  const [centerNodeId, setCenterNodeId] = useState(id);
  const [traversalData, setTraversalData] = useState<TraversalNode[]>([]);
  const [centerDoc, setCenterDoc] = useState<DocumentSummary | null>(null);
  const [loading, setLoading] = useState(true);
  // Chain-scoped resolution debug trace (CAS-ADR-017). Off by default.
  const [debugMode, setDebugMode] = useState(false);
  const [resolutionPath, setResolutionPath] = useState<ResolutionPathEntry[]>([]);

  // Fetch traversal data from API
  useEffect(() => {
    if (!centerNodeId) return;
    setLoading(true);

    Promise.all([
      traverse(vaultId, { start_id: centerNodeId, direction: 'both', depth, debug: debugMode }),
      fetchDocument(vaultId, centerNodeId),
    ])
      .then(([resp, doc]) => {
        setTraversalData(resp.nodes);
        setResolutionPath(resp.resolution_path ?? []);
        setCenterDoc({
          id: doc.id,
          title: doc.title,
          lifecycle_status: doc.lifecycle_status,
          source_type: doc.source_type,
          source_path: doc.source_path,
          version_label: doc.version_label,
          project: doc.project,
          doc_type: doc.doc_type,
          tags: doc.tags,
          document_date: doc.document_date,
          source_modified_at: doc.source_modified_at,
        });
        setLoading(false);
      })
      .catch(() => {
        setTraversalData([]);
        setResolutionPath([]);
        setLoading(false);
      });
  }, [vaultId, centerNodeId, depth, debugMode]);

  // Build and render vis-network from traversal data
  const renderGraph = useCallback(() => {
    if (!containerRef.current || loading) return;

    // Build node and edge sets, applying client-side filters
    const nodeMap = new Map<string, DocumentSummary>();
    if (centerDoc && lifecycleFilters[centerDoc.lifecycle_status]) {
      nodeMap.set(centerDoc.id, centerDoc);
    }

    const filteredEdges = traversalData.filter(n => {
      if (!edgeTypeFilters[n.edge.edge_type]) return false;
      if (!lifecycleFilters[n.document.lifecycle_status]) return false;
      return true;
    });

    for (const n of filteredEdges) {
      nodeMap.set(n.document.id, n.document);
    }

    const visNodes = new DataSet(Array.from(nodeMap.values()).map(doc => {
      const versionSuffix = doc.version_label ? ` (${doc.version_label})` : '';
      const fullLabel = doc.title + versionSuffix;
      return {
        id: doc.id,
        label: fullLabel.length > 30 ? fullLabel.slice(0, 27) + '...' : fullLabel,
        shape: docTypeShapes[doc.doc_type ?? ''] ?? 'dot',
        opacity: lifecycleOpacity[doc.lifecycle_status] ?? 1.0,
        color: doc.id === centerNodeId ? '#ff8f00' : '#607d8b',
        font: { size: 12, color: '#333' },
        title: `${doc.title}${versionSuffix}\nType: ${doc.doc_type ?? 'unset'}\nStatus: ${doc.lifecycle_status}`,
        borderWidth: doc.id === centerNodeId ? 3 : 1,
      };
    }));

    // Deduplicate edges and filter to edges where both endpoints are in nodeMap.
    // `retracts` edges have null target_id (target is an edge, not a doc); they
    // are dropped from the graph view since vis-network needs document endpoints.
    const seenEdges = new Set<string>();
    const visEdgeData = filteredEdges
      .filter(n => {
        if (seenEdges.has(n.edge.id)) return false;
        seenEdges.add(n.edge.id);
        if (n.edge.target_id == null) return false;
        return nodeMap.has(n.edge.source_id) && nodeMap.has(n.edge.target_id);
      })
      .map(n => {
        const style = edgeStyles[n.edge.edge_type] ?? { dashes: false, color: '#999' };
        const tombstoned = n.edge.valid_until_version != null;
        return {
          id: n.edge.id,
          from: n.edge.source_id,
          // target_id is non-null here (filtered above) but TS narrowing
          // doesn't carry through .filter; cast for the network payload.
          to: n.edge.target_id as string,
          dashes: tombstoned ? [1, 4] as number[] : style.dashes,
          color: { color: style.color, opacity: tombstoned ? 0.35 : 1 },
          arrows: 'to',
          label: n.edge.edge_type.replace(/_/g, ' ') + (tombstoned ? ' (tombstoned)' : ''),
          font: { size: 9, color: tombstoned ? '#bbb' : '#999', strokeWidth: 0 },
        };
      });

    const visEdges = new DataSet(visEdgeData);

    const options: Record<string, unknown> = {
      interaction: { hover: true, tooltipDelay: 200 },
      physics: layout === 'force'
        ? { enabled: true, solver: 'forceAtlas2Based' }
        : { enabled: false },
      layout: layout === 'hierarchical'
        ? { hierarchical: { direction: 'UD', sortMethod: 'directed', levelSeparation: 120, nodeSpacing: 150 } }
        : {},
      nodes: { borderWidth: 1, borderWidthSelected: 3 },
      edges: { smooth: { type: 'cubicBezier' } },
    };

    const network = new Network(containerRef.current, { nodes: visNodes, edges: visEdges }, options);
    networkRef.current = network;

    network.on('click', (params: { nodes: string[]; edges: string[] }) => {
      if (params.nodes.length > 0) {
        const clickedId = params.nodes[0];
        const doc = nodeMap.get(clickedId) ?? null;
        setSelectedNode(doc);
        return;
      }
      if (params.edges.length > 0) {
        const edgeId = params.edges[0];
        const edgeInfo = visEdgeData.find(e => e.id === edgeId);
        if (edgeInfo) {
          const nextCenter = pickEdgeEndpoint({ from: edgeInfo.from, to: edgeInfo.to }, centerNodeId);
          if (nextCenter !== centerNodeId) {
            setSelectedNode(null);
            setCenterNodeId(nextCenter);
          }
        }
        return;
      }
      setSelectedNode(null);
    });

    network.on('doubleClick', (params: { nodes: string[] }) => {
      if (params.nodes.length > 0) {
        navigate(`/documents/${params.nodes[0]}`);
      }
    });

    return () => network.destroy();
  }, [traversalData, centerDoc, centerNodeId, layout, edgeTypeFilters, lifecycleFilters, navigate, loading]);

  useEffect(() => {
    const cleanup = renderGraph();
    return () => cleanup?.();
  }, [renderGraph]);

  if (!vault) return <div>Vault not found.</div>;
  if (loading) return <div>Loading graph...</div>;

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <a href="#" onClick={e => { e.preventDefault(); navigate(`/documents/${id}`); }} style={{ fontSize: 12, color: '#666' }}>
          &larr; Back to document
        </a>
      </div>

      <h1 style={{ margin: '0 0 16px', fontSize: 18 }}>Graph Explorer</h1>

      <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', marginBottom: 16, padding: 12, background: '#f9f9f9', border: '1px solid #eee', borderRadius: 4 }}>
        <div>
          <label style={controlLabelStyle}>Depth: {depth}</label>
          <input type="range" min={1} max={5} value={depth} onChange={e => setDepth(Number(e.target.value))} style={{ width: 100 }} />
        </div>

        <div>
          <label style={controlLabelStyle}>Layout</label>
          <select value={layout} onChange={e => setLayout(e.target.value as 'hierarchical' | 'force')} style={{ padding: '2px 6px', fontSize: 12 }}>
            <option value="hierarchical">Hierarchical</option>
            <option value="force">Force-directed</option>
          </select>
        </div>

        <div>
          <label style={controlLabelStyle}>Edge types</label>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {Object.keys(edgeStyles).map(et => (
              <label key={et} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 2 }}>
                <input type="checkbox" checked={edgeTypeFilters[et]} onChange={e => setEdgeTypeFilters({ ...edgeTypeFilters, [et]: e.target.checked })} />
                <span style={{ color: edgeStyles[et].color }}>{et.replace(/_/g, ' ')}</span>
              </label>
            ))}
          </div>
        </div>

        <div>
          <label style={controlLabelStyle}>Lifecycle</label>
          <div style={{ display: 'flex', gap: 6 }}>
            {vault.lifecycle_states.map(ls => (
              <label key={ls.value} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 2 }}>
                <input type="checkbox" checked={lifecycleFilters[ls.value]} onChange={e => setLifecycleFilters({ ...lifecycleFilters, [ls.value]: e.target.checked })} />
                {ls.label}
              </label>
            ))}
          </div>
        </div>

        <div>
          <label style={controlLabelStyle}>Resolution debug</label>
          <label style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
            <input
              type="checkbox"
              checked={debugMode}
              onChange={e => setDebugMode(e.target.checked)}
            />
            <span title="When on, traverse returns a resolution_path trace of anchor checks, retracts, and tombstones (CAS-ADR-017).">
              Debug chain resolution
            </span>
          </label>
        </div>

        <div>
          <label style={controlLabelStyle}>&nbsp;</label>
          <button
            onClick={() => { if (selectedNode) setCenterNodeId(selectedNode.id); }}
            disabled={!selectedNode}
            style={{ padding: '3px 10px', fontSize: 12, border: '1px solid #ccc', borderRadius: 3, cursor: 'pointer' }}
          >
            Re-center
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 16 }}>
        <div ref={containerRef} style={{ flex: 1, height: 500, border: '1px solid #ddd', borderRadius: 4, background: '#fff' }} />

        {selectedNode && (
          <div style={{ width: 220, border: '1px solid #ddd', borderRadius: 4, padding: 12, flexShrink: 0 }}>
            <h3 style={{ margin: '0 0 8px', fontSize: 14 }}>{selectedNode.title}</h3>
            <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}><strong>Type:</strong> {selectedNode.doc_type ?? 'unset'}</div>
            <div style={{ fontSize: 12, color: '#666', marginBottom: 12 }}><strong>Status:</strong> {selectedNode.lifecycle_status}</div>
            <a href="#" onClick={e => { e.preventDefault(); navigate(`/documents/${selectedNode.id}`); }} style={{ fontSize: 12, color: '#1565c0' }}>
              Open Document Detail
            </a>
          </div>
        )}
      </div>

      <div style={{ marginTop: 16, padding: 12, background: '#f9f9f9', border: '1px solid #eee', borderRadius: 4 }}>
        <div style={{ fontSize: 11, color: '#666', marginBottom: 4, fontWeight: 600 }}>Legend</div>
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: 11, color: '#888' }}>
          <span>Node shape = doc type</span>
          <span>Node opacity = lifecycle state</span>
          <span>Orange border = center node</span>
          <span>Edge color/dash = edge type</span>
          <span>Faded edge with dotted pattern = tombstoned (merged_from terminated)</span>
          <span>Click edge = re-center on other endpoint</span>
          <span>Double-click = open document</span>
        </div>
        <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
          Note: <code>retracts</code> edges target an edge instance (not a document)
          and are not rendered in the graph view; use the Document Detail edge
          list to inspect them.
        </div>
      </div>

      {debugMode && (
        <div style={{ marginTop: 16, padding: 12, background: '#fffde7', border: '1px solid #ffe082', borderRadius: 4 }}>
          <div style={{ fontSize: 12, color: '#5d4037', marginBottom: 6, fontWeight: 600 }}>
            Resolution path ({resolutionPath.length} {resolutionPath.length === 1 ? 'event' : 'events'})
          </div>
          {resolutionPath.length === 0 ? (
            <div style={{ fontSize: 11, color: '#888' }}>
              No chain-scoped resolution events for this traversal.
            </div>
          ) : (
            <div style={{ maxHeight: 180, overflowY: 'auto' }}>
              <table style={{ borderCollapse: 'collapse', fontSize: 11, fontFamily: 'ui-monospace, monospace' }}>
                <thead>
                  <tr style={{ color: '#666', textAlign: 'left' }}>
                    <th style={resCellStyle}>event</th>
                    <th style={resCellStyle}>edge_id</th>
                    <th style={resCellStyle}>anchor_field</th>
                    <th style={resCellStyle}>anchor_version</th>
                    <th style={resCellStyle}>retracted_edge_id</th>
                    <th style={resCellStyle}>tombstone_version</th>
                  </tr>
                </thead>
                <tbody>
                  {resolutionPath.map((entry, i) => (
                    <tr key={i} style={{ color: resEventColor(entry.event_type) }}>
                      <td style={resCellStyle}>{entry.event_type}</td>
                      <td style={resCellStyle}>{entry.edge_id}</td>
                      <td style={resCellStyle}>{entry.anchor_field ?? ''}</td>
                      <td style={resCellStyle}>{entry.anchor_version ?? ''}</td>
                      <td style={resCellStyle}>{entry.retracted_edge_id ?? ''}</td>
                      <td style={resCellStyle}>{entry.tombstone_version ?? ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const controlLabelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 10,
  color: '#666',
  marginBottom: 3,
  fontWeight: 600,
  textTransform: 'uppercase',
};

const resCellStyle: React.CSSProperties = {
  padding: '2px 8px',
  borderBottom: '1px solid #f0e9d2',
  whiteSpace: 'nowrap',
};

function resEventColor(event: ResolutionPathEntry['event_type']): string {
  switch (event) {
    case 'anchor_hit':
      return '#2e7d32';
    case 'anchor_miss':
      return '#c62828';
    case 'retracts_applied':
      return '#b71c1c';
    case 'tombstone_applied':
      return '#6d4c41';
  }
}
