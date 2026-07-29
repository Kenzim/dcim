<?php

use PHPUnit\Framework\TestCase;

final class RackflowResellerModuleTest extends TestCase
{
    private function baseParams(): array
    {
        return array(
            'serviceid' => 321,
            'userid' => 654,
            'packageid' => 11,
            'productname' => 'Reseller product',
            'domain' => 'server.example.test',
            'password' => 'guest-password',
            'clientsdetails' => array(
                'username' => 'client-654',
                'email' => 'client@example.test',
            ),
            'customfields' => array(
                'SSH Public Keys' => 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test@example',
            ),
            'configoptions' => array(),
        );
    }

    public function testModulesCanBeLoadedTogetherWithoutFunctionCollision(): void
    {
        $this->assertTrue(function_exists('rackflow_MetaData'));
        $this->assertTrue(function_exists('rackflow_reseller_MetaData'));
        $this->assertSame('RackFlow Reseller', rackflow_reseller_MetaData()['DisplayName']);
    }

    public function testApiRequestUsesServerBaseBearerAuthAndStrictTls(): void
    {
        $base = rackflow_reseller_buildApiBase('rackflow.example.test', '8443', true);
        $this->assertSame('https://rackflow.example.test:8443', $base);

        $request = rackflow_reseller_buildApiRequest(
            $base,
            'rsk_example',
            'POST',
            '/api/reseller/services/7/power',
            array('action' => 'off'),
            array('Idempotency-Key' => 'whmcs-service-321')
        );
        $this->assertSame('https://rackflow.example.test:8443/api/reseller/services/7/power', $request['url']);
        $this->assertContains('Authorization: Bearer rsk_example', $request['headers']);
        $this->assertContains('Idempotency-Key: whmcs-service-321', $request['headers']);
        $this->assertTrue($request['curl_options']['ssl_verify_peer']);
        $this->assertSame(2, $request['curl_options']['ssl_verify_host']);
        $this->assertSame(5, $request['curl_options']['connect_timeout']);
        $this->assertLessThanOrEqual(45, $request['curl_options']['timeout']);
    }

    public function testApiRequestRejectsEveryNonResellerSurface(): void
    {
        $this->expectException(InvalidArgumentException::class);
        rackflow_reseller_buildApiRequest(
            'https://rackflow.example.test',
            'rsk_example',
            'GET',
            '/api/users/me'
        );
    }

    public function testBareMetalCreatePayloadAndIdempotencyHeader(): void
    {
        $params = $this->baseParams();
        $params['configoption1'] = 'bare_metal';
        $params['configoption2'] = 'bm-small';
        $params['configoption3'] = 'ubuntu-24';
        $params['configoption4'] = '9|London Rack';

        $request = rackflow_reseller_buildCreateRequest($params);
        $this->assertTrue($request['ok']);
        $this->assertSame('/api/reseller/bare-metal/services', $request['endpoint']);
        $this->assertSame('whmcs-service-321', $request['headers']['Idempotency-Key']);
        $this->assertSame('bare_metal', $request['payload']['service_type']);
        $this->assertSame('bm-small', $request['payload']['product_code']);
        $this->assertSame('ubuntu-24', $request['payload']['os_code']);
        $this->assertSame(9, $request['payload']['service_config']['server_group_id']);
        $this->assertSame('654', $request['payload']['external_user_id']);
        $this->assertSame(
            'guest-password',
            $request['payload']['service_config']['template_parameters']['admin_password']
        );
        $this->assertCount(
            1,
            $request['payload']['service_config']['template_parameters']['ssh_public_keys']
        );
    }

    public function testVmCreatePayloadUsesTemplateClusterAndNode(): void
    {
        $params = $this->baseParams();
        $params['configoption1'] = 'vm';
        $params['configoption2'] = 'vm-medium';
        $params['configoption5'] = '4|Manchester';
        $params['configoption6'] = 'pve-02';
        $params['configoptions']['Operating System'] = 'rfvt:77|Debian 13';

        $request = rackflow_reseller_buildCreateRequest($params);
        $this->assertTrue($request['ok']);
        $this->assertSame('/api/reseller/vm/services', $request['endpoint']);
        $this->assertSame(77, $request['payload']['vm_template_id']);
        $this->assertArrayNotHasKey('os_code', $request['payload']);
        $this->assertSame(4, $request['payload']['proxmox_cluster_id']);
        $this->assertSame('pve-02', $request['payload']['proxmox_node_name']);
        $this->assertTrue($request['payload']['auto_provision']);
    }

    public function testVmCreatePayloadResolvesOsCodeConfigOption(): void
    {
        $params = $this->baseParams();
        $params['configoption1'] = 'vm';
        $params['configoption2'] = 'vm-medium';
        $params['configoption3'] = 'rfvt:77';
        $params['configoption5'] = '1';

        $request = rackflow_reseller_buildCreateRequest($params);
        $this->assertTrue($request['ok']);
        $this->assertSame(77, $request['payload']['vm_template_id']);
        $this->assertArrayNotHasKey('os_code', $request['payload']);
        $this->assertSame(1, $request['payload']['proxmox_cluster_id']);
    }

    public function testHttpProxyCreatePayloadStaysOnBareMetalCreateRoute(): void
    {
        $params = $this->baseParams();
        $params['configoption1'] = 'http_proxy';
        $params['configoption2'] = 'proxy-10';

        $request = rackflow_reseller_buildCreateRequest($params);
        $this->assertTrue($request['ok']);
        $this->assertSame('/api/reseller/bare-metal/services', $request['endpoint']);
        $this->assertSame('http_proxy', $request['payload']['service_type']);
        $this->assertArrayNotHasKey('os_code', $request['payload']);
    }

    public function testNestedCreateEnvelopeIsRequiredAndParsed(): void
    {
        $parsed = rackflow_reseller_parseCreateEnvelope(array(
            'service' => array('id' => 88, 'status' => 'pending'),
            'invoice' => array('id' => 99),
            'idempotent_replay' => true,
        ));
        $this->assertTrue($parsed['ok']);
        $this->assertSame(88, $parsed['service']['id']);
        $this->assertSame(99, $parsed['invoice']['id']);
        $this->assertTrue($parsed['idempotent_replay']);

        $invalid = rackflow_reseller_parseCreateEnvelope(array('id' => 88));
        $this->assertFalse($invalid['ok']);
        $this->assertStringContainsString('nested service.id', $invalid['error']);
    }

    public function testPaymentRequiredErrorIncludesStableAmountsAndInvoice(): void
    {
        $message = rackflow_reseller_errorMessage(array(
            'http_code' => 402,
            'data' => array(
                'detail' => array(
                    'code' => 'insufficient_credit',
                    'required_cents' => 3000,
                    'available_cents' => 500,
                    'shortfall_cents' => 2500,
                    'invoice_id' => 42,
                ),
            ),
        ));
        $this->assertStringContainsString('required 3000 cents', $message);
        $this->assertStringContainsString('available 500 cents', $message);
        $this->assertStringContainsString('shortfall 2500 cents', $message);
        $this->assertStringContainsString('invoice ID 42', $message);
        $this->assertStringContainsString('No service was provisioned', $message);
    }

    public function testLifecycleAndClientActionEndpointsAreResellerScoped(): void
    {
        $this->assertSame('/api/reseller/services/17', rackflow_reseller_serviceEndpoint(17));
        $this->assertSame('/api/reseller/services/17/suspend', rackflow_reseller_serviceEndpoint(17, '/suspend'));
        $this->assertSame('/api/reseller/services/17/unsuspend', rackflow_reseller_serviceEndpoint(17, '/unsuspend'));
        $this->assertSame('/api/reseller/services/17/power', rackflow_reseller_serviceEndpoint(17, '/power'));
        $this->assertSame('/api/reseller/services/17/status', rackflow_reseller_serviceEndpoint(17, '/status'));
        $this->assertSame('/api/reseller/services/17/portal-sso', rackflow_reseller_serviceEndpoint(17, '/portal-sso'));
    }

    public function testNewModuleContainsOnlyPrefixedFunctionsAndResellerApi(): void
    {
        $directory = realpath(__DIR__ . '/../modules/servers/rackflow_reseller');
        $this->assertNotFalse($directory);
        $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory));
        foreach ($iterator as $file) {
            if (!$file->isFile()) {
                continue;
            }
            $contents = file_get_contents($file->getPathname());
            $this->assertStringNotContainsString('/api/' . 'billing', $contents, $file->getPathname());
            if ($file->getExtension() !== 'php') {
                continue;
            }
            preg_match_all('/function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/', $contents, $matches);
            foreach ($matches[1] as $functionName) {
                $this->assertStringStartsWith('rackflow_reseller_', $functionName, $file->getPathname());
            }
        }
    }
}
