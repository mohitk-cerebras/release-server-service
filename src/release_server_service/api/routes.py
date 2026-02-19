"""REST API routes for the release server service.

Endpoints:
  POST   /api/v1/replicas           — Create a new replica
  GET    /api/v1/replicas           — List all replicas
  GET    /api/v1/replicas/{id}      — Get status of a specific replica
  POST   /api/v1/replicas/{id}/stop — Stop a replica
  POST   /api/v1/replicas/{id}/health — Run health check on a replica
  GET    /health                     — Service-level health check
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from release_server_service.core.replica_manager import ReplicaManager
from release_server_service.models.requests import (
    CreateReplicaRequest,
    HealthCheckRequest,
    StopReplicaRequest,
)
from release_server_service.models.responses import (
    CreateReplicaResponse,
    ErrorResponse,
    HealthCheckResponse,
    ReplicaListResponse,
    ReplicaStatus,
    ReplicaStatusResponse,
    StopReplicaResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# Singleton replica manager — injected at app startup
_replica_manager: Optional[ReplicaManager] = None


def set_replica_manager(manager: ReplicaManager):
    global _replica_manager
    _replica_manager = manager


def get_manager() -> ReplicaManager:
    if _replica_manager is None:
        raise RuntimeError("ReplicaManager not initialized")
    return _replica_manager


# ─────────────────────────────────────────────────────────────
# POST /api/v1/replicas — Create a new replica
# ─────────────────────────────────────────────────────────────
@router.post(
    "/replicas",
    response_model=CreateReplicaResponse,
    status_code=201,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        422: {"model": ErrorResponse, "description": "Validation error"},
        500: {"model": ErrorResponse, "description": "Server creation failed"},
    },
)
async def create_replica(request: CreateReplicaRequest):
    """
    Create and start a new inference server replica.

    The request payload must contain all branch-specific configuration
    (full_config, replica_config, etc.) so the service remains independent
    of any monolith branch.

    Returns 201 with replica info on success.
    Returns 500 if server creation fails.
    """
    manager = get_manager()

    # Validate required params for the mode
    required = request.server_mode.get_required_params()
    missing = []
    if "multibox" in required and not request.placement.multibox:
        missing.append("placement.multibox")
    if "platform_config" in required and not request.platform_config:
        missing.append("platform_config")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required parameters for mode {request.server_mode.value}: {missing}",
        )

    try:
        replica = await manager.create_replica(request)
    except Exception as e:
        logger.exception(f"Failed to create replica: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Replica creation failed: {str(e)}",
        )

    info = replica.to_info()

    # Return appropriate status code based on outcome
    if replica.status in {ReplicaStatus.FAILED, ReplicaStatus.ERROR}:
        raise HTTPException(
            status_code=500,
            detail=f"Replica created but failed: {replica.error_message}",
        )

    return CreateReplicaResponse(
        replica_id=replica.replica_id,
        status=replica.status,
        message=f"Replica {replica.replica_id} created successfully",
        base_url=info.base_url,
        request_id=request.request_id,
    )


# ─────────────────────────────────────────────────────────────
# GET /api/v1/replicas — List all replicas
# ───────────────────────────────────────────────────────────��─
@router.get(
    "/replicas",
    response_model=ReplicaListResponse,
    status_code=200,
)
async def list_replicas(
    server_mode: Optional[str] = Query(None, description="Filter by server mode"),
    status: Optional[str] = Query(None, description="Filter by status"),
    model_name: Optional[str] = Query(None, description="Filter by model name"),
):
    """List all tracked replicas with optional filtering."""
    manager = get_manager()
    replicas = await manager.list_replicas(
        server_mode=server_mode, status=status, model_name=model_name
    )
    return ReplicaListResponse(
        total=len(replicas),
        replicas=[r.to_info() for r in replicas],
    )


# ─────────────────────────────────────────────────────────────
# GET /api/v1/replicas/{replica_id} — Get replica status
# ─────────────────────────────────────────────────────────────
@router.get(
    "/replicas/{replica_id}",
    response_model=ReplicaStatusResponse,
    status_code=200,
    responses={404: {"model": ErrorResponse}},
)
async def get_replica_status(replica_id: str):
    """Get detailed status of a specific replica."""
    manager = get_manager()
    replica = await manager.get_replica(replica_id)
    if not replica:
        raise HTTPException(status_code=404, detail=f"Replica {replica_id} not found")

    return ReplicaStatusResponse(
        replica_id=replica_id,
        status=replica.status,
        info=replica.to_info(),
    )


# ─────────────────────────────────────────────────────────────
# POST /api/v1/replicas/{replica_id}/stop — Stop a replica
# ─────────────────────────────────────────────────────────────
@router.post(
    "/replicas/{replica_id}/stop",
    response_model=StopReplicaResponse,
    status_code=200,
    responses={404: {"model": ErrorResponse}},
)
async def stop_replica(
    replica_id: str,
    request: StopReplicaRequest = StopReplicaRequest(),
):
    """Stop a running replica."""
    manager = get_manager()
    replica = await manager.stop_replica(replica_id, force=request.force)
    if not replica:
        raise HTTPException(status_code=404, detail=f"Replica {replica_id} not found")

    return StopReplicaResponse(
        replica_id=replica_id,
        status=replica.status,
        message=f"Replica {replica_id} stopped",
    )


# ─────────────────────────────────────────────────────────────
# POST /api/v1/replicas/{replica_id}/health — Health check
# ─────────────────────────────────────────────────────────────
@router.post(
    "/replicas/{replica_id}/health",
    response_model=HealthCheckResponse,
    status_code=200,
    responses={404: {"model": ErrorResponse}},
)
async def health_check_replica(
    replica_id: str,
    request: HealthCheckRequest = HealthCheckRequest(),
):
    """Run a health check on a specific replica."""
    manager = get_manager()
    replica = await manager.get_replica(replica_id)
    if not replica:
        raise HTTPException(status_code=404, detail=f"Replica {replica_id} not found")

    is_healthy = await manager.health_check_replica(
        replica_id,
        timeout_s=request.timeout_s,
        poll_interval_s=request.poll_interval_s,
    )

    if is_healthy is None:
        raise HTTPException(
            status_code=400,
            detail=f"Replica {replica_id} has no server handle or base_url",
        )

    info = replica.to_info()
    return HealthCheckResponse(
        replica_id=replica_id,
        healthy=is_healthy,
        base_url=info.base_url,
        message="healthy" if is_healthy else "unhealthy",
    )


# ─────────────────────────────────────────────────────────────
# Service-level health
# ─────────────────────────────────────────────────────────────
service_health_router = APIRouter()


@service_health_router.get("/health", status_code=200)
async def service_health():
    """Service-level health check."""
    manager = get_manager()
    total = len(manager.replicas)
    ready = sum(
        1 for r in manager.replicas.values() if r.status == ReplicaStatus.READY
    )
    return {
        "status": "ok",
        "replicas_total": total,
        "replicas_ready": ready,
    }
