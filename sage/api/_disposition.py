"""The headers a delivered source or projection file is sent with."""

import re
from urllib.parse import quote

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def attachment_disposition(filename: str) -> str:
    """An ``attachment`` disposition naming ``filename``.

    Control characters are dropped and backslashes and quotes replaced, so the
    header is always well formed whatever name a document was retained under;
    a non-ASCII name is also carried percent-encoded in ``filename*``.
    """
    name = _CONTROL.sub("", filename).replace("\\", "_").replace('"', "_")
    disposition = f'attachment; filename="{name}"'
    if not name.isascii():
        disposition = (
            f'attachment; filename="{name.encode("ascii", "replace").decode()}"; '
            f"filename*=UTF-8''{quote(name)}"
        )
    return disposition


def delivery_headers(filename: str, size: int) -> dict[str, str]:
    """The headers a raw byte delivery is sent with, beside its media type.

    The bytes are whatever a document's author put there, so a browser is told
    to save them rather than render them, not to second-guess the declared
    media type, and -- should it render them anyway -- to treat them as a
    sandboxed document with no script, origin or navigation of its own.
    """
    return {
        "Content-Disposition": attachment_disposition(filename),
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }
