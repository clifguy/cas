import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';

interface LayoutProps {
  activeVault: string;
  onVaultChange: (vaultId: string) => void;
}

export default function Layout({ activeVault, onVaultChange }: LayoutProps) {
  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif', fontSize: 14 }}>
      <Sidebar activeVault={activeVault} onVaultChange={onVaultChange} />
      <main style={{ flex: 1, overflow: 'auto', padding: 24 }}>
        <Outlet context={{ vaultId: activeVault }} />
      </main>
    </div>
  );
}
