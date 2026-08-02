"""Tests for .onion subpath URL routing (Task 3.c.1)."""

import json
import pytest
from typing import Optional

from selfprivacy_api.services.onion_routing import TOR_SERVICE_PATHS
from selfprivacy_api.services.test_service import DummyService
from selfprivacy_api.services import ServiceManager
from selfprivacy_api.services.prometheus import Prometheus
from selfprivacy_api.repositories.users import ACTIVE_USERS_PROVIDER
from selfprivacy_api.repositories.users.json_user_repository import JsonUserRepository

ONION = "teststesting12345.onion"
DOMAIN = "example.tld"

SERVICE_MODULE = "selfprivacy_api.services.service"
TEMPLATED_MODULE = "selfprivacy_api.services.templated_service"
SERVICES_MODULE = "selfprivacy_api.services"
PROMETHEUS_MODULE = "selfprivacy_api.services.prometheus"


def _make_service_class(service_id: str, subdomain: str = "placeholder"):
    """Minimal concrete Service subclass for a given service ID."""

    class _S(DummyService, folders=[]):
        @staticmethod
        def get_id() -> str:
            return service_id

        @classmethod
        def get_subdomain(cls) -> Optional[str]:
            return subdomain

    _S.__name__ = f"Service_{service_id}"
    return _S


def _minimal_def(service_id: str, show_url: bool = True) -> str:
    return json.dumps(
        {
            "meta": {
                "id": service_id,
                "name": service_id.title(),
                "systemdServices": [],
            },
            "options": {},
        }
    )


# ── Section 7.1: Service.get_url() ───────────────────────────────────────────


@pytest.mark.parametrize(
    "service_id,expected_path",
    list(TOR_SERVICE_PATHS.items()),
)
def test_service_get_url_onion(mocker, service_id, expected_path):
    mocker.patch(f"{SERVICE_MODULE}.get_domain", return_value=ONION)
    svc = _make_service_class(service_id)
    assert svc.get_url() == f"https://{ONION}{expected_path}"


def test_service_get_url_normal_domain_nextcloud(mocker):
    mocker.patch(f"{SERVICE_MODULE}.get_domain", return_value=DOMAIN)
    svc = _make_service_class("nextcloud", subdomain="nextcloud")
    assert svc.get_url() == f"https://nextcloud.{DOMAIN}"


def test_service_get_url_normal_domain_api(mocker):
    mocker.patch(f"{SERVICE_MODULE}.get_domain", return_value=DOMAIN)
    svc = _make_service_class("selfprivacy-api", subdomain="api")
    assert svc.get_url() == f"https://api.{DOMAIN}"


def test_service_get_url_unknown_id_no_crash(mocker):
    mocker.patch(f"{SERVICE_MODULE}.get_domain", return_value=ONION)
    svc = _make_service_class("unknown-service-xyz", subdomain="unknown")
    # ID not in TOR_SERVICE_PATHS — must not raise, falls back to subdomain logic
    result = svc.get_url()
    assert isinstance(result, str)


# ── Section 7.2: TemplatedService.get_url() ──────────────────────────────────


@pytest.mark.parametrize(
    "service_id,expected_path",
    list(TOR_SERVICE_PATHS.items()),
)
def test_templated_service_get_url_onion(mocker, service_id, expected_path):
    mocker.patch(f"{TEMPLATED_MODULE}.get_domain", return_value=ONION)
    from selfprivacy_api.services.templated_service import TemplatedService

    svc = TemplatedService(service_id, _minimal_def(service_id))
    assert svc.get_url() == f"https://{ONION}{expected_path}"


def test_templated_service_get_url_show_url_false(mocker):
    mocker.patch(f"{TEMPLATED_MODULE}.get_domain", return_value=ONION)
    from selfprivacy_api.services.templated_service import TemplatedService

    definition = json.dumps(
        {
            "meta": {
                "id": "nextcloud",
                "name": "Nextcloud",
                "systemdServices": [],
                "showUrl": False,
            },
            "options": {},
        }
    )
    svc = TemplatedService("nextcloud", definition)
    assert svc.get_url() is None


# ── Section 7.3: ServiceManager.get_url() ────────────────────────────────────


def test_service_manager_get_url_onion(mocker):
    mocker.patch(f"{SERVICES_MODULE}.get_domain", return_value=ONION)
    assert ServiceManager.get_url() == f"https://{ONION}/api/"


def test_service_manager_get_url_normal_domain(mocker):
    mocker.patch(f"{SERVICES_MODULE}.get_domain", return_value=DOMAIN)
    assert ServiceManager.get_url() == f"https://api.{DOMAIN}"


# ── Section 7.4: Prometheus.get_url() ────────────────────────────────────────


def test_prometheus_get_url_onion(mocker):
    mocker.patch(f"{PROMETHEUS_MODULE}.get_domain", return_value=ONION)
    assert Prometheus.get_url() == f"https://{ONION}/prometheus/"


def test_prometheus_get_url_normal_domain(mocker):
    mocker.patch(f"{PROMETHEUS_MODULE}.get_domain", return_value=DOMAIN)
    assert Prometheus.get_url() is None


# ── Section 7.5: User repository ─────────────────────────────────────────────


def test_active_users_provider_is_json():
    assert ACTIVE_USERS_PROVIDER is JsonUserRepository


def test_json_user_repository_instantiates(generic_userdata):
    repo = JsonUserRepository()
    assert repo is not None


def test_json_user_repository_get_users_returns_list(generic_userdata):
    repo = JsonUserRepository()
    result = repo.get_users()
    assert isinstance(result, list)
