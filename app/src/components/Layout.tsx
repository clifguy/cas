import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import type { VaultSummary } from '../api/types';
import type { UserClaims } from '../api/auth';
import type { VaultContext } from '../App';

interface LayoutProps {
  activeVault: string;
  onVaultChange: (vaultId: string) => void;
  onVaultCreated: (vaultId: string) => void;
  vaultList: VaultSummary[];
  currentVault: VaultSummary | null;
  // The signed-in user, or null in the local profile (no session to end).
  user: UserClaims | null;
  onSignOut: () => void;
}

export default function Layout({ activeVault, onVaultChange, onVaultCreated, vaultList, currentVault, user, onSignOut }: LayoutProps) {
  const ctx: VaultContext = {
    vaultId: activeVault,
    vault: currentVault,
    vaults: vaultList,
  };

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif', fontSize: 14 }}>
      <Sidebar activeVault={activeVault} onVaultChange={onVaultChange} onVaultCreated={onVaultCreated} vaultList={vaultList} />
      <main style={{ flex: 1, overflow: 'auto', padding: 24 }}>
        {user && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginBottom: 16 }}>
            <span style={{ color: '#555' }}>{user.email ?? user.name ?? user.subject}</span>
            <button
              type="button"
              onClick={onSignOut}
              style={{ padding: '4px 12px', fontSize: 13, cursor: 'pointer', border: '1px solid #ccc', borderRadius: 4, background: '#fff' }}
            >
              Sign out
            </button>
          </div>
        )}
        <Outlet context={ctx} />
      </main>
    </div>
  );
}
