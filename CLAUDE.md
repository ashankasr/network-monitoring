# CLAUDE.md

Onboard network QoE monitor (POC). Scheduled runner containers (one per train
router) generate synthetic web-browsing traffic against a test endpoint, measure
it, and score it with a POC QoE heuristic. Events flow RabbitMQ → ingest worker
→ MongoDB → dashboard API → React dashboard.

**Source of truth for design:** `docs/DECISIONS.md` (code comments cite `D-xx`)
and `README.md`. The handoff brief `CLAUDE_CODE_HANDOFF_NETWORK_QOE_POC.md` is
historical. Don't treat it as requirements, and don't cite its scope list
against current work.

## Commands

```bash
docker compose up -d --build        # everything: infra, endpoint, worker, dashboard (:8001), 2 example runners
docker compose logs -f runner-train-101
docker compose down                 # add -v to wipe MongoDB/RabbitMQ/output volumes

uv sync && uv run pytest            # tests (~2 s); the MongoDB round-trip test skips if Mongo is down
uv run python -m app.traffic_generator.main --config configs/example.yaml --duration 10   # local single run
cd web && npm run dev               # :5173, proxies /api -> :8001
```

Before calling work done:
1. Run `uv run pytest`, and `npx tsc --noEmit` in `web/` if the frontend changed.
2. Rebuild with `docker compose up -d --build`.
3. Confirm a runner logs `Run ... finished` and the run appears at
   `http://127.0.0.1:8001/api/runs`.

To check the pipeline end to end:
- events published = 1 + the run's requests + 1;
- `db.requests.countDocuments({test_run_id})` matches;
- `rabbitmqctl list_queues` shows `qoe.ingest` and `qoe.ingest.dead` at 0.

## Layout

- `Dockerfile`: one image (`onboard-qoe`) for all Python services, with a Node stage that builds `web/dist`.
- `docker-compose.yml`:
  - the `x-runner` template, with example runners `runner-train-101` and `runner-train-202`;
  - per-instance settings are `QOE_*` environment variables.
- `configs/`:
  - `runner.yaml`: scheduled; used by the image.
  - `example.yaml`: single run, files only.
  - `live.yaml`: single run with publishing.
- `app/limits.py`: hard limits shared by the endpoint and config validation. Change both sides together.
- `app/test_server/main.py`: endpoints. A middleware adds `Server-Timing`, `Cache-Control` and `X-QoE-Test-Endpoint`.
- `app/traffic_generator/`:
  - Data flow: `config` → `profiles` → `pacing` → `measure` → `runner` → `metrics` → `qoe` → `reporter`.
  - `main.py` is the CLI: single or scheduled mode, and SIGTERM handling.
  - `scheduler.py`: cadence, jitter, retention, per-device output folders.
  - `publisher.py`: buffered RabbitMQ publishing.
  - `outbox.py`: the SQLite outbox of unconfirmed events (scheduled runners).
- `app/events.py`: the message contract. The publisher and worker must agree on it.
- `app/settings.py`: `QOE_RABBITMQ_URL` / `QOE_MONGO_URL` / `QOE_MONGO_DB` for the worker and API.
- `app/ingest_worker/`: `store.py` holds the pure update builders plus `Store`; `main.py` holds the consumer loop and ack rules.
- `app/dashboard_api/`: `queries.py` holds the aggregations; `main.py` holds the routes (`/api/devices`, `/api/runs`, `/api/runs/{id}/live`) and serves `web/dist`.
- `web/`: React 19 + Vite + Tailwind v4 + shadcn/ui.
  - `src/components/ui/` is generated: don't hand-edit it; re-add with `npx shadcn@4.22.0 add`.
  - The dashboard is in `src/components/dashboard/` and `src/App.tsx`.
  - `src/api.ts` mirrors the API responses.

## Invariants — do not break without updating docs/DECISIONS.md

- **Safety:**
  - Every load-related config field has a hard ceiling; there is no unbounded mode.
  - Scheduled runs are finite, never overlap (`jitter + duration + timeout ≤ interval`), and use at most 50 % of the interval.
  - Runners only target the configured URL, after the `/health` preflight confirms the test-endpoint header.
  - No scanning, discovery, proxy use (`trust_env=False`), or raw packets.
- **Config layering:** YAML < environment (`ENV_OVERRIDES` in `config.py`) < CLI, all validated alike.
  - `QOE_RABBITMQ_URL` never enables publishing on its own.
  - `device_id` defaults to the hostname.
- **Definitions:**
  - `latency_ms` = TTFB − `Server-Timing` duration.
  - Throughput uses the transfer phase only, for bodies ≥ 64 KiB.
  - Run-level throughput is aggregate bytes ÷ time.
- **Accounting:**
  - `cancelled` requests are excluded from success/failure counts.
  - Timing distributions use successful requests only.
  - 4xx/5xx and short bodies are failures.
- **Robustness:**
  - Network errors are recorded, never raised.
  - JSONL is flushed per record.
  - Ctrl+C and SIGTERM both still write and publish the summary.
  - A failed preflight skips one scheduled run, never the schedule.
- **Rate limiting** is global and burst-free (`RateLimiter`). Don't swap in a limiter that allows bursts.
- **QoE:**
  - All scoring stays in `qoe.py`, labelled `poc-v1` with its disclaimer; never present it as validated passenger satisfaction.
  - The API reuses `qoe.score`; don't reimplement it in TypeScript.
- **Publishing:**
  - It never blocks or fails measurement: `publish()` is synchronous and buffered.
  - Files are written regardless.
  - A scheduled runner keeps one connection for its lifetime, and starts measuring even if the broker is unreachable.
- **Outbox (scheduled runners):**
  - An event's row is inserted before it is buffered, and deleted only on the broker's confirm.
  - Backlog is read only when the buffer is empty, and sent only between runs (`backlog_allowed`).
  - It is capped (`max_outbox_mb`); over the cap the oldest events are dropped with a warning.
- **Ingest:**
  - Ack only after the MongoDB write.
  - Writes are idempotent upserts by natural id.
  - Malformed messages go to the dead-letter queue.
- **Message schema:** a change means bumping `schema_version`, and keeping the worker able to read the old version.
- **Output retention:** scheduled runners prune only result files in their own `output/<device_id>/` folder, within both `keep_output_runs` and `max_output_mb`, and never the outbox. Single runs never delete.
- **Dashboard:**
  - The browser only talks to the read-only API.
  - Live figures come from MongoDB aggregation; finished runs show the runner's summary.
- **Charts:**
  - Colors come from the validated palette in `web/src/index.css` (`--chart-*`, `--status-*`).
  - Status colors always appear with an icon and label.
  - Every chart keeps its Table view. No dual-axis charts.

## Conventions

- Python 3.12+ (pinned to 3.12 in `.python-version` and the image). Type hints everywhere.
- Pydantic models use `extra="forbid"`. Validation errors should name the offending field.
- Time:
  - `time.perf_counter()` for durations;
  - `time.monotonic()` for scheduling;
  - wall-clock UTC for record timestamps and log lines.
- Keep modules small, with no frameworks or abstractions beyond need. Make time-based logic injectable so it can be tested without waiting (see `scheduler.py`).
- Tests use `asyncio.run()` directly (no pytest-asyncio), and `httpx.ASGITransport` for in-process end-to-end runs.
- Frontend: Tailwind + shadcn/ui only. No hand-written CSS beyond the theme tokens in `index.css`.

## Adding things

- **A per-instance setting:** add it to `ENV_OVERRIDES` in `config.py`, then to the README table and the compose examples.
- **A profile (download/streaming next):**
  1. Add a `<Name>ProfileConfig` with `type: Literal[...]`, and make `ProfileConfig` a discriminated union.
  2. Add the class and a `create_profile` case in `profiles.py`. Set `expected_bytes`.
  3. Add any endpoint, with limits from `app/limits.py`.
  4. Read D-19 first: streaming must not count client-side chunks.

## Gotchas

- Python as a container's PID 1 ignores SIGTERM unless it has a handler (`install_sigterm_handler` in `main.py`). Keep it.
- Testing Ctrl+C from a non-interactive shell: background jobs start with SIGINT
  ignored, and `uv run` forwards signals, so the child gets two. Use
  `subprocess.Popen` with `preexec_fn` restoring `SIG_DFL` on `.venv/bin/python`,
  or test with `docker stop`.
- Local `uvicorn` on :8000/:8001 conflicts with the compose `test-endpoint` and `dashboard` services. Run one or the other.
- Same-host throughput comes out in the Gbps range and means nothing (D-02, D-04).
- `fastapi.testclient` emits a Starlette deprecation warning about httpx. It is harmless.
- RabbitMQ's `guest` user is refused through Docker port mappings; use `qoe`/`qoe`.
- Runners bind-mount `./output`. On a Linux host it must be writable by uid 10001 (`sudo chown 10001 output`); macOS needs nothing.
- shadcn CLI v4: `init -y -b radix -p nova`. The `cn` import in generated components is shadcn's own `cn` npm package.
- Recharts 3 sorts legend and tooltip items alphabetically unless you pass `itemSorter`.
- Headless Chrome screenshots of the dashboard are unreliable from the command line:
  - `--virtual-time-budget` hangs, because polling never lets the page go idle.
  - `--timeout` stops all loading when it fires, which can capture an empty page.
  - Instead, drive Chrome over the DevTools protocol (`--remote-debugging-port`): navigate, wait about 8 s, then `Page.captureScreenshot`.
- The dashboard API has no `--reload`. Restart it (or `docker compose up -d --build`) after editing `app/`.
