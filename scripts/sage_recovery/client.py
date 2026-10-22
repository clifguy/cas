"""Scoped HTTPS client for explicitly authorized hosted SAGE restoration."""

import fcntl
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import settings

_TOKEN_LOCK = threading.RLock()


def _publish_token(data):
    # Publish a complete private cache atomically; readers never see a truncated file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=settings().token_file.parent,
            prefix=settings().token_file.name + ".",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, settings().token_file)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def token():
    # The thread lock and file lock cover reads as well as publication. Separate
    # observer/controller/verifier processes therefore share one refresh owner.
    with _TOKEN_LOCK:
        lock_path = settings().token_file.with_name(settings().token_file.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(settings().token_file.read_text())
            if int(data.get("expires_on", 0)) < time.time() + 180:
                scope = settings().audience + "/.default"
                data = json.loads(
                    subprocess.check_output(
                        ["az", "account", "get-access-token", "--scope", scope, "--output", "json"],
                        text=True,
                        timeout=60,
                    )
                )
                if (
                    not isinstance(data.get("accessToken"), str)
                    or not data["accessToken"]
                    or int(data.get("expires_on", 0)) <= time.time() + 180
                ):
                    raise RuntimeError(
                        "Refreshed authentication cache is invalid or expires too soon"
                    )
                _publish_token(data)
            return data["accessToken"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused for authenticated SAGE request")


def request(path, method="GET", body=None, timeout=15):
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Only a path on the verified SAGE origin is accepted")
    headers = {"Authorization": "Bearer " + token(), "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 - config validates the HTTPS origin
        settings().base_url + path, data=data, headers=headers, method=method
    )
    start = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as resp:
            code, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        code, raw = err.code, err.read()
    return {
        "status": code,
        "seconds": time.monotonic() - start,
        "body": json.loads(raw)
        if raw.lstrip().startswith((b"{", b"["))
        else raw.decode(errors="replace"),
    }
