"""Small shared filesystem primitives; no installation policy or discovery."""

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath


def regular_path(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("path traversal refused: " + str(path))
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError("symlink path refused: " + str(part))
        if part.exists() and not (part.is_file() or part.is_dir()):
            raise ValueError("nonregular path refused: " + str(part))
    return path


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError("path escape or noncanonical path: " + value)
    return value


def name(value):
    import re

    if (
        not isinstance(value, str)
        or not re.fullmatch("[a-z][a-z0-9-]*", value)
        or value == "manifest"
    ):
        raise ValueError("invalid component name: " + str(value))
    return value


def tree(path):
    """Exact bytes, modes and empty directories, without following links."""
    path = regular_path(path)
    if not path.exists():
        return None

    def entry(p):
        mode = stat.S_IMODE(p.stat().st_mode)
        if p.is_dir():
            return {"type": "directory", "mode": mode}
        if p.is_file():
            return {
                "type": "file",
                "mode": mode,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
        raise ValueError("nonregular entry: " + str(p))

    result = {".": entry(path)}
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            regular_path(p)
            result[p.relative_to(path).as_posix()] = entry(p)
    return result


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    return json.loads(regular_path(path).read_text(), object_pairs_hook=pairs)


def atomic_json(path, value):
    """Atomic metadata replacement in the same directory; fsync data and directory."""
    import tempfile

    path = regular_path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_exclusive(source, destination):
    """No-overwrite root rename on supported local POSIX hosts; fail closed."""
    import ctypes
    import sys

    source, destination = regular_path(source), regular_path(destination)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        fn = library.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        fn = library.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        arguments = (
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise ValueError("exclusive rename requires macOS or Linux renameat2 support")
    fn.restype = ctypes.c_int
    if fn(*arguments) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))
