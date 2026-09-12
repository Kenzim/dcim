"""Concrete deployment steps shared by VM strategies.

Every step is idempotent so a crash-reclaim can safely re-run the current step:
clone skips when the VMID already exists, power-on skips when already running,
and configuration is a declarative Proxmox ``config`` PUT.
"""
from __future__ import annotations

from app.plugins.base import PowerState
from app.services.deployment.step import DeploymentError, DeploymentStep, StepOutcome

_MSG_GUEST_AGENT_NOT_READY = "Guest agent not reachable yet"
_MSG_STATIC_NETWORK_REQUIRES_LINKED_VM_IP = "Static network requires a linked VM IP allocation"


class CloneFromTemplateStep(DeploymentStep):
    name = "clone_from_template"

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            exists = await plugin.vm_exists()
        except Exception:
            exists = False
        if exists:
            return StepOutcome.skip("Target VMID already exists; skipping clone")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        _cid, placed_node, target_vmid = ctx.require_placement()
        template_node, template_vmid = await ctx.resolve_template_location()
        plugin = ctx.get_plugin()
        specs = ctx.get_specs()
        # Linked clones are the default; products may opt into full clones via VM config.
        full_clone = bool(specs.get("full_clone", False))
        name = (ctx.service.name or f"vm-{target_vmid}")[:90]
        template_ref: dict = {"vmid": int(template_vmid), "node": template_node}
        vm_config: dict = {"vmid": int(target_vmid), "name": name, "full_clone": full_clone}
        # Shared storage: template may live on another node; clone onto the placed node.
        if (template_node or "").strip() != (placed_node or "").strip():
            vm_config["target_node"] = placed_node
        clone_out = await plugin.clone_vm_from_template(template_ref, vm_config)
        upid = clone_out.get("task")
        if upid:
            await plugin.wait_for_proxmox_task(str(upid))


class ConfigureSizingStep(DeploymentStep):
    name = "configure_sizing"

    async def precheck(self, ctx) -> StepOutcome:
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        _cid, _node, target_vmid = ctx.require_placement()
        specs = ctx.get_specs()
        memory_mb = int(specs.get("memory_mb", 2048))
        cores = int(specs.get("cores", 2))
        plugin = ctx.get_plugin()
        ok = await plugin.configure_vm({"vmid": int(target_vmid)}, {"memory_mb": memory_mb, "cores": cores})
        if not ok:
            raise DeploymentError("Proxmox VM sizing config update failed")

        # Apply IP-pool / product bridge before power-on (clones keep the template vmbr).
        alloc = ctx.get_ip_allocation()
        bridge = (specs.get("network_bridge") or "").strip()
        if not bridge and alloc is not None:
            bridge = (getattr(alloc, "bridge_name", None) or "").strip()
        if bridge:
            if not hasattr(plugin, "ensure_network_bridge"):
                ctx.logger.warning("Plugin does not support network bridge updates; skipping bridge=%s", bridge)
            else:
                result = await plugin.ensure_network_bridge(bridge, vmid=int(target_vmid))
                ctx.logger.info(
                    "Network bridge net=%s bridge=%s changed=%s",
                    result.get("net_key"),
                    bridge,
                    result.get("changed"),
                )

        # Grow the primary virtual disk when catalog/product asks for more space.
        # Guest filesystem growth (APFS / Linux) happens later via the guest agent.
        disk_gb = specs.get("disk_gb")
        if disk_gb in (None, ""):
            return
        try:
            target_disk_gb = int(disk_gb)
        except (TypeError, ValueError) as exc:
            raise DeploymentError(f"Invalid disk_gb in effective_specs: {disk_gb!r}") from exc
        if target_disk_gb <= 0:
            return
        if not hasattr(plugin, "ensure_primary_disk_gb"):
            ctx.logger.warning("Plugin does not support disk resize; skipping disk_gb=%s", target_disk_gb)
            return
        result = await plugin.ensure_primary_disk_gb(target_disk_gb, vmid=int(target_vmid))
        ctx.logger.info(
            "Disk sizing disk=%s current_gb=%s target_gb=%s resized=%s",
            result.get("disk"),
            result.get("current_gb"),
            result.get("target_gb"),
            result.get("resized"),
        )


class ConfigureCloudInitNetworkStep(DeploymentStep):
    name = "configure_cloudinit_network"

    async def precheck(self, ctx) -> StepOutcome:
        alloc = ctx.get_ip_allocation()
        if not alloc:
            return StepOutcome.failed("No VM IP pool row linked (service_vm.vm_ip_allocation_id)")
        if not (alloc.ip_address or "").strip() or not (alloc.gateway or "").strip():
            return StepOutcome.failed("VM IP allocation is missing ip_address or gateway")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.deployment.guest_config import (
            build_cloudinit_network_payload,
            cloudinit_credentials_from_ctx,
        )
        from app.services.ssh_public_keys import ssh_public_keys_from_service_config

        _cid, _node, target_vmid = ctx.require_placement()
        alloc = ctx.get_ip_allocation()
        ciuser, cipassword = cloudinit_credentials_from_ctx(ctx)
        keys = ssh_public_keys_from_service_config(
            ctx.service.config if getattr(ctx, "service", None) else None
        )
        payload = build_cloudinit_network_payload(
            alloc,
            ciuser=ciuser,
            cipassword=cipassword,
            ssh_public_keys=keys,
        )
        plugin = ctx.get_plugin()
        ok = await plugin.configure_vm({"vmid": int(target_vmid)}, payload)
        if not ok:
            raise DeploymentError("Proxmox cloud-init network config update failed")


class PowerOnStep(DeploymentStep):
    name = "power_on"

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            state = await plugin.get_power_state()
        except Exception:
            state = PowerState.UNKNOWN
        if state == PowerState.ON:
            return StepOutcome.skip("VM already running")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        plugin = ctx.get_plugin()
        try:
            powered = await plugin.power_on()
        except Exception as exc:
            raise DeploymentError(f"Proxmox power_on failed: {exc}") from exc
        if not powered:
            raise DeploymentError("Proxmox power_on returned failure")
        # Defense in depth: never mark succeeded while the guest is still stopped.
        try:
            state = await plugin.get_power_state()
        except Exception as exc:
            raise DeploymentError(f"Could not verify power state after power_on: {exc}") from exc
        if state != PowerState.ON:
            raise DeploymentError(
                f"Proxmox reported power_on success but VM is not running (state={state})"
            )


class WaitForGuestAgentStep(DeploymentStep):
    """Wait until the QEMU guest agent responds (no side effect)."""

    name = "wait_for_guest_agent"
    # Give the guest a few minutes to boot and start the agent before failing.
    timeout_seconds = 600
    default_retry_after_seconds = 10

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        ready = False
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:  # treat unreachable as "not yet"
            return StepOutcome.wait(
                message=_MSG_GUEST_AGENT_NOT_READY,
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if ready:
            return StepOutcome.ready("Guest agent responding")
        return StepOutcome.wait(
            message="Waiting for guest agent to come up",
            retry_after_seconds=self.default_retry_after_seconds,
        )


class ConfigureViaGuestAgentStep(DeploymentStep):
    """Post-boot configuration via the guest agent (Linux)."""

    name = "configure_via_guest_agent"
    timeout_seconds = 300
    default_retry_after_seconds = 10

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:
            return StepOutcome.wait(
                message=_MSG_GUEST_AGENT_NOT_READY,
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if not ready:
            return StepOutcome.wait(message="Waiting for guest agent before configuring")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.deployment.guest_config import (
            apply_root_authorized_keys,
            configure_linux_network,
            set_guest_password,
            strategy_options_from_ctx,
        )
        from app.services.ssh_public_keys import ssh_public_keys_from_service_config

        opts = strategy_options_from_ctx(ctx)
        plugin = ctx.get_plugin()
        username = str(opts.get("guest_username") or opts.get("cloudinit_ciuser") or "root")
        password = opts.get("guest_password") or opts.get("admin_password") or opts.get("cloudinit_cipassword")
        mode = str(opts.get("network_mode") or "static").lower()
        if mode == "static" and not ctx.get_ip_allocation():
            raise DeploymentError(_MSG_STATIC_NETWORK_REQUIRES_LINKED_VM_IP)
        await configure_linux_network(plugin, mode=mode, alloc=ctx.get_ip_allocation())
        if password:
            from app.services.deployment.guest_config import old_guest_passwords_from_ctx

            await set_guest_password(
                plugin,
                username,
                str(password),
                old_passwords=old_guest_passwords_from_ctx(ctx, include_stored=False),
            )
        keys = ssh_public_keys_from_service_config(
            ctx.service.config if getattr(ctx, "service", None) else None
        )
        if keys:
            await apply_root_authorized_keys(plugin, keys)
        ctx.logger.info(
            "Linux guest-agent configure done (network_mode=%s user=%s password_set=%s ssh_keys=%s)",
            mode,
            username,
            bool(password),
            len(keys),
        )


class ConfigureMacosGuestStep(DeploymentStep):
    """macOS post-boot: optional SMBIOS, network, password via AppleQEMUGuestAgent."""

    name = "configure_macos_guest"
    timeout_seconds = 900
    default_retry_after_seconds = 15

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:
            return StepOutcome.wait(
                message=_MSG_GUEST_AGENT_NOT_READY,
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if not ready:
            return StepOutcome.wait(message="Waiting for guest agent before macOS configure")
        return StepOutcome.ready()

    @staticmethod
    def _parse_randomize_flag(value) -> bool:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)

    async def _maybe_grow_root_disk(self, ctx, plugin) -> None:
        specs = ctx.get_specs()
        if specs.get("disk_gb") in (None, ""):
            return
        from app.services.deployment.guest_config import grow_macos_root_apfs

        await grow_macos_root_apfs(plugin)

    async def _maybe_randomize_smbios(self, ctx, plugin, opts: dict) -> None:
        if not self._parse_randomize_flag(opts.get("randomize_smbios", True)):
            return
        from app.services.deployment.guest_config import (
            apply_opencore_smbios,
            reboot_guest_and_wait_agent,
        )

        sm = await apply_opencore_smbios(
            plugin,
            model=str(opts.get("smbios_model") or "iMacPro1,1"),
            oc_disk=str(opts.get("opencore_disk") or "disk1s1"),
            cfg_path=str(
                opts.get("opencore_config") or "/Volumes/OPENCORE/EFI/OC/config.plist"
            ),
        )
        ctx.logger.info(
            "OpenCore SMBIOS applied serial=%s mlb=%s uuid=%s",
            sm.get("SystemSerialNumber"),
            sm.get("MLB"),
            sm.get("SystemUUID"),
        )
        await reboot_guest_and_wait_agent(plugin, max_wait=600.0)

    async def _apply_guest_password(self, ctx, plugin, username: str, password) -> None:
        if not password:
            return
        from app.services.deployment.guest_config import (
            old_guest_passwords_from_ctx,
            set_guest_password,
        )

        await set_guest_password(
            plugin,
            username,
            str(password),
            old_passwords=old_guest_passwords_from_ctx(ctx, include_stored=False),
        )

    async def execute(self, ctx) -> None:
        from app.services.deployment.guest_config import (
            configure_macos_network,
            strategy_options_from_ctx,
        )

        opts = strategy_options_from_ctx(ctx)
        plugin = ctx.get_plugin()
        username = str(opts.get("guest_username") or "client")
        password = opts.get("guest_password") or opts.get("admin_password") or opts.get("cloudinit_cipassword")
        mode = str(opts.get("network_mode") or "static").lower()
        randomize = self._parse_randomize_flag(opts.get("randomize_smbios", True))

        await self._maybe_grow_root_disk(ctx, plugin)
        await self._maybe_randomize_smbios(ctx, plugin, opts)

        if mode == "static" and not ctx.get_ip_allocation():
            raise DeploymentError(_MSG_STATIC_NETWORK_REQUIRES_LINKED_VM_IP)
        await configure_macos_network(plugin, mode=mode, alloc=ctx.get_ip_allocation())
        await self._apply_guest_password(ctx, plugin, username, password)
        ctx.logger.info(
            "macOS guest configure done (network_mode=%s user=%s smbios=%s password_set=%s)",
            mode,
            username,
            randomize,
            bool(password),
        )


class ConfigureWindowsGuestStep(DeploymentStep):
    """Windows post-boot: network and password via qemu-ga.

    Guest filesystem expand is left to the image's own first-boot resize
    script; deployment only grows the Proxmox virtual disk in ``configure_sizing``.
    """

    name = "configure_windows_guest"
    timeout_seconds = 600
    default_retry_after_seconds = 15

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:
            return StepOutcome.wait(
                message=_MSG_GUEST_AGENT_NOT_READY,
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if not ready:
            return StepOutcome.wait(message="Waiting for guest agent before Windows configure")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.deployment.guest_config import (
            configure_windows_network,
            old_guest_passwords_from_ctx,
            set_guest_password,
            strategy_options_from_ctx,
        )

        opts = strategy_options_from_ctx(ctx)
        plugin = ctx.get_plugin()
        username = str(opts.get("guest_username") or opts.get("cloudinit_ciuser") or "Administrator")
        password = opts.get("guest_password") or opts.get("admin_password") or opts.get("cloudinit_cipassword")
        mode = str(opts.get("network_mode") or "static").lower()

        if mode == "static" and not ctx.get_ip_allocation():
            raise DeploymentError(_MSG_STATIC_NETWORK_REQUIRES_LINKED_VM_IP)
        await configure_windows_network(plugin, mode=mode, alloc=ctx.get_ip_allocation())
        if password:
            await set_guest_password(
                plugin,
                username,
                str(password),
                old_passwords=old_guest_passwords_from_ctx(ctx, include_stored=False),
            )
        ctx.logger.info(
            "Windows guest-agent configure done (network_mode=%s user=%s password_set=%s)",
            mode,
            username,
            bool(password),
        )


class ApplyGuestPasswordStep(DeploymentStep):
    """Apply the desired guest password from ``template_parameters`` via the agent.

    Used by the deferred ``apply_guest_password`` runtime job after power-on /
    agent wait. Reads live config so a newer Change Password while waiting wins.
    """

    name = "apply_guest_password"
    timeout_seconds = 300
    default_retry_after_seconds = 10

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:
            return StepOutcome.wait(
                message=_MSG_GUEST_AGENT_NOT_READY,
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if not ready:
            return StepOutcome.wait(message="Waiting for guest agent before password apply")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.deployment.guest_config import (
            old_guest_passwords_from_ctx,
            set_guest_password,
            strategy_options_from_ctx,
        )

        opts = strategy_options_from_ctx(ctx)
        tpl = (ctx.service.config or {}).get("template_parameters") or {}
        username = str(
            opts.get("guest_username")
            or tpl.get("admin_username")
            or tpl.get("guest_username")
            or "client"
        )
        password = (
            tpl.get("admin_password")
            or tpl.get("guest_password")
            or opts.get("guest_password")
            or opts.get("admin_password")
        )
        if not password:
            raise DeploymentError("No desired guest password in template_parameters")

        old_passwords = old_guest_passwords_from_ctx(ctx, include_stored=False)
        prev = tpl.get("previous_admin_password")
        if prev:
            old_passwords = [str(prev), *old_passwords]

        await set_guest_password(
            ctx.get_plugin(),
            username,
            str(password),
            old_passwords=old_passwords,
        )
        ctx.logger.info("Deferred guest password applied for user=%s", username)


def _vm_restore_state(service) -> dict:
    cfg = service.config if isinstance(service.config, dict) else {}
    state = cfg.get("vm_restore")
    return dict(state) if isinstance(state, dict) else {}


def _write_vm_restore_state(ctx, state: dict) -> None:
    cfg = dict(ctx.service.config or {})
    cfg["vm_restore"] = state
    ctx.service.config = cfg


class StopGuestForRestoreStep(DeploymentStep):
    """Power off the guest so a force-restore can overwrite the VMID."""

    name = "stop_guest_for_restore"
    timeout_seconds = 180
    default_retry_after_seconds = 5

    async def precheck(self, ctx) -> StepOutcome:
        plugin = ctx.get_plugin()
        try:
            exists = await plugin.vm_exists()
        except Exception:
            exists = False
        if not exists:
            return StepOutcome.skip("Guest absent; nothing to stop")
        try:
            state = await plugin.get_power_state()
        except Exception as exc:
            return StepOutcome.wait(
                message="Unable to read power state before restore",
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        if state == PowerState.OFF:
            return StepOutcome.skip("Guest already stopped")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.vm_backup_service import stop_guest_for_restore

        plugin = ctx.get_plugin()
        await stop_guest_for_restore(plugin)


class StartBackupRestoreStep(DeploymentStep):
    """Kick off ``qmrestore`` / PBS restore; store the Proxmox UPID for wait."""

    name = "start_backup_restore"
    timeout_seconds = 300

    async def precheck(self, ctx) -> StepOutcome:
        state = _vm_restore_state(ctx.service)
        if state.get("upid"):
            return StepOutcome.skip("Restore task already started")
        if not state.get("volid"):
            return StepOutcome.failed("vm_restore.volid missing on service config")
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.vm_backup_service import start_restore_task

        state = _vm_restore_state(ctx.service)
        result = await start_restore_task(
            ctx.db,
            ctx.service,
            volid=str(state.get("volid") or ""),
            storage=state.get("storage"),
            # Power-on happens in finalize so metadata updates run first.
            start=False,
        )
        state.update(
            {
                "upid": result.get("upid"),
                "kind": result.get("kind"),
                "normalized_volid": result.get("volid"),
                "storage": result.get("storage"),
                "status": "running",
            }
        )
        _write_vm_restore_state(ctx, state)
        if ctx.service.vm:
            from app.models.service_vm import VMGuestState

            ctx.service.vm.guest_state = VMGuestState.PROVISIONING
            ctx.service.vm.guest_last_error = None


class WaitForBackupRestoreStep(DeploymentStep):
    """Poll the Proxmox restore UPID until it finishes."""

    name = "wait_for_backup_restore"
    timeout_seconds = 7200
    default_retry_after_seconds = 10

    async def precheck(self, ctx) -> StepOutcome:
        state = _vm_restore_state(ctx.service)
        upid = state.get("upid")
        if not upid:
            return StepOutcome.failed("No restore UPID to wait on")
        plugin = ctx.get_plugin()
        try:
            data = await plugin.get_proxmox_task_status(str(upid))
        except Exception as exc:
            return StepOutcome.wait(
                message="Restore task status not readable yet",
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"last_error": str(exc)},
            )
        status = (data.get("status") or "").lower()
        if status != "stopped":
            return StepOutcome.wait(
                message="Waiting for restore task to finish",
                retry_after_seconds=self.default_retry_after_seconds,
                detail={"upid": upid, "status": status or "running"},
            )
        exitstatus = (data.get("exitstatus") or "").upper()
        if exitstatus != "OK":
            return StepOutcome.failed(f"Restore task failed: {data!r}")
        return StepOutcome.ready("Restore task finished")

    async def execute(self, ctx) -> None:
        state = _vm_restore_state(ctx.service)
        state["status"] = "restored"
        _write_vm_restore_state(ctx, state)


async def _set_guest_state_after_restore(ctx, plugin, start: bool) -> None:
    from app.models.service_vm import VMGuestState

    if start:
        try:
            powered = await plugin.power_on()
        except Exception as exc:
            raise DeploymentError(f"Failed to start VM after restore: {exc}") from exc
        if not powered:
            raise DeploymentError("Proxmox power_on returned failure after restore")
        if ctx.service.vm:
            ctx.service.vm.guest_state = VMGuestState.RUNNING
    elif ctx.service.vm:
        ctx.service.vm.guest_state = VMGuestState.STOPPED


class FinalizeBackupRestoreStep(DeploymentStep):
    """Start the guest (default) and refresh/clear OS metadata after restore."""

    name = "finalize_backup_restore"
    timeout_seconds = 300

    async def precheck(self, ctx) -> StepOutcome:
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.vm_backup_service import apply_metadata_after_restore
        from app.services.vm_identity_stamp import resolve_template_id_for_restore, stamp_vm_identity

        state = _vm_restore_state(ctx.service)
        plugin = ctx.get_plugin()
        await _set_guest_state_after_restore(ctx, plugin, bool(state.get("start", True)))

        explicit = state.get("vm_template_id")
        volid = state.get("volid") or state.get("normalized_volid")
        notes = state.get("notes")
        # Enqueue historically omitted PBS notes; resolve them from the volume list
        # so rf1 tokens in notes can recover template identity after cross-OS restore.
        if not notes and volid:
            try:
                from app.services.vm_backup_service import lookup_backup_notes

                notes = await lookup_backup_notes(
                    ctx.db,
                    ctx.service,
                    volid=str(volid),
                    storage=state.get("storage"),
                )
            except Exception as exc:
                ctx.logger.info("Could not look up backup notes for %s: %s", volid, exc)
        resolved = await resolve_template_id_for_restore(
            ctx.db,
            plugin,
            explicit_template_id=int(explicit) if explicit is not None else None,
            volid=volid,
            notes=notes,
        )
        apply_metadata_after_restore(
            ctx.db,
            ctx.service,
            vm_template_id=resolved,
        )
        try:
            await stamp_vm_identity(ctx.db, ctx.service, plugin)
        except Exception as exc:
            ctx.logger.warning("stamp_vm_identity after restore failed: %s", exc)
        # Re-read after metadata helper may have rewritten config.vm_restore.
        state = _vm_restore_state(ctx.service)
        state["status"] = "success"
        _write_vm_restore_state(ctx, state)
        if ctx.service.vm:
            ctx.service.vm.guest_last_error = None


class StampVmIdentityStep(DeploymentStep):
    """Write Proxmox description + smbios1 sku after successful provision/reinstall."""

    name = "stamp_vm_identity"
    timeout_seconds = 120

    async def precheck(self, ctx) -> StepOutcome:
        return StepOutcome.ready()

    async def execute(self, ctx) -> None:
        from app.services.vm_identity_stamp import stamp_vm_identity

        plugin = ctx.get_plugin()
        token = await stamp_vm_identity(ctx.db, ctx.service, plugin)
        ctx.logger.info("Stamped VM identity token=%s", token)
