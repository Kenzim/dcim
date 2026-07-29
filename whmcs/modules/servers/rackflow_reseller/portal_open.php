<?php
/**
 * Client-owned one-click RackFlow portal launcher.
 */

@ini_set('display_errors', '0');
while (ob_get_level() > 0) {
    ob_end_clean();
}
ob_start();

require_once __DIR__ . '/../../../init.php';
require_once __DIR__ . '/rackflow_reseller.php';

header('X-Robots-Tag: noindex, nofollow');
header('Cache-Control: no-store, no-cache, must-revalidate');

$whmcsServiceId = isset($_GET['serviceid']) ? (int)$_GET['serviceid'] : 0;
$clientId = !empty($_SESSION['uid']) ? (int)$_SESSION['uid'] : 0;
if ($whmcsServiceId <= 0 || $clientId <= 0) {
    http_response_code(403);
    echo 'A valid client login and service are required.';
    exit;
}
if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    http_response_code(500);
    echo 'WHMCS database services are unavailable.';
    exit;
}

$hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
    ->where('id', $whmcsServiceId)
    ->first();
if (!$hosting || (int)$hosting->userid !== $clientId) {
    http_response_code(404);
    echo 'Service not found.';
    exit;
}
$product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
    ->where('id', (int)$hosting->packageid)
    ->first();
if (!$product || strtolower((string)$product->servertype) !== 'rackflow_reseller') {
    http_response_code(400);
    echo 'This is not a RackFlow reseller service.';
    exit;
}
if (!rackflow_reseller_isPortalEnabled(isset($product->configoption8) ? $product->configoption8 : null)) {
    http_response_code(403);
    echo 'Client portal sign-in is disabled for this product.';
    exit;
}

$params = array(
    'serviceid' => $whmcsServiceId,
    'packageid' => (int)$hosting->packageid,
    'serverid' => (int)$hosting->server,
);
$rackflowServiceId = rackflow_reseller_getServiceId($params);
$apiConfig = rackflow_reseller_getApiConfig($params);
if (!$rackflowServiceId || empty($apiConfig['url']) || empty($apiConfig['key'])) {
    http_response_code(409);
    echo 'This service is not linked to RackFlow.';
    exit;
}

$ticket = rackflow_reseller_mintPortalSsoTicket(
    $apiConfig['url'],
    $apiConfig['key'],
    (int)$rackflowServiceId
);
if (empty($ticket['redeem_url'])) {
    rackflow_reseller_log(
        'portal_sso_failed',
        array('serviceid' => $whmcsServiceId, 'rackflow_service_id' => (int)$rackflowServiceId),
        isset($ticket['error']) ? $ticket['error'] : 'Unknown error'
    );
    http_response_code(502);
    echo 'Unable to open the RackFlow portal.';
    exit;
}

$redeemUrl = (string)$ticket['redeem_url'];
while (ob_get_level() > 0) {
    ob_end_clean();
}
header('Location: ' . $redeemUrl, true, 302);
header('Content-Length: 0');
exit;
