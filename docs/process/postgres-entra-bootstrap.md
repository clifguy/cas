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
require. The `pgaadauth_*` administration functions live only in the server's
built-in `postgres` maintenance database, so the bootstrap creates the roles and
grants there; the extensions are per-database and are created on the application
database itself. This is **provisioning-as-code, not a hand-run runbook** (CAS
Cloud Deployment Discipline, Principle 3): the executable substance is the codified
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
- `pgstattuple` backs bloat measurement.

Both are **untrusted** extensions on this server, so only a member of `azure_pg_admin`
may create them — the unprivileged application roles cannot. The privilege is enforced at
the `CREATE EXTENSION` command level, so an idempotent `CREATE EXTENSION IF NOT EXISTS`
issued by an application role is rejected regardless of whether the extension already
exists. The workloads therefore **do not issue `CREATE EXTENSION` at all** under managed
identity: each self-bootstrap creates only its own (owned) schema and tables and relies on
the administrator having pre-created the extensions.

The job creates the extensions on the **application database itself** — the same
database the workloads connect to — and then **verifies each is present there** before
returning. Extensions are per-database, so the verification guarantees the extension the
content store needs is installed in the database the workloads open (the connection pool's
pgvector type registration needs `vector` present, not self-created); a create that landed
in a different database fails the job loud, naming the database and the missing extension,
rather than surfacing later as a vault that fails to load.

## Verify

Roll (or restart) the SAGE container app so it opens fresh connections under its
now-provisioned role, then confirm it authenticates and loads the seeded vault end
to end:

```bash
az containerapp logs show -g "$RG" -n "ca-sage-${ENVIRONMENT_NAME}" --tail 200 \
  | grep -i "vaults loaded"
# expect: vaults loaded (1): cloud_validation     — and no OperationalError
```

`list_vaults` then includes `cloud_validation`, and `get_vault_config` returns its
configuration read back from the document store (CAS-ADR-043). This is the live proof
the cloud profile authenticates to Postgres and serves the seeded vault.
