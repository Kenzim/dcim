<?php
/**
 * Admin JSON API for RackFlow service link management (WHMCS ↔ RackFlow).
 *
 * Requires an authenticated WHMCS admin session. Proxies billing API calls
 * so the browser never sees the billing API key.
 *
 * POST JSON body:
 *   action: search | unlink | link | adopt | set_vmid
 *   serviceid: WHMCS tblhosting.id
 *   …action-specific fields
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

function rackflow_admin_link_json($payload, $httpCode = 200)
{
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    http_response_code((int)$httpCode);
    echo json_encode($payload);
    exit;
}

function rackflow_admin_link_api_detail($result)
{
    $err = isset($result['error']) ? $result['error'] : 'Unknown error';
    $detail = is_array($result['data']) && isset($result['data']['detail'])
        ? $result['data']['detail']
        : $err;
    if (is_array($detail)) {
        $detail = json_encode($detail);
    }
    return (string)$detail;
}

if (empty($_SESSION['adminid'])) {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Admin login required.'), 403);
}

if (strtoupper((string)$_SERVER['REQUEST_METHOD']) !== 'POST') {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'POST required.'), 405);
}

// CSRF defense: see rackflow_isAjaxRequest() in rackflow.php. Especially
// important here since a fallback below reads $_POST when the JSON body is
// empty/unparseable, which a plain cross-site <form> submission could reach.
if (!rackflow_isAjaxRequest()) {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Invalid request.'), 403);
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
$serviceId = isset($body['serviceid']) ? (int)$body['serviceid'] : 0;
if ($serviceId <= 0) {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Missing serviceid.'), 400);
}
if ($action === '') {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Missing action.'), 400);
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Database unavailable.'), 500);
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Service not found.'), 404);
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'Not a RackFlow service.'), 400);
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
    'configoption1' => isset($product->configoption1) ? (string)$product->configoption1 : '',
);

$apiConfig = rackflow_getApiConfig($params);
$apiUrl = isset($apiConfig['url']) ? $apiConfig['url'] : '';
$apiKey = isset($apiConfig['key']) ? $apiConfig['key'] : '';
if ($apiUrl === '' || $apiKey === '') {
    rackflow_admin_link_json(array('ok' => false, 'error' => 'RackFlow API is not configured for this product.'), 400);
}

$linkedId = rackflow_getRackflowServiceId($params);
$client = \Illuminate\Database\Capsule\Manager::table('tblclients')
    ->where('id', (int)$hosting->userid)
    ->first();
$externalUsername = '';
if ($client) {
    $externalUsername = trim(
        (isset($client->firstname) ? $client->firstname : '')
        . ' '
        . (isset($client->lastname) ? $client->lastname : '')
    );
}
$externalEmail = ($client && !empty($client->email)) ? (string)$client->email : '';

if ($action === 'search') {
    $qType = isset($body['q_type']) ? strtolower(trim((string)$body['q_type'])) : 'any';
    $q = isset($body['q']) ? trim((string)$body['q']) : '';
    if ($q === '') {
        rackflow_admin_link_json(array('ok' => false, 'error' => 'Enter a search value.'), 400);
    }
    $qs = array();
    if ($qType === 'vmid') {
        if (!ctype_digit($q)) {
            rackflow_admin_link_json(array('ok' => false, 'error' => 'Enter a numeric VMID.'), 400);
        }
        // Free-text q also searches Proxmox guests + partial VMID / name matches.
        $qs[] = 'q=' . rawurlencode($q);
        $qs[] = 'proxmox_vmid=' . rawurlencode($q);
    } elseif ($qType === 'server_ip') {
        $qs[] = 'q=' . rawurlencode($q);
        $qs[] = 'server_ip=' . rawurlencode($q);
    } elseif ($qType === 'service_id') {
        if (!ctype_digit($q)) {
            rackflow_admin_link_json(array('ok' => false, 'error' => 'Enter a numeric RackFlow service ID.'), 400);
        }
        $qs[] = 'q=' . rawurlencode($q);
        $qs[] = 'service_id=' . rawurlencode($q);
    } else {
        // Optimistic: id / VMID / name / IP substring matches + Proxmox guests.
        $qs[] = 'q=' . rawurlencode($q);
    }
    $result = rackflow_apiCall(
        $apiUrl,
        $apiKey,
        'GET',
        '/api/billing/services/lookup?' . implode('&', $qs),
        null
    );
    if (!$result['success']) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => rackflow_admin_link_api_detail($result),
        ), !empty($result['http_code']) ? (int)$result['http_code'] : 502);
    }
    $rows = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
    rackflow_admin_link_json(array('ok' => true, 'results' => $rows));
}

if ($action === 'unlink') {
    $rfId = !empty($body['rackflow_service_id'])
        ? (int)$body['rackflow_service_id']
        : (int)$linkedId;
    if ($rfId > 0) {
        $result = rackflow_apiCall(
            $apiUrl,
            $apiKey,
            'POST',
            '/api/billing/services/' . $rfId . '/unlink',
            array()
        );
        if (!$result['success'] && !(isset($result['http_code']) && (int)$result['http_code'] === 404)) {
            rackflow_admin_link_json(array(
                'ok' => false,
                'error' => 'Unlink on RackFlow failed: ' . rackflow_admin_link_api_detail($result),
            ), 502);
        }
    }
    if (!rackflow_saveServiceIdCustomField($serviceId, (int)$hosting->packageid, '')) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => 'Cleared RackFlow link remotely, but failed to clear the WHMCS custom field.',
        ), 500);
    }
    rackflow_admin_link_json(array('ok' => true, 'rackflow_service_id' => null));
}

if ($action === 'link' || $action === 'adopt') {
    $payload = array(
        'external_service_id' => (string)$serviceId,
        'external_user_id' => (string)((int)$hosting->userid),
        'external_username' => $externalUsername !== '' ? $externalUsername : null,
        'external_email' => $externalEmail !== '' ? $externalEmail : null,
    );

    if ($action === 'adopt') {
        // Free the current WHMCS↔RF mapping so adopt can bind this external id.
        if (!empty($linkedId)) {
            rackflow_apiCall(
                $apiUrl,
                $apiKey,
                'POST',
                '/api/billing/services/' . (int)$linkedId . '/unlink',
                array()
            );
        }
        $clusterId = isset($body['proxmox_cluster_id']) ? (int)$body['proxmox_cluster_id'] : 0;
        $nodeName = isset($body['proxmox_node_name']) ? trim((string)$body['proxmox_node_name']) : '';
        $vmid = isset($body['proxmox_vmid']) ? (int)$body['proxmox_vmid'] : 0;
        $guestName = isset($body['name']) ? trim((string)$body['name']) : '';
        if ($clusterId <= 0 || $nodeName === '' || $vmid <= 0) {
            rackflow_admin_link_json(array(
                'ok' => false,
                'error' => 'proxmox_cluster_id, proxmox_node_name, and proxmox_vmid are required to adopt a VM.',
            ), 400);
        }
        $payload['proxmox_cluster_id'] = $clusterId;
        $payload['proxmox_node_name'] = $nodeName;
        $payload['proxmox_vmid'] = $vmid;
        if ($guestName !== '') {
            $payload['name'] = $guestName;
        }
        $productCode = isset($product->configoption2) ? trim((string)$product->configoption2) : '';
        if ($productCode !== '') {
            $payload['product_code'] = $productCode;
        }
        $result = rackflow_apiCall(
            $apiUrl,
            $apiKey,
            'POST',
            '/api/billing/services/adopt-vm',
            $payload
        );
        if (!$result['success']) {
            rackflow_admin_link_json(array(
                'ok' => false,
                'error' => rackflow_admin_link_api_detail($result),
            ), !empty($result['http_code']) ? (int)$result['http_code'] : 502);
        }
        $svc = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
        $rfId = isset($svc['id']) ? (int)$svc['id'] : 0;
        if ($rfId <= 0) {
            rackflow_admin_link_json(array('ok' => false, 'error' => 'Adopt succeeded but no service id returned.'), 502);
        }
        if (!rackflow_saveServiceIdCustomField($serviceId, (int)$hosting->packageid, $rfId)) {
            rackflow_admin_link_json(array(
                'ok' => false,
                'error' => 'Adopted on RackFlow, but failed to save the WHMCS RackFlow Service ID field.',
            ), 500);
        }
        $openUrl = rackflow_adminServicePageUrl($apiUrl, $rfId);
        rackflow_admin_link_json(array(
            'ok' => true,
            'rackflow_service_id' => $rfId,
            'service' => $svc,
            'open_url' => $openUrl,
        ));
    }

    $rfId = isset($body['rackflow_service_id']) ? (int)$body['rackflow_service_id'] : 0;
    if ($rfId <= 0) {
        rackflow_admin_link_json(array('ok' => false, 'error' => 'Missing rackflow_service_id.'), 400);
    }

    // If WHMCS already points at a different RF service, unlink that mapping first.
    if (!empty($linkedId) && (int)$linkedId !== $rfId) {
        rackflow_apiCall(
            $apiUrl,
            $apiKey,
            'POST',
            '/api/billing/services/' . (int)$linkedId . '/unlink',
            array()
        );
    }

    $result = rackflow_apiCall(
        $apiUrl,
        $apiKey,
        'POST',
        '/api/billing/services/' . $rfId . '/link',
        $payload
    );
    if (!$result['success']) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => rackflow_admin_link_api_detail($result),
        ), !empty($result['http_code']) ? (int)$result['http_code'] : 502);
    }
    if (!rackflow_saveServiceIdCustomField($serviceId, (int)$hosting->packageid, $rfId)) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => 'Linked on RackFlow, but failed to save the WHMCS RackFlow Service ID field.',
        ), 500);
    }
    $svc = isset($result['data']) && is_array($result['data']) ? $result['data'] : array('id' => $rfId);
    $openUrl = rackflow_adminServicePageUrl($apiUrl, $rfId);
    rackflow_admin_link_json(array(
        'ok' => true,
        'rackflow_service_id' => $rfId,
        'service' => $svc,
        'open_url' => $openUrl,
    ));
}

if ($action === 'set_vmid') {
    $serviceType = rackflow_getConfiguredServiceType($params);
    if ($serviceType !== 'vm') {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => 'VMID editing is only available for virtual machine products.',
        ), 400);
    }
    $rfId = !empty($body['rackflow_service_id'])
        ? (int)$body['rackflow_service_id']
        : (int)$linkedId;
    if ($rfId <= 0) {
        rackflow_admin_link_json(array('ok' => false, 'error' => 'Service is not linked to RackFlow.'), 400);
    }
    $clusterId = isset($body['proxmox_cluster_id']) ? (int)$body['proxmox_cluster_id'] : 0;
    $nodeName = isset($body['proxmox_node_name']) ? trim((string)$body['proxmox_node_name']) : '';
    $vmid = isset($body['proxmox_vmid']) ? (int)$body['proxmox_vmid'] : 0;
    if ($clusterId <= 0 || $nodeName === '' || $vmid <= 0) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => 'proxmox_cluster_id, proxmox_node_name, and proxmox_vmid are required.',
        ), 400);
    }
    $result = rackflow_apiCall(
        $apiUrl,
        $apiKey,
        'PUT',
        '/api/billing/services/' . $rfId . '/vm/placement',
        array(
            'proxmox_cluster_id' => $clusterId,
            'proxmox_node_name' => $nodeName,
            'proxmox_vmid' => $vmid,
            // Allow binding to an already-existing guest (e.g. legacy VMIDs).
            'adopt_existing' => true,
        )
    );
    if (!$result['success']) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => rackflow_admin_link_api_detail($result),
        ), !empty($result['http_code']) ? (int)$result['http_code'] : 502);
    }
    $svc = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
    rackflow_admin_link_json(array('ok' => true, 'service' => $svc));
}

if ($action === 'rotate_proxy') {
    $serviceType = rackflow_getConfiguredServiceType($params);
    if ($serviceType !== 'http_proxy') {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => 'Credential rotation is only available for HTTP/SOCKS proxy products.',
        ), 400);
    }
    $rfId = !empty($body['rackflow_service_id']) ? (int)$body['rackflow_service_id'] : (int)$linkedId;
    if ($rfId <= 0) {
        rackflow_admin_link_json(array('ok' => false, 'error' => 'Service is not linked to RackFlow.'), 400);
    }
    $result = rackflow_apiCall(
        $apiUrl,
        $apiKey,
        'POST',
        '/api/billing/services/' . $rfId . '/proxy/rotate',
        array()
    );
    if (!$result['success']) {
        rackflow_admin_link_json(array(
            'ok' => false,
            'error' => rackflow_admin_link_api_detail($result),
        ), !empty($result['http_code']) ? (int)$result['http_code'] : 502);
    }
    $data = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
    rackflow_admin_link_json(array(
        'ok' => true,
        'proxy_assignments' => isset($data['proxy_assignments']) ? $data['proxy_assignments'] : array(),
    ));
}
// Note: the client-facing equivalent lives in proxy_action.php and calls
// rackflow_RotateProxyCredentials(), which hits the same billing endpoint.

rackflow_admin_link_json(array('ok' => false, 'error' => 'Unknown action.'), 400);
