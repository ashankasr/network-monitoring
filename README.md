# Onboard Network QoE Monitor — POC

Measures what network conditions mean for passengers. Runners on (or standing
in for) train routers periodically generate synthetic, passenger-like HTTP
traffic against a test endpoint. Each request is measured, each run is
summarised with an **experimental** QoE score, and results stream to a live
fleet dashboard.

> **Authorised use only.** Generate traffic only against systems and networks you
> are authorised to test.

**Status:** web-browsing profile; scheduled runners in Docker (one or many);
live pipeline RabbitMQ → MongoDB → React dashboard. Download and streaming
profiles are next (D-19).

Design decisions and their reasons are in [docs/DECISIONS.md](docs/DECISIONS.md)
(cited as D-xx). That log is the authoritative record. The original handoff
brief (`CLAUDE_CODE_HANDOFF_NETWORK_QOE_POC.md`) is historical and has been
superseded where they differ.

## Architecture

```mermaid
flowchart LR
    subgraph Trains["Runners (one container per router)"]
        R1[runner train-101]
        R2[runner train-202]
        RN[runner ...]
    end
    R1 & R2 & RN -->|synthetic HTTP, 60 s every 15 min| EP[Test endpoint]
    R1 & R2 & RN -->|run + request events| MQ[(RabbitMQ)]
    MQ --> W[Ingest worker]
    W --> DB[(MongoDB)]
    DB --> API[Dashboard API + React UI :8001]
```

Every service is built from one Docker image (`onboard-qoe`); the compose
file picks each service's command.

| Service | Code | Role |
|---|---|---|
| runner(s) | `app/traffic_generator/` | Scheduled synthetic traffic, measurement, summary, publishing |
| test-endpoint | `app/test_server/` | Controllable stand-in for a remote internet service |
| ingest-worker | `app/ingest_worker/` | RabbitMQ → MongoDB, idempotent |
| dashboard | `app/dashboard_api/` + `web/` | Read-only API and the React dashboard |
| rabbitmq, mongodb | official images | Event transport and storage |

## Quick start (Docker)

Requires Docker with Compose.

```bash
docker compose up -d --build
```

This starts the infrastructure, the test endpoint, the ingest worker, the
dashboard and **two example runners** (`runner-train-101`, `runner-train-202`).
Each runner starts its first run within 30 s, then runs again every 15 minutes.

- Dashboard: **http://127.0.0.1:8001**. Pick a device or "All devices"; it
  follows the latest run.
- RabbitMQ management: http://127.0.0.1:15672 (`qoe` / `qoe`)
- Runner logs: `docker compose logs -f runner-train-101`
- Stop everything: `docker compose down`. Add `-v` to also delete stored data.

All ports are published on 127.0.0.1 only, and the credentials are development
values. Do not deploy this compose file as-is (D-21).

## Runners

A runner repeats a **finite** test run on a fixed cadence (D-27). The default
is a **60-second run every 15 minutes** (`configs/runner.yaml`), so a runner
generates traffic about 7 % of the time.

- **Cadence:** slots are fixed (`interval_seconds` apart). A run's length never
  shifts later runs.
- **Jitter:** each start is delayed by a random 0–`jitter_seconds`, so a fleet
  doesn't test in lockstep.
- **Validation:**
  - `jitter + duration + request timeout` must fit in the interval, so runs never overlap.
  - A run may use at most 50 % of the interval.
- **Failed preflight:** if the endpoint is unreachable at a run's start, that run
  is skipped and logged. The schedule continues.
- **Stopping:** `docker stop` (SIGTERM) or Ctrl+C ends the current run early.
  Its summary is still written and published (D-29).
- **Offline delivery:** every event is stored in `output/<device-id>/outbox.db`
  (SQLite) before it is published, and deleted once RabbitMQ confirms it. If
  the broker is unreachable, even at startup, the runner keeps measuring and
  the events wait there. They are sent between runs once the broker is back.
  The outbox is capped at `QOE_MAX_OUTBOX_MB` (default 200 MB, about 7 days);
  beyond that the oldest events are dropped with a warning (D-32).
  - SQLite runs inside the runner process; there is no separate service. The
    file lives on the host and survives container restarts and rebuilds.
  - Each `QOE_DEVICE_ID` must belong to exactly one runner, because the outbox
    is found by device id. Changing a router's id leaves its backlog unsent.
  - Don't open `outbox.db` from the host while its runner is running. Check
    the runner's `Events: … waiting in the outbox` log line instead, or stop
    the runner first.
- **Disk use:** result files are kept in `output/<device-id>/`, which compose
  bind-mounts from `./output` on the host. They are a byproduct; nothing reads
  them back. The newest runs are kept within both `keep_output_runs` (default
  96, one day) and `QOE_MAX_OUTPUT_MB` (default 50 MB); MongoDB keeps the
  history (D-33). On a Linux host, make the folder writable by the container
  user first: `sudo chown 10001 output` (D-34).

### Per-instance settings (environment variables)

Train and router details are passed as environment variables, which override the
YAML (D-28). The order of precedence is **YAML < environment < CLI flags**.
Values are validated with the same limits as the YAML.

| Variable | Overrides | Typical use |
|---|---|---|
| `QOE_DEVICE_ID` | `metadata.device_id` | Router / device id. **Default: the container hostname**, so `--scale` replicas never collide |
| `QOE_TRAIN_ID` | `metadata.train_id` | Train / unit id |
| `QOE_JOURNEY_ID` | `metadata.journey_id` | Service or journey id |
| `QOE_TARGET_URL` | `target.base_url` | Test endpoint the runner measures against |
| `QOE_RUN_DURATION_SECONDS` | `run.duration_seconds` | Run length (default 60) |
| `QOE_SCHEDULE_INTERVAL_SECONDS` | `schedule.interval_seconds` | Cadence (default 900); setting it turns scheduling on |
| `QOE_SCHEDULE_JITTER_SECONDS` | `schedule.jitter_seconds` | Max random start delay (default 30) |
| `QOE_CONCURRENT_USERS` | `run.concurrent_users` | Simulated users per run |
| `QOE_MAX_REQUESTS_PER_SECOND` | `run.max_requests_per_second` | Request-rate cap |
| `QOE_MAX_MBPS` | `run.max_mbps` | Bandwidth cap |
| `QOE_MAX_OUTPUT_MB` | `schedule.max_output_mb` | Disk cap for result files (default 50) |
| `QOE_RABBITMQ_URL` | `publish.rabbitmq_url` | Broker URL. It only applies when the config has a `publish` section; it never enables publishing by itself |
| `QOE_MAX_OUTBOX_MB` | `publish.max_outbox_mb` | Disk cap for events not yet delivered (default 200) |

### Running one or many instances

**Add a service to `docker-compose.yml`**: copy a runner block, change its
environment, then run `docker compose up -d`:

```yaml
  runner-train-404:
    <<: *runner
    environment:
      <<: *app-env
      QOE_DEVICE_ID: router-404-a
      QOE_TRAIN_ID: train-404
      QOE_JOURNEY_ID: journey-404-0800
```

**Or start an instance with `docker run`** on the compose network:

```bash
docker run -d --name qoe-runner-train-303 --network network-monitoring_default --init \
  -e QOE_DEVICE_ID=router-303-a -e QOE_TRAIN_ID=train-303 -e QOE_JOURNEY_ID=journey-303-0730 \
  -e QOE_RABBITMQ_URL=amqp://qoe:qoe@rabbitmq:5672/ \
  onboard-qoe:latest
```

**Or scale one service.** Replicas share their environment but get distinct
device ids from their hostnames. For a service that doesn't set
`QOE_DEVICE_ID`:

```bash
docker compose up -d --scale runner-train-101=3
```

On a real router, run the same image with `QOE_TARGET_URL` pointing at an
endpoint off the train, and `QOE_RABBITMQ_URL` at the central broker.

## Configuration reference (YAML)

Files: `configs/runner.yaml` (scheduled, used by the image),
`configs/example.yaml` (single run) and `configs/live.yaml` (single run with
publishing). Unknown keys are rejected.

| Key | Default | Limit | Meaning |
|---|---|---|---|
| `target.base_url` | *(required)* | http/https | Test endpoint address |
| `run.duration_seconds` | 30 | ≤ 3600 | Run length. No new requests start after it |
| `run.concurrent_users` | 2 | 1–50 | Simulated users, each with its own connection pool |
| `run.request_timeout_seconds` | 10 | ≤ 60 | Total time allowed per request |
| `run.max_requests_per_second` | 5 | ≤ 100 | Global, across all users, no bursts |
| `run.max_mbps` | 20 | ≤ 1000 | Global bandwidth budget, from planned body sizes |
| `run.seed` | none | — | Makes the request sequence reproducible |
| `profile.type` | `web_browsing` | — | Only profile so far |
| `profile.think_time_min_ms` / `_max_ms` | 500 / 3000 | ≤ 60000 | Pause between a user's requests |
| `profile.payload_min_kb` / `_max_kb` | 50 / 500 | ≤ 10240 | Page body size |
| `profile.server_delay_min_ms` / `_max_ms` | 0 / 0 | ≤ 30000 | Injected server delay (excluded from latency) |
| `profile.error_rate` | 0 | 0–1 | Share of requests sent to `/failure` |
| `metadata.device_id` | hostname | — | Copied into every record |
| `metadata.train_id` / `journey_id` | `local-*` | — | Copied into every record |
| `schedule.interval_seconds` | 900 | 60–86400 | Present = scheduled mode |
| `schedule.jitter_seconds` | 30 | ≤ 3600 | Random start delay per run |
| `schedule.max_runs` | none | ≥ 1 | Stop after N runs (none: until stopped) |
| `schedule.keep_output_runs` | 96 | 1–10000 | Runs whose result files are kept on disk |
| `schedule.max_output_mb` | 50 | 1–10000 | Disk cap for result files; the tighter of the two limits applies |
| `publish.rabbitmq_url` | `amqp://qoe:qoe@127.0.0.1:5672/` | amqp(s) | Present = publish live events |
| `publish.buffer_size` | 10000 | 100–1M | Events held in memory while the broker is slow or down. Beyond it, scheduled runners keep them in the outbox only; single runs drop them |
| `publish.max_outbox_mb` | 200 | 1–10000 | Scheduled runners: disk cap for undelivered events |

CLI flags: `--config`, `--duration`, `--users`, `--interval` (turns scheduling
on), `--once` (ignores the schedule), `--output-dir`.

## Test endpoint

| Endpoint | Parameters | Behaviour |
|---|---|---|
| `GET /health` | — | `{"status": "ok"}` |
| `GET /page` | `size_kb` (0–10240), `delay_ms` (0–30000), `status_code` (200–599) | Random binary body of exactly `size_kb` KiB after `delay_ms` |
| `GET /failure` | `status_code` (400–599, default 500), `delay_ms` | Error response |

Every response has `Server-Timing: app;dur=<ms>` (server processing time),
`Cache-Control: no-store`, and `X-QoE-Test-Endpoint`. Out-of-range parameters
return 422. Runners refuse to send traffic to anything that doesn't identify
itself as this endpoint (D-09).

## Measurements and output

Each run writes `<run-id>-requests.jsonl` and `<run-id>-summary.json`, and
publishes the same data as events when publishing is on.

**Requests JSONL** has one object per request, flushed as it is written:

| Field | Meaning |
|---|---|
| `outcome` | `success`, `failure`, or `cancelled` (run interrupted mid-request) |
| `time_to_first_byte_ms` | Request start → response headers |
| `server_time_ms` | Endpoint processing time, including injected delay |
| `latency_ms` | **TTFB − server time**: the network share (D-01) |
| `total_duration_ms` | Request start → last byte |
| `transfer_duration_ms` | Headers → last byte |
| `throughput_mbps` | Body bits ÷ transfer time; only for bodies ≥ 64 KiB (D-02) |
| `new_connection`, `connect_ms` | Whether a TCP connection was opened, and how long it took (D-06) |
| `expected_bytes`, `bytes_received` | Planned and actual body size |

Also included: `timestamp`, `test_run_id`, `device_id`, `train_id`,
`journey_id`, `profile`, `request_id`, `url`, `http_method`, `status_code`,
`success`, `error`.

**Summary** contains:
- request counts, success %, and achieved req/s
- latency, TTFB and total-duration distributions (avg/min/max/p50/p95/p99, successful requests only)
- total bytes received
- aggregate throughput
- the QoE result
- the effective config

## Dashboard

At http://127.0.0.1:8001:
- a **Device** filter
- a **Run** picker with "Follow latest run"
- the QoE score and its components
- run details (device, train, journey, progress)
- KPI tiles
- latency, request-rate and throughput charts, each with a Table view
- the most recent requests

While a run is in progress, figures are live aggregates (approximate
percentiles). Once it finishes, the runner's exact summary is shown (D-25).
The page polls every 2 s while a run is live, and every 10 s otherwise.

## The QoE score (POC heuristic)

> This is **not** a validated QoE model and does **not** represent passenger
> satisfaction. It exists to show the concept and to compare runs with each other.

`QoE = 0.40 × latency + 0.35 × reliability + 0.25 × throughput`. Each component
is 0–100, mapped linearly between placeholder thresholds:

| Component | Input | 100 at | 0 at |
|---|---|---|---|
| latency | p95 network latency | ≤ 100 ms | ≥ 1000 ms |
| reliability | success % | 100 % | ≤ 80 % |
| throughput | aggregate Mbps | ≥ 10 | ≤ 0.5 |

A component without data is left out and the remaining weights are rescaled.
The logic is isolated in `app/traffic_generator/qoe.py` for replacement (D-15).

## Safety restrictions

- Traffic only goes to the configured target, and only after it identifies
  itself as the test endpoint. There is no scanning, discovery, proxy rotation,
  or evasion. Environment proxies are ignored.
- Hard limits on users, request rate, bandwidth, duration and timeout. No
  unbounded mode. Scheduled runs are finite, never overlap, and use at most
  50 % of each interval.
- The endpoint rejects oversized or overlong requests.
- HTTP only, through the OS network stack: no raw packets.

## Local development (without Docker for the Python services)

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12+) and Node.js 20+.
RabbitMQ and MongoDB can still come from compose:

```bash
uv sync
docker compose up -d rabbitmq mongodb
uv run uvicorn app.test_server.main:app --host 127.0.0.1 --port 8000
uv run python -m app.ingest_worker.main
uv run uvicorn app.dashboard_api.main:app --host 127.0.0.1 --port 8001
cd web && npm install && npm run dev            # http://127.0.0.1:5173, proxies /api to :8001
```

Stop the compose `test-endpoint` and `dashboard` services first if they are
running, because they use the same ports.

```bash
# single runs
uv run python -m app.traffic_generator.main --config configs/example.yaml              # files only
uv run python -m app.traffic_generator.main --config configs/live.yaml --duration 60   # + live events
# a scheduled runner on the host
QOE_DEVICE_ID=laptop QOE_TRAIN_ID=dev uv run python -m app.traffic_generator.main \
  --config configs/live.yaml --duration 60 --interval 300
```

Exit codes for single runs: `0` completed, `1` preflight/broker failure or
internal error, `2` invalid config, `130` interrupted.

## Tests

```bash
uv run pytest
```

The tests cover:
- config validation, including schedules and environment overrides
- the endpoint
- profiles and rate limiting
- the scheduler's cadence, jitter and retention
- percentiles, throughput and QoE
- end-to-end runs, including cancellation
- the event contract, publisher buffering and the outbox (backlog, cap, retries)
- the worker's ack rules and idempotent writes
- the dashboard queries

One test round-trips data through MongoDB and the API. It is skipped when
MongoDB isn't running.

## Limitations

- Localhost or same-host results reflect the host, not a network (D-04).
- Closed-loop users understate tail latency under slowdowns (D-05).
- Throughput is overestimated on very fast links (D-02).
- No TLS yet (D-06).
- QoE thresholds are placeholders (D-15).
- `journey_id` is a static environment variable. A real deployment would read
  it from train systems.

## Next steps

1. Trend view: QoE and latency across scheduled runs, per device and train.
2. Download and streaming profiles (D-19).
3. Put the runner and endpoint on separate machines with shaped links (latency,
   loss, bandwidth, outages), and compare how the metrics and QoE respond.
4. TLS on the endpoint, and handshake cost on high-RTT links.
5. Production hardening: secrets, TLS for RabbitMQ and MongoDB, auth on the dashboard.
