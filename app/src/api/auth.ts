// Backend-for-frontend auth client: the SPA's view of the interactive sign-in
// surface (login / session / logout). The browser-facing redirect endpoints
// (authorization, callback) are navigated to by the browser, not fetched here.
//
// The response shapes are declared in ./types with the other published mirrors.

import { apiGet, apiPostVoid } from './client';
import type { LoginChallenge, SessionInfo } from './types';

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
