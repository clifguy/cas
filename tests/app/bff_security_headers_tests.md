# Standalone BFF security response headers (CAS-ADR-042)

Covers the response headers the standalone backend-for-frontend built by
`create_bff_app` (`app/backend/asgi.py`) adds to every response, and the
frame protection of the local Vite development server (`app/vite.config.ts`).
Implemented in `tests/app/test_bff_security_headers.py` and
`app/src/test/viteConfig.test.ts`.

The headers wrap the whole application, outside its error middleware, so a
response the framework itself produces for an unhandled exception carries them
too. A header a response already carries is left as it is: the proxy relays
SAGE's own delivery headers for raw document bytes, which are stricter than the
page policy.

The page policy is `default-src 'self'; script-src 'self'; object-src 'none';
base-uri 'none'; frame-ancestors 'none'`. It is sent as
`Content-Security-Policy-Report-Only` unless `CAS_BFF_CSP_ENFORCE` is set, so
a violation is reported in the browser before it is enforced. Because
`frame-ancestors` is not honored in a report-only policy, `X-Frame-Options:
DENY` carries frame protection meanwhile.

| ID | Behavior | Anti-coincidental control |
|----|----------|---------------------------|
| SEC-001 | The SPA shell at `/`, a client route, an asset under `/assets/` and the health probe each carry the report-only page policy exactly, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`, and no enforcing `Content-Security-Policy`. | The asset and shell bodies are served byte-exact, so the headers ride on real responses. Removing the wrapper turns every case red. |
| SEC-002 | A signed-in proxied response carries the same headers on both the buffered and the streamed path. | SAGE's body arrives unchanged. |
| SEC-003 | Error responses carry the headers: a proxy refusal (`route_not_found` envelope), an unsessioned request refused by the proxy, and a SAGE error envelope relayed by the proxy. | Each response's envelope code is asserted, so the case is the error path. |
| SEC-004 | A response produced for an unhandled exception (500) carries the headers. | Moving the wrapper inside the application's own middleware stack, where the server-error middleware sits outside it, turns this case red while SEC-001 stays green. |
| SEC-005 | Under the cloud profile every response carries `Strict-Transport-Security: max-age=31536000; includeSubDomains`; under the local profile none does. | The local-profile response still carries the other headers. |
| SEC-006 | With `CAS_BFF_CSP_ENFORCE` set (or `csp_enforce=True` passed), the page policy is sent as an enforcing `Content-Security-Policy` and no report-only header is sent; a false value keeps report-only. | The policy value is identical in both modes. |
| SEC-007 | On the proxied raw-content path, SAGE's `Content-Security-Policy: sandbox` and `X-Content-Type-Options` are relayed and not replaced, while the page's report-only policy is still added. A response without them from SAGE receives the BFF defaults. | Dropping the two names from the relay allowlist loses the sandbox. |
| SEC-008 | The Vite development server's configured headers include `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`. | The test reads the exported configuration object, not the file text. |
