{if $rackflow_power_available}
<div class="panel panel-default">
    <div class="panel-heading">
        <h3 class="panel-title">Server status</h3>
    </div>
    <div class="panel-body">
        <p class="mb-2">
            <strong>Power:</strong>
            <span style="{$rackflow_power_status_style}padding:4px 10px;border-radius:4px;font-size:13px;font-weight:600;text-transform:uppercase;">{$rackflow_power_status_label}</span>
        </p>
        {if $rackflow_server_name}
        <p class="mb-1"><strong>Server:</strong> {$rackflow_server_name}</p>
        {/if}
        {if $rackflow_service_status}
        <p class="mb-0 text-muted small">Service status: {$rackflow_service_status}</p>
        {/if}
    </div>
</div>
{else}
{if $rackflow_power_message}
<div class="alert alert-info">
    <strong>Server status:</strong> {$rackflow_power_message}
</div>
{/if}
{/if}
