"""Controllable HTTP endpoint that stands in for a remote internet service.

Run with:
    uv run uvicorn app.test_server.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from app.limits import MAX_DELAY_MS, MAX_PAYLOAD_KB, TEST_ENDPOINT_HEADER, TEST_ENDPOINT_VERSION

# Generated once at startup so per-request cost is a slice, not data generation
# (D-03). Random bytes are incompressible, so a compressing middlebox cannot
# shrink them and inflate measured throughput (D-07).
PAYLOAD = os.urandom(MAX_PAYLOAD_KB * 1024)

app = FastAPI(title="Onboard QoE Test Endpoint", version=TEST_ENDPOINT_VERSION)


@app.middleware("http")
async def add_measurement_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    # Time spent producing the response, including any artificial delay. The
    # generator subtracts it from TTFB to isolate the network share (D-01).
    server_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={server_ms:.3f}"
    response.headers["Cache-Control"] = "no-store"
    response.headers[TEST_ENDPOINT_HEADER] = TEST_ENDPOINT_VERSION
    return response


async def _delay(delay_ms: int) -> None:
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/page")
async def page(
    delay_ms: int = Query(0, ge=0, le=MAX_DELAY_MS),
    size_kb: int = Query(100, ge=0, le=MAX_PAYLOAD_KB),
    status_code: int = Query(200, ge=200, le=599),
) -> Response:
    await _delay(delay_ms)
    return Response(
        content=PAYLOAD[: size_kb * 1024],
        status_code=status_code,
        media_type="application/octet-stream",
    )


@app.get("/failure")
async def failure(
    status_code: int = Query(500, ge=400, le=599),
    delay_ms: int = Query(0, ge=0, le=MAX_DELAY_MS),
) -> JSONResponse:
    await _delay(delay_ms)
    return JSONResponse({"error": "simulated failure"}, status_code=status_code)
