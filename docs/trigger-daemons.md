# Trigger daemons (`kind: trigger`) — runtime reference

A **trigger daemon** is a plugin-declared, workspace-owned sidecar whose job
is to fire the agent's own autonomous self-turns. A scheduler is the first
(today the only) trigger type: it reads a schedule grid and fires a
`self-scheduler` turn when a cron entry is due. This document is the runtime's
`kind: trigger` daemon reference promised by the scheduler-as-trigger-plugin
RFC (molecule-core `docs/design/rfc-scheduler-as-trigger-plugin.md`, P1 docs
row).

For the shared local A2A transport a trigger daemon uses (sockets,
capabilities, provenance stamping, the allow-list), see the
[Plugin local A2A transport section of the README](../README.md#plugin-local-a2a-transport-channel--trigger-lanes).
This document covers everything else: supervision + hot-start, the durable
state directory, the `/internal/schedules` API, poke semantics, and the
health-file contract.

## Declaring a trigger daemon

Same `contributes.daemons` descriptor as a channel plugin
(`molecule_runtime/plugin_daemons.py`); only the manifest `kind` differs:

```yaml
kind: trigger
contributes:
  daemons:
    - name: scheduler
      command: python
      args: ["-m", "scheduler"]
```

Discovery reuses the canonical installed-plugin scan
(`molecule_runtime.plugins.load_plugins` — per-workspace
`<configs>/plugins` first, shared `/plugins` fallback, dedup, SSOT
enforcement), so a trigger plugin lands exactly like any other installed
plugin. For lane daemons (`channel` / `trigger`) the daemon's owning identity
is the **validated manifest `name`**, not the install directory name.
Malformed daemon entries are skipped with a loud log line; they never crash
boot.

Plugin authors do not hand-write the daemon: the SDK ships a `kind: trigger`
scaffold (`molecule-ai-sdk` `templates/trigger/`) whose `scheduler.py`
implements the grid/tick/poke/health loop this document describes, and the
fleet-default `molecule-scheduler` plugin is an instance of that scaffold.

## Supervision and lifecycle

Supervision is deliberately minimal — spawn + restart + env injection
(`molecule_runtime.plugin_daemons.DaemonSupervisor`):

- one monitor **thread** per daemon (daemon liveness must not depend on
  event-loop health), each child in its own session/process group;
- exponential restart backoff, 1s → 2s → 4s … capped at 60s;
- a circuit breaker after 10 consecutive fast failures (a run shorter than
  30s counts as fast) — the daemon is given up on until the next workspace
  boot;
- `stop()` SIGTERMs each live group, escalates to SIGKILL after a 10s grace,
  and joins the monitors — daemons die with the workspace.

Failures are logged, never fatal: the daemon is auxiliary to the agent, and
the workspace keeps serving without it. The child inherits the workspace
process env overlaid with the manifest `env`, but the reserved lane variables
are never inherited from the parent — the socket manager publishes
authoritative values only after its private listener is bound and chmodded.

### Boot (cold start) and hot-start

The lifecycle lives in `molecule_runtime.daemon_runtime.DaemonRuntime`, whose
single idempotent entry point `ensure_daemons()` converges the running
daemons toward the installed-plugin set. It runs on two paths:

1. **Boot** — a background task in `main.py` builds the supervisor and the
   per-plugin sockets once uvicorn is listening (a lane daemon posts inbound
   turns at the local A2A server, so it must never race the bind).
2. **Hot-start** — `POST /internal/daemons/reload` (same forward-auth as the
   other `/internal/*` routes: the per-workspace `platform_inbound_secret`).
   The platform calls this after installing a plugin post-boot — e.g.
   `molecule-scheduler` is installed into a workspace the moment it gets its
   first schedule — and the warm path binds lanes for and supervises **only**
   the newly-installed daemons, leaving running ones untouched. The response
   is a small summary: `{"armed": N, "added": [...], "trigger": bool}`.
   Reloads are serialized by a lock, so a reload racing boot (or another
   reload) never double-binds a socket or double-supervises a daemon.

Hot-**remove** is intentionally out of scope: uninstalling a plugin still
takes a workspace restart.

When any `kind: trigger` daemon is present, both paths set
`MOLECULE_RUNTIME_NATIVE_SCHEDULER=1` (and clear it when none is): the
workspace schedules natively, the heartbeat advertises the `scheduler`
capability, and the platform's central scheduler defers for this workspace
(`NativeSchedulerCheck`) so the agent is never double-triggered.

## Durable state: one directory, two writers

The schedule grid, poke queue, run history, and health heartbeat all live in
**one per-workspace directory on a durable volume**, resolved by
`molecule_runtime.trigger_state.resolve_trigger_state_dir()`:

- `MOLECULE_TRIGGER_STATE_DIR` wins when set — this is also the env var the
  runtime injects into the trigger daemon subprocess, so the daemon resolves
  exactly the directory the API resolved;
- otherwise it is `<plugin-state root>/.trigger-state`, on the durable
  plugin-state volume the provisioner declares via
  `MOLECULE_PLUGIN_STATE_ROOT` (`/home/agent/.molecule/plugin-state/.trigger-state`
  in a managed container). The `.trigger-state` name is RESERVED and is
  deliberately not a plugin name: this grid belongs to the WORKSPACE, not to
  whichever `kind: trigger` plugin happens to be installed, so keying it on a
  manifest name would orphan every schedule the day that plugin is swapped;
- otherwise — no durable root declared, or the durability probe refutes the
  declaration — it falls back to the legacy `<configs_dir>/schedules`
  (`/configs/schedules`), so a control plane predating the plugin-state
  contract behaves exactly as before.

**Why not `/configs` any more** (molecule-ai-workspace-runtime#370,
molecule-ai/molecule-core#5036): `workspaceTeardownVolumes` in the control
plane's local-docker provisioner removes the `/configs` and `/workspace` named
volumes on *every* teardown, including a plain restart — only `mol-ws-pstate-*`
and `mol-ws-rtstate-*` survive. A grid rooted on `/configs` was therefore
destroyed on every restart, taking user-created `source='runtime'` schedules,
the last-fire watermark (so survivors miss or double-fire), the poke queue and
the run history with it, silently. A `config_dir: existing-volume` restart
response does not contradict this — it only means core selected no template,
not that the provisioner preserved the volume.

On first resolve after upgrading, a grid still sitting at the legacy
`/configs/schedules` is **copied** onto the durable root (never moved, and never
over an existing destination file — `/configs` is re-seeded from the org
template on every provision, so a stale copy can reappear and must not revert
later edits).

Files in that directory (names from `molecule_runtime.trigger_state` and
`molecule_runtime.internal_schedules`):

| File | Writer | Reader | Content |
| --- | --- | --- | --- |
| `schedules.yaml` | schedule API (+ boot seeder) | trigger daemon | the schedule grid — definition only |
| `schedule-pokes.json` | schedule API (RunNow) | trigger daemon (sole clearer) | set of schedule names to fire now |
| `schedule-health.json` | trigger daemon | schedule API | tick heartbeat (see contract below) |
| `schedule-history.json` | trigger daemon | schedule API | recent run log |

The grid holds *definition only* — firing bookkeeping (last run, next run,
run count, …) is the daemon's, in separate state on the same volume, so a
grid edit never races the daemon's fire state.

### The grid and its contract

Every write goes through `molecule_runtime.schedule_store.ScheduleStore`,
which validates against the vendored SDK `schedule` contract
(`molecule_runtime/contracts/schedule.schema.json`) plus byte-level caps —
at most 100 entries, cron expressions ≤ 128 chars, prompts ≤ 16 KiB — and
checks each cron against the `cron` contract
(`molecule_runtime.cronspec.validate`), so an unschedulable expression is
rejected at write time, never at fire time. Persisted entry fields:
`name`, `cron`, `timezone`, `prompt`, `enabled`, `source`.

### Boot seeding

Schedules are volume-authoritative (RFC Option A). On boot — and again on a
hot reload that arms a trigger — `molecule_runtime.schedule_seed` reconciles
the grid from the `schedules.yaml` each installed trigger plugin ships
(delivered to `<config_path>/plugins/<plugin>/schedules.yaml` by the
declared-plugins boot-install). Seeding is additive and edit-preserving
(template-owned entries are refreshed; a user's runtime edits are never
clobbered) and fail-soft (a bad or missing template grid never blocks boot).
It runs before the daemon starts reading the grid.

## The `/internal/schedules` API

The write side of the grid (`molecule_runtime/internal_schedules.py`).
Canvas / admin reach these routes through the platform proxy, with the same
forward-auth as every other `/internal/*` route (the per-workspace
`platform_inbound_secret`); unauthenticated requests get `401`. When no state
directory can be resolved, the routes answer `503`.

| Route | Method | Behaviour |
| --- | --- | --- |
| `/internal/schedules` | GET | list the grid |
| `/internal/schedules` | POST | create an entry — `201`, or `400` on a contract violation |
| `/internal/schedules/{name}` | PATCH | update — `404` unknown name, `400` invalid patch |
| `/internal/schedules/{name}` | DELETE | delete — `404` unknown name |
| `/internal/schedules/{name}/run` | POST | poke ("run now") — see below |
| `/internal/schedules/health` | GET | the daemon's health file (contract below) |
| `/internal/schedules/history` | GET | the run log; `/{name}/history` filters by schedule |

### Poke ("run now") semantics

`POST /internal/schedules/{name}/run` returns `404` for an unknown schedule
and `409` for a disabled one. Otherwise it adds the name to the poke set in
`schedule-pokes.json` and answers `202 {"poked": name}` — **accepted, not
fired**: the daemon consumes the poke on its next tick. The daemon is the
only clearer of the poke file; a poke landing in the same ~poll-interval
window the daemon is clearing is a rare, user-retriable loss, acceptable for
a fire-now signal.

## Health-file contract

The daemon writes `schedule-health.json` each tick; the API returns its JSON
payload verbatim from `GET /internal/schedules/health`. Before the first tick
(no health file yet) the API synthesizes the minimum shape from the grid so
the surface is never blank:

```json
{"last_tick": null, "armed": <entries in the grid>, "errors": {}}
```

The health (and pokes/history) filenames are kept in sync with the SDK
trigger scaffold's `scheduler.py` by the schedule contract's convention, not
a shared import — the daemon is vendored per-plugin, not importable from the
runtime.

## Firing a turn: the trigger client and its boundaries

The daemon fires a turn with `send_trigger_message` from
`molecule_plugin.channel` (the SDK-owned client; the runtime vendors a
byte-for-byte copy in `molecule_runtime/channel_sdk.py`). The host side
enforces, per request (`molecule_runtime/channel_events.py`):

- the plugin-specific ephemeral capability token (constant-time compared) on
  the plugin's own private Unix socket;
- the `source_type` **allow-list** (`TRIGGER_ALLOWED_SOURCE_TYPES`, today
  `{"self-scheduler"}`) — the security boundary described in the README: the
  daemon controls only the prompt text, never the turn's identity;
- provenance stamping (`_stamp_trigger_source`): message-level
  `source`/`source_type` are stripped, and `params.metadata.source` is set to
  the plugin id for audit.

Delivery semantics: capability absence raises
`ChannelCapabilityUnavailable` (known-safe — this host cannot run the
plugin); once a request crosses the boundary, any connection/timeout/HTTP
failure raises `ChannelDeliveryUnknown` and the turn must **not** be
replayed — the daemon should treat an unknown outcome as "possibly fired" and
advance its schedule state rather than retry.

Because the stamped `source_type` is a routine self-ping class
(`molecule_runtime/a2a_executor.py`, `_ROUTINE_SELF_SOURCE_TYPES`), a fired
turn drops rather than queues behind an in-flight turn, and its output runs
through the autonomous-loop replay guard
(`molecule_runtime/autonomous_loop_guard.py`) — a runaway self-fire loop
trips the breaker instead of burning tokens.

## Turn liveness: `GET /turn-liveness` (trigger lane only)

`message/send` on this lane blocks until the agent's turn completes, and how
long that legitimately takes is a property of the **work**, not the transport.
The trigger client therefore sets **no read deadline** on a delivery
(`timeout_seconds=None`; only the connect phase stays bounded). A flat HTTP
read timeout there is a wall-clock turn-duration policy in disguise: it fires on
a genuinely-working agent, and because the request has already crossed the
boundary the outcome can only be reported as `ChannelDeliveryUnknown` —
indeterminate by construction. That is exactly what produced a run history full
of `status: unknown` entries clustered at the old 600s cap.

A daemon still has to tell a **working** agent from a **wedged** one. The
runtime already owns the one honest answer and must not grow a second: the
**turn lease** (`molecule_runtime/turn_lease.py`), touched on every tool call,
expiring only after an idle TTL with no touch (`A2A_COMPLETION_IDLE_TIMEOUT_SECONDS`,
900s), under an un-bypassable absolute cap measured from turn start
(`MOLECULE_MAX_TURN_SECONDS`, default 4x the idle cap = 3600s).

The trigger binding exposes that object, and nothing else, at
`GET /turn-liveness` (`channel_events.turn_liveness_snapshot`). It is gated by
the **same** ephemeral capability as a send — a daemon that cannot fire a turn
cannot inspect one — and it is a pure read: it never touches, arms or ends a
turn. The **channel** lane does not serve it; a channel bridges an external
party and gets no window into the agent's turn.

```json
{"lease": true, "idle_seconds": 3.1, "ttl_seconds": 900.0,
 "turn_age_seconds": 3000.0, "absolute_cap_seconds": 3600.0,
 "idle_expired": false, "absolute_cap_exceeded": false, "alive": true}
```

`{"lease": false, "reason": ...}` means the mailbox kernel is off and there is
**no signal** — a real state a caller must handle by falling back to its own
ceiling. It does not mean the turn is dead; reporting it that way would
reintroduce the wall-clock kill. A host predating this contract answers 404,
which `probe_trigger_liveness` also reports as "no signal" rather than an error.

Client: `probe_trigger_liveness` (SDK `molecule_plugin.channel`, vendored into
`molecule_runtime/channel_sdk.py`). Unlike a delivery it is always time-bounded
(5s) — an unbounded probe against a wedged host would be the very failure the
probe exists to detect. The reference scheduler consumes it in
`SchedulerDaemon.check_watchdog`: an `alive` turn is never cancelled however
long it runs; `idle_expired`, `absolute_cap_exceeded`, and "no signal past the
fallback ceiling" each cancel and re-queue the fire, and each records its own
`cause` in the run history.

### Known asymmetry: who enforces the absolute cap, and when

The executor consults `absolute_cap_exceeded()` **only** at an idle-cap
boundary — inside `turn_is_alive_despite_idle`, which runs when `astream_events`
has produced no event for the idle cap. A turn that keeps emitting events can
therefore exceed the absolute cap inside the runtime without being ended.

A daemon polling `/turn-liveness` evaluates the same predicate **continuously**,
so it can decide to stop waiting on a turn the executor is still running. That is
the intended asymmetry — the daemon is bounding *its own* wait, not the turn —
but it means a cancelled delivery does not imply a cancelled turn. The fire is
re-queued; a re-fire that lands while the original turn is still in flight is
dropped host-side, because a `self-scheduler` `source_type` is a routine
self-ping class that drops rather than queues behind an in-flight turn.

Tightening this properly means the executor checking the absolute cap on the
event path too, not just at the idle boundary. That is a separate change.

### Which runtimes the snapshot is actually about (runtime#408)

The snapshot is only as honest as the lease behind it, and a lease is only
about a turn once something **arms** it at that turn's start. For a long time
only the native executor did. Every adapter-supplied executor now participates
through one wrap applied at `main.py`'s executor funnel
(`turn_lease_executor.TurnLeaseExecutor`), which per turn:

1. materializes and **exports** `MOLECULE_TOOL_ACTIVITY_FILE` before the child
   is spawned — codex and hermes both gate their per-tool-call liveness ping on
   that variable, and it used to be exported only by the native executor, which
   never runs on those flavours. Their feed was written for the lease and was
   dead on arrival;
2. arms the lease, but **only if this runtime has been observed to feed it**
   (`turn_lease.arm_turn_if_fed`);
3. runs the activity-file watcher for the turn's duration.

Arming is deliberately conditional. Arming is what persuades a consumer to
believe the lease — `lease_is_attributable` passes as soon as
`turn_age_seconds < elapsed` — so a lease that is armed but never fed is not a
better signal than an unarmed one, it is a worse one: it reports the turn as
idle from the instant it starts, and a legitimately long turn gets cancelled at
the TTL. Where no feed exists the lease is therefore left unarmed and continues
to read container uptime, which is exactly what makes a daemon reject it as "no
signal" and fall back to its own ceiling.

Current feed status per flavour:

| flavour | feed | armed |
|---|---|---|
| native (langgraph) | tool start/end hooks | yes (always, in `a2a_executor`) |
| openclaw | subprocess output bytes | yes |
| codex | tool-activity file per tool item | yes, once a first tool call is seen |
| hermes | tool-activity file per tool dispatch | yes, once a first tool call is seen |
| claude-code | **none** | **no** — see below |

`ClaudeSDKExecutor` touches the lease by no route at all: the transcript-tail
poller sketched as feeder B in `turn_lease.py` was never built, and it does not
write the tool-activity file. So claude-code keeps the pre-existing "no signal"
behaviour, and a wedged claude-code turn is still only bounded by the daemon's
own ceiling rather than the 900s TTL. Closing that needs a one-line
`record_tool_activity()` at its `_report_tool_use` site — the same pattern codex
and hermes already use — in the template repo. Tracked as runtime#410.

The check is empirical rather than a list of runtime names precisely so that
this heals itself: the moment claude-code starts writing the activity file, its
next turn is armed with no change here.
