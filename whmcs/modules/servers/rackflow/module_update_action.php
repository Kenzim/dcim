<?php
/**
 * Admin JSON API for RackFlow git module updates.
 *
 * POST JSON: action = status | save | check | update
 */

@ini_set('display_errors', '0');
while (ob_get_level() > 0) {
    ob_end_clean();
}
ob_start();

require_once __DIR__ . '/../../../init.php';
require_once __DIR__ . '/rackflow.php';
require_once __DIR__ . '/git_update.php';

header('Content-Type: application/json; charset=utf-8');
header('X-Robots-Tag: noindex, nofollow');
header('Cache-Control: no-store, no-cache, must-revalidate');

function rackflow_gitupdate_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

if (empty($_SESSION['adminid'])) {
    rackflow_gitupdate_json(array('ok' => false, 'error' => 'Admin login required.'), 403);
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_gitupdate_json(array('ok' => false, 'error' => 'POST required.'), 405);
}

if (!rackflow_isAjaxRequest()) {
    rackflow_gitupdate_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
}

$raw = file_get_contents('php://input');
$body = array();
if (is_string($raw) && $raw !== '') {
    $decoded = json_decode($raw, true);
    if (is_array($decoded)) {
        $body = $decoded;
    }
}
if (empty($body) && !empty($_POST) && is_array($_POST)) {
    $body = $_POST;
}

$action = isset($body['action']) ? trim((string)$body['action']) : '';
if ($action === '') {
    rackflow_gitupdate_json(array('ok' => false, 'error' => 'Missing action.'), 400);
}

$settingsFromBody = rackflow_gitupdate_loadSettings();
if (isset($body['repo_url'])) {
    $settingsFromBody['repo_url'] = trim((string)$body['repo_url']);
}
if (isset($body['branch'])) {
    $settingsFromBody['branch'] = rackflow_gitupdate_normalizeBranch($body['branch']);
}
if (isset($body['insecure_tls'])) {
    $settingsFromBody['insecure_tls'] = !empty($body['insecure_tls']);
}
if (isset($body['update_reseller'])) {
    $settingsFromBody['update_reseller'] = !empty($body['update_reseller']);
}
if (isset($body['token']) && trim((string)$body['token']) !== '') {
    $settingsFromBody['token'] = trim((string)$body['token']);
}
if (!empty($body['clear_token'])) {
    $settingsFromBody['token'] = '';
}

if ($action === 'status') {
    rackflow_gitupdate_json(array('ok' => true, 'status' => rackflow_gitupdate_statusPayload()));
}

if ($action === 'save') {
    $saved = rackflow_gitupdate_saveSettings($body);
    if (!$saved['ok']) {
        rackflow_gitupdate_json(array('ok' => false, 'error' => $saved['error']), 400);
    }
    rackflow_gitupdate_json(array('ok' => true, 'status' => rackflow_gitupdate_statusPayload()));
}

if ($action === 'check') {
    $remote = rackflow_gitupdate_checkRemote($settingsFromBody);
    $remote['status'] = rackflow_gitupdate_statusPayload();
    if (!$remote['ok']) {
        rackflow_gitupdate_json($remote, 400);
    }
    rackflow_gitupdate_json($remote);
}

if ($action === 'update') {
    $saved = rackflow_gitupdate_saveSettings($body);
    if (!$saved['ok']) {
        rackflow_gitupdate_json(array('ok' => false, 'error' => $saved['error']), 400);
    }
    $settingsFromBody = rackflow_gitupdate_loadSettings();
    $result = rackflow_gitupdate_apply($settingsFromBody);
    $result['status'] = rackflow_gitupdate_statusPayload();
    if (!$result['ok']) {
        rackflow_gitupdate_json($result, 400);
    }
    rackflow_gitupdate_json($result);
}

rackflow_gitupdate_json(array('ok' => false, 'error' => 'Unknown action.'), 400);
