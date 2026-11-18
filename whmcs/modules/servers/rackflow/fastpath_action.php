<?php
/**
 * Client/admin AJAX endpoint for the proxy fast path of a proxy service.
 *
 * Usage (POST): /modules/servers/rackflow/fastpath_action.php
 *   serviceid, op=on|off
 *
 * Turns the client's private access network to the proxy runners on or off in
 * the locations of the service's proxy IPs.
 *
 * Returns JSON: {"ok":true,"message":"..."} or {"ok":false,"error":"..."}.
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

function rackflow_fastpath_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// Session auth plus the AJAX header check, as in proxy_action.php.
if (!rackflow_isAjaxRequest()) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$op = isset($_POST['op']) ? strtolower(trim((string)$_POST['op'])) : '';
if ($serviceId <= 0 || ($op !== 'on' && $op !== 'off')) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$serviceType = isset($product->configoption1) ? strtolower(trim((string)$product->configoption1)) : '';
if ($serviceType !== 'http_proxy') {
    rackflow_fastpath_action_json(array('ok' => false, 'error' => 'Not a proxy service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
    'configoption1' => $serviceType,
);

$result = rackflow_proxyFastPathToggle($params, $op === 'on');
if ($result === 'success') {
    rackflow_fastpath_action_json(array(
        'ok' => true,
        'message' => $op === 'on' ? 'Private proxy access turned on.' : 'Private proxy access turned off.',
    ));
}

rackflow_fastpath_action_json(array('ok' => false, 'error' => (string)$result), 400);
