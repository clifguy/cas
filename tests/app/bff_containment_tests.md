# Standalone BFF path and header containment tests (CAS-ADR-042)

Covers the containment of the standalone backend-for-frontend built by
`create_bff_app` (`app/backend/asgi.py`) and its SAGE reverse proxy
(`app/backend/proxy.py`). Implemented in `tests/app/test_bff_containment.py`.

The backend serves the files of its SPA bundle and nothing else, forwards only
paths beneath `/sage_vaults` to SAGE, forwards and relays only the headers the
SPA and SAGE exchange, and publishes no interactive documentation page.

Path cases that a normalizing HTTP client would rewrite before sending are
driven through a real uvicorn server with raw request targets, so the request
the application sees is the one a hostile client can send.

| ID | Behavior | Anti-coincidental control |
|----|----------|---------------------------|
| CTN-001 | Over real uvicorn, every request target that names a file outside the bundle -- dot segments (literal, percent-encoded, with an encoded separator), an absolute path (doubled slash or encoded leading slash), an encoded NUL, and a dot-segment escape through the static assets mount -- is refused 404 with the `route_not_found` envelope and never carries the sentinel file's bytes. | Positive controls on the same server: a real top-level bundle file and an asset are served byte-exact, and a client route returns the SPA shell. Reverting the catch-all to unchecked joining serves the sentinel. |
| CTN-002 | A symlink inside the bundle that points to a file outside it is not served; the request receives the SPA shell. | A positive control serves an in-bundle file. Checking containment on the unresolved path serves the symlink target. |
| CTN-003 | A signed-in proxy request whose path below `/sage_vaults/` has an empty, `.` or `..` segment, in any percent-encoding, is refused 404 with the `route_not_found` envelope, and SAGE is never called. Includes one real-uvicorn case with a literal dot segment. | A positive control forwards an ordinary path to exactly that upstream path. Removing the segment check lets a dot-segment request reach SAGE outside `/sage_vaults`. |
| CTN-004 | A percent-encoded `/` or `?` inside a path segment reaches SAGE still encoded inside that segment, neither as a separator nor as a query. A literal `:` in a segment is forwarded unchanged. | Forwarding the decoded path splits the segment or turns its tail into a query. |
| CTN-005 | The backend serves no `/docs`, `/docs/oauth2-redirect`, `/redoc` or `/openapi.json` route, and no response at those paths references a documentation script; the application still generates its OpenAPI document in process. | Restoring the default documentation URLs serves the pages. |
| CTN-006 | The proxy forwards only the allowlisted request headers (`accept`, `accept-language`, `content-type`, `user-agent`), with the transport's own bearer as `Authorization`, on both the buffered and the streamed path. A caller's own `Authorization`, cookies, `Origin`, forwarding and custom headers do not reach SAGE. | The allowlisted headers arrive with the caller's values. Forwarding every header lets the custom header through. |
| CTN-007 | The proxy relays only the allowlisted response headers (`content-type`, `content-disposition`, `cache-control`, `www-authenticate`, `allow`) on both paths; `set-cookie`, `server` and custom headers from SAGE are dropped. | The allowlisted headers arrive with SAGE's values. Relaying every header lets the cookie through. |
