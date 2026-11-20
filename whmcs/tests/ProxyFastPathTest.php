<?php

use PHPUnit\Framework\TestCase;

/**
 * Proxy fast path panel of the proxy client area: turning the billing API
 * view into template variables, and the action endpoint URL.
 */
final class ProxyFastPathTest extends TestCase
{
    private $originalDocumentRoot;

    protected function setUp(): void
    {
        $this->originalDocumentRoot = $_SERVER['DOCUMENT_ROOT'] ?? null;
    }

    protected function tearDown(): void
    {
        if ($this->originalDocumentRoot === null) {
            unset($_SERVER['DOCUMENT_ROOT']);
        } else {
            $_SERVER['DOCUMENT_ROOT'] = $this->originalDocumentRoot;
        }
    }

    public function testNoLocationsHidesThePanel(): void
    {
        $vars = rackflow_proxyFastPathVarsFromView(array('cidr' => '100.80.0.0/16', 'locations' => array()), '/x');
        $this->assertFalse($vars['rackflow_fast_path_available']);
        $this->assertSame(rackflow_proxyFastPathEmptyVars(), $vars);
    }

    public function testOffLocationCanBeTurnedOnAndAddressesAreDeduplicated(): void
    {
        $vars = rackflow_proxyFastPathVarsFromView(array(
            'cidr' => '100.80.0.0/16',
            'locations' => array(
                array('location_id' => 1, 'available' => true, 'enabled' => false, 'status' => null,
                    'eligible' => true, 'reason' => null),
            ),
            'endpoints' => array(
                array('fast_path_ip' => '100.80.0.1', 'port' => 11080, 'public_ip' => '203.0.113.2'),
                array('fast_path_ip' => '100.80.0.1', 'port' => 11080, 'public_ip' => '203.0.113.3'),
                array('fast_path_ip' => '100.80.0.2', 'port' => 11080, 'public_ip' => '198.51.100.7'),
            ),
        ), '/modules/servers/rackflow/fastpath_action.php');
        $this->assertTrue($vars['rackflow_fast_path_available']);
        $this->assertTrue($vars['rackflow_fast_path_can_enable']);
        $this->assertFalse($vars['rackflow_fast_path_can_disable']);
        $this->assertSame("100.80.0.1:11080\n100.80.0.2:11080", $vars['rackflow_fast_path_lines']);
        $this->assertSame('100.80.0.0/16', $vars['rackflow_fast_path_cidr']);
    }

    public function testEndpointsWithCredentialsBecomeCopyableLines(): void
    {
        $vars = rackflow_proxyFastPathVarsFromView(array(
            'locations' => array(
                array('location_id' => 1, 'available' => true, 'enabled' => true, 'status' => 'active',
                    'eligible' => true, 'reason' => null),
            ),
            'endpoints' => array(
                array('fast_path_ip' => '100.80.0.1', 'port' => 11080, 'endpoint' => '100.80.0.1:11080:u1:p1'),
                array('fast_path_ip' => '100.80.0.1', 'port' => 11080, 'endpoint' => '100.80.0.1:11080:u2:p2'),
            ),
        ), '/x');
        $this->assertTrue($vars['rackflow_fast_path_has_credentials']);
        $this->assertSame("100.80.0.1:11080:u1:p1\n100.80.0.1:11080:u2:p2", $vars['rackflow_fast_path_lines']);
        $this->assertSame('100.80.0.1:11080:u2:p2', $vars['rackflow_fast_path_endpoints'][1]['line']);
    }

    public function testEndpointsWithoutCredentialsKeepIpAndPort(): void
    {
        $vars = rackflow_proxyFastPathVarsFromView(array(
            'locations' => array(
                array('location_id' => 1, 'available' => true, 'enabled' => true, 'status' => 'active',
                    'eligible' => true, 'reason' => null),
            ),
            'endpoints' => array(
                array('fast_path_ip' => '100.80.0.1', 'port' => 11080, 'endpoint' => null),
            ),
        ), '/x');
        $this->assertFalse($vars['rackflow_fast_path_has_credentials']);
        $this->assertSame('100.80.0.1:11080', $vars['rackflow_fast_path_lines']);
    }

    public function testIneligibleClientAndEnabledLocation(): void
    {
        $missing = rackflow_proxyFastPathVarsFromView(array('locations' => array(
            array('available' => true, 'enabled' => false, 'status' => null, 'eligible' => false,
                'reason' => 'The proxy fast path needs an active server in this location'),
        )), '/x');
        $this->assertFalse($missing['rackflow_fast_path_can_enable']);

        $on = rackflow_proxyFastPathVarsFromView(array('locations' => array(
            array('available' => true, 'enabled' => true, 'status' => 'active', 'eligible' => true, 'reason' => null),
        )), '/x');
        $this->assertFalse($on['rackflow_fast_path_can_enable']);
        $this->assertTrue($on['rackflow_fast_path_can_disable']);
    }

    public function testActionUrlUnderSubdirectoryFallback(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);
        $this->assertSame(
            '/billing/modules/servers/rackflow/fastpath_action.php',
            rackflow_proxyFastPathActionUrl('https://example.com/billing/')
        );
    }
}
