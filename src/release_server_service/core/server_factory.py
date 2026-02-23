"""Server factory — creates the appropriate server handle based on mode.

This module creates LOCAL server handles that run inside the container.
Since the service runs on the usernode already, there is NO SSH involved.
All server management is done locally via subprocess + HTTP health checks.

Command construction is fully automated based on server_mode — callers
never pass a custom command. Each mode (replica, api_gateway,
platform_workload) has its own command builder.

Python executable resolution:
  1. Explicit ``python_exec`` argument (if caller provides one)
  2. Existing venv discovered in workdir (``cbclient/`` or ``venv/``)
  3. Fresh venv created from ``app_tag`` (if provided)
  4. Fallback to bare ``python``
"""

import asyncio
import hashlib
import json
import logging
import os
import signal
from typing import Any, Dict, List, Optional

import yaml

from release_server_service.models.requests import CreateReplicaRequest
from release_server_service.models.server_modes import ServerMode

logger = logging.getLogger(__name__)

# Env var keys that should be redacted in logs
_SENSITIVE_ENV_KEYS = frozenset({
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "TOKEN",
    "PASSWORD", "SECRET", "API_KEY",
})

# Known venv directory names under a replica workdir (in priority order)
_VENV_DIR_NAMES = ("cbclient", "venv")


def _redact_env(env: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of env with sensitive values masked."""
    redacted = {}
    for k, v in env.items():
        if any(s in k.upper() for s in _SENSITIVE_ENV_KEYS):
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted


def _discover_venv_python(workdir: str) -> Optional[str]:
    """Discover an existing venv python executable inside *workdir*.

    Each replica's workdir may contain a venv created during setup
    (either ``cbclient/`` via deploy_cbclient_env or ``venv/``
    as a bare fallback).  This function checks the known locations
    and returns the first valid python path found.

    Returns:
        Absolute path to the venv python, or None if not found.
    """
    for venv_name in _VENV_DIR_NAMES:
        candidate = os.path.join(workdir, venv_name, "bin", "python")
        if os.path.isfile(candidate):
            logger.debug(
                f"[discover_venv_python] Found venv python: {candidate}"
            )
            return candidate
    return None


async def _ensure_venv_python(
    workdir: str,
    app_tag: Optional[str] = None,
) -> Optional[str]:
    """Ensure a venv exists in *workdir*, creating one if necessary.

    If no venv is found in the workdir:
      - If *app_tag* is provided, create a fresh venv via
        ``deploy_cbclient_env`` and install the matching wheel.
      - Otherwise return None (caller falls back to bare ``python``).

    Returns:
        Absolute path to the venv python, or None.
    """
    # 1. Check for existing venv
    existing = _discover_venv_python(workdir)
    if existing:
        logger.info(
            f"[ensure_venv] Reusing existing venv python: {existing}"
        )
        return existing

    # 2. No venv exists — create one if we have an app_tag
    if not app_tag:
        logger.info(
            "[ensure_venv] No existing venv and no app_tag provided — "
            "will use bare python"
        )
        return None

    logger.info(
        f"[ensure_venv] No venv found in {workdir}, creating fresh "
        f"venv with app_tag='{app_tag}'..."
    )

    # Import here to avoid circular imports
    from release_server_service.core.cbclient_deployer import deploy_cbclient_env
    from release_server_service.core.wheel_resolver import resolve_wheel_path

    # Create venv + install cbclient
    deploy_result = await deploy_cbclient_env(
        workdir=workdir,
        app_tag=app_tag,
    )

    # Also install the inference wheel if resolvable
    whl_path = resolve_wheel_path(app_tag)
    if whl_path:
        from release_server_service.core.cbclient_deployer import _pip_install

        logger.info(
            f"[ensure_venv] Installing wheel '{os.path.basename(whl_path)}' "
            f"into {deploy_result.venv_path}..."
        )
        try:
            await _pip_install(
                deploy_result.venv_path, [whl_path], use_uv=True
            )
            logger.info("[ensure_venv] ✓ Wheel installed")
        except Exception as e:
            logger.warning(
                f"[ensure_venv] Wheel install failed (non-fatal): {e}"
            )
    else:
        logger.info(
            f"[ensure_venv] No local wheel found for app_tag='{app_tag}' "
            "(skipping wheel install)"
        )

    python_exec = deploy_result.python_exec
    logger.info(f"[ensure_venv] ✓ Fresh venv ready: {python_exec}")
    return python_exec


class LocalServerHandle:
    """
    Container-native server handle.

    Manages an inference server running as a local subprocess.
    No SSH — the service is already on the usernode.

    Commands are auto-built from the server mode — callers never
    supply a custom command.
    """

    def __init__(
        self,
        *,
        model: str,
        workdir: str,
        host: str = "0.0.0.0",
        port: int = 0,
        mode: str = "replica",
        process: Optional[asyncio.subprocess.Process] = None,
        mock_backend: bool = False,
        extra_env: Optional[Dict[str, str]] = None,
    ):
        self.model = model
        self.workdir = workdir
        self._host = host
        self._port = port
        self.mode = mode
        self._process = process
        self.mock_backend = mock_backend
        self._extra_env = extra_env or {}

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> Optional[str]:
        if self._port:
            return f"http://{self._host}:{self._port}"
        return None

    # ── Config Writers ──────────────────────────────────────────

    @staticmethod
    def _construct_cbcore_image(app_tag: str) -> str:
        """Construct cbcore_image from app_tag.

        Docker image tags cannot contain '+' characters, so we convert PEP 440
        format back to Docker-compatible format.

        Args:
            app_tag: Application tag (e.g., "0.0.0-build-1b9c30c813", "0.0.0+build.1b9c30c813",
                     "260215-inference-202602201519-2373-9999f993")

        Returns:
            cbcore_image: Full ECR image path with Docker-compatible tag

        Examples:
            "0.0.0-build-1b9c30c813" -> "...cbcore:build-1b9c30c813"
            "0.0.0+build.1b9c30c813" -> "...cbcore:build-1b9c30c813"
            "260215-inference-..." -> "...cbcore:260215-inference-..."
            "build-1b9c30c813" -> "...cbcore:build-1b9c30c813"
        """
        # Standard Cerebras ECR registry path
        registry = "171496337684.dkr.ecr.us-west-2.amazonaws.com/cbcore"

        # Convert PEP 440 format to Docker-compatible format
        # Docker tags cannot contain '+', so we need to extract the original tag format
        docker_tag = app_tag

        # If tag contains '+' (PEP 440 format like "0.0.0+build.1b9c30c813")
        # Extract the local part after '+' and convert back to dash format
        if "+" in app_tag:
            parts = app_tag.split("+", 1)
            local_part = parts[1]  # e.g., "build.1b9c30c813"
            # Check if this looks like a semantic version base (X.Y.Z+...)
            # If so, extract just the local part for Docker tag
            if parts[0] and parts[0][0].isdigit() and "." in parts[0]:
                # Replace dots with dashes in local part to get original format
                docker_tag = local_part.replace(".", "-")
            else:
                # Not a semantic version, use the whole tag with + replaced by -
                docker_tag = app_tag.replace("+", "-")
        # If tag starts with version (like "0.0.0-build-..."), extract suffix
        elif app_tag and app_tag[0].isdigit() and "-" in app_tag:
            parts = app_tag.split("-", 1)
            # Check if first part is a semantic version (X.Y.Z)
            base = parts[0]
            if base.count(".") >= 1:  # Looks like a version number
                # Use only the suffix after the version
                docker_tag = parts[1]

        logger.debug(f"[_construct_cbcore_image] app_tag='{app_tag}' -> docker_tag='{docker_tag}'")
        return f"{registry}:{docker_tag}"

    @staticmethod
    def _inject_cbcore_image(
        full_config: Dict[str, Any], app_tag: Optional[str]
    ) -> Dict[str, Any]:
        """Inject cbcore_image into runconfig if app_tag is provided and cbcore_image is missing.

        Args:
            full_config: Full model configuration dict
            app_tag: Application tag from placement

        Returns:
            Modified full_config with cbcore_image injected
        """
        if not app_tag:
            return full_config

        # Ensure runconfig section exists
        if "runconfig" not in full_config:
            full_config["runconfig"] = {}

        runconfig = full_config["runconfig"]

        # Only inject if cbcore_image is not already specified
        if "cbcore_image" not in runconfig:
            cbcore_image = LocalServerHandle._construct_cbcore_image(app_tag)
            runconfig["cbcore_image"] = cbcore_image
            logger.info(f"[_inject_cbcore_image] Auto-generated cbcore_image from app_tag: {cbcore_image}")
        else:
            logger.debug(f"[_inject_cbcore_image] cbcore_image already specified: {runconfig['cbcore_image']}")

        return full_config

    @staticmethod
    def _ensure_model_dir_in_workdir(
        full_config: Dict[str, Any], workdir: str
    ) -> Dict[str, Any]:
        """Ensure model_dir in runconfig is within workdir for consolidated logging.

        Args:
            full_config: Full model configuration dict
            workdir: Working directory path

        Returns:
            Modified full_config with model_dir set to workdir-relative path
        """
        if "runconfig" not in full_config:
            full_config["runconfig"] = {}

        runconfig = full_config["runconfig"]

        # Check if model_dir is specified
        if "model_dir" in runconfig:
            model_dir = runconfig["model_dir"]
            # If it's a relative path, make it absolute within workdir
            if not os.path.isabs(model_dir):
                model_dir_abs = os.path.join(workdir, model_dir)
                runconfig["model_dir"] = model_dir_abs
                logger.info(f"[_ensure_model_dir_in_workdir] Set model_dir to: {model_dir_abs}")
            else:
                logger.warning(
                    f"[_ensure_model_dir_in_workdir] model_dir is absolute ({model_dir}), "
                    f"logs may not be in workdir. Consider using relative path."
                )
        else:
            # Default model_dir to workdir/model_dir
            model_dir_abs = os.path.join(workdir, "model_dir")
            runconfig["model_dir"] = model_dir_abs
            logger.info(f"[_ensure_model_dir_in_workdir] No model_dir specified, using: {model_dir_abs}")

        return full_config

    @staticmethod
    def _write_debug_proto(workdir: str, app_tag: str) -> str:
        """Write debug.proto file for appliance_host_inference.py.

        Args:
            workdir: Working directory path
            app_tag: Application tag for cbcore image

        Returns:
            Absolute path of the written debug.proto file
        """
        cbcore_image = LocalServerHandle._construct_cbcore_image(app_tag)

        # List of all task types that need the cbcore image
        task_types = [
            "activation",
            "broadcastreduce",
            "chief",
            "command",
            "coordinator",
            "kvstorageserver",
            "swdriver",
            "weight",
            "worker",
        ]

        # Generate task_spec_hints for each task type
        task_spec_hints = []
        for task_type in task_types:
            task_spec_hints.append(f"""  task_spec_hints {{
    key: "{task_type}"
    value {{
      container_image: "{cbcore_image}"
    }}
  }}""")

        debug_proto_content = f"""debug_usr {{
  compile_coord_resource {{
  }}
  execute_coord_resource {{
  }}
}}
debug_mgr {{
{chr(10).join(task_spec_hints)}
  drop_caches_value: DROPCACHES_THREE
}}
ini {{
  bools {{
    key: "inf_kv_cache_defrag_on_dealloc"
  }}
  bools {{
    key: "inf_llguidance_enable_harmony_tokenizer"
    value: true
  }}
  bools {{
    key: "inf_qk_aamatmul_mask_caching"
    value: true
  }}
  bools {{
    key: "inf_rt_enable_mue"
    value: true
  }}
  ints {{
    key: "inf_default_num_alloc_kv_cache_output_tokens"
    value: 8192
  }}
  ints {{
    key: "inf_kernel_swa_window"
    value: 128
  }}
  ints {{
    key: "inf_rt_cmd_streams_per_worker"
    value: 16
  }}
  ints {{
    key: "inf_rt_kv_refill_latency_ns"
    value: 415000
  }}
  floats {{
    key: "inf_rt_kv_refill_overhead_mul"
    value: 6.4
  }}
  floats {{
    key: "inf_rt_kv_refill_time_mul"
    value: 6.4
  }}
  strings {{
    key: "cbcore_task_spec"
    value: "{cbcore_image}"
  }}
  strings {{
    key: "inf_swdriver_placement"
    value: "ax"
  }}
  strings {{
    key: "ws_opt_llvm_stats_file"
    value: "ws_stack_llvm_stats.json"
  }}
}}
"""

        debug_proto_path = os.path.join(workdir, "debug.proto")
        with open(debug_proto_path, "w") as f:
            f.write(debug_proto_content)

        logger.info(f"[_write_debug_proto] Wrote debug.proto to {debug_proto_path} with cbcore_image: {cbcore_image}")
        return debug_proto_path

    @staticmethod
    def _write_params_yaml(
        full_config: Dict[str, Any], workdir: str, app_tag: Optional[str] = None
    ) -> str:
        """Write full_config as YAML to ``params.yaml`` in *workdir*.

        Auto-injects cbcore_image into runconfig if app_tag is provided and cbcore_image is missing.
        Ensures model_dir is within workdir for consolidated logging.

        Args:
            full_config: Full model configuration dict
            workdir: Working directory path
            app_tag: Optional application tag for cbcore_image auto-generation

        Returns:
            Absolute path of the written file.
        """
        # Inject cbcore_image if needed
        full_config = LocalServerHandle._inject_cbcore_image(full_config, app_tag)

        # Ensure model_dir is within workdir
        full_config = LocalServerHandle._ensure_model_dir_in_workdir(full_config, workdir)

        params_path = os.path.join(workdir, "params.yaml")
        with open(params_path, "w") as f:
            yaml.dump(full_config, f, default_flow_style=False)
        return params_path

    @staticmethod
    def _read_port_from_api_server_json(workdir: str) -> Optional[int]:
        """Read port from ``api_server.json`` if it exists.

        This is a fallback port-discovery mechanism: the inference server
        writes its listening port to ``api_server.json`` at startup.
        """
        api_json_path = os.path.join(workdir, "api_server.json")
        try:
            with open(api_json_path, "r") as f:
                data = json.load(f)
            return int(data.get("port", 0)) or None
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return None

    # ── Command Builders ────────────────────────────────────────

    @staticmethod
    def _find_appliance_host_inference_py(python_exec: str) -> Optional[str]:
        """Find appliance_host_inference.py in the venv's site-packages.

        Args:
            python_exec: Path to Python executable

        Returns:
            Path to appliance_host_inference.py or None if not found
        """
        import subprocess
        try:
            # Get site-packages directory
            result = subprocess.run(
                [python_exec, "-c", "import site; print(site.getsitepackages()[0])"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                site_packages = result.stdout.strip()
                # Try common locations
                possible_paths = [
                    os.path.join(site_packages, "cerebras", "appliance_host_inference.py"),
                    os.path.join(site_packages, "cerebras", "appliance", "appliance_host_inference.py"),
                    os.path.join(site_packages, "appliance_host_inference.py"),
                ]
                for path in possible_paths:
                    if os.path.exists(path):
                        logger.info(f"[_find_appliance_host_inference_py] Found: {path}")
                        return path

                # If not found, log site-packages for debugging
                logger.warning(
                    f"[_find_appliance_host_inference_py] appliance_host_inference.py not found in site-packages: {site_packages}"
                )
        except Exception as e:
            logger.warning(f"[_find_appliance_host_inference_py] Error finding appliance_host_inference.py: {e}")

        return None

    @staticmethod
    def _build_replica_cmd(
        *,
        python_exec: str,
        params_path: str,
        port: int,
        namespace: str,
        log_path: str,
        app_tag: Optional[str] = None,
        disable_version_check: bool = False,
        mock_backend: bool = False,
    ) -> List[str]:
        """Build command for replica / replica_mock mode.

        Uses cerebras.inference.workload.main with --cbcore flag if app_tag provided.

        """
        cmd = [
            python_exec,
            "-m", "cerebras.inference.workload.main",
            "--params", params_path,
            "--fastapi",
            "--port", str(port),
            "--mgmt_namespace", namespace,
            "--logfile", log_path
        ]

        # Add --cbcore flag if app_tag is provided
        if app_tag:
            cbcore_image = LocalServerHandle._construct_cbcore_image(app_tag)
            cmd.extend(["--cbcore", cbcore_image])
            logger.info(f"[_build_replica_cmd] Added --cbcore {cbcore_image}")

        if disable_version_check:
            cmd.append("--disable_version_check")
        if mock_backend:
            cmd.append("--mock_backend")

        return cmd

    @staticmethod
    def _build_api_gateway_cmd(
        *,
        python_exec: str,
        params_path: str,
        port: int,
        namespace: str,
        log_path: str,
        mock_backend: bool = False,
    ) -> List[str]:
        """Build command for api_gateway / api_gateway_mock mode.

        API gateway mode uses the same inference server entry point but
        includes the --api_gateway flag to enable gateway routing logic.
        """
        cmd = [
            python_exec,
            "-m", "cerebras.inference.workload.main",
            "--params", params_path,
            "--fastapi",
            "--port", str(port),
            "--mgmt_namespace", namespace,
            "--logfile", log_path,
            "--api_gateway",
        ]
        if mock_backend:
            cmd.append("--mock_backend")
        return cmd

    @staticmethod
    def _build_platform_workload_cmd(
        *,
        python_exec: str,
        params_path: str,
        port: int,
        namespace: str,
        log_path: str,
        mock_backend: bool = False,
    ) -> List[str]:
        """Build command for platform_workload / platform_workload_mock mode.

        Platform workload mode uses the same entry point but includes
        --platform_workload to enable orchestrator-driven lifecycle.
        """
        cmd = [
            python_exec,
            "-m", "cerebras.inference.workload.main",
            "--params", params_path,
            "--fastapi",
            "--port", str(port),
            "--mgmt_namespace", namespace,
            "--logfile", log_path,
            "--platform_workload",
        ]
        if mock_backend:
            cmd.append("--mock_backend")
        return cmd

    @classmethod
    def _build_cmd_for_mode(
        cls,
        mode: str,
        **kwargs,
    ) -> List[str]:
        """Dispatch to the correct command builder based on mode.

        Filters kwargs to only pass parameters supported by each builder.
        """
        builders = {
            "replica": cls._build_replica_cmd,
            "replica_mock": cls._build_replica_cmd,
            "api_gateway": cls._build_api_gateway_cmd,
            "api_gateway_mock": cls._build_api_gateway_cmd,
            "platform_workload": cls._build_platform_workload_cmd,
            "platform_workload_mock": cls._build_platform_workload_cmd,
        }
        builder = builders.get(mode)
        if builder is None:
            raise ValueError(
                f"No command builder for mode '{mode}'. "
                f"Supported modes: {sorted(builders.keys())}"
            )

        # Filter kwargs to only include parameters supported by the builder
        import inspect
        sig = inspect.signature(builder)
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

        return builder(**filtered_kwargs)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        model: str,
        workdir: str,
        multibox: str,
        namespace: Optional[str] = None,
        app_tag: Optional[str] = None,
        full_config: Optional[Dict[str, Any]] = None,
        job_priority: str = "p2",
        job_timeout_s: int = 86400,
        influxdb_params: Optional[Dict] = None,
        mock_backend: bool = False,
        disable_scheduler: bool = False,
        mode: str = "replica",
        extra_env: Optional[Dict[str, str]] = None,
        python_exec: Optional[str] = None,
        **kwargs,
    ) -> "LocalServerHandle":
        """
        Create and start a local inference server process.

        The command is auto-built from ``mode`` — no custom command
        parameter is accepted.

        Python executable resolution order:
          1. Explicit *python_exec* argument
          2. Existing venv in *workdir* (``cbclient/`` or ``venv/``)
          3. Fresh venv created from *app_tag*
          4. Bare ``python``

        Args:
            model: Model name to serve
            workdir: Local working directory
            multibox: Target cluster name
            namespace: Kubernetes namespace
            app_tag: Application tag (used for venv creation if needed)
            full_config: Complete model configuration
            mock_backend: If True, run in mock mode
            mode: Server mode label (replica, api_gateway, platform_workload, etc.)
            extra_env: Additional environment variables
            python_exec: Python executable to use (overrides auto-discovery)

        Returns:
            A running LocalServerHandle
        """
        namespace = namespace or "inf-integ"

        # Extract replica_id from workdir for logging context
        # Workdir format: /path/to/workdir_root/{replica_id}
        replica_id = os.path.basename(workdir)

        # ── Resolve python executable from workdir ─────────────
        if python_exec is None:
            python_exec = await _ensure_venv_python(workdir, app_tag=app_tag)
            if python_exec:
                logger.info(
                    f"[{replica_id}] [create] Resolved python from workdir: {python_exec}"
                )
            else:
                python_exec = "python"
                logger.info(
                    f"[{replica_id}] [create] No venv found and no app_tag — "
                    "using bare 'python'"
                )
        logger.info(f"[{replica_id}] [create] Python executable: {python_exec}")

        logger.info(
            f"[{replica_id}] [create] Starting local server — "
            f"mode={mode}, model={model}, multibox={multibox}, "
            f"namespace={namespace}, mock={mock_backend}, "
            f"python_exec={python_exec}"
        )

        # ── Prepare environment ────────────────────────────────
        env = {
            **os.environ,
            "MODEL_NAME": model,
            "WORKDIR": workdir,
            "MULTIBOX": multibox,
            "NAMESPACE": namespace,
            "MOCK_BACKEND": str(mock_backend).lower(),
            "JOB_PRIORITY": job_priority,
            **(extra_env or {}),
        }

        if app_tag:
            env["APP_TAG"] = app_tag

        logger.debug(
            f"[{replica_id}] [create] Environment variables (redacted): "
            f"{_redact_env(env)}"
        )

        # ── Write full_config as YAML to workdir ───────────────
        if full_config:
            params_path = cls._write_params_yaml(full_config, workdir, app_tag=app_tag)
            # Also write full_config.json for downstream consumers
            config_json_path = os.path.join(workdir, "full_config.json")
            with open(config_json_path, "w") as f:
                json.dump(full_config, f, indent=2)
            config_hash = hashlib.sha256(
                json.dumps(full_config, sort_keys=True).encode()
            ).hexdigest()[:12]
            env["FULL_CONFIG_PATH"] = params_path
            logger.info(
                f"[{replica_id}] [create] Wrote params.yaml to {params_path} "
                f"(sha256={config_hash})"
            )
        else:
            params_path = os.path.join(workdir, "params.yaml")
            logger.warning(
                f"[{replica_id}] [create] No full_config provided — params file not written"
            )

        # ── Write debug.proto if app_tag is provided ───────────
        debug_proto_path = None
        if app_tag:
            try:
                debug_proto_path = cls._write_debug_proto(workdir, app_tag)
                logger.info(f"[{replica_id}] [create] Wrote debug.proto to {debug_proto_path}")
            except Exception as e:
                logger.warning(f"[{replica_id}] [create] Failed to write debug.proto: {e}. Will use fallback command.")

        # ── Find a free port ───────────────────────────────────
        port = cls._find_free_port()
        env["PORT"] = str(port)
        logger.info(f"[{replica_id}] [create] Selected port: {port}")

        # ── Build log path ─────────────────────────────────────
        log_path = os.path.join(workdir, f"{mode}_server.log")

        # ── Build the command (auto from mode) ─────────────────
        cmd = cls._build_cmd_for_mode(
            mode=mode,
            python_exec=python_exec,
            params_path=params_path,
            port=port,
            namespace=namespace,
            log_path=log_path,
            mock_backend=mock_backend,
            app_tag=app_tag,
            debug_proto_path=debug_proto_path,  # Pass debug.proto path for replica mode
        )
        logger.info(f"[{replica_id}] [create] Command: {' '.join(cmd)}")

        # ── Launch subprocess ──────────────────────────────────
        stdout_path = os.path.join(workdir, f"{mode}_stdout.log")
        stderr_path = os.path.join(workdir, f"{mode}_stderr.log")
        logger.info(
            f"[{replica_id}] [create] Stdout log: {stdout_path}, Stderr log: {stderr_path}"
        )

        # Open log files for stdout/stderr
        stdout_file = open(stdout_path, "w")
        stderr_file = open(stderr_path, "w")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=workdir,
            stdout=stdout_file,
            stderr=stderr_file,
        )

        logger.info(
            f"[create] Subprocess launched: pid={process.pid}, "
            f"mode={mode}, model={model}, port={port}"
        )

        # Brief check that the process didn't die immediately
        await asyncio.sleep(0.5)
        if process.returncode is not None:
            # Process died immediately - read stderr from file
            stderr_text = ""
            try:
                with open(stderr_path, "r") as f:
                    stderr_text = f.read(4096)
            except Exception:
                pass
            logger.error(
                f"[create] Process exited immediately! "
                f"exit_code={process.returncode}, "
                f"stderr (last 4096 bytes):\n{stderr_text}"
            )
            raise RuntimeError(
                f"Server process died on startup (exit_code={process.returncode}). "
                f"stderr: {stderr_text[:500]}"
            )

        handle = cls(
            model=model,
            workdir=workdir,
            host="127.0.0.1",
            port=port,
            mode=mode,
            process=process,
            mock_backend=mock_backend,
            extra_env=extra_env,
        )

        logger.info(
            f"[create] ✓ Local {mode} server started: pid={process.pid}, "
            f"base_url={handle.base_url}"
        )
        return handle

    # ── Lifecycle ──────────────────────────────────────────────

    async def stop(self) -> bool:
        """Stop the server subprocess."""
        if self._process is None:
            logger.info("[stop] No process to stop")
            return True

        if self._process.returncode is not None:
            logger.info(
                f"[stop] Process already exited "
                f"(exit_code={self._process.returncode})"
            )
            return True

        logger.info(f"[stop] Stopping server (pid={self._process.pid})...")
        try:
            self._process.send_signal(signal.SIGTERM)
            logger.debug("[stop] Sent SIGTERM, waiting up to 30s...")
            try:
                await asyncio.wait_for(self._process.wait(), timeout=30)
                logger.info(
                    f"[stop] Process terminated gracefully "
                    f"(exit_code={self._process.returncode})"
                )
            except asyncio.TimeoutError:
                logger.warning("[stop] SIGTERM timed out, sending SIGKILL")
                self._process.kill()
                await self._process.wait()
                logger.info("[stop] Process killed via SIGKILL")
            return True
        except Exception as e:
            logger.error(f"[stop] Error stopping process: {e}")
            return False

    async def health_check(
        self, timeout_s: int = 120, poll_interval_s: int = 5
    ) -> bool:
        """Run a health check against the local server."""
        from release_server_service.core.health import poll_health_endpoint

        if not self.base_url:
            logger.warning("[health_check] No base_url — cannot health check")
            return False

        # Get PID for process monitoring during health check
        pid = self._process.pid if self._process else None

        logger.info(
            f"[health_check] Polling {self.base_url}/health "
            f"(timeout={timeout_s}s, interval={poll_interval_s}s, pid={pid})"
        )
        return await poll_health_endpoint(
            base_url=self.base_url,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            pid=pid,
        )

    async def wait_for_ready(
        self,
        port_discovery_timeout_s: int = 600,
        readiness_timeout_s: int = 1800,
        poll_interval_s: int = 5,
    ) -> None:
        """Wait until the server is healthy."""
        logger.info(
            f"[wait_for_ready] Waiting up to {readiness_timeout_s}s "
            f"for {self.base_url} to become healthy..."
        )
        is_healthy = await self.health_check(
            timeout_s=readiness_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        if not is_healthy:
            # Capture stderr for diagnostics
            stderr_snippet = ""
            if self._process and self._process.stderr:
                try:
                    data = await asyncio.wait_for(
                        self._process.stderr.read(4096), timeout=5
                    )
                    stderr_snippet = data.decode(errors="replace")
                except asyncio.TimeoutError:
                    pass
            logger.error(
                f"[wait_for_ready] Server did not become ready within "
                f"{readiness_timeout_s}s. stderr snippet:\n{stderr_snippet}"
            )
            raise TimeoutError(
                f"Server did not become ready within {readiness_timeout_s}s"
            )
        logger.info("[wait_for_ready] ✓ Server is healthy")

    async def run_diagnostics(self) -> Optional[Dict[str, Any]]:
        """Run diagnostics against the local server."""
        from release_server_service.core.health import run_diagnostics

        if not self.base_url:
            return None
        logger.info(f"[run_diagnostics] Running against {self.base_url}")
        return await run_diagnostics(self.base_url)

    async def pull_wsjob_logs(self) -> None:
        """Pull logs from the subprocess (local equivalent)."""
        if self._process and self._process.stderr:
            try:
                stderr = await asyncio.wait_for(
                    self._process.stderr.read(4096), timeout=5
                )
                if stderr:
                    logger.info(
                        f"Server stderr:\n{stderr.decode(errors='replace')}"
                    )
            except asyncio.TimeoutError:
                pass

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _find_free_port() -> int:
        """Find a free TCP port on localhost."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port = s.getsockname()[1]
        return port

    @staticmethod
    def get_external_ip() -> str:
        """Get external IP of this container (for gateway mode)."""
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


# ── Public factory function ────────────────────────────────────


async def create_server_handle(
    request: CreateReplicaRequest,
    local_workdir: str,
    python_exec: Optional[str] = None,
) -> LocalServerHandle:
    """
    Create a local server handle based on the request mode.

    No monolith dependencies — everything runs locally in the container.
    The command is always auto-built from server_mode.

    Args:
        request: The full create-replica request payload
        local_workdir: Local working directory for this replica
        python_exec: Python executable to use (overrides workdir auto-discovery)

    Returns:
        A LocalServerHandle instance

    Raises:
        ValueError: If unsupported server mode
    """
    mode = request.server_mode
    model_name = request.model_name
    full_config = dict(request.full_config) if request.full_config else {}
    placement = request.placement

    # Merge replica and api config overrides into full_config
    if request.replica_config:
        if request.replica_config.replica_config:
            full_config["replica"] = {
                **full_config.get("replica", {}),
                **request.replica_config.replica_config,
            }
        if request.replica_config.api_config:
            full_config["api_config"] = {
                **full_config.get("api_config", {}),
                **request.replica_config.api_config,
            }

    # Build influxdb params dict if provided
    influxdb_params = None
    if request.influxdb:
        influxdb_params = {
            "use_influxdb": request.influxdb.use_influxdb,
            "influxdb_local": request.influxdb.influxdb_local,
            "data_dir": request.influxdb.data_dir,
            "host": request.influxdb.host,
        }

    extra_env: Dict[str, str] = {}
    if influxdb_params:
        if influxdb_params.get("use_influxdb"):
            extra_env["USE_INFLUXDB"] = "true"
        if influxdb_params.get("host"):
            extra_env["INFLUXDB_HOST"] = influxdb_params["host"]

    if mode.is_api_gateway:
        gw_mock = mode.is_mock
        if request.gateway_config:
            gw_mock = request.gateway_config.mock_backend or gw_mock
            if request.gateway_config.extra:
                extra_env.update(
                    {k: str(v) for k, v in request.gateway_config.extra.items()}
                )

    if mode.is_platform_workload:
        if not request.platform_config:
            raise ValueError(
                "platform_config is required for PLATFORM_WORKLOAD mode"
            )
        pc = request.platform_config
        if pc.release_label:
            extra_env["PLATFORM_RELEASE_LABEL"] = pc.release_label
        if pc.control_plane_namespace:
            extra_env["PLATFORM_CP_NAMESPACE"] = pc.control_plane_namespace
        if pc.deployment_host:
            extra_env["PLATFORM_DEPLOYMENT_HOST"] = pc.deployment_host
        if pc.api_gateway_url:
            extra_env["API_GATEWAY_URL"] = pc.api_gateway_url
        if pc.workload_name:
            extra_env["WORKLOAD_NAME"] = pc.workload_name
        if pc.workload_image_tag:
            extra_env["WORKLOAD_IMAGE_TAG"] = pc.workload_image_tag

        cat_cfg = request.catalog_config
        if cat_cfg and cat_cfg.catalog_id_suffix:
            extra_env["CATALOG_ID_SUFFIX"] = cat_cfg.catalog_id_suffix

    return await LocalServerHandle.create(
        model=model_name,
        workdir=local_workdir,
        multibox=placement.multibox,
        namespace=placement.namespace,
        app_tag=placement.app_tag,
        full_config=full_config,
        job_priority=request.job.job_priority,
        job_timeout_s=request.job.job_timeout_s,
        mock_backend=mode.is_mock,
        mode=mode.value,
        extra_env=extra_env,
        python_exec=python_exec,
    )
