"""Tests for guest OS credential resolution from service.config."""
from types import SimpleNamespace

from app.services.vm_guest_credentials import get_service_guest_credentials, session_guest_fields


def test_credentials_from_template_parameters():
    service = SimpleNamespace(
        id=8,
        config={
            "template_parameters": {"admin_password": "s3cret!", "guest_username": "client"},
        },
    )
    user, password = get_service_guest_credentials(service)
    assert user == "client"
    assert password == "s3cret!"


def test_credentials_from_strategy_config_when_template_missing():
    service = SimpleNamespace(
        id=1,
        config={
            "vm_plan": {
                "strategy_plan": {
                    "strategy_config": {
                        "guest_username": "ubuntu",
                        "cloudinit_cipassword": "from-ci",
                    }
                }
            }
        },
    )
    user, password = get_service_guest_credentials(service)
    assert user == "ubuntu"
    assert password == "from-ci"


def test_template_password_wins_over_strategy():
    service = SimpleNamespace(
        id=1,
        config={
            "template_parameters": {"admin_password": "whmcs-pass"},
            "vm_plan": {
                "strategy_plan": {"strategy_config": {"guest_password": "strategy-pass"}},
            },
        },
    )
    _user, password = get_service_guest_credentials(service)
    assert password == "whmcs-pass"


def test_session_guest_fields_empty_when_unknown():
    service = SimpleNamespace(id=4, config={})
    assert session_guest_fields(service) == {
        "service_id": 4,
        "guest_username": "",
        "guest_password": "",
    }
