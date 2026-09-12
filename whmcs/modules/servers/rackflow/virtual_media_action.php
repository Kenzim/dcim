<?php
/**
 * Admin/client AJAX endpoint for BMC virtual CD mount/eject.
 *
 * Usage (POST): /modules/servers/rackflow/virtual_media_action.php
 *   serviceid, op=insert|eject, filename?, boot_once=0|1
 *
 * Returns JSON: {"ok":true,"message":"...","data":{...}} or {"ok":false,"error":"..."}.
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

function rackflow_virtual_media_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

if (!rackflow_isAjaxRequest()) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$op = isset($_POST['op']) ? strtolower(trim((string)$_POST['op'])) : '';
if ($serviceId <= 0 || !in_array($op, array('insert', 'eject'), true)) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);
$rackflowSvcId = rackflow_getRackflowServiceId($params);
if (empty($rackflowSvcId)) {
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Service is not linked to RackFlow.'), 409);
}

$apiConfig = rackflow_getApiConfig($params);
$endpoint = '/api/billing/services/' . (int)$rackflowSvcId . '/virtual-media/' . $op;
$body = null;
if ($op === 'insert') {
    $filename = isset($_POST['filename']) ? trim((string)$_POST['filename']) : '';
    if ($filename === '') {
        rackflow_virtual_media_action_json(array('ok' => false, 'error' => 'Select an ISO.'), 400);
    }
    $body = array(
        'filename' => $filename,
        'boot_once' => !empty($_POST['boot_once']) && (string)$_POST['boot_once'] !== '0',
    );
}

$result = rackflow_apiCall($apiConfig['url'], $apiConfig['key'], 'POST', $endpoint, $body);
if (empty($result['success'])) {
    $err = isset($result['error']) ? $result['error'] : 'Virtual media request failed';
    if (is_array($result['data']) && isset($result['data']['detail'])) {
        $err = $result['data']['detail'];
    }
    if (is_array($err)) {
        $err = json_encode($err);
    }
    rackflow_virtual_media_action_json(array('ok' => false, 'error' => (string)$err), 400);
}

$data = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
$message = $op === 'insert' ? 'ISO mounted as virtual CD.' : 'Virtual CD ejected.';
rackflow_virtual_media_action_json(array('ok' => true, 'message' => $message, 'data' => $data));
