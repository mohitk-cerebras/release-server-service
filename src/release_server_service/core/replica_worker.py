#!/usr/bin/env python3
"""Replica worker process.

This script runs as an independent process to create and monitor a single replica.
It updates shared state via ReplicaStateManager and never crashes the REST server.

Usage:
    python replica_worker.py <replica_id> <request_file> <workdir>
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from release_server_service.core.cbclient_deployer import deploy_cbclient_env
from release_server_service.core.health import poll_health_endpoint, run_diagnostics
from release_server_service.core.server_factory import create_server_handle
from release_server_service.core.state_manager import ReplicaStateManager
from release_server_service.core.wheel_resolver import resolve_wheel_path
from release_server_service.models.requests import CreateReplicaRequest

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def install_wheel_packages(
    replica_id: str,
    venv_path: str,
    python_exec: str,
    app_tag: str,
    whl_path: str | None,
) -> None:
    """Install wheel packages into venv using traditional pip.

    NOTE: We ALWAYS use traditional pip for package installation to avoid TLS issues
    with uv's rustls when connecting to internal devpi server. Even though venv was
    created with 'uv venv', we install pip and use it directly for package installation.
    """
    # Convert app_tag to proper PEP 440 version format
    from release_server_service.core.wheel_resolver import get_whl_version_from_app_tag

    workload_version = get_whl_version_from_app_tag(app_tag)
    if not workload_version:
        # Fallback to old logic if conversion fails
        workload_version = app_tag.split("-")[0].replace(".", "")
        logger.warning(
            f"[{replica_id}] Could not convert app_tag to PEP 440 version, "
            f"using fallback: {workload_version}"
        )

    logger.info(f"[{replica_id}] Resolved workload version: {workload_version}")
    venv_pip = os.path.join(venv_path, "bin", "pip")

    logger.info(f"[{replica_id}] Using traditional pip for package installation (avoiding uv TLS issues)")

    # Ensure pip is installed in the venv (since uv venv doesn't install pip)
    if not os.path.exists(venv_pip):
        logger.info(f"[{replica_id}] Installing pip in venv...")
        ensure_pip_cmd = [python_exec, "-m", "ensurepip", "--default-pip"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *ensure_pip_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(f"[{replica_id}] Failed to install pip: {stderr.decode()}")
                raise RuntimeError(f"Failed to install pip: {stderr.decode()}")
            logger.info(f"[{replica_id}] pip installed successfully")
        except Exception as e:
            logger.error(f"[{replica_id}] Error installing pip: {e}")
            raise

    if not whl_path:
        # Usernode case: no local wheel - install from devpi
        logger.info(f"[{replica_id}] Installing packages from devpi (no local wheel)...")

        # Install api-server and compiler WITH dependencies
        api_server_req = f"cerebras-inference-api-server=={workload_version}"
        compiler_req = f"cerebras-inference-compiler=={workload_version}"

        cmd_with_deps = [
            venv_pip,
            "install",
            "--index-url",
            "https://devpi.cerebras.aws/root/main/+simple/",
            api_server_req,
            compiler_req,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_with_deps,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[{replica_id}] Failed to install api-server/compiler: {stderr.decode()}")
            raise RuntimeError(f"Package installation failed: {stderr.decode()}")

        # Install appliance and workload with --no-deps
        appliance_req = f"cerebras_appliance=={workload_version}"
        workload_req = f"cerebras_inference_workload=={workload_version}"

        cmd_no_deps = [
            venv_pip,
            "install",
            "--no-deps",
            "--index-url",
            "https://devpi.cerebras.aws/root/main/+simple/",
            appliance_req,
            workload_req,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_no_deps,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[{replica_id}] Failed to install appliance/workload: {stderr.decode()}")
            raise RuntimeError(f"Package installation failed: {stderr.decode()}")

        logger.info(f"[{replica_id}] Installed packages from devpi")
    else:
        # Dev machine: install local wheel with --no-deps
        logger.info(f"[{replica_id}] Installing local wheel: {whl_path}")

        cmd = [venv_pip, "install", "--no-deps", whl_path]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[{replica_id}] Failed to install wheel: {stderr.decode()}")
            raise RuntimeError(f"Wheel installation failed: {stderr.decode()}")

        logger.info(f"[{replica_id}] Installed local wheel")


def _make_external_endpoint(base_url: str) -> str:
    """Convert base_url with 127.0.0.1 to use host FQDN for external access.

    Args:
        base_url: Base URL (e.g., "http://127.0.0.1:54137")

    Returns:
        External URL with FQDN (e.g., "http://hostname.domain:54137")
    """
    if not base_url:
        return "NA"

    # If base_url uses 127.0.0.1 or localhost, replace with FQDN
    if "127.0.0.1" in base_url or "localhost" in base_url:
        import socket
        try:
            # Get fully qualified domain name
            fqdn = socket.getfqdn()
            # Replace 127.0.0.1 or localhost with FQDN
            external_url = base_url.replace("127.0.0.1", fqdn).replace("localhost", fqdn)
            logger.debug(f"Converted endpoint: {base_url} -> {external_url}")
            return external_url
        except Exception as e:
            logger.warning(f"Failed to get FQDN, using base_url as-is: {e}")
            return base_url

    # Already uses external hostname
    return base_url


def _resolve_cerebras_pytorch_wheel(app_tag: str) -> Optional[str]:
    """Resolve cerebras-pytorch wheel path from app_tag.

    Similar to resolve_wheel_path() but for cerebras-pytorch package.

    Args:
        app_tag: Application tag (e.g., '260113.3-inference-202602220136-2384-8fb6d540')

    Returns:
        Full path to cerebras-pytorch wheel, or None if not found
    """
    from release_server_service.core.wheel_resolver import get_whl_version_from_app_tag

    # Get version from app_tag (e.g., "260113.3+inference.202602220136.2384.8fb6d540")
    version = get_whl_version_from_app_tag(app_tag)
    if not version:
        logger.warning(f"Cannot resolve version from app_tag: {app_tag}")
        return None

    # Construct wheel filename for cerebras-pytorch
    # Format: cerebras_pytorch-{version}-{py_tag}-{abi_tag}-{platform}.whl
    import sys
    import glob
    from pathlib import Path

    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    platform = "linux_x86_64"

    # Extract base version (before the '+')
    base_version = version.split("+")[0] if "+" in version else version

    logger.info(f"Searching for cerebras-pytorch wheel: version={version}, base={base_version}")

    # Search in common locations
    search_paths = [
        Path("/cb/artifacts/builds/cbcore"),
        Path("/cb/artifacts/release-stage"),
    ]

    # Try multiple patterns to handle both PEP 427 normalized (+ → .) and non-normalized formats
    patterns_to_try = [
        # Pattern 1: PEP 427 normalized format (+ → .)
        f"cerebras_pytorch-{version.replace('+', '.')}-{py_tag}-{py_tag}-{platform}.whl",
        # Pattern 2: Non-normalized format (keep +)
        f"cerebras_pytorch-{version}-{py_tag}-{py_tag}-{platform}.whl",
        # Pattern 3: Wildcard for any local version format
        f"cerebras_pytorch-{base_version}*-{py_tag}-{py_tag}-{platform}.whl",
    ]

    for base_path in search_paths:
        if not base_path.exists():
            logger.debug(f"Search path does not exist: {base_path}")
            continue

        for pattern in patterns_to_try:
            # Search with glob pattern recursively
            search_pattern = str(base_path / "**" / pattern)
            logger.debug(f"Searching: {search_pattern}")
            matches = glob.glob(search_pattern, recursive=True)

            if matches:
                # Filter to match the full version if we used wildcard
                if "*" in pattern:
                    filtered = [
                        m for m in matches
                        if version.replace("+", ".") in m or version in m
                    ]
                    matches = filtered if filtered else matches

                if matches:
                    # Sort to get most recent
                    matches = sorted(matches, reverse=True)
                    logger.info(f"Found cerebras-pytorch wheel: {matches[0]}")
                    return matches[0]

    logger.warning(f"cerebras-pytorch wheel not found for version: {version}")
    return None


async def main(replica_id: str, request_file: str, workdir: str):
    """Main worker function."""
    # Derive state_dir from workdir parent directory
    # workdir = /n0/lab/test/dh1/ec2a2cb2-f63 → state_dir = /n0/lab/test/dh1/state
    workdir_root = os.path.dirname(workdir)
    state_dir = os.path.join(workdir_root, "state")
    state_mgr = ReplicaStateManager(state_dir=state_dir)

    try:
        # Update state: starting worker
        state_mgr.update_replica_state(
            replica_id, {"status": "creating", "worker_started_at": datetime.now(timezone.utc).isoformat()}
        )

        # Load request
        logger.info(f"[{replica_id}] Loading request from {request_file}")
        with open(request_file, "r") as f:
            request_dict = json.load(f)

        request = CreateReplicaRequest(**request_dict)

        # ── STEP 1: Deploy cbclient environment ─────────────
        cbclient_config = request.cbclient_config
        if not cbclient_config and request.placement.app_tag:
            logger.info(f"[{replica_id}] Auto-creating cbclient_config from app_tag")
            from release_server_service.models.requests import CBClientConfig

            # Resolve cerebras-pytorch wheel path from app_tag
            cbclient_whl = _resolve_cerebras_pytorch_wheel(request.placement.app_tag)
            if cbclient_whl:
                logger.info(f"[{replica_id}] Resolved cerebras-pytorch wheel: {cbclient_whl}")

            cbclient_config = CBClientConfig(
                app_tag=request.placement.app_tag,
                cbclient_whl=cbclient_whl,  # Pass the wheel path
                use_uv=True
            )

        venv_path = None
        python_exec = None

        if cbclient_config:
            logger.info(f"[{replica_id}] Deploying cbclient environment...")
            deploy_result = await deploy_cbclient_env(
                workdir=workdir,
                app_tag=cbclient_config.app_tag,
                cbclient_whl=cbclient_config.cbclient_whl,
                client_version=cbclient_config.client_version,
                modelzoo_branch=cbclient_config.modelzoo_branch,
                custom_requirements=cbclient_config.custom_requirements,
                use_uv=cbclient_config.use_uv,
            )
            venv_path = deploy_result.venv_path
            python_exec = deploy_result.python_exec

            # Verify venv exists
            if not os.path.exists(venv_path):
                error_msg = f"Venv not created at {venv_path}"
                logger.error(f"[{replica_id}] {error_msg}")
                raise RuntimeError(error_msg)

            if not os.path.exists(python_exec):
                error_msg = f"Python executable not found at {python_exec}"
                logger.error(f"[{replica_id}] {error_msg}")
                raise RuntimeError(error_msg)

            logger.info(f"[{replica_id}] Venv verified: {venv_path}, python: {python_exec}")

            state_mgr.update_replica_state(
                replica_id,
                {
                    "venv_path": venv_path,
                    "python_exec": python_exec,
                },
            )

            # Install wheel packages
            app_tag = request.placement.app_tag or cbclient_config.app_tag
            if app_tag:
                whl_path = resolve_wheel_path(app_tag)
                await install_wheel_packages(
                    replica_id, venv_path, python_exec, app_tag, whl_path
                )

        # ── STEP 2: Create server handle ────────────────────
        logger.info(f"[{replica_id}] Creating server handle...")
        state_mgr.update_replica_state(replica_id, {"status": "starting"})

        server_handle = await create_server_handle(
            request=request,
            local_workdir=workdir,
        )

        base_url = getattr(server_handle, "base_url", None)
        port = getattr(server_handle, "port", None)
        replica_pid = getattr(server_handle._process, "pid", None) if hasattr(server_handle, "_process") else None

        state_mgr.update_replica_state(
            replica_id,
            {
                "base_url": base_url,
                "port": port,
                "replica_pid": replica_pid,
            },
        )

        logger.info(f"[{replica_id}] Server handle created: {base_url}")

        # ── STEP 3: Wait for readiness ──────────────────────
        if request.wait_for_ready and base_url:
            logger.info(f"[{replica_id}] Waiting for server readiness...")
            state_mgr.update_replica_state(replica_id, {"status": "waiting_for_ready"})

            timeout = 1800  # 30 minutes default
            poll_interval = 5

            is_healthy = await poll_health_endpoint(
                base_url=base_url,
                timeout_s=timeout,
                poll_interval_s=poll_interval,
                pid=replica_pid,
            )

            if is_healthy:
                state_mgr.update_replica_state(
                    replica_id,
                    {
                        "status": "ready",
                        "display_status": "Active",
                        "endpoint": _make_external_endpoint(base_url),
                        "ready_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                logger.info(f"[{replica_id}] Server ready")
            else:
                state_mgr.update_replica_state(
                    replica_id,
                    {
                        "status": "unhealthy",
                        "display_status": "Failed",
                        "endpoint": _make_external_endpoint(base_url),
                        "error_message": "Health check failed within timeout",
                    },
                )
                logger.error(f"[{replica_id}] Health check failed")
                sys.exit(1)
        else:
            # No wait_for_ready or no base_url
            state_mgr.update_replica_state(
                replica_id,
                {
                    "status": "ready",
                    "display_status": "Active",
                    "endpoint": _make_external_endpoint(base_url) if base_url else "NA",
                    "ready_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        # ── STEP 4: Run diagnostics (optional) ──────────────
        if request.run_diagnostics and base_url:
            logger.info(f"[{replica_id}] Running diagnostics...")
            try:
                diagnostics = await run_diagnostics(base_url)
                state_mgr.update_replica_state(
                    replica_id, {"diagnostics": diagnostics}
                )
                logger.info(f"[{replica_id}] Diagnostics complete")
            except Exception as e:
                logger.warning(f"[{replica_id}] Diagnostics failed: {e}")

        logger.info(f"[{replica_id}] Worker completed successfully")
        sys.exit(0)

    except Exception as e:
        logger.exception(f"[{replica_id}] Worker failed: {e}")
        state_mgr.update_replica_state(
            replica_id,
            {
                "status": "failed",
                "display_status": "Failed",
                "endpoint": "NA",
                "error_message": str(e),
            },
        )
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: replica_worker.py <replica_id> <request_file> <workdir>")
        sys.exit(1)

    replica_id = sys.argv[1]
    request_file = sys.argv[2]
    workdir = sys.argv[3]

    asyncio.run(main(replica_id, request_file, workdir))
