"""macOS unified-memory query helper for the OOM guardrail.

``free_unified_memory_bytes`` parses ``vm_stat`` (the page size is
read from the header line so the helper is correct on both Intel
4 KiB-page and Apple Silicon 16 KiB-page systems) and returns
reclaimable bytes (free + inactive + speculative pages). ``inactive``
and ``speculative`` are reclaimable on demand and counting them
matches the operator-intuition ``Activity Monitor`` reports as
"available".

``total_unified_memory_bytes`` reads installed capacity from
``sysctl hw.memsize``. It is the denominator a resident-footprint
figure is read against, and unlike the free reading it does not move
with load.

``min_free_bytes`` resolves the threshold at call time so the
``SAGE_MIN_FREE_UNIFIED_MEMORY_GIB`` env var can be tuned without a
process restart in tests.
"""

import os
import re
import subprocess


class UnifiedMemoryExhaustedError(Exception):
    """Raised by the OOM preflight when free memory is below the threshold.

    Carries the SAGEError attribute shape (``code``, ``message``,
    ``status_code``, ``detail``) so the API and MCP exception handlers
    translate it to the same structured response every other SAGE error
    uses. Lives in ``sage.utils`` rather than ``sage.api.errors`` because
    the import-linter contract forbids ``sage.adapters`` from importing
    ``sage.api``.
    """

    def __init__(
        self,
        free_bytes: int,
        min_free_bytes: int,
        model_id: str,
    ) -> None:
        self.code = "unified_memory_exhausted"
        self.message = (
            f"Free unified memory ({free_bytes} bytes) is below the safe "
            f"threshold ({min_free_bytes} bytes) for model '{model_id}'"
        )
        self.status_code = 503
        self.detail = {
            "free_bytes": free_bytes,
            "min_free_bytes": min_free_bytes,
            "model_id": model_id,
        }
        super().__init__(self.message)


DEFAULT_MIN_FREE_GIB = 4

_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_PAGES_RE = re.compile(r"^Pages (free|inactive|speculative):\s+(\d+)\.")


def free_unified_memory_bytes() -> int:
    """Return free + inactive + speculative pages from ``vm_stat`` in bytes.

    Raises:
        RuntimeError: if vm_stat output cannot be parsed (e.g. running
            on a non-macOS system).
    """
    output = subprocess.check_output(["/usr/bin/vm_stat"], text=True)

    page_match = _PAGE_SIZE_RE.search(output)
    if not page_match:
        raise RuntimeError("Could not parse page size from vm_stat output")
    page_size = int(page_match.group(1))

    total_pages = 0
    for line in output.splitlines():
        m = _PAGES_RE.match(line)
        if m:
            total_pages += int(m.group(2))

    if total_pages == 0:
        raise RuntimeError("vm_stat reported zero reclaimable pages")

    return total_pages * page_size


def total_unified_memory_bytes() -> int:
    """Return the machine's installed physical memory in bytes.

    Capacity, not availability: the figure is fixed for the machine, where
    ``free_unified_memory_bytes`` moves with load. A resident footprint is
    only interpretable against this denominator, so the two are reported
    together rather than one standing in for the other.

    Raises:
        RuntimeError: if the sysctl reading cannot be parsed (e.g. running
            on a non-macOS system).
    """
    output = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True)

    try:
        return int(output.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Could not parse hw.memsize from sysctl output: {output.strip()!r}"
        ) from exc


def min_free_bytes() -> int:
    """Resolve the OOM-guardrail threshold in bytes.

    Reads ``SAGE_MIN_FREE_UNIFIED_MEMORY_GIB`` on each call so operators
    can tune the threshold without restarting SAGE. Falls back to
    ``DEFAULT_MIN_FREE_GIB`` GiB when unset.
    """
    raw = os.environ.get("SAGE_MIN_FREE_UNIFIED_MEMORY_GIB")
    gib = int(raw) if raw is not None else DEFAULT_MIN_FREE_GIB
    return gib * 1024**3
