<?php

use PHPUnit\Framework\TestCase;

/**
 * The admin services tab renders a placeholder for linked services and the
 * browser fetches the live card from admin_link.php (action=card). These cover
 * the parts that run without a WHMCS runtime: the placeholder markup the JS in
 * hooks.php looks for, and the unlinked card (built inline, no API calls).
 */
final class AdminStatusCardLazyLoadTest extends TestCase
{
    public function testPlaceholderHasLoaderHooksAndNoLiveCardId(): void
    {
        $html = rackflow_renderAdminStatusPlaceholder(
            array('serviceid' => 42, 'packageid' => 7, 'configoption1' => 'vm'),
            42,
            '15'
        );

        $this->assertStringContainsString('id="rackflow-admin-status-loading"', $html);
        $this->assertStringContainsString('data-serviceid="42"', $html);
        $this->assertStringContainsString('/modules/servers/rackflow/admin_link.php', $html);
        $this->assertStringContainsString('id="rf-as-loading-note"', $html);
        $this->assertStringContainsString('id="rf-as-loading-retry"', $html);
        $this->assertStringContainsString('Virtual machine · RackFlow #15', $html);
        // The live-card id must only appear once the real card is swapped in,
        // otherwise enhanceRackflowAdminService() would bind to the placeholder.
        $this->assertStringNotContainsString('id="rackflow-admin-status"', $html);
    }

    public function testUnlinkedCardIsBuiltInlineWithoutApiCalls(): void
    {
        $html = rackflow_buildAdminStatusCard(
            array('serviceid' => 42, 'packageid' => 7, 'configoption1' => 'bare_metal'),
            42,
            ''
        );

        $this->assertStringContainsString('id="rackflow-admin-status"', $html);
        $this->assertStringContainsString('Link a RackFlow service to see live server status.', $html);
        $this->assertStringContainsString('GET /status</th><td>—', $html);
    }
}
