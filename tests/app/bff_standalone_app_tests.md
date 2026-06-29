# Standalone BFF ASGI app tests (CAS-ADR-042)

Covers `create_bff_app` (`app/backend/asgi.py`) and the SAGE reverse proxy
(`app/backend/proxy.py`). Implemented in `tests/app/test_bff_standalone_app.py`.

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
