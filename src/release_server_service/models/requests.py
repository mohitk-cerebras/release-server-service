"""
REST API request payload schemas.

All branch-specific or replica-specific dependencies are passed
as part of the request payload — the service itself is independent.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .server_modes import ServerMode


class PlacementConfig(BaseModel):
    """Where to deploy the server."""

    multibox: str = Field(..., description="Target multibox/cluster name")
    usernode: Optional[str] = Field(None, description="Target usernode hostname")
    namespace: Optional[str] = Field(
        "inf-integ", description="Kubernetes namespace"
    )
    app_tag: Optional[str] = Field(None, description="Application tag for deployment")
    remote_workdir: Optional[str] = Field(
        None, description="Pre-existing remote workdir path"
    )
    remote_workdir_root: Optional[str] = Field(
        None, description="Root for auto-creating remote workdirs"
    )


class ReplicaConfig(BaseModel):
    """Replica-specific configuration (branch-dependent, comes from caller)."""

    replica_config: Optional[Dict[str, Any]] = Field(
        None,
        description="Replica config to apply via /config/replica endpoint",
    )
    api_config: Optional[Dict[str, Any]] = Field(
        None,
        description="API server config to apply via /config/api endpoint",
    )


class PlatformConfig(BaseModel):
    """Platform workload specific configuration."""

    release_label: Optional[str] = None
    control_plane_namespace: str = "inf-platform-cp-blue"
    job_namespace: Optional[str] = None
    deployment_host: Optional[str] = None
    dataplane_mgmt_node: Optional[str] = None
    api_gateway_url: Optional[str] = None
    inject_backend_header: bool = False
    workload_name: Optional[str] = None
    workload_image_tag: Optional[str] = None
    use_kubectl: bool = False
    reconfigure_api_via_workload: bool = False
    platform_remote_workdir: Optional[str] = None


class GatewayConfig(BaseModel):
    """API Gateway specific configuration."""

    mock_backend: bool = False
    extra: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Additional gateway-specific config passed through to the handle",
    )


class CatalogConfig(BaseModel):
    """Model catalog configuration."""

    catalog_id_suffix: Optional[str] = "release_qual"
    catalog_pt_client_version: Optional[str] = None
    catalog_tokenizer_path: Optional[str] = None


class JobConfig(BaseModel):
    """Job scheduling configuration."""

    job_priority: str = "p2"
    job_timeout_s: int = 86400  # 24 hours
    job_labels: Optional[List[str]] = None
    disable_scheduler: bool = False


class TimeoutConfig(BaseModel):
    """Timeout settings for server startup."""

    port_discovery_timeout_s: Optional[int] = None
    readiness_timeout_s: Optional[int] = None
    poll_interval_s: Optional[int] = None
    health_check_timeout_s: int = 120


class InfluxDBConfig(BaseModel):
    """InfluxDB configuration for metrics collection."""

    use_influxdb: bool = False
    influxdb_local: bool = False
    data_dir: Optional[str] = None
    host: Optional[str] = None


class CreateReplicaRequest(BaseModel):
    """
    Complete request payload for creating a server replica.

    All branch-specific configuration is provided here so the service
    remains independent of any monolith branch.
    """

    # Server identity
    server_mode: ServerMode = Field(
        ..., description="Deployment mode for the server"
    )
    model_name: str = Field(..., description="Model name to serve")

    # Full model configuration — branch-specific, comes from the caller
    full_config: Dict[str, Any] = Field(
        ...,
        description=(
            "Complete model configuration dict (model, runconfig, api_config). "
            "This is branch-specific and must be provided by the caller."
        ),
    )

    # Placement
    placement: PlacementConfig

    # Server-type specific configs
    replica_config: Optional[ReplicaConfig] = None
    platform_config: Optional[PlatformConfig] = None
    gateway_config: Optional[GatewayConfig] = None
    catalog_config: Optional[CatalogConfig] = None

    # Job and timeout settings
    job: JobConfig = Field(default_factory=JobConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    influxdb: Optional[InfluxDBConfig] = None

    # Behavior flags
    wait_for_ready: bool = Field(
        True, description="Block until server is healthy before returning"
    )
    run_diagnostics: bool = Field(
        True, description="Run diagnostics after server is ready"
    )

    # Caller identity
    invoking_user: Optional[str] = Field(
        None, description="User identity for job labeling"
    )
    request_id: Optional[str] = Field(
        None,
        description="Optional caller-provided request ID for correlation",
    )


class StopReplicaRequest(BaseModel):
    """Request to stop a specific replica."""

    force: bool = Field(False, description="Force stop without graceful shutdown")


class HealthCheckRequest(BaseModel):
    """Request to perform a health check on a replica."""

    timeout_s: int = Field(120, description="Health check timeout in seconds")
    poll_interval_s: int = Field(5, description="Poll interval in seconds")
