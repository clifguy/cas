import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import type { VaultSummary } from '../api/types';
import type { VaultContext } from '../App';

interface LayoutProps {
  activeVault: string;
  onVaultChange: (vaultId: string) => void;
  onVaultCreated: (vaultId: string) => void;
  vaultList: VaultSummary[];
  currentVault: VaultSummary | null;
}

export default function Layout({ activeVault, onVaultChange, onVaultCreated, vaultList, currentVault }: LayoutProps) {
  const ctx: VaultContext = {
    vaultId: activeVault,
    vault: currentVault,
    vaults: vaultList,
  };

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif', fontSize: 14 }}>
      <Sidebar activeVault={activeVault} onVaultChange={onVaultChange} onVaultCreated={onVaultCreated} vaultList={vaultList} />
      <main style={{ flex: 1, overflow: 'auto', padding: 24 }}>
        <Outlet context={ctx} />
      </main>
    </div>
  );
}
