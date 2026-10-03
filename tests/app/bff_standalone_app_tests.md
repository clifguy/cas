# Standalone BFF ASGI app tests (CAS-ADR-042)

Covers `create_bff_app` (`app/backend/asgi.py`) and the SAGE reverse proxy
(`app/backend/proxy.py`). Implemented in `tests/app/test_bff_standalone_app.py`,
except APP-025 and APP-026, which are in `tests/app/test_bff_cloud_ingest.py`.
Path and header containment is specified separately in
`bff_containment_tests.md`.

The hosted profile runs the backend as its own process: it serves the SPA
same-origin, exposes a liveness probe, reverse-proxies the SPA's `/sage_vaults/*`
traffic to SAGE with the user's delegated token, and boots with no SAGE in
process. The proxy covers the bare `/sage_vaults` collection (list/create) as
well as the `/sage_vaults/*` subpaths; the bare path must not fall through to the
SPA catch-all. Local-filesystem scan/ingest is a co-located-profile capability
and is profile-bounded here rather than functional.

| ID | Behavior | Anti-coincidental control |
|----|----------|---------------------------|
| APP-001 | The app starts up without ever creating a vault registry. | A lifespan that aliased the SAGE registry would leave `vault_registry` set. |
| APP-002 | `GET /health` returns the constant liveness envelope. | Omit the route → 404. |
| APP-003 | `GET /` serves the SPA `index.html`. | Misdirected/absent mount → no index markup. |
| APP-004 | `GET /documents` (a client route) returns the SPA shell. | A file-only mount → 404; the catch-all is what resolves deep links. |
| APP-005 | The scan/ingest and auth routers plus `/health` are mounted. | — |
| APP-006 | A logged-in `/sage_vaults/*` call is proxied to SAGE with the delegated bearer. | Drop the session passed to the transport → no upstream bearer (401). |
| APP-007 | An unauthenticated `/sage_vaults/*` call is refused; SAGE is never reached. | Forward before checking the session → an upstream call is recorded. |
| APP-007b | With no auth context the proxy answers `auth_not_configured` (503). | — |
| APP-008 | In the standalone app the scan route returns the typed `local_profile_only` (501), not a 500. | Read `vault_registry` unguarded → `AttributeError`/500. |
| APP-009 | A logged-in bare `GET /sage_vaults` is proxied to SAGE upstream `/sage_vaults` (no trailing slash) with the delegated bearer. | Bare path falls to the SPA catch-all → `200` HTML, no upstream call; or a fix forwarding `/sage_vaults/` records a trailing slash. |
| APP-010 | A logged-in bare `POST /sage_vaults` is proxied to SAGE upstream `/sage_vaults` with the body forwarded. | Bare path falls to the SPA catch-all → no upstream POST; or the body is dropped. |
| APP-011 | An unauthenticated bare `GET /sage_vaults` returns JSON `auth_required` (401), never HTML; SAGE is never reached. | Bare path falls to the SPA catch-all → `200` HTML, not `401`; or forwards before the session check. |
| APP-012 | An upstream timeout is answered with the structured `sage_upstream_timeout` 504. | No transport-exception guard → the `httpx.ReadTimeout` propagates instead of becoming a 504. |
| APP-013 | Any other upstream transport failure is answered with the structured `sage_upstream_unavailable` 502. | No guard → the error propagates; catching `TransportError` before `TimeoutException` mis-maps a timeout to 502. |
| APP-014 | A signed-in GET on the document content route is relayed as a stream: body intact, Content-Type and Content-Disposition relayed, delegated bearer attached. | Dropping Content-Disposition in header filtering breaks the browser download although the bytes round-trip. |
| APP-015 | Streaming is scoped to its listed routes by method: other paths, and non-GET methods on a content-shaped path, keep the buffered `request()` port method. | A bare `/content` suffix match or a method-agnostic branch routes the POST through `stream()`. |
| APP-016 | An unsessioned GET on the content route is refused with the structured 401 and SAGE is never reached. | A streaming branch placed before the session check records an upstream call. |
| APP-017 | A transport failure while opening a stream maps to the same 504/502 envelopes as the buffered path. | An unwrapped `stream()` call propagates the httpx error; reversed exception arms mis-map the timeout. |
| APP-018 | Shutdown releases the process-wide async Entra credential. | The lifespan without the wiring leaves the credential's close count at 0. |
| APP-019 | Without a session, scan and ingest answer `auth_required` 401 before the request-name refusal, body validation or the profile boundary. | Ungated, the rows answer 501, `unknown_parameter` 400 or a validation 400; a later session check leaks the declared field names. |
| APP-019b | A cookie naming no live session is refused like no cookie. | A presence-only check lets both rows through to the 501. |
| APP-020 | The application-backend routes and the proxy refuse an unsessioned request with the same envelope. | A separately authored refusal drifts and fails the equality. |
| APP-021 | With sign-in unconfigured, scan answers `auth_not_configured` 503, as the proxy does. | Passing the request through without an auth context answers the 501. |
| APP-022 | An unsessioned request with a non-JSON body never reaches the route and names no declared field. | Reaching the route answers 501; enumerating the fields carries their names. |
| APP-023 | Every route is session-gated or on the exemption list, and every exemption names an ungated route the app serves. | The SPA bundle and assets are staged so the catch-all and static mount exemptions are exercised, not stale. |
| APP-023b | The gate detector distinguishes a gated route from an ungated one. | — |
| APP-024 | The co-located application includes the same router without the session requirement. | Attaching the requirement to the router passes APP-019 and APP-023 but gates the local profile. |
| APP-025 | In the hosted profile the path-based `/app/ingest` answers `local_profile_only` 501 and points to the upload path. | A route that succeeded or 500'd lacks the code; lost guidance fails the substring check. |
| APP-026 | A batch upload through the proxy without a session is refused and SAGE is never reached. | Forwarding before the session check records an upstream call. |
| APP-027 | Over a real server, a proxied event-stream operation (batch upload ingest, deferred reabstract) delivers its first event while SAGE is still producing the rest, and forwards the request body. | SAGE withholds its second event until the first is read, so a buffering proxy never returns the first event and the read times out. |
| APP-028 | A query name given more than once reaches SAGE with every value, in order, on the buffered and streamed paths. | Forwarding the query as a mapping keeps only the last value. |
