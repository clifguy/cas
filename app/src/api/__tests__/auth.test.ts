import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiError } from '../client';
import * as client from '../client';
import { getSession, beginLogin, logout } from '../auth';
import type { SessionInfo, LoginChallenge } from '../auth';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return {
    ...actual,
    apiGet: vi.fn(),
    apiPostVoid: vi.fn(),
  };
});

const apiGetMock = vi.mocked(client.apiGet);
const apiPostVoidMock = vi.mocked(client.apiPostVoid);

beforeEach(() => {
  apiGetMock.mockReset();
  apiPostVoidMock.mockReset();
});

describe('getSession', () => {
  it('A1: returns the authenticated session and probes /app/auth/me', async () => {
    const session: SessionInfo = {
      authenticated: true,
      user: { subject: 's', name: 'N', email: 'e@example.com' },
    };
    apiGetMock.mockResolvedValue(session);

    expect(await getSession()).toEqual(session);
    expect(apiGetMock).toHaveBeenCalledWith('/app/auth/me');
  });

  it('A2: returns the unauthenticated session verbatim (no coercion)', async () => {
    const session: SessionInfo = { authenticated: false, user: null };
    apiGetMock.mockResolvedValue(session);

    expect(await getSession()).toEqual(session);
  });

  it('A3: propagates the 503 auth_not_configured ApiError rather than swallowing it', async () => {
    const err = new ApiError('auth_not_configured', 'auth not configured', { status: 503 });
    apiGetMock.mockRejectedValue(err);

    // The caller must be able to distinguish the local profile (this error) from
    // an unauthenticated cloud session (authenticated:false). Swallowing it or
    // returning authenticated:false would collapse that distinction.
    await expect(getSession()).rejects.toBe(err);
  });
});

describe('beginLogin', () => {
  it('A4: returns the authorization challenge and probes /app/auth/login', async () => {
    const challenge: LoginChallenge = {
      authorization_url: 'https://login.microsoftonline.com/x',
      state: 'st',
    };
    apiGetMock.mockResolvedValue(challenge);

    expect(await beginLogin()).toEqual(challenge);
    expect(apiGetMock).toHaveBeenCalledWith('/app/auth/login');
  });
});

describe('logout', () => {
  it('A5: posts to /app/auth/logout via the body-less helper', async () => {
    apiPostVoidMock.mockResolvedValue(undefined);

    await expect(logout()).resolves.toBeUndefined();
    expect(apiPostVoidMock).toHaveBeenCalledWith('/app/auth/logout');
  });
});
