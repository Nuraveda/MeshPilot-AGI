"""CLIPNET reservation reconcile: a crashed publish must be settled from the platform's truth, never
retried blindly. Measured 2026-09-28: both stuck YouTube rows had in fact been SENT by Buffer."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meshpilot.agent.clipnet import reconcile as rc

T = datetime(2026, 9, 28, 0, 7, 30, tzinfo=UTC)


def _post(i, dt_s=1, status="sent"):
    return {"id": i, "status": status, "createdAt": (T + timedelta(seconds=dt_s)).isoformat(),
            "externalLink": f"https://www.youtube.com/shorts/{i}"}


def test_the_2026_09_28_case_one_sent_post_settles_as_posted():
    verdict, post = rc.decide(T, [_post("b1"), _post("old", dt_s=-5 * 3600)], set())
    assert verdict == "posted" and post["id"] == "b1"


def test_nothing_on_buffer_means_it_never_left_and_may_be_retried():
    verdict, _ = rc.decide(T, [_post("old", dt_s=-5 * 3600)], set())
    assert verdict == "failed"


def test_a_post_still_in_flight_waits():
    assert rc.decide(T, [_post("b1", status="sending")], set())[0] == "wait"


def test_two_posts_in_the_window_are_ambiguous_and_wait_for_a_human():
    assert rc.decide(T, [_post("b1"), _post("b2", dt_s=60)], set())[0] == "wait"


def test_a_post_already_recorded_for_another_clip_is_not_claimed_again():
    assert rc.decide(T, [_post("b1")], {"b1"})[0] == "failed"


def test_a_buffer_side_failure_is_not_turned_into_a_retry():
    """An unattributable Buffer error could be a partial publish; leave it for a human."""
    assert rc.decide(T, [_post("b1", status="error")], set())[0] == "wait"


# ── IO wiring with a fake engine ───────────────────────────────────────────────────────────────
class _Res:
    def __init__(self, rows=(), rowcount=1):
        self._rows, self.rowcount = list(rows), rowcount

    def mappings(self):
        return self._rows

    def all(self):
        return [(r,) for r in self._rows]


class _Conn:
    def __init__(self, eng):
        self.eng = eng

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.eng.calls.append((sql.split()[0], params))
        if "status = 'reserved' AND p.updated_at <" in sql:
            return _Res(self.eng.stale)
        if "SELECT p.external_id" in sql:
            return _Res(self.eng.known)
        return _Res(rowcount=self.eng.rowcount)


class _Engine:
    def __init__(self, stale, known=(), rowcount=1):
        self.stale, self.known, self.rowcount, self.calls = stale, known, rowcount, []

    def connect(self):
        return _Conn(self)

    def begin(self):
        return _Conn(self)


def _row(platform, error=None):
    return {"clip_id": "c1", "platform": platform, "updated_at": T, "error": error, "hook": "Meet Jev"}


async def test_reconcile_settles_a_buffer_row_as_posted_with_its_link():
    eng = _Engine([_row("youtube")])

    async def lookup(brand, service):
        assert service == "youtube"
        return [_post("b1")]
    out = await rc.reconcile("ai_empire", engine=eng, lookup=lookup, notify_fn=None)
    assert out["posted"] == ["youtube:c1"]
    upd = [p for verb, p in eng.calls if verb == "UPDATE"]
    assert upd and upd[0]["x"] == "b1" and upd[0]["u"].endswith("/b1")


async def test_reconcile_alerts_once_for_a_meta_row_and_never_retries_it():
    sent = []

    async def notify(brand, msg):
        sent.append(msg)
    eng = _Engine([_row("instagram")])
    out = await rc.reconcile("ai_empire", engine=eng, lookup=None, notify_fn=notify)
    assert out["manual"] == ["instagram:c1"] and len(sent) == 1
    eng2 = _Engine([_row("instagram", error=rc.MANUAL_NOTE)])       # next tick: already marked
    await rc.reconcile("ai_empire", engine=eng2, lookup=None, notify_fn=notify)
    assert len(sent) == 1
    assert not any("status='failed'" in str(p) for _, p in eng.calls)


async def test_a_lookup_failure_leaves_the_row_alone():
    async def boom(brand, service):
        raise RuntimeError("buffer down")
    eng = _Engine([_row("youtube")])
    out = await rc.reconcile("ai_empire", engine=eng, lookup=boom, notify_fn=None)
    assert out["waiting"] == ["youtube:c1"]
    assert not [v for v, _ in eng.calls if v == "UPDATE"]
