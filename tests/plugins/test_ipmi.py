"""
Tests for IPMI plugin
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from app.plugins.ipmi import (
    IPMIPlugin,
    describe_chassis_power_actions,
    normalize_chassis_power_action,
)
from app.plugins.base import PowerState


def test_ipmi_plugin_initialization():
    """Test IPMI plugin initialization"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password",
        "port": 623,
        "timeout": 30
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        assert plugin.config == config
        assert plugin.PLUGIN_NAME == "ipmi"
        assert plugin.PLUGIN_VERSION == "1.0.0"


def test_ipmi_plugin_missing_ipmitool():
    """Test IPMI plugin raises error when ipmitool is not installed"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value=None):
        with pytest.raises(ImportError) as exc_info:
            IPMIPlugin(config)
        assert "ipmitool is not installed" in str(exc_info.value)


def test_ipmi_plugin_get_capabilities():
    """Test IPMI plugin capabilities"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        capabilities = plugin.get_capabilities()
        
        assert "test_connection" in capabilities
        assert "get_power_state" in capabilities
        assert "power_on" in capabilities
        assert "power_off" in capabilities
        assert "power_reset" in capabilities
        # Should only have power control capabilities
        assert len(capabilities) == 5


def test_ipmi_plugin_build_ipmitool_args():
    """Test building ipmitool command arguments"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password",
        "port": 623
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        args = plugin._build_ipmitool_args("power status")
        
        assert "ipmitool" in args
        assert "-H" in args
        assert "192.168.1.100" in args
        assert "-U" in args
        assert "admin" in args
        # Password must be delivered via env (-E), never placed on argv.
        assert "-E" in args
        assert "-P" not in args
        assert "password" not in args
        assert "-p" in args
        assert "623" in args
        assert "-I" in args
        assert "lanplus" in args
        assert "power" in args
        assert "status" in args


@pytest.mark.asyncio
async def test_ipmi_plugin_password_passed_via_env_not_argv():
    """The IPMI password must reach ipmitool through IPMI_PASSWORD env, not argv."""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "s3cr3t-pass",
        "port": 623,
    }

    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)

        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch('asyncio.create_subprocess_exec', return_value=mock_process) as mock_exec:
            await plugin._run_ipmitool("power status")

        args, kwargs = mock_exec.call_args
        assert "s3cr3t-pass" not in args
        assert kwargs["env"]["IPMI_PASSWORD"] == "s3cr3t-pass"


@pytest.mark.asyncio
async def test_ipmi_plugin_test_connection_success():
    """Test IPMI plugin test_connection with successful connection"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password",
        "port": 623,
        "timeout": 30
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        # Mock successful ipmitool execution
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(
            b"Device ID                 : 51\nDevice Revision          : 1\n",
            b""
        ))
        mock_process.returncode = 0
        
        mock_power_process = Mock()
        mock_power_process.communicate = AsyncMock(return_value=(
            b"Chassis Power is on\n",
            b""
        ))
        mock_power_process.returncode = 0
        
        with patch('asyncio.create_subprocess_exec') as mock_exec:
            mock_exec.side_effect = [mock_process, mock_power_process]
            
            result = await plugin.test_connection()
            
            assert result["success"] is True
            assert "Successfully connected" in result["message"]
            assert result["details"]["hostname"] == "192.168.1.100"
            assert result["details"]["port"] == 623


@pytest.mark.asyncio
async def test_ipmi_plugin_test_connection_failure():
    """Test IPMI plugin test_connection with failed connection"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "wrongpassword",
        "port": 623,
        "timeout": 30
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        # Mock failed ipmitool execution
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(
            b"",
            b"Authentication type mismatch\n"
        ))
        mock_process.returncode = 1
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await plugin.test_connection()
            
            assert result["success"] is False
            assert "Authentication" in result["message"] or "failed" in result["message"]


@pytest.mark.asyncio
async def test_ipmi_plugin_get_power_state():
    """Test IPMI plugin get_power_state"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(
            b"Chassis Power is on\n",
            b""
        ))
        mock_process.returncode = 0
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            power_state = await plugin.get_power_state()
            assert power_state == PowerState.ON


@pytest.mark.asyncio
async def test_ipmi_plugin_power_on():
    """Test IPMI plugin power_on"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await plugin.power_on()
            assert result is True


@pytest.mark.asyncio
async def test_ipmi_plugin_power_off():
    """Test IPMI plugin power_off"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await plugin.power_off()
            assert result is True


@pytest.mark.asyncio
async def test_ipmi_plugin_power_reset():
    """Test IPMI plugin power_reset"""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        mock_process = Mock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0
        
        with patch('asyncio.create_subprocess_exec', return_value=mock_process):
            result = await plugin.power_reset()
            assert result is True


@pytest.mark.asyncio
async def test_ipmi_plugin_not_implemented_methods():
    """User control methods are unsupported; boot order command path is executable."""
    config = {
        "hostname": "192.168.1.100",
        "username": "admin",
        "password": "password"
    }
    
    with patch('shutil.which', return_value='/usr/bin/ipmitool'):
        plugin = IPMIPlugin(config)
        
        # User account control methods should raise NotImplementedError
        with pytest.raises(NotImplementedError):
            await plugin.list_users()
        
        with pytest.raises(NotImplementedError):
            await plugin.create_user("user", "pass")
        
        # Boot order control methods should raise NotImplementedError
        with patch.object(plugin, "_run_ipmitool", new=AsyncMock(return_value=(b"Boot parameter data: force pxe\n", b"", 0))):
            boot = await plugin.get_boot_order()
            assert boot["current_device"] == "pxe"


def test_describe_chassis_power_actions_enablement():
    ids = ["on", "soft", "off", "reset", "cycle", "diag"]
    off = {row["id"]: row["enabled"] for row in describe_chassis_power_actions("off")}
    assert list(off) == ids
    assert off == {"on": True, "soft": False, "off": False, "reset": False, "cycle": False, "diag": False}
    on = {row["id"]: row["enabled"] for row in describe_chassis_power_actions(PowerState.ON)}
    assert on["on"] is False
    assert on["cycle"] is True
    unknown = describe_chassis_power_actions(None)
    assert [row["id"] for row in unknown] == ids
    assert all(row["enabled"] for row in unknown)


def test_normalize_chassis_power_aliases():
    assert normalize_chassis_power_action("reboot") == "reset"
    assert normalize_chassis_power_action("WARM") == "reset"
    assert normalize_chassis_power_action("cold") == "cycle"
    assert normalize_chassis_power_action("nmi") == "diag"
    assert normalize_chassis_power_action("shutdown") == "soft"
    assert normalize_chassis_power_action("on") == "on"


@pytest.mark.asyncio
async def test_chassis_power_maps_ipmitool_verbs():
    config = {"hostname": "192.168.1.100", "username": "admin", "password": "password"}
    with patch("shutil.which", return_value="/usr/bin/ipmitool"):
        plugin = IPMIPlugin(config)
        plugin._run_ipmitool = AsyncMock(return_value=(b"", b"", 0))
        assert await plugin.chassis_power("cycle") is True
        plugin._run_ipmitool.assert_awaited_with("power cycle")
        assert await plugin.chassis_power("reboot") is True
        plugin._run_ipmitool.assert_awaited_with("power reset")
        assert await plugin.chassis_power("diag") is True
        plugin._run_ipmitool.assert_awaited_with("power diag")


@pytest.mark.asyncio
async def test_chassis_power_rejects_unknown_action():
    config = {"hostname": "192.168.1.100", "username": "admin", "password": "password"}
    with patch("shutil.which", return_value="/usr/bin/ipmitool"):
        plugin = IPMIPlugin(config)
        with pytest.raises(ValueError, match="Unsupported chassis power action"):
            await plugin.chassis_power("explode")


@pytest.mark.asyncio
async def test_power_off_falls_back_to_hard_off():
    config = {"hostname": "192.168.1.100", "username": "admin", "password": "password"}
    with patch("shutil.which", return_value="/usr/bin/ipmitool"):
        plugin = IPMIPlugin(config)
        plugin._run_ipmitool = AsyncMock(side_effect=[(b"", b"unsupported", 1), (b"", b"", 0)])
        assert await plugin.power_off() is True
        assert plugin._run_ipmitool.await_args_list[0].args[0] == "power soft"
        assert plugin._run_ipmitool.await_args_list[1].args[0] == "power off"
