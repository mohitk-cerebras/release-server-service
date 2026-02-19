"""
Server factory — creates the appropriate server handle based on mode.

This module mirrors the logic of `_create_server_handle()` in
`server_management.py` but is invoked via REST payloads rather than
pytest fixtures. Branch-specific dependencies (full_config, replica_config,
etc.) are received in the request body.

IMPORTANT: This module imports from the monolith's inference_server_ctl
package at runtime. Those packages must be installed in the container image.
The key difference is that this service does NOT depend on any specific
monolith branch at build time — the caller provides all branch-specific
configuration via the REST payload.
"""

import logging
import os
from typing import Any, Dict, Optional

from release_server_service.models.requests import CreateReplicaRequest
from release_server_service.models.server_modes import ServerMode

logger = logging.getLogger(__name__)


async def create_server_handle(
    request: CreateReplicaRequest,
    local_workdir: str,
) -> Any:
    """
    Create an inference server handle based on the request mode.

    This function dynamically imports from monolith's inference_server_ctl
    package. The package must be installed in the container, but the
    branch-specific model configuration comes from the REST payload.

    Args:
        request: The full create-replica request payload
        local_workdir: Local working directory for this replica

    Returns:
        An InferenceServerHandle instance

    Raises:
        ValueError: If unsupported server mode or missing required params
        ImportError: If monolith inference_server_ctl is not installed
    """
    mode = request.server_mode
    model_name = request.model_name
    full_config = request.full_config
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

    if mode.is_replica:
        return await _create_replica_handle(
            model_name=model_name,
            local_workdir=local_workdir,
            full_config=full_config,
            placement=placement,
            job=request.job,
            influxdb_params=influxdb_params,
            mock_backend=mode.is_mock,
        )
    elif mode.is_api_gateway:
        return await _create_api_gateway_handle(
            model_name=model_name,
            local_workdir=local_workdir,
            full_config=full_config,
            placement=placement,
            job=request.job,
            influxdb_params=influxdb_params,
            gateway_config=request.gateway_config,
            mock_backend=mode.is_mock,
        )
    elif mode.is_platform_workload:
        return await _create_platform_workload_handle(
            model_name=model_name,
            local_workdir=local_workdir,
            full_config=full_config,
            placement=placement,
            job=request.job,
            influxdb_params=influxdb_params,
            platform_config=request.platform_config,
            catalog_config=request.catalog_config,
            mock_backend=mode.is_mock,
        )
    else:
        raise ValueError(f"Unsupported server mode: {mode}")


async def _create_replica_handle(
    model_name: str,
    local_workdir: str,
    full_config: Dict[str, Any],
    placement,
    job,
    influxdb_params: Optional[Dict],
    mock_backend: bool,
):
    """Create a FastAPI replica server handle."""
    from cerebras.regress.common.integration.cif.inference_server_ctl import (
        FastAPIServerHandle,
    )

    logger.info(
        f"Creating FastAPI replica for model {model_name} "
        f"(mock_backend={mock_backend})"
    )
    return await FastAPIServerHandle.create(
        model=model_name,
        workdir=local_workdir,
        multibox=placement.multibox,
        usernode=placement.usernode,
        namespace=placement.namespace,
        app_tag=placement.app_tag,
        remote_workdir=placement.remote_workdir,
        remote_workdir_root=placement.remote_workdir_root,
        full_config=full_config,
        job_priority=job.job_priority,
        job_timeout_s=job.job_timeout_s,
        influxdb_params=influxdb_params,
        mock_backend=mock_backend,
        disable_scheduler=job.disable_scheduler,
    )


async def _create_api_gateway_handle(
    model_name: str,
    local_workdir: str,
    full_config: Dict[str, Any],
    placement,
    job,
    influxdb_params: Optional[Dict],
    gateway_config,
    mock_backend: bool,
):
    """Create an API Gateway server handle."""
    from cerebras.regress.common.integration.cif.inference_server_ctl import (
        APIGatewayServerHandle,
    )

    gw_extra = {}
    gw_mock = mock_backend
    if gateway_config:
        gw_mock = gateway_config.mock_backend or mock_backend
        gw_extra = gateway_config.extra or {}

    logger.info(
        f"Creating API Gateway for model {model_name} "
        f"(mock_backend={gw_mock})"
    )
    return await APIGatewayServerHandle.create(
        model=model_name,
        workdir=local_workdir,
        multibox=placement.multibox,
        usernode=placement.usernode,
        namespace=placement.namespace,
        app_tag=placement.app_tag,
        remote_workdir=placement.remote_workdir,
        remote_workdir_root=placement.remote_workdir_root,
        host=APIGatewayServerHandle.get_external_ip(),
        port=0,
        influxdb_params=influxdb_params,
        mock_backend=gw_mock,
        full_config=full_config,
        disable_scheduler=job.disable_scheduler,
        **gw_extra,
    )


async def _create_platform_workload_handle(
    model_name: str,
    local_workdir: str,
    full_config: Dict[str, Any],
    placement,
    job,
    influxdb_params: Optional[Dict],
    platform_config,
    catalog_config,
    mock_backend: bool,
):
    """Create a Platform Workload server handle."""
    from cerebras.regress.common.integration.cif.inference_server_ctl import (
        WorkloadServerHandle,
    )

    if not platform_config:
        raise ValueError("platform_config is required for PLATFORM_WORKLOAD mode")

    cat_cfg = catalog_config or {}
    cat_suffix = getattr(cat_cfg, "catalog_id_suffix", "release_qual")
    cat_pt_ver = getattr(cat_cfg, "catalog_pt_client_version", None)
    cat_tok = getattr(cat_cfg, "catalog_tokenizer_path", None)

    logger.info(
        f"Creating Platform Workload for model {model_name} "
        f"(mock_backend={mock_backend})"
    )
    return await WorkloadServerHandle.create(
        model=model_name,
        workdir=local_workdir,
        multibox=placement.multibox,
        usernode=placement.usernode,
        namespace=placement.namespace,
        app_tag=placement.app_tag,
        remote_workdir=placement.remote_workdir,
        remote_workdir_root=placement.remote_workdir_root,
        job_priority=job.job_priority,
        # Platform params
        platform_release_label=platform_config.release_label,
        platform_control_plane_namespace=platform_config.control_plane_namespace,
        platform_job_namespace=platform_config.job_namespace or placement.namespace,
        platform_deployment_host=platform_config.deployment_host,
        platform_remote_workdir=platform_config.platform_remote_workdir,
        platform_dataplane_mgmt_node=platform_config.dataplane_mgmt_node,
        api_gateway_url=platform_config.api_gateway_url,
        inject_backend_header=platform_config.inject_backend_header,
        workload_name=platform_config.workload_name,
        prebuilt_image=platform_config.workload_image_tag,
        full_config=full_config,
        mock_backend=mock_backend,
        catalog_id_suffix=cat_suffix,
        catalog_pt_client_version=cat_pt_ver,
        catalog_tokenizer_path=cat_tok,
        use_kubectl=platform_config.use_kubectl,
        reconfigure_api_via_workload=platform_config.reconfigure_api_via_workload,
    )
