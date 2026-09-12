"""Concrete VM deployment strategies.

Strategy names match the existing ``vm_plan.strategy_name`` values so services
planned today resolve to the right pipeline (see
``app/services/vm_install_type_strategy.py``).
"""
from __future__ import annotations

from typing import List, Sequence

from app.services.deployment.actions import (
    OptionField,
    RANDOMIZE_SMBIOS_ACTION,
    StrategyAction,
)

_LABEL_GUEST_USERNAME = "Guest username"
_LABEL_NETWORK_MODE = "Network mode"
_HELP_NETWORK_MODE = "static uses Rackflow VM IP allocation; dhcp leaves DHCP."
_LABEL_CLIENT_ACTIONS = "Client-visible actions"
_HELP_CLIENT_ACTIONS = "Action names end-users may invoke (also gated by permissions)."
from app.services.deployment.step import DeploymentStep
from app.services.deployment.steps import (
    ApplyGuestPasswordStep,
    CloneFromTemplateStep,
    ConfigureCloudInitNetworkStep,
    ConfigureMacosGuestStep,
    ConfigureSizingStep,
    ConfigureViaGuestAgentStep,
    ConfigureWindowsGuestStep,
    FinalizeBackupRestoreStep,
    PowerOnStep,
    StampVmIdentityStep,
    StartBackupRestoreStep,
    StopGuestForRestoreStep,
    WaitForBackupRestoreStep,
    WaitForGuestAgentStep,
)
from app.services.deployment.strategy import DeploymentStrategy


class CloudinitCloneStrategy(DeploymentStrategy):
    """Clone template, size, apply cloud-init static IP, then power on."""

    name = "cloudinit_clone"

    def steps(self) -> List[DeploymentStep]:
        return [
            CloneFromTemplateStep(),
            ConfigureSizingStep(),
            ConfigureCloudInitNetworkStep(),
            PowerOnStep(),
            StampVmIdentityStep(),
        ]

    def option_schema(self) -> List[OptionField]:
        # Linux cloud-init templates use root only — no extra OS user.
        return [
            OptionField(
                name="guest_username",
                label=_LABEL_GUEST_USERNAME,
                field_type="string",
                default="root",
                description="Always root for cloud-init Linux; password is applied to root (ciuser).",
            ),
            OptionField(
                name="network_mode",
                label=_LABEL_NETWORK_MODE,
                field_type="select",
                default="static",
                choices=["static", "dhcp"],
                description=_HELP_NETWORK_MODE,
            ),
            OptionField(
                name="client_actions",
                label=_LABEL_CLIENT_ACTIONS,
                field_type="string_list",
                default=["change_password"],
                description=_HELP_CLIENT_ACTIONS,
            ),
        ]


class GuestAgentStrategy(DeploymentStrategy):
    """Clone template, size, power on, wait for the guest agent, then configure."""

    name = "guest_agent"

    def steps(self) -> List[DeploymentStep]:
        return [
            CloneFromTemplateStep(),
            ConfigureSizingStep(),
            PowerOnStep(),
            WaitForGuestAgentStep(),
            ConfigureViaGuestAgentStep(),
            StampVmIdentityStep(),
        ]


class MacosGuestAgentStrategy(DeploymentStrategy):
    """Clone macOS template; configure SMBIOS/network/password via guest agent."""

    name = "macos_guest_agent"

    def steps(self) -> List[DeploymentStep]:
        return [
            CloneFromTemplateStep(),
            ConfigureSizingStep(),
            PowerOnStep(),
            WaitForGuestAgentStep(timeout_seconds=900),
            ConfigureMacosGuestStep(),
            StampVmIdentityStep(),
        ]

    def option_schema(self) -> List[OptionField]:
        return [
            OptionField(
                name="guest_username",
                label=_LABEL_GUEST_USERNAME,
                field_type="string",
                default="client",
                description="macOS user for password changes.",
            ),
            OptionField(
                name="template_password",
                label="Template password",
                field_type="string",
                default="client",
                description=(
                    "Current password on the Proxmox template image. Required to "
                    "set a new password for Secure Token users (dscl old→new)."
                ),
            ),
            OptionField(
                name="network_mode",
                label=_LABEL_NETWORK_MODE,
                field_type="select",
                default="static",
                choices=["static", "dhcp"],
                description=_HELP_NETWORK_MODE,
            ),
            OptionField(
                name="randomize_smbios",
                label="Randomize SMBIOS on provision",
                field_type="bool",
                default=True,
                description="Rewrite OpenCore PlatformInfo + Proxmox smbios1 after clone.",
            ),
            OptionField(
                name="smbios_model",
                label="SMBIOS model",
                field_type="string",
                default="iMacPro1,1",
                description="SystemProductName used when generating SMBIOS.",
            ),
            OptionField(
                name="opencore_disk",
                label="OpenCore disk identifier",
                field_type="string",
                default="disk1s1",
                description="Guest diskutil identifier for the OPENCORE EFI volume.",
            ),
            OptionField(
                name="opencore_config",
                label="OpenCore config.plist path",
                field_type="string",
                default="/Volumes/OPENCORE/EFI/OC/config.plist",
            ),
            OptionField(
                name="client_actions",
                label=_LABEL_CLIENT_ACTIONS,
                field_type="string_list",
                default=["change_password"],
                description=_HELP_CLIENT_ACTIONS,
            ),
        ]

    def extra_actions(self) -> Sequence[StrategyAction]:
        return (RANDOMIZE_SMBIOS_ACTION,)


class WindowsGuestAgentStrategy(DeploymentStrategy):
    """Clone Windows template; configure network/password via QEMU guest agent."""

    name = "windows_guest_agent"

    def steps(self) -> List[DeploymentStep]:
        return [
            CloneFromTemplateStep(),
            ConfigureSizingStep(),
            PowerOnStep(),
            WaitForGuestAgentStep(timeout_seconds=900),
            ConfigureWindowsGuestStep(),
            StampVmIdentityStep(),
        ]

    def option_schema(self) -> List[OptionField]:
        return [
            OptionField(
                name="guest_username",
                label=_LABEL_GUEST_USERNAME,
                field_type="string",
                default="Administrator",
                description="Windows local user for password changes (default Administrator).",
            ),
            OptionField(
                name="network_mode",
                label=_LABEL_NETWORK_MODE,
                field_type="select",
                default="static",
                choices=["static", "dhcp"],
                description=_HELP_NETWORK_MODE,
            ),
            OptionField(
                name="client_actions",
                label=_LABEL_CLIENT_ACTIONS,
                field_type="string_list",
                default=["change_password"],
                description=_HELP_CLIENT_ACTIONS,
            ),
        ]


class ApplyGuestPasswordStrategy(DeploymentStrategy):
    """Runtime job: power on if needed, wait for agent, apply stored guest password.

    Not a provision pipeline — used when Change Password runs while the VM is
    off or the guest agent is not ready yet.
    """

    name = "apply_guest_password"
    include_default_actions = False
    mutates_provision_lifecycle = False

    def steps(self) -> List[DeploymentStep]:
        return [
            PowerOnStep(),
            WaitForGuestAgentStep(timeout_seconds=900),
            ApplyGuestPasswordStep(),
        ]


class RestoreBackupStrategy(DeploymentStrategy):
    """Runtime job: stop guest, restore backup, wait, start, refresh metadata.

    Params live on ``service.config.vm_restore`` (volid/storage/start/template).
    """

    name = "restore_backup"
    include_default_actions = False
    mutates_provision_lifecycle = False
    max_attempts = 2

    def steps(self) -> List[DeploymentStep]:
        return [
            StopGuestForRestoreStep(),
            StartBackupRestoreStep(),
            WaitForBackupRestoreStep(),
            FinalizeBackupRestoreStep(),
        ]
