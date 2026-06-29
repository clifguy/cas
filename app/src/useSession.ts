import { useEffect, useState } from 'react';
import { ApiError, onAuthRequired } from './api/client';
import { getSession, logout, type UserClaims } from './api/auth';

// The auth gate's resolved state.
//   loading        -- the initial /app/auth/me check is in flight.
//   authenticated  -- a live session; render the app with the user.
//   unauthenticated-- cloud profile, no session; render the sign-in interstitial.
//   local          -- local profile (auth not configured); render the app, no sign-in.
//   error          -- the session check failed unexpectedly; surface it rather
//                     than silently rendering the app or the sign-in screen.
export type SessionStatus =
  | 'loading'
  | 'authenticated'
  | 'unauthenticated'
  | 'local'
  | 'error';

export interface SessionState {
  status: SessionStatus;
  user: UserClaims | null;
  error: string | null;
  signOut: () => Promise<void>;
}

/**
 * Resolve the auth gate by probing GET /app/auth/me, and keep it live: a
 * mid-session auth_required signal (an expired session surfaced by any data
 * call) flips an authenticated gate back to the sign-in interstitial.
 */
export function useSession(): SessionState {
  const [status, setStatus] = useState<SessionStatus>('loading');
  const [user, setUser] = useState<UserClaims | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getSession()
      .then((session) => {
        if (!active) return;
        if (session.authenticated) {
          setUser(session.user);
          setStatus('authenticated');
        } else {
          setUser(null);
          setStatus('unauthenticated');
        }
      })
      .catch((err) => {
        if (!active) return;
        if (err instanceof ApiError && err.code === 'auth_not_configured') {
          setStatus('local');
        } else {
          setError(err instanceof Error ? err.message : 'Failed to check session');
          setStatus('error');
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    // An expired session surfaced by a data call returns the user to sign-in.
    return onAuthRequired(() => {
      setUser(null);
      setStatus('unauthenticated');
    });
  }, []);

  async function signOut(): Promise<void> {
    await logout();
    setUser(null);
    setStatus('unauthenticated');
  }

  return { status, user, error, signOut };
}
