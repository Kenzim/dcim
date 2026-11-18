<?php
/**
 * Client/admin AJAX endpoint for backup slot actions.
 *
 * Usage (POST): /modules/servers/rackflow/slot_action.php
 *   serviceid  WHMCS service the page belongs to (a VM for retain, a backup slot for release/restore)
 *   op         retain | release | restore
 *   retain:    slot_id (RackFlow slot service id), volid, name?, replace?
 *   release:   (nothing more)
 *   restore:   target_id (RackFlow VM service id), confirm_data_loss
 *
 * Returns JSON: {"ok":true,"message":"..."} or {"ok":false,"error":"..."}.
 * RackFlow checks that the slot, the VM and the backup belong to the same client.
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

function rackflow_slot_action_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'POST required'), 405);
}

// Session auth only; CSRF defence is the AJAX header check (see rackflow_isAjaxRequest()).
if (!rackflow_isAjaxRequest()) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$serviceId = isset($_POST['serviceid']) ? (int)$_POST['serviceid'] : 0;
$op = isset($_POST['op']) ? strtolower(trim((string)$_POST['op'])) : '';
if ($serviceId <= 0 || !in_array($op, array('retain', 'release', 'restore'), true)) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Invalid request.'), 400);
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Login required.'), 403);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')->where('id', $serviceId)->first();
if (!$hosting) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}
if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Access denied.'), 403);
}
$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')->where('id', (int)$hosting->packageid)->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);
$audience = (!$isAdmin || $clientId > 0) ? 'client' : 'admin';
$rackflowServiceId = (int)rackflow_getRackflowServiceId($params);
if ($rackflowServiceId <= 0) {
    rackflow_slot_action_json(array('ok' => false, 'error' => 'This service is not linked to RackFlow.'), 400);
}

$serviceType = rackflow_getConfiguredServiceType($params);
if ($op === 'retain') {
    if ($serviceType !== 'vm') {
        rackflow_slot_action_json(array('ok' => false, 'error' => 'Backups are retained from a VM service.'), 400);
    }
    $slotId = isset($_POST['slot_id']) ? (int)$_POST['slot_id'] : 0;
    $volid = isset($_POST['volid']) ? trim((string)$_POST['volid']) : '';
    if ($slotId <= 0 || $volid === '') {
        rackflow_slot_action_json(array('ok' => false, 'error' => 'Choose a slot and a backup.'), 400);
    }
    $body = array(
        'source_service_id' => $rackflowServiceId,
        'volid' => $volid,
        'replace' => !empty($_POST['replace']) && (string)$_POST['replace'] !== '0',
    );
    if (isset($_POST['name']) && trim((string)$_POST['name']) !== '') {
        $body['name'] = trim((string)$_POST['name']);
    }
    $result = rackflow_slotPost($params, $slotId, 'retain', $body, $audience);
    $message = 'Backup kept in the slot.';
} else {
    if ($serviceType !== 'backup_slot') {
        rackflow_slot_action_json(array('ok' => false, 'error' => 'This action is for a backup slot service.'), 400);
    }
    if ($op === 'release') {
        $result = rackflow_slotPost($params, $rackflowServiceId, 'release', array(), $audience);
        $message = 'Backup released and deleted.';
    } else {
        $targetId = isset($_POST['target_id']) ? (int)$_POST['target_id'] : 0;
        if ($targetId <= 0) {
            rackflow_slot_action_json(array('ok' => false, 'error' => 'Choose the VM to restore onto.'), 400);
        }
        $result = rackflow_slotPost($params, $rackflowServiceId, 'restore', array(
            'target_service_id' => $targetId,
            'confirm_data_loss' => !empty($_POST['confirm_data_loss']) && (string)$_POST['confirm_data_loss'] !== '0',
            'start' => true,
        ), $audience);
        $message = 'Restore started. The VM keeps its address and login details.';
    }
}

if (!empty($result['success'])) {
    rackflow_slot_action_json(array('ok' => true, 'message' => $message));
}
rackflow_slot_action_json(array('ok' => false, 'error' => (string)$result['error']), 400);
