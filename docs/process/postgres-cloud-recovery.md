# Cloud PostgreSQL replacement and recovery

The serving server remains the database of record until an explicitly dispatched
cutover. Provisioning a replacement does not move SAGE, the BFF, bootstrap, or
maintenance. The empty `postgresGeneration` preserves the original server name;
a stable nonempty generation creates a second server through the shared module.
Both servers retain their declared deletion locks.

## Preparation and verification boundary

The repository supports an offline logical migration from the manifest's
`postgres.deploy_major` to `postgres.dev_major`. During preparation these remain
16 and 17 respectively. The runtime client covers the greater major; the
PostgreSQL 16 storage CI job and its required branch check remain in place.

Merge preparation separately from authorizing production changes. First deploy
the preparation image through the normal `infra` workflow with
`POSTGRES_GENERATION` empty. Verify its usual preflight. This installs the
migration module and client while keeping all consumers on the incumbent.
The migration workflow reuses that deployed bootstrap image and identity. It
does not build or substitute a different image during downtime.

For the intended GitHub environment, record the serving deployment's resource
group, source FQDN, database name, image, Entra administrator identity, and the
chosen generation. Check the actual tenant/environment mapping. A generation is
one to twelve lowercase letters, digits, or hyphens, starting with a letter.
Do not change it when retrying the same migration.

Dispatch `postgres-migration` on `main`, action `prepare`. It provisions the
replacement on the existing private network and prepares an in-VNet manual job.
It verifies target readiness, the manifest major, 35-day backup retention,
geo-redundancy, and the deletion lock. The job then runs in its default `preflight`
mode: it bootstraps the replacement roles/extensions and checks database CREATE,
SET ROLE, and inherited access for the source's restored object owners. Preparation
fails if any required role or privilege is missing; the source apps and CONNECT
privileges are untouched. It does not grant migration-admin membership automatically.
Have an authorized database administrator establish the required scoped memberships
and rerun preparation; record any temporary grants and their later revocation.
This creates billable overlapping resources;
geo backup storage may also add cost. It does not stop applications.

The local PostgreSQL roundtrip tests verify logical transfer, reconciliation,
source fencing, and resume rejection against disposable databases. A separate
non-superuser rehearsal tests missing ownership privileges and a successful restore
with explicit SET ROLE and inherited owner access. Bicep builds
and source gates verify wiring. They do not establish Azure managed-identity
privileges, private DNS, geo-backup availability, or application health. Those
remain live gates during preparation, migration, and the serving preflight.
Never describe a preparation PR as a completed production migration.

## Offline migration

Agree on a downtime window before dispatch. All normal mutating workflows share
a tenant concurrency group. A resource-group tag also blocks maintenance and
ordinary deployment across failed or cancelled workflow runs. Do not bypass that
tag or start jobs from an independent operator session.

1. Confirm the replacement preparation succeeded and the deployed image contains
   this migration module. Check that no bootstrap, maintenance, or migration job
   remains active. A cancelled GitHub run does not prove its Azure job stopped.
2. Dispatch `postgres-migration`, action `migrate`, using the prepared generation.
   Enter that same generation in `confirm` to authorize stopping the applications.
   The driver first reruns the permission preflight and waits for success before
   recording recovery state, setting the source fence, or stopping an application.
   A failed preflight leaves the existing serving/fence state unchanged.
3. The driver saves active source revision names, sets `copying:<generation>`,
   and deactivates SAGE and BFF revisions. It explicitly starts the job in `migrate`
   mode by updating its arguments and starting the deployed template without
   execution overrides, under the tenant lock. The job bootstraps only the replacement identities/extensions,
   rechecks ownership privileges, revokes source workload CONNECT (including PUBLIC),
   terminates existing client sessions, and refuses remaining connections or
   prepared transactions. Inherited CONNECT privileges or another non-superuser login retaining CONNECT
   cause failure. Do not introduce administrator writes during this window.
4. PostgreSQL's custom-format logical archive includes all workload schemas and
   tables, including canonical graph edges and their rationale/retraction state,
   document metadata and version chains, indexed projection chunks and embeddings,
   any database-resident registry state, and BFF sessions. External source files
   and vault configuration remain in their existing document store; preserve those
   coordinates and verify source reads separately. It preserves object owners and
   ACLs; it never reconstructs human-curated edges from content.
5. Restore runs in one transaction with exit-on-error. Reconciliation compares
   every workload table's row count and ordered content hash, column definitions,
   indexes, constraints, table/schema owners and ACLs, sequence state and definitions
   (data type, start, increment, bounds, cache, and cycle), default
   grants, workload function/procedure definitions, owners and ACLs, trigger
   definitions and enabled state, and the required extension versions. Unsupported
   relation or routine kinds (including workload aggregates) fail closed. Review the
   job's report and target `_cas_migration.checkpoint` record.
6. Only successful reconciliation sets `verified:<generation>`. Applications stay
   stopped and source CONNECT stays revoked. The archive is temporary job-local
   data and is removed when the job ends; it is not a retained disaster-recovery
   backup. The incumbent server remains intact.

Retries require identical source/target identities, major versions, generation,
and reconciled content. A nonempty destination without a verified checkpoint is
rejected. A partial or ambiguous result is not permission to erase it: investigate
and explicitly select recovery or a fresh replacement. Do not edit checkpoint
records to force acceptance.

## Cutover and acceptance

Set the intended environment's persistent `POSTGRES_GENERATION` to the verified
generation, then explicitly dispatch the normal `infra` workflow from the
approved main revision. Its shared module switches SAGE, BFF, bootstrap, and
maintenance together. During deployment the fence becomes `cutover:<generation>`;
a retry may target only that generation. Successful serving preflight advances
it to `serving:<generation>`, permitting normal maintenance while preventing an
accidental empty-generation switch back.

Before declaring cutover complete, inspect the stored job/app configurations to
confirm all four consumers name the replacement FQDN. Verify the actual server
major, backup configuration and deletion lock. Run the authenticated SAGE read
and write smoke and BFF sign-in/session checks; verify representative chains,
retracted edges, curated rationale, source/projection reads and vector search.
Inspect the migration report for every vault and BFF, not merely aggregate counts.
Retain the source until this evidence has been reviewed and retirement is
explicitly authorized.

Only after live acceptance, use a separate cleanup change to raise
`postgres.deploy_major`, remove the temporary PostgreSQL 16 CI job, and update the
actual branch ruleset's required check. Neither CI-floor removal nor source-server
retirement is part of preparation. Removing the incumbent deletion lock and
server requires a separate explicit retirement decision.

## Recovery boundaries

Before cutover has started (`copying` or `verified`), dispatch action `rollback`
with the same generation and confirmation. It refuses active database jobs,
reruns the standing source bootstrap to restore the standard workload grants,
reactivates the recorded source revisions, and releases the migration tag. The
replacement is retained. Run the normal serving deployment and preflight before
announcing recovery. PUBLIC CONNECT stays revoked; workload access uses its
explicit least-privilege grants.

Once cutover has started, automated source rollback is refused. New target writes
may already exist even if preflight later fails. Keep both servers, stop further
writes as required, inspect which database contains accepted work, and plan a
reconciled recovery. A blind switch to the old FQDN would lose those writes.

Azure automated backup history belongs to each server. Logical migration does
not transfer the source's earlier point-in-time restore history to the new server.
The replacement begins its own history; retain the incumbent for the agreed
pre-cutover recovery horizon. Inspect Azure's actual earliest restore point before
relying on a timestamp. A 35-day retention setting does not mean a new server
already has 35 days of history.

For accidental data loss, point-in-time restore creates a separate server. Keep
the damaged server and validate the restored database, network access, identities,
and application behavior before another explicit consumer switch. Geo-restore is
for regional loss, uses asynchronously copied backups and can lose recent writes;
it is not synchronous replication, high availability, or zero-RPO failover.
Verify geo-backup availability and destination-region support before relying on
it. Restored servers need their own inspected backup policy and deletion lock.
Use a reviewed deployment/recovery change to adopt their coordinates; never
silently replace the configured serving generation.

See Microsoft's [backup and restore model](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-backup-restore)
and [geo-disaster recovery](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-geo-disaster-recovery),
and PostgreSQL's [logical dump compatibility](https://www.postgresql.org/docs/17/app-pgdump.html).
