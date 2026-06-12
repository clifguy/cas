# Infrastructure modules

Hosting-environment Bicep modules for the CAS cloud deployment profile
(CAS-ADR-042). The orchestrator at [`../main.bicep`](../main.bicep) targets
the subscription, creates the resource group, and deploys each module into
that group.

## Authoring convention

- **One concern per module.** Each Azure concern is a single
  `modules/<concern>.bicep` file (foundation networking + compute
  environment, the relational store, the secrets vault, the API facade, …).
- **Resource-group scoped.** Modules declare resources at resource-group
  scope. The orchestrator deploys them with `scope: rg`; a module does not
  re-create the resource group.
- **Parameterized, never hardcoded.** Take `location` and `tags` from the
  orchestrator. Identity coordinates (subscription, tenant, client ids) and
  secrets are never written into a module — they arrive as parameters,
  deployment-time variables, or managed-identity references.
- **Foundation first.** The foundation module establishes the shared
  network and compute environment that later modules consume via outputs;
  it is the first module wired into the orchestrator.
- **Composed through outputs.** A module that depends on another consumes
  the producer's `output` values through the orchestrator rather than
  reaching across module boundaries.

## Wiring a module

Add the module to [`../main.bicep`](../main.bicep), scoped to the resource
group, passing `location` and `tags`:

```bicep
module foundation 'modules/foundation.bicep' = {
  name: 'foundation'
  scope: rg
  params: {
    location: location
    tags: tags
  }
}
```
