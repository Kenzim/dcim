<?php
/**
 * RackFlow Provisioning Module for WHMCS
 *
 * This module allows WHMCS to provision and manage servers through the RackFlow backend API.
 *
 * For linking already-deployed servers (no provisioning): use "Register in RackFlow" from the
 * admin service tab. That command creates/updates the product custom field "RackFlow Service ID"
 * and stores the RackFlow service mapping automatically.
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
    $redactKeys = array(
        'serverpassword',
        'serveraccesshash',
        'password',
        'accesshash',
        'api_key',
        'apiKey',
        'token',
        'access_token',
        'accesstoken',
        'git_token',
    );
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
 * Build newline-joined ip:port:user:pass lines from proxy assignment payloads.
 *
 * @param array $assignments
 * @return string
 */
function rackflow_proxyEndpointLines(array $assignments)
{
    $lines = array();
    foreach ($assignments as $assignment) {
        if (!is_array($assignment)) {
            continue;
        }
        if (!empty($assignment['endpoint'])) {
            $lines[] = (string)$assignment['endpoint'];
            continue;
        }
        $ip = isset($assignment['ip_address']) ? (string)$assignment['ip_address'] : '';
        $user = isset($assignment['username']) ? (string)$assignment['username'] : '';
        $pass = isset($assignment['password']) ? (string)$assignment['password'] : '';
        $port = isset($assignment['port']) && $assignment['port'] !== '' && $assignment['port'] !== null
            ? (string)$assignment['port']
            : '8080';
        if ($ip === '' || $user === '' || $pass === '') {
            continue;
        }
        $lines[] = $ip . ':' . $port . ':' . $user . ':' . $pass;
    }
    return implode("\n", $lines);
}

/**
 * Lightweight CSRF defense for the standalone AJAX action endpoints
 * (reinstall_action.php, backup_action.php, proxy_action.php, ip_action.php,
 * admin_link.php). These are session-cookie-authenticated but intentionally
 * skip WHMCS's check_token() (which tears down the session on failure and
 * isn't designed for JSON/fetch-based endpoints), so a cross-site
 * auto-submitted <form> could otherwise ride the victim's session.
 *
 * A plain HTML form submission (the classic CSRF vector) cannot set custom
 * request headers, and a cross-origin fetch/XHR that tries to add one
 * triggers a CORS preflight that WHMCS won't satisfy for an untrusted
 * origin -- so requiring this header on every state-changing request here
 * blocks both without needing a stateful per-request token.
 *
 * @return bool True if the request carries the expected AJAX header.
 */
function rackflow_isAjaxRequest()
{
    $header = isset($_SERVER['HTTP_X_REQUESTED_WITH']) ? (string)$_SERVER['HTTP_X_REQUESTED_WITH'] : '';
    return strtolower($header) === 'xmlhttprequest';
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
 * WHMCS does not pass $params here, so service type is resolved from the admin
 * service page request (or optional $params if a future WHMCS version supplies them).
 *
 * @param array $params optional module parameters when available
 * @return array Button label => custom function name (without module prefix)
 */
function rackflow_AdminCustomButtonArray(array $params = array())
{
    // Core buttons. Strategy handlers stay registered so modop=custom URLs work.
    $buttons = array(
        'Power On' => 'PowerOn',
        'Power Off' => 'PowerOff',
        'Reboot' => 'Reboot',
        'Change Guest Password' => 'ChangePassword',
        'Reset Guest Network' => 'ResetNetwork',
        'Randomize SMBIOS' => 'RandomizeSmbios',
        'Reinstall' => 'Reinstall',
        'Create VM Backup' => 'CreateBackup',
        'Delete VM Backup' => 'DeleteBackup',
        'Restore VM Backup' => 'RestoreBackup',
    );

    // Register links an existing RackFlow Server by Dedicated IP as bare_metal only.
    if (rackflow_getConfiguredServiceType($params) === 'bare_metal') {
        // Keep Register first in the Module Commands list.
        $buttons = array_merge(
            array('Register in RackFlow' => 'RegisterInRackflow'),
            $buttons
        );
    }

    return $buttons;
}

/**
 * Client Area custom buttons (shown to clients on the product details page).
 *
 * Only registers the power buttons when the client's effective RackFlow
 * permissions (resolved server-side from product/user/service permission
 * presets) grant the power action for this service. The billing API is the
 * hard gate (returns 403 regardless); this only controls whether WHMCS shows
 * the button at all. Defaults to showing the buttons when permissions can't
 * be determined (e.g. not yet linked to RackFlow) to avoid regressing
 * existing behavior for those cases.
 *
 * @see https://developers.whmcs.com/provisioning-modules/custom-functions/
 *
 * @param array $params WHMCS module parameters (serviceid, packageid, etc.)
 * @return array Button label => custom function name (without module prefix)
 */
function rackflow_ClientAreaCustomButtonArray(array $params = array())
{
    // Power / strategy buttons stay registered so modop=custom URLs work, but the
    // Service Details Actions sidebar is hidden for RackFlow (see hooks.php) and
    // the controls are rendered in clientarea.tpl instead.
    $buttons = array();
    $perms = rackflow_getClientPermissions($params);
    $powerAllowed = $perms === null ? true : !empty($perms['power_available']);

    if ($powerAllowed) {
        $buttons['Power On'] = 'PowerOn';
        $buttons['Power Off'] = 'PowerOff';
        $buttons['Reboot'] = 'Reboot';
    }

    // Strategy client actions from billing /status (client_actions list).
    // change_password is handled via the custom modal + WHMCS modulechangepassword
    // form (calls rackflow_ChangePassword; WHMCS persists the service password
    // only after the module returns "success"), not a sidebar button.
    $status = rackflow_getServiceStatusPayload($params);
    if (is_array($status) && !empty($status['client_actions']) && is_array($status['client_actions'])) {
        foreach ($status['client_actions'] as $action) {
            if (empty($action['name']) || empty($action['label'])) {
                continue;
            }
            $name = (string)$action['name'];
            if ($name === 'reset_network') {
                $buttons[$action['label']] = 'ResetNetwork';
            }
        }
    }

    // Backup actions stay registered so clientarea.tpl modop=custom URLs work
    // (sidebar Actions is hidden for RackFlow). Gate on backups_available from /status.
    if (is_array($status) && !empty($status['backups_available'])) {
        $buttons['Create VM Backup'] = 'CreateBackup';
        $buttons['Delete VM Backup'] = 'DeleteBackup';
        $buttons['Restore VM Backup'] = 'RestoreBackup';
    }

    return $buttons;
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
        'configoption8' => isset($vars['configoption8']) ? $vars['configoption8'] : null,
    );
    if (empty($params['pid']) && !empty($params['packageid'])) {
        $params['pid'] = $params['packageid'];
    }
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    $powerStatusLabel = '';
    $powerStatusStyle = '';
    $powerBadgeClass = 'rf-ca__badge--unknown';
    $serverName = '';
    $serviceStatus = '';
    $powerAvailable = false;
    $powerMessage = '';
    $installationStatusText = '';
    $ipmiAvailable = false;
    $ipmiViewerUsername = '';
    $ipmiViewerPassword = '';
    $vncAvailable = false;
    $kvmAvailable = false;
    $solAvailable = false;
    $virtualMediaAvailable = false;
    $virtualMediaInserted = false;
    $virtualMediaImage = '';
    $virtualMediaIsos = array();
    $changePasswordAllowed = false;
    $backupsAllowed = false;
    $backups = array();
    $backupJobs = array();
    $backupsError = '';
    $proxyCredentialsAvailable = false;
    $proxyRotateAvailable = false;
    $proxyAssignments = array();
    $serviceType = rackflow_getConfiguredServiceType($params);
    $isProxyService = ($serviceType === 'http_proxy');
    $perms = rackflow_getClientPermissions($params);
    $powerControlsAllowed = $perms === null ? true : !empty($perms['power_available']);
    // Product Module Setting (configoption8) can turn the button off entirely.
    // When status can't be fetched yet, default true — billing API remains the hard gate.
    $portalAllowed = rackflow_isPortalSignInEnabled(
        isset($params['configoption8']) ? $params['configoption8'] : null
    );
    $hostname = '';
    if (!empty($vars['domain'])) {
        $hostname = trim((string)$vars['domain']);
    } elseif (isset($vars['model']) && is_object($vars['model']) && !empty($vars['model']->domain)) {
        $hostname = trim((string)$vars['model']->domain);
    }
    $primaryIp = '';
    if (!empty($vars['dedicatedip'])) {
        $primaryIp = trim((string)$vars['dedicatedip']);
    } elseif (!empty($vars['serverip'])) {
        $primaryIp = trim((string)$vars['serverip']);
    }
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
            $powerBadgeClass = 'rf-ca__badge--unknown';
            $statusLower = strtolower((string)$statusLabel);
            if ($statusLower === 'on') {
                $powerBadgeClass = 'rf-ca__badge--on';
            } elseif ($statusLower === 'off') {
                $powerBadgeClass = 'rf-ca__badge--off';
            } elseif ($statusLower === 'suspended') {
                $powerBadgeClass = 'rf-ca__badge--warn';
            }
            $ipmiAvailable = !empty($statusData['ipmi_proxy_available']);
            // Always surface viewer credentials when the IPMI proxy is enabled.
            if ($ipmiAvailable) {
                $ipmiViewerUsername = isset($statusData['ipmi_viewer_username']) ? (string)$statusData['ipmi_viewer_username'] : '';
                $ipmiViewerPassword = isset($statusData['ipmi_viewer_password']) ? (string)$statusData['ipmi_viewer_password'] : '';
            }
            $vncAvailable = !empty($statusData['vnc_console_available']);
            $kvmAvailable = !empty($statusData['kvm_console_available']);
            $solAvailable = !empty($statusData['sol_console_available']);
            $virtualMediaAvailable = !empty($statusData['virtual_media_available']);
            if ($virtualMediaAvailable) {
                $media = rackflow_fetchVirtualMedia($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
                if ($media) {
                    $virtualMediaInserted = !empty($media['inserted']);
                    $virtualMediaImage = isset($media['image_name']) ? (string)$media['image_name'] : '';
                    $virtualMediaIsos = rackflow_isoRowsFromPayload(isset($media['isos']) ? $media['isos'] : array());
                }
                if (empty($virtualMediaIsos)) {
                    $virtualMediaIsos = rackflow_fetchBillingIsos($apiConfig['url'], $apiConfig['key']);
                }
            }
            $backupsAllowed = !empty($statusData['backups_available']);
            $clientPermissions = isset($statusData['client_permissions']) && is_array($statusData['client_permissions'])
                ? $statusData['client_permissions']
                : array();
            if (array_key_exists('service.portal', $clientPermissions)) {
                $portalAllowed = $portalAllowed && !empty($clientPermissions['service.portal']);
            }
            // Same visibility as WHMCS's built-in Change Password tab: available once
            // the service is linked. Billing API / strategy permissions remain the hard gate.
            // Meaningless for a server-less http_proxy (there is no root/guest
            // password to change), so hide it there.
            $changePasswordAllowed = !$isProxyService;
            $proxyCredentialsAvailable = !empty($statusData['proxy_credentials_available']);
            $proxyRotateAvailable = !empty($statusData['proxy_rotate_available']);
            if ($proxyCredentialsAvailable && isset($statusData['proxy_assignments']) && is_array($statusData['proxy_assignments'])) {
                $proxyAssignments = $statusData['proxy_assignments'];
            }
            if ($backupsAllowed) {
                $backupFetch = rackflow_fetchBackups($params, 'client');
                if (!empty($backupFetch['success'])) {
                    $backups = array();
                    foreach ($backupFetch['backups'] as $bRow) {
                        if (!is_array($bRow)) {
                            continue;
                        }
                        $rawNotes = isset($bRow['notes']) ? (string)$bRow['notes'] : '';
                        $bRow['notes_display'] = rackflow_backupNotesDisplay($rawNotes);
                        $bRow['kind_label'] = rackflow_backupKindLabel(isset($bRow['kind']) ? $bRow['kind'] : '');
                        $backups[] = $bRow;
                    }
                    $backupJobs = isset($backupFetch['jobs']) && is_array($backupFetch['jobs'])
                        ? $backupFetch['jobs']
                        : array();
                } else {
                    $backupsError = isset($backupFetch['error']) ? (string)$backupFetch['error'] : 'Unable to load backups.';
                }
            }
            // Optional installation status (OS install progress) from billing API
            if (isset($statusData['installation']) && is_array($statusData['installation'])) {
                $install = $statusData['installation'];
                $istatus = isset($install['status']) ? (string)$install['status'] : '';
                $iprogress = isset($install['progress_percent']) && $install['progress_percent'] !== null ? (int)$install['progress_percent'] : null;
                if ($istatus !== '') {
                    $installationStatusText = $istatus;
                    if ($iprogress !== null) {
                        $installationStatusText .= ' (' . $iprogress . '%)';
                    }
                }
            }
        } else {
            $powerMessage = 'Unable to load server status.';
        }
    } else {
        $powerMessage = 'Server status will appear here once the service is linked.';
    }
    return array(
        'templatefile' => 'clientarea',
        'vars' => array(
            'rackflow_hostname' => $hostname,
            'rackflow_primary_ip' => $primaryIp,
            'rackflow_power_available' => $powerAvailable,
            'rackflow_power_status_label' => $powerStatusLabel,
            'rackflow_power_status_style' => $powerStatusStyle,
            'rackflow_power_badge_class' => $powerBadgeClass,
            'rackflow_server_name' => $serverName,
            'rackflow_service_status' => $serviceStatus,
            'rackflow_installation_status' => $installationStatusText,
            'rackflow_power_message' => $powerMessage,
            'rackflow_ipmi_available' => $ipmiAvailable,
            // One-click: opens redirect endpoint in a new tab (mints ticket server-side).
            'rackflow_ipmi_open_url' => !empty($params['serviceid'])
                ? rackflow_ipmiOpenEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_ipmi_viewer_username' => $ipmiViewerUsername,
            'rackflow_ipmi_viewer_password' => $ipmiViewerPassword,
            'rackflow_vnc_available' => $vncAvailable,
            // One-click: opens redirect endpoint in a popup window (mints ticket server-side).
            'rackflow_vnc_open_url' => !empty($params['serviceid'])
                ? rackflow_vncOpenEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_kvm_available' => $kvmAvailable,
            'rackflow_kvm_open_url' => !empty($params['serviceid'])
                ? rackflow_kvmOpenEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_sol_available' => $solAvailable,
            'rackflow_sol_open_url' => !empty($params['serviceid'])
                ? rackflow_solOpenEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_virtual_media_available' => $virtualMediaAvailable && !empty($rackflowServiceId),
            'rackflow_virtual_media_inserted' => $virtualMediaInserted,
            'rackflow_virtual_media_image' => $virtualMediaImage,
            'rackflow_virtual_media_isos' => $virtualMediaIsos,
            'rackflow_virtual_media_action_url' => !empty($params['serviceid'])
                ? rackflow_virtualMediaActionEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            // One-click sign-in to the RackFlow client portal, scoped to this client's own
            // service. Gated by product Module Setting (configoption8) and the
            // `service.portal` client permission (UI gating only — the billing API's
            // portal-sso endpoint is the hard gate).
            'rackflow_portal_open_url' => !empty($rackflowServiceId) && !empty($params['serviceid']) && $portalAllowed
                ? rackflow_portalOpenEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_whmcs_service_id' => !empty($params['serviceid']) ? (int)$params['serviceid'] : 0,
            'rackflow_power_controls_allowed' => $powerControlsAllowed && !empty($rackflowServiceId),
            'rackflow_change_password_allowed' => $changePasswordAllowed && !empty($rackflowServiceId),
            'rackflow_power_on_url' => !empty($params['serviceid'])
                ? 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=PowerOn'
                : '',
            'rackflow_power_off_url' => !empty($params['serviceid'])
                ? 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=PowerOff'
                : '',
            'rackflow_reboot_url' => !empty($params['serviceid'])
                ? 'clientarea.php?action=productdetails&id=' . (int)$params['serviceid'] . '&modop=custom&a=Reboot'
                : '',
            'rackflow_backups_allowed' => $backupsAllowed && !empty($rackflowServiceId),
            'rackflow_backups' => $backups,
            'rackflow_backup_jobs' => $backupJobs,
            'rackflow_backups_error' => $backupsError,
            // AJAX endpoint (avoids modop=custom success redirects that drop Login-as-Owner sessions).
            'rackflow_backup_action_url' => !empty($params['serviceid'])
                ? rackflow_backupActionEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
            'rackflow_proxy_credentials_available' => $proxyCredentialsAvailable && !empty($rackflowServiceId),
            'rackflow_proxy_rotate_available' => $proxyRotateAvailable && !empty($rackflowServiceId),
            'rackflow_proxy_assignments' => $proxyAssignments,
            'rackflow_proxy_endpoint_lines' => rackflow_proxyEndpointLines($proxyAssignments),
            // AJAX endpoint (avoids modop=custom success redirects that drop Login-as-Owner sessions).
            'rackflow_proxy_action_url' => !empty($params['serviceid'])
                ? rackflow_proxyActionEndpointUrl((int)$params['serviceid'], isset($vars['systemurl']) ? (string)$vars['systemurl'] : '')
                : '',
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
    // Index order is fixed by WHMCS (configoption1..N). Keep CreateAccount /
    // resolve helpers / the admin Module Settings hook in sync when reordering.
    return array(
        // configoption1
        'Service Type' => array(
            'Type' => 'dropdown',
            'Options' => 'bare_metal,vm,http_proxy',
            'Default' => 'bare_metal',
            'SimpleMode' => true,
            'Description' => 'Controls which fields apply below and which RackFlow billing API is used (bare metal vs VM).',
        ),
        // configoption2 — maps to RackFlow catalog products.code
        'Product Code' => array(
            'Type' => 'dropdown',
            'Loader' => 'rackflow_ProductCodeLoader',
            'SimpleMode' => true,
            'Description' => 'RackFlow catalog product (products.code). Bare metal/VM: plans, OS profiles, and VM templates come from this product. HTTP proxy: IP count/subnet/allocation strategy defaults come from this product\'s http_proxy family — required to auto-assign IP(s) from IPAM on create.',
        ),
        // configoption3 — default OS template (bare metal) / unused for VM
        'OS Code' => array(
            'Type' => 'text',
            'Size' => '40',
            'SimpleMode' => true,
            'Description' => 'Bare metal only: optional default OS template id (from the server group) when the customer does not choose one. Leave blank for no default. Not used for VM or HTTP proxy.',
        ),
        // configoption4 — bare_metal (required) / http_proxy (legacy, optional)
        'RackFlow Server Group' => array(
            'Type' => 'text',
            'Size' => '25',
            'Loader' => 'rackflow_ServerGroupLoader',
            'SimpleMode' => true,
            'Description' => 'Bare metal: RackFlow server group to provision a physical server from (ISOs, scripts, OS templates). HTTP proxy: leave blank for the normal IPAM-based proxy (IP(s) auto-assigned from the Product Code\'s subnet); only set this for the legacy hardware-bound proxy flow. Not used for VM products.',
        ),
        // configoption5 — vm
        'Proxmox Location' => array(
            'Type' => 'dropdown',
            'Loader' => 'rackflow_ProxmoxClusterLoader',
            'SimpleMode' => true,
            'Description' => 'VM only: default Proxmox cluster / location (e.g. London). Overridden by configurable option proxmox_cluster_id or Location.',
        ),
        // configoption6 — vm
        'Proxmox Node' => array(
            'Type' => 'text',
            'Size' => '40',
            'SimpleMode' => true,
            'Description' => 'VM only: optional default node within the location (e.g. epyc). Leave blank to auto-pick.',
        ),
        // configoption7 — sync WHMCS Configurable Option "Operating System" on save
        'Customer OS Selection' => array(
            'Type' => 'yesno',
            'SimpleMode' => true,
            'Description' => 'When enabled, syncs an order-form "Operating System" configurable option from this RackFlow product (VM templates) or its server group (bare-metal OS templates).',
        ),
        // configoption8 — show "Open RackFlow portal" on the client product page
        'Allow Client Portal Sign-In' => array(
            'Type' => 'dropdown',
            'Options' => 'Yes,No',
            'Default' => 'Yes',
            'SimpleMode' => true,
            'Description' => 'When Yes, clients see a one-click button to sign into the RackFlow client portal for this service. Set to No to hide it (SSO endpoint is also blocked).',
        ),
    );
}

/**
 * Parse a WHMCS config value into a Proxmox cluster id.
 *
 * Accepts bare ids ("1"), pipe forms ("1|London" / "London|1"), or known
 * location aliases (london, epyc → 1).
 *
 * @param mixed $raw
 * @return int|null
 */
function rackflow_parseProxmoxClusterId($raw)
{
    if ($raw === null || $raw === '') {
        return null;
    }
    $value = trim((string)$raw);
    if ($value === '') {
        return null;
    }
    if (ctype_digit($value)) {
        return (int)$value;
    }
    if (strpos($value, '|') !== false) {
        foreach (explode('|', $value) as $part) {
            $part = trim($part);
            if ($part !== '' && ctype_digit($part)) {
                return (int)$part;
            }
        }
    }
    $aliases = array(
        'london' => 1,
        'epyc' => 1,
        'epyc home' => 1,
    );
    $key = strtolower($value);
    if (isset($aliases[$key])) {
        return (int)$aliases[$key];
    }
    return null;
}

/**
 * Resolve Proxmox cluster id: order-form configurable options override module setting.
 *
 * @param array $params
 * @return int|null
 */
function rackflow_resolveProxmoxClusterId(array $params)
{
    $cfg = isset($params['configoptions']) && is_array($params['configoptions'])
        ? $params['configoptions']
        : array();
    foreach (array('proxmox_cluster_id', 'Location', 'location') as $key) {
        if (isset($cfg[$key]) && $cfg[$key] !== '') {
            $parsed = rackflow_parseProxmoxClusterId($cfg[$key]);
            if ($parsed !== null) {
                return $parsed;
            }
        }
    }
    // Module Settings (ConfigOptions): configoption5 = Proxmox Location
    // (indices: 1=Service Type, 2=Product Code, 3=OS Code, 4=Server Group,
    //  5=Proxmox Location, 6=Proxmox Node)
    if (isset($params['configoption5']) && $params['configoption5'] !== '') {
        return rackflow_parseProxmoxClusterId($params['configoption5']);
    }
    return null;
}

/**
 * Resolve optional Proxmox node name (configurable option overrides module setting).
 *
 * @param array $params
 * @return string|null
 */
function rackflow_resolveProxmoxNodeName(array $params)
{
    $cfg = isset($params['configoptions']) && is_array($params['configoptions'])
        ? $params['configoptions']
        : array();
    foreach (array('proxmox_node_name', 'Node', 'node') as $key) {
        if (isset($cfg[$key]) && trim((string)$cfg[$key]) !== '') {
            return trim((string)$cfg[$key]);
        }
    }
    if (isset($params['configoption6']) && trim((string)$params['configoption6']) !== '') {
        return trim((string)$params['configoption6']);
    }
    return null;
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
        // Verify TLS: the RackFlow URL must present a valid certificate (use a
        // publicly-trusted cert or add your CA to the WHMCS host's CA bundle).
        curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, true);
        curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, 2);
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
 * @param int $timeoutSeconds CURL timeout in seconds (default 30)
 *
 * @return array Response data or error
 */
function rackflow_apiCall($apiUrl, $apiKey, $method, $endpoint, $data = null, $timeoutSeconds = 30)
{
    $method = strtoupper($method);
    $url = rtrim($apiUrl, '/') . $endpoint;
    $timeoutSeconds = (int)$timeoutSeconds;
    if ($timeoutSeconds < 1) {
        $timeoutSeconds = 30;
    }
    
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, $timeoutSeconds);
    // Verify TLS: the RackFlow URL must present a valid certificate (use a
    // publicly-trusted cert or add your CA to the WHMCS host's CA bundle).
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, true);
    curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, 2);
    
    $headers = array(
        'Authorization: Bearer ' . $apiKey,
        'Content-Type: application/json',
    );
    
    if ($method !== 'GET') {
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        if ($data !== null && in_array($method, array('POST', 'PUT', 'PATCH'))) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
        }
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
        
        // Module Settings (ConfigOptions order):
        //   configoption1 = Service Type (bare_metal|vm|http_proxy)
        //   configoption2 = Product Code (RackFlow catalog products.code)
        //   configoption3 = OS Code (default when no checkout selection)
        //   configoption4 = RackFlow Server Group ID (bare-metal / http_proxy)
        //   configoption5 = Proxmox Location (cluster id, VM)
        //   configoption6 = Proxmox Node (optional, VM)
        //   configoption7 = Customer OS Selection (yes/no → sync order-form option)
        //   configoption8 = Allow Client Portal Sign-In (Yes/No)
        // Named Configurable Options may still override for order-time choices.
        $serviceType = isset($params['configoption1']) && $params['configoption1'] !== ''
            ? (string)$params['configoption1']
            : 'bare_metal';
        $productCode = isset($params['configoption2']) && $params['configoption2'] !== ''
            ? (string)$params['configoption2']
            : null;
        $osCode = isset($params['configoption3']) && $params['configoption3'] !== ''
            ? (string)$params['configoption3']
            : null;
        $serverGroupId = isset($params['configoption4']) && !empty($params['configoption4'])
            ? (int)$params['configoption4']
            : null;
        $proxmoxClusterId = rackflow_resolveProxmoxClusterId($params);
        $proxmoxNodeName = rackflow_resolveProxmoxNodeName($params);

        // Configurable Options overrides (order form)
        if (isset($params['configoptions']['product_code']) && $params['configoptions']['product_code'] !== '') {
            $productCode = (string)$params['configoptions']['product_code'];
        }
        if (isset($params['configoptions']['service_type']) && $params['configoptions']['service_type'] !== '') {
            $serviceType = (string)$params['configoptions']['service_type'];
        }
        if (!$productCode) {
            $productCode = 'whmcs-product-' . (isset($params['packageid']) ? (int)$params['packageid'] : 0);
        }

        // Get Location ID and Plugin Name from server custom fields or use defaults
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
        
        // Optional OS template selection and parameters
        $templateId = isset($params['configoptions']['os_template']) && $params['configoptions']['os_template'] !== ''
            ? (string)$params['configoptions']['os_template']
            : null;

        // Template parameters: WHMCS service password → guest admin/client password
        $templateParameters = array();
        if (!empty($params['password'])) {
            $templateParameters['admin_password'] = (string)$params['password'];
        }
        $sshText = rackflow_sshKeysFromParams($params);
        if ($sshText !== '') {
            $parsedKeys = rackflow_parseSshPublicKeys($sshText);
            if (!$parsedKeys['ok']) {
                return array('error' => $parsedKeys['error'] ?: 'Invalid SSH public keys');
            }
            if (!empty($parsedKeys['keys'])) {
                $templateParameters['ssh_public_keys'] = $parsedKeys['keys'];
            }
        }

        $serviceConfig = array();
        if ($serverGroupId) {
            $serviceConfig['server_group_id'] = $serverGroupId;
        }

        // Checkout OS / VM template (order form) overrides module default OS Code.
        $vmTemplateId = null;
        if (isset($params['configoptions']['vm_template_id']) && $params['configoptions']['vm_template_id'] !== '') {
            $vmTemplateId = (int)$params['configoptions']['vm_template_id'];
        }
        $checkoutOs = rackflow_configOptionValue($params, array(
            'Operating System',
            'os_code',
            'OS',
            'os',
            'vm_template_id',
        ));
        $checkoutTemplateId = null;
        if ($checkoutOs !== null && $checkoutOs !== '') {
            $resolved = rackflow_resolveCheckoutOsSelection($params, $productCode, $checkoutOs);
            if (!empty($resolved['vm_template_id'])) {
                $vmTemplateId = (int)$resolved['vm_template_id'];
                // Template strategy owns the effective OS; don't also send os_code.
                $osCode = null;
            } elseif (!empty($resolved['template_id'])) {
                $checkoutTemplateId = (string)$resolved['template_id'];
                $osCode = null;
            } elseif (!empty($resolved['os_code'])) {
                $osCode = (string)$resolved['os_code'];
            }
        }

        // os_template configurable option is an explicit override; otherwise
        // checkout rfot: then Module Settings default OS template (configoption3).
        if (empty($templateId) && $checkoutTemplateId !== null && $checkoutTemplateId !== '') {
            $templateId = $checkoutTemplateId;
        }
        if (strtolower($serviceType) === 'bare_metal') {
            if (empty($templateId) && $osCode !== null && $osCode !== '') {
                $templateId = rackflow_bareMetalTemplateIdFromOsSetting($osCode);
            }
            $osCode = null;
        }
        if (!empty($templateId)) {
            $serviceConfig['template_id'] = $templateId;
        }
        if (!empty($templateParameters)) {
            $serviceConfig['template_parameters'] = $templateParameters;
        }

        $createPath = '/api/billing/bare-metal/services';
        $serviceData = array();

        if (strtolower($serviceType) === 'vm') {
            $createPath = '/api/billing/vm/services';
            $vmServiceConfig = array();
            if (!empty($templateId)) {
                $vmServiceConfig['template_id'] = $templateId;
            }
            if (!empty($templateParameters)) {
                $vmServiceConfig['template_parameters'] = $templateParameters;
            }
            $serviceData = array(
                'name' => $serviceName,
                'external_service_id' => isset($params['serviceid']) ? (string)$params['serviceid'] : null,
                'external_user_id' => $externalUserId,
                'external_username' => $externalUsername,
                'external_email' => $externalEmail,
                'product_code' => $productCode,
                'description' => isset($params['productname']) ? $params['productname'] : null,
                'service_config' => $vmServiceConfig,
            );
            if ($vmTemplateId !== null && $vmTemplateId > 0) {
                $serviceData['vm_template_id'] = $vmTemplateId;
            }
            if ($osCode !== null && $osCode !== '') {
                $serviceData['os_code'] = $osCode;
            }
            if ($proxmoxClusterId !== null && $proxmoxClusterId > 0) {
                $serviceData['proxmox_cluster_id'] = $proxmoxClusterId;
            }
            if ($proxmoxNodeName !== null && $proxmoxNodeName !== '') {
                $serviceData['proxmox_node_name'] = $proxmoxNodeName;
            }
            if (isset($params['configoptions']['proxmox_vmid']) && $params['configoptions']['proxmox_vmid'] !== '') {
                $serviceData['proxmox_vmid'] = (int)$params['configoptions']['proxmox_vmid'];
            }
            // Auto-provision (default on): RackFlow places, reserves a VMID, and provisions the
            // guest in the background. Add an "auto_provision" config option set to No/0/off to
            // instead create a pending VM for the manual two-phase flow.
            $autoProvision = true;
            if (isset($params['configoptions']['auto_provision']) && $params['configoptions']['auto_provision'] !== '') {
                $apVal = strtolower((string)$params['configoptions']['auto_provision']);
                $autoProvision = !in_array($apVal, array('0', 'no', 'off', 'false'), true);
            }
            $serviceData['auto_provision'] = $autoProvision;
        } else {
            $serviceData = array(
                'name' => $serviceName,
                'external_service_id' => isset($params['serviceid']) ? (string)$params['serviceid'] : null,
                'external_user_id' => $externalUserId,
                'external_username' => $externalUsername,
                'external_email' => $externalEmail,
                'product_code' => $productCode,
                'os_code' => $osCode,
                'service_type' => $serviceType,
                'server_name' => $serverName,
                'server_ip' => $serverIp ?: '0.0.0.0',
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
                'service_config' => $serviceConfig,
            );
        }

        $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', $createPath, $serviceData);
        
        if (!$result['success']) {
            return 'Error creating service: ' . (isset($result['error']) ? $result['error'] : 'Unknown error');
        }
        
        $service = $result['data'];
        
        // If server group is specified, assign server to group
        if ($serverGroupId && isset($service['server_id']) && $service['server_id'] !== null) {
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
        
        // Store RackFlow service ID in custom field for future reference
        if (isset($params['serviceid']) && isset($params['packageid']) && isset($service['id'])) {
            $rackflowServiceId = (int)$service['id'];
            rackflow_saveServiceIdCustomField((int)$params['serviceid'], (int)$params['packageid'], $rackflowServiceId);
        }

        // Persist assigned VM IP into WHMCS dedicated IP when present
        if (strtolower($serviceType) === 'vm' && !empty($service['vm_ip_address']) && !empty($params['serviceid'])) {
            try {
                $dedicatedIp = (string)$service['vm_ip_address'];
                if (function_exists('localAPI')) {
                    localAPI('UpdateClientProduct', array(
                        'serviceid' => (int)$params['serviceid'],
                        'dedicatedip' => $dedicatedIp,
                    ));
                }
            } catch (Exception $ipEx) {
                logModuleCall('rackflow', __FUNCTION__ . '_dedicatedip', $service, $ipEx->getMessage(), '');
            }
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
 * Suspend a service in RackFlow when WHMCS suspends the account.
 *
 * RackFlow's billing suspend endpoint force-powers off the linked BM/VM
 * (when applicable) and then marks the service as suspended.
 *
 * @param array $params common module parameters
 *
 * @return string "success" or an error message
 */
function rackflow_SuspendAccount(array $params)
{
    try {
        $rackflowServiceId = rackflow_getRackflowServiceId($params);
        if (empty($rackflowServiceId)) {
            return 'This service is not linked to RackFlow. Use Register in RackFlow first or set the RackFlow Service ID.';
        }

        $apiConfig = rackflow_getApiConfig($params);
        if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
            return 'API URL and API key must be configured (Setup > Servers).';
        }

        $reason = isset($params['suspendreason']) && $params['suspendreason'] !== ''
            ? (string)$params['suspendreason']
            : 'Suspended from WHMCS';

        $result = rackflow_apiCall(
            $apiConfig['url'],
            $apiConfig['key'],
            'POST',
            '/api/billing/services/' . (int)$rackflowServiceId . '/suspend',
            array('reason' => $reason)
        );

        if (!$result['success']) {
            $err = isset($result['error']) ? $result['error'] : 'Unknown error';
            $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
            return 'Suspend failed: ' . $detail;
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
 * Unsuspend a service in RackFlow when WHMCS unsuspends the account.
 *
 * This will mark the service as active again.
 * Powering the server on remains a manual action via the Power On command.
 *
 * @param array $params common module parameters
 *
 * @return string "success" or an error message
 */
function rackflow_UnsuspendAccount(array $params)
{
    try {
        $rackflowServiceId = rackflow_getRackflowServiceId($params);
        if (empty($rackflowServiceId)) {
            return 'This service is not linked to RackFlow. Use Register in RackFlow first or set the RackFlow Service ID.';
        }

        $apiConfig = rackflow_getApiConfig($params);
        if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
            return 'API URL and API key must be configured (Setup > Servers).';
        }

        $reason = isset($params['unsuspendreason']) && $params['unsuspendreason'] !== ''
            ? (string)$params['unsuspendreason']
            : 'Unsuspended from WHMCS';

        $result = rackflow_apiCall(
            $apiConfig['url'],
            $apiConfig['key'],
            'POST',
            '/api/billing/services/' . (int)$rackflowServiceId . '/unsuspend',
            array('reason' => $reason)
        );

        if (!$result['success']) {
            $err = isset($result['error']) ? $result['error'] : 'Unknown error';
            $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
            return 'Unsuspend failed: ' . $detail;
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
 * Terminate a service in RackFlow when WHMCS terminates the account.
 *
 * This will power the server off and then terminate the RackFlow service,
 * while leaving server-level administrative enablement unchanged.
 *
 * @param array $params common module parameters
 *
 * @return string "success" or an error message
 */
function rackflow_TerminateAccount(array $params)
{
    try {
        $rackflowServiceId = rackflow_getRackflowServiceId($params);
        if (empty($rackflowServiceId)) {
            return 'This service is not linked to RackFlow. Use Register in RackFlow first or set the RackFlow Service ID.';
        }

        $apiConfig = rackflow_getApiConfig($params);
        if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
            return 'API URL and API key must be configured (Setup > Servers).';
        }

        // HTTP-proxy services are provisioned straight from the IP pool with
        // no linked rack Server/VM to power off; the billing API release of
        // IPAM assignments below is the only teardown they need.
        $serviceType = rackflow_getConfiguredServiceType($params);
        if ($serviceType !== 'http_proxy') {
            // First ensure the server is powered off
            $powerResult = rackflow_powerAction($params, 'off');
            if ($powerResult !== 'success') {
                return 'Failed to power off server before termination: ' . $powerResult;
            }
        }

        $result = rackflow_apiCall(
            $apiConfig['url'],
            $apiConfig['key'],
            'DELETE',
            '/api/billing/services/' . (int)$rackflowServiceId,
            null
        );

        if (!$result['success']) {
            $err = isset($result['error']) ? $result['error'] : 'Unknown error';
            $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
            return 'Terminate failed: ' . $detail;
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
        // Fallback to legacy full_query function. The old `mysql_query()`
        // last-resort branch that used to live here was removed: that function
        // was dropped in PHP 7 (WHMCS's minimum supported PHP version), so it
        // was unreachable dead code (int-cast on the interpolated ID anyway).
        elseif (function_exists('full_query')) {
            $result = full_query("SELECT * FROM tblservers WHERE id = " . (int)$serverId);
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
 * Resolve product Service Type (configoption1): bare_metal|vm|http_proxy.
 *
 * Prefers $params['configoption1']; otherwise loads the product for the current
 * admin service (clientsservices.php?id=…) or $params['serviceid'] / packageid.
 * Defaults to bare_metal when unknown (matches ConfigOptions default).
 *
 * @param array $params optional WHMCS module parameters
 * @return string lowercase service type
 */
function rackflow_getConfiguredServiceType(array $params = array())
{
    if (isset($params['configoption1']) && trim((string)$params['configoption1']) !== '') {
        return strtolower(trim((string)$params['configoption1']));
    }
    if (isset($params['configoptions']['service_type']) && trim((string)$params['configoptions']['service_type']) !== '') {
        return strtolower(trim((string)$params['configoptions']['service_type']));
    }

    $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);
    $serviceId = isset($params['serviceid']) ? (int)$params['serviceid'] : 0;
    if ($serviceId <= 0 && !empty($_REQUEST['id'])) {
        $serviceId = (int)$_REQUEST['id'];
    }

    try {
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            if ($packageId <= 0 && $serviceId > 0) {
                $hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
                    ->where('id', $serviceId)
                    ->first();
                if ($hosting && isset($hosting->packageid)) {
                    $packageId = (int)$hosting->packageid;
                }
            }
            if ($packageId > 0) {
                $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
                    ->where('id', $packageId)
                    ->first();
                if ($product && isset($product->configoption1) && trim((string)$product->configoption1) !== '') {
                    return strtolower(trim((string)$product->configoption1));
                }
            }
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_getConfiguredServiceType failed', array(
            'serviceid' => $serviceId,
            'packageid' => $packageId,
            'error' => $e->getMessage(),
        ));
    }

    return 'bare_metal';
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
                // WHMCS column is servergroup (not servergroupid).
                foreach (array('servergroup', 'servergroupid', 'serverGroupId', 'server_group_id') as $field) {
                    if (isset($product->{$field}) && $product->{$field} !== '' && $product->{$field} !== null) {
                        return (int)$product->{$field};
                    }
                }
            }
        } elseif (function_exists('full_query')) {
            $result = full_query('SELECT servergroup FROM tblproducts WHERE id = ' . (int)$productId);
            if ($result && mysql_num_rows($result) > 0) {
                $row = mysql_fetch_assoc($result);
                if (!empty($row['servergroup'])) {
                    return (int)$row['servergroup'];
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
            // Modern WHMCS: servers are linked via tblservergroupsrel.
            $rel = \Illuminate\Database\Capsule\Manager::table('tblservergroupsrel')
                ->where('groupid', (int)$serverGroupId)
                ->orderBy('serverid')
                ->first();
            if ($rel && isset($rel->serverid)) {
                rackflow_log('Using first server from WHMCS server group', array(
                    'servergroupid' => $serverGroupId,
                    'serverid' => $rel->serverid,
                ));
                return (int)$rel->serverid;
            }
            // Legacy fallback if groupid still exists on tblservers.
            $server = \Illuminate\Database\Capsule\Manager::table('tblservers')
                ->where('groupid', (int)$serverGroupId)
                ->orderBy('id')
                ->first();
            if ($server && isset($server->id)) {
                return (int)$server->id;
            }
        } elseif (function_exists('full_query')) {
            $result = full_query(
                'SELECT serverid FROM tblservergroupsrel WHERE groupid = '
                . (int)$serverGroupId . ' ORDER BY serverid ASC LIMIT 1'
            );
            if ($result && mysql_num_rows($result) > 0) {
                $row = mysql_fetch_assoc($result);
                return isset($row['serverid']) ? (int)$row['serverid'] : null;
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
 * Resolved OS templates for a RackFlow server group id.
 *
 * @param array $params
 * @param int|string|null $serverGroupId
 * @return array
 */
function rackflow_osTemplatesForServerGroupId(array $params, $serverGroupId = null)
{
    if ($serverGroupId === null || $serverGroupId === '') {
        if (isset($params['configoption4']) && $params['configoption4'] !== '') {
            $serverGroupId = $params['configoption4'];
        }
    }
    $serverGroupId = trim((string)$serverGroupId);
    if ($serverGroupId === '') {
        return array();
    }
    $groups = rackflow_getServerGroups($params);
    if (!is_array($groups)) {
        return array();
    }
    foreach ($groups as $group) {
        if (!isset($group['id'])) {
            continue;
        }
        if ((string)$group['id'] !== $serverGroupId) {
            continue;
        }
        if (!empty($group['os_templates']) && is_array($group['os_templates'])) {
            return $group['os_templates'];
        }
        return array();
    }
    return array();
}

/**
 * Read the first non-empty configurable-option value for any of the given keys.
 *
 * @param array $params
 * @param array $keys
 * @return string|null
 */
function rackflow_configOptionValue(array $params, array $keys)
{
    $cfg = isset($params['configoptions']) && is_array($params['configoptions'])
        ? $params['configoptions']
        : array();
    foreach ($keys as $key) {
        if (isset($cfg[$key]) && $cfg[$key] !== '' && $cfg[$key] !== null) {
            return (string)$cfg[$key];
        }
    }
    return null;
}

/**
 * Resolve the WHMCS server id used for RackFlow API calls on a product.
 *
 * @param int|null $productId tblproducts.id
 * @param array $params optional module params (may already include serverid)
 * @return int|null
 */
function rackflow_resolveModuleServerId($productId = null, array $params = array())
{
    if (!empty($params['serverid'])) {
        return (int)$params['serverid'];
    }
    $groupId = null;
    if (!empty($params['servergroupid'])) {
        $groupId = (int)$params['servergroupid'];
    } elseif (!empty($productId)) {
        $groupId = rackflow_getProductServerGroupId((int)$productId);
    }
    if ($groupId) {
        return rackflow_getFirstServerIdInGroup($groupId);
    }
    return null;
}

/**
 * Fetch RackFlow catalog products via the billing API.
 *
 * @param array $params must resolve to API URL/key (serverid recommended)
 * @param string|null $serviceType optional filter: bare_metal|vm|http_proxy
 * @return array list of product payloads (empty on failure)
 */
function rackflow_fetchCatalogProducts(array $params, $serviceType = null)
{
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return array();
    }
    $endpoint = '/api/billing/products';
    if ($serviceType !== null && $serviceType !== '') {
        $endpoint .= '?service_type=' . rawurlencode((string)$serviceType);
    }
    $result = rackflow_apiCall($apiConfig['url'], $apiConfig['key'], 'GET', $endpoint, null);
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        rackflow_log('rackflow_fetchCatalogProducts failed', array(
            'error' => isset($result['error']) ? $result['error'] : 'unknown',
            'http_code' => isset($result['http_code']) ? $result['http_code'] : 0,
        ));
        return array();
    }
    return $result['data'];
}

/**
 * Whether Customer OS Selection (configoption7) is enabled.
 *
 * @param mixed $raw
 * @return bool
 */
function rackflow_isCustomerOsSelectionEnabled($raw)
{
    if ($raw === true || $raw === 1 || $raw === '1') {
        return true;
    }
    $val = strtolower(trim((string)$raw));
    return in_array($val, array('on', 'yes', 'true'), true);
}

/**
 * Whether clients may use one-click RackFlow portal sign-in (configoption8).
 *
 * Empty/missing defaults to enabled so existing products keep the button after
 * upgrade. Explicit "No" / "off" disables both the client-area button and the
 * portal_open.php launcher.
 *
 * @param mixed $raw
 * @return bool
 */
function rackflow_isPortalSignInEnabled($raw)
{
    if ($raw === null || $raw === '') {
        return true;
    }
    if ($raw === true || $raw === 1 || $raw === '1') {
        return true;
    }
    if ($raw === false || $raw === 0 || $raw === '0') {
        return false;
    }
    $val = strtolower(trim((string)$raw));
    if (in_array($val, array('no', 'off', 'false', 'disabled'), true)) {
        return false;
    }
    return in_array($val, array('on', 'yes', 'true'), true);
}

/**
 * Ensure a configurable-option sub-choice has zero pricing for every currency.
 *
 * WHMCS omits choices from the order form when no tblpricing row exists.
 *
 * @param int $subOptionId tblproductconfigoptionssub.id
 * @return void
 */
function rackflow_ensureConfigOptionSubPricing($subOptionId)
{
    $subOptionId = (int)$subOptionId;
    if ($subOptionId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $currencies = $capsule::table('tblcurrencies')->pluck('id');
    foreach ($currencies as $currencyId) {
        $currencyId = (int)$currencyId;
        $exists = $capsule::table('tblpricing')
            ->where('type', 'configoptions')
            ->where('currency', $currencyId)
            ->where('relid', $subOptionId)
            ->first();
        if ($exists) {
            continue;
        }
        $capsule::table('tblpricing')->insert(array(
            'type' => 'configoptions',
            'currency' => $currencyId,
            'relid' => $subOptionId,
            'msetupfee' => '0.00',
            'qsetupfee' => '0.00',
            'ssetupfee' => '0.00',
            'asetupfee' => '0.00',
            'bsetupfee' => '0.00',
            'tsetupfee' => '0.00',
            'monthly' => '0.00',
            'quarterly' => '0.00',
            'semiannually' => '0.00',
            'annually' => '0.00',
            'biennially' => '0.00',
            'triennially' => '0.00',
        ));
    }
}

/**
 * Sync / hide the order-form "OS" configurable option for a product.
 *
 * Sub-option names are customer-facing labels. CreateAccount resolves them back
 * to a VM template id, OS template id, or OS profile code via the RackFlow
 * catalog (see rackflow_resolveCheckoutOsSelection). Machine tokens rfvt:{id} /
 * rfot:{template_id} / rfos:{code} are also accepted if present.
 *
 * @param int $productId WHMCS tblproducts.id
 * @param array $catalogProduct one item from /api/billing/products
 * @param bool $enabled
 * @param array $groupOsTemplates resolved os_templates from the product's server group
 * @return bool
 */
function rackflow_syncCheckoutOsOption($productId, array $catalogProduct, $enabled, $groupOsTemplates = array())
{
    $productId = (int)$productId;
    if ($productId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }

    $capsule = '\Illuminate\Database\Capsule\Manager';
    $groupName = 'RackFlow Operating System';
    $optionName = 'OS';

    try {
        $group = $capsule::table('tblproductconfiggroups')->where('name', $groupName)->first();
        if (!$group) {
            $groupId = (int)$capsule::table('tblproductconfiggroups')->insertGetId(array(
                'name' => $groupName,
                'description' => 'Auto-synced by the RackFlow module from catalog OS / VM templates.',
            ));
        } else {
            $groupId = (int)$group->id;
        }

        $link = $capsule::table('tblproductconfiglinks')
            ->where('gid', $groupId)
            ->where('pid', $productId)
            ->first();
        if (!$link) {
            $capsule::table('tblproductconfiglinks')->insert(array(
                'gid' => $groupId,
                'pid' => $productId,
            ));
        }

        // Drop stray OS-related config-group links left behind by product clones
        // (WHMCS merges all linked groups at order time and can pick the wrong OS).
        rackflow_unlinkStrayOsConfigGroups($productId, $groupId);

        // Prefer "OS"; migrate legacy "Operating System" rows created by older syncs.
        $option = $capsule::table('tblproductconfigoptions')
            ->where('gid', $groupId)
            ->where('optionname', $optionName)
            ->first();
        if (!$option) {
            $option = $capsule::table('tblproductconfigoptions')
                ->where('gid', $groupId)
                ->where('optionname', 'Operating System')
                ->first();
        }
        if (!$option) {
            $optionId = (int)$capsule::table('tblproductconfigoptions')->insertGetId(array(
                'gid' => $groupId,
                'optionname' => $optionName,
                'optiontype' => 1, // dropdown
                'qtyminimum' => 0,
                'qtymaximum' => 0,
                'order' => 0,
                'hidden' => $enabled ? 0 : 1,
            ));
        } else {
            $optionId = (int)$option->id;
            $capsule::table('tblproductconfigoptions')
                ->where('id', $optionId)
                ->update(array(
                    'optionname' => $optionName,
                    'hidden' => $enabled ? 0 : 1,
                    'optiontype' => 1,
                ));
        }

        // Hide leftover OS option rows in any remaining non-canonical groups.
        $otherGroupIds = $capsule::table('tblproductconfiglinks')
            ->where('pid', $productId)
            ->where('gid', '!=', $groupId)
            ->pluck('gid');
        if (!empty($otherGroupIds)) {
            $capsule::table('tblproductconfigoptions')
                ->whereIn('gid', $otherGroupIds)
                ->whereIn('optionname', array('vm_template_id', 'OS', 'Operating System'))
                ->update(array('hidden' => 1));
        }

        if (!$enabled) {
            rackflow_log('Checkout OS option hidden', array('pid' => $productId, 'option_id' => $optionId));
            return true;
        }

        $choices = array();
        $mode = isset($catalogProduct['checkout_os_mode']) ? $catalogProduct['checkout_os_mode'] : 'none';
        if ($mode === 'vm_template' && !empty($catalogProduct['vm_templates']) && is_array($catalogProduct['vm_templates'])) {
            foreach ($catalogProduct['vm_templates'] as $tmpl) {
                if (empty($tmpl['id'])) {
                    continue;
                }
                // Token first so CreateAccount can parse even if the label is edited later.
                $label = !empty($tmpl['name']) ? (string)$tmpl['name'] : ('Template #' . $tmpl['id']);
                $choices[] = 'rfvt:' . (int)$tmpl['id'] . '|' . $label;
            }
        } elseif ($mode === 'server_group' && !empty($groupOsTemplates) && is_array($groupOsTemplates)) {
            foreach ($groupOsTemplates as $tmpl) {
                if (empty($tmpl['id'])) {
                    continue;
                }
                $label = !empty($tmpl['name']) ? (string)$tmpl['name'] : (string)$tmpl['id'];
                $choices[] = 'rfot:' . (string)$tmpl['id'] . '|' . $label;
            }
        } elseif (!empty($catalogProduct['os_profiles']) && is_array($catalogProduct['os_profiles'])) {
            foreach ($catalogProduct['os_profiles'] as $profile) {
                if (empty($profile['code'])) {
                    continue;
                }
                $label = !empty($profile['name']) ? (string)$profile['name'] : (string)$profile['code'];
                $choices[] = 'rfos:' . (string)$profile['code'] . '|' . $label;
            }
        }

        if (!$choices) {
            $capsule::table('tblproductconfigoptions')
                ->where('id', $optionId)
                ->update(array('hidden' => 1));
            rackflow_log('Checkout OS option hidden (no choices)', array('pid' => $productId));
            return true;
        }

        $existing = $capsule::table('tblproductconfigoptionssub')
            ->where('configid', $optionId)
            ->get();
        $byName = array();
        foreach ($existing as $row) {
            $byName[(string)$row->optionname] = $row;
        }

        $keep = array();
        $sort = 0;
        foreach ($choices as $optionname) {
            $keep[$optionname] = true;
            $sort++;
            if (isset($byName[$optionname])) {
                $subId = (int)$byName[$optionname]->id;
                $capsule::table('tblproductconfigoptionssub')
                    ->where('id', $subId)
                    ->update(array('sortorder' => $sort, 'hidden' => 0));
            } else {
                $subId = (int)$capsule::table('tblproductconfigoptionssub')->insertGetId(array(
                    'configid' => $optionId,
                    'optionname' => $optionname,
                    'sortorder' => $sort,
                    'hidden' => 0,
                ));
            }
            rackflow_ensureConfigOptionSubPricing($subId);
        }

        foreach ($byName as $name => $row) {
            if (!isset($keep[$name])) {
                $capsule::table('tblproductconfigoptionssub')
                    ->where('id', (int)$row->id)
                    ->update(array('hidden' => 1));
            }
        }

        rackflow_log('Checkout OS option synced', array(
            'pid' => $productId,
            'option_id' => $optionId,
            'choices' => count($choices),
            'mode' => $mode,
        ));
        return true;
    } catch (Exception $e) {
        rackflow_log('rackflow_syncCheckoutOsOption failed', array(
            'pid' => $productId,
            'error' => $e->getMessage(),
        ));
        return false;
    }
}

/**
 * Remove product config-group links that look like legacy/duplicate OS groups.
 *
 * Keeps the canonical RackFlow OS group ($canonicalGroupId). A linked group is
 * considered stray when its name matches RackFlow or Operating System patterns
 * or it contains an OS / Operating System / vm_template_id option.
 *
 * @param int $productId
 * @param int $canonicalGroupId
 * @return int Number of links removed
 */
function rackflow_unlinkStrayOsConfigGroups($productId, $canonicalGroupId)
{
    $productId = (int)$productId;
    $canonicalGroupId = (int)$canonicalGroupId;
    if ($productId <= 0 || $canonicalGroupId <= 0) {
        return 0;
    }
    if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
        return 0;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $removed = 0;
    try {
        $links = $capsule::table('tblproductconfiglinks')
            ->where('pid', $productId)
            ->where('gid', '!=', $canonicalGroupId)
            ->get();
        foreach ($links as $link) {
            $gid = (int)$link->gid;
            $group = $capsule::table('tblproductconfiggroups')->where('id', $gid)->first();
            $name = $group && isset($group->name) ? (string)$group->name : '';
            $nameLooksOs = (
                stripos($name, 'RackFlow') === 0
                || stripos($name, 'Operating System') !== false
                || preg_match('/\\bOS\\b/i', $name)
            );
            $hasOsOption = $capsule::table('tblproductconfigoptions')
                ->where('gid', $gid)
                ->whereIn('optionname', array('OS', 'Operating System', 'vm_template_id'))
                ->exists();
            if (!$nameLooksOs && !$hasOsOption) {
                continue;
            }
            $capsule::table('tblproductconfiglinks')
                ->where('pid', $productId)
                ->where('gid', $gid)
                ->delete();
            $removed++;
            rackflow_log('Unlinked stray OS config group', array(
                'pid' => $productId,
                'gid' => $gid,
                'group_name' => $name,
            ));
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_unlinkStrayOsConfigGroups failed', array(
            'pid' => $productId,
            'error' => $e->getMessage(),
        ));
    }
    return $removed;
}

/**
 * Strip an rfot: prefix from a Module Settings default OS value.
 *
 * @param string $raw
 * @return string
 */
function rackflow_bareMetalTemplateIdFromOsSetting($raw)
{
    $value = trim((string)$raw);
    if ($value === '') {
        return '';
    }
    if (strpos($value, '|') !== false) {
        $value = trim(explode('|', $value, 2)[0]);
    }
    if (preg_match('/^rfot:(.+)$/', $value, $m)) {
        return trim($m[1]);
    }
    return $value;
}

/**
 * Resolve a checkout OS selection into vm_template_id, template_id, and/or os_code.
 *
 * Accepts machine tokens (rfvt:/rfot:/rfos:), bare ids/codes, "token|Label" WHMCS
 * pipe forms, or friendly labels matched against the catalog product.
 *
 * @param array $params
 * @param string $productCode
 * @param string $selected
 * @return array{vm_template_id:?int,os_code:?string,template_id:?string}
 */
function rackflow_resolveCheckoutOsSelection(array $params, $productCode, $selected)
{
    $out = array('vm_template_id' => null, 'os_code' => null, 'template_id' => null);
    $raw = trim((string)$selected);
    if ($raw === '') {
        return $out;
    }

    // WHMCS often stores dropdown choices as "value|friendly label".
    if (strpos($raw, '|') !== false) {
        $raw = trim(explode('|', $raw, 2)[0]);
    }

    if (preg_match('/^rfvt:(\d+)$/', $raw, $m)) {
        $out['vm_template_id'] = (int)$m[1];
        return $out;
    }
    if (preg_match('/^rfot:(.+)$/', $raw, $m)) {
        $out['template_id'] = $m[1];
        return $out;
    }
    if (preg_match('/^rfos:(.+)$/', $raw, $m)) {
        $out['os_code'] = $m[1];
        return $out;
    }
    if (ctype_digit($raw)) {
        $out['vm_template_id'] = (int)$raw;
        return $out;
    }

    // Treat as OS code, disk template id, or friendly name — confirm against catalog when possible.
    $out['os_code'] = $raw;
    if ($productCode === null || $productCode === '') {
        return $out;
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return $out;
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'GET',
        '/api/billing/products/' . rawurlencode((string)$productCode),
        null
    );
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        return $out;
    }
    $product = $result['data'];
    $needle = strtolower($raw);

    if (!empty($product['vm_templates']) && is_array($product['vm_templates'])) {
        foreach ($product['vm_templates'] as $tmpl) {
            $name = isset($tmpl['name']) ? strtolower((string)$tmpl['name']) : '';
            $osType = isset($tmpl['os_type']) ? strtolower((string)$tmpl['os_type']) : '';
            $label = $name;
            if ($osType !== '') {
                $label .= ' (' . $osType . ')';
            }
            if ((string)$tmpl['id'] === $raw || $name === $needle || $label === $needle) {
                $out['vm_template_id'] = (int)$tmpl['id'];
                $out['os_code'] = null;
                return $out;
            }
        }
    }
    $groupTemplates = rackflow_osTemplatesForServerGroupId($params, null);
    if (!empty($groupTemplates) && is_array($groupTemplates)) {
        foreach ($groupTemplates as $tmpl) {
            $tid = isset($tmpl['id']) ? (string)$tmpl['id'] : '';
            $name = isset($tmpl['name']) ? strtolower((string)$tmpl['name']) : '';
            if ($tid !== '' && ($tid === $raw || strtolower($tid) === $needle || $name === $needle)) {
                $out['template_id'] = $tid;
                $out['os_code'] = null;
                return $out;
            }
        }
    }
    if (!empty($product['os_profiles']) && is_array($product['os_profiles'])) {
        foreach ($product['os_profiles'] as $profile) {
            $code = isset($profile['code']) ? (string)$profile['code'] : '';
            $name = isset($profile['name']) ? strtolower((string)$profile['name']) : '';
            if ($code === $raw || strtolower($code) === $needle || $name === $needle) {
                $out['os_code'] = $code !== '' ? $code : $raw;
                return $out;
            }
        }
    }
    return $out;
}

/**
 * Loader for module "Product Code" dropdown (configoption2).
 *
 * @param array $params
 * @return array code => "Name (code)"
 */
function rackflow_ProductCodeLoader($params)
{
    try {
        $serverId = rackflow_resolveModuleServerId(
            isset($params['pid']) ? (int)$params['pid'] : null,
            $params
        );
        if (!$serverId) {
            return array('' => '-- Assign a WHMCS Server Group first --');
        }
        $params['serverid'] = $serverId;
        $serviceType = isset($params['configoption1']) ? (string)$params['configoption1'] : null;
        $products = rackflow_fetchCatalogProducts($params, $serviceType);
        if (!$products) {
            // Retry without filter so admins still see the catalog if type mismatches.
            $products = rackflow_fetchCatalogProducts($params, null);
        }
        if (!$products) {
            return array('' => '-- No RackFlow products found (check API key) --');
        }
        $list = array('' => '-- Select a RackFlow product --');
        foreach ($products as $product) {
            if (empty($product['code'])) {
                continue;
            }
            $code = (string)$product['code'];
            $name = !empty($product['name']) ? (string)$product['name'] : $code;
            $stype = !empty($product['service_type']) ? (string)$product['service_type'] : '';
            $label = $name . ' (' . $code . ')';
            if ($stype !== '') {
                $label .= ' [' . $stype . ']';
            }
            $list[$code] = $label;
        }
        return $list;
    } catch (Exception $e) {
        rackflow_log('ProductCodeLoader: exception', array('message' => $e->getMessage()));
        return array('' => '-- Failed to load RackFlow products --');
    }
}

/**
 * Loader for module "Proxmox Location" dropdown (configoption5; VM products).
 *
 * @param array $params
 * @return array id => "Name (ID: n, nodes: …)"
 */
function rackflow_ProxmoxClusterLoader($params)
{
    rackflow_log('ProxmoxClusterLoader called', array('serverid' => isset($params['serverid']) ? $params['serverid'] : null));
    try {
        if (empty($params['serverid'])) {
            return array('' => '-- Configure server in Module Settings first --', '1' => 'London (ID: 1)');
        }
        $serverId = (int)$params['serverid'];
        $serverConfig = rackflow_getServerConfigFromDB($serverId);
        if (!$serverConfig) {
            return array('' => '-- Server not found in WHMCS --', '1' => 'London (ID: 1)');
        }
        $hostname = !empty($serverConfig['ipaddress']) ? $serverConfig['ipaddress'] : ($serverConfig['hostname'] ?? '');
        $apiKey = trim($serverConfig['accesshash'] ?? $serverConfig['password'] ?? '');
        $port = isset($serverConfig['port']) ? $serverConfig['port'] : '8000';
        $useSSL = isset($serverConfig['secure']) ? (bool)$serverConfig['secure'] : false;
        if (empty($hostname) || empty($apiKey)) {
            return array('' => '-- Configure server credentials in Setup > Servers --', '1' => 'London (ID: 1)');
        }
        $protocol = $useSSL ? 'https' : 'http';
        $hostname = preg_replace('#^https?://#', '', $hostname);
        $hostname = rtrim($hostname, '/');
        $apiUrl = $protocol . '://' . $hostname;
        if (!empty($port) && $port != '80' && $port != '443') {
            $apiUrl .= ':' . $port;
        }
        $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/proxmox/clusters', null);
        if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
            rackflow_log('ProxmoxClusterLoader: API failed, using fallback', array(
                'error' => isset($result['error']) ? $result['error'] : 'unknown',
            ));
            return array('' => '-- Auto (any cluster) --', '1' => 'London (ID: 1)');
        }
        $list = array('' => '-- Auto (any cluster) --');
        foreach ($result['data'] as $cluster) {
            if (!isset($cluster['id'])) {
                continue;
            }
            $cid = (string)$cluster['id'];
            $name = isset($cluster['name']) ? $cluster['name'] : ('Cluster ' . $cid);
            $nodeNames = array();
            if (!empty($cluster['nodes']) && is_array($cluster['nodes'])) {
                foreach ($cluster['nodes'] as $n) {
                    if (!empty($n['node_name'])) {
                        $nodeNames[] = $n['node_name'];
                    }
                }
            }
            $label = $name . ' (ID: ' . $cid;
            if ($nodeNames) {
                $label .= ', nodes: ' . implode(', ', $nodeNames);
            }
            $label .= ')';
            $list[$cid] = $label;
        }
        return $list;
    } catch (Exception $e) {
        rackflow_log('ProxmoxClusterLoader: exception', array('message' => $e->getMessage()));
        return array('' => '-- Auto (any cluster) --', '1' => 'London (ID: 1)');
    }
}

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
define('RACKFLOW_SSH_KEYS_FIELD_NAME', 'SSH Public Keys');

/**
 * Validate and normalize multiline OpenSSH public keys (one key per line).
 *
 * @param string $text
 * @return array{ok:bool,keys:string[],error:?string}
 */
function rackflow_parseSshPublicKeys($text)
{
    $out = array('ok' => true, 'keys' => array(), 'error' => null);
    if ($text === null || trim((string)$text) === '') {
        return $out;
    }
    $lines = preg_split('/\r\n|\r|\n/', (string)$text);
    $seen = array();
    $pattern = '/^(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+\S+(?:\s+.*)?$/';
    foreach ($lines as $raw) {
        $line = trim((string)$raw);
        if ($line === '' || strpos($line, '#') === 0) {
            continue;
        }
        if (!preg_match($pattern, $line)) {
            $out['ok'] = false;
            $out['error'] = 'Invalid SSH public key line (expected ssh-ed25519 / ssh-rsa / ecdsa…): '
                . substr($line, 0, 80);
            return $out;
        }
        if (!isset($seen[$line])) {
            $seen[$line] = true;
            $out['keys'][] = $line;
        }
    }
    return $out;
}

/**
 * Ensure the product has a client-visible "SSH Public Keys" textarea custom field.
 *
 * @param int $productId
 * @return int|null
 */
function rackflow_ensureSshKeysCustomField($productId)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    try {
        $capsule = \Illuminate\Database\Capsule\Manager::class;
        $field = $capsule::table('tblcustomfields')
            ->where('relid', (int)$productId)
            ->where('fieldname', RACKFLOW_SSH_KEYS_FIELD_NAME)
            ->first();
        if (!$field || !isset($field->id)) {
            $capsule::table('tblcustomfields')->insert(array(
                'type' => 'product',
                'relid' => (int)$productId,
                'fieldname' => RACKFLOW_SSH_KEYS_FIELD_NAME,
                'fieldtype' => 'textarea',
                'description' => 'One OpenSSH public key per line (optional). Shown for Linux OS selections that support SSH keys.',
                'required' => '',
                'showorder' => 'on',
                'showinvoice' => '',
                'adminonly' => '',
            ));
            $field = $capsule::table('tblcustomfields')
                ->where('relid', (int)$productId)
                ->where('fieldname', RACKFLOW_SSH_KEYS_FIELD_NAME)
                ->first();
        }
        if (!$field || !isset($field->id)) {
            return null;
        }
        $capsule::table('tblcustomfields')
            ->where('id', (int)$field->id)
            ->update(array(
                'fieldtype' => 'textarea',
                'showorder' => 'on',
                'adminonly' => '',
                'required' => '',
            ));
        return (int)$field->id;
    } catch (Exception $e) {
        rackflow_log('rackflow_ensureSshKeysCustomField failed', array(
            'error' => $e->getMessage(),
            'productid' => $productId,
        ));
        return null;
    }
}

/**
 * Build rfvt:/rfos: → accepts_ssh_key map from a billing catalog product payload.
 *
 * @param array $catalogProduct
 * @return array<string,bool>
 */
function rackflow_sshAcceptMapFromCatalog(array $catalogProduct, $groupOsTemplates = array())
{
    $map = array();
    if (!empty($catalogProduct['vm_templates']) && is_array($catalogProduct['vm_templates'])) {
        foreach ($catalogProduct['vm_templates'] as $tmpl) {
            if (empty($tmpl['id'])) {
                continue;
            }
            $token = 'rfvt:' . (int)$tmpl['id'];
            $map[$token] = !empty($tmpl['accepts_ssh_key']);
        }
    }
    if (!empty($groupOsTemplates) && is_array($groupOsTemplates)) {
        foreach ($groupOsTemplates as $tmpl) {
            if (empty($tmpl['id'])) {
                continue;
            }
            $token = 'rfot:' . (string)$tmpl['id'];
            $map[$token] = !empty($tmpl['accepts_ssh_key']);
        }
    }
    if (!empty($catalogProduct['os_profiles']) && is_array($catalogProduct['os_profiles'])) {
        foreach ($catalogProduct['os_profiles'] as $profile) {
            if (empty($profile['code'])) {
                continue;
            }
            $map['rfos:' . (string)$profile['code']] = false;
        }
    }
    return $map;
}

/**
 * Extract rfvt:/rfos: token from a WHMCS config sub optionname ("token|Label").
 *
 * @param string $optionname
 * @return string
 */
function rackflow_checkoutOsTokenFromOptionname($optionname)
{
    $raw = trim((string)$optionname);
    if ($raw === '') {
        return '';
    }
    $pipe = strpos($raw, '|');
    if ($pipe !== false) {
        $raw = substr($raw, 0, $pipe);
    }
    $raw = trim($raw);
    if (preg_match('/^(rfvt:\d+|rfos:[A-Za-z0-9._-]+|rfot:[A-Za-z0-9._-]+)$/', $raw)) {
        return $raw;
    }
    return '';
}

/**
 * Checkout SSH visibility map: catalog tokens plus WHMCS sub-option IDs.
 *
 * Order-form <select> values are tblproductconfigoptionssub.id, not rfvt:/rfos:
 * tokens, so the client script needs numeric keys as well.
 *
 * @param int $productId
 * @param array<string,bool> $tokenAcceptMap from rackflow_sshAcceptMapFromCatalog
 * @return array{map:array<string,bool>,os_config_option_id:int}
 */
function rackflow_sshAcceptMapForCheckout($productId, array $tokenAcceptMap)
{
    $out = array(
        'map' => $tokenAcceptMap,
        'os_config_option_id' => 0,
    );
    $productId = (int)$productId;
    if ($productId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return $out;
    }
    try {
        $capsule = '\Illuminate\Database\Capsule\Manager';
        $groupIds = $capsule::table('tblproductconfiglinks')
            ->where('pid', $productId)
            ->pluck('gid');
        if (empty($groupIds)) {
            return $out;
        }
        $options = $capsule::table('tblproductconfigoptions')
            ->whereIn('gid', $groupIds)
            ->whereIn('optionname', array('OS', 'Operating System', 'vm_template_id'))
            ->where('hidden', 0)
            ->get();
        $option = null;
        $preference = array('OS' => 0, 'Operating System' => 1, 'vm_template_id' => 2);
        foreach ($options as $candidate) {
            $name = isset($candidate->optionname) ? (string)$candidate->optionname : '';
            if (!isset($preference[$name])) {
                continue;
            }
            if (
                !$option
                || $preference[$name] < $preference[(string)$option->optionname]
            ) {
                $option = $candidate;
            }
        }
        if (!$option || empty($option->id)) {
            return $out;
        }
        $out['os_config_option_id'] = (int)$option->id;
        $subs = $capsule::table('tblproductconfigoptionssub')
            ->where('configid', (int)$option->id)
            ->where('hidden', 0)
            ->get();
        foreach ($subs as $sub) {
            if (empty($sub->id)) {
                continue;
            }
            $token = rackflow_checkoutOsTokenFromOptionname(
                isset($sub->optionname) ? (string)$sub->optionname : ''
            );
            $accepts = false;
            if ($token !== '' && array_key_exists($token, $tokenAcceptMap)) {
                $accepts = !empty($tokenAcceptMap[$token]);
            } elseif ($token !== '' && strpos($token, 'rfvt:') === 0) {
                // Catalog miss: do not assume Linux/Windows — hide until known.
                $accepts = false;
            }
            $out['map'][(string)(int)$sub->id] = $accepts;
            if ($token !== '') {
                $out['map'][$token] = $accepts;
            }
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_sshAcceptMapForCheckout failed', array(
            'pid' => $productId,
            'error' => $e->getMessage(),
        ));
    }
    return $out;
}

/**
 * Warn admins about duplicate active product slugs / missing billing cycles.
 *
 * Soft guardrail only (does not block save). Messages are stashed in the
 * session and rendered on the next Module Settings page load.
 *
 * @param int $productId
 * @return void
 */
function rackflow_warnProductHygiene($productId)
{
    $productId = (int)$productId;
    if ($productId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $warnings = array();
    try {
        $product = $capsule::table('tblproducts')->where('id', $productId)->first();
        if (!$product || empty($product->servertype) || $product->servertype !== 'rackflow') {
            return;
        }

        // Duplicate active slugs (tblproducts_slugs) cause order-form redirects
        // to the wrong product (domain step / wrong config).
        if ($capsule::schema()->hasTable('tblproducts_slugs')) {
            $slugs = $capsule::table('tblproducts_slugs')
                ->where('product_id', $productId)
                ->where('active', 1)
                ->pluck('slug');
            foreach ($slugs as $slug) {
                $slug = trim((string)$slug);
                if ($slug === '') {
                    continue;
                }
                $dupes = $capsule::table('tblproducts_slugs')
                    ->where('slug', $slug)
                    ->where('active', 1)
                    ->where('product_id', '!=', $productId)
                    ->get();
                foreach ($dupes as $dupe) {
                    $other = $capsule::table('tblproducts')->where('id', (int)$dupe->product_id)->first();
                    $otherHidden = $other && (!empty($other->hidden) || !empty($other->retired));
                    // Warn for any other active slug owner; hidden/retired dupes
                    // are especially surprising because they still win routing.
                    $warnings[] = 'Slug "' . $slug . '" is also active on product #'
                        . (int)$dupe->product_id
                        . ($other && !empty($other->name) ? ' (' . $other->name . ')' : '')
                        . ($otherHidden ? ' [hidden/retired]' : '')
                        . '. Deactivate the duplicate slug to avoid cart redirects.';
                }
            }
        }

        $pricing = $capsule::table('tblpricing')
            ->where('type', 'product')
            ->where('relid', $productId)
            ->first();
        if ($pricing) {
            $cycles = array('monthly', 'quarterly', 'semiannually', 'annually', 'biennially', 'triennially');
            $anyActive = false;
            foreach ($cycles as $cycle) {
                if (isset($pricing->$cycle) && (float)$pricing->$cycle >= 0) {
                    $anyActive = true;
                    break;
                }
            }
            if (!$anyActive) {
                $warnings[] = 'No active billing cycle is enabled (all prices are -1). '
                    . 'The order form may redirect unexpectedly until a cycle is set.';
            }
        } else {
            $warnings[] = 'No pricing row found for this product. Set at least one billing cycle.';
        }
    } catch (Exception $e) {
        rackflow_log('rackflow_warnProductHygiene failed', array(
            'pid' => $productId,
            'error' => $e->getMessage(),
        ));
        return;
    }

    if (!$warnings) {
        return;
    }
    if (session_status() === PHP_SESSION_ACTIVE || (function_exists('session_id') && session_id() !== '')) {
        $_SESSION['rackflow_product_hygiene_warnings'] = array(
            'pid' => $productId,
            'messages' => array_values(array_unique($warnings)),
        );
    }
    foreach ($warnings as $msg) {
        if (function_exists('logActivity')) {
            logActivity('RackFlow product #' . $productId . ': ' . $msg);
        }
        rackflow_log('Product hygiene warning', array('pid' => $productId, 'message' => $msg));
    }
}

/**
 * Read SSH Public Keys custom field from CreateAccount/module params.
 *
 * @param array $params
 * @return string
 */
function rackflow_sshKeysFromParams(array $params)
{
    $candidates = array(
        RACKFLOW_SSH_KEYS_FIELD_NAME,
        'ssh_public_keys',
        'SSH Public Key',
        'ssh_public_key',
    );
    if (!empty($params['customfields']) && is_array($params['customfields'])) {
        foreach ($candidates as $name) {
            if (isset($params['customfields'][$name]) && trim((string)$params['customfields'][$name]) !== '') {
                return (string)$params['customfields'][$name];
            }
        }
        // WHMCS sometimes keys custom fields by numeric id — scan values looking like keys.
        foreach ($params['customfields'] as $val) {
            if (!is_string($val)) {
                continue;
            }
            if (preg_match('/^\s*ssh-(ed25519|rsa|dss|ecdsa)/m', $val)) {
                return $val;
            }
        }
    }
    return '';
}

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
 * Ensure the product has an admin-only "RackFlow Service ID" custom field; create it if missing.
 *
 * @param int $productId WHMCS product/package ID (tblproducts.id)
 * @return int|null Custom field ID, or null on failure
 */
function rackflow_ensureServiceIdCustomField($productId)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    try {
        $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('relid', (int)$productId)
            ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
            ->first();
        if (!$field || !isset($field->id)) {
            \Illuminate\Database\Capsule\Manager::table('tblcustomfields')->insert(array(
                'type' => 'product',
                'relid' => (int)$productId,
                'fieldname' => RACKFLOW_SERVICE_ID_FIELD_NAME,
                'fieldtype' => 'text',
                'adminonly' => 'on',
            ));
            $field = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
                ->where('relid', (int)$productId)
                ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
                ->first();
            if (!$field || !isset($field->id)) {
                rackflow_log('rackflow_ensureServiceIdCustomField: create failed', array(
                    'productid' => $productId,
                ));
                return null;
            }
            rackflow_log('rackflow_ensureServiceIdCustomField: created', array(
                'productid' => $productId,
                'fieldid' => (int)$field->id,
            ));
        }
        $fieldId = (int)$field->id;
        // Keep admin-only so the mapping is not shown in the client area
        \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('id', $fieldId)
            ->update(array('adminonly' => 'on'));
        return $fieldId;
    } catch (Exception $e) {
        rackflow_log('rackflow_ensureServiceIdCustomField failed', array(
            'error' => $e->getMessage(),
            'productid' => $productId,
        ));
        return null;
    }
}

/**
 * Persist the RackFlow service ID into the product custom field "RackFlow Service ID".
 * Creates the field when missing. Pass an empty string to clear the link
 * (does not create a value row if none exists).
 *
 * @param int         $serviceId          WHMCS service ID (tblhosting.id)
 * @param int         $productId          WHMCS product/package ID (tblproducts.id)
 * @param int|string  $rackflowServiceId  RackFlow service ID to store, or '' to clear
 * @return bool True if saved, false on error
 */
function rackflow_saveServiceIdCustomField($serviceId, $productId, $rackflowServiceId)
{
    if (empty($serviceId) || empty($productId)) {
        return false;
    }
    $value = trim((string)$rackflowServiceId);
    try {
        if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
            return false;
        }
        $existingField = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('relid', (int)$productId)
            ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
            ->first();
        if ($value === '' && (!$existingField || !isset($existingField->id))) {
            return true;
        }
        $fieldId = rackflow_ensureServiceIdCustomField((int)$productId);
        if (empty($fieldId)) {
            rackflow_log('rackflow_saveServiceIdCustomField: no custom field found/created', array(
                'productid' => $productId,
                'fieldname' => RACKFLOW_SERVICE_ID_FIELD_NAME,
            ));
            return false;
        }
        $existing = \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
            ->where('fieldid', $fieldId)
            ->where('relid', (int)$serviceId)
            ->first();
        if ($value === '') {
            if ($existing) {
                \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
                    ->where('fieldid', $fieldId)
                    ->where('relid', (int)$serviceId)
                    ->update(array('value' => ''));
            }
            rackflow_log('rackflow_saveServiceIdCustomField: cleared', array(
                'serviceid' => $serviceId,
                'productid' => $productId,
            ));
            return true;
        }
        if ($existing) {
            \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')
                ->where('fieldid', $fieldId)
                ->where('relid', (int)$serviceId)
                ->update(array('value' => $value));
        } else {
            \Illuminate\Database\Capsule\Manager::table('tblcustomfieldsvalues')->insert(array(
                'fieldid' => $fieldId,
                'relid' => (int)$serviceId,
                'value' => $value,
            ));
        }
        rackflow_log('rackflow_saveServiceIdCustomField: saved', array(
            'serviceid' => $serviceId,
            'productid' => $productId,
            'rackflow_service_id' => $value,
        ));
        return true;
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
 * Absolute SPA URL for a RackFlow admin service page (no SSO).
 *
 * @param string $apiUrl        RackFlow API base URL from server config
 * @param int    $rackflowSvcId RackFlow service id
 * @return string
 */
function rackflow_adminServicePageUrl($apiUrl, $rackflowSvcId)
{
    $base = rtrim((string)$apiUrl, '/');
    if ($base === '' || empty($rackflowSvcId)) {
        return '';
    }
    return $base . '/admin/services/' . (int)$rackflowSvcId;
}

/**
 * Same-origin URL for the admin link-management JSON endpoint.
 *
 * @param string $systemUrl optional WHMCS SystemURL fallback
 * @return string
 */
function rackflow_adminLinkEndpointUrl($systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/admin_link.php';
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
        $serviceType = rackflow_getConfiguredServiceType($params);
        if ($serviceType !== 'bare_metal') {
            return 'Register in RackFlow is only available for bare metal services (this product is "' . $serviceType . '").';
        }

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

        // Dedicated IP / package / client: prefer params, then load from DB (module commands can omit some fields)
        $dedicatedIp = isset($params['dedicatedip']) ? trim($params['dedicatedip']) : '';
        if (class_exists('\Illuminate\Database\Capsule\Manager')) {
            $hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')->where('id', $serviceId)->first();
            if ($hosting) {
                if (empty($dedicatedIp) && isset($hosting->dedicatedip) && trim($hosting->dedicatedip) !== '') {
                    $dedicatedIp = trim($hosting->dedicatedip);
                }
                if (empty($packageId) && isset($hosting->packageid)) {
                    $packageId = (int)$hosting->packageid;
                }
                if (empty($userId) && isset($hosting->userid)) {
                    $userId = (int)$hosting->userid;
                }
            }
        }
        if (empty($dedicatedIp)) {
            return 'Dedicated IP is required. Set the Dedicated IP on this service and save, then run this command again.';
        }
        if (empty($packageId)) {
            return 'Product/package ID is missing; cannot store the RackFlow Service ID link.';
        }
        if (empty($userId)) {
            return 'Client/user ID is missing; cannot assign the billing owner in RackFlow.';
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

        // WHMCS provisioning modules expose client profile as clientsdetails (with an "s")
        $clientDetails = array();
        if (isset($params['clientsdetails']) && is_array($params['clientsdetails'])) {
            $clientDetails = $params['clientsdetails'];
        } elseif (isset($params['clientdetails']) && is_array($params['clientdetails'])) {
            $clientDetails = $params['clientdetails'];
        }
        $externalUsername = '';
        if (!empty($clientDetails['firstname']) || !empty($clientDetails['lastname'])) {
            $externalUsername = trim(
                (isset($clientDetails['firstname']) ? $clientDetails['firstname'] : '')
                . ' '
                . (isset($clientDetails['lastname']) ? $clientDetails['lastname'] : '')
            );
        }
        if ($externalUsername === '' && !empty($params['username'])) {
            $externalUsername = (string)$params['username'];
        }
        $externalEmail = '';
        if (!empty($clientDetails['email'])) {
            $externalEmail = (string)$clientDetails['email'];
        } elseif (!empty($params['email'])) {
            $externalEmail = (string)$params['email'];
        }

        $registerPayload = array(
            'server_id' => $serverId,
            'external_service_id' => (string)$serviceId,
            'external_user_id' => (string)$userId,
            'external_username' => $externalUsername !== '' ? $externalUsername : null,
            'external_email' => $externalEmail !== '' ? $externalEmail : null,
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

        // Persist WHMCS <-> RackFlow mapping (creates the product custom field when needed)
        if (!rackflow_saveServiceIdCustomField($serviceId, $packageId, $rackflowServiceId)) {
            return 'RackFlow service ' . $rackflowServiceId
                . ' was created, but saving the RackFlow Service ID custom field on this WHMCS service failed.'
                . ' Enter that ID manually on the service and save.';
        }

        rackflow_log('RegisterInRackflow success', array(
            'serviceid' => $serviceId,
            'userid' => $userId,
            'packageid' => $packageId,
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
 * Rotate proxy credentials (new username+password, same IP(s)) for an
 * http_proxy service via the billing API. Gated server-side by the
 * ``proxy.rotate_credentials`` client permission (billing API returns 403
 * when the caller lacks it).
 *
 * @param array $params WHMCS module params (serviceid, packageid, serverid, etc.)
 * @return string "success" or an error message
 */
function rackflow_RotateProxyCredentials(array $params)
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return 'This service is not linked to RackFlow.';
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return 'API URL and API key must be configured (Setup > Servers).';
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowServiceId . '/proxy/rotate',
        array()
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return 'Rotate failed: ' . $detail;
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
 * Run a strategy action via billing API.
 *
 * @param array  $params
 * @param string $actionName
 * @param array  $actionParams
 * @param string $audience admin|client
 * @return string
 */
function rackflow_runStrategyAction(array $params, $actionName, array $actionParams = array(), $audience = 'admin')
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return 'This service is not linked to RackFlow.';
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return 'API URL and API key must be configured (Setup > Servers).';
    }
    $qs = '?audience=' . urlencode($audience);
    // change_password may briefly wait for the guest agent (~8s) before
    // deferring; keep headroom under WHMCS's module call budget.
    $timeout = ($actionName === 'change_password') ? 45 : 30;
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowServiceId . '/actions/' . rawurlencode($actionName) . $qs,
        array('params' => $actionParams),
        $timeout
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return is_string($detail) ? $detail : $err;
    }
    return 'success';
}

/**
 * WHMCS ChangePassword module hook + admin/client custom button.
 * Uses the service password from WHMCS as the new guest password.
 */
function rackflow_ChangePassword(array $params)
{
    $password = isset($params['password']) ? (string)$params['password'] : '';
    if ($password === '') {
        return 'Password is empty';
    }
    $audience = 'admin';
    // Client area custom button vs ChangePassword hook — both use same path
    if (!empty($params['clientsdetails'])) {
        $audience = 'client';
    }
    return rackflow_runStrategyAction($params, 'change_password', array('password' => $password), $audience);
}

function rackflow_ResetNetwork(array $params)
{
    $audience = !empty($params['clientsdetails']) ? 'client' : 'admin';
    return rackflow_runStrategyAction($params, 'reset_network', array(), $audience);
}

function rackflow_RandomizeSmbios(array $params)
{
    return rackflow_runStrategyAction($params, 'randomize_smbios', array(), 'admin');
}

/**
 * List free VM IP pool rows for the linked RackFlow VM service's Proxmox cluster.
 *
 * @param array $params WHMCS module params
 * @return array{success:bool,data?:array,error?:string}
 */
function rackflow_fetchAvailableIps(array $params)
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return array('success' => false, 'error' => 'This service is not linked to RackFlow.');
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return array('success' => false, 'error' => 'API URL and API key must be configured (Setup > Servers).');
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'GET',
        '/api/billing/services/' . (int)$rackflowServiceId . '/available-ips',
        null
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return array('success' => false, 'error' => is_string($detail) ? $detail : $err);
    }
    return array(
        'success' => true,
        'data' => is_array($result['data']) ? $result['data'] : array(),
    );
}

/**
 * Reassign the linked VM service to a free pool IP and optionally reset guest networking.
 * Syncs WHMCS dedicatedip on success. Cloud-init guests reboot when reset_network is true.
 *
 * @param array $params WHMCS module params
 * @param int   $allocationId
 * @param bool  $resetNetwork
 * @return array{success:bool,message?:string,data?:array,error?:string}
 */
function rackflow_reassignVmIp(array $params, $allocationId, $resetNetwork = true)
{
    $allocationId = (int)$allocationId;
    if ($allocationId <= 0) {
        return array('success' => false, 'error' => 'Select a free IP address.');
    }
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return array('success' => false, 'error' => 'This service is not linked to RackFlow.');
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return array('success' => false, 'error' => 'API URL and API key must be configured (Setup > Servers).');
    }
    // Cloud-init reset regenerates the drive, cleans, and reboots (agent wait).
    $timeout = $resetNetwork ? 180 : 45;
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowServiceId . '/reassign-ip',
        array(
            'allocation_id' => $allocationId,
            'reset_network' => (bool)$resetNetwork,
        ),
        $timeout
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return array('success' => false, 'error' => is_string($detail) ? $detail : $err);
    }
    $data = is_array($result['data']) ? $result['data'] : array();
    $newIp = isset($data['vm_ip_address']) ? (string)$data['vm_ip_address'] : '';
    if ($newIp !== '' && !empty($params['serviceid']) && function_exists('localAPI')) {
        try {
            localAPI('UpdateClientProduct', array(
                'serviceid' => (int)$params['serviceid'],
                'dedicatedip' => $newIp,
            ));
        } catch (Exception $ipEx) {
            logModuleCall('rackflow', __FUNCTION__ . '_dedicatedip', $data, $ipEx->getMessage(), '');
        }
    }
    $message = $newIp !== '' ? ('IP reassigned to ' . $newIp) : 'IP reassigned';
    if (!empty($data['network_error'])) {
        $message .= '. Guest network reset failed: ' . (string)$data['network_error'];
    } elseif ($resetNetwork) {
        $message .= ' (guest network reset).';
    }
    return array('success' => true, 'message' => $message, 'data' => $data);
}

/**
 * Same-origin URL for the admin AJAX VM IP reassignment endpoint.
 *
 * @param int    $whmcsServiceId
 * @param string $systemUrl
 * @return string
 */
function rackflow_ipActionEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    unset($whmcsServiceId);
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/ip_action.php';
}

/**
 * Fetch VM backups from the billing API.
 *
 * @param array  $params
 * @param string $audience admin|client
 * @return array{success:bool,backups?:array,error?:string}
 */
/**
 * Human label for backup kind/scope (API still uses platform|client).
 *
 * @param string $kind
 * @return string
 */
function rackflow_backupKindLabel($kind)
{
    $k = strtolower(trim((string)$kind));
    if ($k === 'platform') {
        return 'Scheduled';
    }
    if ($k === 'client') {
        return 'Customer';
    }
    if ($k === 'restore' || $k === 'backup') {
        return ucfirst($k);
    }
    return $kind !== '' ? (string)$kind : 'Backup';
}

/**
 * Strip durable rf1 identity tokens from PBS notes for display.
 *
 * @param string $notes
 * @return string
 */
function rackflow_backupNotesDisplay($notes)
{
    $text = trim((string)$notes);
    if ($text === '') {
        return '';
    }
    $text = preg_replace('/\s*[·•]\s*rf1:\S+/u', '', $text);
    $text = preg_replace('/\s*rf1:\S+/', '', $text);
    return trim((string)$text);
}

function rackflow_fetchBackups(array $params, $audience = 'client')
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return array('success' => false, 'error' => 'This service is not linked to RackFlow.');
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return array('success' => false, 'error' => 'API URL and API key must be configured.');
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'GET',
        '/api/billing/services/' . (int)$rackflowServiceId . '/backups?audience=' . urlencode($audience),
        null,
        60
    );
    if (!$result['success']) {
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : (isset($result['error']) ? $result['error'] : 'Failed to list backups');
        return array('success' => false, 'error' => is_string($detail) ? $detail : 'Failed to list backups');
    }
    $backups = (isset($result['data']['backups']) && is_array($result['data']['backups'])) ? $result['data']['backups'] : array();
    $jobs = (isset($result['data']['jobs']) && is_array($result['data']['jobs'])) ? $result['data']['jobs'] : array();
    return array('success' => true, 'backups' => $backups, 'jobs' => $jobs);
}

/**
 * @param array  $params
 * @param string $method POST
 * @param string $path relative to /api/billing/services/{id}/
 * @param array  $body
 * @param string $audience
 * @param int    $timeoutSeconds
 * @return string success or error message
 */
function rackflow_backupApiAction(array $params, $path, array $body, $audience = 'client', $timeoutSeconds = 600)
{
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return 'This service is not linked to RackFlow.';
    }
    $apiConfig = rackflow_getApiConfig($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key'])) {
        return 'API URL and API key must be configured (Setup > Servers).';
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowServiceId . '/' . ltrim($path, '/')
            . '?audience=' . urlencode($audience),
        $body,
        $timeoutSeconds
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return is_string($detail) ? $detail : $err;
    }
    return 'success';
}

function rackflow_CreateBackup(array $params)
{
    $audience = !empty($params['clientsdetails']) ? 'client' : 'admin';
    $notes = isset($_REQUEST['rf_notes']) ? trim((string)$_REQUEST['rf_notes']) : '';
    // Async: vzdump can take minutes; running jobs are shown on the product page.
    $body = array('wait' => false, 'mode' => 'snapshot');
    if ($notes !== '') {
        $body['notes'] = $notes;
    }
    return rackflow_backupApiAction($params, 'backups', $body, $audience, 60);
}

function rackflow_DeleteBackup(array $params)
{
    $audience = !empty($params['clientsdetails']) ? 'client' : 'admin';
    $volid = isset($_REQUEST['volid']) ? (string)$_REQUEST['volid'] : '';
    $storage = isset($_REQUEST['storage']) ? (string)$_REQUEST['storage'] : '';
    if ($volid === '') {
        return 'Missing backup volid';
    }
    $body = array('volid' => $volid);
    if ($storage !== '') {
        $body['storage'] = $storage;
    }
    return rackflow_backupApiAction($params, 'backups/delete', $body, $audience, 120);
}

function rackflow_RestoreBackup(array $params)
{
    $audience = !empty($params['clientsdetails']) ? 'client' : 'admin';
    $volid = isset($_REQUEST['volid']) ? (string)$_REQUEST['volid'] : '';
    $storage = isset($_REQUEST['storage']) ? (string)$_REQUEST['storage'] : '';
    if ($volid === '') {
        return 'Missing backup volid';
    }
    // Enqueue durable restore job (start guest after restore). Progress appears
    // under Running jobs / auto-refresh on the product page.
    $body = array('volid' => $volid, 'wait' => false, 'start' => true);
    if ($storage !== '') {
        $body['storage'] = $storage;
    }
    return rackflow_backupApiAction($params, 'backups/restore', $body, $audience, 60);
}

/**
 * Reinstall VM guest at the reserved VMID (optional template / SSH keys).
 *
 * Prefer the admin status-card Reinstall panel (template picker). Module Commands
 * use the current Operating System config option when present.
 *
 * @param array $params
 * @return string "success" or error message
 */
function rackflow_Reinstall(array $params)
{
    if (rackflow_getConfiguredServiceType($params) !== 'vm') {
        return 'Reinstall is only available for VM products.';
    }
    $apiConfig = rackflow_getApiConfig($params);
    $rackflowSvcId = rackflow_getRackflowServiceId($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key']) || empty($rackflowSvcId)) {
        return 'Missing API URL, API key, or RackFlow Service ID.';
    }
    $audience = !empty($params['clientsdetails']) ? 'client' : 'admin';
    $body = array();

    $vmTemplateId = isset($_REQUEST['rf_vm_template_id']) ? trim((string)$_REQUEST['rf_vm_template_id']) : '';
    if ($vmTemplateId === '' && isset($_REQUEST['vm_template_id'])) {
        $vmTemplateId = trim((string)$_REQUEST['vm_template_id']);
    }
    if ($vmTemplateId === '' && !empty($params['configoptions']) && is_array($params['configoptions'])) {
        foreach (array('Operating System', 'OS', 'vm_template_id') as $optName) {
            if (!isset($params['configoptions'][$optName]) || $params['configoptions'][$optName] === '') {
                continue;
            }
            $productCode = rackflow_configOptionValue($params, array('product_code', 'Product Code'));
            if ($productCode === null || $productCode === '') {
                $productCode = isset($params['configoption2']) ? (string)$params['configoption2'] : '';
            }
            $resolved = rackflow_resolveCheckoutOsSelection(
                $params,
                (string)$productCode,
                (string)$params['configoptions'][$optName]
            );
            if (!empty($resolved['vm_template_id'])) {
                $vmTemplateId = (string)(int)$resolved['vm_template_id'];
            }
            break;
        }
    }
    if ($vmTemplateId !== '' && ctype_digit($vmTemplateId)) {
        $body['vm_template_id'] = (int)$vmTemplateId;
    }

    $sshKeys = isset($_REQUEST['rf_ssh_public_keys']) ? (string)$_REQUEST['rf_ssh_public_keys'] : '';
    if ($sshKeys === '') {
        $sshKeys = rackflow_sshKeysFromParams($params);
    }
    if (is_string($sshKeys) && trim($sshKeys) !== '') {
        $body['ssh_public_keys'] = $sshKeys;
    }

    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'POST',
        '/api/billing/services/' . (int)$rackflowSvcId . '/vm/reinstall?audience=' . urlencode($audience),
        $body,
        120
    );
    if (!$result['success']) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return is_string($detail) ? $detail : $err;
    }
    return 'success';
}

/**
 * Fetch VM reinstall template options from the billing API.
 *
 * @param array  $params
 * @param string $audience admin|client
 * @return array{success:bool,templates?:array,vm_template_id?:int|null,error?:string}
 */
function rackflow_fetchReinstallOptions(array $params, $audience = 'admin')
{
    $apiConfig = rackflow_getApiConfig($params);
    $rackflowSvcId = rackflow_getRackflowServiceId($params);
    if (empty($apiConfig['url']) || empty($apiConfig['key']) || empty($rackflowSvcId)) {
        return array('success' => false, 'error' => 'Missing API URL, API key, or RackFlow Service ID.');
    }
    if (!in_array($audience, array('admin', 'client'), true)) {
        $audience = 'admin';
    }
    $result = rackflow_apiCall(
        $apiConfig['url'],
        $apiConfig['key'],
        'GET',
        '/api/billing/services/' . (int)$rackflowSvcId . '/vm/reinstall-options?audience=' . urlencode($audience),
        null,
        30
    );
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unable to load reinstall options';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        return array('success' => false, 'error' => is_string($detail) ? $detail : $err);
    }
    $data = $result['data'];
    return array(
        'success' => true,
        'templates' => isset($data['reinstall_templates']) && is_array($data['reinstall_templates'])
            ? $data['reinstall_templates']
            : array(),
        'vm_template_id' => isset($data['vm_template_id']) ? $data['vm_template_id'] : null,
        'accepts_ssh_key' => !empty($data['accepts_ssh_key']),
        'ssh_public_keys_text' => isset($data['ssh_public_keys_text']) ? (string)$data['ssh_public_keys_text'] : '',
    );
}

/**
 * Same-origin URL for the AJAX VM reinstall endpoint.
 *
 * @param int    $whmcsServiceId
 * @param string $systemUrl
 * @return string
 */
function rackflow_reinstallActionEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    unset($whmcsServiceId);
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/reinstall_action.php';
}

/**
 * Cached full /status payload for strategy action button discovery.
 *
 * @param array $params
 * @return array|null
 */
function rackflow_getServiceStatusPayload(array $params)
{
    static $cache = array();
    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return null;
    }
    if (array_key_exists($rackflowServiceId, $cache)) {
        return $cache[$rackflowServiceId];
    }
    $apiConfig = rackflow_getApiConfig($params);
    $statusData = rackflow_fetchServiceStatus($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
    $cache[$rackflowServiceId] = $statusData;
    return $statusData;
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
 * Fetch (and cache for the current request) the effective client permissions
 * and power availability for a WHMCS service, via the billing API's
 * ``/status`` endpoint (``client_permissions`` / ``power_available`` fields).
 *
 * See ``app.core.client_permissions`` / ``app.services.client_permission_resolver``
 * on the RackFlow backend for how these are resolved (product -> user ->
 * service preset -> service overrides).
 *
 * @param array $params WHMCS module parameters (serviceid, packageid/pid, serverid, etc.)
 * @return array{permissions: array<string,bool>, power_available: bool}|null
 *         null when the service isn't linked to RackFlow yet or the status
 *         fetch fails (caller should treat this as "unknown" and decide its
 *         own fallback, not as "denied").
 */
function rackflow_getClientPermissions(array $params)
{
    static $cache = array();

    $rackflowServiceId = rackflow_getRackflowServiceId($params);
    if (empty($rackflowServiceId)) {
        return null;
    }
    if (array_key_exists($rackflowServiceId, $cache)) {
        return $cache[$rackflowServiceId];
    }

    $apiConfig = rackflow_getApiConfig($params);
    $statusData = rackflow_fetchServiceStatus($apiConfig['url'], $apiConfig['key'], (int)$rackflowServiceId);
    if (!$statusData) {
        $cache[$rackflowServiceId] = null;
        return null;
    }

    $result = array(
        'permissions' => isset($statusData['client_permissions']) && is_array($statusData['client_permissions'])
            ? $statusData['client_permissions']
            : array(),
        'power_available' => !empty($statusData['power_available']),
    );
    $cache[$rackflowServiceId] = $result;
    return $result;
}

/**
 * Build a same-page URL with an extra query parameter (e.g. rackflow_ipmi=1).
 * Prefer a real href over inline onclick — WHMCS admin CSP often blocks onclick handlers.
 *
 * @param string $key
 * @param string $value
 * @return string
 */
function rackflow_urlWithQueryParam($key, $value)
{
    $params = $_GET;
    $params[$key] = $value;
    $script = isset($_SERVER['SCRIPT_NAME']) ? (string)$_SERVER['SCRIPT_NAME'] : '';
    $query = http_build_query($params);
    if ($script === '') {
        return '?' . $query;
    }
    return $script . '?' . $query;
}

/**
 * WHMCS install's URL base path (e.g. "/billing", or "" if installed at the
 * webroot), derived purely from filesystem paths so it works regardless of
 * which hook/context calls it and doesn't depend on any module param being
 * populated (WHMCS does NOT reliably pass a "systemurl" key into every
 * hook - e.g. it's absent from AdminServicesTabFields).
 *
 * This file lives at WHMCS_ROOT/modules/servers/rackflow/rackflow.php, so
 * three levels up from __DIR__ is WHMCS_ROOT on disk. Diffing that against
 * DOCUMENT_ROOT gives the URL path prefix nginx/Apache actually uses to
 * reach it.
 *
 * @return string|null Base path (may be ""), or null if it can't be determined.
 */
function rackflow_whmcsUrlBasePath()
{
    $docRoot = isset($_SERVER['DOCUMENT_ROOT']) ? rtrim((string)$_SERVER['DOCUMENT_ROOT'], '/') : '';
    if ($docRoot === '') {
        return null;
    }
    $whmcsRoot = realpath(__DIR__ . '/../../../');
    if ($whmcsRoot === false) {
        return null;
    }
    $whmcsRoot = rtrim($whmcsRoot, '/');
    if (strpos($whmcsRoot, $docRoot) !== 0) {
        return null;
    }
    return substr($whmcsRoot, strlen($docRoot));
}

/**
 * Same-origin URL for the one-click IPMI open redirect endpoint.
 *
 * Root-relative (no scheme/host) so admin/client stay on whatever host they
 * are already using (e.g. whmcs.lan.*) instead of jumping to the scheme/host
 * of SystemURL (often a public hostname that is broken/mis-TLS'd and surfaces
 * as ERR_HTTP2_PROTOCOL_ERROR). We DO still need the *path* portion though:
 * WHMCS is frequently installed in a subdirectory (e.g. "/billing"), not at
 * the domain root, and hardcoding "/" breaks that case with a "Primary
 * script unknown" / file-not-found from PHP-FPM.
 *
 * @param int    $whmcsServiceId tblhosting.id
 * @param string $systemUrl      Optional WHMCS "systemurl" module param, used
 *                                only as a fallback if the base path can't be
 *                                derived from the filesystem.
 * @return string
 */
function rackflow_ipmiOpenEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/ipmi_open.php?serviceid=' . (int)$whmcsServiceId;
}

/**
 * Same-origin URL for the AJAX backup create/delete/restore endpoint.
 *
 * @param int    $whmcsServiceId tblhosting.id (included for callers; POST body also sends it)
 * @param string $systemUrl
 * @return string
 */
function rackflow_backupActionEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    unset($whmcsServiceId); // service id is POSTed; kept for call-site symmetry with other helpers
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/backup_action.php';
}

/**
 * Same-origin URL for the AJAX virtual CD mount/eject endpoint.
 *
 * @param int    $whmcsServiceId
 * @param string $systemUrl
 * @return string
 */
function rackflow_virtualMediaActionEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    unset($whmcsServiceId);
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/virtual_media_action.php';
}

/**
 * Fetch BMC virtual-media status + ISO catalog from the billing API.
 *
 * @param string $apiUrl
 * @param string $apiKey
 * @param int    $rackflowSvcId
 * @return array|null
 */
function rackflow_fetchVirtualMedia($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return null;
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/services/' . (int)$rackflowSvcId . '/virtual-media', null);
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        return null;
    }
    return $result['data'];
}

/**
 * Normalize ISO catalog rows from billing or virtual-media payloads.
 *
 * @param mixed $rows
 * @return array<int, array{filename: string}>
 */
function rackflow_isoRowsFromPayload($rows)
{
    $out = array();
    if (!is_array($rows)) {
        return $out;
    }
    foreach ($rows as $iso) {
        if (!is_array($iso)) {
            continue;
        }
        $name = '';
        if (!empty($iso['filename'])) {
            $name = (string)$iso['filename'];
        } elseif (!empty($iso['name'])) {
            $name = (string)$iso['name'];
        } elseif (!empty($iso['id'])) {
            $name = (string)$iso['id'];
        }
        if ($name === '') {
            continue;
        }
        $out[] = array('filename' => $name);
    }
    return $out;
}

/**
 * ISO catalog from GET /api/billing/isos (WHMCS dropdowns when BMC status is empty).
 *
 * @param string $apiUrl
 * @param string $apiKey
 * @return array<int, array{filename: string}>
 */
function rackflow_fetchBillingIsos($apiUrl, $apiKey)
{
    if (empty($apiUrl) || empty($apiKey)) {
        return array();
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'GET', '/api/billing/isos', null);
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        return array();
    }
    return rackflow_isoRowsFromPayload($result['data']);
}

/**
 * Same-origin URL for the AJAX proxy credential rotation endpoint.
 *
 * @param int    $whmcsServiceId tblhosting.id (included for callers; POST body also sends it)
 * @param string $systemUrl
 * @return string
 */
function rackflow_proxyActionEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    unset($whmcsServiceId); // service id is POSTed; kept for call-site symmetry with other helpers
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/proxy_action.php';
}

/**
 * Mint a one-time IPMI proxy launch ticket for a service via the billing API.
 *
 * @param string $apiUrl        Base API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array Decoded launch payload, or array with 'error' key on failure
 */
function rackflow_mintIpmiTicket($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return array('error' => 'API URL, API key, or RackFlow service ID is missing.');
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services/' . (int)$rackflowSvcId . '/ipmi-ticket', array());
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        if (is_array($detail)) {
            $detail = json_encode($detail);
        }
        rackflow_log('mintIpmiTicket failed', array(
            'rackflow_service_id' => (int)$rackflowSvcId,
            'error' => (string)$detail,
            'http_code' => isset($result['http_code']) ? $result['http_code'] : null,
        ));
        return array('error' => (string)$detail);
    }
    return $result['data'];
}

/**
 * Same-origin URL for the one-click VNC console open redirect endpoint.
 * Mirrors {@see rackflow_ipmiOpenEndpointUrl()}.
 *
 * @param int    $whmcsServiceId tblhosting.id
 * @param string $systemUrl      Optional WHMCS "systemurl" module param, used
 *                                only as a fallback if the base path can't be
 *                                derived from the filesystem.
 * @return string
 */
function rackflow_vncOpenEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/vnc_open.php?serviceid=' . (int)$whmcsServiceId;
}

/**
 * Same-origin URL for the one-click HTML5 KVM console open redirect endpoint.
 * Mirrors {@see rackflow_vncOpenEndpointUrl()}.
 *
 * @param int    $whmcsServiceId tblhosting.id
 * @param string $systemUrl      Optional WHMCS "systemurl" module param, used
 *                                only as a fallback if the base path can't be
 *                                derived from the filesystem.
 * @return string
 */
function rackflow_kvmOpenEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/kvm_open.php?serviceid=' . (int)$whmcsServiceId;
}

/**
 * Same-origin URL for the one-click Serial-over-LAN console open redirect endpoint.
 * Mirrors {@see rackflow_kvmOpenEndpointUrl()}.
 *
 * @param int    $whmcsServiceId tblhosting.id
 * @param string $systemUrl     Optional WHMCS system URL when the base path cannot be
 *                                derived from the filesystem.
 * @return string
 */
function rackflow_solOpenEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/sol_open.php?serviceid=' . (int)$whmcsServiceId;
}

/**
 * Mint a one-time IPMI HTML5 KVM launch ticket for a service via the billing API.
 *
 * @param string $apiUrl        Base API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array Decoded launch payload, or array with 'error' key on failure
 */
function rackflow_mintKvmTicket($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return array('error' => 'API URL, API key, or RackFlow service ID is missing.');
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services/' . (int)$rackflowSvcId . '/kvm-ticket', array());
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        if (is_array($detail)) {
            $detail = json_encode($detail);
        }
        rackflow_log('mintKvmTicket failed', array(
            'rackflow_service_id' => (int)$rackflowSvcId,
            'error' => (string)$detail,
            'http_code' => isset($result['http_code']) ? $result['http_code'] : null,
        ));
        return array('error' => (string)$detail);
    }
    return $result['data'];
}

/**
 * Mint a one-time Serial-over-LAN launch ticket for a service via the billing API.
 *
 * @param string $apiUrl        Base API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array Decoded launch payload, or array with 'error' key on failure
 */
function rackflow_mintSolTicket($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return array('error' => 'API URL, API key, or RackFlow service ID is missing.');
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services/' . (int)$rackflowSvcId . '/sol-ticket', array());
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        if (is_array($detail)) {
            $detail = json_encode($detail);
        }
        rackflow_log('mintSolTicket failed', array(
            'rackflow_service_id' => (int)$rackflowSvcId,
            'error' => (string)$detail,
            'http_code' => isset($result['http_code']) ? $result['http_code'] : null,
        ));
        return array('error' => (string)$detail);
    }
    return $result['data'];
}

/**
 * Mint a one-time VM VNC console launch ticket for a service via the billing API.
 *
 * @param string $apiUrl        Base API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array Decoded launch payload, or array with 'error' key on failure
 */
function rackflow_mintVncTicket($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return array('error' => 'API URL, API key, or RackFlow service ID is missing.');
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services/' . (int)$rackflowSvcId . '/vnc-ticket', array());
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        if (is_array($detail)) {
            $detail = json_encode($detail);
        }
        rackflow_log('mintVncTicket failed', array(
            'rackflow_service_id' => (int)$rackflowSvcId,
            'error' => (string)$detail,
            'http_code' => isset($result['http_code']) ? $result['http_code'] : null,
        ));
        return array('error' => (string)$detail);
    }
    return $result['data'];
}

/**
 * Same-origin URL for the one-click "Open client portal" redirect endpoint.
 * Mirrors {@see rackflow_ipmiOpenEndpointUrl()}.
 *
 * @param int    $whmcsServiceId tblhosting.id
 * @param string $systemUrl      Optional WHMCS "systemurl" module param fallback.
 * @return string
 */
function rackflow_portalOpenEndpointUrl($whmcsServiceId, $systemUrl = '')
{
    $basePath = rackflow_whmcsUrlBasePath();
    if ($basePath === null) {
        $basePath = '';
        if (!empty($systemUrl)) {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/portal_open.php?serviceid=' . (int)$whmcsServiceId;
}

/**
 * Mint a one-time client-portal SSO ticket for a service via the billing API,
 * and build the RackFlow-side redeem URL the browser should land on.
 *
 * @param string $apiUrl        Base RackFlow API URL
 * @param string $apiKey        Billing API key
 * @param int    $rackflowSvcId RackFlow service ID
 * @return array{redeem_url?: string, error?: string}
 */
function rackflow_mintPortalSsoTicket($apiUrl, $apiKey, $rackflowSvcId)
{
    if (empty($apiUrl) || empty($apiKey) || empty($rackflowSvcId)) {
        return array('error' => 'API URL, API key, or RackFlow service ID is missing.');
    }
    $result = rackflow_apiCall($apiUrl, $apiKey, 'POST', '/api/billing/services/' . (int)$rackflowSvcId . '/portal-sso', array());
    if (!$result['success'] || !isset($result['data']) || !is_array($result['data']) || empty($result['data']['token'])) {
        $err = isset($result['error']) ? $result['error'] : 'Unknown error';
        $detail = is_array($result['data']) && isset($result['data']['detail']) ? $result['data']['detail'] : $err;
        if (is_array($detail)) {
            $detail = json_encode($detail);
        }
        rackflow_log('mintPortalSsoTicket failed', array(
            'rackflow_service_id' => (int)$rackflowSvcId,
            'error' => (string)$detail,
            'http_code' => isset($result['http_code']) ? $result['http_code'] : null,
        ));
        return array('error' => (string)$detail);
    }
    $redeemPath = !empty($result['data']['redeem_path']) ? (string)$result['data']['redeem_path'] : '/api/client/sso/redeem';
    $redeemUrl = rtrim($apiUrl, '/') . $redeemPath . '?token=' . urlencode((string)$result['data']['token']);
    return array('redeem_url' => $redeemUrl);
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
    $rackflowSvcId = trim($value);
    if ($rackflowSvcId === '' || !is_numeric($rackflowSvcId)) {
        // Fall back to custom-field helper (same source ClientArea uses).
        $linkedId = rackflow_getRackflowServiceId($params);
        if (!empty($linkedId)) {
            $rackflowSvcId = (string)$linkedId;
        }
    }

    $statusData = null;
    $statusError = '';
    if ($rackflowSvcId !== '' && is_numeric($rackflowSvcId)) {
        $apiConfig = rackflow_getApiConfig($params);
        $statusData = rackflow_fetchServiceStatus($apiConfig['url'], $apiConfig['key'], (int)$rackflowSvcId);
        if (!$statusData) {
            $statusError = 'Could not load status (check API and RackFlow service ID).';
        }
    } else {
        $statusError = 'Link a RackFlow service to see live server status.';
    }

    $out['RackFlow Server Status'] = rackflow_renderAdminStatusCard(
        $params,
        $serviceId,
        $statusData,
        $statusError,
        $rackflowSvcId
    );

    return $out;
}

/**
 * Build the admin "RackFlow Server Status" card (matches client-area / Module Settings look).
 *
 * @param array       $params        WHMCS module params
 * @param int         $serviceId     WHMCS hosting service id
 * @param array|null  $statusData    billing /status payload or null
 * @param string      $statusError   empty when $statusData is present
 * @param string      $rackflowSvcId linked RackFlow service id (display only)
 * @return string HTML
 */
function rackflow_renderAdminStatusCard(array $params, $serviceId, $statusData, $statusError, $rackflowSvcId)
{
    $serviceType = rackflow_getConfiguredServiceType($params);
    $typeLabel = $serviceType === 'vm' ? 'Virtual machine' : ($serviceType === 'http_proxy' ? 'HTTP proxy' : 'Bare metal');
    $linked = ($rackflowSvcId !== '' && is_numeric($rackflowSvcId));
    $rfIdText = $linked ? 'RackFlow #' . (int)$rackflowSvcId : 'Not linked';
    $packageId = isset($params['packageid']) ? (int)$params['packageid'] : (isset($params['pid']) ? (int)$params['pid'] : 0);
    $apiConfig = rackflow_getApiConfig($params);
    $apiUrl = isset($apiConfig['url']) ? (string)$apiConfig['url'] : '';
    $systemUrl = isset($params['systemurl']) ? (string)$params['systemurl'] : '';
    $adminLinkUrl = rackflow_adminLinkEndpointUrl($systemUrl);
    $openUrl = $linked ? rackflow_adminServicePageUrl($apiUrl, (int)$rackflowSvcId) : '';

    $clusterId = '';
    $nodeName = '';
    $vmid = '';
    if (is_array($statusData)) {
        if (isset($statusData['proxmox_cluster_id']) && $statusData['proxmox_cluster_id'] !== null && $statusData['proxmox_cluster_id'] !== '') {
            $clusterId = (string)(int)$statusData['proxmox_cluster_id'];
        }
        if (isset($statusData['proxmox_node_name']) && $statusData['proxmox_node_name'] !== null) {
            $nodeName = (string)$statusData['proxmox_node_name'];
        }
        if (isset($statusData['proxmox_vmid']) && $statusData['proxmox_vmid'] !== null && $statusData['proxmox_vmid'] !== '') {
            $vmid = (string)(int)$statusData['proxmox_vmid'];
        }
    }

    $h = function ($v) {
        return htmlspecialchars((string)$v, ENT_QUOTES, 'UTF-8');
    };

    $attrs = ' id="rackflow-admin-status"'
        . ' data-serviceid="' . (int)$serviceId . '"'
        . ' data-packageid="' . (int)$packageId . '"'
        . ' data-service-type="' . $h($serviceType) . '"'
        . ' data-rackflow-id="' . ($linked ? (int)$rackflowSvcId : '') . '"'
        . ' data-admin-link-url="' . $h($adminLinkUrl) . '"'
        . ' data-open-url="' . $h($openUrl) . '"'
        . ' data-cluster-id="' . $h($clusterId) . '"'
        . ' data-node-name="' . $h($nodeName) . '"'
        . ' data-vmid="' . $h($vmid) . '"';

    $headActions = '<div class="rf-as__head-actions">';
    if ($openUrl !== '') {
        $headActions .= '<a href="' . $h($openUrl) . '" target="_blank" rel="noopener" class="rf-as__btn rf-as__btn--secondary" id="rf-as-open">Open in RackFlow</a>';
    } else {
        $headActions .= '<a href="#" class="rf-as__btn rf-as__btn--secondary" id="rf-as-open" hidden>Open in RackFlow</a>';
    }
    $headActions .= '</div>';

    $badgeClass = 'rf-as__badge--unknown';
    $statusLabel = 'unknown';
    if (is_array($statusData)) {
        $statusLabel = isset($statusData['status'])
            ? (string)$statusData['status']
            : (isset($statusData['power_state']) ? strtolower((string)$statusData['power_state']) : 'unknown');
        $statusLower = strtolower($statusLabel);
        if ($statusLower === 'on') {
            $badgeClass = 'rf-as__badge--on';
        } elseif ($statusLower === 'off') {
            $badgeClass = 'rf-as__badge--off';
        } elseif ($statusLower === 'suspended') {
            $badgeClass = 'rf-as__badge--warn';
        }
    }

    $html = '<div class="rf-as"' . $attrs . '>'
        . '<div class="rf-as__card">'
        . '<div class="rf-as__head">'
        . '<div><h3 class="rf-as__title">Server overview</h3>'
        . '<p class="rf-as__subtitle" id="rf-as-subtitle">' . $h($typeLabel) . ' · <span id="rf-as-link-label">' . $h($rfIdText) . '</span>'
        . (is_array($statusData) ? ' · Live from RackFlow' : '') . '</p></div>'
        . '<div class="rf-as__head-right">'
        . $headActions
        . '<span class="rf-as__badge ' . $h($badgeClass) . '" id="rf-as-power-badge">'
        . '<span class="rf-as__badge-dot" aria-hidden="true"></span><span id="rf-as-power-label">' . $h($statusLabel) . '</span></span>'
        . '</div></div>'
        . '<div class="rf-as__body">';

    if (!is_array($statusData)) {
        $html .= '<p class="rf-as__note" id="rf-as-status-note">' . $h($statusError) . '</p>';
    } else {
        $serviceStatus = isset($statusData['service_status']) ? (string)$statusData['service_status'] : '—';
        $serverName = isset($statusData['server_name']) && $statusData['server_name'] !== null && $statusData['server_name'] !== ''
            ? (string)$statusData['server_name'] : '—';
        $serverEnabled = isset($statusData['server_enabled']) ? (bool)$statusData['server_enabled'] : null;
        $enabledText = $serverEnabled === true ? 'Yes' : ($serverEnabled === false ? 'No' : '—');
        $installStatus = '—';
        if (isset($statusData['installation']) && is_array($statusData['installation'])) {
            $inst = $statusData['installation'];
            $istatus = isset($inst['status']) ? (string)$inst['status'] : '';
            $iprogress = isset($inst['progress_percent']) && $inst['progress_percent'] !== null ? (int)$inst['progress_percent'] : null;
            if ($istatus !== '') {
                $installStatus = $istatus;
                if ($iprogress !== null) {
                    $installStatus .= ' (' . $iprogress . '%)';
                }
            }
        }

        $html .= '<div class="rf-as__grid" id="rf-as-stats">'
            . '<div class="rf-as__stat"><span class="rf-as__stat-label">Service status</span>'
            . '<p class="rf-as__stat-value">' . $h($serviceStatus) . '</p></div>'
            . '<div class="rf-as__stat"><span class="rf-as__stat-label">Server</span>'
            . '<p class="rf-as__stat-value">' . $h($serverName) . '</p></div>'
            . '<div class="rf-as__stat"><span class="rf-as__stat-label">Server enabled</span>'
            . '<p class="rf-as__stat-value">' . $h($enabledText) . '</p></div>'
            . '<div class="rf-as__stat"><span class="rf-as__stat-label">Installation</span>'
            . '<p class="rf-as__stat-value">' . $h($installStatus) . '</p></div>'
            . '</div>';

        $actions = '';
        if (!empty($statusData['ipmi_proxy_available'])) {
            $user = isset($statusData['ipmi_viewer_username']) ? (string)$statusData['ipmi_viewer_username'] : '';
            $pass = isset($statusData['ipmi_viewer_password']) ? (string)$statusData['ipmi_viewer_password'] : '';
            $creds = '';
            if ($user !== '' || $pass !== '') {
                $creds = '<p class="rf-as__creds">';
                if ($user !== '') {
                    $creds .= '<code>' . $h($user) . '</code>';
                }
                if ($user !== '' && $pass !== '') {
                    $creds .= ' / ';
                }
                if ($pass !== '') {
                    $creds .= '<code>' . $h($pass) . '</code>';
                }
                $creds .= '</p>';
            }
            $openHref = $h(rackflow_ipmiOpenEndpointUrl($serviceId, $systemUrl));
            $actions .= '<div class="rf-as__action">'
                . '<div class="rf-as__action-copy"><p class="rf-as__action-title">IPMI console</p>'
                . '<p class="rf-as__action-help">Opens in a new tab. The console link is single-use and expires shortly.</p>'
                . $creds . '</div>'
                . '<a href="' . $openHref . '" target="_blank" rel="noopener" class="rf-as__btn rf-as__btn--secondary">Open IPMI</a>'
                . '</div>';
        }
        if (!empty($statusData['vnc_console_available'])) {
            $vncHref = $h(rackflow_vncOpenEndpointUrl($serviceId, $systemUrl));
            $actions .= '<div class="rf-as__action">'
                . '<div class="rf-as__action-copy"><p class="rf-as__action-title">VNC console</p>'
                . '<p class="rf-as__action-help">Opens in a new tab. The console link is single-use and expires shortly.</p></div>'
                . '<a href="' . $vncHref . '" target="_blank" rel="noopener" class="rf-as__btn rf-as__btn--secondary">Open VNC</a>'
                . '</div>';
        }
        if (!empty($statusData['kvm_console_available'])) {
            $kvmHref = $h(rackflow_kvmOpenEndpointUrl($serviceId, $systemUrl));
            $actions .= '<div class="rf-as__action">'
                . '<div class="rf-as__action-copy"><p class="rf-as__action-title">HTML5 KVM</p>'
                . '<p class="rf-as__action-help">Opens in a new tab. The console link is single-use and expires shortly.</p></div>'
                . '<a href="' . $kvmHref . '" target="_blank" rel="noopener" class="rf-as__btn rf-as__btn--secondary">Open KVM</a>'
                . '</div>';
        }
        if (!empty($statusData['sol_console_available'])) {
            $solHref = $h(rackflow_solOpenEndpointUrl($serviceId, $systemUrl));
            $actions .= '<div class="rf-as__action">'
                . '<div class="rf-as__action-copy"><p class="rf-as__action-title">Serial-over-LAN</p>'
                . '<p class="rf-as__action-help">Opens in a new tab. The console link is single-use and expires shortly.</p></div>'
                . '<a href="' . $solHref . '" target="_blank" rel="noopener" class="rf-as__btn rf-as__btn--secondary">Open Serial</a>'
                . '</div>';
        }
        if ($actions !== '') {
            $html .= '<div class="rf-as__actions">' . $actions . '</div>';
        }

        if (!empty($statusData['virtual_media_available'])) {
            $media = rackflow_fetchVirtualMedia($apiConfig['url'], $apiConfig['key'], (int)$rackflowSvcId);
            $isos = rackflow_isoRowsFromPayload(($media && isset($media['isos'])) ? $media['isos'] : array());
            if (empty($isos)) {
                $isos = rackflow_fetchBillingIsos($apiConfig['url'], $apiConfig['key']);
            }
            $inserted = $media && !empty($media['inserted']);
            $imageName = ($media && !empty($media['image_name'])) ? (string)$media['image_name'] : '';
            $actionUrl = $h(rackflow_virtualMediaActionEndpointUrl($serviceId, $systemUrl));
            $html .= '<div class="rf-as__panel" id="rf-as-virtual-media-panel"'
                . ' data-rf-virtual-media-action="' . $actionUrl . '"'
                . ' data-rf-service-id="' . (int)$serviceId . '">'
                . '<p class="rf-as__panel-title">Virtual CD</p>'
                . '<p class="rf-as__note" id="rf-as-virtual-media-status">'
                . ($inserted ? ('Mounted: ' . $h($imageName !== '' ? $imageName : 'ISO')) : 'No virtual CD inserted')
                . '</p>'
                . '<div class="rf-as__reinstall-form">'
                . '<div class="rf-as__reinstall-row">'
                . '<div class="rf-as__reinstall-field">'
                . '<label class="rf-as__stat-label" for="rf-as-virtual-media-iso">ISO</label>'
                . '<select id="rf-as-virtual-media-iso" class="rf-as__input">';
            $html .= '<option value="">Select ISO</option>';
            foreach ($isos as $iso) {
                $name = isset($iso['filename']) ? (string)$iso['filename'] : (isset($iso['name']) ? (string)$iso['name'] : '');
                if ($name === '') {
                    continue;
                }
                $html .= '<option value="' . $h($name) . '">' . $h($name) . '</option>';
            }
            $html .= '</select></div></div>'
                . '<label class="rf-as__note"><input type="checkbox" id="rf-as-virtual-media-boot-once" /> Set next boot to CD-ROM</label>'
                . '<div class="rf-as__reinstall-row">'
                . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-virtual-media-mount">Mount</button>'
                . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-virtual-media-eject">Eject</button>'
                . '</div>'
                . '<p class="rf-as__note" id="rf-as-virtual-media-msg" hidden></p>'
                . '</div></div>';
        }

        // HTTP/SOCKS proxy: assigned IP(s) + credentials + ready-to-use URLs,
        // with an optional rotate action (billing API gates both on the
        // proxy.view_credentials / proxy.rotate_credentials client permissions).
        if ($serviceType === 'http_proxy' && !empty($statusData['proxy_assignments']) && is_array($statusData['proxy_assignments'])) {
            $rotateAvailable = !empty($statusData['proxy_rotate_available']);
            $endpointLines = rackflow_proxyEndpointLines($statusData['proxy_assignments']);
            $html .= '<div class="rf-as__panel" id="rf-as-proxy-panel"'
                . ' data-rf-service-id="' . (int)$serviceId . '"'
                . ' data-rf-rackflow-id="' . ($linked ? (int)$rackflowSvcId : '') . '">'
                . '<div class="rf-as__panel-head">'
                . '<p class="rf-as__panel-title">Proxy access</p>'
                . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-proxy-copy-all">Copy all (ip:port:user:pass)</button>'
                . '</div>'
                . '<textarea id="rf-as-proxy-endpoint-lines" class="rf-as__sr-only" readonly aria-hidden="true">'
                . $h($endpointLines) . '</textarea>';
            foreach ($statusData['proxy_assignments'] as $assignment) {
                $ip = isset($assignment['ip_address']) ? (string)$assignment['ip_address'] : '';
                $user = isset($assignment['username']) ? (string)$assignment['username'] : '';
                $pass = isset($assignment['password']) ? (string)$assignment['password'] : '';
                $httpUrl = isset($assignment['http_url']) ? (string)$assignment['http_url'] : '';
                $socksUrl = isset($assignment['socks5_url']) ? (string)$assignment['socks5_url'] : '';
                $html .= '<div class="rf-as__proxy-row">'
                    . '<p class="rf-as__creds"><code>' . $h($ip) . '</code>'
                    . ($user !== '' ? ' · <code>' . $h($user) . '</code>' : '')
                    . ($pass !== '' ? ' / <code>' . $h($pass) . '</code>' : '')
                    . '</p>';
                if ($httpUrl !== '') {
                    $html .= '<p class="rf-as__proxy-url"><code>' . $h($httpUrl) . '</code></p>';
                }
                if ($socksUrl !== '') {
                    $html .= '<p class="rf-as__proxy-url"><code>' . $h($socksUrl) . '</code></p>';
                }
                $html .= '</div>';
            }
            if ($rotateAvailable) {
                $html .= '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-proxy-rotate">Rotate credentials</button>';
            }
            $html .= '<p class="rf-as__note" id="rf-as-proxy-msg" hidden></p>';
            $html .= '</div>';
        }
    }

    // VM IP reassignment + reinstall + backups (admin view)
    $isVm = ($serviceType === 'vm');
    if ($isVm && $linked) {
        $currentIp = '';
        if (is_array($statusData) && !empty($statusData['vm_ip_address'])) {
            $currentIp = (string)$statusData['vm_ip_address'];
        }
        $ipActionUrl = rackflow_ipActionEndpointUrl((int)$serviceId, $systemUrl);
        $html .= '<div class="rf-as__panel" id="rf-as-ip-panel"'
            . ' data-rf-ip-action="' . $h($ipActionUrl) . '"'
            . ' data-rf-service-id="' . (int)$serviceId . '">'
            . '<div class="rf-as__panel-head">'
            . '<h4 class="rf-as__panel-title">Change IP</h4>'
            . '<p class="rf-as__panel-help">Browse free pool IPs for this Proxmox cluster, assign one, and release the current address. '
            . 'Resetting guest network on Linux cloud-init guests regenerates cloud-init and reboots the VM.</p>'
            . '</div>'
            . '<div class="rf-as__ip-current-row">'
            . '<div class="rf-as__ip-current">'
            . '<span class="rf-as__stat-label">Current IP</span>'
            . '<span class="rf-as__stat-value" id="rf-as-ip-current">' . $h($currentIp !== '' ? $currentIp : '—') . '</span>'
            . '</div>'
            . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-ip-refresh">Browse available IPs</button>'
            . '</div>'
            . '<div class="rf-as__reinstall-form" id="rf-as-ip-form" hidden>'
            . '<div class="rf-as__reinstall-row">'
            . '<div class="rf-as__reinstall-field">'
            . '<label class="rf-as__stat-label" for="rf-as-ip-select">New IP</label>'
            . '<select id="rf-as-ip-select" class="rf-as__input"></select></div>'
            . '<button type="button" class="rf-as__btn rf-as__btn--danger" id="rf-as-ip-apply"'
            . ' data-rf-confirm="Assign the selected IP, release the current one, and reset guest networking? Cloud-init guests will reboot.">Assign IP</button>'
            . '</div>'
            . '<label class="rf-as__stat-label" style="display:flex;align-items:center;gap:8px;font-weight:500;">'
            . '<input type="checkbox" id="rf-as-ip-reset" checked /> Reset guest network after assign</label>'
            . '</div>'
            . '<p class="rf-as__note" id="rf-as-ip-msg" hidden></p>'
            . '</div>';

        $reinstallOpts = rackflow_fetchReinstallOptions($params, 'admin');
        $reinstallActionUrl = rackflow_reinstallActionEndpointUrl((int)$serviceId, $systemUrl);
        $currentTmplId = isset($reinstallOpts['vm_template_id']) && $reinstallOpts['vm_template_id'] !== null
            ? (string)(int)$reinstallOpts['vm_template_id']
            : '';
        $html .= '<div class="rf-as__panel" id="rf-as-reinstall-panel"'
            . ' data-rf-reinstall-action="' . $h($reinstallActionUrl) . '"'
            . ' data-rf-service-id="' . (int)$serviceId . '">'
            . '<div class="rf-as__panel-head">'
            . '<h4 class="rf-as__panel-title">Reinstall</h4>'
            . '<p class="rf-as__panel-help">Destroy the guest and reprovision at the same reserved VMID. Optional template change applies before recreate.</p>'
            . '</div>';
        if (empty($reinstallOpts['success'])) {
            $html .= '<p class="rf-as__note">' . $h(isset($reinstallOpts['error']) ? $reinstallOpts['error'] : 'Unable to load templates') . '</p>';
        } else {
            $sshText = isset($reinstallOpts['ssh_public_keys_text']) ? (string)$reinstallOpts['ssh_public_keys_text'] : '';
            $html .= '<div class="rf-as__reinstall-form">'
                . '<div class="rf-as__reinstall-row">'
                . '<div class="rf-as__reinstall-field">'
                . '<label class="rf-as__stat-label" for="rf-as-reinstall-template">Template</label>'
                . '<select id="rf-as-reinstall-template" class="rf-as__input">';
            $html .= '<option value="">Keep current template</option>';
            $templates = isset($reinstallOpts['templates']) && is_array($reinstallOpts['templates'])
                ? $reinstallOpts['templates']
                : array();
            foreach ($templates as $tmpl) {
                if (!is_array($tmpl) || empty($tmpl['id'])) {
                    continue;
                }
                $tid = (string)(int)$tmpl['id'];
                $tname = isset($tmpl['name']) ? (string)$tmpl['name'] : ('Template #' . $tid);
                $accepts = !empty($tmpl['accepts_ssh_key']) ? '1' : '0';
                $sel = ($currentTmplId !== '' && $tid === $currentTmplId) ? ' selected' : '';
                $html .= '<option value="' . $h($tid) . '" data-accepts-ssh="' . $accepts . '"' . $sel . '>'
                    . $h($tname) . '</option>';
            }
            $html .= '</select></div>'
                . '<button type="button" class="rf-as__btn rf-as__btn--danger" id="rf-as-reinstall-btn"'
                . ' data-rf-confirm="Reinstall this VM? The guest will be destroyed and rebuilt at the same VMID.">Reinstall</button>'
                . '</div>'
                . '<div class="rf-as__reinstall-field" id="rf-as-reinstall-ssh-wrap">'
                . '<label class="rf-as__stat-label" for="rf-as-reinstall-ssh">SSH public keys <span style="font-weight:500;color:var(--rf-muted)">(optional)</span></label>'
                . '<textarea id="rf-as-reinstall-ssh" class="rf-as__input rf-as__textarea" rows="3" placeholder="One OpenSSH public key per line">'
                . $h($sshText) . '</textarea>'
                . '</div>'
                . '<p class="rf-as__note" id="rf-as-reinstall-msg" hidden></p>'
                . '</div>';
        }
        $html .= '</div>';

        $backupFetch = rackflow_fetchBackups($params, 'admin');
        $backupActionUrl = rackflow_backupActionEndpointUrl((int)$serviceId, $systemUrl);
        $html .= '<div class="rf-as__panel" id="rf-as-backups-panel"'
            . ' data-rf-backup-action="' . $h($backupActionUrl) . '"'
            . ' data-rf-service-id="' . (int)$serviceId . '">'
            . '<div class="rf-as__panel-head">'
            . '<h4 class="rf-as__panel-title">VM backups</h4>'
            . '<p class="rf-as__panel-help">Scheduled backups are provider-owned in Proxmox (read-only). Customer backups use the product on-demand storage; the name is stored as the PBS note.</p>'
            . '</div>'
            . '<div class="rf-as__backup-create">'
            . '<input type="text" id="rf-as-backup-notes" class="rf-as__input" placeholder="Backup name / notes" maxlength="200" autocomplete="off" />'
            . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-backup-create">Create backup</button>'
            . '</div>'
            . '<p class="rf-as__note" id="rf-as-backup-msg" hidden></p>';
        $jobs = (!empty($backupFetch['success']) && !empty($backupFetch['jobs']) && is_array($backupFetch['jobs']))
            ? $backupFetch['jobs']
            : array();
        if (!empty($jobs)) {
            $html .= '<p class="rf-as__panel-help"><strong>Running jobs</strong></p>'
                . '<ul class="rf-as__backup-list">';
            foreach ($jobs as $j) {
                if (!is_array($j)) {
                    continue;
                }
                $scope = isset($j['scope']) ? (string)$j['scope'] : (isset($j['kind']) ? (string)$j['kind'] : 'backup');
                $scopeClass = preg_replace('/[^a-z0-9_-]/i', '', strtolower($scope));
                $jstatus = isset($j['status']) ? (string)$j['status'] : 'RUNNING';
                $jstorage = isset($j['storage']) ? (string)$j['storage'] : '';
                $jstart = isset($j['starttime']) ? (int)$j['starttime'] : 0;
                $jwhen = $jstart > 0 ? date('Y-m-d H:i', $jstart) : '';
                $html .= '<li class="rf-as__backup-item rf-as__backup-item--running rf-as__backup-item--' . $h($scopeClass) . '">'
                    . '<span class="rf-as__backup-kind rf-as__backup-kind--' . $h($scopeClass) . '">'
                    . $h(rackflow_backupKindLabel($scope)) . '</span>'
                    . '<span>' . $h($jstatus) . '</span>';
                if ($jwhen !== '') {
                    $html .= '<span>' . $h($jwhen) . '</span>';
                }
                if ($jstorage !== '') {
                    $html .= '<span class="rf-as__backup-storage">' . $h($jstorage) . '</span>';
                }
                $html .= '</li>';
            }
            $html .= '</ul>';
        }
        if (empty($backupFetch['success'])) {
            $html .= '<p class="rf-as__note">' . $h(isset($backupFetch['error']) ? $backupFetch['error'] : 'Unable to load backups') . '</p>';
        } elseif (empty($backupFetch['backups'])) {
            $html .= '<p class="rf-as__note">No backups found for this VMID.</p>';
        } else {
            $html .= '<ul class="rf-as__backup-list">';
            foreach ($backupFetch['backups'] as $b) {
                if (!is_array($b)) {
                    continue;
                }
                $kind = isset($b['kind']) ? (string)$b['kind'] : '';
                $kindClass = preg_replace('/[^a-z0-9_-]/i', '', strtolower($kind));
                $volid = isset($b['volid']) ? (string)$b['volid'] : '';
                $storage = isset($b['storage']) ? (string)$b['storage'] : '';
                $notes = isset($b['notes']) && $b['notes'] !== null && $b['notes'] !== ''
                    ? (string)$b['notes']
                    : '';
                $notesDisplay = rackflow_backupNotesDisplay($notes);
                $templateName = isset($b['template_name']) && $b['template_name'] !== null && $b['template_name'] !== ''
                    ? (string)$b['template_name']
                    : '';
                $ctime = isset($b['ctime']) ? (int)$b['ctime'] : 0;
                $when = $ctime > 0 ? date('Y-m-d H:i', $ctime) : '—';
                $deletable = !empty($b['deletable']);
                $isRunning = !empty($b['running']);
                $itemClass = 'rf-as__backup-item rf-as__backup-item--' . $kindClass
                    . ($isRunning ? ' rf-as__backup-item--running' : '');
                $html .= '<li class="' . $h($itemClass) . '">'
                    . '<span class="rf-as__backup-kind rf-as__backup-kind--' . $h($kindClass) . '">'
                    . $h(rackflow_backupKindLabel($kind)) . '</span>'
                    . '<span class="rf-as__backup-when">' . $h($when) . '</span>'
                    . ($templateName !== '' ? '<span class="rf-as__backup-template">' . $h($templateName) . '</span>' : '')
                    . ($notesDisplay !== '' ? '<span class="rf-as__backup-title">' . $h($notesDisplay) . '</span>' : '')
                    . '<span class="rf-as__backup-storage">' . $h($storage) . '</span>';
                if ($isRunning) {
                    $html .= '<span class="rf-as__backup-running-label">Running</span>';
                } else {
                    $html .= '<button type="button" class="rf-as__btn rf-as__btn--secondary" data-rf-backup-op="restore" data-rf-volid="'
                        . $h($volid) . '" data-rf-storage="' . $h($storage)
                        . '" data-rf-confirm="Restore this backup onto the current VMID?">Restore</button>';
                    if ($deletable) {
                        $html .= '<button type="button" class="rf-as__btn rf-as__btn--danger" data-rf-backup-op="delete" data-rf-volid="'
                            . $h($volid) . '" data-rf-storage="' . $h($storage)
                            . '" data-rf-confirm="Delete this customer backup?">Delete</button>';
                    }
                }
                $html .= '</li>';
            }
            $html .= '</ul>';
        }
        $html .= '</div>';
    }

    // Link management (always shown). VM search is optimistic (id / VMID / name /
    // live Proxmox guests); bare metal uses id / IP.
    $linkHelp = $isVm
        ? 'Search by RackFlow service ID, Proxmox VMID, or VM name (partial matches). Unmanaged Proxmox guests can be adopted and linked.'
        : 'Search by RackFlow service ID or server IP (partial matches).';
    $searchPlaceholder = $isVm ? 'e.g. 2104, vm-name, or service #20' : 'e.g. 8 or 192.168.1.10';

    $html .= '<div class="rf-as__panel" id="rf-as-link-panel">'
        . '<div class="rf-as__panel-head">'
        . '<h4 class="rf-as__panel-title">RackFlow link</h4>'
        . '<p class="rf-as__panel-help">' . $h($linkHelp) . '</p>'
        . '</div>'
        . '<div class="rf-as__link-row">'
        . '<div class="rf-as__link-current">'
        . '<span class="rf-as__stat-label">Current link</span>'
        . '<p class="rf-as__stat-value" id="rf-as-current-link">' . $h($rfIdText) . '</p>'
        . '</div>'
        . '<button type="button" class="rf-as__btn rf-as__btn--danger" id="rf-as-unlink"'
        . ($linked ? '' : ' hidden') . '>Unlink</button>'
        . '</div>'
        . '<div class="rf-as__search">'
        . '<div class="rf-as__seg" role="group" aria-label="Search by">'
        . '<label class="rf-as__seg-opt"><input type="radio" name="rf_as_qtype" value="any" checked/> Any</label>'
        . '<label class="rf-as__seg-opt"><input type="radio" name="rf_as_qtype" value="service_id"/> Service ID</label>';
    if ($isVm) {
        $html .= '<label class="rf-as__seg-opt"><input type="radio" name="rf_as_qtype" value="vmid"/> VMID</label>';
    } else {
        $html .= '<label class="rf-as__seg-opt"><input type="radio" name="rf_as_qtype" value="server_ip"/> Server IP</label>';
    }
    $html .= '</div>'
        . '<div class="rf-as__search-row">'
        . '<input type="text" class="rf-as__input" id="rf-as-search-q" placeholder="' . $h($searchPlaceholder) . '" autocomplete="off"/>'
        . '<button type="button" class="rf-as__btn" id="rf-as-search-btn">Search</button>'
        . '</div>'
        . '<div class="rf-as__results" id="rf-as-results" hidden></div>'
        . '<p class="rf-as__msg" id="rf-as-msg" hidden></p>'
        . '</div>';

    if ($isVm) {
        $html .= '<div class="rf-as__vmid" id="rf-as-vmid-panel"' . ($linked && $clusterId !== '' && $nodeName !== '' ? '' : ' hidden') . '>'
            . '<div class="rf-as__panel-head">'
            . '<h4 class="rf-as__panel-title">Proxmox VMID</h4>'
            . '<p class="rf-as__panel-help">Updates the VMID on the linked RackFlow service (cluster/node stay the same).</p>'
            . '</div>'
            . '<div class="rf-as__search-row">'
            . '<input type="number" class="rf-as__input" id="rf-as-vmid-input" min="1" step="1" value="' . $h($vmid) . '" placeholder="VMID"/>'
            . '<button type="button" class="rf-as__btn rf-as__btn--secondary" id="rf-as-vmid-save">Save VMID</button>'
            . '</div>'
            . '</div>';
    }

    $html .= '</div>'
        . '<p class="rf-as__foot">Reload the page to refresh live status after linking.</p>'
        . '</div></div></div>';

    return $html;
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
        $existingField = \Illuminate\Database\Capsule\Manager::table('tblcustomfields')
            ->where('relid', $packageId)
            ->where('fieldname', RACKFLOW_SERVICE_ID_FIELD_NAME)
            ->first();
        $weCreatedField = !($existingField && isset($existingField->id));
        $fieldId = rackflow_ensureServiceIdCustomField($packageId);
        if (empty($fieldId)) {
            return;
        }
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
