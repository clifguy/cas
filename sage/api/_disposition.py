"""The ``Content-Disposition`` header for a delivered source file."""

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
