"""Tests for Secure Token-aware guest password old-password candidates."""

from types import SimpleNamespace

from app.services.deployment.guest_config import (
    old_guest_passwords_from_ctx,
    strategy_options_from_ctx,
)


def _ctx(config: dict):
    return SimpleNamespace(service=SimpleNamespace(config=config), get_specs=lambda: {})


def test_old_passwords_prefer_template_then_stored():
    cfg = {
        "template_parameters": {"admin_password": "stored-pass"},
        "vm_plan": {
            "strategy_plan": {
                "strategy_config": {
                    "guest_username": "client",
                    "template_password": "client",
                }
            }
        },
    }
    ctx = _ctx(cfg)
    assert old_guest_passwords_from_ctx(ctx, include_stored=True) == [
        "client",
        "stored-pass",
    ]
    assert old_guest_passwords_from_ctx(ctx, include_stored=False) == ["client"]


def test_strategy_options_include_template_password():
    cfg = {
        "template_parameters": {"admin_password": "new-from-whmcs"},
        "vm_plan": {
            "strategy_plan": {
                "strategy_config": {
                    "template_password": "client",
                    "guest_username": "client",
                }
            },
            "effective_specs": {},
        },
    }
    opts = strategy_options_from_ctx(_ctx(cfg))
    assert opts.get("template_password") == "client"
    assert opts.get("guest_password") == "new-from-whmcs"
