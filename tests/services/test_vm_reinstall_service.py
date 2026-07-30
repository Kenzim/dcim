import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.service import ServiceType
from app.models.service_vm import VMGuestState
from app.plugins.base import PowerState
from app.services import vm_reinstall_service as mod


def service(vm=True):
    result = MagicMock(id=12, service_type=ServiceType.VM)
    result.vm = MagicMock(vm_template_id=3, guest_state=None, guest_last_error="old") if vm else None
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["not-vm", "no-vm"])
async def test_reinstall_rejects_non_vm(kind):
    candidate = service(vm=kind != "no-vm")
    if kind == "not-vm": candidate.service_type = MagicMock()
    with pytest.raises(mod.VmReinstallError, match="Not a VM service") as exc:
        await mod.reinstall_vm_guest(MagicMock(), candidate)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reinstall_translates_template_error():
    candidate = service()
    with patch.object(mod, "apply_template_change_for_reinstall", side_effect=mod.VmSshKeysError("bad", 409)):
        with pytest.raises(mod.VmReinstallError, match="bad") as exc:
            await mod.reinstall_vm_guest(MagicMock(), candidate, vm_template_id=4)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reinstall_translates_placement_error():
    candidate = service()
    error = mod.ProxmoxPlacementError("missing", status_code=404)
    with patch.object(mod, "resolve_proxmox_plugin_for_service", side_effect=error):
        with pytest.raises(mod.VmReinstallError, match="missing") as exc:
            await mod.reinstall_vm_guest(MagicMock(), candidate)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_reinstall_existing_running_guest_forces_off_then_deletes():
    candidate, db = service(), MagicMock()
    plugin = MagicMock(vm_exists=AsyncMock(return_value=True), get_power_state=AsyncMock(side_effect=[PowerState.ON, PowerState.ON, PowerState.OFF]),
                       power_off=AsyncMock(), delete_vm=AsyncMock())
    with patch.object(mod, "resolve_proxmox_plugin_for_service", new=AsyncMock(return_value=(plugin, 1, "n", 99))), \
         patch.object(mod, "provision_vm_service_async", return_value=(candidate, MagicMock())), \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()), \
         patch.object(mod.ServiceDAO, "update") as update:
        outcome = await mod.reinstall_vm_guest(db, candidate)
    plugin.delete_vm.assert_awaited_once_with({"vmid": 99}); update.assert_called_once_with(db, candidate)
    assert outcome["proxmox_vmid"] == 99 and candidate.vm.guest_state == VMGuestState.PROVISIONING


@pytest.mark.asyncio
@pytest.mark.parametrize("provision_error,status", [(ValueError("invalid"), 400), (RuntimeError("queue"), 502)])
async def test_reinstall_translates_provision_errors(provision_error, status):
    candidate, plugin = service(), MagicMock(vm_exists=AsyncMock(return_value=False))
    with patch.object(mod, "resolve_proxmox_plugin_for_service", new=AsyncMock(return_value=(plugin, 1, "n", 7))), \
         patch.object(mod, "provision_vm_service_async", side_effect=provision_error), \
         patch.object(mod.ServiceDAO, "update"):
        with pytest.raises(mod.VmReinstallError) as exc:
            await mod.reinstall_vm_guest(MagicMock(), candidate)
    assert exc.value.status_code == status


@pytest.mark.asyncio
async def test_reinstall_ignores_exists_and_delete_failures():
    candidate = service(); plugin = MagicMock(vm_exists=AsyncMock(side_effect=RuntimeError("no")), delete_vm=AsyncMock())
    with patch.object(mod, "resolve_proxmox_plugin_for_service", new=AsyncMock(return_value=(plugin, 1, "n", 7))), \
         patch.object(mod, "provision_vm_service_async", return_value=(candidate, None)), patch.object(mod.ServiceDAO, "update"):
        assert (await mod.reinstall_vm_guest(MagicMock(), candidate))["status"] == "ok"
