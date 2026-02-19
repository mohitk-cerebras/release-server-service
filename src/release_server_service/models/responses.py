"""
REST API response schemas."""

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
    base_url: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    multibox: Optional[str] = None
    namespace: Optional[str] = None
    usernode: Optional[str] = None
    workdir: Optional[str] = None
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
    """Response for a replica status query."""

    replica_id: str
    status: ReplicaStatus
    info: ReplicaInfo


class ReplicaListResponse(BaseModel):
    """Response listing all tracked replicas."""

    total: int
    replicas: List[ReplicaInfo]


class StopReplicaResponse(BaseModel):
    """Response after stopping a replica."""

    replica_id: str
    status: ReplicaStatus
    message: str


class HealthCheckResponse(BaseModel):
    """Response for a health check request."""

    replica_id: str
    healthy: bool
    base_url: Optional[str] = None
    message: str


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: Optional[str] = None
    replica_id: Optional[str] = None
    status_code: int
