"""service-level configuration and settings."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ServiceConfig:
    """Global service configuration loaded from environment variables."""

    # Service settings
    host: str = field(default_factory=lambda: os.getenv("SERVICE_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("SERVICE_PORT", "8080")))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Workdir settings
    local_workdir_root: str = field(
        default_factory=lambda: os.getenv("LOCAL_WORKDIR_ROOT", "/tmp/release-server-service")
    )
    remote_workdir_root: str = field(
        default_factory=lambda: os.getenv("REMOTE_WORKDIR_ROOT", "/n0/lab/tests")
    )

    # Defaults
    default_health_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("DEFAULT_HEALTH_TIMEOUT_S", "120"))
    )
    default_poll_interval_s: int = field(
        default_factory=lambda: int(os.getenv("DEFAULT_POLL_INTERVAL_S", "5"))
    )
    default_readiness_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("DEFAULT_READINESS_TIMEOUT_S", "1800"))
    )
    default_port_discovery_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("DEFAULT_PORT_DISCOVERY_TIMEOUT_S", "600"))
    )


def get_config() -> ServiceConfig:
    return ServiceConfig()
