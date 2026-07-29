<?php
/**
 * Admin/client AJAX endpoint for VM reinstall.
 *
 * Usage (POST): /modules/servers/rackflow/reinstall_action.php
 *   serviceid, rf_vm_template_id?, rf_ssh_public_keys?
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

function rackflow_reinstall_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// CSRF defense: see rackflow_isAjaxRequest() in rackflow.php.
if (!rackflow_isAjaxRequest()) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
if ($serviceId <= 0) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_reinstall_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
    'configoption1' => isset($product->configoption1) ? (string)$product->configoption1 : '',
    'configoption2' => isset($product->configoption2) ? (string)$product->configoption2 : '',
);
if (!$isAdmin || $clientId > 0) {
    $params['clientsdetails'] = array('userid' => $clientId > 0 ? $clientId : (int)$hosting->userid);
}

if (isset($_POST['rf_vm_template_id'])) {
    $_REQUEST['rf_vm_template_id'] = (string)$_POST['rf_vm_template_id'];
}
if (isset($_POST['rf_ssh_public_keys'])) {
    $_REQUEST['rf_ssh_public_keys'] = (string)$_POST['rf_ssh_public_keys'];
}

$result = rackflow_Reinstall($params);
if ($result === 'success') {
    rackflow_reinstall_action_json(array(
        'ok' => true,
        'message' => 'Reinstall queued. Guest will reprovision at the same VMID.',
    ));
}

rackflow_reinstall_action_json(array('ok' => false, 'error' => (string)$result), 400);
