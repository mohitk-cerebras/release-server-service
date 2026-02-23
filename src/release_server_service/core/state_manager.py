"""Shared state manager for replica tracking.

Uses file-based JSON storage to track replica state across processes.
This ensures the REST server can stay up even if replica worker processes crash.
"""

import json
import logging
import os
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ReplicaStateManager:
    """Manages replica state in shared JSON files.

    Each replica has its own JSON file for atomic updates.
    This allows worker processes to update state independently
    without affecting the REST server process.
    """

    def __init__(self, state_dir: str = "/tmp/release-server-service/state"):
        """Initialize state manager.

        Args:
            state_dir: Directory to store replica state files
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[StateManager] Initialized with state_dir: {self.state_dir}")

    def _get_state_file(self, replica_id: str) -> Path:
        """Get the state file path for a replica."""
        return self.state_dir / f"{replica_id}.json"

    def _get_worker_pid_file(self, replica_id: str) -> Path:
        """Get the worker PID file path for a replica."""
        return self.state_dir / f"{replica_id}.worker.pid"

    def create_replica_state(
        self,
        replica_id: str,
        initial_state: Dict[str, Any],
    ) -> None:
        """Create initial state for a new replica.

        Args:
            replica_id: Unique replica identifier
            initial_state: Initial state dictionary
        """
        state_file = self._get_state_file(replica_id)

        state = {
            "replica_id": replica_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **initial_state,
        }

        with open(state_file, "w") as f:
            # Acquire exclusive lock for atomic write
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        logger.info(f"[{replica_id}] Created state file: {state_file}")

    def update_replica_state(
        self,
        replica_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """Update replica state atomically.

        Args:
            replica_id: Unique replica identifier
            updates: Dictionary of fields to update
        """
        state_file = self._get_state_file(replica_id)

        if not state_file.exists():
            logger.warning(f"[{replica_id}] State file not found: {state_file}")
            return

        with open(state_file, "r+") as f:
            # Acquire exclusive lock for atomic read-modify-write
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                state = json.load(f)
                state.update(updates)
                state["updated_at"] = datetime.now(timezone.utc).isoformat()

                # Rewind and truncate file before writing
                f.seek(0)
                f.truncate()
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        logger.debug(f"[{replica_id}] Updated state: {updates}")

    def get_replica_state(self, replica_id: str) -> Optional[Dict[str, Any]]:
        """Get current state of a replica.

        Args:
            replica_id: Unique replica identifier

        Returns:
            State dictionary or None if not found
        """
        state_file = self._get_state_file(replica_id)

        if not state_file.exists():
            return None

        try:
            with open(state_file, "r") as f:
                # Acquire shared lock for reading
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"[{replica_id}] Failed to read state: {e}")
            return None

    def list_replica_states(self) -> Dict[str, Dict[str, Any]]:
        """List all replica states.

        Returns:
            Dictionary mapping replica_id to state
        """
        states = {}

        for state_file in self.state_dir.glob("*.json"):
            # Skip worker PID files
            if state_file.name.endswith(".worker.pid"):
                continue

            replica_id = state_file.stem
            state = self.get_replica_state(replica_id)
            if state:
                states[replica_id] = state

        return states

    def delete_replica_state(self, replica_id: str) -> None:
        """Delete replica state file.

        Args:
            replica_id: Unique replica identifier
        """
        state_file = self._get_state_file(replica_id)
        worker_pid_file = self._get_worker_pid_file(replica_id)

        if state_file.exists():
            state_file.unlink()
            logger.info(f"[{replica_id}] Deleted state file")

        if worker_pid_file.exists():
            worker_pid_file.unlink()
            logger.debug(f"[{replica_id}] Deleted worker PID file")

    def set_worker_pid(self, replica_id: str, pid: int) -> None:
        """Record worker process PID for a replica.

        Args:
            replica_id: Unique replica identifier
            pid: Worker process ID
        """
        pid_file = self._get_worker_pid_file(replica_id)

        with open(pid_file, "w") as f:
            f.write(str(pid))

        logger.info(f"[{replica_id}] Recorded worker PID: {pid}")

    def get_worker_pid(self, replica_id: str) -> Optional[int]:
        """Get worker process PID for a replica.

        Args:
            replica_id: Unique replica identifier

        Returns:
            Worker PID or None if not found
        """
        pid_file = self._get_worker_pid_file(replica_id)

        if not pid_file.exists():
            return None

        try:
            with open(pid_file, "r") as f:
                return int(f.read().strip())
        except Exception as e:
            logger.error(f"[{replica_id}] Failed to read worker PID: {e}")
            return None

    def is_worker_alive(self, replica_id: str) -> bool:
        """Check if worker process is still alive.

        Args:
            replica_id: Unique replica identifier

        Returns:
            True if worker is alive, False otherwise
        """
        pid = self.get_worker_pid(replica_id)
        if not pid:
            return False

        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
