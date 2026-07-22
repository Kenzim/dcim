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
        {if $rackflow_ipmi_available}
        <hr>
        <div class="mt-2">
            <strong>IPMI / Console:</strong>
            {if $rackflow_ipmi_launch_url}
                <p class="mb-1">
                    <a href="{$rackflow_ipmi_launch_url}" target="_blank" rel="noopener" class="btn btn-primary btn-sm" id="rackflow-ipmi-launch">Launch IPMI console</a>
                </p>
                {if $rackflow_ipmi_viewer_username}
                <p class="mb-0 small text-muted">
                    Login: <code>{$rackflow_ipmi_viewer_username}</code>{if $rackflow_ipmi_viewer_password} / <code>{$rackflow_ipmi_viewer_password}</code>{/if}
                </p>
                {/if}
                <p class="mb-0 small text-muted">This link is single-use and expires shortly. Reopen from here if it stops working.</p>
                <script type="text/javascript">
                    // Auto-open the freshly minted console in a new tab.
                    (function () {
                        try { window.open('{$rackflow_ipmi_launch_url}', '_blank', 'noopener'); } catch (e) {}
                    })();
                </script>
            {else}
                <p class="mb-1">
                    <a href="#" class="btn btn-primary btn-sm" onclick="(function(){ var u = new URL(window.location.href); u.searchParams.set('rackflow_ipmi','1'); window.location.href = u.toString(); })(); return false;">Open IPMI console</a>
                </p>
                {if $rackflow_ipmi_error}
                <p class="mb-0 small text-danger">{$rackflow_ipmi_error}</p>
                {/if}
            {/if}
        </div>
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
