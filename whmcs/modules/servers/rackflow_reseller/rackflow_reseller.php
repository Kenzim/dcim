<?php
/**
 * RackFlow reseller provisioning module for WHMCS.
 *
 * This module is intentionally isolated from the RackFlow integration module.
 * Its only backend surface is the tenant-authenticated reseller API.
 */

if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

if (!defined('RACKFLOW_RESELLER_SERVICE_ID_FIELD_NAME')) {
    define('RACKFLOW_RESELLER_SERVICE_ID_FIELD_NAME', 'RackFlow Service ID');
}
if (!defined('RACKFLOW_RESELLER_SSH_KEYS_FIELD_NAME')) {
    define('RACKFLOW_RESELLER_SSH_KEYS_FIELD_NAME', 'SSH Public Keys');
}

function rackflow_reseller_MetaData()
{
    return array(
        'DisplayName' => 'RackFlow Reseller',
        'APIVersion' => '1.1',
        'RequiresServer' => true,
        'DefaultNonSSLPort' => '8000',
        'DefaultSSLPort' => '443',
    );
}

function rackflow_reseller_ConfigOptions()
{
    return array(
        'Service Type' => array(
            'Type' => 'dropdown',
            'Options' => 'bare_metal,vm,http_proxy',
            'Default' => 'bare_metal',
            'SimpleMode' => true,
            'Description' => 'RackFlow reseller service type.',
        ),
        'Product Code' => array(
            'Type' => 'dropdown',
            'Loader' => 'rackflow_reseller_ProductCodeLoader',
            'SimpleMode' => true,
            'Description' => 'An allowed RackFlow reseller catalog product. WHMCS retail pricing remains unchanged.',
        ),
        'OS Code' => array(
            'Type' => 'text',
            'Size' => '40',
            'SimpleMode' => true,
            'Description' => 'Optional default OS profile code when the customer does not select an OS.',
        ),
        'RackFlow Server Group' => array(
            'Type' => 'dropdown',
            'Loader' => 'rackflow_reseller_ServerGroupLoader',
            'SimpleMode' => true,
            'Description' => 'Bare metal only; select a reseller-visible server group.',
        ),
        'Proxmox Location' => array(
            'Type' => 'dropdown',
            'Loader' => 'rackflow_reseller_ProxmoxClusterLoader',
            'SimpleMode' => true,
            'Description' => 'VM only; select a reseller-visible Proxmox cluster.',
        ),
        'Proxmox Node' => array(
            'Type' => 'text',
            'Size' => '40',
            'SimpleMode' => true,
            'Description' => 'VM only; optional node name. Leave blank for automatic placement.',
        ),
        'Customer OS Selection' => array(
            'Type' => 'yesno',
            'SimpleMode' => true,
            'Description' => 'Sync a zero-priced Operating System configurable option from the selected product.',
        ),
        'Allow Client Portal Sign-In' => array(
            'Type' => 'dropdown',
            'Options' => 'Yes,No',
            'Default' => 'Yes',
            'SimpleMode' => true,
            'Description' => 'Show tenant-scoped one-click RackFlow portal sign-in.',
        ),
    );
}

function rackflow_reseller_AdminCustomButtonArray(array $params = array())
{
    $buttons = array('Refresh Status' => 'RefreshStatus');
    if (rackflow_reseller_configuredServiceType($params) !== 'http_proxy') {
        $buttons = array(
            'Power On' => 'PowerOn',
            'Power Off' => 'PowerOff',
            'Reboot' => 'Reboot',
        ) + $buttons;
    }
    return $buttons;
}

function rackflow_reseller_ClientAreaCustomButtonArray(array $params = array())
{
    // Power controls are rendered in clientarea_reseller.tpl; keep the Lagom
    // sidebar Actions list empty so they are not duplicated.
    return array();
}

function rackflow_reseller_redact(array $value)
{
    $redacted = array();
    foreach ($value as $key => $item) {
        $name = strtolower((string)$key);
        if (preg_match('/(?:password|accesshash|api.?key|authorization|client_secret|token)/i', $name)) {
            $redacted[$key] = '[REDACTED]';
        } elseif (is_array($item)) {
            $redacted[$key] = rackflow_reseller_redact($item);
        } else {
            $redacted[$key] = $item;
        }
    }
    return $redacted;
}

function rackflow_reseller_log($action, array $request = array(), $response = '', array $processed = array())
{
    if (!function_exists('logModuleCall')) {
        return;
    }
    logModuleCall(
        'rackflow_reseller',
        (string)$action,
        rackflow_reseller_redact($request),
        is_array($response) ? rackflow_reseller_redact($response) : $response,
        rackflow_reseller_redact($processed)
    );
}

function rackflow_reseller_buildApiBase($hostname, $port, $secure)
{
    $hostname = trim((string)$hostname);
    if ($hostname === '') {
        return '';
    }
    $hostname = preg_replace('#^https?://#i', '', $hostname);
    $hostname = preg_replace('#/.*$#', '', $hostname);
    $hostname = rtrim($hostname, '/');
    if ($hostname === '') {
        return '';
    }
    $scheme = $secure ? 'https' : 'http';
    $port = trim((string)$port);
    $hasPort = preg_match('/:\d+$/', $hostname) === 1;
    if (!$hasPort && $port !== '' && !(($scheme === 'http' && $port === '80') || ($scheme === 'https' && $port === '443'))) {
        $hostname .= ':' . (int)$port;
    }
    return $scheme . '://' . $hostname;
}

function rackflow_reseller_getServerConfigFromDb($serverId)
{
    if (empty($serverId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    try {
        $server = \Illuminate\Database\Capsule\Manager::table('tblservers')
            ->where('id', (int)$serverId)
            ->first();
        return $server ? (array)$server : false;
    } catch (Exception $exception) {
        rackflow_reseller_log('server_config_failed', array('serverid' => (int)$serverId), $exception->getMessage());
        return false;
    }
}

function rackflow_reseller_getProductServerId($productId)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    try {
        $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
            ->where('id', (int)$productId)
            ->first();
        if (!$product || strtolower((string)$product->servertype) !== 'rackflow_reseller') {
            return null;
        }
        if (!empty($product->server)) {
            return (int)$product->server;
        }
        $groupId = !empty($product->servergroup) ? (int)$product->servergroup : 0;
        if ($groupId > 0) {
            $relation = \Illuminate\Database\Capsule\Manager::table('tblservergroupsrel')
                ->where('groupid', $groupId)
                ->orderBy('serverid')
                ->first();
            if ($relation && !empty($relation->serverid)) {
                return (int)$relation->serverid;
            }
        }
    } catch (Exception $exception) {
        rackflow_reseller_log('product_server_failed', array('productid' => (int)$productId), $exception->getMessage());
    }
    return null;
}

function rackflow_reseller_resolveModuleServerId(array $params)
{
    if (!empty($params['serverid'])) {
        return (int)$params['serverid'];
    }
    $productId = !empty($params['pid'])
        ? (int)$params['pid']
        : (!empty($params['packageid']) ? (int)$params['packageid'] : 0);
    return rackflow_reseller_getProductServerId($productId);
}

function rackflow_reseller_getApiConfig(array $params)
{
    $hostname = '';
    $port = '8000';
    $secure = false;
    $key = '';
    $serverId = rackflow_reseller_resolveModuleServerId($params);
    $server = $serverId ? rackflow_reseller_getServerConfigFromDb($serverId) : false;
    if ($server) {
        $hostname = !empty($server['ipaddress']) ? $server['ipaddress'] : (isset($server['hostname']) ? $server['hostname'] : '');
        $port = isset($server['port']) ? $server['port'] : $port;
        $secure = !empty($server['secure']);
        $key = trim(!empty($server['accesshash']) ? $server['accesshash'] : (isset($server['password']) ? $server['password'] : ''));
    } else {
        $hostname = !empty($params['serverip']) ? $params['serverip'] : (isset($params['serverhostname']) ? $params['serverhostname'] : '');
        $port = isset($params['serverport']) ? $params['serverport'] : $port;
        $secure = !empty($params['serversecure']);
        foreach (array('serveraccesshash', 'serverpassword', 'accesshash', 'password') as $field) {
            if (!empty($params[$field])) {
                $key = trim((string)$params[$field]);
                break;
            }
        }
    }
    return array(
        'url' => rackflow_reseller_buildApiBase($hostname, $port, $secure),
        'key' => $key,
    );
}

function rackflow_reseller_buildApiRequest($apiUrl, $apiKey, $method, $endpoint, $data = null, array $extraHeaders = array(), $timeoutSeconds = 30)
{
    $endpoint = (string)$endpoint;
    if (!preg_match('#^/api/reseller(?:/|\?|$)#', $endpoint) || strpos($endpoint, '..') !== false) {
        throw new InvalidArgumentException('Only RackFlow reseller API endpoints are allowed');
    }
    $apiUrl = rtrim((string)$apiUrl, '/');
    $apiKey = trim((string)$apiKey);
    if ($apiUrl === '' || $apiKey === '') {
        throw new InvalidArgumentException('RackFlow API URL and reseller API key are required');
    }
    $method = strtoupper((string)$method);
    $headers = array(
        'Authorization: Bearer ' . $apiKey,
        'Accept: application/json',
        'Content-Type: application/json',
    );
    foreach ($extraHeaders as $name => $value) {
        if ($value !== null && trim((string)$value) !== '') {
            $headers[] = trim((string)$name) . ': ' . trim((string)$value);
        }
    }
    $timeoutSeconds = min(max((int)$timeoutSeconds, 1), 45);
    $body = $data === null ? null : json_encode($data, JSON_UNESCAPED_SLASHES);
    if ($data !== null && $body === false) {
        throw new InvalidArgumentException('Unable to encode RackFlow API request as JSON');
    }
    return array(
        'url' => $apiUrl . $endpoint,
        'method' => $method,
        'headers' => $headers,
        'body' => $body,
        'curl_options' => array(
            'return_transfer' => true,
            'connect_timeout' => 5,
            'timeout' => $timeoutSeconds,
            'ssl_verify_peer' => true,
            'ssl_verify_host' => 2,
        ),
    );
}

function rackflow_reseller_errorMessage(array $result)
{
    $httpCode = isset($result['http_code']) ? (int)$result['http_code'] : 0;
    $data = isset($result['data']) && is_array($result['data']) ? $result['data'] : array();
    $detail = isset($data['detail']) ? $data['detail'] : null;
    if ($httpCode === 402 && is_array($detail)) {
        $code = isset($detail['code']) ? (string)$detail['code'] : 'payment_required';
        if (isset($detail['required_cents'], $detail['available_cents'], $detail['shortfall_cents'], $detail['invoice_id'])) {
            return sprintf(
                'RackFlow payment required (%s): required %d cents, available %d cents, shortfall %d cents; invoice ID %s. No service was provisioned. Fund the invoice, then retry with the same idempotency key.',
                $code,
                (int)$detail['required_cents'],
                (int)$detail['available_cents'],
                (int)$detail['shortfall_cents'],
                (string)$detail['invoice_id']
            );
        }
        $message = !empty($detail['message']) ? (string)$detail['message'] : 'RackFlow has blocked new deployments';
        return 'RackFlow payment required (' . $code . '): ' . $message . '. No service was provisioned.';
    }
    if (is_string($detail) && trim($detail) !== '') {
        return trim($detail);
    }
    if (is_array($detail)) {
        if (!empty($detail['message'])) {
            return (string)$detail['message'];
        }
        if (!empty($detail['code'])) {
            return (string)$detail['code'];
        }
    }
    return !empty($result['error']) ? (string)$result['error'] : ('HTTP ' . $httpCode);
}

function rackflow_reseller_apiCall($apiUrl, $apiKey, $method, $endpoint, $data = null, array $extraHeaders = array(), $timeoutSeconds = 30)
{
    if (!function_exists('curl_init')) {
        return array('success' => false, 'http_code' => 0, 'data' => null, 'error' => 'The PHP cURL extension is required');
    }
    try {
        $request = rackflow_reseller_buildApiRequest($apiUrl, $apiKey, $method, $endpoint, $data, $extraHeaders, $timeoutSeconds);
    } catch (Exception $exception) {
        return array('success' => false, 'http_code' => 0, 'data' => null, 'error' => $exception->getMessage());
    }
    $handle = curl_init();
    curl_setopt($handle, CURLOPT_RETURNTRANSFER, $request['curl_options']['return_transfer']);
    curl_setopt($handle, CURLOPT_CONNECTTIMEOUT, $request['curl_options']['connect_timeout']);
    curl_setopt($handle, CURLOPT_TIMEOUT, $request['curl_options']['timeout']);
    curl_setopt($handle, CURLOPT_SSL_VERIFYPEER, $request['curl_options']['ssl_verify_peer']);
    curl_setopt($handle, CURLOPT_SSL_VERIFYHOST, $request['curl_options']['ssl_verify_host']);
    curl_setopt($handle, CURLOPT_URL, $request['url']);
    curl_setopt($handle, CURLOPT_CUSTOMREQUEST, $request['method']);
    curl_setopt($handle, CURLOPT_HTTPHEADER, $request['headers']);
    if ($request['body'] !== null && in_array($request['method'], array('POST', 'PUT', 'PATCH'), true)) {
        curl_setopt($handle, CURLOPT_POSTFIELDS, $request['body']);
    }
    $raw = curl_exec($handle);
    $httpCode = (int)curl_getinfo($handle, CURLINFO_HTTP_CODE);
    $curlError = curl_error($handle);
    curl_close($handle);
    if ($raw === false || $curlError !== '') {
        return array('success' => false, 'http_code' => 0, 'data' => null, 'error' => 'Connection failed: ' . $curlError);
    }
    $decoded = null;
    if ($raw !== '') {
        $decoded = json_decode($raw, true);
        if (json_last_error() !== JSON_ERROR_NONE) {
            return array('success' => false, 'http_code' => $httpCode, 'data' => null, 'error' => 'RackFlow returned invalid JSON');
        }
    }
    $result = array(
        'success' => $httpCode >= 200 && $httpCode < 300,
        'http_code' => $httpCode,
        'data' => $decoded,
        'error' => '',
    );
    if (!$result['success']) {
        $result['error'] = rackflow_reseller_errorMessage($result);
    }
    return $result;
}

function rackflow_reseller_TestConnection(array $params)
{
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url'])) {
        return array('success' => false, 'error' => 'RackFlow hostname or IP is required.');
    }
    if (empty($config['key'])) {
        return array('success' => false, 'error' => 'A reseller API key is required in Access Hash or Password.');
    }
    $result = rackflow_reseller_apiCall($config['url'], $config['key'], 'GET', '/api/reseller/products', null, array(), 10);
    if (!$result['success']) {
        return array('success' => false, 'error' => 'Connection failed: ' . rackflow_reseller_errorMessage($result));
    }
    $count = is_array($result['data']) ? count($result['data']) : 0;
    return array('success' => true, 'error' => 'Authenticated reseller API; ' . $count . ' product(s) visible.');
}

function rackflow_reseller_configuredServiceType(array $params)
{
    $value = !empty($params['configoption1']) ? $params['configoption1'] : 'bare_metal';
    if (!empty($params['configoptions']['service_type'])) {
        $value = $params['configoptions']['service_type'];
    }
    $value = strtolower(trim((string)$value));
    return in_array($value, array('bare_metal', 'vm', 'http_proxy'), true) ? $value : 'bare_metal';
}

function rackflow_reseller_configOptionValue(array $params, array $names)
{
    $options = isset($params['configoptions']) && is_array($params['configoptions']) ? $params['configoptions'] : array();
    foreach ($names as $name) {
        if (isset($options[$name]) && trim((string)$options[$name]) !== '') {
            return trim((string)$options[$name]);
        }
    }
    return null;
}

function rackflow_reseller_parseIntegerSetting($value)
{
    $value = trim((string)$value);
    if ($value === '') {
        return null;
    }
    if (strpos($value, '|') !== false) {
        foreach (explode('|', $value) as $part) {
            if (ctype_digit(trim($part))) {
                return (int)trim($part);
            }
        }
    }
    return ctype_digit($value) ? (int)$value : null;
}

function rackflow_reseller_parseSshPublicKeys($text)
{
    $result = array('ok' => true, 'keys' => array(), 'error' => null);
    if (trim((string)$text) === '') {
        return $result;
    }
    $pattern = '/^(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+\S+(?:\s+.*)?$/';
    $seen = array();
    foreach (preg_split('/\r\n|\r|\n/', (string)$text) as $line) {
        $line = trim($line);
        if ($line === '' || strpos($line, '#') === 0) {
            continue;
        }
        if (!preg_match($pattern, $line)) {
            return array('ok' => false, 'keys' => array(), 'error' => 'Invalid SSH public key');
        }
        if (!isset($seen[$line])) {
            $seen[$line] = true;
            $result['keys'][] = $line;
        }
    }
    return $result;
}

function rackflow_reseller_sshKeysFromParams(array $params)
{
    $fields = isset($params['customfields']) && is_array($params['customfields']) ? $params['customfields'] : array();
    foreach (array(RACKFLOW_RESELLER_SSH_KEYS_FIELD_NAME, 'ssh_public_keys', 'SSH Public Key') as $name) {
        if (!empty($fields[$name])) {
            return (string)$fields[$name];
        }
    }
    return '';
}

function rackflow_reseller_resolveOsSelection($selected, array $catalogProduct = array())
{
    $value = trim((string)$selected);
    if (strpos($value, '|') !== false) {
        $value = trim(explode('|', $value, 2)[0]);
    }
    if (preg_match('/^rfvt:(\d+)$/', $value, $match)) {
        return array('vm_template_id' => (int)$match[1], 'os_code' => null);
    }
    if (preg_match('/^rfos:(.+)$/', $value, $match)) {
        return array('vm_template_id' => null, 'os_code' => trim($match[1]));
    }
    foreach (isset($catalogProduct['vm_templates']) && is_array($catalogProduct['vm_templates']) ? $catalogProduct['vm_templates'] : array() as $template) {
        if ((isset($template['name']) && strcasecmp($value, (string)$template['name']) === 0) || (isset($template['code']) && $value === (string)$template['code'])) {
            return array('vm_template_id' => (int)$template['id'], 'os_code' => null);
        }
    }
    foreach (isset($catalogProduct['os_profiles']) && is_array($catalogProduct['os_profiles']) ? $catalogProduct['os_profiles'] : array() as $profile) {
        if ((isset($profile['name']) && strcasecmp($value, (string)$profile['name']) === 0) || (isset($profile['code']) && $value === (string)$profile['code'])) {
            return array('vm_template_id' => null, 'os_code' => (string)$profile['code']);
        }
    }
    return array('vm_template_id' => null, 'os_code' => $value !== '' ? $value : null);
}

function rackflow_reseller_clientValue(array $params, $name, $fallback = '')
{
    if (isset($params[$name]) && $params[$name] !== '') {
        return (string)$params[$name];
    }
    if (isset($params['clientsdetails'][$name]) && $params['clientsdetails'][$name] !== '') {
        return (string)$params['clientsdetails'][$name];
    }
    return (string)$fallback;
}

function rackflow_reseller_fetchProduct(array $params, $productCode)
{
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key']) || trim((string)$productCode) === '') {
        return array();
    }
    $result = rackflow_reseller_apiCall(
        $config['url'],
        $config['key'],
        'GET',
        '/api/reseller/products/' . rawurlencode((string)$productCode)
    );
    return $result['success'] && is_array($result['data']) ? $result['data'] : array();
}

function rackflow_reseller_buildCreateRequest(array $params, array $catalogProduct = array())
{
    $serviceType = rackflow_reseller_configuredServiceType($params);
    $productCode = !empty($params['configoption2']) ? trim((string)$params['configoption2']) : '';
    $productOverride = rackflow_reseller_configOptionValue($params, array('product_code', 'Product Code'));
    if ($productOverride !== null) {
        $productCode = $productOverride;
    }
    if ($productCode === '') {
        return array('ok' => false, 'error' => 'Product Code is required.');
    }
    $serviceId = !empty($params['serviceid']) ? (int)$params['serviceid'] : 0;
    $userId = !empty($params['userid']) ? (string)$params['userid'] : '';
    if ($serviceId <= 0 || $userId === '') {
        return array('ok' => false, 'error' => 'WHMCS service and client identity are required.');
    }

    $templateParameters = array();
    if (!empty($params['password'])) {
        $templateParameters['admin_password'] = (string)$params['password'];
    }
    $sshKeys = rackflow_reseller_parseSshPublicKeys(rackflow_reseller_sshKeysFromParams($params));
    if (!$sshKeys['ok']) {
        return array('ok' => false, 'error' => $sshKeys['error']);
    }
    if (!empty($sshKeys['keys'])) {
        $templateParameters['ssh_public_keys'] = $sshKeys['keys'];
    }

    $osCode = !empty($params['configoption3']) ? trim((string)$params['configoption3']) : null;
    $vmTemplateId = null;
    $selection = rackflow_reseller_configOptionValue($params, array('Operating System', 'OS', 'os_code', 'vm_template_id'));
    if ($selection === null && $osCode !== null && $osCode !== '') {
        $selection = $osCode;
    }
    if ($selection !== null) {
        $resolved = rackflow_reseller_resolveOsSelection($selection, $catalogProduct);
        $vmTemplateId = $resolved['vm_template_id'];
        $osCode = $resolved['os_code'];
    }
    $base = array(
        'name' => 'whmcs-service-' . $serviceId,
        'external_service_id' => (string)$serviceId,
        'external_user_id' => $userId,
        'external_username' => rackflow_reseller_clientValue($params, 'username', 'client-' . $userId),
        'external_email' => rackflow_reseller_clientValue($params, 'email'),
        'product_code' => $productCode,
        'description' => isset($params['productname']) ? (string)$params['productname'] : null,
    );
    $serviceConfig = array();
    if (!empty($templateParameters) && $serviceType !== 'http_proxy') {
        $serviceConfig['template_parameters'] = $templateParameters;
    }

    if ($serviceType === 'vm') {
        $payload = $base;
        $payload['service_config'] = $serviceConfig;
        $payload['auto_provision'] = true;
        if ($vmTemplateId) {
            $payload['vm_template_id'] = (int)$vmTemplateId;
        } elseif ($osCode) {
            $payload['os_code'] = $osCode;
        }
        $cluster = rackflow_reseller_configOptionValue($params, array('proxmox_cluster_id', 'Location', 'location'));
        if ($cluster === null && !empty($params['configoption5'])) {
            $cluster = $params['configoption5'];
        }
        $clusterId = rackflow_reseller_parseIntegerSetting($cluster);
        if ($clusterId) {
            $payload['proxmox_cluster_id'] = $clusterId;
        }
        $node = rackflow_reseller_configOptionValue($params, array('proxmox_node_name', 'Node', 'node'));
        if ($node === null && !empty($params['configoption6'])) {
            $node = trim((string)$params['configoption6']);
        }
        if ($node !== null && $node !== '') {
            $payload['proxmox_node_name'] = $node;
        }
        return array(
            'ok' => true,
            'endpoint' => '/api/reseller/vm/services',
            'payload' => $payload,
            'headers' => array('Idempotency-Key' => 'whmcs-service-' . $serviceId),
        );
    }

    $group = rackflow_reseller_configOptionValue($params, array('server_group_id', 'RackFlow Server Group'));
    if ($group === null && !empty($params['configoption4'])) {
        $group = $params['configoption4'];
    }
    $groupId = rackflow_reseller_parseIntegerSetting($group);
    if ($groupId) {
        $serviceConfig['server_group_id'] = $groupId;
    }
    $payload = $base;
    $payload['service_type'] = $serviceType;
    $payload['server_name'] = !empty($params['domain']) ? (string)$params['domain'] : $base['name'];
    $payload['service_config'] = $serviceConfig;
    if ($osCode && $serviceType === 'bare_metal') {
        $payload['os_code'] = $osCode;
    }
    if (!empty($params['dedicatedip'])) {
        $payload['server_ip'] = (string)$params['dedicatedip'];
    }
    return array(
        'ok' => true,
        'endpoint' => '/api/reseller/bare-metal/services',
        'payload' => $payload,
        'headers' => array('Idempotency-Key' => 'whmcs-service-' . $serviceId),
    );
}

function rackflow_reseller_ensureServiceIdCustomField($productId)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    try {
        $field = $capsule::table('tblcustomfields')
            ->where('type', 'product')
            ->where('relid', (int)$productId)
            ->where('fieldname', RACKFLOW_RESELLER_SERVICE_ID_FIELD_NAME)
            ->first();
        if (!$field) {
            $fieldId = (int)$capsule::table('tblcustomfields')->insertGetId(array(
                'type' => 'product',
                'relid' => (int)$productId,
                'fieldname' => RACKFLOW_RESELLER_SERVICE_ID_FIELD_NAME,
                'fieldtype' => 'text',
                'adminonly' => 'on',
                'showorder' => '',
                'showinvoice' => '',
                'required' => '',
            ));
            return $fieldId ?: null;
        }
        $capsule::table('tblcustomfields')->where('id', (int)$field->id)->update(array('adminonly' => 'on'));
        return (int)$field->id;
    } catch (Exception $exception) {
        rackflow_reseller_log('custom_field_failed', array('productid' => (int)$productId), $exception->getMessage());
        return null;
    }
}

function rackflow_reseller_saveServiceIdCustomField($serviceId, $productId, $rackflowServiceId)
{
    $fieldId = rackflow_reseller_ensureServiceIdCustomField($productId);
    if (!$fieldId || empty($serviceId) || empty($rackflowServiceId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    try {
        \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')->updateOrInsert(
            array('fieldid' => $fieldId, 'relid' => (int)$serviceId),
            array('value' => (string)(int)$rackflowServiceId)
        );
        return true;
    } catch (Exception $exception) {
        rackflow_reseller_log('service_id_save_failed', array('serviceid' => (int)$serviceId), $exception->getMessage());
        return false;
    }
}

function rackflow_reseller_getServiceId(array $params)
{
    $serviceId = !empty($params['serviceid']) ? (int)$params['serviceid'] : 0;
    $productId = !empty($params['packageid']) ? (int)$params['packageid'] : (!empty($params['pid']) ? (int)$params['pid'] : 0);
    if (!$serviceId || !$productId || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    try {
        $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('type', 'product')
            ->where('relid', $productId)
            ->where('fieldname', RACKFLOW_RESELLER_SERVICE_ID_FIELD_NAME)
            ->first();
        if (!$field) {
            return null;
        }
        $row = \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
            ->where('fieldid', (int)$field->id)
            ->where('relid', $serviceId)
            ->first();
        return $row && is_numeric(trim((string)$row->value)) ? (int)$row->value : null;
    } catch (Exception $exception) {
        return null;
    }
}

function rackflow_reseller_updateWhmcsService(array $params, array $service)
{
    if (!function_exists('localAPI') || empty($params['serviceid'])) {
        return;
    }
    $updates = array('serviceid' => (int)$params['serviceid']);
    $ip = !empty($service['vm_ip_address']) ? $service['vm_ip_address'] : (!empty($service['server_ip']) ? $service['server_ip'] : '');
    if ($ip === '' && !empty($service['proxy_assignments'][0]['ip_address'])) {
        $ip = $service['proxy_assignments'][0]['ip_address'];
    }
    if ($ip !== '') {
        $updates['dedicatedip'] = (string)$ip;
    }
    if (!empty($service['credentials']) && is_array($service['credentials'])) {
        $credentialUsername = !empty($service['credentials']['username'])
            ? $service['credentials']['username']
            : (!empty($service['credentials']['admin_username']) ? $service['credentials']['admin_username'] : '');
        $credentialPassword = !empty($service['credentials']['password'])
            ? $service['credentials']['password']
            : (!empty($service['credentials']['admin_password']) ? $service['credentials']['admin_password'] : '');
        if ($credentialUsername !== '') {
            $updates['serviceusername'] = (string)$credentialUsername;
        }
        if ($credentialPassword !== '') {
            $updates['servicepassword'] = (string)$credentialPassword;
        }
    } elseif (!empty($service['proxy_assignments'][0]) && is_array($service['proxy_assignments'][0])) {
        $assignment = $service['proxy_assignments'][0];
        if (!empty($assignment['username'])) {
            $updates['serviceusername'] = (string)$assignment['username'];
        }
        if (!empty($assignment['password'])) {
            $updates['servicepassword'] = (string)$assignment['password'];
        }
    }
    if (count($updates) > 1) {
        localAPI('UpdateClientProduct', $updates);
    }
}

function rackflow_reseller_parseCreateEnvelope($data)
{
    $envelope = is_array($data) ? $data : array();
    $service = isset($envelope['service']) && is_array($envelope['service'])
        ? $envelope['service']
        : array();
    if (empty($service['id'])) {
        return array(
            'ok' => false,
            'error' => 'RackFlow returned an invalid create response: nested service.id is missing.',
            'service' => array(),
            'invoice' => array(),
            'idempotent_replay' => false,
        );
    }
    return array(
        'ok' => true,
        'error' => '',
        'service' => $service,
        'invoice' => isset($envelope['invoice']) && is_array($envelope['invoice']) ? $envelope['invoice'] : array(),
        'idempotent_replay' => !empty($envelope['idempotent_replay']),
    );
}

function rackflow_reseller_CreateAccount(array $params)
{
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key'])) {
        return 'RackFlow API URL and reseller API key must be configured.';
    }
    $productCode = !empty($params['configoption2']) ? trim((string)$params['configoption2']) : '';
    $productOverride = rackflow_reseller_configOptionValue($params, array('product_code', 'Product Code'));
    if ($productOverride !== null) {
        $productCode = $productOverride;
    }
    $catalogProduct = $productCode !== '' ? rackflow_reseller_fetchProduct($params, $productCode) : array();
    $request = rackflow_reseller_buildCreateRequest($params, $catalogProduct);
    if (!$request['ok']) {
        return $request['error'];
    }
    $result = rackflow_reseller_apiCall(
        $config['url'],
        $config['key'],
        'POST',
        $request['endpoint'],
        $request['payload'],
        $request['headers'],
        45
    );
    if (!$result['success']) {
        return rackflow_reseller_errorMessage($result);
    }
    $parsed = rackflow_reseller_parseCreateEnvelope($result['data']);
    if (!$parsed['ok']) {
        return $parsed['error'];
    }
    $service = $parsed['service'];
    if (!rackflow_reseller_saveServiceIdCustomField(
        (int)$params['serviceid'],
        !empty($params['packageid']) ? (int)$params['packageid'] : (int)$params['pid'],
        (int)$service['id']
    )) {
        return 'RackFlow service was created, but WHMCS could not save the RackFlow Reseller Service ID. Manual reconciliation is required.';
    }
    rackflow_reseller_updateWhmcsService($params, $service);
    rackflow_reseller_log(
        'create_success',
        array('serviceid' => (int)$params['serviceid'], 'service_type' => rackflow_reseller_configuredServiceType($params)),
        '',
        array(
            'rackflow_service_id' => (int)$service['id'],
            'invoice_id' => isset($parsed['invoice']['id']) ? (int)$parsed['invoice']['id'] : null,
            'idempotent_replay' => $parsed['idempotent_replay'],
        )
    );
    return 'success';
}

function rackflow_reseller_serviceEndpoint($serviceId, $suffix = '')
{
    $serviceId = (int)$serviceId;
    $suffix = (string)$suffix;
    if ($serviceId <= 0 || ($suffix !== '' && !preg_match('#^/[a-z-]+$#', $suffix))) {
        throw new InvalidArgumentException('Invalid RackFlow reseller service endpoint');
    }
    return '/api/reseller/services/' . $serviceId . $suffix;
}

function rackflow_reseller_lifecycleAction(array $params, $method, $suffix, $data = null)
{
    $serviceId = rackflow_reseller_getServiceId($params);
    if (!$serviceId) {
        return 'This WHMCS service has no RackFlow Reseller Service ID.';
    }
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key'])) {
        return 'RackFlow API URL and reseller API key must be configured.';
    }
    $endpoint = rackflow_reseller_serviceEndpoint($serviceId, $suffix);
    $result = rackflow_reseller_apiCall($config['url'], $config['key'], $method, $endpoint, $data);
    return $result['success'] ? 'success' : rackflow_reseller_errorMessage($result);
}

function rackflow_reseller_SuspendAccount(array $params)
{
    $reason = !empty($params['suspendreason']) ? (string)$params['suspendreason'] : 'Suspended from WHMCS';
    return rackflow_reseller_lifecycleAction($params, 'POST', '/suspend', array('reason' => $reason));
}

function rackflow_reseller_UnsuspendAccount(array $params)
{
    return rackflow_reseller_lifecycleAction($params, 'POST', '/unsuspend', array('reason' => 'Unsuspended from WHMCS'));
}

function rackflow_reseller_TerminateAccount(array $params)
{
    return rackflow_reseller_lifecycleAction($params, 'DELETE', '', null);
}

function rackflow_reseller_powerAction(array $params, $action)
{
    return rackflow_reseller_lifecycleAction($params, 'POST', '/power', array('action' => (string)$action));
}

function rackflow_reseller_PowerOn(array $params)
{
    return rackflow_reseller_powerAction($params, 'on');
}

function rackflow_reseller_PowerOff(array $params)
{
    return rackflow_reseller_powerAction($params, 'off');
}

function rackflow_reseller_Reboot(array $params)
{
    return rackflow_reseller_powerAction($params, 'reboot');
}

function rackflow_reseller_statusPayload(array $params)
{
    $serviceId = rackflow_reseller_getServiceId($params);
    $config = rackflow_reseller_getApiConfig($params);
    if (!$serviceId || empty($config['url']) || empty($config['key'])) {
        return array('success' => false, 'error' => 'Service is not linked or API configuration is missing.');
    }
    return rackflow_reseller_apiCall(
        $config['url'],
        $config['key'],
        'GET',
        rackflow_reseller_serviceEndpoint($serviceId, '/status')
    );
}

function rackflow_reseller_RefreshStatus(array $params)
{
    $result = rackflow_reseller_statusPayload($params);
    if (!$result['success']) {
        return rackflow_reseller_errorMessage($result);
    }
    $state = !empty($result['data']['power_state']) ? $result['data']['power_state'] : 'unknown';
    return 'RackFlow status: ' . $state;
}

function rackflow_reseller_isPortalEnabled($value)
{
    if ($value === null || $value === '') {
        return true;
    }
    return !in_array(strtolower(trim((string)$value)), array('0', 'no', 'off', 'false'), true);
}

function rackflow_reseller_portalEndpointUrl($serviceId, $systemUrl = '')
{
    $basePath = '';
    $documentRoot = !empty($_SERVER['DOCUMENT_ROOT']) ? realpath($_SERVER['DOCUMENT_ROOT']) : false;
    $whmcsRoot = realpath(__DIR__ . '/../../..');
    if ($documentRoot && $whmcsRoot && strpos($whmcsRoot, $documentRoot) === 0) {
        $basePath = str_replace(DIRECTORY_SEPARATOR, '/', substr($whmcsRoot, strlen($documentRoot)));
    } elseif ($systemUrl !== '') {
        $basePath = rtrim((string)parse_url($systemUrl, PHP_URL_PATH), '/');
    }
    return $basePath . '/modules/servers/rackflow_reseller/portal_open.php?serviceid=' . (int)$serviceId;
}

function rackflow_reseller_mintPortalSsoTicket($apiUrl, $apiKey, $rackflowServiceId)
{
    $result = rackflow_reseller_apiCall(
        $apiUrl,
        $apiKey,
        'POST',
        rackflow_reseller_serviceEndpoint($rackflowServiceId, '/portal-sso'),
        array()
    );
    if (!$result['success']) {
        return array('redeem_url' => '', 'error' => rackflow_reseller_errorMessage($result));
    }
    $data = is_array($result['data']) ? $result['data'] : array();
    $path = isset($data['redeem_path']) ? (string)$data['redeem_path'] : '';
    $token = isset($data['token']) ? (string)$data['token'] : '';
    if ($path !== '/api/client/sso/redeem' || $token === '') {
        return array('redeem_url' => '', 'error' => 'RackFlow returned an invalid portal SSO response.');
    }
    return array(
        'redeem_url' => rtrim((string)$apiUrl, '/') . $path . '?token=' . rawurlencode($token),
        'error' => '',
    );
}

function rackflow_reseller_statusBadgeClass($status)
{
    $value = strtolower(trim((string)$status));
    if (in_array($value, array('active', 'running', 'on', 'online'), true)) {
        return 'rfr-ca__badge--on';
    }
    if (in_array($value, array('pending', 'provisioning', 'installing', 'deploying'), true)) {
        return 'rfr-ca__badge--warn';
    }
    if (in_array($value, array('suspended', 'terminated', 'cancelled', 'failed', 'offline', 'off'), true)) {
        return 'rfr-ca__badge--off';
    }
    return 'rfr-ca__badge--unknown';
}

function rackflow_reseller_ClientArea(array $vars)
{
    $params = array_merge($vars, isset($vars['params']) && is_array($vars['params']) ? $vars['params'] : array());
    $status = rackflow_reseller_statusPayload($params);
    $data = $status['success'] && is_array($status['data']) ? $status['data'] : array();
    $powerAvailable = !empty($data['power_available']);
    $portalEnabled = rackflow_reseller_isPortalEnabled(isset($params['configoption8']) ? $params['configoption8'] : null);
    $serviceStatus = !empty($data['status']) ? (string)$data['status'] : 'unknown';
    return array(
        'templatefile' => 'clientarea_reseller',
        'vars' => array(
            'rackflow_reseller_linked' => !empty($data['id']),
            'rackflow_reseller_error' => $status['success'] ? '' : rackflow_reseller_errorMessage($status),
            'rackflow_reseller_status' => $serviceStatus,
            'rackflow_reseller_status_badge' => rackflow_reseller_statusBadgeClass($serviceStatus),
            'rackflow_reseller_power_state' => !empty($data['power_state']) ? $data['power_state'] : 'unknown',
            'rackflow_reseller_power_available' => $powerAvailable,
            'rackflow_reseller_primary_ip' => !empty($data['vm_ip_address']) ? $data['vm_ip_address'] : (!empty($data['server_ip']) ? $data['server_ip'] : ''),
            'rackflow_reseller_hostname' => !empty($params['domain']) ? (string)$params['domain'] : '',
            'rackflow_reseller_power_on_url' => 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=PowerOn',
            'rackflow_reseller_power_off_url' => 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=PowerOff',
            'rackflow_reseller_reboot_url' => 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=Reboot',
            'rackflow_reseller_portal_url' => $portalEnabled && !empty($params['serviceid'])
                ? rackflow_reseller_portalEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? $vars['systemurl'] : '')
                : '',
        ),
    );
}

function rackflow_reseller_loaderParams(array $params)
{
    $serverId = rackflow_reseller_resolveModuleServerId($params);
    if ($serverId) {
        $params['serverid'] = $serverId;
    }
    return $params;
}

function rackflow_reseller_ProductCodeLoader($params)
{
    $params = rackflow_reseller_loaderParams(is_array($params) ? $params : array());
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key'])) {
        return array('' => '-- Configure a RackFlow reseller server first --');
    }
    $serviceType = !empty($params['configoption1']) ? strtolower(trim((string)$params['configoption1'])) : '';
    $endpoint = '/api/reseller/products';
    if ($serviceType !== '') {
        $endpoint .= '?service_type=' . rawurlencode($serviceType);
    }
    $result = rackflow_reseller_apiCall($config['url'], $config['key'], 'GET', $endpoint);
    if (!$result['success'] || !is_array($result['data'])) {
        return array('' => '-- No allowed reseller products found --');
    }
    $options = array('' => '-- Select a RackFlow product --');
    foreach ($result['data'] as $product) {
        if (!empty($product['code'])) {
            $label = !empty($product['name']) ? $product['name'] : $product['code'];
            $options[(string)$product['code']] = $label . ' (' . $product['code'] . ')';
        }
    }
    return $options;
}

function rackflow_reseller_ProxmoxClusterLoader($params)
{
    $params = rackflow_reseller_loaderParams(is_array($params) ? $params : array());
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key'])) {
        return array('' => '-- Configure a RackFlow reseller server first --');
    }
    $result = rackflow_reseller_apiCall($config['url'], $config['key'], 'GET', '/api/reseller/proxmox/clusters');
    if (!$result['success'] || !is_array($result['data'])) {
        return array('' => '-- No reseller-visible clusters found --');
    }
    $options = array('' => '-- Automatic placement --');
    foreach ($result['data'] as $cluster) {
        if (isset($cluster['id'])) {
            $options[(string)$cluster['id']] = (!empty($cluster['name']) ? $cluster['name'] : 'Cluster') . ' (ID: ' . $cluster['id'] . ')';
        }
    }
    return $options;
}

function rackflow_reseller_ServerGroupLoader($params)
{
    $params = rackflow_reseller_loaderParams(is_array($params) ? $params : array());
    $config = rackflow_reseller_getApiConfig($params);
    if (empty($config['url']) || empty($config['key'])) {
        return array('' => '-- Configure a RackFlow reseller server first --');
    }
    $result = rackflow_reseller_apiCall($config['url'], $config['key'], 'GET', '/api/reseller/server-groups');
    if (!$result['success'] || !is_array($result['data'])) {
        return array('' => '-- No reseller-visible server groups found --');
    }
    $options = array('' => '-- None --');
    foreach ($result['data'] as $group) {
        if (isset($group['id'])) {
            $options[(string)$group['id']] = (!empty($group['name']) ? $group['name'] : 'Group') . ' (ID: ' . $group['id'] . ')';
        }
    }
    return $options;
}
