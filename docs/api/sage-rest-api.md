# SAGE REST API

Notes for building a client against a SAGE deployment. This file carries only
what a schema document cannot state: facts about the deployment you are
targeting and about how access to it is provisioned, rather than facts about
the API.

**Start with the schema document.** Every deployment publishes its own,
unauthenticated:

```
GET https://sage.<base-domain>/openapi.json
```

It is generated from the routes that deployment is actually running, so it is
accurate for that deployment specifically. It carries the operation inventory,
the request and response schemas, the error codes each operation returns, the
architectural invariants, the two-phase file-transfer handshake, and the bearer
scheme the deployment accepts. Point a code generator at it. Nothing in this
file repeats it, and nothing here is needed in order to read it.

- **Base URL:** `https://sage.<base-domain>` — supplied by the deployment's
  operator. The first deployment is `https://sage.cor.org`, used as the worked
  example throughout.
- The rendered explorers (`/docs`, `/redoc`) do require a token. The schema
  document itself does not.

## There is more than one deployment

The schema document tells you to resolve the tenant, endpoints, and scope
string from the deployment's own discovery documents at runtime. The reason it
matters is not in there: SAGE is deployed more than once, each deployment owns
its own domain, Entra tenant, and audience, and the scope string is derived
from the domain — `https://sage.cor.org/Sage.Access` on the first one,
something else on the next.

So a client that hardcodes the scope, tenant id, or token endpoint does not
fail visibly. It works against the deployment it was written for and fails
against the second with an opaque `invalid_token`.

---

## 1. Getting access

The schema document states what credential a deployment accepts. It cannot
state whether *you* have been granted one — that is per-principal,
per-deployment provisioning, and it is where integrations actually stall.

### 1.1 Read this before you build

**Headless service-to-service authentication is supported, but it is a
per-deployment capability — check the deployment you are targeting.**

`Sage.Reader` is declared with `allowedMemberTypes: ["User", "Application"]`, so
the role can be assigned to a service principal and the OAuth
**client-credentials grant can produce an accepted token**. Ask the deployment's
own authorization-server metadata rather than assuming:

```
GET https://sage.<base-domain>/.well-known/oauth-authorization-server
```

A deployment that offers the machine path advertises it:

```
grant_types_supported: ["authorization_code", "client_credentials"]
```

| You are building | Supported |
|---|---|
| A tool a developer runs, signing in once interactively | Yes |
| A daemon, cron job, CI step, or backend service | Yes, once granted (below) |

Two things gate the second row, and neither is a code change on your side:

1. **The deployment must be bootstrapped at a version whose registration script
   declares the `Application` member type.** Every deployment is provisioned
   from the same script, but an older deployment keeps whatever its last
   bootstrap wrote until it is re-run. If `grant_types_supported` omits
   `client_credentials`, this deployment is not there yet — ask its operator.
2. **Your service principal must be granted the `Sage.Reader` app role.**
   Admitting application principals to the role does not assign it to anyone.
   This is a per-principal, per-deployment grant, exactly like the group
   membership an interactive user needs (§1.3).

Design against the advertised metadata, not against this paragraph: a client
that reads `grant_types_supported` and falls back to the interactive flow when
the machine grant is absent works against every deployment, at any bootstrap
version.

### 1.2 Two things the metadata will not warn you about

The authorization server is Entra, and the discovery documents the schema
document points you at describe the flow completely. Two details are easy to
get wrong anyway:

- **Use the advertised scope string verbatim.** The resource advertises
  `scopes_supported` in the resource-qualified form — on the first deployment,
  `["https://sage.cor.org/Sage.Access", "offline_access"]`. That form is
  required; a bare `Sage.Access` leaves Entra unable to bind the scope.
- **Dynamic client registration is open at `POST /register`**, so a public
  client can self-register rather than waiting on the operator. Authorization
  code + PKCE (S256) is the interactive path.

### 1.3 Registration succeeds and access still fails

The SAGE application sets `appRoleAssignmentRequired: true`, and access is
provisioned by membership in a single group per deployment (`cas-sage-users` by
default). Because each deployment lives in its own Entra tenant, **access to
one deployment grants nothing on another** — you need a separate assignment for
each.

Registration and sign-in will therefore appear to work for a principal that has
no access — you get a client, you see a login page — and authorization fails
afterward. **If you get a token but every call returns 403, you need a group
assignment, not a code fix.** Ask the SAGE operator.

---

## 2. Maturity

Be aware of where this surface is proven before you depend on it. Coverage is
uneven for structural reasons rather than by accident:

- **SAGE's own agent tooling does not exercise it.** The MCP surface calls the
  internal service layer in process rather than over HTTP, so routine daily
  traffic never reaches the REST adapter at all.
- **The web client exercises the subset it needs** — the read and ingest
  surface behind the browser UI, not the maintenance, migration, or
  access-control operations.
- **Deployment preflight exercises a named set** against the live edge after
  every deploy: vault-scoped reads, document-scoped reads, the filename parser,
  and the unauthenticated schema fetch. Those are proven against a running
  deployment, and the deployment's own preflight report is the current answer
  for which — measured there rather than asserted here.

Everything else is covered by the maintainer's CI suite and expected to work,
but has not been proven against a live deployment. Exercise what you need
before building on it, and report what you find.
