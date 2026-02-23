"""Release Server Service — FastAPI application entry point.

This is a standalone REST API service that manages inference server replicas.
It is NOT tied to any specific monolith branch — all branch-specific
dependencies (model configs, replica configs, etc.) come via REST payloads.

Usage:
    # Direct
    uvicorn release_server_service.main:app --host 0.0.0.0 --port 8080

    # Docker
    docker build -t release-server-service .
    docker run -p 8080:8080 release-server-service
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from release_server_service.api.routes import (
    router as api_router,
    service_health_router,
    set_replica_manager,
)
from release_server_service.config import get_config
from release_server_service.core.replica_manager_v2 import ReplicaManagerV2

# Configure logging
config = get_config()
logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # Startup
    logger.info("Starting Release Server Service (Worker-Process Architecture)...")
    manager = ReplicaManagerV2(config=config)
    set_replica_manager(manager)

    # Start background monitoring task
    manager.start_monitoring()

    logger.info(
        f"Service ready on {config.host}:{config.port} "
        f"(workdir_root={config.local_workdir_root})"
    )
    logger.info("Worker-process mode: Replicas run in independent processes")
    yield
    # Shutdown
    # NOTE: In detached mode, we do NOT kill replicas on REST server shutdown
    # This allows wsjobs to continue running on the cluster even if REST server is restarted
    logger.info("Shutting down REST server...")

    # Optionally call cleanup if config.cleanup_on_shutdown is True
    if getattr(config, 'cleanup_on_shutdown', False):
        logger.warning("cleanup_on_shutdown=True: Stopping all replicas and canceling wsjobs...")
        await manager.cleanup_all()
    else:
        logger.info("Detached mode: Stopping monitoring but leaving replicas running")
        await manager.stop_monitoring()
        logger.info("Replicas continue running. Use DELETE /replicas/{id} to stop them.")

    logger.info("Shutdown complete.")


app = FastAPI(
    title="Release Server Service",
    description=(
        "REST API service for managing Cerebras inference server replicas. "
        "Supports replica, api_gateway, and platform_workload modes. "
        "All branch-specific configuration is provided via request payloads."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)
app.include_router(service_health_router)
