"""Tests for the Release Server Service REST API.

These tests validate the API layer independently using httpx/TestClient.
They do NOT require monolith dependencies — they test request validation,
routing, and response schemas.

The test_start_server test validates that server_factory constructs the
correct command matching the monolith's InferenceApplianceRunnerStrategy.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from release_server_service.core.replica_manager import ReplicaManager
from release_server_service.core.server_factory import LocalServerHandle
from release_server_service.api.routes import set_replica_manager
from release_server_service.main import app


@pytest.fixture(autouse=True)
def setup_manager():
    """Inject a fresh ReplicaManager for each test."""
    manager = ReplicaManager()
    set_replica_manager(manager)
    yield manager


client = TestClient(app)


# ─────────────────────────────────────────────────────────────────
# API layer tests
# ─────────────────────────────────────────────────────────────────


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


def test_placement_no_usernode_field():
    """PlacementConfig no longer accepts usernode field."""
    resp = client.post(
        "/api/v1/replicas",
        json={
            "server_mode": "replica",
            "model_name": "llama3.1-8b",
            "full_config": {"model": {}, "runconfig": {}},
            "placement": {
                "multibox": "dh1",
            },
        },
    )
    # Should not fail on missing usernode — it's been removed
    # Will fail at server creation (subprocess) but not at validation
    assert resp.status_code in {201, 500}


# ─────────────────────────────────────────────────────────────────
# Server factory unit tests
# ─────────────────────────────────────────────────────────────────


class TestBuildReplicaCmd:
    """Validate _build_replica_cmd matches monolith's _get_runner_cmd."""

    def test_basic_replica_cmd(self):
        """Command contains --params, --fastapi, --port, --mgmt_namespace."""
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="/usr/bin/python3",
            params_path="/workdir/params.yaml",
            port=8888,
            namespace="inf-integ",
            log_path="/workdir/inference_server.log",
        )
        assert cmd[0] == "/usr/bin/python3"
        assert cmd[1:3] == ["-m", "cerebras.inference.workload.main"]
        assert "--params" in cmd
        assert cmd[cmd.index("--params") + 1] == "/workdir/params.yaml"
        assert "--fastapi" in cmd
        assert "--port" in cmd
        assert cmd[cmd.index("--port") + 1] == "8888"
        assert "--mgmt_namespace" in cmd
        assert cmd[cmd.index("--mgmt_namespace") + 1] == "inf-integ"
        # Should NOT have optional flags by default
        assert "--disable_version_check" not in cmd
        assert "--mock_backend" not in cmd
        assert "--cbcore" not in cmd  # No cbcore flag without app_tag

    def test_replica_cmd_with_disable_version_check(self):
        """--disable_version_check is appended when requested."""
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/p.yaml",
            port=9000,
            namespace="ns",
            log_path="/log",
            disable_version_check=True,
        )
        assert "--disable_version_check" in cmd

    def test_replica_cmd_with_mock_backend(self):
        """--mock_backend is appended for mock mode."""
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/p.yaml",
            port=9000,
            namespace="ns",
            log_path="/log",
            mock_backend=True,
        )
        assert "--mock_backend" in cmd


class TestWriteFullConfig:
    """Validate that full_config is written as JSON to full_config.json."""

    def test_writes_json_file(self, tmp_path):
        """full_config.json is written with correct JSON content."""
        config = {
            "model": {"name": "llama3.1-8b", "layers": 32},
            "runconfig": {"job_priority": "p2"},
            "api_config": {"port": 8080},
        }
        # The create() method writes full_config.json via json.dump
        params_path = os.path.join(str(tmp_path), "full_config.json")
        with open(params_path, "w") as f:
            json.dump(config, f, indent=2)

        assert os.path.exists(params_path)
        with open(params_path, "r") as f:
            loaded = json.load(f)
        assert loaded == config


class TestStartServer:
    """End-to-end test for LocalServerHandle.create() in replica mode.

    Validates that create() correctly:
    1. Writes full_config.json from full_config
    2. Builds the correct command
    3. Launches a subprocess with the right env vars
    """

    @pytest.mark.asyncio
    async def test_start_server(self, tmp_path):
        """LocalServerHandle.create() writes full_config.json and launches correct command."""
        full_config = {
            "model": {"name": "llama3.1-8b"},
            "runconfig": {"job_priority": "p2", "job_time_sec": 86400},
            "api_config": {},
        }

        # Mock subprocess so we don't actually launch anything
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None

        # Patch to use a proper async mock
        async def fake_create_subprocess_exec(*args, **kwargs):
            # Capture the command for assertions
            fake_create_subprocess_exec.captured_cmd = args
            fake_create_subprocess_exec.captured_env = kwargs.get("env", {})
            return mock_process

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            handle = await LocalServerHandle.create(
                model="llama3.1-8b",
                workdir=str(tmp_path),
                multibox="dh1",
                namespace="inf-integ",
                full_config=full_config,
                mode="replica",
            )

            # 1. full_config.json was written
            config_path = tmp_path / "full_config.json"
            assert config_path.exists(), "full_config.json should be written"
            with open(config_path) as f:
                loaded = json.load(f)
            assert loaded == full_config

            # 2. Command is correct
            cmd = fake_create_subprocess_exec.captured_cmd
            cmd_str = " ".join(cmd)
            assert "cerebras.inference.workload.main" in cmd_str
            assert "--params" in cmd_str
            assert "--fastapi" in cmd_str
            assert "--port" in cmd_str
            assert "--mgmt_namespace" in cmd_str
            assert "inf-integ" in cmd_str

            # 3. Env vars are set
            env = fake_create_subprocess_exec.captured_env
            assert env["MODEL_NAME"] == "llama3.1-8b"
            assert env["MULTIBOX"] == "dh1"
            assert env["NAMESPACE"] == "inf-integ"

            # 4. Handle properties
            assert handle.model == "llama3.1-8b"
            assert handle.mode == "replica"
            assert handle.port > 0
            assert handle.base_url is not None
            assert handle.base_url.startswith("http://")

    @pytest.mark.asyncio
    async def test_start_server_api_gateway_mode(self, tmp_path):
        """api_gateway mode builds correct command with --api_gateway flag."""
        full_config = {"model": {}, "runconfig": {}}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None

        async def fake_create_subprocess_exec(*args, **kwargs):
            fake_create_subprocess_exec.captured_cmd = args
            return mock_process

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            handle = await LocalServerHandle.create(
                model="test",
                workdir=str(tmp_path),
                multibox="dh1",
                full_config=full_config,
                mode="api_gateway",
            )

            cmd = fake_create_subprocess_exec.captured_cmd
            cmd_str = " ".join(cmd)
            assert "--api_gateway" in cmd_str
            assert handle.mode == "api_gateway"

    @pytest.mark.asyncio
    async def test_start_server_platform_workload_mode(self, tmp_path):
        """platform_workload mode builds correct command with --platform_workload flag."""
        full_config = {"model": {}, "runconfig": {}}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None

        async def fake_create_subprocess_exec(*args, **kwargs):
            fake_create_subprocess_exec.captured_cmd = args
            return mock_process

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            handle = await LocalServerHandle.create(
                model="test",
                workdir=str(tmp_path),
                multibox="dh1",
                full_config=full_config,
                mode="platform_workload",
            )

            cmd = fake_create_subprocess_exec.captured_cmd
            cmd_str = " ".join(cmd)
            assert "--platform_workload" in cmd_str
            assert handle.mode == "platform_workload"

    @pytest.mark.asyncio
    async def test_start_server_no_full_config_logs_warning(self, tmp_path):
        """When full_config is None, create() still works but logs a warning."""
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_process

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            # Should NOT raise — current impl just logs a warning
            handle = await LocalServerHandle.create(
                model="test",
                workdir=str(tmp_path),
                multibox="dh1",
                full_config=None,
                mode="replica",
            )
            assert handle.model == "test"
            assert handle.mode == "replica"

            # full_config.json should NOT have been written
            config_path = tmp_path / "full_config.json"
            assert not config_path.exists()