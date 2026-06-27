# Postgres Entra bootstrap — operator doc

The cloud deployment profile (CAS-ADR-042) runs its durable graph and content state
on an Azure Database for PostgreSQL Flexible Server with **password authentication
disabled and Microsoft Entra authentication enabled**. Each workload authenticates
as *itself* — the SAGE and CAS BFF container apps present a managed-identity Entra
access token and the libpq `user` is the identity's own name (`id-sage-<env>`,
`id-cas-bff-<env>`) — and each **self-bootstraps its own schema at startup** (SAGE
one schema per vault, the BFF its session schema). No password is stored anywhere.

Two prerequisites must exist on the live server before that works, and neither is
expressible as declarative ARM: a **database role per managed identity** (created
with `pgaadauth_create_principal`) and the **extensions** the storage adapters
require. This is **provisioning-as-code, not a hand-run runbook** (CAS Cloud
Deployment Discipline, Principle 3): the executable substance is the codified
bootstrap, and this doc records only the operator steps around it.

## What the deploy declares

The Bicep deployment already declares everything the bootstrap needs:

- A dedicated **bootstrap identity** `id-pg-bootstrap-<env>`, set declaratively as
  the server's Entra **administrator** (no deploy-time object id required).
- A Container Apps **Job** `job-pg-bootstrap-<env>`, in the VNet-integrated Container
  Apps environment so it can reach the private server, running as that bootstrap
  identity and invoking the committed `sage.storage.postgres.cloud_bootstrap`
  entrypoint (it reuses the SAGE image, which already carries the driver and the
  Entra credential library).

The job is **idempotent** — re-running converges: extension creates are
`IF NOT EXISTS`, role creation is guarded by an existence check, and grants are
re-grantable.

## Trigger (the operator step)

The job is declared by the deploy and **started** on bring-up. Resolve the
environment coordinates into shell variables — no identifier literal lives in this
doc — then start the job and watch it complete:

```bash
ENVIRONMENT_NAME="prod"
RG="rg-cas-${ENVIRONMENT_NAME}"

az containerapp job start -g "$RG" -n "job-pg-bootstrap-${ENVIRONMENT_NAME}"

az containerapp job execution list -g "$RG" -n "job-pg-bootstrap-${ENVIRONMENT_NAME}" \
  --query '[0].properties.status' -o tsv
```

## Least privilege

The two application roles receive **`CONNECT, CREATE ON DATABASE` and nothing more**.
They are deliberately **not** added to `azure_pg_admin` (the broad
server-administration role). `CREATE ON DATABASE` is exactly what each app needs: at
startup it creates and *owns* its own schema(s), so table and index rights follow
ownership with no further grants.

The extensions are pre-created **as the administrator** by the job:

- `vector` (pgvector) backs the content store's embedding column.
- `pgstattuple` backs bloat measurement and is an **untrusted** extension, so only an
  administrator may create it — the unprivileged application roles cannot.

Pre-creating both is what lets the apps' own idempotent `CREATE EXTENSION IF NOT
EXISTS` calls succeed as privilege-free no-ops thereafter.

## Verify

Roll (or restart) the SAGE container app so it opens fresh connections under its
now-provisioned role, then confirm it authenticates and loads the seeded vault end
to end:

```bash
az containerapp logs show -g "$RG" -n "ca-sage-${ENVIRONMENT_NAME}" --tail 200 \
  | grep -i "vaults loaded"
# expect: vaults loaded (1): test     — and no OperationalError
```

`admin_list_vaults` then includes `test`, and `admin_get_vault_config` returns its
configuration read back from the document store (CAS-ADR-043). This is the live proof
the cloud profile authenticates to Postgres and serves the seeded vault.
