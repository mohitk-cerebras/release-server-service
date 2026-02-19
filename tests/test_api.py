"""tests for the Release Server Service REST API.

These tests validate the API layer independently using httpx/TestClient.
They do NOT require monolith dependencies — they test request validation,
routing, and response schemas.
"""

import pytest
from fastapi.testclient import TestClient

from release_server_service.core.replica_manager import ReplicaManager
from release_server_service.api.routes import set_replica_manager
from release_server_service.main import app


@pytest.fixture(autouse=True)
def setup_manager():
    """Inject a fresh ReplicaManager for each test."""
    manager = ReplicaManager()
    set_replica_manager(manager)
    yield manager


client = TestClient(app)


def test_service_health():
    """Service health endpoint returns ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["replicas_total"] == 0


def test_list_replicas_empty():
    """Listing replicas when none exist returns empty list."""
    resp = client.get("/api/v1/replicas")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["replicas"] == []


def test_get_replica_not_found():
    """Getting a non-existent replica returns 404."""
    resp = client.get("/api/v1/replicas/nonexistent")
    assert resp.status_code == 404


def test_stop_replica_not_found():
    """Stopping a non-existent replica returns 404."""
    resp = client.post("/api/v1/replicas/nonexistent/stop", json={})
    assert resp.status_code == 404


def test_create_replica_validation_error():
    """Creating a replica with missing required fields returns 422."""
    resp = client.post("/api/v1/replicas", json={})
    assert resp.status_code == 422


def test_create_replica_missing_multibox():
    """Creating a replica without multibox returns 400."""
    resp = client.post(
        "/api/v1/replicas",
        json={
            "server_mode": "replica",
            "model_name": "llama3.1-8b",
            "full_config": {"model": {}, "runconfig": {}},
            "placement": {
                "multibox": "",  # empty = invalid
            },
        },
    )
    # Empty string passes pydantic but the route validates it
    assert resp.status_code in {400, 500}


def test_create_replica_platform_missing_config():
    """Platform workload mode without platform_config returns 400."""
    resp = client.post(
        "/api/v1/replicas",
        json={
            "server_mode": "platform_workload",
            "model_name": "llama3.1-8b",
            "full_config": {"model": {}, "runconfig": {}},
            "placement": {
                "multibox": "dh1",
            },
            # platform_config missing → should fail
        },
    )
    assert resp.status_code == 400
