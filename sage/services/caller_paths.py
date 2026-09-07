"""Reading a path that names the caller's machine, not this one.

Under the caller-local transfer channel (CAS-ADR-045) a path argument does
not always name this process's filesystem. Where the deployment is
co-located with its caller, the server is the writer and the path is its
own; where it is not, the path names a machine the server cannot see and
the caller's environment does the writing. Both arms answer the same
question -- is this path absolute -- and they must answer it differently,
because absoluteness is not intrinsic to a path. It is relative to the
platform reading it.

Nor is absoluteness the only reading that turns on the platform. Reducing
a path to its basename splits on separators, and which characters separate
is the same question in a different coat -- so ``caller_basename`` lives
here too, rather than beside the staging code that consumes it.

These live here rather than in any one service so more than one service
can ask without either owning the answer. Three modules import them: the
document and projection read paths on the download leg, and the delivery
gate on the upload leg. The restore tool asks nothing of its own -- it
reads the answer the gate already published, which is the shape the rest
should tend toward, because a site that re-derives is a site free to
drift. They have drifted apart before -- one carried no check, two carried
the wrong platform's -- and a copy per site leaves the next divergence
just as available.

**The two halves are not symmetric, and must not be made so.**
``validate_write_to_path`` interrogates the filesystem the path names, so
it is meaningful only where this process can see that filesystem; there
its own ``Path`` semantics are the correct ones, and a spelling absolute
only on some other platform is rightly refused. ``caller_path_is_absolute``
answers for a machine this process cannot see, so it accepts a path
absolute under either flavour. A genuinely relative spelling is relative
under both, which is what the check exists to refuse, so widening the
accept set does not widen it to the case it guards.

**Why these are called at the arm and not inside the recipe minters.** A
minter runs after its caller has already read what the recipe has to
promise -- a document record, a size, a digest. Validating there would make
a malformed path report whatever the read reported, so the same argument
would get a different answer depending on whether the document happened to
resolve. The ordering is the point, and a minter cannot enforce an
ordering constraint about its own caller.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from sage.api.errors import WritePathExistsError, WritePathInvalidError


def caller_path_is_absolute(path: str) -> bool:
    """Whether ``path`` is absolute on the machine that will use it.

    For use where that machine is not this one: a Windows caller's
    ``C:\\out.md`` and ``\\\\host\\share\\out.md`` are absolute where they
    will be read or written, while ``PosixPath`` reads both as relative.
    Accept a path absolute under either flavour.

    Where *this* process is the one that will use the path, this is the
    wrong predicate -- use ``Path.is_absolute`` directly, or
    ``validate_write_to_path``, which does.
    """
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def caller_basename(path: str | None, fallback: str) -> str:
    """Reduce a caller-named path to a safe basename for staging.

    The basename half of the same asymmetry: which characters separate
    components is the reading platform's business, and this path belongs to
    a machine that need not be this one. ``PurePosixPath`` splits only on
    ``/``, so a Windows caller's ``C:\\docs\\note.md`` has no separator it
    recognizes and reduces to *itself* -- and a staged file, and from it a
    retained vault path, would carry the caller's whole path as its name.

    The Windows reading is applied only to a path that is absolute under
    Windows and *not* under POSIX, which is the narrowest test that
    identifies a Windows spelling. Reducing under both flavours
    unconditionally would be wrong the other way: a backslash is a legal
    character in a POSIX filename, so ``/home/x/we\\ird.md`` would lose its
    leading half.

    Degenerate results (``""``, ``"."``, ``".."``) fall back to the
    synthetic name rather than resolving to the staging directory itself
    and failing with an unstructured OS error.
    """
    if not path:
        return fallback
    windows, posix = PureWindowsPath(path), PurePosixPath(path)
    name = (windows if windows.is_absolute() and not posix.is_absolute() else posix).name
    return fallback if name in ("", ".", "..") else name


def validate_caller_write_path_shape(write_to_path: str) -> None:
    """Verify write_to_path is absolute on the machine that will write it.

    The shape check alone, for the arm where the caller's environment does
    the writing. The filesystem checks in ``validate_write_to_path``
    cannot be widened the same way and are deliberately not applied here:
    they interrogate the filesystem the path names, and only the local
    arm's process can see it.
    """
    if not caller_path_is_absolute(write_to_path):
        raise WritePathInvalidError(write_to_path, "path must be absolute")


def validate_write_to_path(write_to_path: str) -> None:
    """Verify write_to_path is an absolute path with a writable parent directory.

    Absolute-path check, target-must-not-exist check, parent-directory
    must-exist / must-be-dir / must-be-writable check. Raises typed errors
    so MCP and REST callers see a consistent envelope across every
    write-to-disk surface.

    This is the arm where *this* process is the writer, so the platform's
    own ``Path`` semantics are the right ones and a spelling absolute only
    on some other platform is correctly refused -- unlike
    ``validate_caller_write_path_shape``, which answers for a machine it
    cannot see.

    The target-must-not-exist check is a fast refusal, not the enforcement:
    a caller naming an occupied path pays no read. What actually enforces
    the contract is the exclusive-create mode of the write itself, since
    the work that produces the bytes sits between this check and that
    write.
    """
    target = Path(write_to_path)
    if not target.is_absolute():
        raise WritePathInvalidError(write_to_path, "path must be absolute")
    if target.exists():
        raise WritePathExistsError(write_to_path)
    parent = target.parent
    if not parent.exists():
        raise WritePathInvalidError(write_to_path, f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise WritePathInvalidError(write_to_path, f"parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise WritePathInvalidError(write_to_path, f"parent directory is not writable: {parent}")
