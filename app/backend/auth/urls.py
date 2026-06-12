"""External-URL construction that honors the container ingress's forwarded headers.

Behind the container ingress the backend terminates TLS at the edge, so the
request the application sees arrives over plain HTTP carrying the original
scheme and host in ``X-Forwarded-Proto`` / ``X-Forwarded-Host``. The OIDC
redirect URI must be built from those forwarded values, not from the request's
own scheme/host, or the redirect URI will not match the one registered with the
identity provider and the sign-in will fail.
"""

from __future__ import annotations

from starlette.requests import Request


def external_base_url(request: Request) -> str:
    """Return ``scheme://host`` as seen by the original client.

    Prefers the ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` values set by the
    ingress; falls back to the request's own scheme and ``Host`` header when the
    forwarded headers are absent (the direct, no-proxy case). A chained-proxy
    header may be comma-separated; the first (client-most) value wins.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto.split(",")[0].strip() if forwarded_proto else request.url.scheme

    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        host = forwarded_host.split(",")[0].strip()
    else:
        host = request.headers.get("host") or request.url.netloc

    return f"{scheme}://{host}"


def callback_url(request: Request, callback_path: str) -> str:
    """Build the absolute OIDC redirect URI from the forwarded base URL."""
    return external_base_url(request).rstrip("/") + callback_path
