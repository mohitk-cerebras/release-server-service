"""
Tests for server_factory command construction and lifecycle.

Validates that each server mode produces the correct command list,
environment variables, and handles edge cases properly.
"""

import os
import pytest

from release_server_service.core.server_factory import LocalServerHandle


class TestBuildReplicaCmd:
    """Tests for _build_replica_cmd."""

    def test_basic_replica_cmd(self):
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=8080,
            namespace="inf-integ",
            log_path="/tmp/server.log",
        )
        assert cmd[0] == "python"
        assert "--params" in cmd
        assert "/tmp/params.yaml" in cmd
        assert "--fastapi" in cmd
        assert "--port" in cmd
        assert "8080" in cmd
        assert "--mgmt_namespace" in cmd
        assert "inf-integ" in cmd
        assert "--mock_backend" not in cmd
        assert "--disable_version_check" not in cmd
        assert "--cbcore" not in cmd  # No cbcore flag without app_tag

    def test_replica_cmd_with_mock(self):
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=8080,
            namespace="inf-integ",
            log_path="/tmp/server.log",
            mock_backend=True,
        )
        assert "--mock_backend" in cmd

    def test_replica_cmd_with_disable_version_check(self):
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=8080,
            namespace="inf-integ",
            log_path="/tmp/server.log",
            disable_version_check=True,
        )
        assert "--disable_version_check" in cmd

    def test_replica_cmd_with_app_tag(self):
        """Test that --cbcore flag is added when app_tag is provided."""
        cmd = LocalServerHandle._build_replica_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=8080,
            namespace="inf-integ",
            log_path="/tmp/server.log",
            app_tag="260215-inference-202602201519-2373-9999f993",
        )
        assert "--cbcore" in cmd
        assert cmd[cmd.index("--cbcore") + 1].split(":")[-1] == "260215-inference-202602201519-2373-9999f993"


class TestBuildApiGatewayCmd:
    """Tests for _build_api_gateway_cmd."""

    def test_basic_gateway_cmd(self):
        cmd = LocalServerHandle._build_api_gateway_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=9090,
            namespace="inf-integ",
            log_path="/tmp/gw.log",
        )
        assert "--api_gateway" in cmd
        assert "--fastapi" in cmd
        assert "9090" in cmd
        assert "--mock_backend" not in cmd

    def test_gateway_cmd_with_mock(self):
        cmd = LocalServerHandle._build_api_gateway_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=9090,
            namespace="inf-integ",
            log_path="/tmp/gw.log",
            mock_backend=True,
        )
        assert "--api_gateway" in cmd
        assert "--mock_backend" in cmd


class TestBuildPlatformWorkloadCmd:
    """Tests for _build_platform_workload_cmd."""

    def test_basic_platform_cmd(self):
        cmd = LocalServerHandle._build_platform_workload_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=7070,
            namespace="inf-platform",
            log_path="/tmp/platform.log",
        )
        assert "--platform_workload" in cmd
        assert "--fastapi" in cmd
        assert "7070" in cmd
        assert "--mock_backend" not in cmd

    def test_platform_cmd_with_mock(self):
        cmd = LocalServerHandle._build_platform_workload_cmd(
            python_exec="python",
            params_path="/tmp/params.yaml",
            port=7070,
            namespace="inf-platform",
            log_path="/tmp/platform.log",
            mock_backend=True,
        )
        assert "--platform_workload" in cmd
        assert "--mock_backend" in cmd


class TestBuildCmdForMode:
    """Tests for the _build_cmd_for_mode dispatcher."""

    COMMON_KWARGS = dict(
        python_exec="python",
        params_path="/tmp/p.yaml",
        port=8080,
        namespace="ns",
        log_path="/tmp/log",
    )

    def test_replica_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="replica", **self.COMMON_KWARGS
        )
        assert "--fastapi" in cmd
        assert "--api_gateway" not in cmd
        assert "--platform_workload" not in cmd

    def test_replica_mock_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="replica_mock", mock_backend=True, **self.COMMON_KWARGS
        )
        assert "--mock_backend" in cmd

    def test_api_gateway_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="api_gateway", **self.COMMON_KWARGS
        )
        assert "--api_gateway" in cmd

    def test_api_gateway_mock_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="api_gateway_mock", mock_backend=True, **self.COMMON_KWARGS
        )
        assert "--api_gateway" in cmd
        assert "--mock_backend" in cmd

    def test_platform_workload_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="platform_workload", **self.COMMON_KWARGS
        )
        assert "--platform_workload" in cmd

    def test_platform_workload_mock_mode(self):
        cmd = LocalServerHandle._build_cmd_for_mode(
            mode="platform_workload_mock", mock_backend=True,
            **self.COMMON_KWARGS,
        )
        assert "--platform_workload" in cmd
        assert "--mock_backend" in cmd

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="No command builder"):
            LocalServerHandle._build_cmd_for_mode(
                mode="unknown_mode", **self.COMMON_KWARGS
            )


class TestFindFreePort:
    """Tests for _find_free_port."""

    def test_returns_valid_port(self):
        port = LocalServerHandle._find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535


class TestRedactEnv:
    """Tests for _redact_env helper."""

    def test_redacts_sensitive_keys(self):
        from release_server_service.core.server_factory import _redact_env

        env = {
            "MODEL_NAME": "llama",
            "AWS_SECRET_ACCESS_KEY": "supersecret",
            "AWS_SESSION_TOKEN": "tok123",
            "NAMESPACE": "inf-integ",
        }
        redacted = _redact_env(env)
        assert redacted["MODEL_NAME"] == "llama"
        assert redacted["AWS_SECRET_ACCESS_KEY"] == "***REDACTED***"
        assert redacted["AWS_SESSION_TOKEN"] == "***REDACTED***"
        assert redacted["NAMESPACE"] == "inf-integ"
