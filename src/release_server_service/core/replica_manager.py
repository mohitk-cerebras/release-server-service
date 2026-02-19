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
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from release_server_service.config import ServiceConfig, get_config
from release_server_service.core.health import poll_health_endpoint, run_diagnostics
from release_server_service.core.server_factory import create_server_handle
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
        self.status = ReplicaStatus.PENDING
        self.server_handle: Any = None  # InferenceServerHandle once created
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

        return ReplicaInfo(
            replica_id=self.replica_id,
            server_mode=self.request.server_mode.value,
            model_name=self.request.model_name,
            status=self.status,
            base_url=base_url,
            host=host,
            port=port,
            multibox=self.request.placement.multibox,
            namespace=self.request.placement.namespace,
            usernode=self.request.placement.usernode,
            workdir=self.workdir,
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
        self._lock = asyncio.Lock()

    @property
    def replicas(self) -> Dict[str, ManagedReplica]:
        return self._replicas

    async def create_replica(
        self, request: CreateReplicaRequest
    ) -> ManagedReplica:
        """
        Create a new replica.

        This mirrors the key steps of test_start_server:
        1. Create local workdir
        2. Create the server handle (delegates to server_factory)
        3. Wait for readiness (health check)
        4. Run diagnostics

        The method runs synchronously if wait_for_ready=True, or
        kicks off a background task if wait_for_ready=False.

        Args:
            request: Full replica creation request

        Returns:
            ManagedReplica instance
        """
        replica_id = str(uuid.uuid4())[:12]
        local_workdir = os.path.join(
            self._config.local_workdir_root, replica_id
        )
        os.makedirs(local_workdir, exist_ok=True)

        replica = ManagedReplica(
            replica_id=replica_id,
            request=request,
            workdir=local_workdir,
        )

        async with self._lock:
            self._replicas[replica_id] = replica

        logger.info(
            f"Registered replica {replica_id}: "
            f"mode={request.server_mode.value}, model={request.model_name}, "
            f"multibox={request.placement.multibox}"
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

        This mirrors what test_start_server does:
        1. Create server handle
        2. Health check
        3. Run diagnostics
        """
        request = replica.request

        # Step 1: Create server handle
        try:
            replica._set_status(ReplicaStatus.CREATING)
            logger.info(f"[{replica.replica_id}] Creating server handle...")

            server_handle = await create_server_handle(
                request=request,
                local_workdir=replica.workdir,
            )
            replica.server_handle = server_handle
            replica._set_status(ReplicaStatus.STARTING)
            logger.info(
                f"[{replica.replica_id}] Server handle created: "
                f"{getattr(server_handle, 'base_url', 'unknown')}"
            )
        except Exception as e:
            error_msg = f"Failed to create server handle: {e}"
            logger.exception(f"[{replica.replica_id}] {error_msg}")
            replica._set_status(ReplicaStatus.FAILED, error=error_msg)
            return

        # Step 2: Wait for readiness
        try:
            replica._set_status(ReplicaStatus.WAITING_FOR_READY)
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

                # Use the handle's own wait_for_ready if available (mirrors test_start_server)
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
                    is_healthy = await poll_health_endpoint(
                        base_url=base_url,
                        timeout_s=timeout,
                        poll_interval_s=poll_interval,
                    )

                if is_healthy:
                    replica.ready_at = datetime.now(timezone.utc)
                    replica._set_status(ReplicaStatus.READY)
                    logger.info(
                        f"[{replica.replica_id}] Server ready at {base_url}"
                    )
                else:
                    # Pull logs on failure (mirrors test_start_server behavior)
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
            logger.exception(f"[{replica.replica_id}] {error_msg}")
            replica._set_status(ReplicaStatus.UNHEALTHY, error=error_msg)
            return

        # Step 3: Run diagnostics (mirrors test_start_server)
        if request.run_diagnostics:
            try:
                if hasattr(server_handle, "run_diagnostics"):
                    await server_handle.run_diagnostics()
                    logger.info(f"[{replica.replica_id}] Diagnostics complete")
                base_url = getattr(server_handle, "base_url", None)
                if base_url:
                    replica.diagnostics = await run_diagnostics(base_url)
            except Exception as e:
                logger.warning(
                    f"[{replica.replica_id}] Diagnostics failed (non-fatal): {e}"
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
            is_healthy = await poll_health_endpoint(
                base_url=base_url,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
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
                await self.stop_replica(replica_id)
            except Exception as e:
                logger.error(f"Error cleaning up replica {replica_id}: {e}")
