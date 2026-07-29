<?php
/**
 * One-click VM VNC console launcher.
 *
 * Opens in a popup window from admin/client UI, mints a short-lived RackFlow
 * VM VNC launch ticket, then redirects the browser to Rackflow's ``/vnc``
 * page, which redeems the ticket and renders the noVNC viewer. Proxmox
 * account credentials never reach the browser.
 *
 * Usage: /modules/servers/rackflow/vnc_open.php?serviceid=<tblhosting.id>
 */

// Avoid stray notices/warnings corrupting redirects (can trigger HTTP/2 protocol errors).
@ini_set('display_errors', '0');
while (ob_get_level() > 0) {
    ob_end_clean();
}
ob_start();

require_once __DIR__ . '/../../../init.php';
require_once __DIR__ . '/rackflow.php';

header('X-Robots-Tag: noindex, nofollow');
header('Cache-Control: no-store, no-cache, must-revalidate');

$serviceId = isset($_GET['serviceid']) ? (int)$_GET['serviceid'] : 0;
if ($serviceId <= 0) {
    http_response_code(400);
    echo 'Missing serviceid.';
    exit;
}

$isAdmin = !empty($_SESSION['adminid']);
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if (!$isAdmin && $clientId <= 0) {
    http_response_code(403);
    echo 'Login required.';
    exit;
}

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    http_response_code(500);
    echo 'Database unavailable.';
    exit;
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $serviceId)
    ->first();
if (!$hosting) {
    http_response_code(404);
    echo 'Service not found.';
    exit;
}

if (!$isAdmin && (int)$hosting->userid !== $clientId) {
    http_response_code(403);
    echo 'Access denied.';
    exit;
}

$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow') {
    http_response_code(400);
    echo 'Not a RackFlow service.';
    exit;
}

$params = array(
    'serviceid' => $serviceId,
    'userid' => (int)$hosting->userid,
    'packageid' => (int)$hosting->packageid,
    'pid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);

$rackflowServiceId = rackflow_getRackflowServiceId($params);
if (empty($rackflowServiceId)) {
    http_response_code(400);
    echo 'This service is not linked to RackFlow.';
    exit;
}

$apiConfig = rackflow_getApiConfig($params);
$launch = rackflow_mintVncTicket($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
if (empty($launch['launch_url'])) {
    $err = !empty($launch['error']) ? (string)$launch['error'] : 'Unknown error';
    rackflow_log('vnc_open redirect failed', array(
        'serviceid' => $serviceId,
        'rackflow_service_id' => (int)$rackflowServiceId,
        'error' => $err,
    ));
    http_response_code(502);
    echo 'Unable to open VNC console: ' . htmlspecialchars($err, ENT_QUOTES, 'UTF-8');
    exit;
}

$launchUrl = (string)$launch['launch_url'];
// Discard any accidental buffered output before the redirect.
while (ob_get_level() > 0) {
    ob_end_clean();
}
header('Location: ' . $launchUrl, true, 302);
header('Content-Length: 0');
exit;
