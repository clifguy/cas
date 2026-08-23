# Using the SAGE API from Claude Code

You do not need a written API reference. The SAGE deployment publishes a
complete, authored description of itself — every operation, request and response
schema, error code, and the auth scheme — at one **unauthenticated** URL:

```
https://sage.cor.org/openapi.json
```

Point Claude Code at that document and let it read the contract. This page is
the prompt that does it, plus the one thing the document cannot hand you:
credentials. (You — Joel, Jared — already have the access grant, so a token you
mint will authorize.)

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

1. Ask Clif to provision a **service principal** for your app and **grant it the
   `Sage.Reader` app role** on the cor.org deployment. The role assignment is a
   per-principal grant — without it a perfectly valid token still returns `403`,
   and it is not something you can self-serve.
2. Store the principal's `client_id` and secret as environment variables (never
   in code).

Then your app mints its own tokens, forever, from that secret:

```bash
curl -s -X POST \
  "https://login.microsoftonline.com/ac771f28-72d5-4e2e-8751-ce45165efc64/oauth2/v2.0/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=$SAGE_CLIENT_ID" \
  -d "client_secret=$SAGE_CLIENT_SECRET" \
  -d "scope=api://ab32d173-6043-4b81-af68-54830d806689/.default"
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
  --scope "api://ab32d173-6043-4b81-af68-54830d806689/.default" \
  --query accessToken -o tsv; }
# curl -H "Authorization: Bearer $(sage-token)" https://sage.cor.org/...
```

> The tenant id, audience (`api://ab32d173…`), and scope above are all
> advertised at `https://sage.cor.org/.well-known/oauth-authorization-server` —
> read them there rather than trusting this page if you ever target a different
> deployment.

## 2. Hand Claude Code this prompt

```
The SAGE API is fully described at https://sage.cor.org/openapi.json — fetch it
and treat it as the authoritative contract: operations, request/response
schemas, error codes, and the auth scheme. Base URL is https://sage.cor.org.
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
- **`403`** — a valid token whose principal lacks the role grant. For your app's
  service principal that means step 1 above hasn't been done; ping Clif. It is
  never a code fix.

## Notes

- **The document is the source of truth, not this page.** It is generated from
  the routes the deployment actually runs, so it is always accurate for that
  deployment. Read it fresh; don't hardcode the operation list.
- **It's per-deployment.** `sage.cor.org` is the current deployment. Against a
  different one, swap the base domain — the scope, audience, and tenant are all
  derived from it and advertised at that deployment's `openapi.json` and
  `/.well-known/` discovery documents.
- **The rendered explorers** (`/docs`, `/redoc`) require a token; the raw
  `openapi.json` does not — which is why Claude can read the contract before you
  have even authenticated.
- For the provisioning story behind access (the client-credentials capability,
  the per-principal role grant, multiple deployments), see
  [`sage-rest-api.md`](sage-rest-api.md).
