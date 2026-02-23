"""Replica Manager V2 - Worker Process Architecture.

This version spawns independent worker processes for each replica,
ensuring the REST server stays up even if replicas crash.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from release_server_service.config import ServiceConfig, get_config
from release_server_service.core.state_manager import ReplicaStateManager
from release_server_service.models.requests import CreateReplicaRequest
from release_server_service.models.responses import ReplicaInfo, ReplicaStatus

logger = logging.getLogger(__name__)


class ReplicaManagerV2:
    """Manages replicas via independent worker processes.

    Architecture:
    - REST server (this process) only coordinates and reads state
    - Worker processes handle replica lifecycle independently
    - Shared state stored in JSON files for cross-process communication
    - REST server never crashes due to replica failures
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        self._config = config or get_config()
        self._state_mgr = ReplicaStateManager(
            state_dir=os.path.join(self._config.local_workdir_root, "state")
        )
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_interval = 30  # seconds
        logger.info("[ReplicaManagerV2] Initialized with worker-process architecture")

    async def create_replica(self, request: CreateReplicaRequest) -> ReplicaInfo:
        """Create a new replica by spawning a worker process.

        This method returns immediately after spawning the worker.
        The worker process handles all replica lifecycle independently.

        Args:
            request: Replica creation request

        Returns:
            ReplicaInfo with pending status
        """
        # ── STEP 1: Create replica ID ──────────────────────────
        replica_id = str(uuid.uuid4())[:12]
        logger.info(f"[{replica_id}] Creating replica (worker-process mode)")

        # ── STEP 2: Create workdir ─────────────────────────────
        workdir = os.path.join(self._config.local_workdir_root, replica_id)
        os.makedirs(workdir, exist_ok=True)
        logger.info(f"[{replica_id}] Created workdir: {workdir}")

        # ── STEP 3: Write request to file ──────────────────────
        request_file = os.path.join(workdir, "request.json")
        with open(request_file, "w") as f:
            json.dump(request.dict(), f, indent=2)
        logger.info(f"[{replica_id}] Wrote request to {request_file}")

        # ── STEP 4: Create initial state ───────────────────────
        initial_state = {
            "server_mode": request.server_mode.value,
            "model_name": request.model_name,
            "status": "pending",
            "display_status": "Pending",
            "endpoint": "NA",
            "workdir": workdir,
            "multibox": request.placement.multibox,
            "namespace": request.placement.namespace,
            "request_id": request.request_id,
            "compile_wsjob": [],
            "execute_wsjob": [],
        }
        self._state_mgr.create_replica_state(replica_id, initial_state)

        # ── STEP 5: Spawn worker process ───────────────────────
        worker_script = os.path.join(
            os.path.dirname(__file__), "replica_worker.py"
        )

        # Use the same Python interpreter as the current process
        python_exec = sys.executable

        worker_cmd = [
            python_exec,
            worker_script,
            replica_id,
            request_file,
            workdir,
        ]

        logger.info(f"[{replica_id}] Spawning worker process: {' '.join(worker_cmd)}")

        # Spawn worker as a completely independent process
        # Use Popen (not subprocess.run) to avoid waiting
        # Redirect stdout/stderr to log files in workdir
        stdout_log = os.path.join(workdir, "worker_stdout.log")
        stderr_log = os.path.join(workdir, "worker_stderr.log")

        with open(stdout_log, "w") as stdout_file, open(stderr_log, "w") as stderr_file:
            worker_proc = subprocess.Popen(
                worker_cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=workdir,
                start_new_session=True,  # Detach from parent process group
            )

        worker_pid = worker_proc.pid
        self._state_mgr.set_worker_pid(replica_id, worker_pid)

        logger.info(
            f"[{replica_id}] Worker process spawned: PID={worker_pid}, "
            f"logs: {stdout_log}, {stderr_log}"
        )

        # ── STEP 6: Return immediately with pending status ─────
        return self._build_replica_info(replica_id)

    def _build_replica_info(self, replica_id: str) -> ReplicaInfo:
        """Build ReplicaInfo from shared state."""
        state = self._state_mgr.get_replica_state(replica_id)

        if not state:
            raise ValueError(f"Replica {replica_id} not found")

        return ReplicaInfo(
            replica_id=replica_id,
            server_mode=state.get("server_mode", "unknown"),
            model_name=state.get("model_name", "unknown"),
            status=ReplicaStatus(state.get("status", "pending")),
            display_status=state.get("display_status", "Pending"),
            endpoint=state.get("endpoint", "NA"),
            base_url=state.get("base_url"),
            host=state.get("host"),
            port=state.get("port"),
            multibox=state.get("multibox"),
            namespace=state.get("namespace"),
            workdir=state.get("workdir"),
            venv_path=state.get("venv_path"),
            compile_wsjob=state.get("compile_wsjob", []),
            execute_wsjob=state.get("execute_wsjob", []),
            created_at=datetime.fromisoformat(state["created_at"]),
            updated_at=datetime.fromisoformat(state["updated_at"]),
            ready_at=datetime.fromisoformat(state["ready_at"]) if state.get("ready_at") else None,
            error_message=state.get("error_message"),
            request_id=state.get("request_id"),
            diagnostics=state.get("diagnostics"),
            metadata=state.get("metadata", {}),
        )

    async def get_replica(self, replica_id: str) -> Optional[ReplicaInfo]:
        """Get replica info by ID.

        Args:
            replica_id: Replica identifier

        Returns:
            ReplicaInfo or None if not found
        """
        try:
            return self._build_replica_info(replica_id)
        except ValueError:
            return None

    async def list_replicas(
        self,
        server_mode: Optional[str] = None,
        status: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> List[ReplicaInfo]:
        """List all replicas with optional filtering.

        Args:
            server_mode: Filter by server mode
            status: Filter by status
            model_name: Filter by model name

        Returns:
            List of ReplicaInfo
        """
        all_states = self._state_mgr.list_replica_states()

        replicas = []
        for replica_id, state in all_states.items():
            # Apply filters
            if server_mode and state.get("server_mode") != server_mode:
                continue
            if status and state.get("status") != status:
                continue
            if model_name and state.get("model_name") != model_name:
                continue

            try:
                replicas.append(self._build_replica_info(replica_id))
            except Exception as e:
                logger.warning(f"[{replica_id}] Failed to build info: {e}")

        return replicas

    async def stop_replica(
        self, replica_id: str, force: bool = False
    ) -> Optional[ReplicaInfo]:
        """Stop a replica by killing its worker and replica processes.

        Args:
            replica_id: Replica identifier
            force: If True, use SIGKILL instead of SIGTERM

        Returns:
            ReplicaInfo or None if not found
        """
        state = self._state_mgr.get_replica_state(replica_id)
        if not state:
            return None

        logger.info(f"[{replica_id}] Stopping replica (force={force})")

        # Kill worker process
        worker_pid = self._state_mgr.get_worker_pid(replica_id)
        if worker_pid:
            try:
                import signal
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.kill(worker_pid, sig)
                logger.info(f"[{replica_id}] Killed worker process: PID={worker_pid}")
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"[{replica_id}] Failed to kill worker: {e}")

        # Kill replica subprocess
        replica_pid = state.get("replica_pid")
        if replica_pid:
            try:
                import signal
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.kill(replica_pid, sig)
                logger.info(f"[{replica_id}] Killed replica process: PID={replica_pid}")
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"[{replica_id}] Failed to kill replica: {e}")

        # Update state
        self._state_mgr.update_replica_state(
            replica_id,
            {
                "status": "stopped",
                "display_status": "Failed",
                "endpoint": "NA",
            },
        )

        return self._build_replica_info(replica_id)

    async def delete_replica(self, replica_id: str) -> bool:
        """Delete replica state and cleanup.

        Args:
            replica_id: Replica identifier

        Returns:
            True if deleted, False if not found
        """
        state = self._state_mgr.get_replica_state(replica_id)
        if not state:
            return False

        # Stop replica first
        await self.stop_replica(replica_id, force=True)

        # Delete state
        self._state_mgr.delete_replica_state(replica_id)

        logger.info(f"[{replica_id}] Deleted replica state")
        return True

    async def health_check_replica(
        self, replica_id: str, timeout_s: int = 120, poll_interval_s: int = 5
    ) -> Optional[bool]:
        """Run a health check on a replica.

        This just reads the current state - the worker process
        handles ongoing health monitoring.

        Args:
            replica_id: Replica identifier
            timeout_s: Unused (kept for API compatibility)
            poll_interval_s: Unused (kept for API compatibility)

        Returns:
            True if healthy, False if unhealthy, None if not found
        """
        state = self._state_mgr.get_replica_state(replica_id)
        if not state:
            return None

        status = state.get("status")
        return status == "ready"

    async def stop_monitoring(self):
        """Stop the monitoring task without killing replicas.

        This is for detached mode where replicas should continue running
        even when the REST server shuts down.
        """
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            logger.info("Monitoring task stopped (replicas continue running)")

    async def cleanup_all(self):
        """Stop all replicas and cleanup.

        WARNING: This kills all replica processes and cancels their wsjobs!
        Only call this if you want to forcefully terminate everything.
        For detached mode, use stop_monitoring() instead.
        """
        # Stop monitoring task
        await self.stop_monitoring()

        all_states = self._state_mgr.list_replica_states()

        for replica_id in all_states.keys():
            try:
                logger.info(f"[{replica_id}] Cleaning up replica")
                await self.stop_replica(replica_id, force=True)
            except Exception as e:
                logger.error(f"[{replica_id}] Cleanup failed: {e}")

    def start_monitoring(self):
        """Start background monitoring task for all replicas.

        This task periodically checks replica health and updates state.
        """
        if self._monitoring_task is None or self._monitoring_task.done():
            self._monitoring_task = asyncio.create_task(self._monitor_replicas())
            logger.info(
                f"Started replica monitoring task (interval: {self._monitoring_interval}s)"
            )

    async def _monitor_replicas(self):
        """Background task that monitors all replicas.

        Runs indefinitely, checking replica health at regular intervals.
        Updates state if replicas have died.
        """
        logger.info("[Monitor] Replica monitoring started")

        while True:
            try:
                await asyncio.sleep(self._monitoring_interval)

                all_states = self._state_mgr.list_replica_states()
                active_count = 0
                dead_count = 0

                for replica_id, state in all_states.items():
                    status = state.get("status")
                    replica_pid = state.get("replica_pid")

                    # Only monitor replicas that are supposedly running
                    if status not in ["ready", "waiting_for_ready", "starting"]:
                        continue

                    active_count += 1

                    # Check if replica process is still alive
                    if replica_pid:
                        is_alive = self._check_pid_alive(replica_pid)

                        if not is_alive:
                            logger.warning(
                                f"[{replica_id}] Replica process (PID {replica_pid}) has died"
                            )
                            # Update state to reflect dead process
                            self._state_mgr.update_replica_state(
                                replica_id,
                                {
                                    "status": "failed",
                                    "display_status": "Failed",
                                    "endpoint": "NA",
                                    "error_message": f"Replica process died (PID {replica_pid})",
                                    "died_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                            dead_count += 1
                        else:
                            # Try to get wsjob IDs from run_meta.json if not already captured
                            # Execute jobs are created AFTER compile jobs finish, so we need to
                            # keep checking until we have both types (or replica completes)
                            current_compile = state.get("compile_wsjob", [])
                            current_execute = state.get("execute_wsjob", [])

                            # Check if we still need to look for jobs
                            # Keep checking if:
                            # 1. We don't have compile jobs yet, OR
                            # 2. We don't have execute jobs yet (they appear after compilation)
                            if not current_compile or not current_execute:
                                workdir = state.get("workdir")
                                if workdir:
                                    compile_jobs, execute_jobs = self._get_wsjob_ids_from_workdir(
                                        workdir
                                    )

                                    # Check if we found new jobs
                                    new_compile = [j for j in compile_jobs if j not in current_compile]
                                    new_execute = [j for j in execute_jobs if j not in current_execute]

                                    # Only update if we found new jobs
                                    if new_compile or new_execute:
                                        # Merge with existing jobs (append new ones)
                                        updated_compile = current_compile + new_compile
                                        updated_execute = current_execute + new_execute

                                        logger.info(
                                            f"[{replica_id}] Captured wsjob IDs: "
                                            f"compile={updated_compile}, execute={updated_execute}"
                                        )
                                        self._state_mgr.update_replica_state(
                                            replica_id,
                                            {
                                                "compile_wsjob": updated_compile,
                                                "execute_wsjob": updated_execute,
                                            },
                                        )

                if active_count > 0:
                    logger.debug(
                        f"[Monitor] Checked {active_count} active replicas, "
                        f"found {dead_count} dead"
                    )

            except asyncio.CancelledError:
                logger.info("[Monitor] Monitoring task cancelled")
                raise
            except Exception as e:
                logger.error(f"[Monitor] Error in monitoring loop: {e}", exc_info=True)
                # Continue monitoring despite errors

    def _check_pid_alive(self, pid: int) -> bool:
        """Check if a process ID is still alive.

        Args:
            pid: Process ID to check

        Returns:
            True if alive, False otherwise
        """
        try:
            # Send signal 0 to check if process exists (doesn't actually send signal)
            os.kill(pid, 0)
            return True
        except OSError:
            # Process doesn't exist or we don't have permission
            return False
        except Exception:
            # Unexpected error, assume dead
            return False

    def _get_wsjob_ids_from_workdir(
        self, workdir: str
    ) -> Tuple[List[str], List[str]]:
        """Get wsjob IDs from run_meta.json in workdir.

        Reads workdir/model_dir/cerebras_logs/run_meta.json and extracts
        compile and execute job IDs.

        Args:
            workdir: Replica workdir path

        Returns:
            Tuple of (compile_job_ids, execute_job_ids) as lists of strings.
            Returns ([], []) if file doesn't exist or parsing fails.
        """
        try:
            # Construct path to run_meta.json
            run_meta_path = os.path.join(
                workdir, "model_dir", "cerebras_logs", "run_meta.json"
            )

            if not os.path.exists(run_meta_path):
                return [], []

            # Read and parse run_meta.json
            with open(run_meta_path, "r") as f:
                run_meta = json.load(f)

            # Extract compile job IDs
            compile_jobs = []
            for job in run_meta.get("compile_jobs", []):
                if "id" in job:
                    compile_jobs.append(str(job["id"]))

            # Extract execute job IDs
            execute_jobs = []
            for job in run_meta.get("execute_jobs", []):
                if "id" in job:
                    execute_jobs.append(str(job["id"]))

            return compile_jobs, execute_jobs

        except Exception as e:
            logger.debug(
                f"Failed to get wsjob IDs from {workdir}: {e}"
            )
            return [], []

    @property
    def replicas(self) -> dict:
        """Get all replicas as a dictionary (for compatibility).

        Returns:
            Dictionary mapping replica_id to state dict
        """
        return self._state_mgr.list_replica_states()
