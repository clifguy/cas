import { Link, useOutletContext } from 'react-router-dom';
import { vaults, getVaultStats } from '../mock/data';

export default function Dashboard() {
  const { vaultId } = useOutletContext<{ vaultId: string }>();
  const vault = vaults[vaultId];
  const stats = getVaultStats(vaultId);
  if (!vault || !stats) return <div>Vault not found.</div>;

  const { identity } = vault;

  return (
    <div>
      {/* Vault Identity */}
      <h1 style={{ margin: '0 0 4px' }}>{identity.name}</h1>
      <p style={{ margin: '0 0 4px', color: '#666' }}>{identity.description}</p>
      <p style={{ margin: '0 0 24px', color: '#999', fontSize: 12 }}>{identity.storage_root}</p>

      {/* Statistics */}
      <Section title="Statistics">
        {/* Documents */}
        <StatGroupLabel>Documents</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
          <CountCard value={stats.totalDocuments} label="Total" />
          <BreakdownCard label="By Lifecycle" data={stats.byLifecycle} linkParam="lifecycle_status" />
          <BreakdownCard label="By Doc Type" data={stats.byDocType} formatKey={k => k.replace(/_/g, ' ')} linkParam="doc_type" />
          <BreakdownCard label="By Source Adapter" data={stats.byAdapter} linkParam="source_type" />
        </div>

        {/* Edges */}
        <StatGroupLabel>Edges</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
          <CountCard value={stats.totalEdges} label="Total" />
          <BreakdownCard label="By Type" data={stats.byEdgeType} formatKey={k => k.replace(/_/g, ' ')} />
          <CountCard value={stats.stagingEdgeCount} label="Staging" />
        </div>

        {/* Storage */}
        <StatGroupLabel>Storage</StatGroupLabel>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
          <CountCard value={stats.lancedbSize} label="LanceDB" />
          <CountCard value={stats.sqliteSize} label="SQLite" />
          <CountCard value={new Date(stats.lastIngestion).toLocaleDateString()} label="Last Ingestion" />
        </div>
      </Section>

      {/* Health Indicators */}
      <Section title="Health Indicators">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          <HealthCard label="Pending metadata review" count={stats.pendingMetadata} linkTo="/review?tab=metadata" />
          <HealthCard label="Pending edge review" count={stats.pendingEdges} linkTo="/review?tab=edges" />
          <HealthCard label="Deferred abstracts" count={stats.deferredAbstracts} linkTo="/search?pipeline_status=abstraction_skipped" />
          <HealthCard label="Failed ingestions" count={stats.failedIngestions.length} linkTo="/search?pipeline_status=failed" />
        </div>
      </Section>

      {/* Adapter Registry */}
      <Section title="Adapter Registry">
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>Adapter</th>
              <th style={thStyle}>Extensions</th>
              <th style={thStyle}>Version</th>
              <th style={thStyle}>Enabled</th>
            </tr>
          </thead>
          <tbody>
            {vault.adapters.map(a => (
              <tr key={a.source_type}>
                <td style={tdStyle}>{a.source_type}</td>
                <td style={tdStyle}>{a.extensions.join(', ')}</td>
                <td style={tdStyle}>{a.version}</td>
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
