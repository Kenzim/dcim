<?php

use PHPUnit\Framework\TestCase;

/**
 * RackFlow request bodies are JSON objects. An empty PHP array must be written
 * as "{}" (a list "[]" is rejected by the API with 422), while real lists nested
 * in a body must stay lists.
 */
final class JsonBodyTest extends TestCase
{
    public function testEmptyBodyIsAnObject(): void
    {
        $this->assertSame('{}', rackflow_jsonBody(array()));
    }

    public function testEmptyActionParamsAreAnObject(): void
    {
        $this->assertSame('{"params":{}}', rackflow_jsonBody(array('params' => (object)array())));
    }

    public function testEmptyServiceConfigIsAnObject(): void
    {
        $this->assertSame(
            '{"name":"service-1","service_config":{}}',
            rackflow_jsonBody(array('name' => 'service-1', 'service_config' => (object)array()))
        );
    }

    public function testNestedListsStayLists(): void
    {
        $this->assertSame(
            '{"params":{"keys":["a","b"]}}',
            rackflow_jsonBody(array('params' => (object)array('keys' => array('a', 'b'))))
        );
    }
}
