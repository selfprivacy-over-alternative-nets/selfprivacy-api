"""Class representing Nextcloud service."""

import base64
import subprocess
from typing import Optional, List

from selfprivacy_api.services.owned_path import OwnedPath
from selfprivacy_api.utils.systemd import (
    get_service_status,
    start_unit,
    stop_unit,
    restart_unit,
    wait_for_unit_state,
)
from selfprivacy_api.services.service import Service, ServiceStatus
from selfprivacy_api.utils import get_domain

from selfprivacy_api.services.prometheus.icon import PROMETHEUS_ICON


class Prometheus(Service):
    """Class representing Prometheus service."""

    @staticmethod
    def get_id() -> str:
        return "monitoring"

    @staticmethod
    def get_display_name() -> str:
        return "Prometheus"

    @staticmethod
    def get_description() -> str:
        return "Prometheus is used for resource monitoring and alerts."

    @staticmethod
    def get_svg_icon(raw=False) -> str:
        if raw:
            return PROMETHEUS_ICON
        return base64.b64encode(PROMETHEUS_ICON.encode("utf-8")).decode("utf-8")

    @staticmethod
    def get_url() -> Optional[str]:
        """Return service url."""
        domain = get_domain()
        if domain and domain.endswith(".onion"):
            return f"https://{domain}/prometheus/"
        return None

    @staticmethod
    def get_subdomain() -> Optional[str]:
        return None

    @staticmethod
    def is_movable() -> bool:
        return False

    @staticmethod
    def is_required() -> bool:
        return True

    @staticmethod
    def is_system_service() -> bool:
        return True

    @staticmethod
    def can_be_backed_up() -> bool:
        return False

    @staticmethod
    def get_backup_description() -> str:
        return "Backups are not available for Prometheus."

    @staticmethod
    async def get_status() -> ServiceStatus:
        return await get_service_status("prometheus.service")

    @staticmethod
    async def wait_for_statuses(self, expected_statuses: List[ServiceStatus]):
        await wait_for_unit_state("prometheus.service", expected_statuses)

    @staticmethod
    async def stop():
        await stop_unit("prometheus.service")
        subprocess.run(["systemctl", "stop", "prometheus.service"])

    @staticmethod
    async def start():
        await start_unit("prometheus.service")

    @staticmethod
    async def restart():
        await restart_unit("prometheus.service")

    @staticmethod
    def get_owned_folders() -> List[OwnedPath]:
        return [
            OwnedPath(
                path="/var/lib/prometheus",
                owner="prometheus",
                group="prometheus",
            ),
        ]
