<?php

use PHPUnit\Framework\TestCase;

/**
 * Pure-PHP parts of the backup slot support: the product option, the AJAX
 * endpoint URL and the client-area variable defaults. Calls that reach the
 * RackFlow API are covered by tests/api/test_backup_slots_api.py.
 */
final class BackupSlotModuleTest extends TestCase
{
    public function testServiceTypeDropdownOffersBackupSlot(): void
    {
        $options = rackflow_ConfigOptions();
        $types = explode(',', $options['Service Type']['Options']);
        $this->assertContains('backup_slot', $types);
    }

    public function testSlotActionEndpointUrl(): void
    {
        $_SERVER['DOCUMENT_ROOT'] = realpath(__DIR__ . '/../');
        $this->assertSame('/modules/servers/rackflow/slot_action.php', rackflow_slotActionEndpointUrl());
    }

    public function testSlotActionEndpointUrlFallsBackToSystemUrlPath(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);
        $this->assertSame(
            '/billing/modules/servers/rackflow/slot_action.php',
            rackflow_slotActionEndpointUrl('https://example.com/billing/')
        );
    }

    public function testClientAreaVarsAreEmptyWithoutAService(): void
    {
        $vars = rackflow_slotClientAreaVars(array(), 'backup_slot', false);
        $this->assertFalse($vars['rackflow_slot_panel']);
        $this->assertSame(array(), $vars['rackflow_vm_slots']);
        $this->assertSame('', $vars['rackflow_slot_error']);
    }

    public function testSlotActionScriptAndModuleAreValidPhp(): void
    {
        $this->assertFileExists(__DIR__ . '/../modules/servers/rackflow/slot_action.php');
        $source = file_get_contents(__DIR__ . '/../modules/servers/rackflow/slot_action.php');
        foreach (array('retain', 'release', 'restore', 'confirm_data_loss', 'rackflow_isAjaxRequest') as $needle) {
            $this->assertStringContainsString($needle, $source);
        }
    }
}
