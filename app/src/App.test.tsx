import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import { resolveInitialVaultId } from './activeVault';
import * as vaultsApi from './api/vaults';
import * as authApi from './api/auth';
import { ApiError, notifyAuthRequired } from './api/client';
import type { VaultSummary } from './api/types';
import type { SessionInfo, UserClaims } from './api/auth';

const pending = <T,>(): Promise<T> => new Promise<T>(() => {});

vi.mock('./api/vaults', () => ({
  listVaults: vi.fn(),
  createVault: vi.fn(() => pending()),
  getVaultStats: vi.fn(() => pending()),
  getVaultConfig: vi.fn(() => pending()),
  updateVaultConfig: vi.fn(() => pending()),
}));

vi.mock('./api/auth', () => ({
  getSession: vi.fn(),
  beginLogin: vi.fn(),
  logout: vi.fn(),
}));

const listVaultsMock = vi.mocked(vaultsApi.listVaults);
const getSessionMock = vi.mocked(authApi.getSession);
const logoutMock = vi.mocked(authApi.logout);

const AUTH_USER: UserClaims = { subject: 's', name: 'Test User', email: 'test@example.com' };
const AUTHENTICATED: SessionInfo = { authenticated: true, user: AUTH_USER };

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
  getSessionMock.mockReset();
  // Default the gate to an authenticated session so the existing vault-selection
  // tests reach the app shell. Auth-gate tests below override per case.
  getSessionMock.mockResolvedValue(AUTHENTICATED);
  logoutMock.mockReset();
  logoutMock.mockResolvedValue(undefined);
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

describe('App — auth gate (component)', () => {
  const signInButton = () => screen.queryByRole('button', { name: /sign in with microsoft/i });
  const signOutButton = () => screen.queryByRole('button', { name: /^sign out$/i });

  it('D1: an authenticated session renders the shell and fetches the vault list', async () => {
    getSessionMock.mockResolvedValue(AUTHENTICATED);
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    expect(listVaultsMock).toHaveBeenCalled();
  });

  it('D2: an unauthenticated session renders the interstitial and does NOT fetch vaults', async () => {
    getSessionMock.mockResolvedValue({ authenticated: false, user: null });

    render(<App />);
    await waitFor(() => expect(signInButton()).toBeInTheDocument());

    expect(screen.queryByRole('combobox')).toBeNull();
    expect(listVaultsMock).not.toHaveBeenCalled();
  });

  it('D3: the local profile (auth_not_configured) renders the shell, no interstitial', async () => {
    getSessionMock.mockRejectedValue(new ApiError('auth_not_configured', 'auth not configured', { status: 503 }));
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    expect(signInButton()).toBeNull();
    expect(listVaultsMock).toHaveBeenCalled();
  });

  it('D4: an unexpected session-check failure surfaces an error, not the shell or sign-in', async () => {
    getSessionMock.mockRejectedValue(new ApiError('internal_error', 'boom', { status: 500 }));

    render(<App />);
    await waitFor(() => expect(screen.getByText(/error: boom/i)).toBeInTheDocument());

    expect(screen.queryByRole('combobox')).toBeNull();
    expect(signInButton()).toBeNull();
    expect(listVaultsMock).not.toHaveBeenCalled();
  });

  it('D5: a mid-session auth_required signal returns the user to the interstitial', async () => {
    getSessionMock.mockResolvedValue(AUTHENTICATED);
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    act(() => {
      notifyAuthRequired();
    });

    await waitFor(() => expect(signInButton()).toBeInTheDocument());
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('D6: signing out ends the session and returns to the interstitial', async () => {
    getSessionMock.mockResolvedValue(AUTHENTICATED);
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    const button = signOutButton();
    expect(button).not.toBeNull();
    await userEvent.click(button as HTMLElement);

    expect(logoutMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(signInButton()).toBeInTheDocument());
  });

  it('D7: the local profile shows no sign-out control (no session to end)', async () => {
    getSessionMock.mockRejectedValue(new ApiError('auth_not_configured', 'auth not configured', { status: 503 }));
    listVaultsMock.mockResolvedValue(TWO_VAULTS);

    render(<App />);
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());

    expect(signOutButton()).toBeNull();
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
