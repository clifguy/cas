# Proposed guide fragment and dependency interfaces

**INACTIVE proposal.** Do not copy this fragment or `project.json` into an active
guide/profile without separate owner approval of the complete transition.

## Fragment for a disposable fixture or approved future guide

After activation, the guide would explicitly designate
`docs/development/project-policy.md` as project-owned development policy and
`.development-skills/project.json` as its supported v3 operation bindings.
The canonical CAS code-review file and its existing agent pointer remain in place.
The fragment does not govern the repository containing this proposal.

## Dependency keys

The machine-readable map uses descriptive keys; these name delivery ownership,
not completion or permission to substitute an interface.

| Key | Delivery dependency | Required interface |
|---|---|---|
| ticketing | T-0005 | Project-routed ticket-management with live lineage, schema and authorization |
| specialized-operations | T-0006 | Existing specialized operations, including conditional Azure review, with explicit policy resolution |
| external-integration | T-0007 | Verified SAGE/transfer replacement; retain working transfer until accepted |
| distribution | T-0008 | Complete compatible package and manifest; no partial install |
| acceptance | T-0009 | Fresh runtime/installed discovery and observed agent acceptance |
| CI | T-0010 | Automated package and conformance gates |

Coordinated authority activation, installation and rollback are separately named
owner decisions; no sibling coding ticket substitutes for that approval.

Core operations may read a local required procedure while a dependency remains
undelivered. They must then report the dependent step blocked, never report the
capability implemented because the local binding resolved.

## Fixture use

Use an isolated repository copied from the exact candidate. Copy `project.json`
to `.development-skills/project.json` there only, and explicitly install this
fragment's policy pointer into that fixture's guide. Run the shipped shared
resolver with its verified complete bundle. Missing-authority and rename probes
mutate the fixture only. Record this as fixture acceptance, not active adoption.
