"""CLIPNET-UNATTENDED: the channels run a week untended — bounded storage, no retry storm on a 0 balance."""
from __future__ import annotations

from meshpilot.agent.clipnet import dispatch as d
from meshpilot.agent.clipnet import purge as pg

SUPA = "https://abc.supabase.co"


def test_only_our_own_clip_files_are_ever_deleted():
    ok = f"{SUPA}/storage/v1/object/public/aie-media/clipnet/organic-ai-empire/xyz/1.mp4"
    assert pg.object_path(ok, SUPA, "aie-media") == "clipnet/organic-ai-empire/xyz/1.mp4"
    assert pg.object_path(ok, SUPA, "hd-media") is None                       # another brand's bucket
    assert pg.object_path(f"{SUPA}/storage/v1/object/public/aie-media/brand/logo.png", SUPA, "aie-media") is None
    assert pg.object_path("https://evil.example/storage/v1/object/public/aie-media/clipnet/a.mp4", SUPA,
                          "aie-media") is None
    assert pg.object_path(f"{SUPA}/storage/v1/object/public/aie-media/clipnet/../x", SUPA, "aie-media") is None


class _Result:
    def __init__(self, rows=(), scalar=None):
        self._rows, self._scalar = list(rows), scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self


class _Conn:
    def __init__(self, eng):
        self.eng = eng

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.eng.sql.append((sql, params))
        return self.eng.answer(sql, params)


class _Begin:
    def __init__(self, eng):
        self.eng = eng

    async def __aenter__(self):
        return _Conn(self.eng)

    async def __aexit__(self, *a):
        return False


class _Eng:
    def __init__(self, answer):
        self.answer, self.sql = answer, []

    def begin(self):
        return _Begin(self)


async def test_purge_deletes_the_files_then_clears_their_urls(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPA)
    url = f"{SUPA}/storage/v1/object/public/aie-media/clipnet/c/1.mp4"
    eng = _Eng(lambda sql, p: _Result([("11111111-1111-1111-1111-111111111111", url),
                                       ("22222222-2222-2222-2222-222222222222", "https://elsewhere/x.mp4")])
               if sql.startswith("SELECT") else _Result())
    deleted = []

    async def delete(bucket, paths):
        deleted.append((bucket, paths))
    out = await pg.purge("ai_empire", engine=eng, delete=delete, platforms=("x", "youtube"),
                         bucket="aie-media")
    assert deleted == [("aie-media", ["clipnet/c/1.mp4"])]
    assert out == {"deleted": 1, "skipped_foreign_url": 1}
    sel = eng.sql[0][1]
    assert sel["plats"] == ["x", "youtube"] and sel["keep"] == pg.KEEP_AFTER_POST_H
    assert eng.sql[-1][1] == {"ids": ["11111111-1111-1111-1111-111111111111"]}


async def test_purge_with_nothing_due_deletes_nothing(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPA)
    eng = _Eng(lambda sql, p: _Result())

    async def delete(*a):
        raise AssertionError("nothing is due")
    assert (await pg.purge("ai_empire", engine=eng, delete=delete, platforms=("x",),
                           bucket="aie-media"))["deleted"] == 0


def _dispatch_engine(prev_error=""):
    def answer(sql, p):
        if "SELECT count(*)" in sql:
            return _Result(scalar=0)
        if sql.startswith("UPDATE clipnet_job SET heartbeat_at=now()"):
            return _Result(scalar="job-1")
        if sql.startswith("SELECT error"):
            return _Result(scalar=prev_error)
        return _Result()
    return _Eng(answer)


async def _low():
    return 0.33


async def test_a_low_balance_pauses_the_job_instead_of_starting_it_and_says_so_once():
    for prev, expect_notes in (("", 1), (f"{d.PAUSED} $0.40 < $1.00", 0)):
        eng, notes = _dispatch_engine(prev), []

        async def notify(b, msg, notes=notes):
            notes.append(msg)
        out = await d.dispatch("ai_empire", engine=eng, bucket="aie-media", credit_fn=_low, notify_fn=notify,
                               start=lambda *a: (_ for _ in ()).throw(AssertionError("must not start")))
        assert out["started"] is None and out["paused"] == "0.33"
        unmark = [p for s, p in eng.sql if s.startswith("UPDATE clipnet_job SET heartbeat_at=NULL")]
        assert unmark and unmark[0]["e"].startswith(d.PAUSED)     # the attempt is handed back, not spent
        assert len(notes) == expect_notes


async def test_an_unreadable_balance_never_halts_dispatch(monkeypatch):
    import meshpilot.agent.clipnet.learn as learn

    async def no_guidance(*a, **k):
        return ""
    monkeypatch.setattr(learn, "guidance", no_guidance)

    async def unknown():
        return None
    started = []

    async def notify(*a):
        pass
    out = await d.dispatch("ai_empire", engine=_dispatch_engine(), bucket="aie-media", credit_fn=unknown,
                           notify_fn=notify, start=lambda j, b: started.append(j))
    assert started == ["job-1"] and out["paused"] is None


async def test_the_pause_threshold_can_be_lowered_for_a_byok_day(monkeypatch):
    monkeypatch.setenv("CLIPNET_MIN_CREDIT_USD", "0.15")
    assert d.min_credit_usd() == 0.15
    monkeypatch.setenv("CLIPNET_MIN_CREDIT_USD", "nonsense")
    assert d.min_credit_usd() == d.MIN_CREDIT_USD
