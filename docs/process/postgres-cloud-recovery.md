# Cloud PostgreSQL replacement and recovery

The serving server remains the database of record until an explicitly dispatched
cutover. Provisioning a replacement does not move SAGE, the BFF, bootstrap, or
maintenance. The empty `postgresGeneration` preserves the original server name;
a stable nonempty generation creates a second server through the shared module.
Both servers retain their declared deletion locks.

## Preparation and verification boundary

This mechanism is deliberately for the one-time 16-to-17 migration. Execute that
migration soon after the preparation change lands, keeping its manifest versions
fixed through cutover. It is not a reusable sequence of generation upgrades:
`serving:<generation>` remains a protective terminal state and blocks another
migration. Never clear that tag to attempt a second migration.

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
also compares the source's installed `vector` and `pgstattuple` versions and schemas
with the post-bootstrap target, retaining exact parity as the final reconciliation
contract. Missing extensions or a mismatch fail preparation. Have an authorized
administrator align installed extension versions/schemas before the window (for
example, an explicitly reviewed source extension update or target version selection),
then rerun preparation; the migration tool performs no source extension update.

The source fence is rehearsed inside a rolled-back transaction: revoke the same
CONNECT grants, check inherited/residual access and session-termination privileges,
and reject prepared transactions. It does not terminate sessions during preflight.
The actual fence and quiescence checks still run after the apps stop because
permissions and connections can change after preparation. Preparation
fails if any required role or privilege is missing; the source apps and CONNECT
privileges are untouched. It does not grant migration-admin membership automatically.
Have an authorized database administrator establish the required scoped memberships
and rerun preparation; record any temporary grants and their later revocation.
The preflight report includes `capacity_estimate.database_bytes` and
`capacity_estimate.scratch_free_bytes` from the actual job. These are observations,
not an archive-size or duration guarantee: database size includes indexes and free
space, while archive compression and JSON sorting have different costs. Before
approving downtime, record a representative full-size rehearsal's archive peak
scratch usage and total dump/hash/restore duration against the same job resources.
Require measured scratch headroom and completion within both the job's 7,200-second
replica timeout and each dump/restore command's 3,000-second limit. If either bound
is unproven or exceeded, resize/reconfigure and rehearse before scheduling the window.
A disk-full or timeout after fencing still requires the recovery procedure below.

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

## Provider-owned extension privileges

Both logical restore paths capture permission metadata in the same exported,
repeatable-read snapshot consumed by `pg_dump`. An in-memory manifest binds that
metadata to the archive SHA-256. Restore refuses a different archive and compares
schema, relation, column, routine, type, language, large-object and default-grant permissions before
reporting success. Extension routine comparisons include the extension identity
and version, routine kind, exact signature, owner, grantor, recipient, privilege and grant option.
They also participate in migration reconciliation and resume fingerprints. Each installed
extension object's own name, version, schema and owner are compared separately from
its member objects, including extensions beyond the required vector/pgstattuple pair.
Native restore can recreate a trusted extension under the migration administrator
while its members still have matching owners and grants. That ownership loss now
fails permission reconciliation: seed restore cannot report success, migration cannot
write a verified checkpoint, and resume rejects a changed extension owner.

Native restore does not transfer extension ownership. An application-owned extension
can be copied when the destination already has the matching extension installed under
that owner and the administrator has the required restore rights. Otherwise the copy
fails closed; this engine does not rewrite provider catalogs or grant itself membership
to repair ownership. A failed restore can leave data in the isolated target and requires
the existing inspection and cleanup procedure. Required-extension parity preflight
alone does not certify ownership of additional extensions; restore reconciliation does.

Column grants retain the schema, relation and column identity, grantor, recipient, privilege
and grant option in both permission snapshots and full reconciliation. Row-security
comparison includes enabled/forced flags and policy names, roles, command,
permissive/restrictive mode, USING and WITH CHECK expressions, including extension relations. Ordinary and extension-defined
types (including domains, composite and table-row types) retain their identity, owner
and ACLs. Automatically derived array types have no independent grant operation and
are represented by their element type. Explicit table-row-type ACLs omitted by native
dump/restore are detected as mismatches; they cannot produce a verified checkpoint.

Language comparisons include name, owner, trusted/procedural flags and canonical ACLs.
Large objects retain their OID, owner and canonical ACLs; full reconciliation also
hashes their content in bounded chunks. A target containing a large object is not
pristine. Extension relations retain their extension identity/version, owner, ACLs,
column grants and row-security state. Table-row types derive extension identity
through their table when the server does not store a direct type membership.

The ACL catalog inventory has these explicit boundaries:

| Catalog | Handling |
| --- | --- |
| `pg_attribute`, `pg_class`, `pg_proc`, `pg_type` | Compare workload and extension object grants and owners within the supported relation/routine kinds; unsupported ordinary kinds fail. Derived arrays use their element type. |
| `pg_namespace`, `pg_default_acl` | Compare workload schemas and scoped/global default grants. |
| `pg_language`, `pg_largeobject_metadata` | Compare language and large-object ownership and grants. |
| `pg_foreign_data_wrapper`, `pg_foreign_server` | A foreign-data wrapper causes explicit rejection before restore; servers require a wrapper. This non-superuser restore does not support these objects. |
| `pg_database` | Database ACLs are deployment state: preparation checks the administrator's rights, the source CONNECT fence is verified, and rehearsal provisioning sets scratch-database access. The archive does not copy source database ACLs. |
| `pg_parameter_acl`, `pg_tablespace` | Shared cluster grants are outside a per-database archive and remain deployment preflight responsibilities, alongside roles and memberships. |
| `pg_init_privs` | Initial privilege baselines are reference metadata, not an independent effective ACL; compare the actual object's privileges. |

A verified database-copy result is scoped to these checks. It does not certify
cluster permissions, transfer database ACLs, or establish deployment readiness.

Azure installs `pgstattuple` functions owned by `azuresu`, with EXECUTE grants from
that owner to itself and `pg_stat_scan_tables`, and EXECUTE WITH GRANT OPTION to
`azure_pg_admin`. Replaying the last grant as an administrator inheriting
`azure_pg_admin` can attempt to grant an option back to its grantor. Restore handles
this exact catalog-verified relationship without replaying the provider-owned grants.
It requires extension membership and the complete grant relationship; an object name
or an Azure-looking role name is insufficient.

For these functions only, restore replaces the complete ACL archive entry, matched
by its catalog-derived schema, signature and owner. Every replacement must map to
exactly one archive entry. The installed target must have the same extension identity,
owner and provider baseline. Additional source EXECUTE grants made by `azure_pg_admin`
are replayed as that grantor, retaining the stored recipient identities and grant options.
Unexpected target grants and unsupported source grant relationships fail closed.
No permission comparison is waived, and other archive ownership and ACL entries
continue through ordinary restore. Provider ownership is verified directly rather
than requiring the migration administrator to impersonate the provider role.

The archive restore remains a single transaction. Application-grant replay follows
in a separate transaction, before permission reconciliation and any verified
checkpoint. Failure at that stage can leave restored data in the isolated target;
it does not authorize retrying over it or declaring success. Use the existing
inspection and cleanup procedure. The temporary restore selection is private and
removed on exit. Archive inspection, permission replay and verification are inside
the measured restore stage; their scratch usage and elapsed time count toward the
same limits as the rest of the operation.

Permission reports name `catalog-acl-v1-owner-maintain`. Its only cross-version ACL
normalization treats PostgreSQL 17's added MAINTAIN bit as equivalent when it is
the unchanged table owner's non-grantable self-grant alongside all seven prior
table privileges. The same rule applies to the owner's table default privileges.
It never hides MAINTAIN for an application role, PUBLIC or another recipient, a
grantable MAINTAIN privilege, or an owner change. PostgreSQL 16/17 introduce this
difference when `GRANT ALL` is replayed; the rule changes comparison only and
issues no extra grants. All other differences remain failures.

## Full-size rehearsal before downtime

After `prepare` succeeds, dispatch `postgres-migration` on the approved `main`
revision with action `rehearse`, the same environment and generation (`cor-prod`,
`pg17` for this transition). Leave `rehearsal_run_id` empty: the workflow selects
`r<GitHub run id>`. Record that identity for inspection and cleanup. The serving
preparation image must include the rehearsal runtime and both client majors;
redeploy that image with `POSTGRES_GENERATION` still empty, then rerun preparation.

The job reads a logical snapshot of the serving database using the PostgreSQL 16
seed client, restores it into an isolated PostgreSQL 16 database, then runs the
actual migration engine with the PostgreSQL 17 client against an isolated
PostgreSQL 17 destination. The seed client is separate because a PG17-produced
archive contains settings PG16 cannot restore. The measured migration still uses
the same client, job image, private network, 1 CPU, 2 GiB memory and 7,200-second
replica timeout as production migration. All dump/restore commands retain their
3,000-second limit. These one-time client/version bindings must be retired with
the eventual migration-baseline cleanup, not before cutover.

Database names are `cas_rehearsal_<generation>_r<run id>_source` and
`cas_rehearsal_<generation>_r<run id>_target` (generation hyphens become underscores),
on the incumbent and replacement respectively. They are created with connections
disabled, marked with the run's ownership/serving identities, and denied PUBLIC
access before connections are enabled. No new global role memberships are granted.
The administrator must already have database creation and restored-owner privileges;
missing privileges remain a live prerequisite. The serving database and final
migration destination receive no rehearsal tables or checkpoints, no CONNECT
revocation, and no session termination. Scratch databases add storage and load to
both servers: production stays online, but this is not a zero-impact read.

The seed is representative of logical contents at seed time. It does not account
for later production writes, physical bloat, or changing contention; source database
and clone sizes are both reported. Exact reconciliation is between the frozen
rehearsal source and its target, not between a past snapshot and changing production.
Repeat the rehearsal if data scale, extensions, image or resources change materially
before downtime. Preparation and these measurements do not establish application
acceptance after cutover.

The workflow uploads `postgres-rehearsal-<GitHub run id>` containing an execution
receipt and, when available, `rehearsal-report.json`, retained for 90 days. Copy the
report to the approved long-term operational evidence location before expiry. The
runtime emits the sanitized `CAS_REHEARSAL_REPORT` JSON record into Log Analytics;
the collector selects the exact job, execution replica prefix and rehearsal id,
waiting up to five minutes for ingestion. Missing, conflicting or failed reports
fail the workflow. A receipt alone is not a passing rehearsal. Handled failures retain collected
measurements, identities and timings with a safe `stage` and `reason` code; fields
for work not reached are `null`. Partial archive sizes describe bytes written,
not verified archives. Failure reports remain failures in the collector. The console table
uses `ContainerJobName_s` (not the system table's `JobName_s`) and
`ContainerGroupName_s`; the execution receipt supplies both selection values.

Review `status`, the image/commit and identities, seed and archive sizes, table/row
counts, reconciliation fingerprint, per-stage timings, total measured migration
time and whole-job elapsed time. Scratch observations include both seeding and
migration, sampled every 50 ms and at phase boundaries; `observed_peak_scratch_bytes`
is a measured high-water observation, not a guarantee against shorter unseen peaks.
The admission policy requires at least 256 MiB remaining scratch, initial free
space at least 125% of the observed use/archive requirement, and completion inside
the job and command limits. Missing or failed measurements never satisfy that policy.
If the job is killed before its report, it has not passed, even if a scratch
checkpoint exists. Resize/reconfigure deliberately and repeat when bounds are unproven.

The 1-vCPU rehearsal also enforces 25% headroom against Azure's 4-GiB
ephemeral-storage limit, even if the underlying filesystem reports more free space.
See [Azure ephemeral storage limits](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts).

Scratch databases remain after success or failure for inspection; they are not
retained disaster-recovery backups. Dispatch action `cleanup-rehearsal` with the
original `rehearsal_run_id` and generation. Cleanup checks both ownership markers
and database owners before deleting either, refuses active connections, and never
uses forced deletion or session termination. Missing already-cleaned databases
are accepted; foreign or unmarked databases are refused for administrator inspection.
A cancellation between database creation and its ownership mark deliberately leaves
an ambiguous database that automatic cleanup cannot adopt. Do not copy a marker or
edit one to force cleanup. An administrator must inspect and explicitly authorize
removal of that specific orphan.

Do not rerun `rehearse` over existing scratch databases: inspect and clean the
original run, then dispatch with a new identity. Cancellation of GitHub does not
stop an Azure execution. All shared workflow guards now inspect active migration-job
executions before admitting deployment, maintenance or migration. Wait for the
execution to stop (or explicitly stop the named Azure execution) before cleanup or
retry; keep the same execution receipt if report collection needs to be retried.
Rehearsal/cleanup require an empty production migration fence and never advance it.
Confirm the serving revisions, incumbent protection, unchanged consumer coordinates
and idle database jobs after the rehearsal and cleanup before proposing downtime.

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
   The driver first reruns the extension and source/target permission preflight and waits for success before
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

App-tier convergence validates both apps before restarting or activating either.
The successful serving deployment supplies each app's approved revision name,
image tag, and an ARM `guid()` fingerprint of its exact generated cloud configuration.
The driver checks the deployment's generation and resource group against the workflow
inputs and the migration fence, verifies each revision's image and configuration
mount, and compares the stored configuration value with that fingerprint. The
fingerprint is a drift check, not a credential or an authorization token; approval
comes from the controlled serving deployment. Configuration values are never logged.

When the approved revision is already the sole active revision, convergence restarts
it to reload the app-level configuration. When no revision is active, convergence
activates the deployment-approved revision and checks that it becomes the sole active
revision. This handles the stopped state left by migration, including a configuration-only
cutover that creates no new revision. Saved source rollback revisions are not target
activation authority. Missing approval evidence, conflicting active revisions, a
configuration/image mismatch, or an Azure read or activation failure stops deployment.
Inspect the reported condition and retry the same approved serving deployment after
resolving it; do not select an arbitrary historical revision or clear the source fence.
A retry after one app activated safely restarts that app and activates the remaining
approved revision. Readback establishes activation only: the subsequent authenticated
preflight must still pass before the fence advances to serving.

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
server requires a separate explicit retirement decision. That cleanup must adopt
the verified generation and its exact major as the new serving baseline before a
future `postgres.dev_major` change; a nonempty generation currently selects that
manifest major. Repeated migration requires a separate reviewed change covering
explicit per-generation majors, admission from the current serving state, and
rollback to that state. The operator step is to land and validate that baseline
adoption, not to delete the serving fence or select an empty generation manually.

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

During the overlap, maintenance snapshots produced by the newer `pg_dump` require
its matching `pg_restore` client. Use the migration runtime's client when inspecting
or restoring those archives; do not assume the incumbent server's older client can
read them.
