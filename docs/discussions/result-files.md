# Discussion: result files and offline delivery (resolved)

Status: **resolved 2026-10-19.** Implemented and recorded as D-32 (SQLite
outbox), D-33 (result files capped by size) and D-34 (bind mount) in
`docs/DECISIONS.md`, with the proposed defaults (200 MB outbox, 50 MB files).
The decisions are the source of truth; this file keeps the reasoning.
Diagram of the design: https://claude.ai/artifact/UQhSdy7aW65nZMH1qyYWWE

"Behaviour before this change" describes the code as it was when the discussion started.

## Behaviour before this change

Each run writes two files:

| File | Contents | Measured size (60 s run) |
|---|---|---|
| `<run-id>-requests.jsonl` | One line per request, flushed per record (D-14) | ~280 KB, ~430 lines (~650 B/line) |
| `<run-id>-summary.json` | Counts, latency distributions, throughput, QoE, effective config | ~2 KB |

- **Single runs** write to `output/`.
- **Scheduled runners** write to `output/<device_id>/` (D-30).
- **In Docker**, that folder is the named volume `runner-output`, mounted at `/app/output` and shared by all runners.
- **Retention:** each runner keeps its newest `keep_output_runs` runs (default 96). That is about **27 MB and one day per router** at one 60 s run every 15 minutes. Single runs never delete anything.
- **Compression** (measured): gzip shrinks a requests file 8.4×, from 277 KB to 33 KB.

**The files are a byproduct.** Nothing in the system reads them back; the only
code that touches them writes them or prunes them. Each measured request is
handed, in memory, to two independent consumers (`runner.py`, `_record`):
the JSONL writer and `publisher.publish()`. RabbitMQ is fed from memory, and the
dashboard reads only MongoDB. The files matter for local single runs without a
broker (their only output) and for a person inspecting a run.

**The real gap is offline delivery, not the files.** The publisher's in-memory
buffer holds 10,000 events (~23 runs). Beyond that, events are dropped (D-23),
and a restart or power loss loses whatever was buffered. Also, a scheduled
runner that cannot reach RabbitMQ within 120 s of starting exits and is
restarted in a loop, so a router rebooted while offline measures nothing.

## Answers (2026-10-19)

1. **Role of the files:** at first "send queue", then revised: the files stay a
   byproduct. Durability comes from a SQLite outbox instead (see below).
2. **Router disk:** unknown; every cap must be configurable per instance.
3. **Offline time:** up to a few days.
4. **Host access:** bind mount `./output:/app/output`.

## Options considered for offline delivery

- **Files as the outbox** (events file per run, `.delivered` markers, a
  re-sender reading files). **Rejected.** It would mean building our own
  durable queue: markers, replay order, partial lines after power loss,
  rename-after-gzip, safe deletion. That is where bugs would come from.
- **SQLite outbox in the runner.** **Chosen.**
- **Local RabbitMQ per router plus a shovel to the central broker.** Durable
  with little runner code, but a second broker on every router (~100–200 MB
  RAM). Revisit if routers turn out to have the resources.
- **Bigger in-memory buffer.** Lost on reboot or power loss; a few days is
  ~125k events, ~80 MB of RAM. Rejected.
- **Accept the loss.** Rejected: up to a few days offline is expected.

## Agreed design: SQLite outbox (as implemented)

- **Where:** `output/<device_id>/outbox.db`, scheduled runners only. Single runs
  keep today's behaviour: refuse to start without the broker, and drain for 5 s
  at the end.
- **Write path:** `publish()` stays synchronous. It inserts the envelope
  (routing key and body, which carries `schema_version`) as one row, then puts
  it on the in-memory buffer. WAL mode with `synchronous=NORMAL`: no fsync per
  event, and a power loss can lose only the last moments, never corrupt the file.
- **Send path:** the sender sends from memory as today. When the broker
  confirms an event, its row is deleted (in batches).
- **Buffer full:** nothing is dropped. The event is already in SQLite and simply
  waits there as backlog.
- **Backlog:** rows left from a previous process (restart, power loss) or spilled
  from a full buffer. When the in-memory buffer is empty, the sender reads the
  backlog from SQLite, oldest first. Backlog is sent only **between runs**, so
  catch-up traffic stays out of the measurement window. Live events of the
  current run are still sent during it, as today.
- **Repeats are harmless:** an event sent but not yet confirmed when the runner
  stops is sent again later. MongoDB upserts make that a no-op (D-24).
- **Cap:** `max_outbox_mb` (env `QOE_MAX_OUTBOX_MB`), proposed default 200 MB,
  which is about 300k events, or about 7 days at the default cadence. When it is
  reached, the oldest rows are deleted and a warning is logged with the count.
  Incremental vacuum keeps the file from staying at its high-water size.
- **Startup without the broker:** a scheduled runner starts measuring at once
  and connects in the background, retrying indefinitely. Its events wait in the
  outbox. This amends D-23.
- **Result files:** stay a byproduct, unchanged in format. Retention keeps
  `keep_output_runs` and adds a size cap `max_output_mb` (env
  `QOE_MAX_OUTPUT_MB`, proposed default 50 MB); whichever limit is hit first
  applies. No gzip needed. This amends D-30.
- **Bind mount:** `./output:/app/output` replaces the `runner-output` volume.
  On macOS it works as is; on a Linux host the folder must be writable by uid
  10001. The runner checks this at startup and exits with a clear message.
- **Unchanged:** the message schema, the ingest worker, MongoDB and the
  dashboard.

Decisions to record when implemented: D-32 (SQLite outbox; scheduled runners
start without the broker; amends D-23), D-33 (result file size cap; amends
D-30), D-34 (bind mount for `./output`).

## Context for picking this up

- Relevant code:
  - `app/traffic_generator/publisher.py`: buffer, confirms, `published` / `undelivered` counters
  - `app/traffic_generator/main.py`: `start_publisher`, `run_one`, `scheduled`
  - `app/traffic_generator/scheduler.py`: `prune_outputs`, `device_output_dir`, `run_schedule`
  - `app/traffic_generator/config.py`: `PublishConfig`, `ScheduleConfig`, `ENV_OVERRIDES`
- Relevant decisions: D-14, D-22, D-23, D-24, D-27, D-30.
- Stack state when this was written: `docker compose down` was run without
  `-v`, so the `mongo-data`, `rabbitmq-data` and `runner-output` volumes still
  hold data. Restart with `docker compose up -d --build`.
