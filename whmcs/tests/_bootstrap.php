<?php
/**
 * Minimal PHPUnit bootstrap for the RackFlow WHMCS module's dependency-free
 * helper functions (URL builders, log redaction, etc).
 *
 * This intentionally does NOT stand up a full WHMCS runtime (Capsule/ORM,
 * localAPI, session auth) — those parts of rackflow.php/admin_link.php/
 * proxy_action.php are exercised indirectly via the Rackflow API's own
 * Python test suite (tests/api/test_billing_proxy_service.py,
 * tests/api/test_client_proxy_credentials.py) plus manual WHMCS QA. Here we
 * only cover the pure PHP logic that runs before any WHMCS API/DB call,
 * which is where subtle bugs (e.g. wrong base path under a WHMCS
 * subdirectory install) tend to hide silently until a client clicks a
 * broken button in production.
 */

if (!defined('WHMCS')) {
    define('WHMCS', true);
}

require_once __DIR__ . '/../modules/servers/rackflow/rackflow.php';
require_once __DIR__ . '/../modules/servers/rackflow_reseller/rackflow_reseller.php';
