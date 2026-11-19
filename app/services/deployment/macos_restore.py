"""Steps for restoring a retained macOS backup onto another macOS VM of the same client.

The restore brings back the source VM's disk and Proxmox configuration. These steps put the target's own
Proxmox identity back (network interfaces, SMBIOS and macOS arguments), keep its public network interface
disconnected while the restored guest still carries the source's address, then boot the guest and apply the
target's own address, DNS and password through the guest agent before connecting the interface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.service_vm import VMGuestState
from app.services.deployment.step import DeploymentError, DeploymentStep, StepOutcome
from app.services.deployment.steps import (
    CaptureTargetIdentityStep,
    ConfigureMacosGuestStep,
    ReapplyTargetNetworkStep,
    _vm_restore_state,
    _write_vm_restore_state,
)
from app.services.macos_smbios import (
    DEFAULT_SMBIOS_MODEL,
    generate_smbios,
    smbios1_config_value,
    smbios_from_smbios1,
)

# The VM's public network interface; extra interfaces (private networks) are never held down.
PUBLIC_NIC = "net0"
# Proxmox settings that make up a macOS VM's own identity beyond its network interfaces.
MACOS_IDENTITY_KEYS = ("smbios1", "args", "vmgenid")
AGENT_WAIT_SECONDS = 900
CONFIGURE_MAX_ATTEMPTS = 3
RETRY_ACTION = "retry_restore_configuration"  # StrategyAction on the macOS strategy
_MSG_STAYS_DISCONNECTED = "The VM's public network connection stays disconnected"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def with_link_down(net_value: str) -> str:
    """A ``netN`` value with the interface's link set down."""
    parts = [p for p in str(net_value).split(",") if p and not p.startswith("link_down=")]
    return ",".join([*parts, "link_down=1"])


def without_link_down(net_value: str) -> str:
    """A ``netN`` value with no ``link_down`` option, which leaves the link up."""
    return ",".join(p for p in str(net_value).split(",") if p and not p.startswith("link_down="))


def _parse_time(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def record_restore_failure(service, message: str) -> None:
    """Mark the restore failed and the VM errored. A restore that already finished is left alone."""
    state = _vm_restore_state(service)
    if not state or state.get("status") == "success":
        return
    state["status"] = "failed"
    state["error"] = state.get("error") or message
    service.config = {**dict(service.config or {}), "vm_restore": state}
    if service.vm:
        service.vm.guest_state = VMGuestState.ERROR
        service.vm.guest_last_error = state["error"]


def identity_restore_pending(service) -> bool:
    """True while the VM's disk and Proxmox config may still be the source VM's, with the target's own
    network identity (MAC addresses, SMBIOS) not yet put back. Booting then would bring up the source's identity."""
    state = _vm_restore_state(service)
    return bool(
        state.get("macos")
        and not state.get("identity_reapplied")
        and state.get("status") != "success"
        and (state.get("upid") or state.get("carried_identity"))
    )


def _fail_restore(ctx, message: str) -> StepOutcome:
    """Mark the restore failed and the VM errored, and stop the job."""
    state = _vm_restore_state(ctx.service)
    state["error"] = message
    _write_vm_restore_state(ctx, state)
    record_restore_failure(ctx.service, message)
    return StepOutcome.abort(message)


async def _agent_wait_outcome(ctx, *, retry_after: int, waiting_message: str) -> Optional[StepOutcome]:
    """None once the guest agent answers; otherwise wait, or abort when it stays silent too long."""
    plugin = ctx.get_plugin()
    last_error = None
    try:
        ready = await plugin.guest_agent_ready()
    except Exception as exc:
        ready, last_error = False, str(exc)
    state = _vm_restore_state(ctx.service)
    if ready:
        if state.pop("agent_wait_started", None) is not None:
            _write_vm_restore_state(ctx, state)
        return None
    now = _utcnow()
    started = _parse_time(state.get("agent_wait_started"))
    if started is None:
        state["agent_wait_started"] = now.isoformat()
        _write_vm_restore_state(ctx, state)
    elif (now - started).total_seconds() > AGENT_WAIT_SECONDS:
        return _fail_restore(
            ctx,
            f"The macOS guest agent did not respond within {AGENT_WAIT_SECONDS // 60} minutes of the restore, so the "
            f"VM could not be given its own address. {_MSG_STAYS_DISCONNECTED}. Once the guest is reachable, "
            f"staff can run the {RETRY_ACTION} action.",
        )
    return StepOutcome.wait(
        message=waiting_message,
        retry_after_seconds=retry_after,
        detail={"last_error": last_error} if last_error else None,
    )


class CaptureMacosIdentityStep(CaptureTargetIdentityStep):
    """Record the target's network interfaces, name and Proxmox identity settings before the restore.

    A target without an SMBIOS serial and UUID gets a fresh identity, written to Proxmox with the rest and into
    OpenCore later, so it never keeps the restored source's.
    """

    def _capture(self, config: dict, ctx=None) -> dict:
        identity = super()._capture(config, ctx)
        public = identity["nets"].get(PUBLIC_NIC)
        if public:
            # A link left down by an earlier, failed restore is not the target's own setting.
            identity["nets"][PUBLIC_NIC] = without_link_down(public)
        identity["config"] = {k: str(config[k]) for k in MACOS_IDENTITY_KEYS if config.get(k) not in (None, "")}
        if smbios_from_smbios1(identity["config"].get("smbios1")) is None:
            model = str(self._smbios_model(ctx))
            identity["config"]["smbios1"] = smbios1_config_value(generate_smbios(model))
            identity["smbios_generated"] = True
        return identity

    @staticmethod
    def _smbios_model(ctx) -> str:
        from app.services.deployment.guest_config import strategy_options_from_ctx

        try:
            return str(strategy_options_from_ctx(ctx).get("smbios_model") or DEFAULT_SMBIOS_MODEL)
        except Exception:
            return DEFAULT_SMBIOS_MODEL


class ReapplyMacosIdentityStep(ReapplyTargetNetworkStep):
    """Put the target's interfaces and Proxmox identity back, with the public interface link down."""

    def _identity_changes(self, identity: dict, restored: dict) -> tuple[dict, list]:
        payload, stale = super()._identity_changes(identity, restored)
        if PUBLIC_NIC in payload:
            payload[PUBLIC_NIC] = with_link_down(payload[PUBLIC_NIC])
        captured = identity.get("config") or {}
        for key in MACOS_IDENTITY_KEYS:
            if key in captured:
                # Only touch settings that differ: some (args) can only be written by a root account.
                if str(restored.get(key)) != captured[key]:
                    payload[key] = captured[key]
            elif key in restored:
                stale.append(key)
        return payload, sorted(stale)

    async def execute(self, ctx) -> None:
        await super().execute(ctx)
        state = _vm_restore_state(ctx.service)
        state["link_held"] = PUBLIC_NIC in (state["target_identity"].get("nets") or {})
        state["identity_reapplied"] = True
        _write_vm_restore_state(ctx, state)


class WaitForRestoredGuestAgentStep(DeploymentStep):
    """Wait for the restored guest's agent; fail the job if it stays silent for too long."""

    name = "wait_for_restored_guest_agent"
    default_retry_after_seconds = 15

    async def precheck(self, ctx) -> StepOutcome:
        outcome = await _agent_wait_outcome(
            ctx,
            retry_after=self.default_retry_after_seconds,
            waiting_message="Waiting for the restored macOS guest agent",
        )
        return outcome or StepOutcome.ready("Guest agent responding")


class ConfigureRestoredMacosGuestStep(ConfigureMacosGuestStep):
    """Apply the target's own address, DNS, password and SMBIOS to the restored guest.

    Reuses the macOS guest-agent configuration. Differences: the SMBIOS identity written into OpenCore is the
    target's own (from its Proxmox ``smbios1``), not a new random one, whatever ``randomize_smbios`` says, and
    the password change also tries the source VM's stored passwords, because the restored guest still has the
    source's password.
    """

    name = "configure_restored_macos_guest"

    async def precheck(self, ctx) -> StepOutcome:
        state = _vm_restore_state(ctx.service)
        if int(state.get("configure_attempts") or 0) >= CONFIGURE_MAX_ATTEMPTS:
            return _fail_restore(
                ctx,
                f"Could not configure the restored macOS guest after {CONFIGURE_MAX_ATTEMPTS} attempts: "
                f"{state.get('configure_error') or 'unknown error'}. {_MSG_STAYS_DISCONNECTED}. Once the cause is "
                f"fixed, staff can run the {RETRY_ACTION} action.",
            )
        outcome = await _agent_wait_outcome(
            ctx,
            retry_after=self.default_retry_after_seconds,
            waiting_message="Waiting for the guest agent before configuring the restored macOS guest",
        )
        return outcome or StepOutcome.ready()

    async def _maybe_randomize_smbios(self, ctx, plugin, opts: dict) -> None:
        state = _vm_restore_state(ctx.service)
        if state.get("opencore_aligned"):
            return
        from app.services.deployment import guest_config

        model = str(opts.get("smbios_model") or DEFAULT_SMBIOS_MODEL)
        captured = ((state.get("target_identity") or {}).get("config") or {}).get("smbios1")
        smbios = smbios_from_smbios1(captured, default_model=model)
        if smbios is None:
            raise DeploymentError("The target VM's SMBIOS identity was not recorded before the restore")
        await guest_config.apply_opencore_smbios(
            plugin,
            model=model,
            oc_disk=str(opts.get("opencore_disk") or "disk1s1"),
            cfg_path=str(opts.get("opencore_config") or "/Volumes/OPENCORE/EFI/OC/config.plist"),
            smbios=smbios,
            update_proxmox=False,
        )
        await guest_config.reboot_guest_and_wait_agent(plugin, max_wait=600.0)
        state = _vm_restore_state(ctx.service)
        state["opencore_aligned"] = True
        _write_vm_restore_state(ctx, state)
        ctx.logger.info("OpenCore SMBIOS set to the target's own serial=%s", smbios["SystemSerialNumber"])

    def _source_guest_passwords(self, ctx) -> list:
        """Passwords the source VM was last known to use, if it still exists and belongs to the same client."""
        source_id = _vm_restore_state(ctx.service).get("source_service_id")
        if not source_id or getattr(ctx, "db", None) is None:
            return []
        from app.dao.service_dao import ServiceDAO

        source = ServiceDAO.get_by_id(ctx.db, int(source_id))
        if source is None or source.owner_user_id != ctx.service.owner_user_id:
            return []
        params = (source.config or {}).get("template_parameters") or {}
        values = (params.get(key) for key in ("admin_password", "guest_password", "previous_admin_password"))
        return [str(v) for v in values if v]

    async def _apply_guest_password(self, ctx, plugin, username: str, password) -> None:
        if not password:
            return
        from app.services.deployment.guest_config import old_guest_passwords_from_ctx, set_guest_password

        candidates = [*self._source_guest_passwords(ctx), *old_guest_passwords_from_ctx(ctx)]
        await set_guest_password(
            plugin,
            username,
            str(password),
            old_passwords=[p for p in dict.fromkeys(candidates) if p != str(password)],
        )

    async def execute(self, ctx) -> None:
        state = _vm_restore_state(ctx.service)
        state["configure_attempts"] = int(state.get("configure_attempts") or 0) + 1
        _write_vm_restore_state(ctx, state)
        try:
            await super().execute(ctx)
        except Exception as exc:
            state = _vm_restore_state(ctx.service)
            state["configure_error"] = str(exc)
            _write_vm_restore_state(ctx, state)
            raise
        state = _vm_restore_state(ctx.service)
        state.pop("configure_error", None)
        _write_vm_restore_state(ctx, state)


class ReleasePublicLinkStep(DeploymentStep):
    """Connect the public network interface once the guest has its own address."""

    name = "release_public_link"
    timeout_seconds = 60

    async def precheck(self, ctx) -> StepOutcome:
        if not _vm_restore_state(ctx.service).get("link_held"):
            return StepOutcome.skip("The public network interface was not held down")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        plugin = ctx.get_plugin()
        current = str((await plugin.get_qemu_config()).get(PUBLIC_NIC) or "")
        if "link_down" in current:
            await plugin.set_qemu_config({PUBLIC_NIC: without_link_down(current)})
        state = _vm_restore_state(ctx.service)
        state["link_held"] = False
        _write_vm_restore_state(ctx, state)
