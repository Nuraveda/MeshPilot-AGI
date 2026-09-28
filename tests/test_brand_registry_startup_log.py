"""The brand registry announces itself at startup — ids only, and never on /healthz.

Why this exists
---------------
`_default_brand_config()` is a built-in fallback that serves a brand nobody configured.
That is supportable on a laptop and wrong in production, and until this log line there
was NO way to tell from outside which had happened: a healthy instance looks identical
either way. Deciding whether the fallback could be retired meant guessing.

⚠️ This is deliberately a LOG and not a /healthz field. `/healthz` is unauthenticated
on purpose (tests/test_healthz_scheduler.py pins its exact key set), so brand ids there
would publish a multi-tenant instance's customer list to anyone who asks.

⚠️ These tests read STDOUT and never call logging.basicConfig, on purpose. The first
version of this line used stdlib `logging.info` and was verified with a caplog test
that passed — while emitting NOTHING in production, because nothing in src/meshpilot/
configures logging and uvicorn leaves the root logger at WARNING. The test created the
very condition that made the code look correct. Capturing stdout under the default
configuration is the only assertion that distinguishes "logged" from "logged where
somebody will actually read it".
"""
from __future__ import annotations

import json

from meshpilot import config


def _load(capsys, tmp_path, monkeypatch, files: dict[str, dict], env_json: str = "",
          default: str | None = None):
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    for bid, body in files.items():
        (cfgs / f"{bid}.json").write_text(json.dumps({"brand_id": bid, **body}))
    s = config.settings()
    # No built-in fallback any more: the default must be one of the loaded brands,
    # or the loader refuses. Tests say which one rather than relying on a stand-in.
    monkeypatch.setattr(s, "default_brand_id", default or sorted(files)[0])
    monkeypatch.setattr(s, "brand_configs_dir", str(cfgs))
    monkeypatch.setattr(s, "brand_configs_json", env_json)
    monkeypatch.setattr(s, "brand_config_path", str(tmp_path / "nope.json"))
    config._brand_registry = None
    # No basicConfig, no caplog: exactly the production configuration.
    registry = config._load_brand_registry()
    out = capsys.readouterr().out
    line = next((ln for ln in out.splitlines() if "startup.brand_registry" in ln), "")
    assert line, "the startup line emitted NOTHING under the default logging setup"
    return registry, line


def test_it_reports_the_ids_and_where_each_came_from(capsys, tmp_path, monkeypatch):
    """Provenance is the point: a brand from a FILE exists on this disk, a brand from
    the ENV is what actually reaches production. Conflating them is how you conclude
    production has a config it does not have."""
    _, line = _load(
        capsys, tmp_path, monkeypatch,
        files={"northwind": {"env_prefix": "NW"}, "example": {"env_prefix": "ACME"}},
        env_json=json.dumps({"from_cloud": {"env_prefix": "CLOUD"}}),
        default="example",
    )
    assert "example" in line and "northwind" in line
    assert "from_files" in line and "from_env" in line
    assert "from_cloud" in line


def test_a_clone_holding_only_the_example_resolves_its_default(capsys, tmp_path, monkeypatch):
    """Replaces a test for the built-in fallback, which is GONE.

    The fallback existed so a fresh clone could start. That job now belongs to
    `default_brand_id = "example"` plus the tracked brand/configs/example.json — the
    clone resolves a real config instead of a stand-in compiled into config.py that
    happened to be a live customer's.
    """
    monkeypatch.setattr(config.settings(), "default_brand_id", "example")
    registry, line = _load(capsys, tmp_path, monkeypatch,
                           files={"example": {"env_prefix": "ACME"}}, default="example")
    assert config.brand_config("example")["env_prefix"] == "ACME"
    assert "example" in line


def test_a_missing_default_refuses_rather_than_substituting(capsys, tmp_path, monkeypatch):
    """The behaviour the fallback used to hide. Starting anyway would run one brand's
    work under whatever config happened to be loaded."""
    import pytest

    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "acme.json").write_text(json.dumps({"brand_id": "acme", "env_prefix": "ACME"}))
    s = config.settings()
    monkeypatch.setattr(s, "default_brand_id", "not_configured")
    monkeypatch.setattr(s, "brand_configs_dir", str(cfgs))
    monkeypatch.setattr(s, "brand_configs_json", "")
    monkeypatch.setattr(s, "brand_config_path", str(tmp_path / "nope.json"))
    config._brand_registry = None
    with pytest.raises(RuntimeError, match="has no matching config"):
        config._load_brand_registry()


def test_it_logs_no_values_from_any_brand_config(capsys, tmp_path, monkeypatch):
    """A brand config carries a customer's site, handles, offer and approver ids.
    The ids are operational; the values are the customer's. Only ids go in the log."""
    secretish = {
        "env_prefix": "ACME",
        "site_url": "https://really-private.example",
        "offpage": {"brand_terms": ["Totally Secret Co"],
                    "approvers": ["1240025800904933407"],
                    "reply": {"reddit_username": "privateHandle"}},
    }
    _, line = _load(capsys, tmp_path, monkeypatch,
                    files={"northwind": {"env_prefix": "NW"}, "acme": secretish}, default="acme")
    assert "acme" in line                       # the id is expected
    for leak in ("really-private.example", "Totally Secret Co",
                 "1240025800904933407", "privateHandle", "ACME"):
        assert leak not in line, f"brand config value {leak!r} reached the startup log"


async def test_healthz_still_carries_no_brand_information(monkeypatch):
    """The guard that rejected the first design for this lane. /healthz is
    unauthenticated, so the registry summary must NOT appear there."""
    from meshpilot import server

    async def _lag():
        return {"last_run_age_s": 1, "worst_overdue_s": 0}

    monkeypatch.setattr(server, "_scheduler_lag", _lag)
    out = await server.healthz()
    assert set(out) == {"status", "service", "version", "build", "dispatch_mode", "scheduler"}  # build: OPS-BOX-1, a commit sha
    assert "brand" not in json.dumps(out).lower()


async def test_startup_loads_the_registry_so_the_line_is_really_a_startup_line(monkeypatch, capsys):
    """A log named `startup.brand_registry` must be emitted BY startup.

    It was lazy for three deploys: the app booted, reported healthy, and said nothing
    about its brands until something happened to ask for one. Loading in `_on_startup`
    also makes a broken registry stop the revision instead of failing inside a job.
    """
    from meshpilot import config, server

    config._brand_registry = None
    monkeypatch.setattr(server, "_oauth_keepalive", lambda: _noop())
    called: list[bool] = []

    real = config._brands

    def _spy():
        called.append(True)
        return real()

    monkeypatch.setattr(config, "_brands", _spy)
    try:
        await server._on_startup()
    except Exception:
        pass  # other startup work (scheduler, db) is not under test here
    assert called, "_on_startup did not load the brand registry"


async def _noop():
    return None
