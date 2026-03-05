<?php
/**
 * RackFlow Provisioning Module for WHMCS
 *
 * This module allows WHMCS to provision and manage servers through the RackFlow backend API.
 *
 * For linking already-deployed servers (no provisioning): use "Register in RackFlow" from the
 * admin service tab. Create a Product Custom Field named exactly "RackFlow Service ID" for the
 * RackFlow product so the mapping is stored.
 *
 * @copyright Copyright (c) 2025
 * @license MIT
 */

if (!defined("WHMCS")) {
    die("This file cannot be accessed directly");
}

/**
 * Log a message to /rackflow.log.
 *
 * @param string $message Log message
 * @param array $context Optional context (sensitive keys like password/accesshash are redacted)
 */
function rackflow_log($message, array $context = array())
{
    $redactKeys = array('serverpassword', 'serveraccesshash', 'password', 'accesshash', 'api_key', 'apiKey');
    $safe = array();
    foreach ($context as $k => $v) {
        $keyLower = is_string($k) ? strtolower($k) : $k;
        $safe[$k] = in_array($keyLower, $redactKeys) ? '[REDACTED]' : $v;
    }
    $line = date('Y-m-d H:i:s') . ' ' . $message;
    if (!empty($safe)) {
        $line .= ' ' . json_encode($safe);
    }
    $line .= "\n";
    @file_put_contents('/rackflow.log', $line, FILE_APPEND | LOCK_EX);
}

/**
 * Define module related meta data.
 *
 * @return array
 */
function rackflow_MetaData()
{
    return array(
        'DisplayName' => 'RackFlow Server Management',
        'APIVersion' => '1.1',
        'RequiresServer' => true, // The RackFlow backend is the server that provisions services
        'DefaultNonSSLPort' => '8000',
        'DefaultSSLPort' => '8443',
    );
}

/**
 * Admin custom buttons (Module Commands dropdown).
 * These appear alongside Create, Suspend, etc. and call the named custom function.
 * @see https://developers.whmcs.com/provisioning-modules/custom-functions/
 *
 * @return array Button label => custom function name (without module prefix)
 */
function rackflow_AdminCustomButtonArray()
{
    return array(
        'Register in RackFlow' => 'RegisterInRackflow',
        'Power On' => 'PowerOn',
        'Power Off' => 'PowerOff',
        'Reboot' => 'Reboot',
    );
}

/**
 * Client Area custom buttons (shown to clients on the product details page).
 * @see https://developers.whmcs.com/provisioning-modules/custom-functions/
 *
 * @return array Button label => custom function name (without module prefix)
 */
function rackflow_ClientAreaCustomButtonArray()
{
    return array(
        'Power On' => 'PowerOn',
        'Power Off' => 'PowerOff',
        'Reboot' => 'Reboot',
    );
}

/**
 * Client area output: power status and server info (no RackFlow ID shown).
 *
 * @param array $vars serviceid, model, packageid, serverid, etc.
 * @return array templatefile and vars for Smarty
 */
function rackflow_ClientArea(array $vars)
{
    $params = array(
        'serviceid' => isset($vars['serviceid']) ? $vars['serviceid'] : (isset($vars['model']) && is_object($vars['model']) ? $vars['model']->id : 0),
        'packageid' => isset($vars['packageid']) ? $vars['packageid'] : (isset($vars['model']) && is_object($vars['model']) ? $vars['model']->packageid : 0),
        'pid' => isset($vars['pid']) ? $vars['pid'] : null,
        'serverid' => isset($vars['serverid']) ? $vars['serverid'] : (isset($vars['model']) && is_object($vars['model']) && isset($vars['model']->serverId) ? $vars['model']->serverId : 0),
    );
    if (empty($params['pid']) && !empty($params['packageid'])) {
        $params['pid'] = $params['packageid'];
    }
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    $powerStatusLabel = '';
    $powerStatusStyle = '';
    $serverName = '';
    $serviceStatus = '';
    $powerAvailable = false;
    $powerMessage = '';
    if (!empty($rackflowServiceId)) {
        $apiConfig = rackflow_getApiConfig($params);
        $statusData = rackflow_fetchServiceStatus($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
        if ($statusData) {
            $powerAvailable = true;
            $serviceStatus = isset($statusData['service_status']) ? $statusData['service_status'] : '';
            $serverName = isset($statusData['server_name']) ? $statusData['server_name'] : '';
            $statusLabel = isset($statusData['status']) ? $statusData['status'] : (isset($statusData['power_state']) ? strtolower($statusData['power_state']) : 'unknown');
            $powerStatusLabel = $statusLabel;
            $powerStatusStyle = $statusLabel === 'on' ? 'background:#28a745;color:#fff;' : ($statusLabel === 'off' ? 'background:#6c757d;color:#fff;' : ($statusLabel === 'suspended' ? 'background:#ffc107;color:#212529;' : 'background:#6c757d;color:#fff;'));
        } else {
            $powerMessage = 'Unable to load server status.';
        }
    } else {
        $powerMessage = 'Server status will appear here once the service is linked.';
    }
    return array(
        'templatefile' => 'clientarea',
        'vars' => array(
            'rackflow_power_available' => $powerAvailable,
            'rackflow_power_status_label' => $powerStatusLabel,
            'rackflow_power_status_style' => $powerStatusStyle,
            'rackflow_server_name' => $serverName,
            'rackflow_service_status' => $serviceStatus,
            'rackflow_power_message' => $powerMessage,
        ),
    );
}

/**
 * Define product configuration options.
 *
 * These options appear when configuring a product for use with this module.
 * Backend API configuration is done at the server level (Setup > Servers).
 *
 * @return array
 */
function rackflow_ConfigOptions()
{
    return array(
        'RackFlow Server Group' => array(
            'Type' => 'text',
            'Size' => '25',
            'Loader' => 'rackflow_ServerGroupLoader',
            'SimpleMode' => true,
            'Description' => 'RackFlow/DCIM server group. Permitted ISOs, scripts, and OS templates are configured per group in the RackFlow admin UI.',
        ),
    );
}

/**
 * Test connection with the RackFlow backend.
 *
 * This function is called when adding/editing a server in WHMCS.
 * It verifies that the API URL and API key are valid.
 *
 * @param array $params common module parameters
 *
 * @return array
 */
function rackflow_TestConnection(array $params)
{
    try {
        // Extract configuration from server parameters
        // When testing a server (Setup > Servers), use serverhostname and serverpassword/serveraccesshash
        // When testing a product, use configoption1 and configoption2
        
        $apiUrl = '';
        $apiKey = '';
        $port = isset($params['serverport']) ? $params['serverport'] : '8000';
        $useSSL = isset($params['serversecure']) ? $params['serversecure'] : false;
        
        // Server configuration comes from Setup > Servers (use Access Hash or Password)
        $hostname = !empty($params['serverip']) ? $params['serverip'] : ($params['serverhostname'] ?? '');
        $apiKey = trim($params['serveraccesshash'] ?? $params['serverpassword'] ?? $params['accesshash'] ?? $params['password'] ?? '');
        
        // Build API URL - add protocol if missing
        if (empty($hostname)) {
            return array(
                'success' => false,
                'error' => 'API URL is required. Enter the RackFlow backend IP address in the IP Address field.',
            );
        }
        
        // Remove any existing protocol
        $hostname = preg_replace('#^https?://#', '', $hostname);
        $hostname = rtrim($hostname, '/');
        
        // Build full URL with protocol and port
        $protocol = $useSSL ? 'https' : 'http';
        $apiUrl = $protocol . '://' . $hostname;
        if (!empty($port) && $port != '80' && $port != '443') {
            $apiUrl .= ':' . $port;
        }
        
        if (empty($apiKey)) {
            return array(
                'success' => false,
                'error' => 'API key is required. Enter your RackFlow Billing Integration API key in the Password field or the Access Hash field.',
            );
        }
        
        // Test connection by calling the billing API endpoint (doesn't require admin auth)
        // This endpoint is available to billing integrations
        $testUrl = $apiUrl . '/api/billing/scripts';
        
        $ch = curl_init();
        curl_setopt($ch, CURLOPT_URL, $testUrl);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 10);
        curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
        curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, false);
        curl_setopt($ch, CURLOPT_HTTPHEADER, array(
            'Authorization: Bearer ' . $apiKey,
            'Content-Type: application/json',
        ));
        
        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curlError = curl_error($ch);
        curl_close($ch);
        
        if ($curlError) {
            return array(
                'success' => false,
                'error' => 'Connection error: ' . $curlError . '. Please check that the API URL is correct and reachable.',
            );
        }
        
        if ($httpCode == 401) {
            return array(
                'success' => false,
                'error' => 'Authentication failed. Please check your API key is correct.',
            );
        }
        
        if ($httpCode == 404) {
            return array(
                'success' => false,
                'error' => 'API endpoint not found. Please verify the API URL is correct (should be the base URL, e.g., http://192.168.1.100:8000).',
            );
        }
        
        if ($httpCode >= 200 && $httpCode < 300) {
            // Try to fetch and display available server groups
            $serverGroupsInfo = '';
            try {
                $groups = rackflow_getServerGroups($params);
                if ($groups && is_array($groups) && count($groups) > 0) {
                    $serverGroupsInfo = "\n\nAvailable Server Groups:\n";
                    foreach ($groups as $group) {
                        if (isset($group['id']) && isset($group['name'])) {
                            $serverGroupsInfo .= "  - ID: {$group['id']}, Name: {$group['name']}\n";
                        }
                    }
                }
            } catch (Exception $e) {
                // Ignore errors when fetching groups - connection test is the main thing
            }
            
            return array(
                'success' => true,
                'error' => $serverGroupsInfo,
            );
        }
        
        $errorMsg = 'HTTP ' . $httpCode;
        if ($response) {
            $responseData = json_decode($response, true);
            if (isset($responseData['detail'])) {
                $errorMsg = $responseData['detail'];
            }
        }
        
        return array(
            'success' => false,
            'error' => 'Connection test failed: ' . $errorMsg,
        );
        
    } catch (Exception $e) {
        logModuleCall(
            'rackflow',
            __FUNCTION__,
            $params,
            $e->getMessage(),
            $e->getTraceAsString()
        );
        
        return array(
            'success' => false,
            'error' => $e->getMessage(),
        );
    }
}

/**
 * Make API call to RackFlow backend.
 *
 * @param string $apiUrl Base API URL
 * @param string $apiKey API key
 * @param string $method HTTP method (GET, POST, etc.)
 * @param string $endpoint API endpoint (e.g., /api/billing/services)
 * @param array $data Request data (for POST/PUT)
 *
 * @return array Response data or error
 */
function rackflow_apiCall($apiUrl, $apiKey, $method, $endpoint, $data = null)
{
    $url = rtrim($apiUrl, '/') . $endpoint;
    
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 30);
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
    curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, false);
    
    $headers = array(
        'Authorization: Bearer ' . $apiKey,
        'Content-Type: application/json',
    );
    
    if ($method == 'POST' || $method == 'PUT') {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
    }
    
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError = curl_error($ch);
    curl_close($ch);
    
    if ($curlError) {
        return array(
            'success' => false,
            'error' => 'CURL error: ' . $curlError,
            'http_code' => 0,
        );
    }
    
    $responseData = json_decode($response, true);
    
    if ($httpCode >= 200 && $httpCode < 300) {
        return array(
            'success' => true,
            'data' => $responseData,
            'http_code' => $httpCode,
        );
    }
    
    $errorMsg = 'HTTP ' . $httpCode;
    if ($responseData && isset($responseData['detail'])) {
        $errorMsg = $responseData['detail'];
    }
    
    return array(
        'success' => false,
        'error' => $errorMsg,
        'http_code' => $httpCode,
        'data' => $responseData,
    );
}

/**
 * Provision a new instance of a product/service.
 *
 * @param array $params common module parameters
 *
 * @return string "success" or an error message
 */
function rackflow_CreateAccount(array $params)
{
    try {
        // Get API configuration from server settings
        $apiConfig = rackflow_getApiConfig($params);
        $apiUrl = $apiConfig['url'];
        $apiKey = $apiConfig['key'];
        
        if (empty($apiUrl) || empty($apiKey)) {
            return 'Error: API URL and API Key must be configured in server settings';
        }
        
        // Extract configuration options
        // configoption1 = RackFlow Server Group ID
        // Backend API details come from server configuration (serverip, serverhostname, serverpassword, etc.)
        $serverGroupId = isset($params['configoption1']) && !empty($params['configoption1']) ? (int)$params['configoption1'] : null;
        
        // Get Location ID and Plugin Name from server custom fields or use defaults
        // These can be configured in the server settings (Setup > Servers) as custom fields
        $locationId = isset($params['servercustomfields']['location_id']) ? (int)$params['servercustomfields']['location_id'] : 1;
        $pluginName = isset($params['servercustomfields']['plugin_name']) ? $params['servercustomfields']['plugin_name'] : 'proxmox';
        
        // Get service details from WHMCS
        $serviceName = isset($params['serviceid']) ? 'service-' . $params['serviceid'] : 'service-' . time();
        $serverName = isset($params['domain']) ? $params['domain'] : $serviceName;
        $serverIp = isset($params['customfields']['server_ip']) ? $params['customfields']['server_ip'] : '';
        
        // Get user information
        $externalUserId = isset($params['userid']) ? (string)$params['userid'] : '';
        $externalUsername = isset($params['username']) ? $params['username'] : '';
        $externalEmail = isset($params['email']) ? $params['email'] : '';
        
        // Build service creation data
        $serviceData = array(
            'name' => $serviceName,
            'external_service_id' => isset($params['serviceid']) ? (string)$params['serviceid'] : null,
            'external_user_id' => $externalUserId,
            'external_username' => $externalUsername,
            'external_email' => $externalEmail,
            'server_name' => $serverName,
            'server_ip' => $serverIp ?: '0.0.0.0', // Will need to be configured
            'description' => isset($params['productname']) ? $params['productname'] : null,
            'cpu_count' => isset($params['configoptions']['cpu_count']) ? (int)$params['configoptions']['cpu_count'] : 1,
            'ram_gb' => isset($params['configoptions']['ram_gb']) ? (int)$params['configoptions']['ram_gb'] : null,
            'port_speed_mbps' => isset($params['configoptions']['port_speed_mbps']) ? (int)$params['configoptions']['port_speed_mbps'] : null,
            'location_id' => $locationId,
            'plugin_name' => $pluginName,
            'plugin_config' => isset($params['configoptions']['plugin_config']) ? $params['configoptions']['plugin_config'] : array(),
            'os_boot_mode' => isset($params['configoptions']['os_boot_mode']) ? $params['configoptions']['os_boot_mode'] : 'uefi',
            'disks' => isset($params['configoptions']['disks']) ? $params['configoptions']['disks'] : array(),
            'network_ports' => isset($params['configoptions']['network_ports']) ? $params['configoptions']['network_ports'] : array(),
        );
        
        // Create service via billing API
        $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services', $serviceData);
        
        if (!$result['success']) {
            return 'Error creating service: ' . (isset($result['error']) ? $result['error'] : 'Unknown error');
        }
        
        $service = $result['data'];
        
        // If server group is specified, assign server to group
        if ($serverGroupId && isset($service['server_id'])) {
            // Add server to group via admin API
            // Note: This requires admin API access, not billing API
            // If using billing API key, this will fail gracefully
            $groupResult = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/server-groups/' . $serverGroupId . '/servers', array(
                'server_ids' => array($service['server_id'])
            ));
            
            if (!$groupResult['success']) {
                // Log but don't fail - server group assignment is optional
                logModuleCall(
                    'rackflow',
                    __FUNCTION__ . '_server_group',
                    array('server_id' => $service['server_id'], 'group_id' => $serverGroupId),
                    $groupResult['error'],
                    ''
                );
                // Continue - service was created successfully
            }
        }
        
        // Store service ID in custom field for future reference
        if (isset($params['serviceid'])) {
            // You may want to store $service['id'] in a custom field
        }
        

        return 'success';
        
    } catch (Exception $e) {
        logModuleCall(
            'rackflow',
            __FUNCTION__,
            $params,
            $e->getMessage(),
            $e->getTraceAsString()
        );
        return 'Error: ' . $e->getMessage();
    }
}

/**
 * Get API configuration from params.
 *
 * Uses serverid when present (product/provisioning); otherwise direct params
 * when testing from Setup > Servers (serverip, serverhostname, serverpassword).
 *
 * @param array $params Module parameters
 *
 * @return array API URL and key
 */
function rackflow_getApiConfig($params)
{
    $hostname = '';
    $apiKey = '';
    $port = '8000';
    $useSSL = false;

    if (!empty($params['serverid'])) {
        $serverConfig = rackflow_getServerConfigFromDB((int)$params['serverid']);
        if ($serverConfig) {
            $hostname = !empty($serverConfig['ipaddress']) ? $serverConfig['ipaddress'] : ($serverConfig['hostname'] ?? '');
            $apiKey = trim($serverConfig['accesshash'] ?? $serverConfig['password'] ?? '');
            $port = isset($serverConfig['port']) ? $serverConfig['port'] : '8000';
            $useSSL = isset($serverConfig['secure']) ? (bool)$serverConfig['secure'] : false;
        }
    } elseif (!empty($params['serverip']) || !empty($params['serverhostname'])) {
        $hostname = !empty($params['serverip']) ? $params['serverip'] : $params['serverhostname'];
        $apiKey = trim($params['serveraccesshash'] ?? $params['serverpassword'] ?? $params['accesshash'] ?? $params['password'] ?? '');
        $port = isset($params['serverport']) ? $params['serverport'] : '8000';
        $useSSL = isset($params['serversecure']) ? $params['serversecure'] : false;
    }

    $protocol = $useSSL ? 'https' : 'http';
    $hostname = preg_replace('#^https?://#', '', $hostname);
    $hostname = rtrim($hostname, '/');
    $apiUrl = $protocol . '://' . $hostname;
    if (!empty($port) && $port != '80' && $port != '443') {
        $apiUrl .= ':' . $port;
    }

    if (empty($apiUrl) || empty($apiKey)) {
        rackflow_log('rackflow_getApiConfig: no API URL/key resolved', array('has_serverid' => !empty($params['serverid'])));
    }

    return array(
        'url' => $apiUrl,
        'key' => $apiKey,
    );
}

/**
 * Get server configuration from WHMCS database.
 *
 * @param int $serverId Server ID
 *
 * @return array|false Server configuration array or false on error
 */
function rackflow_getServerConfigFromDB($serverId)
{
    try {
        // Use WHMCS database functions
        // Check if Capsule (Laravel DB) is available (WHMCS 7.0+)
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $server = \Illuminate\Database\Capsule\Manager::table('tblservers')
                ->where('id', $serverId)
                ->first();
            
            if ($server) {
                return (array)$server;
            }
        } 
        // Fallback to legacy full_query function
        elseif (function_exists('full_query')) {
            $result = full_query("SELECT * FROM tblservers WHERE id = " . (int)$serverId);
            if ($result && mysql_num_rows($result) > 0) {
                return mysql_fetch_assoc($result);
            }
        }
        // Last resort: direct query (not recommended but works)
        else {
            $result = mysql_query("SELECT * FROM tblservers WHERE id = " . (int)$serverId);
            if ($result && mysql_num_rows($result) > 0) {
                return mysql_fetch_assoc($result);
            }
        }
        
        return false;
    } catch (Exception $e) {
        logModuleCall(
            'rackflow',
            __FUNCTION__,
            array('serverid' => $serverId),
            $e->getMessage(),
            ''
        );
        return false;
    }
}

/**
 * Get the product's WHMCS Server Group ID from the database.
 * WHMCS does not always pass servergroupid in params when loading product module config.
 *
 * @param int $productId Product ID (tblproducts.id / pid)
 *
 * @return int|null Server group ID or null
 */
function rackflow_getProductServerGroupId($productId)
{
    if (empty($productId)) {
        return null;
    }
    try {
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
                ->where('id', (int)$productId)
                ->first();
            if ($product) {
                $groupId = isset($product->servergroupid) ? $product->servergroupid : (isset($product->serverGroupId) ? $product->serverGroupId : (isset($product->server_group_id) ? $product->server_group_id : null));
                if ($groupId !== null && $groupId !== '') {
                    return (int)$groupId;
                }
            }
        } elseif (function_exists('full_query')) {
            $result = full_query('SELECT servergroupid FROM tblproducts WHERE id = ' . (int)$productId);
            if ($result && mysql_num_rows($result) > 0) {
                $row = mysql_fetch_assoc($result);
                if (!empty($row['servergroupid'])) {
                    return (int)$row['servergroupid'];
                }
            }
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_getProductServerGroupId failed', array('pid' => $productId, 'error' => $e->getMessage()));
    }
    return null;
}

/**
 * Get the first server ID in a WHMCS server group.
 * When a product is assigned to a Server Group (WHMCS side), we use the first server in that group for API connection.
 *
 * @param int $serverGroupId WHMCS server group ID (tblservergroups.id)
 *
 * @return int|null First server ID or null if none found
 */
function rackflow_getFirstServerIdInGroup($serverGroupId)
{
    if (empty($serverGroupId)) {
        return null;
    }
    try {
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $server = \Illuminate\Database\Capsule\Manager::table('tblservers')
                ->where('groupid', (int)$serverGroupId)
                ->orderBy('id')
                ->first();
            if ($server && isset($server->id)) {
                rackflow_log('Using first server from WHMCS server group', array('servergroupid' => $serverGroupId, 'serverid' => $server->id));
                return (int)$server->id;
            }
        } elseif (function_exists('full_query')) {
            $result = full_query('SELECT id FROM tblservers WHERE groupid = ' . (int)$serverGroupId . ' ORDER BY id ASC LIMIT 1');
            if ($result && mysql_num_rows($result) > 0) {
                $row = mysql_fetch_assoc($result);
                return isset($row['id']) ? (int)$row['id'] : null;
            }
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_getFirstServerIdInGroup failed', array('servergroupid' => $serverGroupId, 'error' => $e->getMessage()));
    }
    return null;
}

/**
 * Fetch server groups from the RackFlow API.
 *
 * This function fetches available server groups from the backend.
 * Note: This requires admin API access (not billing API).
 *
 * @param array $params Module parameters with API configuration
 *
 * @return array|false Array of server groups or false on error
 */
function rackflow_getServerGroups($params = null)
{
    try {
        // If params not provided, return empty
        if (!$params) {
            return array();
        }
        
        // Get API configuration
        $apiConfig = rackflow_getApiConfig($params);
        $apiUrl = $apiConfig['url'];
        $apiKey = $apiConfig['key'];
        
        if (empty($apiUrl) || empty($apiKey)) {
            return array();
        }
        
        // Fetch server groups via billing API (same key as connection test)
        $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/server-groups', null);
        
        if ($result['success'] && isset($result['data']) && is_array($result['data'])) {
            return $result['data'];
        }
        
        return array();
        
    } catch (Exception $e) {
        logModuleCall(
            'rackflow',
            __FUNCTION__,
            $params ?: array(),
            $e->getMessage(),
            $e->getTraceAsString()
        );
        return false;
    }
}

/**
 * Loader function for RackFlow Server Group field.
 *
 * This function is called by WHMCS to populate the RackFlow Server Group dropdown
 * with available server groups from the backend API.
 *
 * @param array $params Module parameters with API configuration
 *
 * @return array Array of server groups as key-value pairs (id => display_name)
 * @throws Exception If connection fails or invalid response
 */
function rackflow_ServerGroupLoader($params)
{
    rackflow_log('ServerGroupLoader called', array('serverid' => isset($params['serverid']) ? $params['serverid'] : null));

    try {
        if (empty($params['serverid'])) {
            rackflow_log('ServerGroupLoader: no serverid, returning configure message', array());
            return array('' => '-- Configure server in Module Settings first --');
        }
        $serverId = (int)$params['serverid'];

        // Get server configuration from WHMCS database (IP and password/access hash from Setup > Servers)
        $serverConfig = rackflow_getServerConfigFromDB($serverId);
        if (!$serverConfig) {
            rackflow_log('ServerGroupLoader: server not found in WHMCS tblservers', array('serverid' => $serverId));
            return array('' => '-- Server not found in WHMCS --');
        }

        // Extract server connection details (prefer Access Hash, then Password)
        $hostname = !empty($serverConfig['ipaddress']) ? $serverConfig['ipaddress'] : ($serverConfig['hostname'] ?? '');
        $apiKey = trim($serverConfig['accesshash'] ?? $serverConfig['password'] ?? '');
        $port = isset($serverConfig['port']) ? $serverConfig['port'] : '8000';
        $useSSL = isset($serverConfig['secure']) ? (bool)$serverConfig['secure'] : false;

        if (empty($hostname)) {
            rackflow_log('ServerGroupLoader: server has no IP/hostname in Setup > Servers', array('serverid' => $serverId));
            return array('' => '-- Configure server IP/hostname in Setup > Servers --');
        }
        if (empty($apiKey)) {
            rackflow_log('ServerGroupLoader: server has no password or access hash in Setup > Servers', array('serverid' => $serverId));
            return array('' => '-- Configure server Password or Access Hash in Setup > Servers --');
        }

        // Build API URL
        $protocol = $useSSL ? 'https' : 'http';
        $hostname = preg_replace('#^https?://#', '', $hostname);
        $hostname = rtrim($hostname, '/');
        $apiUrl = $protocol . '://' . $hostname;
        if (!empty($port) && $port != '80' && $port != '443') {
            $apiUrl .= ':' . $port;
        }
        rackflow_log('ServerGroupLoader: calling RackFlow API for server groups', array('serverid' => $serverId, 'api_url' => $apiUrl));

        // Fetch server groups via billing API (same key as connection test)
        $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/server-groups', null);

        if (!$result['success']) {
            $httpCode = isset($result['http_code']) ? $result['http_code'] : 0;
            $errMsg = isset($result['error']) ? $result['error'] : 'Unknown error';
            rackflow_log('ServerGroupLoader: API call failed', array('error' => $errMsg, 'http_code' => $httpCode));
            if ($httpCode === 401) {
                return array('' => '-- Use RackFlow Billing Integration API key in Setup > Servers (Password/Access Hash) --');
            }
            throw new Exception('Failed to fetch server groups: ' . $errMsg);
        }
        if (!isset($result['data']) || !is_array($result['data'])) {
            rackflow_log('ServerGroupLoader: API response invalid (no data array)', array());
            throw new Exception('Invalid response format from server groups API');
        }

        $count = count($result['data']);
        rackflow_log('ServerGroupLoader: success, loaded ' . $count . ' server group(s)', array('serverid' => $serverId));

        // Format the list of values for display
        // ['id' => 'Display Name (ID: id)']
        $list = array();
        foreach ($result['data'] as $group) {
            if (isset($group['id']) && isset($group['name'])) {
                $groupId = (string)$group['id'];
                $groupName = $group['name'];
                $serverCount = isset($group['server_count']) ? $group['server_count'] : 0;
                
                // Format: "Group Name (ID: 1, Servers: 5)"
                $displayLabel = $groupName . ' (ID: ' . $groupId;
                if ($serverCount > 0) {
                    $displayLabel .= ', Servers: ' . $serverCount;
                }
                $displayLabel .= ')';
                
                $list[$groupId] = $displayLabel;
            }
        }
        
        // Add empty option at the beginning
        $list = array('' => '-- None --') + $list;
        
        return $list;
        
    } catch (Exception $e) {
        rackflow_log('ServerGroupLoader: exception', array('message' => $e->getMessage(), 'servergroupid' => isset($params['servergroupid']) ? $params['servergroupid'] : null));
        logModuleCall(
            'rackflow',
            __FUNCTION__,
            $params ?: array(),
            $e->getMessage(),
            $e->getTraceAsString()
        );
        throw $e; // Re-throw so WHMCS can display the error
    }
}

/**
 * Custom field name used to store the RackFlow service ID for WHMCS service mapping.
 * Create a Product Custom Field with this exact name for the RackFlow product.
 */
define('RACKFLOW_SERVICE_ID_FIELD_NAME', 'RackFlow Service ID');

/**
 * Get the RackFlow service ID linked to this WHMCS service (from product custom field).
 *
 * @param array $params Must contain serviceid and packageid (or pid).
 * @return int|null RackFlow service ID or null if not linked
 */
function rackflow_getRackflowServiceId(array $params)
{
    $serviceId = isset($params['serviceid']) ? (int)$params['serviceid'] : 0;
    $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);
    if (empty($serviceId) || empty($packageId)) {
        return null;
    }
    try {
        if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
            return null;
        }
        $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('relid', $packageId)
            ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
            ->first();
        if (!$field || !isset($field->id)) {
            return null;
        }
        $row = \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
            ->where('fieldid', (int)$field->id)
            ->where('relid', $serviceId)
            ->first();
        if ($row && isset($row->value) && $row->value !== '' && is_numeric(trim($row->value))) {
            return (int)trim($row->value);
        }
    } catch (Exception $e) {
        return null;
    }
    return null;
}

/**
 * Save the RackFlow service ID into the product custom field for a given WHMCS service.
 *
 * @param int $serviceId   WHMCS service ID (tblhosting.id)
 * @param int $productId   WHMCS product/package ID (tblproducts.id)
 * @param int $rackflowServiceId RackFlow service ID to store
 * @return bool True if saved, false on error
 */
function rackflow_saveServiceIdCustomField($serviceId, $productId, $rackflowServiceId)
{
    if (empty($serviceId) || empty($productId)) {
        return false;
    }
    try {
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $capsule = \Illuminate\Database\Capsule\Manager::getInstance();
            $field = $capsule->table('tblcustomfields')
                ->where('relid', (int)$productId)
                ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
                ->first();
            if (!$field || !isset($field->id)) {
                rackflow_log('rackflow_saveServiceIdCustomField: no custom field found', array(
                    'productid' => $productId,
                    'fieldname' => RACKFLOW_SERVICE_ID_FIELD_NAME,
                ));
                return false;
            }
            $fieldId = (int)$field->id;
            $existing = $capsule->table('tblcustomfieldsvalues')
                ->where('fieldid', $fieldId)
                ->where('relid', (int)$serviceId)
                ->first();
            if ($existing) {
                $capsule->table('tblcustomfieldsvalues')
                    ->where('fieldid', $fieldId)
                    ->where('relid', (int)$serviceId)
                    ->update(array('value' => (string)$rackflowServiceId));
            } else {
                $capsule->table('tblcustomfieldsvalues')->insert(array(
                    'fieldid' => $fieldId,
                    'relid' => (int)$serviceId,
                    'value' => (string)$rackflowServiceId,
                ));
            }
            rackflow_log('rackflow_saveServiceIdCustomField: saved', array(
                'serviceid' => $serviceId,
                'rackflow_service_id' => $rackflowServiceId,
            ));
            return true;
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_saveServiceIdCustomField failed', array(
            'error' => $e->getMessage(),
            'serviceid' => $serviceId,
            'productid' => $productId,
        ));
    }
    return false;
}

/**
 * Register an existing (already deployed) server in RackFlow and link it to this WHMCS service.
 * Uses the Dedicated IP field to find the server in RackFlow, creates a service + user in RackFlow,
 * and stores the RackFlow service ID in the product custom field "RackFlow Service ID".
 * Does not provision or install any OS.
 *
 * Call this when adding already deployed servers to WHMCS (e.g. from Module Command or admin action).
 *
 * @param array $params WHMCS module parameters (serviceid, userid, dedicatedip, serverid, packageid, etc.)
 * @return string "success" or an error message
 */
function rackflow_RegisterInRackflow(array $params)
{
    try {
        $apiConfig = rackflow_getApiConfig($params);
        $apiUrl = $apiConfig['url'];
        $apiKey = $apiConfig['key'];

        if (empty($apiUrl) || empty($apiKey)) {
            return 'API URL and API key must be configured (Setup > Servers).';
        }

        $serviceId = isset($params['serviceid']) ? (int)$params['serviceid'] : 0;
        $userId = isset($params['userid']) ? (int)$params['userid'] : 0;
        $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);

        if (empty($serviceId)) {
            return 'Service ID is missing.';
        }

        // Dedicated IP: prefer params (may be empty when WHMCS runs module command), then load from DB
        $dedicatedIp = isset($params['dedicatedip']) ? trim($params['dedicatedip']) : '';
        if (empty($dedicatedIp) && class_exists('\Illuminate\Database\Capsule\Manager')) {
            $hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')->where('id', $serviceId)->first();
            if ($hosting && isset($hosting->dedicatedip) && trim($hosting->dedicatedip) !== '') {
                $dedicatedIp = trim($hosting->dedicatedip);
            }
        }
        if (empty($dedicatedIp)) {
            return 'Dedicated IP is required. Set the Dedicated IP on this service and save, then run this command again.';
        }

        // Look up server in RackFlow by IP
        $lookup = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/server-by-ip?ip=' . urlencode($dedicatedIp), null);
        if (!$lookup['success']) {
            $err = isset($lookup['error']) ? $lookup['error'] : 'Unknown error';
            if (isset($lookup['http_code']) && $lookup['http_code'] == 404) {
                return 'No server in RackFlow with IP ' . $dedicatedIp . '. Add the server in RackFlow first.';
            }
            return 'RackFlow lookup failed: ' . $err;
        }
        $serverData = isset($lookup['data']) ? $lookup['data'] : array();
        $serverId = isset($serverData['id']) ? (int)$serverData['id'] : 0;
        if (empty($serverId)) {
            return 'RackFlow server-by-ip returned no server ID.';
        }

        // Get client details for external user
        $externalUsername = isset($params['clientdetails']['firstname']) || isset($params['clientdetails']['lastname'])
            ? trim((isset($params['clientdetails']['firstname']) ? $params['clientdetails']['firstname'] : '') . ' ' . (isset($params['clientdetails']['lastname']) ? $params['clientdetails']['lastname'] : ''))
            : (isset($params['username']) ? $params['username'] : '');
        $externalEmail = isset($params['clientdetails']['email']) ? $params['clientdetails']['email'] : (isset($params['email']) ? $params['email'] : '');

        $registerPayload = array(
            'server_id' => $serverId,
            'external_service_id' => (string)$serviceId,
            'external_user_id' => (string)$userId,
            'external_username' => $externalUsername ?: null,
            'external_email' => $externalEmail ?: null,
            'name' => 'service-' . $serviceId,
        );

        $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/register-service', $registerPayload);
        if (!$result['success']) {
            $err = isset($result['error']) ? $result['error'] : 'Unknown error';
            $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
            return 'Register service failed: ' . $detail;
        }

        $service = isset($result['data']) ? $result['data'] : array();
        $rackflowServiceId = isset($service['id']) ? (int)$service['id'] : 0;
        if (empty($rackflowServiceId)) {
            return 'RackFlow did not return a service ID.';
        }

        // Save RackFlow service ID to product custom field so we can map WHMCS service <-> RackFlow service
        if ($packageId) {
            rackflow_saveServiceIdCustomField($serviceId, $packageId, $rackflowServiceId);
        }

        rackflow_log('RegisterInRackflow success', array(
            'serviceid' => $serviceId,
            'rackflow_service_id' => $rackflowServiceId,
            'server_id' => $serverId,
        ));

        return 'success';
    } catch (Exception $e) {
        logModuleCall('rackflow', __FUNCTION__, $params, $e->getMessage(), $e->getTraceAsString());
        rackflow_log('RegisterInRackflow exception', array('message' => $e->getMessage()));
        return 'Error: ' . $e->getMessage();
    }
}

/**
 * Run a power action (on, off, reboot) via RackFlow billing API.
 * Used by PowerOn, PowerOff, Reboot custom functions.
 *
 * @param array  $params WHMCS module params (serviceid, packageid, serverid, etc.)
 * @param string $action One of: on, off, reboot, reset
 * @return string "success" or an error message
 */
function rackflow_powerAction(array $params, $action)
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return 'This service is not linked to RackFlow. Use Register in RackFlow first or set the RackFlow Service ID.';
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return 'API URL and API key must be configured (Setup > Servers).';
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowServiceId . '/power',
        array('action' => $action)
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return $detail;
    }
    return 'success';
}

/**
 * Custom function: Power On the server (admin and client).
 * @see https://developers.whmcs.com/provisioning-modules/custom-functions/
 *
 * @param array $params WHMCS module parameters
 * @return string "success" or an error message
 */
function rackflow_PowerOn(array $params)
{
    return rackflow_powerAction($params, 'on');
}

/**
 * Custom function: Power Off the server (admin and client).
 *
 * @param array $params WHMCS module parameters
 * @return string "success" or an error message
 */
function rackflow_PowerOff(array $params)
{
    return rackflow_powerAction($params, 'off');
}

/**
 * Custom function: Reboot the server (admin and client).
 *
 * @param array $params WHMCS module parameters
 * @return string "success" or an error message
 */
function rackflow_Reboot(array $params)
{
    return rackflow_powerAction($params, 'reboot');
}

/**
 * Fetch service status from RackFlow billing API (server status, power state, etc.).
 *
 * @param string $apiUrl        Base API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array|null Decoded response or null on failure
 */
function rackflow_fetchServiceStatus($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return null;
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/services/' . (int)$rackflowSvcId . '/status', null);
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        return null;
    }
    return $result['data'];
}

/**
 * Admin Services Tab: editable RackFlow Service ID and live server status from RackFlow.
 *
 * @param array $params serviceid, userid, packageid, serverid, etc.
 * @return array Extra fields for the admin service tab
 */
function rackflow_AdminServicesTabFields(array $params)
{
    $serviceId = isset($params['serviceid']) ? (int)$params['serviceid'] : 0;
    $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);
    if (empty($serviceId) || empty($packageId)) {
        return array();
    }
    $value = '';
    $customFieldExists = false;
    try {
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
                ->where('relid', $packageId)
                ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
                ->first();
            if ($field && isset($field->id)) {
                $customFieldExists = true;
                $row = \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
                    ->where('fieldid', (int)$field->id)
                    ->where('relid', $serviceId)
                    ->first();
                if ($row && isset($row->value)) {
                    $value = $row->value;
                }
            }
        }
    } catch (Exception $e) {
        $value = '';
    }

    $out = array();
    // Only show our "RackFlow Service ID" input when the product custom field does not exist yet.
    // Once it exists, WHMCS shows it automatically elsewhere — showing it here too would duplicate the field.
    if (!$customFieldExists) {
        $safeValue = htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
        $inputName = 'modulefields[rackflow_service_id]';
        $out['RackFlow Service ID'] = '<input type="text" name="' . $inputName . '" value="' . $safeValue . '" class="form-control" placeholder="e.g. 1 (from RackFlow)" style="max-width: 200px;" /> '
            . '<span class="text-muted">Set by &quot;Register in RackFlow&quot; or enter the ID and save (creates the field).</span>';
    }

    // Live server status from RackFlow when we have a linked service
    $statusHtml = '';
    $rackflowSvcId = trim($value);
    if ($rackflowSvcId !== '' && is_numeric($rackflowSvcId)) {
        $apiConfig = rackflow_getApiConfig($params);
        $statusData = rackflow_fetchServiceStatus($apiConfig['url'], $apiConfig['key'], (int)$rackflowSvcId);
        if ($statusData) {
            $serviceStatus = isset($statusData['service_status']) ? htmlspecialchars($statusData['service_status']) : '—';
            $serverName = isset($statusData['server_name']) ? htmlspecialchars($statusData['server_name']) : '—';
            $serverEnabled = isset($statusData['server_enabled']) ? (bool)$statusData['server_enabled'] : null;
            $powerState = isset($statusData['power_state']) ? strtolower($statusData['power_state']) : 'unknown';
            $statusLabel = isset($statusData['status']) ? $statusData['status'] : $powerState; // on / off / unknown / suspended
            $enabledText = $serverEnabled === true ? 'Yes' : ($serverEnabled === false ? 'No' : '—');
            $powerStyle = $statusLabel === 'on' ? 'background:#28a745;color:#fff;' : ($statusLabel === 'off' ? 'background:#6c757d;color:#fff;' : ($statusLabel === 'suspended' ? 'background:#ffc107;color:#212529;' : 'background:#6c757d;color:#fff;'));
            $statusHtml = '<div class="row"><div class="col-sm-12">'
                . '<table class="table table-condensed table-bordered" style="max-width: 500px;">'
                . '<tr><th style="width: 140px;">Service status</th><td>' . $serviceStatus . '</td></tr>'
                . '<tr><th>Server name</th><td>' . $serverName . '</td></tr>'
                . '<tr><th>Server enabled</th><td>' . $enabledText . '</td></tr>'
                . '<tr><th>Power state</th><td><span style="' . $powerStyle . 'padding:2px 8px;border-radius:4px;font-size:12px;">' . htmlspecialchars($statusLabel) . '</span></td></tr>'
                . '</table><p class="text-muted small">Live from RackFlow. Reload the page to refresh.</p></div></div>';
        } else {
            $statusHtml = '<span class="text-muted">Could not load status (check API and RackFlow service ID).</span>';
        }
    } else {
        $statusHtml = '<span class="text-muted">Link a RackFlow service above to see server status.</span>';
    }
    $out['RackFlow Server Status'] = $statusHtml;

    return $out;
}

/**
 * Save RackFlow Service ID from the admin service tab form.
 * Creates the product custom field if it does not exist.
 *
 * @param array $params serviceid, packageid, etc.
 */
function rackflow_AdminServicesTabFieldsSave(array $params)
{
    $serviceId = isset($params['serviceid']) ? (int)$params['serviceid'] : 0;
    $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);
    $newValue = isset($_POST['modulefields']['rackflow_service_id']) ? trim((string)$_POST['modulefields']['rackflow_service_id']) : '';
    if (empty($serviceId) || empty($packageId)) {
        return;
    }
    try {
        if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
            return;
        }
        $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('relid', $packageId)
            ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
            ->first();
        $weCreatedField = false;
        if (!$field || !isset($field->id)) {
            \Illuminate\Database\Capsule\Manager::table('tblcustomfields')->insert(array(
                'type' => 'product',
                'relid' => (int)$packageId,
                'fieldname' => RACKFLOW_SERVICE_ID_FIELD_NAME,
                'fieldtype' => 'text',
                'adminonly' => 'on',
            ));
            $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
                ->where('relid', $packageId)
                ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
                ->first();
            if (!$field || !isset($field->id)) {
                return;
            }
            $fieldId = (int)$field->id;
            $weCreatedField = true;
        } else {
            $fieldId = (int)$field->id;
        }
        // Ensure RackFlow Service ID is admin-only so it is not shown in client area
        \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('id', $fieldId)
            ->update(array('adminonly' => 'on'));
        // Only write the value when we rendered the input (field didn't exist before). When the field
        // already existed, WHMCS saves it from its own form — we must not overwrite with our empty POST key.
        if ($weCreatedField) {
            $existing = \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
                ->where('fieldid', $fieldId)
                ->where('relid', $serviceId)
                ->first();
            if ($existing) {
                \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
                    ->where('fieldid', $fieldId)
                    ->where('relid', $serviceId)
                    ->update(array('value' => $newValue));
            } else {
                \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')->insert(array(
                    'fieldid' => $fieldId,
                    'relid' => (int)$serviceId,
                    'value' => $newValue,
                ));
            }
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_AdminServicesTabFieldsSave failed', array(
            'error' => $e->getMessage(),
            'serviceid' => $serviceId,
            'packageid' => $packageId,
        ));
    }
}
