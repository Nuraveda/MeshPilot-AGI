"""OPS-MEM-1: memory measurement + malloc_trim must never break the scheduler loop."""
from __future__ import annotations

import sys

from meshpilot import memwatch


def test_rss_is_a_positive_number_on_linux_and_none_elsewhere():
    r = memwatch.rss_mb()
    if sys.platform.startswith("linux"):
        assert r is not None and r > 0
    else:
        assert r is None or r > 0


def test_trim_never_raises_and_reports_whether_it_ran():
    assert memwatch.trim() in (True, False)
    if sys.platform.startswith("linux"):
        assert memwatch.trim() is True          # glibc: malloc_trim exists


def test_trim_and_measure_has_both_sides(monkeypatch):
    seq = iter([500.0, 420.0])
    monkeypatch.setattr(memwatch, "rss_mb", lambda: next(seq))
    monkeypatch.setattr(memwatch, "trim", lambda: True)
    assert memwatch.trim_and_measure() == {"rss_before_mb": 500.0, "rss_after_mb": 420.0, "trimmed": True}


def test_a_missing_libc_degrades_to_a_noop(monkeypatch):
    monkeypatch.setattr(memwatch, "_LIBC", False)
    assert memwatch.trim() is False
