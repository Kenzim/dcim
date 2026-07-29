<?php

use PHPUnit\Framework\TestCase;

/**
 * Covers the same-origin URL builders used by the proxy credential rotate
 * button (admin status card + client area). These run before any WHMCS
 * API/session call, so they're safe to test as pure PHP: given a
 * DOCUMENT_ROOT (or fallback systemurl), they must resolve the right
 * root-relative path — including when WHMCS is installed in a subdirectory,
 * which is the case that has broken other endpoint URLs in the past.
 */
final class ProxyActionUrlTest extends TestCase
{
    private $originalDocumentRoot;

    protected function setUp(): void
    {
        $this->originalDocumentRoot = $_SERVER['DOCUMENT_ROOT'] ?? null;
    }

    protected function tearDown(): void
    {
        if ($this->originalDocumentRoot === null) {
            unset($_SERVER['DOCUMENT_ROOT']);
        } else {
            $_SERVER['DOCUMENT_ROOT'] = $this->originalDocumentRoot;
        }
    }

    private function whmcsRoot(): string
    {
        return realpath(__DIR__ . '/../');
    }

    public function testEndpointUrlAtDocumentRoot(): void
    {
        $_SERVER['DOCUMENT_ROOT'] = $this->whmcsRoot();

        $this->assertSame(
            '/modules/servers/rackflow/proxy_action.php',
            rackflow_proxyActionEndpointUrl(123)
        );
        $this->assertSame(
            '/modules/servers/rackflow/ip_action.php',
            rackflow_ipActionEndpointUrl(123)
        );
        $this->assertSame(
            '/modules/servers/rackflow/backup_action.php',
            rackflow_backupActionEndpointUrl(123)
        );
    }

    public function testEndpointUrlWhenWhmcsInSubdirectory(): void
    {
        // Simulates WHMCS installed at e.g. https://host/whmcs/ instead of
        // the domain root: DOCUMENT_ROOT is the parent of the WHMCS folder.
        $_SERVER['DOCUMENT_ROOT'] = dirname($this->whmcsRoot());

        $expectedPrefix = '/' . basename($this->whmcsRoot());
        $this->assertSame(
            $expectedPrefix . '/modules/servers/rackflow/proxy_action.php',
            rackflow_proxyActionEndpointUrl(123)
        );
    }

    public function testEndpointUrlFallsBackToSystemUrlWhenDocumentRootUnusable(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);

        $this->assertSame(
            '/billing/modules/servers/rackflow/proxy_action.php',
            rackflow_proxyActionEndpointUrl(123, 'https://example.com/billing/')
        );
    }

    public function testEndpointUrlFallsBackToEmptyBasePathWithoutSystemUrl(): void
    {
        unset($_SERVER['DOCUMENT_ROOT']);

        $this->assertSame(
            '/modules/servers/rackflow/proxy_action.php',
            rackflow_proxyActionEndpointUrl(123)
        );
    }

    public function testWhmcsUrlBasePathReturnsNullWhenDocumentRootIsUnrelated(): void
    {
        $_SERVER['DOCUMENT_ROOT'] = '/some/unrelated/path';

        $this->assertNull(rackflow_whmcsUrlBasePath());
    }
}
