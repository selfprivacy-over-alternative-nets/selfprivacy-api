"""Abstract class for a service running on a server"""

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import List, Optional
from os.path import exists

from selfprivacy_api import utils
from selfprivacy_api.services.config_item import ServiceConfigItem
from selfprivacy_api.utils.default_subdomains import DEFAULT_SUBDOMAINS
from selfprivacy_api.utils import ReadUserData, WriteUserData, get_domain
from selfprivacy_api.utils.block_devices import BlockDevice, BlockDevices

from selfprivacy_api.jobs import Job, Jobs, JobStatus, report_progress
from selfprivacy_api.jobs.upgrade_system import rebuild_system

from selfprivacy_api.models.services import (
    License,
    ServiceStatus,
    ServiceDnsRecord,
    SupportLevel,
)
from selfprivacy_api.services.generic_size_counter import get_storage_usage
from selfprivacy_api.services.owned_path import OwnedPath, Bind
from selfprivacy_api.services.moving import (
    check_binds,
    check_volume,
    unbind_folders,
    bind_folders,
    ensure_folder_ownership,
    MoveError,
    move_data_to_volume,
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


DEFAULT_START_STOP_TIMEOUT = 5 * 60

logger = logging.getLogger(__name__)


class Service(ABC):
    """
    Service here is some software that is hosted on the server and
    can be installed, configured and used by a user.
    """

    config_items: dict[str, "ServiceConfigItem"] = {}

    @staticmethod
    @abstractmethod
    def get_id() -> str:
        """
        The unique id of the service.
        """
        pass

    @staticmethod
    @abstractmethod
    def get_display_name() -> str:
        """
        The name of the service that is shown to the user.
        """
        pass

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """
        The description of the service that is shown to the user.
        """
        pass

    @staticmethod
    @abstractmethod
    def get_svg_icon(raw=False) -> str:
        """
        The monochrome svg icon of the service.
        """
        pass

    @classmethod
    def get_url(cls) -> Optional[str]:
        """
        The url of the service if it is accessible from the internet browser.
        For .onion domains, returns subpath-based URLs matching nginx config.
        """
        domain = get_domain()
        if domain and domain.endswith(".onion"):
            path = _TOR_SERVICE_PATHS.get(cls.get_id())
            if path:
                return f"https://{domain}{path}"
        subdomain = cls.get_subdomain()
        return f"https://{subdomain}.{domain}"

    @classmethod
    def get_subdomain(cls) -> Optional[str]:
        """
        The assigned primary subdomain for this service.
        """
        name = cls.get_id()
        with ReadUserData() as user_data:
            if "modules" in user_data:
                if name in user_data["modules"]:
                    if "subdomain" in user_data["modules"][name]:
                        return user_data["modules"][name]["subdomain"]

        return DEFAULT_SUBDOMAINS.get(name)

    @classmethod
    def get_user(cls) -> Optional[str]:
        """
        The user that owns the service's files.
        Defaults to the service's id.
        """
        return cls.get_id()

    @classmethod
    def get_group(cls) -> Optional[str]:
        """
        The group that owns the service's files.
        Defaults to the service's user.
        """
        return cls.get_user()

    @staticmethod
    def is_always_active() -> bool:
        """`True` if the service cannot be stopped, which is true for api itself"""
        return False

    @staticmethod
    @abstractmethod
    def is_movable() -> bool:
        """`True` if the service can be moved to the non-system volume."""
        pass

    @staticmethod
    @abstractmethod
    def is_required() -> bool:
        """`True` if the service is required for the server to function."""
        pass

    @staticmethod
    def can_be_backed_up() -> bool:
        """`True` if the service can be backed up."""
        return True

    @staticmethod
    @abstractmethod
    def get_backup_description() -> str:
        """
        The text shown to the user that exlplains what data will be
        backed up.
        """
        pass

    @classmethod
    def is_enabled(cls) -> bool:
        """
        `True` if the service is enabled.
        `False` if it is not enabled or not defined in file
        If there is nothing in the file, this is equivalent to False
        because NixOS won't enable it then.
        """
        name = cls.get_id()
        with ReadUserData() as user_data:
            return user_data.get("modules", {}).get(name, {}).get("enable", False)

    @classmethod
    def is_installed(cls) -> bool:
        """
        `True` if the service is installed.
        `False` if there is no module data in user data
        """
        name = cls.get_id()
        with ReadUserData() as user_data:
            return user_data.get("modules", {}).get(name, {}) != {}

    def is_system_service(self) -> bool:
        """
        `True` if the service is a system service and should be hidden from the user.
        `False` if it is not a system service.
        """
        return False

    def get_license(self) -> List[License]:
        """
        The licenses of the service.
        """
        return []

    def get_homepage(self) -> Optional[str]:
        """
        The homepage of the service.
        """
        return None

    def get_source_page(self) -> Optional[str]:
        """
        The source page of the service.
        """
        return None

    def get_support_level(self) -> SupportLevel:
        """
        The support level of the service.
        """
        return SupportLevel.NORMAL

    def get_sso_user_group(self) -> Optional[str]:
        """
        The access group for Single Sign On.
        """
        return None

    def get_sso_admin_group(self) -> Optional[str]:
        """
        The admin group for Single Sign On.
        """
        return None

    @staticmethod
    @abstractmethod
    async def get_status() -> ServiceStatus:
        """The status of the service, reported by systemd."""
        pass

    @staticmethod
    @abstractmethod
    async def wait_for_statuses(expected_statuses: List[ServiceStatus]):
        """Asynchronously wait for active to became one of expected_statuses. Should be cancellable"""
        pass

    @classmethod
    def _set_enable(cls, enable: bool):
        name = cls.get_id()
        with WriteUserData() as user_data:
            if "modules" not in user_data:
                user_data["modules"] = {}
            if name not in user_data["modules"]:
                user_data["modules"][name] = {}
            user_data["modules"][name]["enable"] = enable

    @classmethod
    def enable(cls):
        """Enable the service. Usually this means enabling systemd unit."""
        cls._set_enable(True)

    @classmethod
    def disable(cls):
        """Disable the service. Usually this means disabling systemd unit."""
        cls._set_enable(False)

    @staticmethod
    @abstractmethod
    async def stop():
        """Stop the service. Usually this means stopping systemd unit."""
        pass

    @staticmethod
    @abstractmethod
    async def start():
        """Start the service. Usually this means starting systemd unit."""
        pass

    @staticmethod
    @abstractmethod
    async def restart():
        """Restart the service. Usually this means restarting systemd unit."""
        pass

    @classmethod
    def get_configuration(cls):
        return {
            key: cls.config_items[key].as_dict(cls.get_id()) for key in cls.config_items
        }

    @classmethod
    def set_configuration(cls, config_items):
        for key, value in config_items.items():
            if key not in cls.config_items:
                raise ValueError(f"Key {key} is not valid for {cls.get_id()}")
            if cls.config_items[key].validate_value(value) is False:
                raise ValueError(f"Value {value} is not valid for {key}")
        for key, value in config_items.items():
            cls.config_items[key].set_value(
                value,
                cls.get_id(),
            )

    @classmethod
    async def get_storage_usage(cls) -> int:
        """
        Calculate the real storage usage of folders occupied by service
        Calculate using pathlib.
        Do not follow symlinks.
        """
        storage_used = 0
        for folder in cls.get_folders():
            storage_used += await get_storage_usage(folder)
        return storage_used

    @classmethod
    def has_folders(cls) -> int:
        """
        If there are no folders on disk, moving is noop
        """
        for folder in cls.get_folders():
            if exists(folder):
                return True
        return False

    @classmethod
    def get_dns_records(cls, ip4: str, ip6: Optional[str]) -> List[ServiceDnsRecord]:
        subdomain = cls.get_subdomain()
        display_name = cls.get_display_name()
        if subdomain is None:
            return []
        dns_records = [
            ServiceDnsRecord(
                type="A",
                name=subdomain,
                content=ip4,
                ttl=3600,
                display_name=display_name,
            )
        ]
        if ip6 is not None:
            dns_records.append(
                ServiceDnsRecord(
                    type="AAAA",
                    name=subdomain,
                    content=ip6,
                    ttl=3600,
                    display_name=f"{display_name} (IPv6)",
                )
            )
        return dns_records

    @classmethod
    def get_drive(cls) -> str:
        """
        Get the name of the drive/volume where the service is located.
        Example values are `sda1`, `vda`, `sdb`.
        """
        root_device: str = BlockDevices().get_root_block_device().canonical_name
        if not cls.is_movable():
            return root_device
        with utils.ReadUserData() as userdata:
            if userdata.get("useBinds", False):
                return (
                    userdata.get("modules", {})
                    .get(cls.get_id(), {})
                    .get(
                        "location",
                        root_device,
                    )
                )
            else:
                return root_device

    @classmethod
    def get_folders(cls) -> List[str]:
        """
        get a plain list of occupied directories
        Default extracts info from overriden get_owned_folders()
        """
        if cls.get_owned_folders == Service.get_owned_folders:
            raise NotImplementedError(
                "you need to implement at least one of get_folders() or get_owned_folders()"
            )
        return [owned_folder.path for owned_folder in cls.get_owned_folders()]

    @classmethod
    def get_folders_to_back_up(cls) -> List[str]:
        return cls.get_folders()

    @classmethod
    def get_owned_folders(cls) -> List[OwnedPath]:
        """
        Get a list of occupied directories with ownership info
        Default extracts info from overriden get_folders()
        """
        if cls.get_folders == Service.get_folders:
            raise NotImplementedError(
                "you need to implement at least one of get_folders() or get_owned_folders()"
            )
        return [cls.owned_path(path) for path in cls.get_folders()]

    @staticmethod
    def get_foldername(path: str) -> str:
        return path.split("/")[-1]

    def get_postgresql_databases(self) -> List[str]:
        return []

    # TODO: with better json utils, it can be one line, and not a separate function
    @classmethod
    def set_location(cls, volume: BlockDevice):
        """
        Only changes userdata
        """

        service_id = cls.get_id()
        with WriteUserData() as user_data:
            if "modules" not in user_data:
                user_data["modules"] = {}
            if service_id not in user_data["modules"]:
                user_data["modules"][service_id] = {}
            user_data["modules"][service_id]["location"] = volume.name

    def binds(self) -> List[Bind]:
        owned_folders = self.get_owned_folders()

        return [
            Bind.from_owned_path(folder, self.get_drive()) for folder in owned_folders
        ]

    async def assert_can_move(self, new_volume: BlockDevice):
        """
        Checks if the service can be moved to new volume
        Raises errors if it cannot
        """
        service_name = self.get_display_name()
        if not self.is_movable():
            raise MoveError(f"{service_name} is not movable")

        with ReadUserData() as user_data:
            if not user_data.get("useBinds", False):
                raise MoveError("Server is not using binds.")

        current_volume_name = self.get_drive()
        if current_volume_name == new_volume.canonical_name:
            raise MoveError(f"{service_name} is already on volume {new_volume}")

        check_volume(new_volume, space_needed=await self.get_storage_usage())

        binds = self.binds()
        if binds == []:
            raise MoveError("nothing to move")

        # It is ok if service is uninitialized, we will just reregister it
        if self.has_folders():
            check_binds(current_volume_name, binds)

    def do_move_to_volume(
        self,
        new_volume: BlockDevice,
        job: Job,
    ):
        """
        Move a service to another volume.
        Note: It may be much simpler to write it per bind, but a bit less safe?
        """
        service_name = self.get_display_name()
        binds = self.binds()

        report_progress(10, job, "Unmounting folders from old volume...")
        unbind_folders(binds)

        report_progress(20, job, "Moving data to new volume...")
        binds = move_data_to_volume(binds, new_volume, job)

        report_progress(70, job, f"Making sure {service_name} owns its files...")
        try:
            ensure_folder_ownership(binds)
        except Exception as error:
            # We have logged it via print and we additionally log it here in the error field
            # We are continuing anyway but Job has no warning field
            Jobs.update(
                job,
                JobStatus.RUNNING,
                error=f"Service {service_name} will not be able to write files: "
                + str(error),
            )

        report_progress(90, job, f"Mounting {service_name} data...")
        bind_folders(binds)

        report_progress(95, job, f"Finishing moving {service_name}...")
        self.set_location(new_volume)

    async def move_to_volume(self, volume: BlockDevice, job: Job) -> Job:
        service_name = self.get_display_name()

        report_progress(0, job, "Performing pre-move checks...")

        await self.assert_can_move(volume)
        if not self.has_folders():
            self.set_location(volume)
            Jobs.update(
                job=job,
                status=JobStatus.FINISHED,
                result=f"{service_name} moved successfully (no folders).",
                status_text=f"NOT starting {service_name}",
                progress=100,
            )
            return job

        report_progress(5, job, f"Stopping {service_name}...")
        if self is None:
            raise AssertionError
        async with StoppedService(self):
            report_progress(9, job, "Stopped service, starting the move...")
            self.do_move_to_volume(volume, job)

            report_progress(98, job, "Move complete, rebuilding...")
            await rebuild_system(job, upgrade=False)

            Jobs.update(
                job=job,
                status=JobStatus.FINISHED,
                result=f"{service_name} moved successfully.",
                status_text=f"Starting {service_name}...",
                progress=100,
            )

        return job

    @classmethod
    def owned_path(cls, path: str):
        """Default folder ownership"""
        service_name = cls.get_display_name()

        try:
            owner = cls.get_user()
            if owner is None:
                # TODO: assume root?
                # (if we do not want to do assumptions, maybe not declare user optional?)
                raise LookupError(f"no user for service: {service_name}")
            group = cls.get_group()
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
        pass

    def post_backup(self, job: Job):
        pass

    def pre_restore(self, job: Job):
        pass

    async def post_restore(self, job: Job):
        pass


class StoppedService:
    """
    A context manager that stops the service if needed and reactivates it
    after you are done if it was active

    Example:
        ```
            assert service.get_status() == ServiceStatus.ACTIVE
            async with StoppedService(service) [as stopped_service]:
                assert service.get_status() == ServiceStatus.INACTIVE
        ```
    """

    def __init__(self, service: Service):
        self.service = service

    async def __aenter__(self) -> Service:
        self.original_status = await self.service.get_status()
        if (
            self.original_status not in [ServiceStatus.INACTIVE, ServiceStatus.FAILED]
            and not self.service.is_always_active()
        ):
            try:
                await self.service.stop()
                async with asyncio.timeout(DEFAULT_START_STOP_TIMEOUT):
                    await self.service.wait_for_statuses(
                        [ServiceStatus.INACTIVE, ServiceStatus.FAILED]
                    )
            except TimeoutError as error:
                raise TimeoutError(
                    f"timed out waiting for {self.service.get_display_name()} to stop"
                ) from error
        return self.service

    async def __aexit__(self, type, value, traceback):
        if (
            self.original_status in [ServiceStatus.ACTIVATING, ServiceStatus.ACTIVE]
            and not self.service.is_always_active()
        ):
            try:
                await self.service.start()
                async with asyncio.timeout(DEFAULT_START_STOP_TIMEOUT):
                    await self.service.wait_for_statuses([ServiceStatus.ACTIVE])
            except TimeoutError as error:
                raise TimeoutError(
                    f"timed out waiting for {self.service.get_display_name()} to start"
                ) from error
