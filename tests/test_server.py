import zlib

from fastapi.testclient import TestClient

from app.limits import MAX_PAYLOAD_KB, TEST_ENDPOINT_HEADER
from app.test_server.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert TEST_ENDPOINT_HEADER in response.headers


def test_page_returns_requested_size() -> None:
    response = client.get("/page", params={"size_kb": 64})
    assert response.status_code == 200
    assert len(response.content) == 64 * 1024
    assert response.headers["cache-control"] == "no-store"


def test_page_payload_is_not_compressible() -> None:
    body = client.get("/page", params={"size_kb": 64}).content
    assert len(zlib.compress(body)) > 0.95 * len(body)


def test_page_custom_status_code() -> None:
    assert client.get("/page", params={"size_kb": 1, "status_code": 503}).status_code == 503


def test_server_timing_includes_delay() -> None:
    response = client.get("/page", params={"size_kb": 1, "delay_ms": 50})
    server_ms = float(response.headers["server-timing"].split("dur=")[1])
    assert server_ms >= 50


def test_page_rejects_out_of_range_parameters() -> None:
    assert client.get("/page", params={"size_kb": MAX_PAYLOAD_KB + 1}).status_code == 422
    assert client.get("/page", params={"size_kb": -1}).status_code == 422
    assert client.get("/page", params={"delay_ms": 10**9}).status_code == 422
    assert client.get("/page", params={"status_code": 99}).status_code == 422


def test_failure_endpoint() -> None:
    response = client.get("/failure", params={"status_code": 502})
    assert response.status_code == 502
    assert response.json() == {"error": "simulated failure"}
    assert client.get("/failure", params={"status_code": 200}).status_code == 422
