<?php

use PHPUnit\Framework\TestCase;

/**
 * The generic plugin panel renderer and the helpers around plugin_action.php.
 * The API side is covered by tests/plugin_host/test_api.py.
 */
final class PluginPanelsTest extends TestCase
{
    private function panels(): array
    {
        return array(
            array(
                'plugin_id' => 'uptime_reminder',
                'plugin_name' => 'Uptime reminder',
                'panel_id' => 'status',
                'title' => 'Uptime <reminder>',
                'health' => 'ok',
                'notice' => null,
                'elements' => array(
                    array('type' => 'badge', 'label' => 'Status', 'text' => 'Please confirm', 'tone' => 'warning'),
                    array('type' => 'field', 'label' => 'Note', 'value' => '<script>alert(1)</script>'),
                    array('type' => 'text', 'text' => 'Plain text', 'tone' => null),
                    array('type' => 'divider'),
                    array('type' => 'future_widget', 'label' => 'Skipped'),
                    array(
                        'type' => 'action',
                        'plugin_id' => 'uptime_reminder',
                        'action_id' => 'confirm',
                        'label' => 'Confirm',
                        'style' => 'primary',
                        'confirm' => 'Sure "now"?',
                        'params' => array(
                            array('key' => 'note', 'label' => 'Note', 'type' => 'string', 'required' => false, 'default' => 'x"y', 'max_length' => 200),
                            array('key' => 'days', 'label' => 'Days', 'type' => 'integer', 'required' => true, 'default' => 3, 'min' => 1, 'max' => 9),
                            array('key' => 'kind', 'label' => 'Kind', 'type' => 'select', 'default' => 'b',
                                'options' => array(array('value' => 'a', 'label' => 'A'), array('value' => 'b', 'label' => 'B'))),
                        ),
                    ),
                    array('type' => 'action', 'plugin_id' => '../evil', 'action_id' => 'x', 'label' => 'Bad'),
                ),
            ),
        );
    }

    public function testRendersAndEscapes(): void
    {
        $html = rackflow_renderPluginPanels($this->panels(), 12, '/modules/servers/rackflow/plugin_action.php', 'client');
        $this->assertStringContainsString('Uptime &lt;reminder&gt;', $html);
        $this->assertStringContainsString('&lt;script&gt;alert(1)&lt;/script&gt;', $html);
        $this->assertStringNotContainsString('<script>alert(1)', $html);
        $this->assertStringContainsString('label label-warning', $html);
        $this->assertStringContainsString('data-rf-confirm="Sure &quot;now&quot;?"', $html);
        $this->assertStringContainsString('name="p_note" value="x&quot;y"', $html);
        $this->assertStringContainsString('min="1" max="9" step="1"', $html);
        $this->assertStringContainsString('<option value="b" selected>B</option>', $html);
        $this->assertStringContainsString('data-rf-service-id="12"', $html);
        $this->assertStringContainsString('data-rf-audience="client"', $html);
        $this->assertStringNotContainsString('Skipped', $html);
        $this->assertStringNotContainsString('evil', $html);
    }

    public function testNoPanelsRendersNothing(): void
    {
        $this->assertSame('', rackflow_renderPluginPanels(array(), 1, '/x', 'client'));
    }

    public function testUnavailablePanelDisablesButtons(): void
    {
        $panels = $this->panels();
        $panels[0]['health'] = 'unavailable';
        $panels[0]['notice'] = 'Not responding';
        $html = rackflow_renderPluginPanels($panels, 1, '/x', 'admin');
        $this->assertStringContainsString('alert alert-warning">Not responding', $html);
        $this->assertStringContainsString('btn-primary" disabled', $html);
    }

    public function testIdsAreValidated(): void
    {
        $this->assertTrue(rackflow_pluginIdsValid('uptime_reminder', 'confirm'));
        $this->assertFalse(rackflow_pluginIdsValid('Up', 'confirm'));
        $this->assertFalse(rackflow_pluginIdsValid('uptime_reminder', 'a/b'));
        $this->assertFalse(rackflow_pluginIdsValid(array(), 'x'));
    }

    public function testParamValuesTakeOnlyPrefixedFields(): void
    {
        $values = rackflow_pluginParamValues(array(
            'serviceid' => '3',
            'p_note' => ' hello ',
            'p_empty' => '',
            'p_Bad' => 'x',
            'p_list' => array('x'),
            'p_flag' => '0',
        ));
        $this->assertSame(array('note' => 'hello', 'flag' => '0'), $values);
    }

    public function testAddonHookStatus(): void
    {
        $this->assertSame('active', rackflow_addonHookStatus('AddonActivation'));
        $this->assertSame('suspended', rackflow_addonHookStatus('AddonSuspended'));
        $this->assertSame('terminated', rackflow_addonHookStatus('AddonCancelled'));
        $this->assertNull(rackflow_addonHookStatus('AfterModuleCreate'));
    }

    public function testActionEndpointUrl(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);
        $this->assertSame(
            '/billing/modules/servers/rackflow/plugin_action.php',
            rackflow_pluginActionEndpointUrl('https://example.com/billing/')
        );
    }
}
