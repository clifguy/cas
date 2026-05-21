import { useState, useEffect, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import type { VaultContext } from '../App';
import type {
  VaultConfig,
  VaultIdentityConfig,
  DocTypeConfig,
  LifecycleStateConfig,
  LifecycleTransitionConfig,
  AbstractionConfig,
} from '../api/types';
import { getVaultConfig, updateVaultConfig } from '../api/vaults';
import MaintenancePanel from '../components/MaintenancePanel';

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

const TABS = [
  { key: 'identity', label: 'Identity' },
  { key: 'document_types', label: 'Document Types' },
  { key: 'lifecycle', label: 'Lifecycle' },
  { key: 'source_adapters', label: 'Source Adapters' },
  { key: 'metadata_extraction', label: 'Metadata Extraction' },
  { key: 'edge_inference', label: 'Edge Inference' },
  { key: 'abstraction', label: 'Abstraction' },
  { key: 'maintenance', label: 'Maintenance' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function Settings() {
  const { vaultId } = useOutletContext<VaultContext>();
  const [config, setConfig] = useState<VaultConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState<TabKey>('identity');
  const [editingSection, setEditingSection] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [successMsg, setSuccessMsg] = useState('');

  const fetchConfig = useCallback(() => {
    setLoading(true);
    setError('');
    getVaultConfig(vaultId)
      .then(data => { setConfig(data); setLoading(false); })
      .catch(err => { setError(err.message ?? 'Failed to load config'); setLoading(false); });
  }, [vaultId]);

  useEffect(() => { fetchConfig(); }, [fetchConfig]);

  const handleSave = async (sectionKey: string, sectionData: unknown) => {
    setSaving(true);
    setSaveError('');
    setWarnings([]);
    setSuccessMsg('');
    try {
      const resp = await updateVaultConfig(vaultId, { [sectionKey]: sectionData } as Partial<VaultConfig>);
      if (resp.warnings.length > 0) {
        setWarnings(resp.warnings);
      }
      setSuccessMsg('Configuration saved.');
      setEditingSection(null);
      fetchConfig();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Save failed';
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  };

  const startEditing = (section: string) => {
    setEditingSection(section);
    setSaveError('');
    setWarnings([]);
    setSuccessMsg('');
  };

  const cancelEditing = () => {
    setEditingSection(null);
    setSaveError('');
    setWarnings([]);
    fetchConfig();
  };

  if (loading) return <div>Loading configuration...</div>;
  if (error) return <div style={{ color: '#c62828' }}>Error: {error}</div>;
  if (!config) return null;

  return (
    <div>
      <h1 style={{ margin: '0 0 24px' }}>Settings</h1>

      {/* Feedback messages */}
      {successMsg && <div style={successStyle}>{successMsg}</div>}
      {warnings.length > 0 && (
        <div style={warningStyle}>
          <strong>Warnings:</strong>
          <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
            {warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
      {saveError && <div style={errorStyle}>Error: {saveError}</div>}

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid #ddd', marginBottom: 20 }}>
        {TABS.map(tab => (
          <button
            key={tab.key}
            onClick={() => { setActiveTab(tab.key); cancelEditing(); }}
            style={{
              padding: '8px 16px',
              border: 'none',
              borderBottom: activeTab === tab.key ? '2px solid #333' : '2px solid transparent',
              background: 'none',
              fontWeight: activeTab === tab.key ? 600 : 400,
              color: activeTab === tab.key ? '#000' : '#666',
              cursor: 'pointer',
              fontSize: 13,
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === 'identity' && (
        <IdentityEditor
          config={config.vault}
          editing={editingSection === 'vault'}
          onEdit={() => startEditing('vault')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('vault', data)}
          saving={saving}
        />
      )}
      {activeTab === 'document_types' && (
        <DocTypesEditor
          docTypes={config.document_types.doc_types}
          editing={editingSection === 'document_types'}
          onEdit={() => startEditing('document_types')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('document_types', { doc_types: data })}
          saving={saving}
        />
      )}
      {activeTab === 'lifecycle' && (
        <LifecycleEditor
          lifecycle={config.lifecycle}
          editing={editingSection === 'lifecycle'}
          onEdit={() => startEditing('lifecycle')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('lifecycle', data)}
          saving={saving}
        />
      )}
      {activeTab === 'source_adapters' && (
        <JsonEditor
          label="Source Adapters"
          data={config.source_adapters}
          editing={editingSection === 'source_adapters'}
          onEdit={() => startEditing('source_adapters')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('source_adapters', data)}
          saving={saving}
        />
      )}
      {activeTab === 'metadata_extraction' && (
        <JsonEditor
          label="Metadata Extraction"
          data={config.metadata_extraction}
          editing={editingSection === 'metadata_extraction'}
          onEdit={() => startEditing('metadata_extraction')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('metadata_extraction', data)}
          saving={saving}
        />
      )}
      {activeTab === 'edge_inference' && (
        <JsonEditor
          label="Edge Inference"
          data={config.edge_inference}
          editing={editingSection === 'edge_inference'}
          onEdit={() => startEditing('edge_inference')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('edge_inference', data)}
          saving={saving}
        />
      )}
      {activeTab === 'abstraction' && (
        <AbstractionEditor
          config={config.abstraction}
          editing={editingSection === 'abstraction'}
          onEdit={() => startEditing('abstraction')}
          onCancel={cancelEditing}
          onSave={(data) => handleSave('abstraction', data)}
          saving={saving}
        />
      )}
      {activeTab === 'maintenance' && <MaintenancePanel />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section editor components
// ---------------------------------------------------------------------------

interface EditorProps {
  editing: boolean;
  onEdit: () => void;
  onCancel: () => void;
  saving: boolean;
}

function EditControls({ editing, onEdit, onCancel, saving, onSave }: EditorProps & { onSave: () => void }) {
  if (!editing) {
    return <button onClick={onEdit} style={btnStyle}>Edit</button>;
  }
  return (
    <div style={{ display: 'flex', gap: 8 }}>
      <button onClick={onSave} disabled={saving} style={btnPrimaryStyle}>
        {saving ? 'Saving...' : 'Save'}
      </button>
      <button onClick={onCancel} disabled={saving} style={btnStyle}>Cancel</button>
    </div>
  );
}

// --- Identity ---

function IdentityEditor({
  config, editing, onEdit, onCancel, onSave, saving,
}: EditorProps & { config: VaultIdentityConfig; onSave: (data: VaultIdentityConfig) => void }) {
  const [draft, setDraft] = useState<VaultIdentityConfig>(config);
  useEffect(() => { setDraft(config); }, [config]);

  const update = (field: keyof VaultIdentityConfig, value: string | null) => {
    setDraft(prev => ({ ...prev, [field]: value }));
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Vault Identity</h2>
        <EditControls editing={editing} onEdit={onEdit} onCancel={onCancel} saving={saving} onSave={() => onSave(draft)} />
      </div>
      <table style={tableStyle}>
        <tbody>
          <FieldRow label="ID" value={config.id} />
          {editing ? (
            <>
              <EditableFieldRow label="Name" value={draft.name} onChange={v => update('name', v)} />
              <EditableFieldRow label="Description" value={draft.description ?? ''} onChange={v => update('description', v || null)} />
              <EditableFieldRow label="Owner" value={draft.owner} onChange={v => update('owner', v)} />
              <EditableFieldRow label="Storage Root" value={draft.storage_root} onChange={v => update('storage_root', v)} />
              <EditableFieldRow label="Brain Root" value={draft.brain_root} onChange={v => update('brain_root', v)} />
              <tr>
                <td style={tdLabelStyle}>Visibility</td>
                <td style={tdStyle}>
                  <select value={draft.visibility} onChange={e => update('visibility', e.target.value)} style={inputStyle}>
                    <option value="personal">personal</option>
                    <option value="team">team</option>
                    <option value="org">org</option>
                  </select>
                </td>
              </tr>
              <EditableFieldRow
                label="Timezone"
                value={draft.timezone}
                onChange={v => update('timezone', v)}
              />
            </>
          ) : (
            <>
              <FieldRow label="Name" value={config.name} />
              <FieldRow label="Description" value={config.description ?? ''} />
              <FieldRow label="Owner" value={config.owner} />
              <FieldRow label="Storage Root" value={config.storage_root} />
              <FieldRow label="Brain Root" value={config.brain_root} />
              <FieldRow label="Visibility" value={config.visibility} />
              <FieldRow label="Timezone" value={config.timezone} />
            </>
          )}
        </tbody>
      </table>
    </div>
  );
}

// --- Document Types ---

function DocTypesEditor({
  docTypes, editing, onEdit, onCancel, onSave, saving,
}: EditorProps & { docTypes: DocTypeConfig[]; onSave: (data: DocTypeConfig[]) => void }) {
  const [draft, setDraft] = useState<DocTypeConfig[]>(docTypes);
  useEffect(() => { setDraft(docTypes); }, [docTypes]);

  const updateRow = (idx: number, field: keyof DocTypeConfig, value: string) => {
    setDraft(prev => prev.map((row, i) => i === idx ? { ...row, [field]: value } : row));
  };
  const addRow = () => {
    setDraft(prev => [...prev, { value: '', label: '', description: '' }]);
  };
  const removeRow = (idx: number) => {
    setDraft(prev => prev.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Document Types</h2>
        <EditControls editing={editing} onEdit={onEdit} onCancel={onCancel} saving={saving} onSave={() => onSave(draft)} />
      </div>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Value</th>
            <th style={thStyle}>Label</th>
            <th style={thStyle}>Description</th>
            <th style={thStyle}>Source Types</th>
            {editing && <th style={thStyle}></th>}
          </tr>
        </thead>
        <tbody>
          {(editing ? draft : docTypes).map((dt, i) => (
            <tr key={i}>
              {editing ? (
                <>
                  <td style={tdStyle}><input style={inputStyle} value={dt.value} onChange={e => updateRow(i, 'value', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={dt.label} onChange={e => updateRow(i, 'label', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={dt.description ?? ''} onChange={e => updateRow(i, 'description', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={(dt.source_types ?? []).join(', ')} onChange={e => updateRow(i, 'source_types' as keyof DocTypeConfig, e.target.value)} /></td>
                  <td style={tdStyle}><button onClick={() => removeRow(i)} style={btnDangerStyle}>Remove</button></td>
                </>
              ) : (
                <>
                  <td style={tdStyle}><code>{dt.value}</code></td>
                  <td style={tdStyle}>{dt.label}</td>
                  <td style={tdStyle}>{dt.description ?? ''}</td>
                  <td style={tdStyle}>{(dt.source_types ?? []).join(', ')}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {editing && (
        <button onClick={addRow} style={{ ...btnStyle, marginTop: 8 }}>+ Add Type</button>
      )}
    </div>
  );
}

// --- Lifecycle ---

function LifecycleEditor({
  lifecycle, editing, onEdit, onCancel, onSave, saving,
}: EditorProps & {
  lifecycle: { base_states_required: boolean; states: LifecycleStateConfig[]; transitions: LifecycleTransitionConfig[] };
  onSave: (data: typeof lifecycle) => void;
}) {
  const [states, setStates] = useState(lifecycle.states);
  const [transitions, setTransitions] = useState(lifecycle.transitions);
  useEffect(() => { setStates(lifecycle.states); setTransitions(lifecycle.transitions); }, [lifecycle]);

  const updateState = (idx: number, field: string, value: string | boolean) => {
    setStates(prev => prev.map((s, i) => i === idx ? { ...s, [field]: value } : s));
  };
  const addState = () => setStates(prev => [...prev, { value: '', label: '' }]);
  const removeState = (idx: number) => setStates(prev => prev.filter((_, i) => i !== idx));

  const updateTransition = (idx: number, field: string, value: string) => {
    setTransitions(prev => prev.map((t, i) => i === idx ? { ...t, [field]: value } : t));
  };
  const addTransition = () => setTransitions(prev => [...prev, { from_state: '', action: '', to_state: '' }]);
  const removeTransition = (idx: number) => setTransitions(prev => prev.filter((_, i) => i !== idx));

  const handleSave = () => {
    onSave({ base_states_required: lifecycle.base_states_required, states, transitions });
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Lifecycle</h2>
        <EditControls editing={editing} onEdit={onEdit} onCancel={onCancel} saving={saving} onSave={handleSave} />
      </div>

      <h3 style={{ margin: '0 0 8px' }}>States</h3>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Value</th>
            <th style={thStyle}>Label</th>
            <th style={thStyle}>Terminal</th>
            {editing && <th style={thStyle}></th>}
          </tr>
        </thead>
        <tbody>
          {(editing ? states : lifecycle.states).map((s, i) => (
            <tr key={i}>
              {editing ? (
                <>
                  <td style={tdStyle}><input style={inputStyle} value={s.value} onChange={e => updateState(i, 'value', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={s.label} onChange={e => updateState(i, 'label', e.target.value)} /></td>
                  <td style={tdStyle}><input type="checkbox" checked={s.is_terminal ?? false} onChange={e => updateState(i, 'is_terminal', e.target.checked)} /></td>
                  <td style={tdStyle}><button onClick={() => removeState(i)} style={btnDangerStyle}>Remove</button></td>
                </>
              ) : (
                <>
                  <td style={tdStyle}><code>{s.value}</code></td>
                  <td style={tdStyle}>{s.label}</td>
                  <td style={tdStyle}>{s.is_terminal ? 'Yes' : ''}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {editing && (
        <button onClick={addState} style={{ ...btnStyle, marginTop: 8, marginBottom: 24 }}>+ Add State</button>
      )}

      <h3 style={{ margin: '24px 0 8px' }}>Transitions</h3>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>From State</th>
            <th style={thStyle}>Action</th>
            <th style={thStyle}>To State</th>
            <th style={thStyle}>Creates Edge</th>
            {editing && <th style={thStyle}></th>}
          </tr>
        </thead>
        <tbody>
          {(editing ? transitions : lifecycle.transitions).map((t, i) => (
            <tr key={i}>
              {editing ? (
                <>
                  <td style={tdStyle}><input style={inputStyle} value={t.from_state} onChange={e => updateTransition(i, 'from_state', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={t.action} onChange={e => updateTransition(i, 'action', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={t.to_state} onChange={e => updateTransition(i, 'to_state', e.target.value)} /></td>
                  <td style={tdStyle}><input style={inputStyle} value={t.creates_edge ?? ''} onChange={e => updateTransition(i, 'creates_edge', e.target.value)} /></td>
                  <td style={tdStyle}><button onClick={() => removeTransition(i)} style={btnDangerStyle}>Remove</button></td>
                </>
              ) : (
                <>
                  <td style={tdStyle}><code>{t.from_state}</code></td>
                  <td style={tdStyle}>{t.action}</td>
                  <td style={tdStyle}><code>{t.to_state}</code></td>
                  <td style={tdStyle}>{t.creates_edge ?? ''}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {editing && (
        <button onClick={addTransition} style={{ ...btnStyle, marginTop: 8 }}>+ Add Transition</button>
      )}
    </div>
  );
}

// --- JSON editor for complex sections ---

function JsonEditor({
  label, data, editing, onEdit, onCancel, onSave, saving,
}: EditorProps & { label: string; data: Record<string, unknown>; onSave: (data: Record<string, unknown>) => void }) {
  const [text, setText] = useState('');
  const [parseError, setParseError] = useState('');

  useEffect(() => {
    setText(JSON.stringify(data, null, 2));
    setParseError('');
  }, [data]);

  const handleSave = () => {
    try {
      const parsed = JSON.parse(text);
      setParseError('');
      onSave(parsed);
    } catch {
      setParseError('Invalid JSON');
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>{label}</h2>
        <EditControls editing={editing} onEdit={onEdit} onCancel={onCancel} saving={saving} onSave={handleSave} />
      </div>
      {parseError && <div style={errorStyle}>{parseError}</div>}
      <textarea
        value={text}
        onChange={e => setText(e.target.value)}
        readOnly={!editing}
        style={{
          width: '100%',
          minHeight: 300,
          fontFamily: 'monospace',
          fontSize: 13,
          padding: 12,
          border: '1px solid #ddd',
          borderRadius: 4,
          background: editing ? '#fff' : '#fafafa',
          resize: 'vertical',
          boxSizing: 'border-box',
        }}
      />
    </div>
  );
}

// --- Abstraction ---

function AbstractionEditor({
  config, editing, onEdit, onCancel, onSave, saving,
}: EditorProps & { config: AbstractionConfig; onSave: (data: AbstractionConfig) => void }) {
  const [draft, setDraft] = useState<AbstractionConfig>(config);
  useEffect(() => { setDraft(config); }, [config]);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Abstraction</h2>
        <EditControls editing={editing} onEdit={onEdit} onCancel={onCancel} saving={saving} onSave={() => onSave(draft)} />
      </div>
      <table style={tableStyle}>
        <tbody>
          {editing ? (
            <>
              <tr>
                <td style={tdLabelStyle}>Enabled</td>
                <td style={tdStyle}>
                  <input type="checkbox" checked={draft.enabled} onChange={e => setDraft(prev => ({ ...prev, enabled: e.target.checked }))} />
                </td>
              </tr>
              <EditableFieldRow label="Model" value={draft.model ?? ''} onChange={v => setDraft(prev => ({ ...prev, model: v || null }))} />
              <tr>
                <td style={tdLabelStyle}>Max Abstract Tokens</td>
                <td style={tdStyle}>
                  <input
                    type="number"
                    style={inputStyle}
                    value={draft.max_abstract_tokens}
                    onChange={e => setDraft(prev => ({ ...prev, max_abstract_tokens: parseInt(e.target.value) || 0 }))}
                  />
                </td>
              </tr>
            </>
          ) : (
            <>
              <FieldRow label="Enabled" value={config.enabled ? 'Yes' : 'No'} />
              <FieldRow label="Model" value={config.model ?? '(none)'} />
              <FieldRow label="Max Abstract Tokens" value={String(config.max_abstract_tokens)} />
            </>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared sub-components
// ---------------------------------------------------------------------------

function FieldRow({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td style={tdLabelStyle}>{label}</td>
      <td style={tdStyle}>{value}</td>
    </tr>
  );
}

function EditableFieldRow({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <tr>
      <td style={tdLabelStyle}>{label}</td>
      <td style={tdStyle}><input style={inputStyle} value={value} onChange={e => onChange(e.target.value)} /></td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const tableStyle: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  marginBottom: 8,
};

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '8px 12px',
  borderBottom: '2px solid #ddd',
  fontSize: 12,
  fontWeight: 600,
  color: '#666',
};

const tdStyle: React.CSSProperties = {
  padding: '6px 12px',
  borderBottom: '1px solid #eee',
  verticalAlign: 'top',
};

const tdLabelStyle: React.CSSProperties = {
  ...tdStyle,
  fontWeight: 600,
  width: 180,
  color: '#444',
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '4px 8px',
  border: '1px solid #ccc',
  borderRadius: 3,
  fontSize: 13,
  boxSizing: 'border-box',
};

const btnStyle: React.CSSProperties = {
  padding: '6px 16px',
  border: '1px solid #ccc',
  borderRadius: 4,
  background: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};

const btnPrimaryStyle: React.CSSProperties = {
  ...btnStyle,
  background: '#333',
  color: '#fff',
  borderColor: '#333',
};

const btnDangerStyle: React.CSSProperties = {
  padding: '2px 8px',
  border: '1px solid #c62828',
  borderRadius: 3,
  background: '#fff',
  color: '#c62828',
  cursor: 'pointer',
  fontSize: 12,
};

const successStyle: React.CSSProperties = {
  padding: '8px 12px',
  marginBottom: 16,
  background: '#e8f5e9',
  color: '#2e7d32',
  borderRadius: 4,
  fontSize: 13,
};

const warningStyle: React.CSSProperties = {
  padding: '8px 12px',
  marginBottom: 16,
  background: '#fff3e0',
  color: '#e65100',
  borderRadius: 4,
  fontSize: 13,
};

const errorStyle: React.CSSProperties = {
  padding: '8px 12px',
  marginBottom: 16,
  background: '#fce4ec',
  color: '#c62828',
  borderRadius: 4,
  fontSize: 13,
};
