# Cloud PostgreSQL replacement and recovery

The serving server remains the database of record until an explicitly dispatched
cutover. Provisioning a replacement does not move SAGE, the BFF, bootstrap, or
maintenance. The empty `postgresGeneration` preserves the original server name;
a stable nonempty generation creates a second server through the shared module.
Both servers retain their declared deletion locks.

## Preparation and verification boundary

The accepted serving baseline is PostgreSQL 17. Keep the persistent
`POSTGRES_GENERATION` set to the verified generation (`pg17` for the completed
16-to-17 cutover), and retain `serving:pg17`. Normal deployment must use the
populated server for SAGE, BFF, bootstrap and maintenance. An empty-generation
request against this fenced environment is rejected; it is not a rollback.

The retained tooling describes only the original offline 16-to-17 migration.
`postgres.migration.source_major` and `postgres.migration.target_major` fix those
endpoint versions independently of the current deployment/development baseline.
They are historical recovery constraints, not new upgrade targets. A future
`dev_major` change cannot alter the accepted generation's major. Another migration
requires a separately reviewed design covering generation-specific majors,
admission from the serving state and rollback to that state. Never clear the
serving fence to reuse the original migration procedure.

For the original preparation, the image was deployed while the baseline remained
16 and generation selection was empty. Those pre-cutover instructions are not a
procedure to run against the current serving baseline.
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

Baseline cleanup converges deployment and development to 17, removes the redundant
PostgreSQL 16 CI job and coordinates removal of its live required check. It does
not retire the incumbent or authorize another cutover. Preserve the fixed migration
contract and both deletion locks. The source's recovery horizon remains an explicit
owner decision attached to its separate retirement work.

Keep the complete migration report and acceptance results in the approved durable
operational evidence location before temporary files or workflow artifacts expire.
Include exact workflow/execution links, image and commit identities, every table's
reconciliation, read/write and source-hash results, graph checks and any unavailable
case. Distinguish an existing BFF session from a fresh interactive sign-in. Record
the next scheduled maintenance execution and its result against the selected server.
A configured backup is not a demonstrated restore: either perform an explicitly
approved isolated restore exercise or link an owned follow-up. Do not mark missing
acceptance as passed because deployment succeeded.

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

## Isolated restore verification

A configured backup is not a demonstrated restore, and a restore that returns a
healthy-looking database has not thereby proved it returned the moment it was
asked for. The drill below answers both questions against a throwaway server and
leaves the serving server untouched. It is driven by
`deploy/postgres-restore-verify.py`, which has three actions — `provision`,
`verify`, `cleanup` — and reads every coordinate it needs from the serving
deployment's outputs and its bootstrap job. The driver never connects to a
database; every database read happens inside the verification job.

### Bracket the recovery point before restoring

Choose the recovery point first and make it falsifiable. Write one document to the
disposable validation vault, wait, fix the recovery point, wait again, and write a
second. The restore must then contain the first and not the second. Without that
bracket a drill cannot distinguish a genuine point-in-time restore from a copy of
the current database, because both look correct.

Record the live coordinates before starting, and in particular the server's
**actual earliest restore point**. A 35-day retention setting is a ceiling, not
history the server has accumulated: a server created during a recent cutover may
hold only hours. The driver refuses a recovery point outside that real window
before it creates anything billable.

### Run the drill

```sh
python3 deploy/postgres-restore-verify.py provision \
    --environment prod --resource-group rg-cas-prod \
    --run-id <id> --restore-time <ISO 8601 with offset>

python3 deploy/postgres-restore-verify.py verify \
    --environment prod --resource-group rg-cas-prod --run-id <id> \
    --target-fqdn <fqdn from provision> --restore-time <same point> \
    --sentinel-schema <vault schema> \
    --sentinel-before <document id> --sentinel-after <document id> \
    [--image <pinned tag from the tenant registry>]

python3 deploy/postgres-restore-verify.py cleanup \
    --environment prod --resource-group rg-cas-prod --run-id <id>
```

`--image` exists because the drill legitimately runs the verification entrypoint
before the change carrying it has been deployed. An override must be pinned and
must come from the tenant's own registry; anything else would pull an arbitrary
container into the subnet that holds the database of record.

Read the report from the execution's logs. It is a single
`CAS_RESTORE_VERIFY_REPORT` line, and the container has to be named explicitly —
it is `restore-verify`, not the job name, so a `logs show` that omits
`--container` does not resolve:

```sh
az containerapp job logs show --resource-group <rg> \
    --name job-pg-restore-verify-<env> --container restore-verify \
    --execution <execution name> --tail 300 --format text
```

The execution name is the `execution` field of `verify`'s printed payload, and on
the timeout path it is named in the error. Do not recover it from an execution
listing: a just-started execution is briefly absent from that list, so the newest
entry there can be a previous run.

`verify` exits non-zero when the job reaches a terminal failure, so a wrapping
script can read the exit code rather than parsing the payload for a status.

Expect the restore itself to dominate the elapsed time. A 32 GB Burstable server
reached `Ready` in about seven minutes; hashing all 37 workload tables afterwards
took under fifteen seconds at 1 vCPU, so the job's replica timeout is generous by
a wide margin at this data scale.

### What the verification asserts

The job runs `sage.maintenance.postgres_restore_verify` under the bootstrap
identity, in a session made read-only by connection option so nothing it runs can
relax it, and refuses by address to connect to the serving server at all. It
reports, and fails closed on: the server major; every workload table's row count
and ordered content hash; both workload roles, their database CONNECT and CREATE
grants and their EXECUTE on `pgstattuple`; the required extensions with their
version and schema; and the two sentinels. A missing before-sentinel is reported as
`recovery_point_undershoot` and a present after-sentinel as
`recovery_point_overshoot`, each distinct from an ordinary failure.

### Observed behaviour worth knowing in advance

- **The restore carries the Entra administrator across.** The bootstrap identity
  is already the Postgres administrator on the restored server; no manual grant
  is needed before the job can authenticate.
- **The restored server's lineage is not visible on the resource.**
  `sourceServerResourceId` reads null afterwards, so provenance must come from the
  drill's own records rather than from Azure's resource state.
- **Resource tags are inherited from the source.** The drill's ownership mark is
  therefore written incrementally; a plain tag write would replace the whole
  collection.
- **The private DNS record is created automatically** when the restore is given
  the zone, and removed with the server.

### Accepted limitations

The restored server sits in the serving subnet and private DNS zone. That is not
an oversight: an Azure restore of a private-access server yields another
private-access server, and nothing can reach it unless it sits where the
verification job already runs. Isolation is by resource and address, not by
network. The serving server's own record and every application binding are
untouched, and the drill server carries no deletion lock precisely so that
teardown is a single operation.

**A point-in-time restore does not demonstrate regional recovery.** Do not report
a successful point-in-time drill as regional disaster-recovery evidence; the
[regional recovery drill](#regional-recovery-drill) below is the separate exercise
that does.

### Teardown

Run one drill at a time per environment. The verification job is named for the
environment rather than the run, so a `cleanup` for one drill deletes the job a
concurrent drill is using, and a second `verify` redeploys that job under the
other drill's parameters.

`cleanup` deletes only a server carrying this drill's ownership tag, and proves
removal by re-reading rather than by trusting the delete's exit code. A
drill-named server without that tag is reported in cleanup's `inspect` list for an
operator to look at, and is never adopted automatically — the same discipline the
rehearsal cleanup applies.
Confirm afterwards that both production servers, their majors, their backup
configuration and both deletion locks are unchanged, and that the private DNS zone
is back to its pre-drill record set. Delete the throwaway image tag and the
sentinel documents once the evidence record is filed.

## Regional recovery drill

A geo-restore answers a different question from a point-in-time restore: whether
the serving server's backups, copied asynchronously to the paired region, can be
turned into a working server there. It cannot choose its recovery point. Azure
restores to the last data that reached the paired region, and documents the
recovery point objective for geo-restore as under one hour, against under five
minutes for point-in-time restore. A geo drill therefore measures the recovery
point rather than asserting one.

### Why the drill needs its own footprint

Azure will not geo-restore a private-access server onto a public endpoint; the
request must name a delegated subnet and a private DNS zone in the destination
region. The serving network is in the wrong region, and the destination region's
other networks belong to other workloads, so the drill builds its own. The
verification job also has to run there, because the serving Container Apps
environment cannot reach a server in another region's network.

The footprint is **per drill**, not permanent. `footprint` creates a resource group
named `rg-cas-<env>-geodrill-<run id>` in the destination region and deploys
`infra/modules/postgres-geo-drill-footprint.bicep` into it: a virtual network with
the two delegated subnets, a private DNS zone linked to it, a Log Analytics
workspace, and an internal Container Apps environment. The restored server and the
verification job go into the same group, and teardown deletes the group.

The trade-off, stated so it can be revisited:

- **Per drill** costs only while a drill exists, dominated by the restored server
  for the hour or two the drill runs, and adds a few minutes of setup. The serving
  template and resource group gain nothing, and there is no standing
  infrastructure in the destination region to maintain or to mistake for a
  failover capability.
- **Permanent** would cost little at idle, but it would enter every tenant deploy's
  validation and what-if, and it would amount to the network half of a failover
  region with no applications, secrets vault or API facade behind it. Adopt it only
  as part of a real regional failover design, where its value is recovery time.

Isolation is by network as well as by resource. Unlike the point-in-time drill, a
geo drill never places anything in the serving subnet or private DNS zone.

### Bracket and tolerance

The point-in-time bracket asserts an exact boundary; a geo drill states a
tolerance instead. Write the first sentinel at least one documented recovery point
objective before the request, then a stream of sentinels at a fixed interval up to
the request, then one after it. The restored copy must contain the first and not
the last. Where the stream ends inside the copy bounds the observed recovery point
between the last stream sentinel present and the first absent.

The report's row count for the sentinel schema's `documents` table locates that
boundary without changing the verifier: subtract the count before the stream
began. The inference holds only if nothing else wrote to that schema during the
window, so record the vault's document list, with creation times, before and
after, and check it.

### Run a geo drill

```sh
python3 deploy/postgres-restore-verify.py footprint \
    --environment prod --resource-group rg-cas-prod \
    --run-id <id> --geo-location <paired region>

python3 deploy/postgres-restore-verify.py provision \
    --environment prod --resource-group rg-cas-prod --run-id <id> \
    --restore-time <request time with offset> --geo-location <paired region> \
    --geo-subnet <postgres_subnet_id from footprint> \
    --geo-dns-zone <private_dns_zone_id from footprint>

python3 deploy/postgres-restore-verify.py verify \
    --environment prod --resource-group rg-cas-prod --run-id <id> \
    --geo-location <paired region> --target-fqdn <fqdn from provision> \
    --restore-time <same request time> --sentinel-schema <vault schema> \
    --sentinel-before <first sentinel id> --sentinel-after <last sentinel id>

python3 deploy/postgres-restore-verify.py cleanup \
    --environment prod --resource-group rg-cas-prod --run-id <id> \
    --geo-location <paired region>
```

`--resource-group` always names the serving group, from which the driver reads its
coordinates; every write lands in the drill's own group. The driver refuses, before
creating anything: a destination in the serving region, or in any region other than
its Azure pair, since geo-redundant backup restores only into the pair; a serving
server without geo-redundant backup; a drill group that already exists; and a subnet or private
DNS zone outside the drill's own group, which is how it enforces not borrowing
another workload's network. The geo-restore names the source server by resource
id, because the target group holds no server of that name.

Read the report as for the point-in-time drill, with the drill group as the
resource group of `az containerapp job logs show`.

### Observed behaviour of a geo drill

Measured on the first drill against the serving PostgreSQL 17 server, East US 2 to
Central US, at roughly 26,000 workload rows. One observation at low write volume,
so read the recovery point as an instance rather than a bound.

- **The footprint is quick.** The resource group, network, zone, workspace and
  internal Container Apps environment deployed in about three minutes.
- **A geo-restore takes about as long as a point-in-time restore.** The restored
  server reached `Ready` about eight minutes after the request.
- **The observed recovery point was minutes, not the hour Azure allows.** Every
  sentinel written up to 2 minutes 21 seconds before the request was present, and
  the one written 33 seconds after was absent. At a lower write rate or a less
  convenient moment in the backup copy, expect more; the documented tolerance
  remains the planning figure.
- **Whether `--restore-time` is honoured was not distinguished.** The request time
  was the current time, which is also the last available point, so both readings
  predict the same copy.
- **The Entra administrator carries across a geo-restore** as it does across a
  point-in-time restore; the job authenticated with no manual grant.
- **Lineage is again invisible on the resource.** `sourceServerResourceId` reads
  null, and the source's tags are inherited alongside the drill's mark.
- **The private DNS record is created in the drill's zone automatically**, at an
  address in the drill subnet; the serving zone's record set is unchanged.
- **Teardown dominates the drill's wall-clock time.** Deleting the group took about
  26 minutes, longer than building and verifying combined.

### Teardown of a geo drill

`cleanup --geo-location` deletes the drill group only if the group carries this
run's ownership tag and every resource inside it does too. The single exception is
the verification job, recognised by its exact name and type, because its module
leaves it untagged. Anything else untagged aborts the teardown and is named, rather
than being deleted with the group. The delete is issued without waiting, because it
can run longer than a single CLI call is allowed, and removal is proven by polling
the group's existence under an hour's budget. An overrun is reported as such, not
as a removal; Azure keeps deleting, and a run whose group is already gone is a
no-op, so a teardown interrupted part way can simply be rerun.

Deleting the Container Apps environment also removes the managed infrastructure
group Azure creates for it; confirm that too. The Log Analytics workspace enters
Azure's soft-deleted state rather than disappearing, which is harmless because its
name is unique to the run.
