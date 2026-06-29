import { useState, useEffect, useCallback } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import SignIn from './components/SignIn';
import Dashboard from './views/Dashboard';
import Ingest from './views/Ingest';
import Review from './views/Review';
import Search from './views/Search';
import Settings from './views/Settings';
import MaintenancePanel from './components/MaintenancePanel';
import DocumentDetail from './views/DocumentDetail';
import GraphExplorer from './views/GraphExplorer';
import { listVaults } from './api/vaults';
import type { VaultSummary } from './api/types';
import type { UserClaims } from './api/auth';
import { VAULT_STORAGE_KEY, resolveInitialVaultId } from './activeVault';
import { useSession } from './useSession';

export interface VaultContext {
  vaultId: string;
  vault: VaultSummary | null;
  vaults: VaultSummary[];
}

interface AppShellProps {
  // The signed-in user, or null in the local profile (no session, no sign-out).
  user: UserClaims | null;
  onSignOut: () => void;
}

/**
 * The authenticated application: vault list, selection, and routed views. Mounts
 * only once the auth gate has resolved to a session (or the local profile), so
 * the vault list is never fetched before sign-in.
 */
function AppShell({ user, onSignOut }: AppShellProps) {
  const [vaultList, setVaultList] = useState<VaultSummary[]>([]);
  const [activeVault, setActiveVault] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const persistAndSetActiveVault = useCallback((id: string) => {
    localStorage.setItem(VAULT_STORAGE_KEY, id);
    setActiveVault(id);
  }, []);

  const refreshVaults = useCallback((selectVaultId?: string) => {
    listVaults()
      .then(vaults => {
        setVaultList(vaults);
        if (selectVaultId) {
          persistAndSetActiveVault(selectVaultId);
        } else if (vaults.length > 0 && !activeVault) {
          const persisted = localStorage.getItem(VAULT_STORAGE_KEY);
          persistAndSetActiveVault(resolveInitialVaultId(vaults, persisted));
        }
        setLoading(false);
      })
      .catch(err => {
        setError(err.message ?? 'Failed to load vaults');
        setLoading(false);
      });
  }, [activeVault, persistAndSetActiveVault]);

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
            onVaultChange={persistAndSetActiveVault}
            onVaultCreated={(id: string) => refreshVaults(id)}
            vaultList={vaultList}
            currentVault={currentVault}
            user={user}
            onSignOut={onSignOut}
          />
        }>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="ingest" element={<Ingest />} />
          <Route path="review" element={<Review />} />
          <Route path="search" element={<Search />} />
          <Route path="maintenance" element={<MaintenancePanel />} />
          <Route path="settings" element={<Settings />} />
          <Route path="documents/:id" element={<DocumentDetail />} />
          <Route path="documents/:id/graph" element={<GraphExplorer />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

/**
 * Auth gate. Resolves the session before the app loads any data: an
 * unauthenticated cloud user gets the sign-in interstitial; the local profile
 * and an authenticated session both render the app.
 */
export default function App() {
  const { status, user, error, signOut } = useSession();

  if (status === 'loading') {
    return <div style={{ padding: 24, fontFamily: 'system-ui' }}>Checking session...</div>;
  }
  if (status === 'error') {
    return <div style={{ padding: 24, fontFamily: 'system-ui', color: '#c62828' }}>Error: {error}</div>;
  }
  if (status === 'unauthenticated') {
    return <SignIn />;
  }

  // authenticated | local -- the local profile has no session, hence no user.
  return <AppShell user={status === 'authenticated' ? user : null} onSignOut={signOut} />;
}
