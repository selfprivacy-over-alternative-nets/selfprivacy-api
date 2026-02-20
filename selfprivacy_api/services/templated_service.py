"""A Service implementation that loads all needed data from a JSON file"""

import base64
import logging
import json

from typing import List, Optional
from os.path import join, exists
from os import mkdir, remove
from opentelemetry import trace

from selfprivacy_api.utils.postgres import PostgresDumper
from selfprivacy_api.jobs import Job, JobStatus, Jobs
from selfprivacy_api.models.services import (
    License,
    ServiceDnsRecord,
    ServiceMetaData,
    ServiceStatus,
    SupportLevel,
)
from selfprivacy_api.services.flake_service_manager import FlakeServiceManager
from selfprivacy_api.services.generic_size_counter import get_storage_usage
from selfprivacy_api.services.owned_path import OwnedPath
from selfprivacy_api.services.service import Service
from selfprivacy_api.utils import ReadUserData, WriteUserData, get_domain
from selfprivacy_api.services.config_item import (
    ServiceConfigItem,
    StringServiceConfigItem,
    BoolServiceConfigItem,
    EnumServiceConfigItem,
    IntServiceConfigItem,
)
from selfprivacy_api.utils.block_devices import BlockDevice, BlockDevices
from selfprivacy_api.utils.systemd import (
    get_service_status_from_several_units,
    start_unit,
    stop_unit,
    restart_unit,
    listen_for_unit_state_changes,
)

# Tor subpath URL mapping (service_id -> nginx path)
_TOR_SERVICE_PATHS = {
    "nextcloud": "/nextcloud/",
    "gitea": "/git/",
    "jitsi-meet": "/jitsi/",
    "matrix": "/_matrix/",
    "monitoring": "/prometheus/",
    "selfprivacy-api": "/api/",
}

SP_MODULES_DEFINITIONS_PATH = "/etc/sp-modules"
SP_SUGGESTED_MODULES_PATH = "/etc/suggested-sp-modules"

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def config_item_from_json(json_data: dict) -> Optional[ServiceConfigItem]:
    """Create a ServiceConfigItem from JSON data."""
    if "meta" not in json_data:
        return None
    if "type" not in json_data["meta"]:
        return None
    weight = json_data.get("meta", {}).get("weight", 50)
    # if no meta, return None
    if json_data["meta"]["type"] == "enable":
        return None
    if json_data["meta"]["type"] == "location":
        return None
    if json_data["meta"]["type"] == "string":
        return StringServiceConfigItem(
            id=json_data["name"],
            default_value=json_data["default"],
            description=json_data["description"],
            regex=json_data["meta"].get("regex"),
            widget=json_data["meta"].get("widget"),
            allow_empty=json_data["meta"].get("allowEmpty", False),
            weight=weight,
        )
    if json_data["meta"]["type"] == "bool":
        return BoolServiceConfigItem(
            id=json_data["name"],
            default_value=json_data["default"],
            description=json_data["description"],
            widget=json_data["meta"].get("widget"),
            weight=weight,
        )
    if json_data["meta"]["type"] == "enum":
        return EnumServiceConfigItem(
            id=json_data["name"],
            default_value=json_data["default"],
            description=json_data["description"],
            options=json_data["meta"]["options"],
            widget=json_data["meta"].get("widget"),
            weight=weight,
        )
    if json_data["meta"]["type"] == "int":
        return IntServiceConfigItem(
            id=json_data["name"],
            default_value=json_data["default"],
            description=json_data["description"],
            widget=json_data["meta"].get("widget"),
            min_value=json_data["meta"].get("minValue"),
            max_value=json_data["meta"].get("maxValue"),
            weight=weight,
        )
    raise ValueError("Unknown config item type")


class TemplatedService(Service):
    """Class representing a dynamically loaded service."""

    def __init__(self, service_id: str, source_data: str) -> None:
        with tracer.start_as_current_span(
            "TemplatedService.__init__", attributes={"service_id": service_id}
        ):
            self.definition_data = json.loads(source_data)
            # Check if required fields are present
            if "meta" not in self.definition_data:
                raise ValueError("meta not found in service definition")
            if "options" not in self.definition_data:
                raise ValueError("options not found in service definition")
            # Load the meta data
            self.meta = ServiceMetaData(**self.definition_data["meta"])
            # Load the options
            self.options = self.definition_data["options"]
            # Load the config items
            self.config_items = {}
            for option in self.options.values():
                config_item = config_item_from_json(option)
                if config_item:
                    self.config_items[config_item.id] = config_item
            # If it is movable, check for the location option
            if self.meta.is_movable and "location" not in self.options:
                raise ValueError(
                    "Service is movable but does not have a location option"
                )
            # Load all subdomains via options with "subdomain" widget
            self.subdomain_options: List[str] = []
            for option in self.options.values():
                if option.get("meta", {}).get("widget") == "subdomain":
                    self.subdomain_options.append(option["name"])

    def get_id(self) -> str:
        # Check if ID contains elements that might be a part of the path
        if "/" in self.meta.id or "\\" in self.meta.id:
            raise ValueError("Invalid ID")
        return self.meta.id

    def get_display_name(self) -> str:
        return self.meta.name

    def get_description(self) -> str:
        return self.meta.description

    def get_svg_icon(self, raw=False) -> str:
        if raw:
            return self.meta.svg_icon
        return base64.b64encode(self.meta.svg_icon.encode("utf-8")).decode("utf-8")

    def get_subdomain(self) -> Optional[str]:
        # If there are no subdomain options, return None
        if not self.subdomain_options:
            return None
        # If primary_subdomain is set, try to find it in the options
        if (
            self.meta.primary_subdomain
            and self.meta.primary_subdomain in self.subdomain_options
        ):
            option_name = self.meta.primary_subdomain
        # Otherwise, use the first subdomain option
        else:
            option_name = self.subdomain_options[0]

        # Now, read the value from the userdata
        name = self.get_id()
        with ReadUserData() as user_data:
            if "modules" in user_data:
                if name in user_data["modules"]:
                    if option_name in user_data["modules"][name]:
                        return user_data["modules"][name][option_name]
        # Otherwise, return default value for the option
        return self.options[option_name].get("default")

    def get_subdomains(self) -> List[str]:
        # Return a current subdomain for every subdomain option
        subdomains = []
        with ReadUserData() as user_data:
            for option in self.subdomain_options:
                if "modules" in user_data:
                    if self.get_id() in user_data["modules"]:
                        if option in user_data["modules"][self.get_id()]:
                            subdomains.append(
                                user_data["modules"][self.get_id()][option]
                            )
                            continue
                subdomains.append(self.options[option]["default"])
        return subdomains

    def get_url(self) -> Optional[str]:
        if not self.meta.showUrl:
            return None
        domain = get_domain()
        if domain and domain.endswith(".onion"):
            path = _TOR_SERVICE_PATHS.get(self.get_id())
            if path:
                return f"https://{domain}{path}"
        subdomain = self.get_subdomain()
        if not subdomain:
            return None
        return f"https://{subdomain}.{domain}"

    def get_user(self) -> Optional[str]:
        if not self.meta.user:
            return self.get_id()
        return self.meta.user

    def get_group(self) -> Optional[str]:
        if not self.meta.group:
            return self.get_user()
        return self.meta.group

    def is_movable(self) -> bool:
        return self.meta.is_movable

    def is_required(self) -> bool:
        return self.meta.is_required

    def can_be_backed_up(self) -> bool:
        return self.meta.can_be_backed_up

    def get_backup_description(self) -> str:
        return self.meta.backup_description

    def is_enabled(self) -> bool:
        name = self.get_id()
        with ReadUserData() as user_data:
            return user_data.get("modules", {}).get(name, {}).get("enable", False)

    def is_installed(self) -> bool:
        name = self.get_id()
        with FlakeServiceManager() as service_manager:
            return name in service_manager.services

    def get_license(self) -> List[License]:
        return self.meta.license

    def get_homepage(self) -> Optional[str]:
        return self.meta.homepage

    def get_source_page(self) -> Optional[str]:
        return self.meta.source_page

    def get_support_level(self) -> SupportLevel:
        return self.meta.support_level

    def get_sso_user_group(self) -> Optional[str]:
        if not self.meta.sso:
            return None
        return self.meta.sso.user_group

    def get_sso_admin_group(self) -> Optional[str]:
        if not self.meta.sso:
            return None
        return self.meta.sso.admin_group

    async def get_status(self) -> ServiceStatus:
        if not self.meta.systemd_services:
            return ServiceStatus.INACTIVE
        return await get_service_status_from_several_units(self.meta.systemd_services)

    async def wait_for_statuses(self, expected_statuses: List[ServiceStatus]):
        if (await self.get_status()) in expected_statuses:
            return

        async for _ in listen_for_unit_state_changes(self.meta.systemd_services):
            if (await self.get_status()) in expected_statuses:
                return

    def _set_enable(self, enable: bool):
        name = self.get_id()
        with WriteUserData() as user_data:
            if "modules" not in user_data:
                user_data["modules"] = {}
            if name not in user_data["modules"]:
                user_data["modules"][name] = {}
            user_data["modules"][name]["enable"] = enable

    def enable(self):
        """Enable the service. Usually this means enabling systemd unit."""
        name = self.get_id()
        if not self.is_installed():
            # First, double-check that it is a suggested module
            if exists(SP_SUGGESTED_MODULES_PATH):
                with open(SP_SUGGESTED_MODULES_PATH) as file:
                    suggested_modules = json.load(file)
                if name not in suggested_modules:
                    raise ValueError("Service is not a suggested module")
            else:
                raise FileNotFoundError("Suggested modules file not found")
            with FlakeServiceManager() as service_manager:
                service_manager.services[name] = (
                    f"git+https://git.selfprivacy.org/SelfPrivacy/selfprivacy-nixos-config.git?ref=flakes&dir=sp-modules/{name}"
                )
        if "location" in self.options:
            with WriteUserData() as user_data:
                if "modules" not in user_data:
                    user_data["modules"] = {}
                if name not in user_data["modules"]:
                    user_data["modules"][name] = {}
                if "location" not in user_data["modules"][name]:
                    user_data["modules"][name]["location"] = (
                        BlockDevices().get_root_block_device().canonical_name
                    )

        self._set_enable(True)

    def disable(self):
        """Disable the service. Usually this means disabling systemd unit."""
        self._set_enable(False)

    async def start(self):
        """Start the systemd units"""
        for unit in self.meta.systemd_services:
            await start_unit(unit)

    async def stop(self):
        """Stop the systemd units"""
        for unit in self.meta.systemd_services:
            await stop_unit(unit)

    async def restart(self):
        """Restart the systemd units"""
        for unit in self.meta.systemd_services:
            await restart_unit(unit)

    def get_configuration(self) -> dict:
        # If there are no options, return an empty dict
        if not self.config_items:
            return {}
        return {
            key: self.config_items[key].as_dict(self.get_id())
            for key in self.config_items
        }

    def set_configuration(self, config_items):
        for key, value in config_items.items():
            if key not in self.config_items:
                raise ValueError(f"Key {key} is not valid for {self.get_id()}")
            if self.config_items[key].validate_value(value) is False:
                raise ValueError(f"Value {value} is not valid for {key}")
        for key, value in config_items.items():
            self.config_items[key].set_value(
                value,
                self.get_id(),
            )

    async def get_storage_usage(self) -> int:
        """
        Calculate the real storage usage of folders occupied by service
        Calculate using pathlib.
        Do not follow symlinks.
        """
        storage_used = 0
        for folder in self.get_folders():
            storage_used += await get_storage_usage(folder)
        return storage_used

    def has_folders(self) -> int:
        """
        If there are no folders on disk, moving is noop
        """
        for folder in self.get_folders():
            if exists(folder):
                return True
        return False

    def get_dns_records(self, ip4: str, ip6: Optional[str]) -> List[ServiceDnsRecord]:
        display_name = self.get_display_name()
        subdomains = self.get_subdomains()
        # Generate records for every subdomain
        records: List[ServiceDnsRecord] = []
        for subdomain in subdomains:
            if not subdomain:
                continue
            records.append(
                ServiceDnsRecord(
                    type="A",
                    name=subdomain,
                    content=ip4,
                    ttl=3600,
                    display_name=display_name,
                )
            )
            if ip6:
                records.append(
                    ServiceDnsRecord(
                        type="AAAA",
                        name=subdomain,
                        content=ip6,
                        ttl=3600,
                        display_name=display_name,
                    )
                )
        return records

    def get_drive(self) -> str:
        """
        Get the name of the drive/volume where the service is located.
        Example values are `sda1`, `vda`, `sdb`.
        """
        root_device: str = BlockDevices().get_root_block_device().canonical_name
        if not self.is_movable():
            return root_device
        with ReadUserData() as userdata:
            if userdata.get("useBinds", False):
                return (
                    userdata.get("modules", {})
                    .get(self.get_id(), {})
                    .get(
                        "location",
                        root_device,
                    )
                )
            else:
                return root_device

    def _get_db_dumps_folder(self) -> str:
        # Get the drive where the service is located and append the folder name
        return join("/var/lib/postgresql-dumps", self.get_id())

    def get_folders(self) -> List[str]:
        folders = self.meta.folders
        owned_folders = self.meta.owned_folders
        # Include the contents of folders list
        resulting_folders = folders.copy()
        for folder in owned_folders:
            resulting_folders.append(folder.path)
        return resulting_folders

    def get_owned_folders(self) -> List[OwnedPath]:
        folders = self.meta.folders
        owned_folders = self.meta.owned_folders
        resulting_folders = owned_folders.copy()
        for folder in folders:
            resulting_folders.append(self.owned_path(folder))
        return resulting_folders

    def get_folders_to_back_up(self) -> List[str]:
        resulting_folders = self.meta.folders.copy()
        if self.get_postgresql_databases():
            resulting_folders.append(self._get_db_dumps_folder())
        return resulting_folders

    def set_location(self, volume: BlockDevice):
        """
        Only changes userdata
        """

        service_id = self.get_id()
        with WriteUserData() as user_data:
            if "modules" not in user_data:
                user_data["modules"] = {}
            if service_id not in user_data["modules"]:
                user_data["modules"][service_id] = {}
            user_data["modules"][service_id]["location"] = volume.name

    def get_postgresql_databases(self) -> List[str]:
        return self.meta.postgre_databases

    def owned_path(self, path: str):
        """Default folder ownership"""
        service_name = self.get_display_name()

        try:
            owner = self.get_user()
            if owner is None:
                # TODO: assume root?
                # (if we do not want to do assumptions, maybe not declare user optional?)
                raise LookupError(f"no user for service: {service_name}")
            group = self.get_group()
            if group is None:
                raise LookupError(f"no group for service: {service_name}")
        except Exception as error:
            raise LookupError(
                f"when deciding a bind for folder {path} of service {service_name}, error: {str(error)}"
            )

        return OwnedPath(
            path=path,
            owner=owner,
            group=group,
        )

    def pre_backup(self, job: Job):
        if self.get_postgresql_databases():
            db_dumps_folder = self._get_db_dumps_folder()
            # Create folder for the dumps if it does not exist
            if not exists(db_dumps_folder):
                mkdir(db_dumps_folder)
            # Dump the databases
            for db_name in self.get_postgresql_databases():
                Jobs.update(
                    job,
                    status_text=f"Creating a dump of database {db_name}",
                    status=JobStatus.RUNNING,
                )
                db_dumper = PostgresDumper(db_name)
                backup_file = join(db_dumps_folder, f"{db_name}.dump")
                db_dumper.backup_database(backup_file)

    def _clear_db_dumps(self):
        db_dumps_folder = self._get_db_dumps_folder()
        for db_name in self.get_postgresql_databases():
            backup_file = join(db_dumps_folder, f"{db_name}.dump")
            if exists(backup_file):
                remove(backup_file)
            unpacked_file = backup_file.replace(".gz", "")
            if exists(unpacked_file):
                remove(unpacked_file)

    def post_backup(self, job: Job):
        if self.get_postgresql_databases():
            db_dumps_folder = self._get_db_dumps_folder()
            # Remove the backup files
            for db_name in self.get_postgresql_databases():
                backup_file = join(db_dumps_folder, f"{db_name}.dump")
                if exists(backup_file):
                    remove(backup_file)

    def pre_restore(self, job: Job):
        if self.get_postgresql_databases():
            # Create folder for the dumps if it does not exist
            db_dumps_folder = self._get_db_dumps_folder()
            if not exists(db_dumps_folder):
                mkdir(db_dumps_folder)
            # Remove existing dumps if they exist
            self._clear_db_dumps()

    async def post_restore(self, job: Job):
        if self.get_postgresql_databases():
            # Recover the databases
            db_dumps_folder = self._get_db_dumps_folder()
            for db_name in self.get_postgresql_databases():
                if exists(join(db_dumps_folder, f"{db_name}.dump")):
                    Jobs.update(
                        job,
                        status_text=f"Restoring database {db_name}",
                        status=JobStatus.RUNNING,
                    )
                    db_dumper = PostgresDumper(db_name)
                    backup_file = join(db_dumps_folder, f"{db_name}.dump")
                    db_dumper.restore_database(backup_file)
                else:
                    logger.error(f"Database dump for {db_name} not found")
                    raise FileNotFoundError(f"Database dump for {db_name} not found")
        # Remove the dumps
        self._clear_db_dumps()
