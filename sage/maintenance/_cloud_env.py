"""Shared environment plumbing for the in-VNet cloud maintenance entrypoints.

The cloud maintenance jobs run as lifespan-less ``python -m`` processes, so
nothing populates the stack-config singleton; each entrypoint builds its cloud
configuration straight from the environment the job image carries. This module
holds that builder and the truthy-flag parser so every entrypoint reads the
same coordinates the same way.
"""

from __future__ import annotations

from collections.abc import Mapping


def truthy(value: str | None, *, default: bool) -> bool:
    """Parse a flag-style environment value; empty/missing yields ``default``."""
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def config_from_env(env: Mapping[str, str]):
    """Build the cloud stack config from the baked job environment.

    A lifespan-less ``python -m`` job never populates the stack-config singleton, so
    the coordinates are read straight from the environment the job carries (the same
    self-contained pattern the sibling ``cloud_bootstrap`` job uses): the private
    Postgres FQDN/database and the SAGE role to connect as, and the SharePoint
    site/drive/root the document-store binding addresses. The maintenance jobs are
    cloud-only, so the profile and vault-source backend are fixed here. A missing
    required coordinate fails loud.
    """
    from sage.config import SageCoreConfig, StackDocumentStoreConfig, StackPostgresConfig

    def _required(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"missing required environment variable {key!r}")
        return value

    return SageCoreConfig(
        profile="cloud",
        vault_source_backend="document_store",
        postgres=StackPostgresConfig(
            host=_required("PG_FQDN"),
            database=_required("PG_DATABASE"),
            user=_required("PG_USER"),
            sslmode="require",
        ),
        document_store=StackDocumentStoreConfig(
            site_id=_required("SHAREPOINT_SITE_ID"),
            drive_id=_required("SHAREPOINT_DRIVE_ID"),
            root_path=env.get("SHAREPOINT_ROOT_PATH") or "vaults",
        ),
    )
