# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's private
vulnerability reporting: the **Report a vulnerability** button on this
repository's **Security** tab.
Do not open a public issue, pull request, or discussion for a suspected
vulnerability.

A useful report includes:

- the component affected (the SAGE Core API, the MCP surfaces, the CAS
  Application or its backend, the container images, or the deployment
  templates under `infra/`);
- the version or commit you tested against;
- steps to reproduce, and what an attacker gains.

Fixes are developed in the advisory's private fork and published together
with the advisory once released.

## Supported versions

Only the latest release on the default branch receives security fixes. There
are no maintained release branches.

## Scope

In scope: code, configuration, workflows and deployment templates in this
repository.

Out of scope: vulnerabilities in third-party dependencies that are already
publicly disclosed (report those upstream; this repository tracks them through
Dependabot and the dependency audits in CI), and findings that require an
already-compromised host or administrator credentials.
