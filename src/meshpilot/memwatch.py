"""Process memory: measure it, and hand freed heap back to the OS (OPS-MEM-1, 2026-09-28).

Measured on Cloud Run (1 GiB) over three days: the API idled at ~64 % and stepped UP after busy
cron runs, never back down, until a restart — then OOM-killed at 00:07Z mid-publish. That shape
is glibc holding freed memory in per-thread arenas (high-water mark), not necessarily a leak. So:
  - the image caps arenas (MALLOC_ARENA_MAX=2, Dockerfile),
  - every sweep that ran jobs calls `trim()` and logs RSS before/after, so a real leak — RSS that
    stays up AFTER a trim — is visible in the logs instead of inferred from a crash.
Never raises: on a non-glibc platform (macOS dev) trim is a no-op and RSS may be None.
"""
from __future__ import annotations

import ctypes
import ctypes.util

_LIBC = None


def rss_mb() -> float | None:
    """Resident set size of THIS process in MB (Linux /proc; None elsewhere)."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def trim() -> bool:
    """glibc malloc_trim(0): release free heap pages to the OS. True if it ran."""
    global _LIBC
    try:
        if _LIBC is None:
            name = ctypes.util.find_library("c")
            _LIBC = ctypes.CDLL(name) if name else False
        if not _LIBC or not hasattr(_LIBC, "malloc_trim"):
            return False
        _LIBC.malloc_trim(0)
        return True
    except (OSError, AttributeError):
        _LIBC = False
        return False


def trim_and_measure() -> dict:
    before = rss_mb()
    ran = trim()
    return {"rss_before_mb": before, "rss_after_mb": rss_mb(), "trimmed": ran}
