# Using the SAGE API from Claude Code

You do not need a written API reference. The SAGE deployment publishes a
complete, authored description of itself — every operation, request and response
schema, error code, and the auth scheme — at one **unauthenticated** URL:

```
https://sage.resurrection.church/openapi.json
```

Point Claude Code at that document and let it read the contract. This page is
the prompt that does it, plus the one thing the document cannot hand you:
credentials. Once the deployment's operator has granted you access, a token you
mint will authorize.

---

## 1. Authenticate

SAGE accepts Entra bearer tokens that are **short-lived (~1 hour) by design** —
that lifetime is fixed by OAuth and you don't extend it. Don't fight it and
don't hold a static token: **mint on demand and cache until just before
expiry.** Set that up once and the hourly clock never bothers you again.

### For an application you're building — client credentials (recommended)

This deployment advertises the `client_credentials` grant, so an application
authenticates **as itself**, with no human and no interactive login. This is the
durable, set-and-forget path for a months-long project.

One-time setup:

1. Ask the deployment's operator to provision a **service principal** for your app and **grant it the
   `Sage.Reader` app role** on the resurrection.church deployment. The role assignment is a
   per-principal grant — without it Entra refuses the token request itself, and
   it is not something you can self-serve.
2. Store the principal's `client_id` and secret as environment variables (never
   in code).

Then your app mints its own tokens, forever, from that secret. The token
endpoint and the resource to request a token for are advertised by the
deployment itself, so read them from its metadata rather than copying them
from anywhere:

```bash
SAGE=https://sage.resurrection.church
TOKEN_URL="$(curl -s "$SAGE/.well-known/oauth-authorization-server" | jq -r .token_endpoint)"
SCOPE="$(curl -s "$SAGE/.well-known/oauth-protected-resource" | jq -r .resource)/.default"

curl -s -X POST "$TOKEN_URL" \
  -d "grant_type=client_credentials" \
  -d "client_id=$SAGE_CLIENT_ID" \
  -d "client_secret=$SAGE_CLIENT_SECRET" \
  -d "scope=$SCOPE"
```

The response carries `access_token` and `expires_in`. Cache the token and
request a new one only when it's within ~5 minutes of expiring — a few lines in
whatever HTTP layer your app already uses. The client secret lasts as long as
you configure it (months to years), so nobody refreshes anything by hand.

### For poking at the API by hand during development

The Azure CLI is fine for interactive use, and it's **not** an hourly chore:
`az account get-access-token` mints a *fresh* token from your cached `az login`
(valid for weeks) every time you call it, so "refreshing" is just calling it
again — no re-login. Mint on demand instead of pasting a static value:

```bash
sage-token() { az account get-access-token \
  --scope "https://sage.resurrection.church/.default" \
  --query accessToken -o tsv; }
# curl -H "Authorization: Bearer $(sage-token)" https://sage.resurrection.church/...
```

> The scope is the deployment's advertised resource with `/.default`
> appended. The resource is listed at
> `https://sage.resurrection.church/.well-known/oauth-protected-resource`, and the token
> endpoint, which names the tenant, at
> `https://sage.resurrection.church/.well-known/oauth-authorization-server`. Read both
> there if you ever target a different deployment.

## 2. Hand Claude Code this prompt

```
The SAGE API is fully described at https://sage.resurrection.church/openapi.json — fetch it
and treat it as the authoritative contract: operations, request/response
schemas, error codes, and the auth scheme. Base URL is https://sage.resurrection.church.
Authenticate every request as `Authorization: Bearer <token>`, minting the token
fresh (don't cache a stale one) — for interactive work run `sage-token`; in
application code use the client-credentials call. Before you call an operation,
read its schema from that document rather than guessing. Start by summarizing
which operations are available and what each is for.
```

From there, ask for what you want in plain language ("list the documents in the
`cas-adr` vault", "search for X and show the top hits") — Claude reads the
operation it needs from the document and makes the call.

## 3. When something returns an error

The document lists the error codes per operation; two auth cases are worth
knowing up front:

- **`401`** — no token, or an expired one. You're caching too long or minting
  wrong; re-mint.
- **A refused token request** (`AADSTS50105` for a person, a refusal of the
  client-credentials request for an app) — the principal has no access to this
  deployment: a person is not in its access group, or your app's service
  principal lacks the `Sage.Reader` role (step 1 above). Ask the deployment's operator. It is never a
  code fix.
- **`403`** — a token for the SAGE audience that carries neither the
  `Sage.Access` scope nor the `Sage.Reader` role. The supported paths above never
  mint one, so report it rather than retrying.

## Notes

- **The document is the source of truth, not this page.** It is generated from
  the routes the deployment actually runs, so it is always accurate for that
  deployment. Read it fresh; don't hardcode the operation list.
- **It's per-deployment.** `sage.resurrection.church` is the current deployment. Against a
  different one, swap the base domain — the scope, audience, and tenant are all
  derived from it and advertised at that deployment's `openapi.json` and
  `/.well-known/` discovery documents.
- **The rendered explorers** (`/docs`, `/redoc`) require a token; the raw
  `openapi.json` does not — which is why Claude can read the contract before you
  have even authenticated.
- For the provisioning story behind access (the client-credentials capability,
  the per-principal role grant, multiple deployments), see
  [`sage-rest-api.md`](sage-rest-api.md).
