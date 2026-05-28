import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import { resolveInitialVaultId } from './activeVault';
import * as vaultsApi from './api/vaults';
import type { VaultSummary } from './api/types';

const pending = <T,>(): Promise<T> => new Promise<T>(() => {});

vi.mock('./api/vaults', () => ({
  listVaults: vi.fn(),
  createVault: vi.fn(() => pending()),
  getVaultStats: vi.fn(() => pending()),
  getVaultConfig: vi.fn(() => pending()),
  updateVaultConfig: vi.fn(() => pending()),
}));

const listVaultsMock = vi.mocked(vaultsApi.listVaults);

function makeVault(id: string, name: string): VaultSummary {
  return {
    id,
    name,
    description: null,
    storage_root: '/tmp/' + id,
    doc_types: [],
    lifecycle_states: [],
    adapters: [],
    projects: [],
  };
}

const TWO_VAULTS: VaultSummary[] = [
  makeVault('cas', 'CAS'),
  makeVault('other', 'Other'),
];

const memoryStore: Record<string, string> = {};
const localStorageMock = {
  getItem: (key: string) => (key in memoryStore ? memoryStore[key] : null),
  setItem: (key: string, value: string) => {
    memoryStore[key] = String(value);
  },
  removeItem: (key: string) => {
    delete memoryStore[key];
  },
  clear: () => {
    for (const k of Object.keys(memoryStore)) delete memoryStore[k];
  },
  key: (i: number) => Object.keys(memoryStore)[i] ?? null,
  get length() {
    return Object.keys(memoryStore).length;
  },
};

vi.stubGlobal('localStorage', localStorageMock);

beforeEach(() => {
  listVaultsMock.mockReset();
  localStorageMock.clear();
});

describe('App — vault selection persistence (component)', () => {
  it('1: persists vault id to localStorage when the user changes selection', async () => {
    listVaultsMock.mockResolvedValue(TWO_VAULTS);
    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    await userEvent.selectOptions(select, 'other');

    expect(localStorage.getItem('cas.activeVault')).toBe('other');
  });

  it('2: restores persisted vault id on mount when it exists in the loaded list', async () => {
    localStorage.setItem('cas.activeVault', 'other');
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.value).toBe('other');
  });

  it('3: falls back to vaults[0] and rewrites storage when persisted id is no longer in the list', async () => {
    localStorage.setItem('cas.activeVault', 'deleted_vault');
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.value).toBe('cas');
    expect(localStorage.getItem('cas.activeVault')).toBe('cas');
  });

  it('4: falls back to vaults[0] and writes storage when localStorage is empty', async () => {
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.value).toBe('cas');
    expect(localStorage.getItem('cas.activeVault')).toBe('cas');
  });
});

describe('resolveInitialVaultId (pure)', () => {
  const twoVaults = [makeVault('a', 'A'), makeVault('b', 'B')];

  it('5: returns the persisted id when it is present in the vault list', () => {
    expect(resolveInitialVaultId(twoVaults, 'b')).toBe('b');
  });

  it('6: returns vaults[0].id when the persisted id is not in the vault list', () => {
    expect(resolveInitialVaultId(twoVaults, 'stale')).toBe('a');
  });

  it('7: returns vaults[0].id when no persisted id is present', () => {
    expect(resolveInitialVaultId(twoVaults, null)).toBe('a');
  });

  it('8: returns the empty string when the vault list is empty', () => {
    expect(resolveInitialVaultId([], 'anything')).toBe('');
  });
});
