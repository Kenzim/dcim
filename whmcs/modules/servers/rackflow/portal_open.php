<?php
/**
 * One-click RackFlow client portal launcher.
 *
 * Opens in a new tab from the WHMCS client area, mints a short-lived RackFlow
 * portal SSO ticket for the logged-in WHMCS client's own service, then
 * redirects the browser straight to RackFlow's redeem endpoint, which signs
 * them into /client and drops the ticket.
 *
 * Usage: /modules/servers/rackflow/portal_open.php?serviceid=<tblhosting.id>
 *
 * Client-only (unlike ipmi_open.php): admin accounts have no RackFlow client
 * portal, so this intentionally does not allow the admin-session bypass.
 */

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

$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if ($clientId <= 0) {
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

if ((int)$hosting->userid !== $clientId) {
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
    'configoption8' => isset($product->configoption8) ? $product->configoption8 : null,
);

if (!rackflow_isPortalSignInEnabled(isset($params['configoption8']) ? $params['configoption8'] : null)) {
    http_response_code(403);
    echo 'Client portal sign-in is disabled for this product.';
    exit;
}

$rackflowServiceId = rackflow_getRackflowServiceId($params);
if (empty($rackflowServiceId)) {
    http_response_code(400);
    echo 'This service is not linked to RackFlow.';
    exit;
}

$apiConfig = rackflow_getApiConfig($params);
$ticket = rackflow_mintPortalSsoTicket($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
if (empty($ticket['redeem_url'])) {
    $err = !empty($ticket['error']) ? (string)$ticket['error'] : 'Unknown error';
    rackflow_log('portal_open redirect failed', array(
        'serviceid' => $serviceId,
        'rackflow_service_id' => (int)$rackflowServiceId,
        'error' => $err,
    ));
    http_response_code(502);
    echo 'Unable to open client portal: ' . htmlspecialchars($err, ENT_QUOTES, 'UTF-8');
    exit;
}

$redeemUrl = (string)$ticket['redeem_url'];
while (ob_get_level() > 0) {
    ob_end_clean();
}
header('Location: ' . $redeemUrl, true, 302);
header('Content-Length: 0');
exit;
