<?php
/**
 * Client/admin AJAX endpoint for the VM firewall section (docs/vm-firewall.md section 10.5).
 *
 * Usage (POST): /modules/servers/rackflow/firewall_action.php
 *   serviceid  WHMCS service the page belongs to (the only service id read from the request)
 *   op         enable | disable | policy | rules | preset
 *   revision   the firewall revision the page was built from
 *   enable:    confirm_interruption (1 once the client accepted the short network blip)
 *   policy:    policy_in and/or policy_out (ACCEPT, DROP or REJECT)
 *   rules:     rules (JSON list; replaces the whole client rule list)
 *   preset:    preset (key)
 *
 * Returns JSON: {"ok":true,"message":"...","revision":N} or
 *   {"ok":false,"error":"...","code":"...","field":...,"current_revision":N|null}.
 * The RackFlow service id is read from the link stored for the WHMCS service, never from the request.
 */

@ini_set('display_errors', '0');
while (ob_get_level() > 0) {
    ob_end_clean();
}
ob_start();

require_once __DIR__ . '/../../../init.php';
require_once __DIR__ . '/rackflow.php';

header('Content-Type: application/json; charset=utf-8');
header('X-Robots-Tag: noindex, nofollow');
header('Cache-Control: no-store, no-cache, must-revalidate');

function rackflow_firewall_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_firewall_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

// Session auth only; CSRF defence is the AJAX header check (see rackflow_isAjaxRequest()),
// the same as plugin_action.php.
list($status, $payload) = rackflow_firewallHandle(
    isset($_SERVER['REQUEST_METHOD']) ? (string)$_SERVER['REQUEST_METHOD'] : '',
    rackflow_isAjaxRequest(),
    $_POST,
    !empty($_SESSION['adminid']),
    !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0,
    array(
        'hosting' => function ($id) {
            return \Illuminate\Database\Capsule\Manager::table('tblhosting')->where('id', (int)$id)->first();
        },
        'product' => function ($id) {
            return \Illuminate\Database\Capsule\Manager::table('tblproducts')->where('id', (int)$id)->first();
        },
        'link' => 'rackflow_getRackflowServiceId',
        'call' => 'rackflow_firewallRequest',
    )
);
rackflow_firewall_action_json($payload, $status);
