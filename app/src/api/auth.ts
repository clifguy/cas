// Backend-for-frontend auth client: the SPA's view of the interactive sign-in
// surface (login / session / logout). The browser-facing redirect endpoints
// (authorization, callback) are navigated to by the browser, not fetched here.
//
// The types mirror the backend response models (app/backend/models.py):
// LoginChallengeResponse, UserClaims, SessionInfoResponse.

import { apiGet, apiPostVoid } from './client';

/** Identity claims for the signed-in user. Mirrors UserClaims. */
export interface UserClaims {
  subject: string;
  name: string | null;
  email: string | null;
}

/** The caller's session state. Mirrors SessionInfoResponse. */
export interface SessionInfo {
  authenticated: boolean;
  user: UserClaims | null;
}

/** The authorization URL to start interactive sign-in. Mirrors LoginChallengeResponse. */
export interface LoginChallenge {
  authorization_url: string;
  state: string;
}

/**
 * Report the caller's session state. Resolves SessionInfo on 200. In the local
 * profile the endpoint answers 503 auth_not_configured; that ApiError is
 * propagated so the caller can distinguish "no session" (authenticated:false)
 * from "auth not in play" (the local profile).
 */
export async function getSession(): Promise<SessionInfo> {
  return apiGet<SessionInfo>('/app/auth/me');
}

/**
 * Begin interactive sign-in: fetch the identity-provider authorization URL. The
 * caller navigates the browser to authorization_url; this function performs no
 * navigation so it stays unit-testable.
 */
export async function beginLogin(): Promise<LoginChallenge> {
  return apiGet<LoginChallenge>('/app/auth/login');
}

/** End the session and clear the session cookie. The endpoint returns 204. */
export async function logout(): Promise<void> {
  return apiPostVoid('/app/auth/logout');
}
