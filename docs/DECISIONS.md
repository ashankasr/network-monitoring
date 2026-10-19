# Design decisions

The authoritative record of how and why the system works as it does. Code
comments refer to entries by ID. When a decision changes, update its entry
rather than deleting it, and note what replaced it.

The project began from a handoff brief (`CLAUDE_CODE_HANDOFF_NETWORK_QOE_POC.md`).
That brief is now **historical**: the entries below record where the project
has moved past it (for example D-20 and D-27/D-28). Where the two differ, this
log wins.

Status: **Accepted** = implemented; **Limitation** = known issue, documented but
not solved; **Deferred** = decided, to be implemented with a later step.

---

## Measurement

### D-01 Latency is the network share of TTFB — Accepted
**Problem.** The spec has both `latency_ms` and `time_to_first_byte_ms` but does
not define latency. If latency were TTFB or total time, the artificial
`delay_ms` would dominate it, and the latency score would grade our own config
instead of the network.

**Decision.**
- `time_to_first_byte_ms` = request start → response headers received.
- The endpoint reports its own processing time (including injected delay) in a
  `Server-Timing: app;dur=<ms>` header.
- `latency_ms` = TTFB − server time. This is the network and protocol share:
  connection setup (if any), request upload, and the response's first bytes in flight.
- `total_duration_ms` (start → last byte) is reported separately as the "page load" figure.
- If a response has no `Server-Timing`, `latency_ms` falls back to TTFB.

**Consequence.** The QoE latency component uses p95 `latency_ms`. Server
slowness is a controlled input, not a network effect.

### D-02 Throughput is measured over the transfer phase only, and only for large bodies — Accepted
**Problem.** Bytes ÷ total time on a 50 KB page mostly measures setup and TTFB,
not bandwidth. "Average throughput" was also undefined: a mean of per-request
rates and total bytes ÷ total time give different answers.

**Decision.**
- Per request: `throughput_mbps = body bits / (total − TTFB)`, in decimal megabits (10⁶ bits).
- Not reported for bodies under 64 KiB (`MIN_THROUGHPUT_BYTES`), where the
  transfer phase is one or two reads and the number is noise.
- Run level: **aggregate** = sum of eligible bytes ÷ sum of their transfer times.
  The mean of per-request rates is not used, because it gives a small page the
  same weight as a large download.

**Known caveat.** TTFB is taken when headers are parsed. On fast links the
first part of the body often arrives in the same read, so transfer time is
underestimated and throughput is inflated. On localhost, numbers in the
Gbps range are expected and meaningless. The error shrinks as bodies get larger
and links get slower.

### D-03 Payloads are generated once, at startup — Accepted
**Problem.** Generating a payload per request costs CPU time that would be
counted as latency, especially with the generator and endpoint on one machine.

**Decision.** The endpoint creates one `MAX_PAYLOAD_KB` random buffer at import
time and serves slices of it. Startup takes about 10 MB of memory.

### D-04 Localhost runs validate the wiring, not the network — Limitation
With both components on one host, results reflect CPU scheduling and the
loopback interface. Meaningful numbers need the two components on separate
machines with a real or shaped link between them (spec §20). Nothing in the code
assumes localhost: the target is only the configured `base_url`.

### D-05 Closed-loop users understate tail latency — Limitation
Each simulated user waits for a response, then for the think time, before the
next request. When the service slows down, fewer requests are sent, so slow
periods are under-sampled and p95/p99 look better than what users experienced
("coordinated omission"). This is accepted for a POC because it matches how a
single passenger behaves. An open-loop (fixed-schedule) mode would be needed to
measure tails rigorously.

### D-06 One HTTP client per simulated user, with keep-alive — Accepted
**Problem.** Connection pooling hides TCP setup after the first request. Real page
loads often pay that cost, and on lossy links it matters.

**Decision.** Each user has its own `httpx.AsyncClient` (its own connection
pool), like a separate passenger device. Keep-alive is left on, as browsers use
it. Each record shows `new_connection` and `connect_ms`, collected from httpcore
trace events, so connection setup can be analysed rather than hidden.

There is no TLS yet, although real passenger traffic is almost entirely HTTPS.
TLS handshakes are a significant cost on high-RTT links and should be added
before drawing conclusions from the two-machine experiment.

### D-07 Byte counts reflect what crossed the wire — Accepted
**Problem.** A compressing proxy on a real onboard network would shrink
compressible payloads and inflate apparent throughput.

**Decision.** Payloads are random (incompressible) bytes, the generator sends
`Accept-Encoding: identity`, and the generator counts raw (undecoded) body bytes.

---

## Safety

### D-08 The endpoint enforces hard parameter limits — Accepted
**Problem.** Unbounded `size_kb` / `delay_ms` let a single request exhaust
memory or hold connections open indefinitely.

**Decision.** `size_kb` ≤ 10240 (10 MB), `delay_ms` ≤ 30 000, and `status_code`
is restricted to sensible ranges. Out-of-range values return 422. The limits
live in `app/limits.py`, and generator config validation uses the same limits.

### D-09 The generator only runs against the test endpoint — Accepted
**Problem.** A typo in `base_url` could send load to an arbitrary site.

**Decision.** Before a run, the generator calls `GET /health` and requires a
200, `{"status": "ok"}` and the `X-QoE-Test-Endpoint` header. Otherwise it
exits without sending traffic. This is a guard against mistakes, not
authentication.

### D-10 Global request-rate and bandwidth limits, without bursts — Accepted
**Problem.** The spec only limits requests per second, and does not say whether
that is per user or global. A request-rate cap does not limit bandwidth: at 20
req/s, 10 MB downloads would be about 1.6 Gbps.

**Decision.**
- `max_requests_per_second` and `max_mbps` are **global** across all users.
- A single scheduler (`RateLimiter`) hands out start times. Consecutive starts are
  at least `1/max_rps` apart, and at least the time the previous request's
  planned body takes at `max_mbps`. Idle time does not build up credit, so
  there are no bursts.
- Bandwidth is budgeted from the *planned* body size, because the decision is
  made before sending.

### D-11 Configuration is strict and bounded — Accepted
- Unknown keys are errors (`extra="forbid"`), so a typo cannot silently become a default.
- Every load-related setting has a hard ceiling: users ≤ 50, rps ≤ 100,
  Mbps ≤ 1000, duration ≤ 3600 s, timeout ≤ 60 s. There is no unbounded mode.
- Defaults are conservative: 2 users, 5 req/s, 20 Mbps, 30 s.
- `base_url` must be http(s) with a host, and has no default.
- `metadata.device_id` defaults to the hostname (D-28). The other metadata fields default to `local-*`.
- **Renamed from the spec:** the spec's `min_delay_ms`/`max_delay_ms` was
  ambiguous (think time or server delay?). It is split into `think_time_*_ms`
  and `server_delay_*_ms`. Payload fields are `payload_*_kb`.
- Profile settings are per profile type, keyed by `type`. Only `web_browsing`
  exists now; others will join as a discriminated union.

### D-16 The endpoint is documented to bind to 127.0.0.1 — Accepted
The spec's example used `--host 0.0.0.0`, which exposes an unauthenticated
traffic source to the whole network. The docs use `127.0.0.1`. Binding to a LAN
interface is an explicit choice for the two-machine experiment.

*Update (D-28):* in Docker, services listen on 0.0.0.0 *inside* their
container, so other containers can reach them. The host ports are still
published on 127.0.0.1 only.

### D-17 No environment proxies, no redirects — Accepted
The generator ignores `HTTP(S)_PROXY` from the environment (`trust_env=False`)
and does not follow redirects. Traffic only goes to the configured target, and
measurements are not silently routed through a dev machine's proxy.

---

## Run semantics

### D-12 What counts as a failure — Accepted
A request succeeds only if it completes within the timeout, returns a status
below 400, and (when the size is known) delivers exactly the expected number of
bytes. httpx does not raise on 4xx/5xx, so this is checked explicitly.
Redirects (3xx) are not followed and count as success. No current profile
produces them.

To exercise failure accounting end to end, profiles have an `error_rate`: that
share of requests goes to `/failure` (HTTP 500).

### D-13 Deadline and Ctrl+C behaviour — Accepted
- **Deadline:** no request starts after the deadline. Requests in flight
  finish, bounded by the request timeout. A completed run lasts at least the
  configured duration and at most duration + timeout. `achieved_rps` is
  computed over the actual duration.
- **First Ctrl+C:** asyncio cancels the run. In-flight requests are abandoned and
  recorded with `outcome: "cancelled"`. The summary is still written, and the
  exit code is 130.
- **Second Ctrl+C:** immediate exit. The JSONL is already on disk (D-14), but no
  summary is written.
- Cancelled requests are counted separately and excluded from success/failure
  figures, so interrupting a run does not lower its reliability.
- SIGTERM (`docker stop`) behaves like the first Ctrl+C (D-29).
- Timing distributions use successful requests only.

### D-14 JSONL is written and flushed per record — Accepted
Records are not held until the end, so a crash or kill still leaves every
completed measurement on disk. Scheduled runners keep a bounded number of
runs on disk (D-30).

---

## Scoring

### D-15 POC QoE thresholds (model `poc-v1`) — Accepted, placeholder values
The spec gives weights (0.40 / 0.35 / 0.25) but no thresholds. The values below
are placeholders chosen for plausibility, **not** derived from research or user
studies. Each component maps linearly from "bad" (0) to "good" (100), clamped.

| Component   | Input                                  | 100 at     | 0 at       |
|-------------|----------------------------------------|------------|------------|
| latency     | p95 `latency_ms` (D-01)                | ≤ 100 ms   | ≥ 1000 ms  |
| reliability | success % of completed requests        | 100 %      | ≤ 80 %     |
| throughput  | aggregate Mbps (D-02)                  | ≥ 10 Mbps  | ≤ 0.5 Mbps |

- A component without data (e.g. no body ≥ 64 KiB for throughput) is left out,
  the remaining weights are rescaled, and it is listed in `missing`.
- If every request failed, the score is 0. If no request completed, the score is `null`.
- Every output carries `qoe_model` and a disclaimer.
- All of this lives in `app/traffic_generator/qoe.py` so it can be replaced wholesale.

---

## Scope

### D-18 Build order: wiring first, one profile — Accepted
For the first iteration, only the web-browsing profile and the `/health`,
`/page` and `/failure` endpoints are built, end to end: config → scheduling →
measurement → JSONL → summary → QoE → CLI.

### D-19 Download and streaming profiles — Deferred
Notes for when they are added:
- `/download` can reuse the `/page` payload approach.
- Streaming should **not** count client-side chunks. TCP, uvicorn and httpx
  buffering merge and split chunks, so client chunk counts do not match server
  chunks. Track bytes over time instead, and flag a stall when no data arrives
  for longer than about 1.5× the configured interval.
- Streams cut off by the run deadline must not be counted as "incomplete".
- Throughput scoring for streaming should probably be stall-based rather than Mbps-based.

---

## Live pipeline (added 2026-10-17)

### D-20 Scope change: RabbitMQ, MongoDB and a live dashboard — Accepted
The handoff spec (§19) listed message queues, a database and a dashboard as out
of scope. They were added on explicit request:

```
generator ──▶ RabbitMQ ──▶ ingest worker ──▶ MongoDB ──▶ dashboard API ──▶ React dashboard
```

The JSONL and summary files are unchanged and stay the source of truth. Without
a `publish` section in the config, the generator behaves exactly as before.

### D-21 Infrastructure is local-only — Accepted
`docker-compose.yml` binds RabbitMQ and MongoDB to 127.0.0.1. RabbitMQ uses
dev credentials (`qoe` / `qoe`). The built-in `guest` user is refused over a
Docker port mapping, because the connection is not from loopback. MongoDB has
no authentication. None of this is fit for deployment: real use needs secrets
management, TLS, and auth on both services.

### D-22 Message contract — Accepted
- One durable **topic** exchange, `qoe.events`. Routing key = event type:
  `run.started`, `request.measured`, `run.finished`.
- Every message body is a JSON `Envelope`
  (`schema_version: "qoe.v1"`, `type`, `sent_at`, `data`), defined in `app/events.py`.
  `request.measured.data` is exactly a JSONL record, and `run.finished.data`
  is exactly the summary.
- Messages are persistent, published with publisher confirms.
- Durable queue `qoe.ingest` (bound with `#`), dead-lettering to `qoe.dead` → `qoe.ingest.dead`.
- **Both sides declare the topology.** Events published before the worker has
  ever started are queued, not dropped as unroutable.

### D-23 Publishing never affects measurement — Accepted
- `publish()` only appends to an in-memory buffer; a background task sends from
  it. A slow or down broker cannot change timings.
- While the broker is unreachable, sending is retried (the connection
  reconnects itself). When the buffer is full (`buffer_size`, default 10 000),
  new events are dropped and counted.
- At the end the generator reports published / undelivered / dropped counts.
- If the broker is unreachable when the run *starts*, the run is refused before
  any traffic, so a missing broker is never discovered only at the end.
  A scheduled runner first retries for up to 120 s, because containers can
  start before the broker is ready. After that it exits with code 1, and
  Docker's restart policy tries again.
- A scheduled runner keeps one broker connection for its whole lifetime. The
  buffer bridges outages between runs as well as during them.
- The summary file is written *before* the final publish, so Ctrl+C during
  shutdown cannot lose it.

### D-24 Ingest is at-least-once with idempotent writes — Accepted
- The worker acks a message only after MongoDB has stored it. A crash means
  redelivery, not loss.
- Writes are upserts keyed by natural ids (`requests._id = request_id`,
  `runs._id = test_run_id`). Request documents use `$setOnInsert`, so
  redelivery never duplicates or overwrites.
- Order does not matter: a request can create its run document, and a late
  `run.started` does not reopen a finished run.
- Malformed messages are rejected to the dead-letter queue for inspection.
  Database errors cause a requeue after 2 s.
- `prefetch_count=100` bounds how many unacked messages the worker holds.

### D-25 Dashboard data flow — Accepted
- **A read-only API sits between the browser and MongoDB.** A browser must not
  hold database credentials or run arbitrary queries.
- **Polling every 2 s, not push.** MongoDB change streams need a replica set,
  and SSE/WebSockets add connection management. For a POC at ≤ 100 req/s,
  polling one endpoint is simpler and robust. Push is the upgrade path.
- **While running**, figures are aggregated in MongoDB rather than loading
  every request. Percentiles use MongoDB's *approximate* `$percentile`, so live
  values can differ slightly from the final ones. The QoE score is computed with
  the same `qoe.py` as the generator.
- **Once finished**, the API serves the generator's exact summary instead.
- The newest, still-filling bucket is omitted while a run is live, so charts
  never show a false drop at the right edge.
- A run still marked running with no events for 60 s is shown as "No updates".
  Its generator was most likely killed.
- After `npm run build`, the API also serves the dashboard, so one port (8001) is enough.

### D-26 Frontend stack — Accepted
React 19 + Vite + Tailwind CSS v4 + shadcn/ui (Radix base, Nova preset), with
charts through shadcn's `ChartContainer` (Recharts 3).
- Chart colors come from the validated dataviz palette. The blue/orange pair
  passed the CVD and contrast checks in both modes, and the dark mode has its
  own steps.
- Failures use the reserved "critical" status color, always with an icon and label.
- Every chart has a Table view.
- The theme follows the OS, with a manual override.

---

## Scheduled runners in Docker (added 2026-10-19)

### D-27 Runners repeat finite runs on a fixed cadence — Accepted
Monitoring needs repeated measurements, but the "no unbounded load" rule
(D-11) still holds. A runner therefore repeats a **finite** run; it never
generates continuous traffic.

- **Default: a 60 s run every 15 minutes.** On a shared train link, synthetic
  traffic competes with passengers, so it runs about 7 % of the time.
- **Fixed slots** at `first_start + k × interval`: a run's length never shifts
  later runs. If a run overran a slot, the missed slot is skipped rather than
  running back to back.
- **Jitter:** a random 0–`jitter_seconds` delay (default 30) before each run,
  so a fleet restarted together doesn't test in lockstep.
- **Validation:**
  - `jitter + duration + request timeout ≤ interval`, so runs can never overlap.
  - `duration ≤ 50 %` of the interval (`MAX_DUTY_CYCLE`).
  - The interval is between 60 s and 24 h.
- **First run:** starts as soon as the runner starts (plus jitter), so a new
  deployment reports immediately.
- **Failed preflight:** the run is skipped and the schedule continues; one
  outage doesn't kill the runner.
- **Turning it on:** a `schedule` section, `QOE_SCHEDULE_INTERVAL_SECONDS`, or
  `--interval`. `--once` forces a single run.
- **Implementation:** `scheduler.py` is pure, with an injectable clock, sleep
  and random source, so it's tested without waiting.

### D-28 One image; per-instance settings come from environment variables — Accepted
- **One image, `onboard-qoe`,** runs every Python service; compose picks the
  command. The dashboard is built in a Node stage and served by the API.
- **Per-instance details come from environment variables** (train, router,
  journey, target, cadence), so one image and one config file
  (`configs/runner.yaml`) serve every instance.
- **Precedence: YAML < environment < CLI.** Environment values pass the same
  validation as YAML. Empty values are ignored. The full list is
  `ENV_OVERRIDES` in `config.py`.
- **`QOE_RABBITMQ_URL` never enables publishing on its own.** It is set for
  every service in compose (the worker needs it), so an implicit switch would
  surprise. It only overrides the URL when the config has a `publish` section.
  Setting a schedule variable *does* enable scheduling, because that is its
  only purpose.
- **`device_id` defaults to the hostname.** In Docker that is the container
  id, so replicas started with `--scale` never share an id. Set
  `QOE_DEVICE_ID` to the real router id.
- **Three ways to run more instances:** add a compose service from the
  `x-runner` template, `docker run` on the compose network, or `--scale` a
  service that doesn't set `QOE_DEVICE_ID`.
- **Hardening:**
  - containers run as a non-root user (uid 10001)
  - `init: true` reaps child processes and forwards signals
  - `restart: unless-stopped`
- `journey_id` is static per container for now. A real deployment would read
  it from train systems.

### D-29 SIGTERM stops a run gracefully — Accepted
`docker stop` sends SIGTERM. Python running as a container's main process
(PID 1) ignores SIGTERM unless it installs a handler. Docker would then kill it
after the grace period, losing the run's summary and its `run.finished` event.

The generator therefore cancels its main task on SIGTERM, exactly as for the
first Ctrl+C (D-13):
- in-flight requests are marked cancelled;
- the summary is written and published;
- the buffer is drained;
- the process exits 0.

Compose gives runners `stop_grace_period: 30s`. Verified: a runner stopped
mid-run shut down in under 0.2 s, and its interrupted run was in MongoDB with
every record.

### D-30 Result files: per-device folders, bounded retention — Accepted
- **Per-device folders.** A scheduled runner writes to
  `output/<device_id>/` (the id made safe for file paths). Runners can share a
  volume without pruning each other's files.
- **Bounded retention.** After each run, only the newest `keep_output_runs`
  runs are kept (default 96, one day at 15 minutes). Runs are found by their
  requests file, so files of runs killed before writing a summary are pruned too.
- **MongoDB is the long-term store.** The files remain the per-run source of
  truth (D-14) for as long as they are kept.
- Single runs (no schedule) never delete anything.

### D-31 Dashboard: filter by device — Accepted
With many runners, "follow latest run" would jump between routers.

- **API:** `/api/devices` lists each reporting device with its latest
  train/journey and run count. `/api/runs` accepts `device_id` and `train_id` filters.
- **UI:** a Device filter sits first in the filter row. "Follow latest" then
  follows the latest run of that device.

A cross-run **trend view** (QoE and latency per device over time) is the
natural next step for scheduled data. It is not built yet.
