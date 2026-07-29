<?php
/**
 * Admin AJAX endpoint for browsing / reassigning VM pool IPs.
 *
 * Usage (POST): /modules/servers/rackflow/ip_action.php
 *   serviceid, op=list|reassign
 *   allocation_id? (reassign), reset_network? (reassign, default 1)
 *
 * Returns JSON: {"ok":true,...} or {"ok":false,"error":"..."}.
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

function rackflow_ip_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// CSRF defense: see rackflow_isAjaxRequest() in rackflow.php.
if (!rackflow_isAjaxRequest()) {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$op = isset($_POST['op']) ? strtolower(trim((string)$_POST['op'])) : '';
if ($serviceId <= 0 || !in_array($op, array('list', 'reassign'), true)) {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

if (empty($_SESSION['adminid'])) {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Admin login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_ip_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
    'configoption1' => isset($product->configoption1) ? (string)$product->configoption1 : '',
);

if ($op === 'list') {
    $fetch = rackflow_fetchAvailableIps($params);
    if (empty($fetch['success'])) {
        rackflow_ip_action_json(array(
            'ok' => false,
            'error' => isset($fetch['error']) ? (string)$fetch['error'] : 'Unable to list IPs',
        ), 400);
    }
    $data = isset($fetch['data']) && is_array($fetch['data']) ? $fetch['data'] : array();
    rackflow_ip_action_json(array(
        'ok' => true,
        'current' => isset($data['current']) ? $data['current'] : null,
        'available' => isset($data['available']) && is_array($data['available']) ? $data['available'] : array(),
        'proxmox_cluster_id' => isset($data['proxmox_cluster_id']) ? $data['proxmox_cluster_id'] : null,
    ));
}

$allocationId = isset($_POST['allocation_id']) ? (int)$_POST['allocation_id'] : 0;
$resetNetwork = true;
if (isset($_POST['reset_network'])) {
    $raw = strtolower(trim((string)$_POST['reset_network']));
    $resetNetwork = !in_array($raw, array('0', 'false', 'no', 'off'), true);
}

$reassign = rackflow_reassignVmIp($params, $allocationId, $resetNetwork);
if (empty($reassign['success'])) {
    rackflow_ip_action_json(array(
        'ok' => false,
        'error' => isset($reassign['error']) ? (string)$reassign['error'] : 'IP reassignment failed',
    ), 400);
}

rackflow_ip_action_json(array(
    'ok' => true,
    'message' => isset($reassign['message']) ? (string)$reassign['message'] : 'IP reassigned',
    'data' => isset($reassign['data']) ? $reassign['data'] : null,
));
