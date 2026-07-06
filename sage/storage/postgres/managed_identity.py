"""Managed-identity Entra token auth for the cloud Postgres pool (CAS-ADR-042).

The cloud Postgres endpoint (Azure Database for PostgreSQL Flexible Server) is
provisioned Entra-only -- password authentication is disabled -- so the cloud
storage binding authenticates with a Microsoft Entra access token minted by the
workload's managed identity and presented as the libpq password. The token is
short-lived, so it is acquired fresh on every connection the pool opens rather
than baked into a static conninfo; the shared credential's own cache mints a new
token only as the cached one nears expiry. The psycopg and azure imports are
deferred to call time, so an on-box local-profile process never loads them.
"""

from __future__ import annotations

# OAuth scope for an Azure Database for PostgreSQL Entra access token. The token
# minted for this scope is presented to libpq as the connection password.
POSTGRES_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

_credential = None


def get_postgres_credential():
    """Return a process-wide aio managed-identity credential (cached).

    One credential instance is reused across every connection so its internal
    token cache is shared: a fresh access token is minted only when the cached
    one nears expiry, not on every connect. ``DefaultAzureCredential`` resolves
    the user-assigned identity from ``AZURE_CLIENT_ID`` in the environment.
    """
    global _credential
    if _credential is None:
        from azure.identity.aio import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    return _credential


async def close_postgres_credential() -> None:
    """Close and drop the cached aio managed-identity credential, if one was built.

    The aio ``DefaultAzureCredential`` holds an ``aiohttp`` session; a long-lived
    server keeps it for the process lifetime, but a short-lived job should close it
    at shutdown so the session and its connector are released cleanly rather than
    surfacing ``Unclosed client session`` warnings on interpreter exit. Idempotent
    and a no-op when no credential was ever built.
    """
    global _credential
    if _credential is not None:
        await _credential.close()
        _credential = None


def make_token_auth_connection_class(credential, *, scope: str = POSTGRES_AAD_SCOPE):
    """Build an ``AsyncConnection`` subclass that authenticates with an Entra token.

    The returned class overrides ``connect`` to acquire a fresh access token from
    ``credential`` for ``scope`` and inject it as the libpq ``password`` on every
    connection the pool opens. A token-acquisition failure fails closed as a
    ``RuntimeError`` -- it never attempts an unauthenticated connection.
    """
    import psycopg

    class _ManagedIdentityAsyncConnection(psycopg.AsyncConnection):
        @classmethod
        async def connect(cls, conninfo: str = "", **kwargs):
            from azure.core.exceptions import AzureError

            try:
                access_token = await credential.get_token(scope)
            except AzureError as exc:
                raise RuntimeError(
                    "failed to acquire a managed-identity access token for the "
                    f"cloud Postgres endpoint (scope {scope!r}): {exc}"
                ) from exc
            kwargs["password"] = access_token.token
            return await super().connect(conninfo, **kwargs)

    return _ManagedIdentityAsyncConnection
