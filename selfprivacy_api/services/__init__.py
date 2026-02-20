"""Services module."""

import logging
import base64
import typing
import json
import asyncio
from typing import List
from os import listdir, path, makedirs
from os.path import join, exists
from opentelemetry import trace

from shutil import copyfile, copytree, rmtree
from selfprivacy_api.jobs import Job, JobStatus, Jobs
from selfprivacy_api.services.prometheus import Prometheus
from selfprivacy_api.services.mailserver import MailServer

from selfprivacy_api.services.service import Service, ServiceDnsRecord
from selfprivacy_api.services.service import ServiceStatus
from selfprivacy_api.services.remote import get_remote_service
from selfprivacy_api.services.suggested import SuggestedServices
import selfprivacy_api.utils.network as network_utils

from selfprivacy_api.services.api_icon import API_ICON
from selfprivacy_api.utils import (
    USERDATA_FILE,
    DKIM_DIR,
    SECRETS_FILE,
    get_domain,
    read_account_uri,
)
from selfprivacy_api.utils.block_devices import BlockDevices
from selfprivacy_api.services.templated_service import (
    SP_MODULES_DEFINITIONS_PATH,
    SP_SUGGESTED_MODULES_PATH,
    TemplatedService,
)

CONFIG_STASH_DIR = "/etc/selfprivacy/dump"
KANIDM_A_RECORD = "auth"

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ServiceManager(Service):
    folders: List[str] = [CONFIG_STASH_DIR]

    @staticmethod
    @tracer.start_as_current_span("get_all_services")
    async def get_all_services() -> list[Service]:
        return await get_services()

    @staticmethod
    async def get_service_by_id(service_id: str) -> typing.Optional[Service]:
        with tracer.start_as_current_span("get_service_by_id") as span:
            span.set_attribute("service_id", service_id)

            for service in DUMMY_SERVICES:
                if service.get_id() == service_id:
                    return service

            if "ONLY_DUMMY_SERVICE" in TEST_FLAGS:
                return None
            elif "DUMMY_SERVICE_AND_API" in TEST_FLAGS:
                if service_id == ServiceManager.get_id():
                    return ServiceManager()
                return None

            for service in HARDCODED_SERVICES:
                if service.get_id() == service_id:
                    return service

            if exists(join(SP_MODULES_DEFINITIONS_PATH, service_id)):
                return await get_templated_service(service_id)

            suggested_services = await SuggestedServices.get()

            for service in suggested_services:
                if service.get_id() == service_id:
                    return service

            return None

    @staticmethod
    @tracer.start_as_current_span("get_enabled_services")
    async def get_enabled_services() -> list[Service]:
        return [service for service in await get_services() if service.is_enabled()]

    @staticmethod
    @tracer.start_as_current_span("get_enabled_services_with_urls")
    async def get_enabled_services_with_urls() -> list[Service]:
        return [
            service
            for service in await get_services(exclude_remote=True)
            if service.is_enabled() and service.get_url()
        ]

    # This one is not currently used by any code.``
    @staticmethod
    @tracer.start_as_current_span("get_disabled_services")
    async def get_disabled_services() -> list[Service]:
        return [service for service in await get_services() if not service.is_enabled()]

    @staticmethod
    async def get_services_by_location(location: str) -> list[Service]:
        with tracer.start_as_current_span("get_services_by_location") as span:
            span.set_attribute("location", location)
            return [
                service
                for service in await get_services(
                    exclude_remote=True,
                )
                if service.get_drive() == location
            ]

    @staticmethod
    @tracer.start_as_current_span("get_all_required_dns_records")
    async def get_all_required_dns_records() -> list[ServiceDnsRecord]:
        ip4 = network_utils.get_ip4()
        ip6 = network_utils.get_ip6()

        dns_records: list[ServiceDnsRecord] = [
            ServiceDnsRecord(
                type="A",
                name=KANIDM_A_RECORD,
                content=ip4,
                ttl=3600,
                display_name="Record for Kanidm",
            ),
        ]

        try:
            dns_records.append(
                ServiceDnsRecord(
                    type="CAA",
                    name=get_domain(),
                    content=f'128 issue "letsencrypt.org;accounturi={read_account_uri()}"',
                    ttl=3600,
                    display_name="CAA record",
                )
            )
        except Exception as e:
            logging.error(f"Error creating CAA: {e}")

        for service in await ServiceManager.get_enabled_services():
            dns_records += service.get_dns_records(ip4, ip6)
        return dns_records

    @staticmethod
    def get_id() -> str:
        """Return service id."""
        return "selfprivacy-api"

    @staticmethod
    def get_display_name() -> str:
        """Return service display name."""
        return "Selfprivacy API"

    @staticmethod
    def get_description() -> str:
        """Return service description."""
        return "Enables communication between the SelfPrivacy app and the server."

    @staticmethod
    def get_svg_icon(raw=False) -> str:
        """Read SVG icon from file and return it as base64 encoded string."""
        if raw:
            return API_ICON
        return base64.b64encode(API_ICON.encode("utf-8")).decode("utf-8")

    @staticmethod
    def get_url() -> typing.Optional[str]:
        """Return service url."""
        domain = get_domain()
        if domain and domain.endswith(".onion"):
            return f"https://{domain}/api/"
        return f"https://api.{domain}"

    @staticmethod
    def get_subdomain() -> typing.Optional[str]:
        return "api"

    @staticmethod
    def is_always_active() -> bool:
        return True

    @staticmethod
    def is_movable() -> bool:
        return False

    @staticmethod
    def is_required() -> bool:
        return True

    @staticmethod
    def is_enabled() -> bool:
        return True

    @staticmethod
    def is_installed() -> bool:
        return True

    @staticmethod
    def is_system_service() -> bool:
        return True

    @staticmethod
    def get_backup_description() -> str:
        return "General server settings."

    @classmethod
    async def get_status(cls) -> ServiceStatus:
        return ServiceStatus.ACTIVE

    @classmethod
    async def wait_for_statuses(self, expected_statuses: List[ServiceStatus]):
        if ServiceStatus.ACTIVE in expected_statuses:
            return

        raise Exception("Why would API wait for API stopping?")

    @classmethod
    def can_be_backed_up(cls) -> bool:
        """`True` if the service can be backed up."""
        return True

    @classmethod
    async def merge_settings(cls):
        # For now we will just copy settings EXCEPT the locations of services
        # Stash locations as they are set by user right now
        locations = {}
        for service in await get_services(
            exclude_remote=True,
        ):
            if service.is_movable():
                locations[service.get_id()] = service.get_drive()

        # Copy files
        for p in [USERDATA_FILE, SECRETS_FILE, DKIM_DIR]:
            cls.retrieve_stashed_path(p)

        # Pop location
        for service in await get_services(
            exclude_remote=True,
        ):
            if service.is_movable():
                device = BlockDevices().get_block_device_by_canonical_name(
                    locations[service.get_id()]
                )
                if device is not None:
                    service.set_location(device)

    @classmethod
    def stop(cls):
        """
        We are always active
        """
        raise ValueError("tried to stop an always active service")

    @classmethod
    def start(cls):
        """
        We are always active
        """
        pass

    @classmethod
    def restart(cls):
        """
        We are always active
        """
        pass

    @classmethod
    def get_drive(cls) -> str:
        return BlockDevices().get_root_block_device().canonical_name

    @classmethod
    def get_folders(cls) -> List[str]:
        return cls.folders

    @classmethod
    def stash_for(cls, p: str) -> str:
        basename = path.basename(p)
        stashed_file_location = join(cls.dump_dir(), basename)
        return stashed_file_location

    @classmethod
    def stash_a_path(cls, p: str):
        if path.isdir(p):
            rmtree(cls.stash_for(p), ignore_errors=True)
            copytree(p, cls.stash_for(p))
        else:
            copyfile(p, cls.stash_for(p))

    @classmethod
    def retrieve_stashed_path(cls, p: str):
        """
        Takes an original path, hopefully it is stashed somewhere
        """
        if path.isdir(p):
            rmtree(p, ignore_errors=True)
            copytree(cls.stash_for(p), p)
        else:
            copyfile(cls.stash_for(p), p)

    @classmethod
    def pre_backup(cls, job: Job):
        Jobs.update(
            job,
            status_text="Stashing settings",
            status=JobStatus.RUNNING,
        )
        tempdir = cls.dump_dir()
        rmtree(join(tempdir), ignore_errors=True)
        makedirs(tempdir)

        for p in [USERDATA_FILE, SECRETS_FILE, DKIM_DIR]:
            cls.stash_a_path(p)

    @classmethod
    def post_backup(cls, job: Job):
        rmtree(cls.dump_dir(), ignore_errors=True)

    @classmethod
    def dump_dir(cls) -> str:
        """
        A directory we dump our settings into
        """
        return cls.folders[0]

    @classmethod
    async def post_restore(cls, job: Job):
        await cls.merge_settings()
        rmtree(cls.dump_dir(), ignore_errors=True)


# @redis_cached_call(ttl=30)
async def get_templated_service(service_id: str) -> TemplatedService:
    with tracer.start_as_current_span(
        "fetch_templated_service", attributes={"service_id": service_id}
    ) as span:
        if not exists(path.join(SP_MODULES_DEFINITIONS_PATH, service_id)):
            raise FileNotFoundError(f"Service definition for {service_id} not found")
        with open(
            path.join(SP_MODULES_DEFINITIONS_PATH, service_id), "r", encoding="utf-8"
        ) as f:
            service_data = f.read()
    return TemplatedService(service_id, service_data)


DUMMY_SERVICES = []
TEST_FLAGS: list[str] = []

HARDCODED_SERVICES: list[Service] = [
    MailServer(),
    ServiceManager(),
    Prometheus(),
]


@tracer.start_as_current_span("get_services")
async def get_services(exclude_remote=False) -> list[Service]:
    if "ONLY_DUMMY_SERVICE" in TEST_FLAGS:
        return DUMMY_SERVICES
    if "DUMMY_SERVICE_AND_API" in TEST_FLAGS:
        return DUMMY_SERVICES + [ServiceManager()]

    hardcoded_services: list[Service] = HARDCODED_SERVICES
    if DUMMY_SERVICES:
        hardcoded_services += DUMMY_SERVICES
    service_ids = [service.get_id() for service in hardcoded_services]

    templated_services = await get_templated_services(
        ignored_services=service_ids,
    )
    service_ids += [service.get_id() for service in templated_services]

    with tracer.start_as_current_span(
        "check_include_remote_services",
        attributes={
            "exclude_remote": exclude_remote,
            "path_exists": path.exists(SP_SUGGESTED_MODULES_PATH),
        },
    ) as span:
        if not exclude_remote and path.exists(SP_SUGGESTED_MODULES_PATH):
            remote_services = await SuggestedServices.get()
            service_ids += [service.get_id() for service in remote_services]
            span.add_event(
                "Including remote services",
                attributes={"remote_service_count": len(list(remote_services))},
            )

            templated_services += remote_services

    return hardcoded_services + templated_services


@tracer.start_as_current_span("get_templated_services")
async def get_templated_services(ignored_services: list[str]) -> list[Service]:
    templated_services = []
    if path.exists(SP_MODULES_DEFINITIONS_PATH):
        tasks: list[asyncio.Task[TemplatedService]] = []
        async with asyncio.TaskGroup() as tg:
            for module in listdir(SP_MODULES_DEFINITIONS_PATH):
                if module in ignored_services:
                    continue
                tasks.append(tg.create_task(get_templated_service(module)))
        for task in tasks:
            try:
                templated_services.append(task.result())
            except Exception as e:
                logger.error(f"Failed to load service: {e}")

    return templated_services
