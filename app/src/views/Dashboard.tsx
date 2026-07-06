import { useState, useEffect } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import type { VaultContext } from '../App';
import type { LastOptimizeSummary, VaultStats } from '../api/types';
import { getVaultStats } from '../api/vaults';
import BloatIndicator from '../components/BloatIndicator';
import { formatBytes } from '../utils/format';

function formatCount(n: number): string {
  return n.toLocaleString();
}

export default function Dashboard() {
  const { vaultId, vault } = useOutletContext<VaultContext>();
  const [stats, setStats] = useState<VaultStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError('');
      try {
        const data = await getVaultStats(vaultId);
        setStats(data);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load stats');
        setLoading(false);
      }
    }
    load();
  }, [vaultId]);

  if (!vault) return <div>Vault not found.</div>;
  if (loading) return <div>Loading statistics...</div>;
  if (error) return <div style={{ color: '#c62828' }}>Error: {error}</div>;
  if (!stats) return null;

  return (
    <div>
      {/* Vault Identity */}
      <h1 style={{ margin: '0 0 4px' }}>{vault.name}</h1>
      <p style={{ margin: '0 0 4px', color: '#666' }}>{vault.description}</p>
      <p style={{ margin: '0 0 24px', color: '#999', fontSize: 12 }}>{vault.storage_root}</p>

      {/* Statistics */}
      <Section title="Statistics">
        {/* Documents */}
        <StatGroupLabel>Documents</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
          <CountCard value={stats.total_documents} label="Total" />
          <BreakdownCard label="By Lifecycle" data={stats.by_lifecycle_status} linkParam="lifecycle_status" />
          <BreakdownCard label="By Doc Type" data={stats.by_doc_type} formatKey={k => k.replace(/_/g, ' ')} linkParam="doc_type" />
          <BreakdownCard label="By Source Type" data={stats.by_source_type} />
        </div>

        {/* Edges */}
        <StatGroupLabel>Edges</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
          <CountCard value={stats.total_edges} label="Total" />
          <BreakdownCard label="By Type" data={stats.by_edge_type} formatKey={k => k.replace(/_/g, ' ')} />
          <CountCard value={stats.staging_edge_count} label="Staging" />
        </div>

        {/* Storage */}
        <StatGroupLabel>Storage</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          <CountCard value={formatCount(stats.content_store_chunk_count)} label="Content Store Chunks" />
          <CountCard value={formatBytes(stats.content_store_size_bytes)} label="Content Store" />
          <CountCard value={formatBytes(stats.graph_store_size_bytes)} label="Graph Store" />
          <CountCard
            value={stats.last_ingestion_at ? new Date(stats.last_ingestion_at).toLocaleDateString() : '-'}
            label="Last Ingestion"
          />
        </div>
      </Section>

      {/* Health Indicators */}
      <Section title="Health Indicators">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          <HealthCard label="Pending metadata review" count={stats.health.pending_metadata_count} linkTo="/review?tab=metadata" />
          <HealthCard label="Pending edge review" count={stats.health.pending_edge_count} linkTo="/review?tab=edges" />
          {stats.health.deferred_abstract_count != null ? (
            <HealthCard label="Deferred abstracts" count={stats.health.deferred_abstract_count} linkTo="/search?pipeline_status=abstraction_skipped" />
          ) : (
            <div style={{
              border: '1px solid #ddd',
              borderRadius: 4,
              padding: '12px 16px',
              background: '#f5f5f5',
            }}>
              <div style={{ fontSize: 24, fontWeight: 700, color: '#999' }}>N/A</div>
              <div style={{ fontSize: 12, color: '#666' }}>Abstracts disabled</div>
            </div>
          )}
          <HealthCard label="Failed ingestions" count={stats.health.failed_ingestion_count} linkTo="/search?pipeline_status=failed" />
          <BloatIndicator
            deadTuples={stats.content_store_version_count}
            liveChunks={stats.content_store_chunk_count}
            freePages={stats.content_store_small_fragment_count}
          />
          <LastOptimizeCard summary={stats.last_optimize} />
        </div>
      </Section>

      {/* Adapter Registry */}
      <Section title="Adapter Registry">
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>Adapter</th>
              <th style={thStyle}>Extensions</th>
              <th style={thStyle}>Enabled</th>
            </tr>
          </thead>
          <tbody>
            {vault.adapters.map(a => (
              <tr key={a.source_type}>
                <td style={tdStyle}>{a.source_type}</td>
                <td style={tdStyle}>{a.extensions.join(', ')}</td>
                <td style={tdStyle}>{a.enabled ? 'Yes' : 'No'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  );
}

// -- Sub-components --

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h2 style={{ fontSize: 16, borderBottom: '1px solid #ddd', paddingBottom: 4, marginBottom: 12 }}>{title}</h2>
      {children}
    </div>
  );
}

function StatGroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', color: '#888', marginBottom: 6, letterSpacing: 0.5 }}>
      {children}
    </div>
  );
}

function CountCard({ value, label }: { value: string | number; label: string }) {
  return (
    <div style={statCardStyle}>
      <div style={{ fontSize: 22, fontWeight: 700 }}>{value}</div>
      <div style={{ fontSize: 12, color: '#666' }}>{label}</div>
    </div>
  );
}

function BreakdownCard({ label, data, formatKey, linkParam }: { label: string; data: Record<string, number>; formatKey?: (k: string) => string; linkParam?: string }) {
  const fmt = formatKey ?? (k => k);
  return (
    <div style={statCardStyle}>
      <div style={{ fontSize: 12, color: '#666', marginBottom: 6, fontWeight: 500 }}>{label}</div>
      {Object.entries(data).map(([k, v]) => (
        <div key={k} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '1px 0' }}>
          {linkParam ? (
            <Link to={`/search?${linkParam}=${encodeURIComponent(k)}`} style={{ color: '#1565c0', textDecoration: 'none', textTransform: 'capitalize' }}>
              {fmt(k)}
            </Link>
          ) : (
            <span style={{ color: '#444', textTransform: 'capitalize' }}>{fmt(k)}</span>
          )}
          <span style={{ fontWeight: 600 }}>{v}</span>
        </div>
      ))}
    </div>
  );
}

function LastOptimizeCard({ summary }: { summary: LastOptimizeSummary | null }) {
  return (
    <div
      data-testid="last-optimize-card"
      style={{
        border: '1px solid #ddd',
        borderRadius: 4,
        padding: '12px 16px',
        background: '#f5f5f5',
        height: '100%',
        boxSizing: 'border-box',
      }}
    >
      {summary ? (
        <>
          <div style={{ fontSize: 18, fontWeight: 700 }}>
            {new Date(summary.at).toLocaleDateString()}
          </div>
          <div style={{ fontSize: 12, color: '#666' }}>
            Last optimized · reclaimed {formatBytes(summary.bytes_reclaimed)}
          </div>
        </>
      ) : (
        <>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#999' }}>Never</div>
          <div style={{ fontSize: 12, color: '#666' }}>Last optimized</div>
        </>
      )}
    </div>
  );
}

function HealthCard({ label, count, linkTo }: { label: string; count: number; linkTo: string }) {
  return (
    <Link to={linkTo} style={{ textDecoration: 'none', color: 'inherit' }}>
      <div style={{
        border: '1px solid #ddd',
        borderRadius: 4,
        padding: '12px 16px',
        background: count > 0 ? '#fff8e1' : '#f5f5f5',
        height: '100%',
        boxSizing: 'border-box',
      }}>
        <div style={{ fontSize: 24, fontWeight: 700 }}>{count}</div>
        <div style={{ fontSize: 12, color: '#666' }}>{label}</div>
      </div>
    </Link>
  );
}

// -- Styles --

const statCardStyle: React.CSSProperties = {
  border: '1px solid #ddd',
  borderRadius: 4,
  padding: '12px 16px',
  background: '#fafafa',
};

const tableStyle: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
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
