import { useState } from 'react';
import { NavLink, useNavigate } from 'react-router';
import type { VaultSummary } from '../api/types';
import { createVault } from '../api/vaults';

interface SidebarProps {
  activeVault: string;
  onVaultChange: (vaultId: string) => void;
  onVaultCreated: (vaultId: string) => void;
  vaultList: VaultSummary[];
}

const navItems = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/ingest', label: 'Ingest' },
  { to: '/review', label: 'Review' },
  { to: '/search', label: 'Search' },
  { to: '/maintenance', label: 'Maintenance' },
  { to: '/settings', label: 'Settings' },
];

const VAULT_ID_PATTERN = /^[a-z][a-z0-9_-]*$/;

export default function Sidebar({ activeVault, onVaultChange, onVaultCreated, vaultList }: SidebarProps) {
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [newId, setNewId] = useState('');
  const [newName, setNewName] = useState('');
  const [newOwner, setNewOwner] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  const handleCreate = async () => {
    if (!VAULT_ID_PATTERN.test(newId)) {
      setCreateError('ID must start with a lowercase letter and contain only lowercase letters, digits, hyphens, and underscores.');
      return;
    }
    if (!newName.trim()) {
      setCreateError('Name is required.');
      return;
    }
    if (!newOwner.trim()) {
      setCreateError('Owner is required.');
      return;
    }

    setCreating(true);
    setCreateError('');
    try {
      // Fetch the default config from the backend by sending a minimal config
      // that will be validated. We build it client-side matching the backend's
      // default structure.
      const defaultConfig: Record<string, unknown> = {
        vault: {
          id: newId,
          name: newName.trim(),
          owner: newOwner.trim(),
          storage_root: `~/sage_vaults/${newId}/sources`,
          brain_root: `~/sage_vaults/${newId}/brain`,
          visibility: 'personal',
        },
        document_types: {
          doc_types: [
            { value: 'document', label: 'Document', description: 'General-purpose document type.' },
            { value: 'reference', label: 'Reference', description: 'Reference material and supporting documents.' },
          ],
        },
        lifecycle: {
          base_states_required: true,
          states: [
            { value: 'active', label: 'Active' },
            { value: 'completed', label: 'Completed' },
            { value: 'archived', label: 'Archived', is_terminal: true },
          ],
          transitions: [
            { from_state: '(new)', action: 'ingest', to_state: 'active' },
            { from_state: 'active', action: 'supersede', to_state: 'archived', creates_edge: 'supersedes' },
            { from_state: 'active', action: 'complete', to_state: 'completed' },
            { from_state: 'active', action: 'archive', to_state: 'archived' },
            { from_state: 'completed', action: 'archive', to_state: 'archived' },
            { from_state: 'archived', action: 'reactivate', to_state: 'active' },
          ],
        },
        source_adapters: {
          adapters: [
            { source_type: 'markdown', enabled: true },
            { source_type: 'docx', enabled: true },
            { source_type: 'xlsx', enabled: true },
          ],
        },
        metadata_extraction: {
          filename_extraction: { separator: '_' },
        },
        edge_inference: {
          tier_assignments: [
            { edge_type: 'supersedes', tier: 1, inference_rules: [{ method: 'version_chain' }] },
          ],
        },
        abstraction: { enabled: false },
      };

      await createVault(defaultConfig);
      setShowCreate(false);
      setNewId('');
      setNewName('');
      setNewOwner('');
      onVaultCreated(newId);
      navigate('/dashboard');
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : 'Failed to create vault');
    } finally {
      setCreating(false);
    }
  };

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
        <div style={{ display: 'flex', gap: 4 }}>
          <select
            value={activeVault}
            onChange={e => {
              onVaultChange(e.target.value);
              navigate('/dashboard');
            }}
            style={{ flex: 1, padding: '4px 6px' }}
          >
            {vaultList.map(v => (
              <option key={v.id} value={v.id}>{v.name}</option>
            ))}
          </select>
          <button
            onClick={() => { setShowCreate(!showCreate); setCreateError(''); }}
            title="Create new vault"
            style={{
              padding: '4px 8px',
              border: '1px solid #ccc',
              borderRadius: 4,
              background: '#fff',
              cursor: 'pointer',
              fontSize: 14,
              fontWeight: 700,
            }}
          >
            +
          </button>
        </div>

        {showCreate && (
          <div style={{
            marginTop: 12,
            padding: 12,
            border: '1px solid #ddd',
            borderRadius: 4,
            background: '#fafafa',
          }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>New Vault</div>
            <input
              placeholder="vault_id"
              value={newId}
              onChange={e => setNewId(e.target.value)}
              style={{ ...formInput, marginBottom: 6 }}
            />
            <input
              placeholder="Display Name"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              style={{ ...formInput, marginBottom: 6 }}
            />
            <input
              placeholder="Owner"
              value={newOwner}
              onChange={e => setNewOwner(e.target.value)}
              style={{ ...formInput, marginBottom: 8 }}
            />
            {createError && (
              <div style={{ fontSize: 11, color: '#c62828', marginBottom: 6 }}>{createError}</div>
            )}
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                onClick={handleCreate}
                disabled={creating}
                style={{
                  flex: 1,
                  padding: '4px 8px',
                  border: '1px solid #333',
                  borderRadius: 4,
                  background: '#333',
                  color: '#fff',
                  cursor: 'pointer',
                  fontSize: 12,
                }}
              >
                {creating ? 'Creating...' : 'Create'}
              </button>
              <button
                onClick={() => setShowCreate(false)}
                style={{
                  padding: '4px 8px',
                  border: '1px solid #ccc',
                  borderRadius: 4,
                  background: '#fff',
                  cursor: 'pointer',
                  fontSize: 12,
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
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

const formInput: React.CSSProperties = {
  width: '100%',
  padding: '4px 6px',
  border: '1px solid #ccc',
  borderRadius: 3,
  fontSize: 12,
  boxSizing: 'border-box',
};
