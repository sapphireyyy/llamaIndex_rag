from fastapi.testclient import TestClient


def test_health_endpoints_and_correlation_header(client: TestClient) -> None:
    live = client.get("/health/live", headers={"X-Correlation-ID": "test-correlation"})
    assert live.status_code == 200
    assert live.headers["X-Correlation-ID"] == "test-correlation"
    assert live.json()["status"] == "ok"

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert all(ready.json()["checks"].values())


def test_unexpected_errors_are_sanitized(client: TestClient) -> None:
    response = client.get("/api/v1/audit-events")
    assert response.status_code in {200, 403, 404}
    assert "traceback" not in response.text.lower()
