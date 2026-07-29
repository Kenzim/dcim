<?php
/**
 * Client/admin AJAX endpoint for VM backup create/delete/restore.
 *
 * Avoids clientarea.php?modop=custom success redirects, which drop WHMCS
 * "Login as Owner" (and sometimes normal) client sessions after CreateBackup.
 *
 * Usage (POST): /modules/servers/rackflow/backup_action.php
 *   serviceid, op=create|delete|restore, token, rf_notes?, volid?, storage?
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

function rackflow_backup_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// Session auth only (same pattern as ipmi_open.php / vnc_open.php). Avoid
// check_token() here — on failure WHMCS tears down the session (looks like logout).
// CSRF defense: see rackflow_isAjaxRequest() in rackflow.php.
if (!rackflow_isAjaxRequest()) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$op = isset($_POST['op']) ? strtolower(trim((string)$_POST['op'])) : '';
if ($serviceId <= 0 || !in_array($op, array('create', 'delete', 'restore'), true)) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_backup_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);
// Audience: real client session (including admin "Login as Owner") uses client.
if (!$isAdmin || $clientId > 0) {
    $params['clientsdetails'] = array('userid' => $clientId > 0 ? $clientId : (int)$hosting->userid);
}

// Stash request fields for rackflow_*Backup helpers that read $_REQUEST.
if (isset($_POST['rf_notes'])) {
    $_REQUEST['rf_notes'] = (string)$_POST['rf_notes'];
}
if (isset($_POST['volid'])) {
    $_REQUEST['volid'] = (string)$_POST['volid'];
}
if (isset($_POST['storage'])) {
    $_REQUEST['storage'] = (string)$_POST['storage'];
}

$result = 'Unknown backup action';
if ($op === 'create') {
    $result = rackflow_CreateBackup($params);
} elseif ($op === 'delete') {
    $result = rackflow_DeleteBackup($params);
} else {
    $result = rackflow_RestoreBackup($params);
}

if ($result === 'success') {
    $messages = array(
        'create' => 'Backup started.',
        'delete' => 'Backup deleted.',
        'restore' => 'Restore started.',
    );
    rackflow_backup_action_json(array(
        'ok' => true,
        'message' => isset($messages[$op]) ? $messages[$op] : 'Done.',
    ));
}

rackflow_backup_action_json(array('ok' => false, 'error' => (string)$result), 400);
