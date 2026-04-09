import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, useNavigate, useOutletContext } from 'react-router-dom';
import { Network } from 'vis-network';
import { DataSet } from 'vis-data';
import type { VaultContext } from '../App';
import type { DocumentSummary, TraversalNode } from '../api/types';
import { traverse } from '../api/graph';
import { getDocument as fetchDocument } from '../api/documents';

// Shape mapping for doc_type
const docTypeShapes: Record<string, string> = {
  patent_draft: 'diamond',
  technical_disclosure: 'box',
  reference: 'ellipse',
  status_report: 'triangle',
  meeting_notes: 'star',
  note: 'dot',
  article: 'square',
  bookmark: 'triangleDown',
};

// Edge dash patterns by type
const edgeStyles: Record<string, { dashes: boolean | number[]; color: string }> = {
  supersedes: { dashes: false, color: '#c62828' },
  derived_from: { dashes: [10, 5], color: '#1565c0' },
  covers: { dashes: [5, 5], color: '#2e7d32' },
  bundles_with: { dashes: [2, 4], color: '#f57f17' },
  references: { dashes: [8, 3, 2, 3], color: '#6a1b9a' },
  authoritative_for: { dashes: false, color: '#00695c' },
  depends_on: { dashes: [15, 5], color: '#e65100' },
  sync_target: { dashes: [4, 4], color: '#37474f' },
};

const lifecycleOpacity: Record<string, number> = {
  active: 1.0,
  draft: 0.8,
  superseded: 0.4,
  archived: 0.3,
};

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
  const [lifecycleFilters, setLifecycleFilters] = useState<Record<string, boolean>>(() => {
    const filters: Record<string, boolean> = {};
    for (const s of vault?.lifecycle_states ?? []) filters[s.value] = true;
    return filters;
  });
  const [selectedNode, setSelectedNode] = useState<DocumentSummary | null>(null);
  const [centerNodeId, setCenterNodeId] = useState(id);
  const [traversalData, setTraversalData] = useState<TraversalNode[]>([]);
  const [centerDoc, setCenterDoc] = useState<DocumentSummary | null>(null);
  const [loading, setLoading] = useState(true);

  // Fetch traversal data from API
  useEffect(() => {
    if (!centerNodeId) return;
    setLoading(true);

    Promise.all([
      traverse(vaultId, { start_id: centerNodeId, direction: 'both', depth }),
      fetchDocument(vaultId, centerNodeId),
    ])
      .then(([resp, doc]) => {
        setTraversalData(resp.nodes);
        setCenterDoc({
          id: doc.id,
          title: doc.title,
          lifecycle_status: doc.lifecycle_status,
          source_type: doc.source_type,
          version_label: doc.version_label,
          project: doc.project,
          doc_type: doc.doc_type,
          tags: doc.tags,
        });
        setLoading(false);
      })
      .catch(() => {
        setTraversalData([]);
        setLoading(false);
      });
  }, [vaultId, centerNodeId, depth]);

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

    const visNodes = new DataSet(Array.from(nodeMap.values()).map(doc => ({
      id: doc.id,
      label: doc.title.length > 30 ? doc.title.slice(0, 27) + '...' : doc.title,
      shape: docTypeShapes[doc.doc_type ?? ''] ?? 'dot',
      opacity: lifecycleOpacity[doc.lifecycle_status] ?? 1.0,
      color: doc.id === centerNodeId ? '#ff8f00' : '#607d8b',
      font: { size: 12, color: '#333' },
      title: `${doc.title}\nType: ${doc.doc_type ?? 'unset'}\nStatus: ${doc.lifecycle_status}`,
      borderWidth: doc.id === centerNodeId ? 3 : 1,
    })));

    // Deduplicate edges and filter to edges where both endpoints are in nodeMap
    const seenEdges = new Set<string>();
    const visEdgeData = filteredEdges
      .filter(n => {
        if (seenEdges.has(n.edge.id)) return false;
        seenEdges.add(n.edge.id);
        return nodeMap.has(n.edge.source_id) && nodeMap.has(n.edge.target_id);
      })
      .map(n => {
        const style = edgeStyles[n.edge.edge_type] ?? { dashes: false, color: '#999' };
        return {
          id: n.edge.id,
          from: n.edge.source_id,
          to: n.edge.target_id,
          dashes: style.dashes,
          color: { color: style.color },
          arrows: 'to',
          label: n.edge.edge_type.replace(/_/g, ' '),
          font: { size: 9, color: '#999', strokeWidth: 0 },
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

    network.on('click', (params: { nodes: string[] }) => {
      if (params.nodes.length > 0) {
        const clickedId = params.nodes[0];
        const doc = nodeMap.get(clickedId) ?? null;
        setSelectedNode(doc);
      } else {
        setSelectedNode(null);
      }
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
          <span>Double-click = open document</span>
        </div>
      </div>
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
