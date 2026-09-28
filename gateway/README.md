# MeshPilot channel gateway

A tiny, always-on bridge that lets you **talk to the MeshPilot agent from chat channels**.
It is *not* an agent — it's dumb plumbing. A message comes in on a channel, the gateway
forwards it to the MeshPilot agent's HTTP API, waits for the run to finish, and posts the
reply back on the channel. MeshPilot (on FastAPI Cloud) is always the brain.

This is our own lean version of OpenClaw's channel layer — one small file per concern, no
plugin framework. **Discord only for now**; Telegram / WhatsApp are future adapters.

## How it works

```
#agent-chat message
   → POST  {MESHPILOT_URL}/internal/agent/run?brand={BRAND}  {goal, brand}   (header: x-jobs-token)
   → poll  GET  /internal/agent/run/{run_id}?brand={BRAND}   until status = done|error
   → reply with `final` on the channel
```

The `brand` goes on the **query string** of both calls: `x-jobs-token` is validated against
`?brand=`, and the agent resolves each run's target brand from `?brand=` (not the body), so a
non-default `MESHPILOT_BRAND` would 401/400 without it. It stays in the run POST body too, where it
must match the query value.

Discord free-form chat needs a persistent gateway (websocket) connection, which is why this
runs as one always-on container (one replica) rather than on the stateless agent host.

**It also holds the API awake (SCHED-1).** Because this container is already on 24/7, it runs a
`/healthz` heartbeat every `KEEPALIVE_INTERVAL_S` (default **600s**). FastAPI Cloud scales the
API to zero on idle and the in-app cron scheduler dies with the process — measured 2026-09-21,
`startup complete` landed in the SAME SECOND as the request ending each gap (72 min, 109 min),
so the process was BOOTING, not resuming. ⚠️ Idle counts **inbound HTTP only**: the API was
sweeping every 15 min right up to the cut and was reclaimed anyway. `/healthz` is deliberately
unauthenticated, so this needs no credential. Set `KEEPALIVE_ENABLED=0` to turn it off.
See docs/plans/2026-09-21-scheduler-stalls.md.

## Run

```bash
pip install -r requirements.txt
export DISCORD_BOT_TOKEN=...            # the bot token
export DISCORD_AGENT_CHANNEL_ID=...     # the #agent-chat channel id
export MESHPILOT_URL=https://api.meshpilot.app
export MESHPILOT_JOBS_TOKEN=...         # the agent's jobs-auth token (x-jobs-token)
export MESHPILOT_BRAND=acme_supply
python -u bridge.py
```

Enable **Message Content Intent** for the bot in the Discord developer portal, or it can't read
messages. Access control is the channel itself — `#agent-chat` is private (team + bot only), so
anyone who can post there is authorized.

## Deploy

Run it as **one always-on container** anywhere that can hold an outbound websocket — a small VM
next to the API, a container service, or `python bridge.py` under a process supervisor. Build from
this directory's `Dockerfile`, set the env vars above, and point `MESHPILOT_URL` at your API. It
opens no inbound port, so it needs no domain, TLS or healthcheck.

Tag the image by this folder's content (e.g. `git rev-parse HEAD:gateway`) so an API-only deploy
does not restart it: every restart drops the Discord websocket and replays the reconnect.

⚠️ A Discord bot token permits only **one** gateway session at a time — run exactly ONE copy, or
Discord delivers every reply twice. Stop the old instance before starting a new host.

## Per-project chat routing

**The channel IS the brand declaration.** Each chat channel maps to exactly one brand:

| channel | brand | token used |
|---|---|---|
| `#lab-chat` `100000000000000001` | `exampleco_lab` | `EXAMPLECO_JOBS_AUTH_TOKEN` |
| `#acme-chat` `100000000000000002` | `acme_supply` | `ACME_JOBS_AUTH_TOKEN` |

Configured by `DISCORD_CHANNEL_BRANDS`, a JSON map `{"<channel_id>": {"brand": …, "prefix": …}}`.

### CLIPNET `#clip-queue` channels

A second, separate mapping, `CLIPQUEUE_CHANNEL_BRANDS`, same shape plus an optional default
`campaign`: `{"<channel_id>": {"brand": "ai_empire", "prefix": "AIE", "campaign": "lovable"}}`.
YouTube links pasted in one of those channels are queued for THAT brand via
`POST /internal/clipnet/jobs` with `<PREFIX>_JOBS_AUTH_TOKEN` — the channel is the brand
declaration, exactly as for chat. Add `campaign: <slug>` to a message to override the default.
Results (post links, the Whop deadline, failed/blocked jobs) are posted back by the API, which reads
the same channel id from `<PREFIX>_CLIPNET_DISCORD_CHANNEL_ID`. A route whose token is unset is
dropped and logged at startup, never guessed. Channels live 2026-09-25: `#clip-queue-ai-empire`,
`#clip-queue-entertainment-vault`, `#clip-queue-hypedrop-gaming` (MeshPilot category).

Why per-channel rather than one global chat: there is **no brand inference from message text**, so
a message can never silently run as the wrong brand, and a referent like "I don't like that
content" resolves against the right project's episodes without asking. Authorisation stays scoped
per #95 — each call uses that brand's own token, verified live: one brand's token on
another's `?brand=` returns 401.

⚠️ Tokens are read under the **same names the API uses** (`EXAMPLECO_JOBS_AUTH_TOKEN`,
`ACME_JOBS_AUTH_TOKEN`) — deliberately NOT a gateway-specific copy. A duplicate of GE's token under
`MESHPILOT_JOBS_TOKEN` went stale after a rotation and 401'd every message until 2026-09-19. One
secret, one name.

A route whose token is missing is **dropped at startup with a logged reason**, and every route's
token is **probed against the API on `on_ready`** — so a stale token is loud immediately instead of
surfacing the next time someone talks to the agent.

**Restart behavior — there is NO zero-downtime handoff.** This is a single-replica Discord **gateway client**: it holds an *outbound* websocket and has no inbound
HTTP port, so there is no healthcheck and **no readiness gate** on cutover. On
each gateway deploy the old container stops and the new one starts; the bridge only becomes
usable once its Discord `on_ready` fires (a few seconds after start). A Discord bot token permits only
**one** gateway session at a time, so the old and new bridges *cannot* overlap — so expect a **brief
reconnect gap** on every gateway deploy, during which a message posted to `#agent-chat` may be missed
(resend it). This is why the image is tagged by this folder's content: the gateway redeploys only on real `gateway/` changes,
keeping these gaps rare. It's a dumb relay, so a momentary gap is acceptable — and a
healthcheck would **not** fix it, since a second session on the same token just gets disconnected; the
only real remedy (Discord session-resume / sharding) is overkill here.

