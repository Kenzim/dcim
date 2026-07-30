import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.service import ServiceType
from app.services import vm_ssh_keys_service as mod


def service(**attrs):
    obj = MagicMock(id=4, service_type=ServiceType.VM, product_code="", config={})
    obj.vm = MagicMock(vm_template_id=1)
    for key, value in attrs.items(): setattr(obj, key, value)
    return obj


@pytest.mark.asyncio
async def test_save_rejects_non_vm():
    candidate = service(service_type=MagicMock())
    with pytest.raises(mod.VmSshKeysError, match="only supported"):
        await mod.save_and_apply_ssh_public_keys(MagicMock(), candidate, "ssh-ed25519 AAA")


@pytest.mark.asyncio
async def test_save_rejects_unsupported_strategy(monkeypatch):
    monkeypatch.setattr(mod, "service_accepts_ssh_key", lambda *a, **k: False)
    with pytest.raises(mod.VmSshKeysError) as exc:
        await mod.save_and_apply_ssh_public_keys(MagicMock(), service(), "ssh-ed25519 AAA")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_save_parsing_error(monkeypatch):
    monkeypatch.setattr(mod, "service_accepts_ssh_key", lambda *a, **k: True)
    monkeypatch.setattr(mod, "parse_ssh_public_keys", lambda _: (_ for _ in ()).throw(mod.SshPublicKeyError("invalid")))
    with pytest.raises(mod.VmSshKeysError, match="invalid"):
        await mod.save_and_apply_ssh_public_keys(MagicMock(), service(), "bad")


@pytest.mark.asyncio
async def test_save_stores_without_live_apply(monkeypatch):
    candidate, db = service(), MagicMock()
    monkeypatch.setattr(mod, "service_accepts_ssh_key", lambda *a, **k: True)
    monkeypatch.setattr(mod, "parse_ssh_public_keys", lambda _: ["ssh-ed25519 AAA"])
    with patch.object(mod, "set_ssh_public_keys_on_service") as set_keys, patch.object(mod.ServiceDAO, "update") as update:
        result = await mod.save_and_apply_ssh_public_keys(db, candidate, "x", apply_live=False)
    assert result["applied"] is False and result["has_ssh_public_keys"]
    set_keys.assert_called_once(); update.assert_called_once_with(db, candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,expected", [(False, False), (True, True)])
async def test_try_apply_authorized_keys(monkeypatch, ready, expected):
    plugin = MagicMock(guest_agent_ready=AsyncMock(return_value=ready))
    monkeypatch.setattr(mod, "resolve_proxmox_plugin_for_service", AsyncMock(return_value=(plugin, 1, "n", 4)))
    with patch.object(mod, "apply_root_authorized_keys", new=AsyncMock()) as apply:
        assert await mod._try_apply_authorized_keys(MagicMock(), service(), ["k"]) is expected
    assert apply.await_count == int(expected)


@pytest.mark.asyncio
async def test_try_apply_returns_false_for_placement_and_agent_error(monkeypatch):
    monkeypatch.setattr(mod, "resolve_proxmox_plugin_for_service", AsyncMock(side_effect=mod.ProxmoxPlacementError("no")))
    assert not await mod._try_apply_authorized_keys(MagicMock(), service(), [])
    plugin = MagicMock(guest_agent_ready=AsyncMock(side_effect=RuntimeError("bad")))
    monkeypatch.setattr(mod, "resolve_proxmox_plugin_for_service", AsyncMock(return_value=(plugin, 1, "n", 4)))
    assert not await mod._try_apply_authorized_keys(MagicMock(), service(), [])


@pytest.mark.parametrize("template", [None, MagicMock(enabled=False)])
def test_template_change_rejects_missing_or_disabled(monkeypatch, template):
    monkeypatch.setattr(mod.VMTemplateDAO, "get_by_id", lambda *a: template)
    with pytest.raises(mod.VmSshKeysError) as exc:
        mod.apply_template_change_for_reinstall(MagicMock(), service(), vm_template_id=2)
    assert exc.value.status_code == 404


def test_template_change_rejects_unlinked_template(monkeypatch):
    candidate = service(product_code="p"); template = MagicMock(id=2, enabled=True)
    product = MagicMock(vm_template_mappings=[MagicMock(vm_template_id=3)])
    monkeypatch.setattr(mod.VMTemplateDAO, "get_by_id", lambda *a: template)
    monkeypatch.setattr(mod.ProductDAO, "get_by_code", lambda *a: product)
    with pytest.raises(mod.VmSshKeysError, match="not linked"):
        mod.apply_template_change_for_reinstall(MagicMock(), candidate, vm_template_id=2)


def test_stored_keys_and_template_listing(monkeypatch):
    candidate = service(product_code="p", config={"ssh_public_keys": ["k"]})
    monkeypatch.setattr(mod, "ssh_public_keys_from_service_config", lambda config: config["ssh_public_keys"])
    assert mod.stored_keys_for_service(candidate) == ["k"]
    template = MagicMock(id=2, name="Zulu", os_type="linux", enabled=True)
    product = MagicMock(vm_template_mappings=[MagicMock(vm_template=template)])
    monkeypatch.setattr(mod.ProductDAO, "get_by_code", lambda *a: product)
    with patch("app.services.vm_install_type_strategy.resolve_vm_template_strategy", return_value={"strategy_name": "cloud"}), \
         patch("app.services.ssh_public_keys.os_type_accepts_ssh_key", return_value=True):
        assert mod.list_reinstall_templates_for_service(MagicMock(), candidate)[0]["strategy_name"] == "cloud"
