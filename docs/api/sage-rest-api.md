# SAGE REST API

Client-integration guide for the SAGE Core API. Self-contained: you do not need
the CAS repository to build against this surface.

SAGE (Salience-Aware Graph Engine) is a vault-scoped knowledge graph and
document store. This document covers the **REST API**. SAGE also exposes an MCP
server at `/mcp`, which is a separate surface — an MCP client never calls the
paths below, and a REST client never calls `/mcp`.

- **Base URL:** `https://sage.<base-domain>` — supplied by the deployment's
  operator. The first deployment is `https://sage.cor.org`, used as the worked
  example throughout.
- **Spec version:** SAGE Core API 2.1 (OpenAPI 3.1.0)
- **Operations:** 42 across 39 paths

## Write portable clients

SAGE is deployed more than once. Each deployment has **its own domain, its own
Entra tenant, and its own audience**, and the OAuth scope string is derived
from the deployment's domain — `https://sage.cor.org/Sage.Access` on the first
one, something else on the next.

Take the base URL as configuration and resolve everything else at runtime from
the deployment's own discovery documents (§1.2). A client that hardcodes the
scope, tenant id, or token endpoint works against exactly one deployment and
fails against the second with an opaque `invalid_token`.

---

## 1. Authentication

Every path below requires an Entra ID (Azure AD) bearer token. Exceptions:
`/health`, `/upload`, and `/download/{transfer_id}`.

```
Authorization: Bearer <token>
```

A request is authorized if the token carries **either** the delegated scope
`<resource>/Sage.Access` **or** the app role `Sage.Reader`. `<resource>` is the
deployment's own resource URL, advertised by discovery — on the first
deployment it is `https://sage.cor.org`. Read it; do not assemble it.

### 1.1 Read this before you build

**Headless service-to-service authentication is not available today.**

`Sage.Reader` is declared with `allowedMemberTypes: ["User"]`, and no
application-type app role exists. An Entra app role restricted to `User` cannot
be assigned to a service principal, so the OAuth **client-credentials grant
cannot produce an accepted token**. The tenant's authorization-server metadata
confirms this from the other direction:

```
grant_types_supported: ["authorization_code"]
```

Every supported path requires a human at a browser. Consequences for
deterministic software:

| You are building | Works today |
|---|---|
| A tool a developer runs, signing in once interactively | Yes |
| A daemon, cron job, CI step, or backend service | **No** |

This is not specific to one deployment. Every SAGE deployment is bootstrapped
from the same Entra registration script, so each one inherits the same
`User`-only app role.

Unblocking the second row is a configuration change, not a code change: add
`"Application"` to `allowedMemberTypes` on the `Sage.Reader` app role, then
grant that role to each calling service principal. Making the change in the
bootstrap script rather than by hand fixes every future deployment too. Until
then, do not design around client credentials.

### 1.2 Interactive flow

Discovery is public and unauthenticated — fetch it rather than hardcoding
endpoints:

```
GET https://sage.<base-domain>/.well-known/oauth-protected-resource
GET https://sage.<base-domain>/.well-known/oauth-authorization-server
```

The authorization server is Entra. The resource advertises its own
`scopes_supported` — on the first deployment,
`["https://sage.cor.org/Sage.Access", "offline_access"]`. Use the advertised
value verbatim; the resource-qualified form is required, and a bare
`Sage.Access` leaves Entra unable to bind the scope.

Use authorization code + PKCE (S256). Public clients are supported
(`token_endpoint_auth_methods_supported: ["none"]`), and dynamic client
registration is open at `POST /register`, so you can self-register.

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

### 1.4 What a token grants

Access is deliberately uniform: **a valid token reaches every vault on that
deployment.** There is no per-vault grant to request and no per-vault scope to
add to your token — if `GET /sage_vaults` lists it, you can read and write it,
subject to the same operations everywhere.

Provisioning is therefore binary: you either have access to SAGE or you do not.
Plan integrations on that basis rather than expecting to be scoped down to a
subset of vaults.

---

## 2. Conventions

### 2.1 Vault scoping

Every operation except `GET|POST /sage_vaults`, `PUT /upload`, and
`GET /download/{transfer_id}` is scoped to one vault:

```
/sage_vaults/{vault_id}/...
```

`vault_id` is supplied to you by the SAGE operator. `GET /sage_vaults` lists
the vaults your principal can reach.

### 2.2 Two error envelopes

They differ by origin. Handle both.

**Auth failures** (401/403, from middleware) follow RFC 6750 and carry a
`WWW-Authenticate` header:

```json
{ "error": "invalid_token", "error_description": "..." }
```

```
WWW-Authenticate: Bearer resource_metadata="https://sage.<base-domain>/.well-known/oauth-protected-resource"
```

**Application errors** (400/404/409/413/422, from the API) carry:

```json
{ "code": "document_not_found", "message": "...", "detail": { "document_id": "..." } }
```

Branch on `code`, not on `message`. `detail` is omitted when empty and its keys
vary by `code`.

### 2.3 Invariants

- **Documents are never deleted.** There is no document DELETE. Supersession
  and archival are the lifecycle mechanisms; ingest a successor with
  `predecessor_id`.
- **Edges may be deleted.** `DELETE /sage_vaults/{vault_id}/edges/{edge_id}`.
  The no-delete rule covers documents only.
- **Vault isolation.** No operation spans vaults.

---

## 3. Endpoints

`operationId` values match the OpenAPI spec.

### Retrieval

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/discover` | `search` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}` | `get_document` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/content` | `get_document_content` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/projection` | `read_projection` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/section/{heading_path}` | `read_section` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/headings` | `list_headings` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/download-url` | `get_document_download_url` |
| POST | `/sage_vaults/{vault_id}/documents/{document_id}/export` | `export_projection` |
| POST | `/sage_vaults/{vault_id}/eval-retrieval` | `eval_retrieval` |

`POST .../discover` is the primary query endpoint — vector, full-text, and
catalog retrieval modes with metadata filters.

### Ingestion

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/documents` | `ingest_document` |
| POST | `/sage_vaults/{vault_id}/documents:batch` | `batch_ingest_documents` |
| POST | `/sage_vaults/{vault_id}/parse-filename` | `get_filename_metadata` |
| POST | `/sage_vaults/{vault_id}/documents/{document_id}/open` | `open_document` |
| POST | `/sage_vaults/{vault_id}/documents/{document_id}/reabstract` | `recompute_abstract` |

### Graph

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/edges` | `create_edges` |
| DELETE | `/sage_vaults/{vault_id}/edges/{edge_id}` | `delete_edge` |
| POST | `/sage_vaults/{vault_id}/traverse` | `traverse` |
| POST | `/sage_vaults/{vault_id}/chain` | `chain` |
| GET | `/sage_vaults/{vault_id}/preconditions/{function_id}` | `verify_preconditions` |

### Staging edges

| Method | Path | operationId |
|---|---|---|
| GET | `/sage_vaults/{vault_id}/staging-edges` | `list_staging_edges` |
| POST | `/sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm` | `confirm_staging_edge` |
| POST | `/sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss` | `dismiss_staging_edge` |

### Lifecycle and metadata

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/lifecycles` | `update_lifecycles` |
| POST | `/sage_vaults/{vault_id}/metadata` | `update_metadata` |
| GET | `/sage_vaults/{vault_id}/pending-metadata` | `list_pending_metadata` |

Both mutation endpoints take an `items` array — batch by default, one entry for
a single change.

### Access control

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/users` | `register_user` |
| PUT | `/sage_vaults/{vault_id}/documents/{document_id}/editors` | `set_editors` |
| GET | `/sage_vaults/{vault_id}/documents/{document_id}/editors` | `get_editors` |

### Vault management

| Method | Path | operationId |
|---|---|---|
| GET | `/sage_vaults` | `admin_list_vaults` |
| POST | `/sage_vaults` | `admin_create_vault` |
| GET | `/sage_vaults/{vault_id}/stats` | `admin_get_vault_stats` |
| GET | `/sage_vaults/{vault_id}/config` | `admin_get_vault_config` |
| PUT | `/sage_vaults/{vault_id}/config` | `admin_update_vault_config` |
| POST | `/sage_vaults/{vault_id}/hash-check` | `verify_hashes` |

### Maintenance

| Method | Path | operationId |
|---|---|---|
| POST | `/sage_vaults/{vault_id}/admin/detect-drift` | `admin_verify_vault_drift` |
| POST | `/sage_vaults/{vault_id}/admin/verify-source-files` | `admin_verify_vault_source_files` |
| POST | `/sage_vaults/{vault_id}/admin/migrate` | `admin_migrate_vault` |
| POST | `/sage_vaults/{vault_id}/admin/reabstract-deferred` | `admin_recompute_deferred_vault_abstracts` |
| POST | `/sage_vaults/{vault_id}/admin/optimize-content-store` | `admin_optimize_vault_content_store` |
| POST | `/sage_vaults/{vault_id}/refresh-views` | `admin_recompute_views` |

### Transfer (not vault-scoped)

| Method | Path | operationId |
|---|---|---|
| PUT | `/upload` | `transfer_upload` |
| GET | `/download/{transfer_id}` | `transfer_download` |

---

## 4. File transfer is two-phase

The server cannot see your filesystem. Ingesting a local file and downloading
document bytes both use a mint-then-move handshake; a single call will not do
it.

**Upload.** `POST .../documents` with a local source path returns a status of
`upload_required` plus a recipe naming a `PUT /upload` target and a
`transfer_id`. Move the bytes to that target, then repeat the ingest call
citing the `transfer_id`.

**Download.** `GET .../documents/{document_id}/download-url` mints a
`GET /download/{transfer_id}` URL. Fetch the bytes there.

Both transfer paths are exempt from the bearer gate — authority travels in the
`transfer_id`, which is single-use and expiring. Treat it as a credential.

`GET .../content` returns bytes inline instead and fails with `413
content_too_large` above the inline ceiling (100 MB default). Prefer the
transfer path for anything large.

---

## 5. Maturity

Be aware of where this surface is proven before you depend on it.

Roughly **18 of the 39 paths are exercised only by the maintainer's CI suite** —
no client calls them against a running deployment. Deployment smoke testing
covers exactly one authenticated endpoint, `GET /sage_vaults`. The web client
exercises about 17 paths; SAGE's own MCP tools call the internal service layer
directly rather than going over HTTP, so daily agentic traffic does not
exercise this API at all.

Paths in the CI-only set: `parse-filename`, both ingestion endpoints, `chain`,
`preconditions`, `DELETE /edges/{edge_id}`, `users`, the three `admin/*`
verification and migration endpoints, the four document utility endpoints
(`export`, `projection`, `section`, `headings`), `eval-retrieval`,
`refresh-views`, `hash-check`, and `POST /sage_vaults`.

They are covered by tests and expected to work. They have not been proven
against this deployment. Exercise the ones you need before building on them,
and report what you find.
