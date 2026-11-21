<?php
/**
 * Client/admin AJAX endpoint for plugin actions (docs/plugin-system.md section 14.2).
 *
 * Usage (POST): /modules/servers/rackflow/plugin_action.php
 *   serviceid  WHMCS service the page belongs to
 *   audience   client | admin (admin needs an admin session)
 *   plugin_id  plugin id from the render document
 *   action_id  action id from the render document
 *   confirm    1 when the user accepted the action's confirmation text
 *   p_<key>    action params
 *
 * Returns JSON: {"ok":true,"message":"...","panels":[...]} or {"ok":false,"error":"..."}.
 * RackFlow checks the action's audience, its conditions and its params.
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

function rackflow_plugin_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// Session auth only; CSRF defence is the AJAX header check (see rackflow_isAjaxRequest()).
if (!rackflow_isAjaxRequest()) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$pluginId = isset($_POST['plugin_id']) ? (string)$_POST['plugin_id'] : '';
$actionId = isset($_POST['action_id']) ? (string)$_POST['action_id'] : '';
if ($serviceId <= 0 || !rackflow_pluginIdsValid($pluginId, $actionId)) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}
$audience = isset($_POST['audience']) && $_POST['audience'] === 'admin' ? 'admin' : 'client';
if ($audience === 'admin' && !$isAdmin) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')->where('id', $serviceId)->first();
if (!$hosting) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}
if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}
$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')->where('id', (int)$hosting->packageid)->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);
$rackflowServiceId = (int)rackflow_getRackflowServiceId($params);
if ($rackflowServiceId <= 0) {
    rackflow_plugin_action_json(array('ok' => false, 'error' => 'This service is not linked to RackFlow.'), 400);
}

$confirm = !empty($_POST['confirm']) && (string)$_POST['confirm'] !== '0';
$result = rackflow_runPluginAction(
    $params,
    $rackflowServiceId,
    $pluginId,
    $actionId,
    rackflow_pluginParamValues($_POST),
    $confirm,
    $audience
);

if (!empty($result['success'])) {
    $data = $result['data'];
    rackflow_plugin_action_json(array(
        'ok' => true,
        'message' => isset($data['message']) && is_string($data['message']) ? $data['message'] : 'Done.',
        'panels' => isset($data['panels']) && is_array($data['panels']) ? $data['panels'] : array(),
    ));
}
rackflow_plugin_action_json(array('ok' => false, 'error' => (string)$result['error']), 400);
