"""
Independent ServerMode enum — NOT tied to the monolith codebase.

This enum represents the supported server deployment modes.
Branch-specific behavior is driven by the REST request payload,
not by importing monolith code.
"""

from enum import Enum
from typing import Set


class ServerMode(str, Enum):
    """Supported server deployment modes."""

    REPLICA = "replica"
    REPLICA_MOCK = "replica_mock"
    API_GATEWAY = "api_gateway"
    API_GATEWAY_MOCK = "api_gateway_mock"
    PLATFORM_WORKLOAD = "platform_workload"
    PLATFORM_WORKLOAD_MOCK = "platform_workload_mock"

    @property
    def is_mock(self) -> bool:
        return self in {
            self.REPLICA_MOCK,
            self.API_GATEWAY_MOCK,
            self.PLATFORM_WORKLOAD_MOCK,
        }

    @property
    def is_replica(self) -> bool:
        return self in {self.REPLICA, self.REPLICA_MOCK}

    @property
    def is_api_gateway(self) -> bool:
        return self in {self.API_GATEWAY, self.API_GATEWAY_MOCK}

    @property
    def is_platform_workload(self) -> bool:
        return self in {self.PLATFORM_WORKLOAD, self.PLATFORM_WORKLOAD_MOCK}

    @property
    def requires_multibox(self) -> bool:
        return True  # All supported modes require a multibox

    def get_required_params(self) -> Set[str]:
        required = {"multibox", "model_name", "full_config"}
        if self.is_platform_workload:
            required.add("platform_config")
        return required
