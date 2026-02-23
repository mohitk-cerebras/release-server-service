
"""Replica Manager — tracks the lifecycle of all managed replicas.

This is the core state manager. It:
- Accepts creation requests from the REST API
- Delegates to server_factory to create the actual server handle
- Tracks all replicas by ID with full status history
- Provides status queries
- Handles stop/cleanup

Multiple concurrent REST calls are supported — each gets its own
replica_id and is tracked independently.
"""

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from release_server_service.config import ServiceConfig, get_config
from release_server_service.core.cbclient_deployer import deploy_cbclient_env
from release_server_service.core.health import poll_health_endpoint, run_diagnostics
from release_server_service.core.server_factory import create_server_handle
from release_server_service.core.wheel_resolver import resolve_wheel_path
from release_server_service.models.requests import CreateReplicaRequest
from release_server_service.models.responses import ReplicaInfo, ReplicaStatus

logger = logging.getLogger(__name__)


class ManagedReplica:
    """Internal representation of a managed replica."""

    def __init__(
        self,
        replica_id: str,
        request: CreateReplicaRequest,
        workdir: str,
    ):
        self.replica_id = replica_id
        self.request = request
        self.workdir = workdir
        self.venv_path: Optional[str] = None
        self.python_exec: Optional[str] = None
        self.status = ReplicaStatus.PENDING
        self.server_handle: Any = None  # LocalServerHandle once created
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.ready_at: Optional[datetime] = None
        self.error_message: Optional[str] = None
        self.diagnostics: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None

    def _set_status(self, status: ReplicaStatus, error: Optional[str] = None):
        self.status = status
        self.updated_at = datetime.now(timezone.utc)
        if error:
            self.error_message = error

    def to_info(self) -> ReplicaInfo:
        base_url = None
        host = None
        port = None

        if self.server_handle:
            base_url = getattr(self.server_handle, "base_url", None)
            host = getattr(self.server_handle, "host", None)
            port = getattr(self.server_handle, "port", None)

        # Map status to display_status
        if self.status == ReplicaStatus.READY:
            display_status = "Active"
        elif self.status in {
            ReplicaStatus.FAILED,
            ReplicaStatus.ERROR,
            ReplicaStatus.UNHEALTHY,
            ReplicaStatus.STOPPED,
        }:
            display_status = "Failed"
        else:
            # PENDING, CREATING, STARTING, WAITING_FOR_READY, STOPPING
            display_status = "Pending"

        # Endpoint is base_url when Active, "NA" otherwise
        endpoint = base_url if self.status == ReplicaStatus.READY and base_url else "NA"

        return ReplicaInfo(
            replica_id=self.replica_id,
            server_mode=self.request.server_mode.value,
            model_name=self.request.model_name,
            status=self.status,
            display_status=display_status,
            endpoint=endpoint,
            base_url=base_url,
            host=host,
            port=port,
            multibox=self.request.placement.multibox,
            namespace=self.request.placement.namespace,
            workdir=self.workdir,
            venv_path=self.venv_path,
            created_at=self.created_at,
            updated_at=self.updated_at,
            ready_at=self.ready_at,
            error_message=self.error_message,
            request_id=self.request.request_id,
            diagnostics=self.diagnostics,
            metadata={
                "server_mode": self.request.server_mode.value,
                "mock": self.request.server_mode.is_mock,
                "wait_for_ready": self.request.wait_for_ready,
                "venv_path": self.venv_path,
                "python_exec": self.python_exec,
            },
        )


class ReplicaManager:
    """
    Manages the lifecycle of multiple server replicas.

    Thread-safe for concurrent REST calls via asyncio locks.
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        self._config = config or get_config()
        self._replicas: Dict[str, ManagedReplica] = {}
        self._workdir_map: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def replicas(self) -> Dict[str, ManagedReplica]:
        return self._replicas

    def get_workdir(self, replica_id: str) -> Optional[str]:
        """Quick lookup of workdir for a given replica_id."""
        return self._workdir_map.get(replica_id)

    async def create_replica(
        self, request: CreateReplicaRequest
    ) -> ManagedReplica:
        """
        Create a new replica.

        Steps:
        1. Create replica ID
        2. Create local workdir
        3. Deploy cbclient environment (or bare venv fallback)
           3b. Install client wheel from app_tag (placement or cbclient_config)
        4. Register in workdir map
        5. Log summary, then proceed to server creation

        The method runs synchronously if wait_for_ready=True, or
        kicks off a background task if wait_for_ready=False.

        Args:
            request: Full replica creation request

        Returns:
            ManagedReplica instance
        """
        # ── STEP 1: Create replica ID ──────────────────────────
        replica_id = str(uuid.uuid4())[:12]
        logger.info(f"[{replica_id}] STEP 1/5: Created replica ID")

        # ── STEP 2: Create workdir ─────────────────────────────
        local_workdir = os.path.join(
            self._config.local_workdir_root, replica_id
        )
        os.makedirs(local_workdir, exist_ok=True)
        logger.info(f"[{replica_id}] STEP 2/5: Created workdir at {local_workdir}")

        replica = ManagedReplica(
            replica_id=replica_id,
            request=request,
            workdir=local_workdir,
        )

        # ── STEP 3: Deploy cbclient environment ─────────────────
        try:
            # Auto-create cbclient_config if placement.app_tag is provided but cbclient_config is not
            cbclient_config = request.cbclient_config
            if not cbclient_config and request.placement.app_tag:
                logger.info(
                    f"[{replica_id}] No cbclient_config provided, but placement.app_tag='{request.placement.app_tag}' "
                    f"found. Auto-creating cbclient_config..."
                )
                from release_server_service.models.requests import CBClientConfig
                cbclient_config = CBClientConfig(
                    app_tag=request.placement.app_tag,
                    use_uv=True
                )

            if cbclient_config:
                logger.info(
                    f"[{replica_id}] STEP 3/5: Deploying cbclient environment..."
                )
                deploy_result = await deploy_cbclient_env(
                    workdir=local_workdir,
                    app_tag=cbclient_config.app_tag,
                    cbclient_whl=cbclient_config.cbclient_whl,
                    client_version=cbclient_config.client_version,
                    modelzoo_branch=cbclient_config.modelzoo_branch,
                    custom_requirements=cbclient_config.custom_requirements,
                    use_uv=cbclient_config.use_uv,
                )
                replica.venv_path = deploy_result.venv_path
                replica.python_exec = deploy_result.python_exec
                logger.info(
                    f"[{replica_id}] STEP 3/5: CBClient env deployed: "
                    f"venv={deploy_result.venv_path}, python={deploy_result.python_exec}"
                )
                # STEP 3b: Install client wheel into venv ─────────────
                # Check both placement.app_tag and cbclient_config.app_tag
                app_tag = (
                    request.placement.app_tag
                    or cbclient_config.app_tag
                )
                if app_tag:
                    logger.info(
                        f"[{replica_id}] STEP 3b: Resolving wheel for app_tag='{app_tag}'..."
                    )
                    whl_path = resolve_wheel_path(app_tag)
                    if not whl_path:
                        logger.warning(
                            f"[{replica_id}] Wheel not found locally for app_tag='{app_tag}'. "
                            f"Will install cerebras_appliance from devpi in STEP 3c."
                        )

                    # Install wheel into the replica's venv (if found locally)
                    # Check if uv was used to create the venv
                    use_uv = cbclient_config.use_uv

                    try:
                        # Use the venv's pip directly (not uv pip) to avoid version compatibility issues
                        venv_pip = os.path.join(deploy_result.venv_path, "bin", "pip")

                        # Check if pip exists in the venv, if not install it first
                        if not os.path.exists(venv_pip):
                            logger.debug(f"[{replica_id}] pip not found in venv, installing it first...")
                            install_pip_cmd = [deploy_result.python_exec, "-m", "ensurepip", "--default-pip"]
                            pip_install_proc = await asyncio.create_subprocess_exec(
                                *install_pip_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await pip_install_proc.communicate()

                        # Install essential packages needed for inference server
                        # Include uvicorn and other dependencies that cerebras-inference-api-server needs
                        logger.debug(f"[{replica_id}] Installing essential packages (pip, setuptools, wheel, uvicorn, fastapi, pyyaml)...")
                        setup_cmd = [
                            deploy_result.python_exec,
                            "-m", "pip", "install",
                            "--upgrade", "pip", "setuptools", "wheel",
                            "uvicorn", "fastapi", "pydantic", "starlette", "httpx", "pyyaml",
                            "--extra-index-url", "https://devpi.cerebras.aws/root/main/+simple/",
                            "--trusted-host", "devpi.cerebras.aws",
                            "--no-warn-script-location"
                        ]
                        setup_proc = await asyncio.create_subprocess_exec(
                            *setup_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await setup_proc.communicate()

                        if whl_path:
                            logger.info(
                                f"[{replica_id}] STEP 3b: Installing {whl_path} into venv..."
                            )

                            # Workaround: Since pip checks platform tags even with --no-deps,
                            # we'll extract the wheel and install from the directory instead.
                            # This bypasses the platform compatibility check for pure Python wheels.
                            import zipfile
                            import tempfile
                            import shutil

                            extract_dir = os.path.join(replica.workdir, "wheel_extract")
                            os.makedirs(extract_dir, exist_ok=True)

                            try:
                                # Extract the wheel
                                logger.debug(f"[{replica_id}] Extracting wheel to {extract_dir}")
                                with zipfile.ZipFile(whl_path, 'r') as zip_ref:
                                    zip_ref.extractall(extract_dir)

                                # Extract dependencies from wheel metadata and install them first
                                # This mimics what the monolith does by creating requirements.txt
                                requirements_from_wheel = []
                                for item in os.listdir(extract_dir):
                                    if item.endswith('.dist-info'):
                                        metadata_file = os.path.join(extract_dir, item, 'METADATA')
                                        if os.path.exists(metadata_file):
                                            logger.debug(f"[{replica_id}] Reading dependencies from {metadata_file}")
                                            with open(metadata_file, 'r') as f:
                                                for line in f:
                                                    # Parse "Requires-Dist:" lines from METADATA
                                                    if line.startswith('Requires-Dist:'):
                                                        # Format: "Requires-Dist: package-name (>=version) ; extra"
                                                        dep = line.split(':', 1)[1].strip()
                                                        # Remove environment markers (after semicolon)
                                                        if ';' in dep:
                                                            dep = dep.split(';')[0].strip()
                                                        if dep:
                                                            requirements_from_wheel.append(dep)
                                        break

                                # Install dependencies if found
                                if requirements_from_wheel:
                                    logger.info(
                                        f"[{replica_id}] Installing {len(requirements_from_wheel)} dependencies from wheel metadata"
                                    )
                                    # Write requirements to file
                                    req_file = os.path.join(replica.workdir, "wheel_requirements.txt")
                                    with open(req_file, 'w') as f:
                                        for req in requirements_from_wheel:
                                            f.write(req + '\n')
                                            logger.debug(f"[{replica_id}]   - {req}")

                                    # Install requirements using pip with Cerebras internal devpi index
                                    # Use --extra-index-url to check both devpi and PyPI
                                    pip_cmd = [
                                        deploy_result.python_exec,
                                        "-m", "pip", "install",
                                        "--extra-index-url", "https://devpi.cerebras.aws/root/main/+simple/",
                                        "--trusted-host", "devpi.cerebras.aws",
                                        "--pre",  # Allow pre-release versions (matches uv's --prerelease=allow)
                                        "--only-binary", ":all:",  # Prevent building from source
                                        "-r", req_file,
                                        "--no-warn-script-location"
                                    ]
                                    pip_proc = await asyncio.create_subprocess_exec(
                                        *pip_cmd,
                                        stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE,
                                    )
                                    stdout, stderr = await pip_proc.communicate()
                                    if pip_proc.returncode != 0:
                                        logger.warning(
                                            f"[{replica_id}] Failed to install some dependencies: "
                                            f"{stderr.decode(errors='replace').strip()}"
                                        )
                                    else:
                                        logger.info(f"[{replica_id}] Successfully installed dependencies")

                                # Find the package directory (usually the top-level directory in the wheel)
                                # For cerebras_appliance wheel, it should contain a cerebras/ directory
                                site_packages = os.path.join(deploy_result.venv_path, "lib", "python*", "site-packages")
                                # Find the actual site-packages path
                                import glob
                                site_packages_paths = glob.glob(site_packages)
                                if site_packages_paths:
                                    target_site_packages = site_packages_paths[0]
                                else:
                                    # Fallback: create it if it doesn't exist
                                    python_ver = "python3.13"  # Adjust based on actual version
                                    target_site_packages = os.path.join(
                                        deploy_result.venv_path, "lib", python_ver, "site-packages"
                                    )
                                    os.makedirs(target_site_packages, exist_ok=True)

                                # Copy all package contents to site-packages
                                logger.debug(
                                    f"[{replica_id}] Copying extracted contents to {target_site_packages}"
                                )
                                extracted_items = os.listdir(extract_dir)
                                logger.debug(f"[{replica_id}] Extracted wheel contents: {extracted_items}")

                                for item in extracted_items:
                                    item_path = os.path.join(extract_dir, item)
                                    target_path = os.path.join(target_site_packages, item)

                                    # Skip .dist-info directories, we'll handle them separately
                                    if item.endswith('.dist-info'):
                                        # Copy dist-info for metadata
                                        logger.debug(f"[{replica_id}]   Copying dist-info: {item}")
                                        shutil.copytree(
                                            item_path,
                                            target_path,
                                            dirs_exist_ok=True
                                        )
                                    elif item.endswith('.data'):
                                        # Skip .data directories for now
                                        logger.debug(f"[{replica_id}]   Skipping .data: {item}")
                                        continue
                                    else:
                                        # Copy package directories/files
                                        if os.path.isdir(item_path):
                                            logger.debug(f"[{replica_id}]   Copying package directory: {item}")
                                            shutil.copytree(
                                                item_path,
                                                target_path,
                                                dirs_exist_ok=True
                                            )
                                        else:
                                            logger.debug(f"[{replica_id}]   Copying file: {item}")
                                            shutil.copy2(item_path, target_site_packages)

                                # Verify cerebras package was copied and show detailed structure
                                cerebras_path = os.path.join(target_site_packages, "cerebras")
                                if os.path.exists(cerebras_path):
                                    logger.info(f"[{replica_id}] ✓ cerebras package installed at {cerebras_path}")
                                    # List subdirectories to verify inference module
                                    subdirs = [d for d in os.listdir(cerebras_path) if os.path.isdir(os.path.join(cerebras_path, d))]
                                    logger.debug(f"[{replica_id}]   cerebras subdirectories: {subdirs}")

                                    # Show detailed structure of appliance directory
                                    appliance_path = os.path.join(cerebras_path, "appliance")
                                    if os.path.exists(appliance_path):
                                        appliance_contents = os.listdir(appliance_path)
                                        logger.debug(f"[{replica_id}]   cerebras/appliance/ contents ({len(appliance_contents)} items):")
                                        # Show first 20 items
                                        for item in sorted(appliance_contents)[:20]:
                                            item_path = os.path.join(appliance_path, item)
                                            item_type = "dir" if os.path.isdir(item_path) else "file"
                                            logger.debug(f"[{replica_id}]     - {item} ({item_type})")
                                        if len(appliance_contents) > 20:
                                            logger.debug(f"[{replica_id}]     ... and {len(appliance_contents) - 20} more items")
                                else:
                                    logger.error(f"[{replica_id}] ✗ cerebras package NOT found in site-packages!")

                                # Use Python to discover what modules are actually importable
                                logger.debug(f"[{replica_id}] Testing module imports...")
                                test_cmd = [
                                    deploy_result.python_exec, "-c",
                                    "import sys; import os; "
                                    "cerebras_path = os.path.join(sys.path[0], '../lib/python3.11/site-packages/cerebras'); "
                                    "print('cerebras.appliance modules:', sorted(os.listdir(os.path.join(cerebras_path, 'appliance')))[:30] if os.path.exists(os.path.join(cerebras_path, 'appliance')) else 'NOT FOUND')"
                                ]
                                test_proc = await asyncio.create_subprocess_exec(
                                    *test_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                    cwd=replica.workdir
                                )
                                stdout, stderr = await test_proc.communicate()
                                if stdout:
                                    logger.debug(f"[{replica_id}] {stdout.decode().strip()}")

                                logger.info(
                                    f"[{replica_id}] Successfully installed wheel by extraction"
                                )

                            except Exception as e:
                                error_msg = f"Wheel extraction/installation failed: {str(e)}"
                                logger.error(f"[{replica_id}] STEP 3b FAILED: {error_msg}")
                                replica._set_status(ReplicaStatus.FAILED, error=error_msg)
                                async with self._lock:
                                    self._replicas[replica_id] = replica
                                return replica
                            finally:
                                # Clean up extraction directory
                                if os.path.exists(extract_dir):
                                    shutil.rmtree(extract_dir, ignore_errors=True)

                            logger.info(
                                f"[{replica_id}] STEP 3b: ✓ Installed wheel '{os.path.basename(whl_path)}' "
                                f"into {deploy_result.venv_path}"
                            )

                        # STEP 3c: Install cerebras_inference_workload based on app_tag
                        # The monolith creates a requirements file with cerebras_inference_workload
                        # using the same version as the wheel (e.g., 260215+inference.202602201519.2373.9999f993)
                        logger.info(
                            f"[{replica_id}] STEP 3c: Installing cerebras_inference_workload "
                            f"for app_tag='{app_tag}'..."
                        )

                        # Get the wheel version from app_tag
                        # For inference builds, workload uses the SAME version as the wheel
                        # Format: app_tag "260215-inference-202602201519-2373-9999f993"
                        #         -> workload version "260215+inference.202602201519.2373.9999f993"
                        from release_server_service.core.wheel_resolver import get_whl_version_from_app_tag

                        workload_version = get_whl_version_from_app_tag(app_tag)
                        if workload_version:
                            # Install inference packages
                            # Strategy differs based on whether local wheel was found:
                            # - If whl_path exists: dependencies already installed from wheel METADATA -> use --no-deps
                            # - If whl_path is None: install api-server WITH deps, then others with --no-deps

                            api_server_req = f"cerebras-inference-api-server=={workload_version}"
                            workload_req = f"cerebras_inference_workload=={workload_version}"

                            if not whl_path:
                                # Usernode case: no local wheel, need to install from devpi
                                logger.info(f"[{replica_id}] Installing packages from devpi (no local wheel)...")

                                # FIRST: Install cerebras-inference-api-server and cerebras-inference-compiler WITH dependencies
                                # This ensures all transitive deps like starlette_context are installed
                                compiler_req = f"cerebras-inference-compiler=={workload_version}"
                                logger.debug(f"[{replica_id}]   Installing {api_server_req} and {compiler_req} with dependencies...")
                                api_req_file = os.path.join(replica.workdir, "api_server_requirements.txt")
                                with open(api_req_file, 'w') as f:
                                    f.write(api_server_req + '\n')
                                    f.write(compiler_req + '\n')

                                pip_cmd = [
                                    deploy_result.python_exec,
                                    "-m", "pip", "install",
                                    "--extra-index-url", "https://devpi.cerebras.aws/root/main/+simple/",
                                    "--trusted-host", "devpi.cerebras.aws",
                                    "--pre",  # Allow pre-release versions
                                    "--only-binary", ":all:",  # Force binary wheels only
                                    "-r", api_req_file,
                                    "--no-warn-script-location"
                                ]
                                pip_proc = await asyncio.create_subprocess_exec(
                                    *pip_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, stderr = await pip_proc.communicate()
                                if pip_proc.returncode != 0:
                                    logger.warning(
                                        f"[{replica_id}] Failed to install cerebras-inference-api-server/compiler: "
                                        f"{stderr.decode(errors='replace').strip()}"
                                    )
                                else:
                                    logger.info(f"[{replica_id}] ✓ Installed {api_server_req} and {compiler_req} with dependencies")

                                # SECOND: Install cerebras_appliance and cerebras_inference_workload with --no-deps
                                # Dependencies already satisfied from api-server installation
                                appliance_req = f"cerebras_appliance=={workload_version}"
                                packages_no_deps = [appliance_req, workload_req]

                                logger.debug(f"[{replica_id}]   Installing {appliance_req} and {workload_req} (no deps)...")
                                nodeps_req_file = os.path.join(replica.workdir, "inference_nodeps_requirements.txt")
                                with open(nodeps_req_file, 'w') as f:
                                    for pkg in packages_no_deps:
                                        f.write(pkg + '\n')

                                pip_cmd = [
                                    deploy_result.python_exec,
                                    "-m", "pip", "install",
                                    "--extra-index-url", "https://devpi.cerebras.aws/root/main/+simple/",
                                    "--trusted-host", "devpi.cerebras.aws",
                                    "--no-deps",  # Skip dependencies - already installed
                                    "-r", nodeps_req_file,
                                    "--no-warn-script-location"
                                ]
                                pip_proc = await asyncio.create_subprocess_exec(
                                    *pip_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, stderr = await pip_proc.communicate()
                                if pip_proc.returncode != 0:
                                    logger.warning(
                                        f"[{replica_id}] Failed to install cerebras_appliance/workload: "
                                        f"{stderr.decode(errors='replace').strip()}"
                                    )
                                else:
                                    for pkg in packages_no_deps:
                                        logger.info(f"[{replica_id}] ✓ Installed {pkg}")
                            else:
                                # Dev machine case: local wheel found, dependencies already installed
                                # Install all inference packages with --no-deps
                                compiler_req = f"cerebras-inference-compiler=={workload_version}"
                                packages_to_install = [api_server_req, compiler_req, workload_req]

                                logger.debug(f"[{replica_id}]   API server package: {api_server_req}")
                                logger.debug(f"[{replica_id}]   Compiler package: {compiler_req}")
                                logger.debug(f"[{replica_id}]   Workload package: {workload_req}")

                                # Create requirements file with all packages
                                workload_req_file = os.path.join(replica.workdir, "inference_workload_requirements.txt")
                                with open(workload_req_file, 'w') as f:
                                    for pkg in packages_to_install:
                                        f.write(pkg + '\n')

                                # Install using pip with Cerebras internal devpi index
                                # Use --no-deps to skip dependency resolution (already satisfied from STEP 3a)
                                pip_cmd = [
                                    deploy_result.python_exec,
                                    "-m", "pip", "install",
                                    "--extra-index-url", "https://devpi.cerebras.aws/root/main/+simple/",
                                    "--trusted-host", "devpi.cerebras.aws",
                                    "--no-deps",  # Skip dependencies - already installed from wheel METADATA
                                    "-r", workload_req_file,
                                    "--no-warn-script-location"
                                ]
                                pip_proc = await asyncio.create_subprocess_exec(
                                    *pip_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, stderr = await pip_proc.communicate()
                                if pip_proc.returncode != 0:
                                    logger.warning(
                                        f"[{replica_id}] Failed to install inference packages: "
                                        f"{stderr.decode(errors='replace').strip()}"
                                    )
                                else:
                                    for pkg in packages_to_install:
                                        logger.info(f"[{replica_id}] ✓ Installed {pkg}")

                    except Exception as e:
                        error_msg = f"Wheel installation raised exception: {e}"
                        logger.exception(f"[{replica_id}] STEP 3b FAILED: {error_msg}")
                        replica._set_status(ReplicaStatus.FAILED, error=error_msg)
                        async with self._lock:
                            self._replicas[replica_id] = replica
                        return replica
                else:
                    logger.info(
                        f"[{replica_id}] STEP 3b: No app_tag provided, skipping wheel install"
                    )
            else:
                # Fallback: bare venv for testing/development
                logger.info(
                    f"[{replica_id}] STEP 3/5: No cbclient_config — "
                    f"creating bare venv (development mode)..."
                )
                venv_path = os.path.join(local_workdir, "venv")
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "venv", venv_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                if process.returncode != 0:
                    error_msg = (
                        f"venv creation failed (exit code {process.returncode}): "
                        f"{stderr.decode(errors='replace').strip()}"
                    )
                    logger.error(f"[{replica_id}] STEP 3/5 FAILED: {error_msg}")
                    replica._set_status(ReplicaStatus.FAILED, error=error_msg)
                    async with self._lock:
                        self._replicas[replica_id] = replica
                    return replica

                replica.venv_path = venv_path
                replica.python_exec = os.path.join(venv_path, "bin", "python")
                logger.info(f"[{replica_id}] STEP 3/5: Created bare venv at {venv_path}")
        except Exception as e:
            error_msg = f"Environment setup raised exception: {e}"
            logger.exception(f"[{replica_id}] STEP 3/5 FAILED: {error_msg}")
            replica._set_status(ReplicaStatus.FAILED, error=error_msg)
            async with self._lock:
                self._replicas[replica_id] = replica
            return replica

        # ── STEP 4: Register in workdir map ────────────────────
        async with self._lock:
            self._replicas[replica_id] = replica
            self._workdir_map[replica_id] = local_workdir
        logger.info(
            f"[{replica_id}] STEP 4/5: Registered replica in workdir map ({local_workdir})"
        )

        # ── STEP 5: Log summary ────────────────────────────────
        logger.info(
            f"[{replica_id}] STEP 5/5: Replica setup complete. "
            f"Proceeding to server creation...\n"
            f"  mode={request.server_mode.value}\n"
            f"  model={request.model_name}\n"
            f"  multibox={request.placement.multibox}\n"
            f"  workdir={local_workdir}\n"
            f"  venv_path={replica.venv_path}\n"
            f"  python_exec={replica.python_exec}"
        )

        if request.wait_for_ready:
            # Synchronous — block until ready or error
            await self._bring_up_replica(replica)
        else:
            # Async — return immediately, bring up in background
            replica._task = asyncio.create_task(
                self._bring_up_replica(replica)
            )

        return replica

    async def _bring_up_replica(self, replica: ManagedReplica) -> None:
        """
        Full lifecycle: create → wait_for_ready → diagnostics.

        1. Create server handle (local subprocess)
        2. Health check
        3. Run diagnostics

        Python resolution is handled entirely by server_factory via
        workdir-based venv discovery — no env var wiring needed here.
        """
        request = replica.request
        rid = replica.replica_id

        # Step 1: Create server handle
        try:
            replica._set_status(ReplicaStatus.CREATING)
            logger.info(f"[{rid}] Creating server handle...")

            server_handle = await create_server_handle(
                request=request,
                local_workdir=replica.workdir,
            )

            replica.server_handle = server_handle
            replica._set_status(ReplicaStatus.STARTING)
            logger.info(
                f"[{rid}] Server handle created: "
                f"{getattr(server_handle, 'base_url', 'unknown')}"
            )
        except Exception as e:
            error_msg = f"Failed to create server handle: {e}"
            logger.exception(f"[{rid}] {error_msg}")
            replica._set_status(ReplicaStatus.FAILED, error=error_msg)
            return

        # Step 2: Wait for readiness
        try:
            replica._set_status(ReplicaStatus.WAITING_FOR_READY)
            logger.info(f"[{rid}] Waiting for server readiness...")
            base_url = getattr(server_handle, "base_url", None)

            if base_url:
                timeout = (
                    request.timeouts.readiness_timeout_s
                    or self._config.default_readiness_timeout_s
                )
                poll_interval = (
                    request.timeouts.poll_interval_s
                    or self._config.default_poll_interval_s
                )

                # Use the handle's own wait_for_ready if available
                if hasattr(server_handle, "wait_for_ready"):
                    port_timeout = (
                        request.timeouts.port_discovery_timeout_s
                        or self._config.default_port_discovery_timeout_s
                    )
                    await server_handle.wait_for_ready(
                        port_discovery_timeout_s=port_timeout,
                        readiness_timeout_s=timeout,
                        poll_interval_s=poll_interval,
                    )
                    is_healthy = True
                else:
                    # Fallback to our own polling
                    # Get PID for process monitoring
                    pid = getattr(server_handle, '_process', None)
                    pid = pid.pid if pid and hasattr(pid, 'pid') else None

                    is_healthy = await poll_health_endpoint(
                        base_url=base_url,
                        timeout_s=timeout,
                        poll_interval_s=poll_interval,
                        pid=pid,
                    )

                if is_healthy:
                    replica.ready_at = datetime.now(timezone.utc)
                    replica._set_status(ReplicaStatus.READY)
                    logger.info(
                        f"[{rid}] Server ready at {base_url}"
                    )
                else:
                    # Pull logs on failure
                    if hasattr(server_handle, "pull_wsjob_logs"):
                        try:
                            await server_handle.pull_wsjob_logs()
                        except Exception:
                            pass
                    replica._set_status(
                        ReplicaStatus.UNHEALTHY,
                        error=f"Health check failed within timeout",
                    )
                    return
            else:
                # No base_url — mark as ready (handle manages its own endpoint)
                replica.ready_at = datetime.now(timezone.utc)
                replica._set_status(ReplicaStatus.READY)
        except Exception as e:
            error_msg = f"Readiness check failed: {e}"
            logger.exception(f"[{rid}] {error_msg}")
            replica._set_status(ReplicaStatus.UNHEALTHY, error=error_msg)
            return

        # Step 3: Run diagnostics
        if request.run_diagnostics:
            try:
                logger.info(f"[{rid}] Running diagnostics...")
                if hasattr(server_handle, "run_diagnostics"):
                    await server_handle.run_diagnostics()
                    logger.info(f"[{rid}] Diagnostics complete")
                base_url = getattr(server_handle, "base_url", None)
                if base_url:
                    replica.diagnostics = await run_diagnostics(base_url)
            except Exception as e:
                logger.warning(
                    f"[{rid}] Diagnostics failed (non-fatal): {e}"
                )

    async def get_replica(self, replica_id: str) -> Optional[ManagedReplica]:
        """Get a replica by ID."""
        return self._replicas.get(replica_id)

    async def list_replicas(
        self,
        server_mode: Optional[str] = None,
        status: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> List[ManagedReplica]:
        """List replicas with optional filtering."""
        replicas = list(self._replicas.values())

        if server_mode:
            replicas = [
                r for r in replicas if r.request.server_mode.value == server_mode
            ]
        if status:
            replicas = [r for r in replicas if r.status.value == status]
        if model_name:
            replicas = [r for r in replicas if r.request.model_name == model_name]

        return replicas

    async def stop_replica(
        self, replica_id: str, force: bool = False
    ) -> Optional[ManagedReplica]:
        """Stop a replica and clean up resources."""
        replica = self._replicas.get(replica_id)
        if not replica:
            return None

        logger.info(
            f"[{replica_id}] Stopping replica (workdir={replica.workdir})"
        )
        replica._set_status(ReplicaStatus.STOPPING)

        # Cancel background task if running
        if replica._task and not replica._task.done():
            replica._task.cancel()
            try:
                await replica._task
            except asyncio.CancelledError:
                pass

        # Stop the server handle
        if replica.server_handle:
            try:
                if hasattr(replica.server_handle, "stop"):
                    success = await replica.server_handle.stop()
                    if success:
                        logger.info(f"[{replica_id}] Server stopped successfully")
                    else:
                        logger.warning(f"[{replica_id}] Server stop returned False")
            except Exception as e:
                logger.error(f"[{replica_id}] Error stopping server: {e}")

        replica._set_status(ReplicaStatus.STOPPED)

        # Remove from workdir map
        self._workdir_map.pop(replica_id, None)

        return replica

    async def health_check_replica(
        self, replica_id: str, timeout_s: int = 120, poll_interval_s: int = 5
    ) -> Optional[bool]:
        """Run a health check on a specific replica."""
        replica = self._replicas.get(replica_id)
        if not replica or not replica.server_handle:
            return None

        base_url = getattr(replica.server_handle, "base_url", None)
        if not base_url:
            return None

        # Prefer the handle's own health_check method
        if hasattr(replica.server_handle, "health_check"):
            is_healthy = await replica.server_handle.health_check(
                timeout_s=timeout_s, poll_interval_s=poll_interval_s
            )
        else:
            # Fallback: get PID for process monitoring
            pid = getattr(replica.server_handle, '_process', None)
            pid = pid.pid if pid and hasattr(pid, 'pid') else None

            is_healthy = await poll_health_endpoint(
                base_url=base_url,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                pid=pid,
            )

        if is_healthy:
            if replica.status != ReplicaStatus.READY:
                replica._set_status(ReplicaStatus.READY)
        else:
            replica._set_status(ReplicaStatus.UNHEALTHY)

        return is_healthy

    async def cleanup_all(self):
        """Stop all managed replicas (for graceful shutdown)."""
        for replica_id in list(self._replicas.keys()):
            try:
                logger.info(
                    f"[{replica_id}] Cleaning up replica "
                    f"(workdir={self._workdir_map.get(replica_id, 'unknown')})"
                )
                await self.stop_replica(replica_id)
            except Exception as e:
                logger.error(f"Error cleaning up replica {replica_id}: {e}")
