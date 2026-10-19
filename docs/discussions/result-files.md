# Discussion: result files (open)

Status: **open, awaiting decisions**. Started 2026-10-19; no code changed yet.
Once decided, record the outcome as new entries in `docs/DECISIONS.md` (next ID: D-32)
and mark this file resolved.

## Current behaviour (as implemented)

Each run writes two files:

| File | Contents | Measured size (60 s run) |
|---|---|---|
| `<run-id>-requests.jsonl` | One line per request, flushed per record (D-14) | ~280 KB, ~430 lines (~650 B/line) |
| `<run-id>-summary.json` | Counts, latency distributions, throughput, QoE, effective config | ~2 KB |

- **Single runs** write to `output/`.
- **Scheduled runners** write to `output/<device_id>/` (D-30).
- **In Docker**, that folder is the named volume `runner-output`, mounted at `/app/output` and shared by all runners.
- **Retention:** each runner keeps its newest `keep_output_runs` runs (default 96). That is about **27 MB and one day per router** at one 60 s run every 15 minutes. Single runs never delete anything.
- **Duplicate copy:** the same data is published to RabbitMQ (`request.measured`, `run.finished`) and stored in MongoDB.
- **Compression** (measured): gzip shrinks a requests file 8.4×, from 277 KB to 33 KB.

## Issues raised

1. **What are the files for, now that MongoDB exists?** Three possible roles:
   - (a) delivery backup / send queue (recommended, see 2);
   - (b) audit and offline analysis, i.e. raw data pulled off a router;
   - (c) none: drop them in Docker and rely on MongoDB.
2. **Gap: no re-sending after long outages.**
   - The publisher's in-memory buffer holds 10,000 events, about 23 runs.
   - If RabbitMQ is unreachable for longer, as a train might be, new events are dropped (D-23).
   - The data still exists in the files, but nothing ever re-sends it.
3. **Retention by count doesn't suit router storage.** A size cap (e.g. `max_output_mb`), optionally plus a maximum age, would be safer on small disks.
4. **Compression.** Gzipping each requests file once its run finishes would cut a day from about 27 MB to about 3 MB. The cost is that reading them needs `zcat`.
5. **Access.** Named volumes aren't visible on the host; today you'd use
   `docker compose cp runner-train-101:/app/output ./`. A bind mount
   (`./output:/app/output`) is simpler to browse, but raises file-ownership
   questions with the container's non-root user (uid 10001).
6. **No format version in file lines.** Messages carry `schema_version` (D-22), but JSONL lines and summaries don't. That matters if files are ever re-sent or analysed after the format changes.

## Recommendation (proposed, not agreed)

Keep the files and make them the reliable send queue (an "outbox"):

- **Delivered marker:** when RabbitMQ has confirmed every event of a run, write
  `<run-id>.delivered` (or rename the files).
- **Re-send:** on startup and on reconnect, re-send runs without the marker,
  oldest first. MongoDB writes are already safe to repeat (D-24), so re-sends
  can't create duplicates.
- **Retention never deletes an undelivered run**, unless the disk cap forces
  it. In that case, delete the oldest first and log it loudly.
- **Size-based retention:** `max_output_mb`, optionally with a maximum age.
- **Gzip** finished runs' requests files.
- **Format version:** add `schema_version` to JSONL lines and to the summary.

## Open questions for the user

1. Which role do you intend for the files: send queue, audit, or none?
2. How much disk does a target router have?
3. How long can a train realistically be offline?
4. Should the files be easy to reach from the host (bind mount), or is
   `docker compose cp` fine?

## Context for picking this up

- Relevant code:
  - `app/traffic_generator/scheduler.py`: `prune_outputs`, `device_output_dir`
  - `app/traffic_generator/publisher.py`: buffer, confirms, `published` / `undelivered` counters
  - `app/traffic_generator/main.py`: `run_one` writes the files before publishing
  - `app/traffic_generator/reporter.py`: `JsonlWriter`, `write_summary`
- Relevant decisions: D-14, D-22, D-23, D-24, D-30.
- Stack state when this was written: `docker compose down` was run without
  `-v`, so the `mongo-data`, `rabbitmq-data` and `runner-output` volumes still
  hold data. Restart with `docker compose up -d --build`.
