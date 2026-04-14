import { useState, useEffect, useCallback } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './views/Dashboard';
import Ingest from './views/Ingest';
import Review from './views/Review';
import Search from './views/Search';
import Settings from './views/Settings';
import DocumentDetail from './views/DocumentDetail';
import GraphExplorer from './views/GraphExplorer';
import { listVaults } from './api/vaults';
import type { VaultSummary } from './api/types';

export interface VaultContext {
  vaultId: string;
  vault: VaultSummary | null;
  vaults: VaultSummary[];
}

export default function App() {
  const [vaultList, setVaultList] = useState<VaultSummary[]>([]);
  const [activeVault, setActiveVault] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refreshVaults = useCallback((selectVaultId?: string) => {
    listVaults()
      .then(vaults => {
        setVaultList(vaults);
        if (selectVaultId) {
          setActiveVault(selectVaultId);
        } else if (vaults.length > 0 && !activeVault) {
          setActiveVault(vaults[0].id);
        }
        setLoading(false);
      })
      .catch(err => {
        setError(err.message ?? 'Failed to load vaults');
        setLoading(false);
      });
  }, [activeVault]);

  useEffect(() => {
    refreshVaults();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return <div style={{ padding: 24, fontFamily: 'system-ui' }}>Loading vaults...</div>;
  }
  if (error) {
    return <div style={{ padding: 24, fontFamily: 'system-ui', color: '#c62828' }}>Error: {error}</div>;
  }
  if (vaultList.length === 0) {
    return <div style={{ padding: 24, fontFamily: 'system-ui' }}>No vaults configured.</div>;
  }

  const currentVault = vaultList.find(v => v.id === activeVault) ?? null;

  return (
    <BrowserRouter>
      <Routes>
        <Route element={
          <Layout
            activeVault={activeVault}
            onVaultChange={setActiveVault}
            onVaultCreated={(id: string) => refreshVaults(id)}
            vaultList={vaultList}
            currentVault={currentVault}
          />
        }>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="ingest" element={<Ingest />} />
          <Route path="review" element={<Review />} />
          <Route path="search" element={<Search />} />
          <Route path="settings" element={<Settings />} />
          <Route path="documents/:id" element={<DocumentDetail />} />
          <Route path="documents/:id/graph" element={<GraphExplorer />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
