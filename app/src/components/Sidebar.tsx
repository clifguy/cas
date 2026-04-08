import { NavLink, useNavigate } from 'react-router-dom';
import { vaults } from '../mock/data';

interface SidebarProps {
  activeVault: string;
  onVaultChange: (vaultId: string) => void;
}

const navItems = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/ingest', label: 'Ingest' },
  { to: '/review', label: 'Review' },
  { to: '/search', label: 'Search' },
];

export default function Sidebar({ activeVault, onVaultChange }: SidebarProps) {
  const navigate = useNavigate();

  return (
    <aside style={{
      width: 220,
      borderRight: '1px solid #ccc',
      padding: '16px 12px',
      display: 'flex',
      flexDirection: 'column',
      gap: 24,
      flexShrink: 0,
    }}>
      <div>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 16 }}>CAS</div>
        <label style={{ fontSize: 12, color: '#666', display: 'block', marginBottom: 4 }}>Vault</label>
        <select
          value={activeVault}
          onChange={e => {
            onVaultChange(e.target.value);
            navigate('/dashboard');
          }}
          style={{ width: '100%', padding: '4px 6px' }}
        >
          {Object.entries(vaults).map(([id, v]) => (
            <option key={id} value={id}>{v.identity.name}</option>
          ))}
        </select>
      </div>

      <nav style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {navItems.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            style={({ isActive }) => ({
              display: 'block',
              padding: '8px 12px',
              textDecoration: 'none',
              color: isActive ? '#000' : '#555',
              fontWeight: isActive ? 600 : 400,
              background: isActive ? '#e8e8e8' : 'transparent',
              borderRadius: 4,
            })}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
