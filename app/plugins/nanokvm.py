"""Sipeed NanoKVM power control.

The NanoKVM is not an IPMI BMC. Power and reset are momentary ATX header
pulses: a short power press turns the host on, or asks a running OS to shut
down. A long press holds the power button (force off). The power LED is the
only power-state signal the device exposes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.plugins.base import PluginCategory, PowerState, ServerPlugin
from app.plugins.capabilities import ActionDef, Capability, UIPattern
from app.services.nanokvm.client import NanoKvmClient, NanoKvmError

logger = logging.getLogger(__name__)

_POWER_ACTIONS = (
    {"id": "on", "label": "Power button (short)", "confirm": None, "when": "off"},
    {
        "id": "soft",
        "label": "Soft off (short press)",
        "confirm": "Pulse the power button? If the OS is running this requests shutdown.",
        "when": "on",
    },
    {
        "id": "off",
        "label": "Force off (hold)",
        "confirm": "Hold the power button until the host powers off?",
        "when": "on",
    },
    {
        "id": "reset",
        "label": "Reset button",
        "confirm": "Pulse the reset button?",
        "when": "on",
    },
    {
        "id": "cycle",
        "label": "Force off, then power on",
        "confirm": "Hold the power button, then press it again to turn the host back on?",
        "when": "on",
    },
)

_ACTION_ALIASES = {
    "reboot": "reset",
    "shutdown": "soft",
    "hard": "off",
    "immediate": "off",
}


_NO_USER_CONTROL = "NanoKVM plugin does not support user control"


def _led_power(gpio: dict) -> str:
    if gpio.get("pwr") is True:
        return "on"
    if gpio.get("pwr") is False:
        return "off"
    return "unknown"


def normalize_nanokvm_power_action(action: str) -> str:
    raw = (action or "").strip().lower()
    return _ACTION_ALIASES.get(raw, raw)


def describe_nanokvm_power_actions(power_state: str | PowerState | None) -> list[dict[str, Any]]:
    if isinstance(power_state, PowerState):
        state = power_state.value
    else:
        state = (power_state or "unknown").strip().lower() or "unknown"
    actions: list[dict[str, Any]] = []
    for row in _POWER_ACTIONS:
        when = str(row["when"])
        enabled = state == "unknown" or state == when
        actions.append(
            {
                "id": row["id"],
                "label": row["label"],
                "confirm": row["confirm"],
                "enabled": enabled,
            }
        )
    return actions


class NanoKvmPlugin(ServerPlugin):
    """ATX power and reset through a Sipeed NanoKVM."""

    PLUGIN_NAME = "nanokvm"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [PluginCategory.POWER_CONTROL]
    CAPABILITIES = [
        Capability(
            id="power_control",
            display_name="Power Control",
            description=(
                "Pulses the NanoKVM ATX power and reset headers. "
                "A short press turns the host on, or asks a running OS to shut down."
            ),
            optional=False,
            ui_pattern=UIPattern.STATE_AND_ACTIONS,
            state_action="get_power_state",
            actions=[
                ActionDef("power_on", "Power On", variant="success"),
                ActionDef("power_off", "Power Off", variant="danger", confirm="Pulse the power button?"),
                ActionDef("power_reset", "Reset", variant="warning", confirm="Pulse the reset button?"),
            ],
        ),
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "NanoKVM hostname / IP",
                "description": "IP, hostname, or http(s) URL of the NanoKVM web UI",
                "required": True,
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "NanoKVM web login",
                "required": True,
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "NanoKVM web password",
                "format": "password",
                "required": True,
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "Web UI port (80 for HTTP, 443 for HTTPS)",
                "default": 80,
                "required": False,
            },
            "use_tls": {
                "type": "boolean",
                "title": "Use HTTPS",
                "description": "Connect with HTTPS instead of HTTP",
                "default": False,
            },
            "verify_tls": {
                "type": "boolean",
                "title": "Verify TLS certificate",
                "description": "Reject self-signed HTTPS certificates",
                "default": False,
            },
            "short_press_ms": {
                "type": "integer",
                "title": "Short press (ms)",
                "description": "Power-on and soft-off pulse length",
                "default": 800,
            },
            "long_press_ms": {
                "type": "integer",
                "title": "Force-off hold (ms)",
                "description": "How long to hold the power button for a force off",
                "default": 8000,
            },
            "hid_protocol": {
                "type": "string",
                "title": "Keyboard / mouse protocol",
                "description": "auto, json (firmware 2.2), or binary (firmware 2.3 and newer)",
                "default": "auto",
            },
        },
        "required": ["hostname", "username", "password"],
    }

    def __init__(self, config: dict, *, transport=None):
        super().__init__(config)
        self._transport = transport

    def _client(self, web_url: str = "") -> NanoKvmClient:
        return NanoKvmClient(self.config, web_url=web_url, transport=self._transport)

    def list_chassis_power_actions(self, power_state: PowerState | str | None = None) -> list[dict[str, Any]]:
        return describe_nanokvm_power_actions(power_state)

    async def test_connection(self) -> dict[str, Any]:
        host = (self.config or {}).get("hostname") or ""
        try:
            async with self._client() as client:
                await client.login()
                info = await client.device_info()
                led = ""
                power = "unknown"
                try:
                    power = _led_power(await client.gpio())
                except NanoKvmError as exc:
                    led = str(exc)
        except NanoKvmError as exc:
            return {
                "success": False,
                "message": str(exc),
                "details": {"hostname": host},
            }
        message = "Connected to NanoKVM"
        if info.get("application"):
            message = f"Connected to NanoKVM {info.get('application')}"
        if led:
            message = f"{message}; power LED unavailable ({led})"
        return {
            "success": True,
            "message": message,
            "details": {
                "hostname": host,
                "base_url": client.base_url,
                "application": info.get("application") or "",
                "image": info.get("image") or "",
                "mdns": info.get("mdns") or "",
                "power_led": power,
            },
        }

    async def get_power_state(self) -> PowerState:
        try:
            async with self._client() as client:
                await client.login()
                gpio = await client.gpio()
        except NanoKvmError as exc:
            raise RuntimeError(str(exc)) from exc
        if gpio.get("pwr") is True:
            return PowerState.ON
        if gpio.get("pwr") is False:
            return PowerState.OFF
        return PowerState.UNKNOWN

    async def chassis_power(self, action: str) -> bool:
        name = normalize_nanokvm_power_action(action)
        try:
            async with self._client() as client:
                await client.login()
                if name == "on":
                    await client.press("power", client.short_press_ms)
                elif name == "soft":
                    await client.press("power", client.short_press_ms)
                elif name == "off":
                    await client.press("power", client.long_press_ms)
                elif name == "reset":
                    await client.press("reset", client.short_press_ms)
                elif name == "cycle":
                    await client.press("power", client.long_press_ms)
                    await asyncio.sleep(2)
                    await client.press("power", client.short_press_ms)
                else:
                    raise ValueError(f"Unsupported NanoKVM power action: {action}")
        except NanoKvmError as exc:
            logger.warning("NanoKVM power %s failed: %s", name, exc)
            raise RuntimeError(str(exc)) from exc
        return True

    async def power_on(self) -> bool:
        return await self.chassis_power("on")

    async def power_off(self, force: bool = False) -> bool:
        return await self.chassis_power("off" if force else "soft")

    async def power_reset(self) -> bool:
        return await self.chassis_power("reset")

    async def list_users(self) -> list:
        raise NotImplementedError(_NO_USER_CONTROL)

    async def create_user(self, username: str, password: str, roles: list | None = None) -> bool:
        raise NotImplementedError(_NO_USER_CONTROL)

    async def delete_user(self, username: str) -> bool:
        raise NotImplementedError(_NO_USER_CONTROL)

    async def update_user_password(self, username: str, new_password: str) -> bool:
        raise NotImplementedError(_NO_USER_CONTROL)
