# BFF sign-in and session lifecycle hardening tests

Covers the backend-for-frontend's interactive sign-in and session lifecycle
(`app/backend/auth/`): reuse of identity-provider discovery across MSAL calls,
running those calls off the event loop, sweeping expired pre-login and session
rows, rate-limiting sign-in, binding an in-flight sign-in to the browser that
began it, the `__Host-` session cookie, the idle timeout, writing a refreshed
token cache back to its session, and encrypting the stored token cache.
Implemented in `tests/app/test_bff_auth_hardening.py`; the Postgres cases run
against the disposable test database and skip without `SAGE_TEST_PG_DSN`.

| ID | Behavior | Anti-coincidental control |
|----|----------|---------------------------|
| AH-001 | With the real MSAL library and a counting HTTP client, two sign-in starts and two delegated-token acquisitions through one `MsalOidcService` fetch the tenant's OpenID configuration and the instance-discovery document exactly once each. | Building each MSAL application without the shared discovery cache fetches it on every call. |
| AH-002 | The sign-in start, the callback's code redemption and the delegated-token acquisition each run on a thread other than the event loop's. | Calling the identity-provider client directly from the coroutine records the loop's own thread. |
| AH-003 | Persisting a pre-login record deletes expired pre-login records, in memory and in Postgres; unexpired records survive. | Removing the sweep leaves the expired record in place. |
| AH-004 | Creating a session deletes sessions past their absolute expiry or idle beyond the limit, in memory and in Postgres; a live session survives. | Removing the sweep leaves the expired and idle sessions in place. |
| AH-005 | A client's eleventh sign-in start within the window is refused 429 `rate_limited` with a `Retry-After` header and `detail.retry_after_seconds`, before any identity-provider call; a different client is admitted in the same window, and the first is admitted again once the window has passed. | The first ten are admitted. Skipping the limiter admits the eleventh. |
| AH-005b | The hosted standalone app answers a client over the limit with 429 `rate_limited` and a `Retry-After` header equal to `detail.retry_after_seconds`. | The first start is admitted. |
| AH-006 | The limiter keys on the rightmost `X-Forwarded-For` entry, so varying the leftmost (client-supplied) entries does not evade it; without the header it keys on the peer address. | Keying on the leftmost entry admits every varied request. |
| AH-007 | A sign-in start sets `__Host-cas_login` to the SHA-256 hex digest of the issued `state`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=600`, with no `Domain`. | A digest of a different value fails the comparison. |
| AH-008 | A callback that carries no binding cookie, or another browser's binding cookie, is refused 400 `invalid_state`, opens no session and leaves the pre-login record in place, so the browser that began the sign-in still completes it. | The same callback with the matching cookie succeeds. Skipping the binding check opens a session for the foreign browser; not setting the cookie at sign-in start fails the matching-cookie control. |
| AH-009 | A successful callback expires the binding cookie. | The callback still sets the session cookie. |
| AH-010 | The session cookie is `__Host-cas_session`, `Secure`, `Path=/`, with no `Domain`; it resolves the session on `/app/auth/me`, and sign-out expires both it and the legacy `cas_session` cookie. | A cookie under the legacy name no longer resolves a session. |
| AH-011 | A session last seen longer ago than the idle limit is absent, in memory and in Postgres; a session read within the limit is returned, and its last-seen time advances only when more than a minute old. | A session within both limits is returned. |
| AH-012 | A delegated-token acquisition that changes the token cache writes the new cache back to the session through the store, and one that leaves it unchanged writes nothing; the standalone app wires its session store into the SAGE client. | Dropping the write-back leaves the stored cache unchanged. |
| AH-013 | The Postgres store keeps the token cache encrypted: the raw column never contains the plaintext cache, the session round-trips to the plaintext, and a store keyed from a different client secret reads the session as absent and deletes it. Assembling the hosted auth context hands the store a cipher keyed from the configured client secret and the configured idle limit. | An identity cipher leaves the plaintext in the column. |
| AH-014 | The token-cache key derives deterministically from the client secret: the same secret decrypts across cipher instances, and a different secret does not. | — |
| AH-015 | Opening the Postgres store on a schema provisioned before the idle column existed adds the column and the expiry indexes, and opening it again is a no-op. A pre-existing session row reads as idle. | A freshly provisioned schema carries the same column and indexes. |
| AH-016 | The limiter tracks at most its bound of clients, forgetting the least recently seen first: a client refused at its limit and then seen again is kept, while an older client is evicted and starts afresh. | Evicting the most recent instead forgets the refused client. |
