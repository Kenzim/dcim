<?php

use PHPUnit\Framework\TestCase;

/**
 * The VM firewall section: the checks of firewall_action.php (run through
 * rackflow_firewallHandle with fake lookups, so no WHMCS database and no RackFlow call is made),
 * the client area fetch gate and the escaping of the template.
 * The billing routes themselves are covered by tests/api/test_vm_firewall_billing_api.py.
 */
final class FirewallActionTest extends TestCase
{
    /** @var array<int, array{0:int,1:string}> calls the fake RackFlow client received */
    private $calls = array();

    protected function setUp(): void
    {
        $this->calls = array();
    }

    /** WHMCS service 10 belongs to client 5 and is linked to RackFlow service 700; 11 to client 6 (701). */
    private function deps(array $overrides = array()): array
    {
        $hosting = array(
            10 => (object)array('id' => 10, 'userid' => 5, 'packageid' => 3, 'server' => 1),
            11 => (object)array('id' => 11, 'userid' => 6, 'packageid' => 3, 'server' => 1),
            12 => (object)array('id' => 12, 'userid' => 5, 'packageid' => 4, 'server' => 1),
            13 => (object)array('id' => 13, 'userid' => 5, 'packageid' => 3, 'server' => 1),
        );
        $links = array(10 => 700, 11 => 701, 12 => 702);
        $self = $this;
        return $overrides + array(
            'hosting' => function ($id) use ($hosting) {
                return isset($hosting[$id]) ? $hosting[$id] : null;
            },
            'product' => function ($id) {
                return (object)array('servertype' => $id === 4 ? 'cpanel' : 'rackflow');
            },
            'link' => function (array $params) use ($links) {
                return isset($links[$params['serviceid']]) ? $links[$params['serviceid']] : null;
            },
            'call' => function (array $params, $rackflowId, $method, $suffix, $body) use ($self) {
                $self->calls[] = array($rackflowId, $method . ' ' . $suffix, $body);
                return array('success' => true, 'data' => array('revision' => 8));
            },
        );
    }

    private function handle(array $post, $isAdmin = false, $clientId = 5, $method = 'POST', $ajax = true, array $deps = null): array
    {
        return rackflow_firewallHandle($method, $ajax, $post, $isAdmin, $clientId, $deps === null ? $this->deps() : $deps);
    }

    private function enable(array $extra = array()): array
    {
        return $extra + array('serviceid' => '10', 'op' => 'enable', 'revision' => '7');
    }

    public function testPostIsRequired(): void
    {
        list($status, $payload) = $this->handle($this->enable(), false, 5, 'GET');
        $this->assertSame(405, $status);
        $this->assertFalse($payload['ok']);
        $this->assertSame(array(), $this->calls);
    }

    public function testAjaxHeaderIsRequired(): void
    {
        list($status) = $this->handle($this->enable(), false, 5, 'POST', false);
        $this->assertSame(403, $status);
        $this->assertSame(array(), $this->calls);
    }

    public function testLoginIsRequired(): void
    {
        list($status, $payload) = $this->handle($this->enable(), false, 0);
        $this->assertSame(403, $status);
        $this->assertSame('Login required.', $payload['error']);
        $this->assertSame(array(), $this->calls);
    }

    public function testAnotherClientsServiceIsRefusedAndRackflowIsNotCalled(): void
    {
        list($status, $payload) = $this->handle($this->enable(array('serviceid' => '11')));
        $this->assertSame(403, $status);
        $this->assertSame('Access denied.', $payload['error']);
        $this->assertSame(array(), $this->calls);
    }

    public function testUnknownServiceIsNotFound(): void
    {
        list($status) = $this->handle($this->enable(array('serviceid' => '999')));
        $this->assertSame(404, $status);
        $this->assertSame(array(), $this->calls);
    }

    public function testNonRackflowProductIsRefused(): void
    {
        list($status, $payload) = $this->handle($this->enable(array('serviceid' => '12')));
        $this->assertSame(400, $status);
        $this->assertSame('Not a RackFlow service.', $payload['error']);
        $this->assertSame(array(), $this->calls);
    }

    public function testUnlinkedServiceIsRefused(): void
    {
        list($status, $payload) = $this->handle($this->enable(array('serviceid' => '13')));
        $this->assertSame(400, $status);
        $this->assertSame('This service is not linked to RackFlow.', $payload['error']);
        $this->assertSame(array(), $this->calls);
    }

    /** @dataProvider rackflowIdFields */
    public function testATamperedRackflowServiceIdFieldIsRefusedAndRackflowIsNotCalled(string $field): void
    {
        list($status, $payload) = $this->handle($this->enable(array($field => '701')));
        $this->assertSame(400, $status);
        $this->assertFalse($payload['ok']);
        $this->assertSame(array(), $this->calls);
    }

    public function rackflowIdFields(): array
    {
        return array_map(function ($field) {
            return array($field);
        }, array('rackflow_service_id', 'rackflowserviceid', 'rackflow_id', 'service_id', 'slot_id', 'target_id', 'source_service_id'));
    }

    public function testTheLinkedRackflowIdIsTheOnlyOneSent(): void
    {
        list($status, $payload) = $this->handle($this->enable(array('confirm_interruption' => '1')));
        $this->assertSame(200, $status);
        $this->assertTrue($payload['ok']);
        $this->assertSame(8, $payload['revision']);
        $this->assertCount(1, $this->calls);
        $this->assertSame(700, $this->calls[0][0]);
        $this->assertSame('PUT ', $this->calls[0][1]);
        $this->assertSame(array('revision' => 7, 'client_enabled' => true, 'confirm_interruption' => true), $this->calls[0][2]);
    }

    public function testAnAdminSessionMayActOnAnyServiceButStillUsesTheLinkedId(): void
    {
        list($status) = $this->handle($this->enable(array('serviceid' => '11')), true, 0);
        $this->assertSame(200, $status);
        $this->assertSame(701, $this->calls[0][0]);
    }

    public function testAnAdminSessionDoesNotSkipTheServerTypeCheck(): void
    {
        list($status) = $this->handle($this->enable(array('serviceid' => '12')), true, 0);
        $this->assertSame(400, $status);
        $this->assertSame(array(), $this->calls);
    }

    public function testDisableAndPolicyBodies(): void
    {
        $this->handle(array('serviceid' => '10', 'op' => 'disable', 'revision' => '7'));
        $this->handle(array('serviceid' => '10', 'op' => 'policy', 'revision' => '7', 'policy_in' => 'drop', 'policy_out' => 'ACCEPT'));
        $this->assertSame(array('revision' => 7, 'client_enabled' => false), $this->calls[0][2]);
        $this->assertSame(array('revision' => 7, 'policy_in' => 'DROP', 'policy_out' => 'ACCEPT'), $this->calls[1][2]);
    }

    public function testPolicyNeedsAKnownValue(): void
    {
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'policy', 'revision' => '7', 'policy_in' => 'MAYBE'));
        $this->assertSame(400, $status);
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'policy', 'revision' => '7'));
        $this->assertSame(400, $status);
        $this->assertSame(array(), $this->calls);
    }

    public function testRulesAreSavedWithTheBulkReplace(): void
    {
        $rules = array(
            array('kind' => 'rule', 'direction' => 'in', 'action' => 'ACCEPT', 'protocol' => 'tcp', 'ports' => '22', 'enabled' => true),
            array('kind' => 'group', 'group_id' => 4, 'enabled' => true),
        );
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'rules', 'revision' => '7', 'rules' => json_encode($rules)));
        $this->assertSame(200, $status);
        $this->assertSame('PUT /rules', $this->calls[0][1]);
        $this->assertSame(array('revision' => 7, 'rules' => $rules), $this->calls[0][2]);
    }

    public function testBadRulesAreRefusedBeforeRackflowIsCalled(): void
    {
        foreach (array('not json', '{"a":1}', '[1,2]', '"x"') as $text) {
            list($status) = $this->handle(array('serviceid' => '10', 'op' => 'rules', 'revision' => '7', 'rules' => $text));
            $this->assertSame(400, $status, $text);
        }
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'rules', 'revision' => '7', 'rules' => array('x')));
        $this->assertSame(400, $status);
        $this->assertSame(array(), $this->calls);
    }

    public function testPresetUsesThePresetRoute(): void
    {
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'preset', 'revision' => '7', 'preset' => 'web'));
        $this->assertSame(200, $status);
        $this->assertSame('POST /presets/web', $this->calls[0][1]);
        list($status) = $this->handle(array('serviceid' => '10', 'op' => 'preset', 'revision' => '7', 'preset' => '../x'));
        $this->assertSame(400, $status);
        $this->assertCount(1, $this->calls);
    }

    public function testRevisionAndOpAreRequired(): void
    {
        foreach (array(
            array('serviceid' => '10', 'op' => 'enable'),
            array('serviceid' => '10', 'op' => 'enable', 'revision' => '0'),
            array('serviceid' => '10', 'op' => 'enable', 'revision' => '-1'),
            array('serviceid' => '10', 'op' => 'delete', 'revision' => '7'),
            array('op' => 'enable', 'revision' => '7'),
        ) as $post) {
            list($status) = $this->handle($post);
            $this->assertSame(400, $status);
        }
        $this->assertSame(array(), $this->calls);
    }

    public function testRackflowErrorsKeepTheirCodeAndCurrentRevision(): void
    {
        $deps = $this->deps(array('call' => function () {
            return array('success' => false, 'error' => 'The firewall was changed.', 'code' => 'firewall_changed', 'field' => 'revision', 'current_revision' => 9);
        }));
        list($status, $payload) = $this->handle($this->enable(), false, 5, 'POST', true, $deps);
        $this->assertSame(409, $status);
        $this->assertSame(
            array('ok' => false, 'error' => 'The firewall was changed.', 'code' => 'firewall_changed', 'field' => 'revision', 'current_revision' => 9),
            $payload
        );

        $deps = $this->deps(array('call' => function () {
            return array('success' => false, 'error' => 'Ports are not valid.', 'code' => 'invalid_value', 'field' => 'rules.0.ports');
        }));
        list($status, $payload) = $this->handle($this->enable(), false, 5, 'POST', true, $deps);
        $this->assertSame(400, $status);
        $this->assertSame('rules.0.ports', $payload['field']);
    }

    public function testEndpointFileUsesTheHandlerWithTheStoredLink(): void
    {
        $path = __DIR__ . '/../modules/servers/rackflow/firewall_action.php';
        $this->assertFileExists($path);
        $source = file_get_contents($path);
        foreach (array('rackflow_firewallHandle', 'rackflow_isAjaxRequest', "'link' => 'rackflow_getRackflowServiceId'", "'call' => 'rackflow_firewallRequest'", '$_SESSION[\'uid\']', '$_SESSION[\'adminid\']') as $needle) {
            $this->assertStringContainsString($needle, $source);
        }
        $this->assertStringNotContainsString("\$_POST['rackflow", $source);
    }

    public function testActionEndpointUrl(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);
        $this->assertSame(
            '/billing/modules/servers/rackflow/firewall_action.php',
            rackflow_firewallActionEndpointUrl('https://example.com/billing/')
        );
    }

    public function testRequestRefusesBadInputWithoutCallingRackflow(): void
    {
        $this->assertFalse(rackflow_firewallRequest(array(), 0, 'GET')['success']);
        $this->assertFalse(rackflow_firewallRequest(array(), 5, 'DELETE')['success']);
    }

    // Client area

    private function statusPayload(array $extra = array()): array
    {
        return $extra + array('firewall_available' => true, 'client_permissions' => array('vm.firewall' => true));
    }

    private function fetcher(array $view, &$count)
    {
        return function (array $params, $rackflowId) use ($view, &$count) {
            $count++;
            return array('success' => true, 'data' => $view);
        };
    }

    public function testTheFirewallIsFetchedOnlyWhenStatusSaysItIsAvailable(): void
    {
        $params = array('serviceid' => 10);
        $count = 0;
        $fetch = $this->fetcher(array('available' => true), $count);
        foreach (array(
            $this->statusPayload(array('firewall_available' => false)),
            array('client_permissions' => array()),
            null,
        ) as $status) {
            $vars = rackflow_firewallClientAreaVars($params, 700, $status, '', $fetch);
            $this->assertFalse($vars['rackflow_firewall_panel']);
            $this->assertSame('', $vars['rackflow_firewall_json']);
        }
        $vars = rackflow_firewallClientAreaVars($params, 0, $this->statusPayload(), '', $fetch);
        $this->assertFalse($vars['rackflow_firewall_panel']);
        $this->assertSame(0, $count);

        $vars = rackflow_firewallClientAreaVars($params, 700, $this->statusPayload(), '', $fetch);
        $this->assertSame(1, $count);
        $this->assertTrue($vars['rackflow_firewall_panel']);
    }

    public function testViewVarsCarryTheRulesRevisionAndSafeJson(): void
    {
        $count = 0;
        $view = array(
            'available' => true,
            'revision' => 4,
            'client_enabled' => false,
            'needs_interruption' => true,
            'rules' => array(array('id' => 1, 'kind' => 'rule', 'comment' => '</script><b>"x"</b>')),
            'groups' => array(array('id' => 3, 'name' => 'Web')),
            'available_groups' => array(array('id' => 4, 'name' => 'Mail')),
        );
        $vars = rackflow_firewallClientAreaVars(array('serviceid' => 10), 700, $this->statusPayload(), '', $this->fetcher($view, $count));
        $this->assertTrue($vars['rackflow_firewall_can_edit']);
        $this->assertSame('/modules/servers/rackflow/firewall_action.php', str_replace(rackflow_whmcsUrlBasePath() ?: '', '', $vars['rackflow_firewall_action_url']));
        $json = $vars['rackflow_firewall_json'];
        $this->assertStringNotContainsString('<', $json);
        $this->assertStringNotContainsString('"x"', $json);
        $data = json_decode($json, true);
        $this->assertSame(4, $data['revision']);
        $this->assertTrue($data['needs_interruption']);
        $this->assertSame('</script><b>"x"</b>', $data['rules'][0]['comment']);
        $this->assertSame(array(3, 4), array_column($data['groups'], 'id'));
        $this->assertSame(array(), $vars['rackflow_firewall']['baseline']['rules']);
        $this->assertTrue($vars['rackflow_firewall']['baseline']['client_may_disable']);
    }

    public function testWritesAreHiddenWhenThePermissionIsOff(): void
    {
        $count = 0;
        $status = $this->statusPayload(array('client_permissions' => array('vm.firewall' => false)));
        $vars = rackflow_firewallClientAreaVars(array('serviceid' => 10), 700, $status, '', $this->fetcher(array('available' => true), $count));
        $this->assertTrue($vars['rackflow_firewall_panel']);
        $this->assertFalse($vars['rackflow_firewall_can_edit']);
    }

    public function testAnUnavailableViewHidesTheSectionAndAFailedFetchShowsAnError(): void
    {
        $count = 0;
        $vars = rackflow_firewallClientAreaVars(array('serviceid' => 10), 700, $this->statusPayload(), '', $this->fetcher(array('available' => false), $count));
        $this->assertFalse($vars['rackflow_firewall_panel']);

        $failing = function () {
            return array('success' => false, 'error' => 'Could not load.');
        };
        $vars = rackflow_firewallClientAreaVars(array('serviceid' => 10), 700, $this->statusPayload(), '', $failing);
        $this->assertTrue($vars['rackflow_firewall_panel']);
        $this->assertSame('Could not load.', $vars['rackflow_firewall_error']);
    }

    public function testClientAreaOnlyReadsTheFirewallThroughTheGate(): void
    {
        $source = file_get_contents(__DIR__ . '/../modules/servers/rackflow/rackflow.php');
        $this->assertSame(2, preg_match_all('/rackflow_firewallClientAreaVars\(/', $source));
        $this->assertStringContainsString("empty(\$statusData['firewall_available'])", $source);
    }

    // Template and admin tab

    private function firewallTemplate(): string
    {
        $tpl = file_get_contents(__DIR__ . '/../modules/servers/rackflow/clientarea.tpl');
        $start = strpos($tpl, '{if $rackflow_firewall_panel}');
        $end = strpos($tpl, '{if $rackflow_plugin_panels_html}');
        $this->assertNotFalse($start);
        $this->assertGreaterThan($start, $end);
        return substr($tpl, $start, $end - $start);
    }

    public function testEveryValueInTheFirewallTemplateIsEscaped(): void
    {
        $section = $this->firewallTemplate();
        $this->assertGreaterThan(10, preg_match_all('/\{\$[^}]+\}/', $section, $matches));
        foreach ($matches[0] as $output) {
            $this->assertStringContainsString('|escape', $output, $output);
        }
        $this->assertStringNotContainsString('|nofilter', $section);
        $this->assertStringContainsString('smtp_block_help', $section);
        $this->assertStringContainsString('Provider baseline (read only)', $section);
    }

    public function testFirewallTemplateAndScriptTextHasNoEmDash(): void
    {
        $tpl = file_get_contents(__DIR__ . '/../modules/servers/rackflow/clientarea.tpl');
        $start = strpos($tpl, 'rackflow-firewall');
        $block = $this->firewallTemplate() . substr($tpl, strpos($tpl, '{if $rackflow_firewall_panel && !$rackflow_firewall_error}'), 9000);
        $this->assertNotFalse($start);
        $this->assertStringNotContainsString("\u{2014}", $block);
        $this->assertStringNotContainsString('innerHTML', $block);
    }

    public function testAdminTabShowsTheStateAndALinkEscaped(): void
    {
        $html = rackflow_renderAdminFirewall(
            array('serverid' => 0),
            700,
            array('available' => true, 'client_enabled' => true, 'state' => 'applied<b>', 'rules' => array(array(), array()))
        );
        $this->assertStringContainsString('On (applied&lt;b&gt;), 2 client rules', $html);
        $this->assertStringNotContainsString('<b>', $html);
        $this->assertSame('https://rf.example/admin/vm-firewall', rackflow_adminFirewallPageUrl('https://rf.example/'));
        $this->assertSame('', rackflow_adminFirewallPageUrl(''));
        $this->assertStringContainsString('could not be read', rackflow_renderAdminFirewall(array('serverid' => 0), 700, array()));
    }
}
