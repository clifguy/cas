"""Runtime secret resolution for the cloud deployment profile (CAS-ADR-042).

The cloud profile sources its secrets from a managed secret store at runtime
using the workload's managed identity, so no secret value lives in the
environment, the container image, or the repository. The local profile reads
its secrets from the environment and never imports this package.
"""
