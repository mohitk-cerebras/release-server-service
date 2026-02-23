"""REST API response schemas."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReplicaStatus(str, Enum):
    """Lifecycle states for a managed replica."""

    PENDING = "pending"
    CREATING = "creating"
    STARTING = "starting"
    WAITING_FOR_READY = "waiting_for_ready"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    ERROR = "error"


class ReplicaInfo(BaseModel):
    """Detailed information about a managed replica."""

    replica_id: str
    server_mode: str
    model_name: str
    status: ReplicaStatus
    display_status: str  # "Active", "Pending", or "Failed"
    endpoint: str  # base_url when Active, "NA" otherwise
    base_url: Optional[str] = None  # Original base_url field (kept for backwards compatibility)
    host: Optional[str] = None
    port: Optional[int] = None
    multibox: Optional[str] = None
    namespace: Optional[str] = None
    workdir: Optional[str] = None
    venv_path: Optional[str] = None
    compile_wsjob: List[str] = Field(default_factory=list)  # Compile job IDs from run_meta.json
    execute_wsjob: List[str] = Field(default_factory=list)  # Execute job IDs from run_meta.json
    created_at: datetime
    updated_at: datetime
    ready_at: Optional[datetime] = None
    error_message: Optional[str] = None
    request_id: Optional[str] = None
    diagnostics: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CreateReplicaResponse(BaseModel):
    """Response after initiating replica creation."""

    replica_id: str
    status: ReplicaStatus
    message: str
    base_url: Optional[str] = None
    request_id: Optional[str] = None


class ReplicaStatusResponse(BaseModel):
    """Response for replica status query."""

    replica_id: str
    status: ReplicaStatus
    info: ReplicaInfo


class ReplicaListResponse(BaseModel):
    """Response for listing replicas."""

    total: int
    replicas: List[ReplicaInfo]


class StopReplicaResponse(BaseModel):
    """Response after stopping a replica."""

    replica_id: str
    status: ReplicaStatus
    message: str


class HealthCheckResponse(BaseModel):
    """Response for health check."""

    replica_id: str
    healthy: bool
    base_url: Optional[str] = None
    message: str


class ErrorResponse(BaseModel):
    """Error response."""

    detail: str
