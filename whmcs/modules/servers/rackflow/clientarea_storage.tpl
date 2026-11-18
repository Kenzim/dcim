{literal}
<style>
.rf-st {
  --rf-ink: #1c2430;
  --rf-muted: #5b6775;
  --rf-line: #d5dce5;
  --rf-bg: #f4f6f8;
  --rf-accent: #0f6e56;
  width: 100%;
  text-align: left;
  color: var(--rf-ink);
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
}
.rf-st *, .rf-st *::before, .rf-st *::after { box-sizing: border-box; }
.rf-st__card { background: #fff; border: 1px solid var(--rf-line); border-radius: 12px; overflow: hidden; margin-bottom: 16px; }
.rf-st__head {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 18px 20px; background: linear-gradient(135deg, #f7faf8 0%, #eef3f7 100%);
  border-bottom: 1px solid var(--rf-line);
}
.rf-st__title { margin: 0; font-size: 16px; font-weight: 700; }
.rf-st__subtitle { margin: 4px 0 0; font-size: 12px; color: var(--rf-muted); }
.rf-st__badge {
  display: inline-flex; padding: 5px 10px; border-radius: 999px; font-size: 12px; font-weight: 700;
  letter-spacing: 0.04em; text-transform: uppercase; white-space: nowrap; background: #e8ecf0; color: #4a5560;
}
.rf-st__badge--ready { background: #d8f3e3; color: #0b6b3a; }
.rf-st__badge--warn { background: #fff3cd; color: #7a5b00; }
.rf-st__body { padding: 18px 20px 20px; }
.rf-st__grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.rf-st__stat { background: var(--rf-bg); border: 1px solid var(--rf-line); border-radius: 10px; padding: 12px 14px; }
.rf-st__label {
  display: block; margin: 0 0 6px; font-size: 11px; font-weight: 750; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--rf-muted);
}
.rf-st__value { margin: 0; font-size: 14px; font-weight: 650; word-break: break-word; }
.rf-st__bar { height: 6px; margin-top: 8px; border-radius: 3px; background: #e1e6ec; overflow: hidden; }
.rf-st__bar span { display: block; height: 100%; background: var(--rf-accent); }
.rf-st__table { width: 100%; border-collapse: collapse; font-size: 13px; }
.rf-st__table th, .rf-st__table td { padding: 8px 6px; border-bottom: 1px solid var(--rf-line); text-align: left; vertical-align: top; }
.rf-st__table th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--rf-muted); }
.rf-st code, .rf-st__mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
  background: var(--rf-bg); padding: 2px 6px; border-radius: 4px; word-break: break-all;
}
.rf-st__help { margin: 0 0 12px; font-size: 12px; color: var(--rf-muted); line-height: 1.5; }
.rf-st__note { padding: 12px 14px; border-radius: 10px; background: var(--rf-bg); font-size: 13px; }
.rf-st__actions { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
a.rf-st__btn {
  display: inline-flex; align-items: center; justify-content: center; min-width: 148px; height: 36px; padding: 0 14px;
  border-radius: 8px; background: var(--rf-accent); color: #fff !important; font-size: 13px; font-weight: 650;
  text-decoration: none !important;
}
@media (max-width: 720px) { .rf-st__grid { grid-template-columns: 1fr; } }
</style>
{/literal}

<div class="rf-st">
  <div class="rf-st__card">
    <div class="rf-st__head">
      <div>
        <p class="rf-st__title">NFS storage</p>
        <p class="rf-st__subtitle">Your storage is kept whether or not you have servers with us.</p>
      </div>
      {if $rackflow_storage_loaded}
      <span class="rf-st__badge {if $rackflow_storage.status eq 'ready'}rf-st__badge--ready{elseif $rackflow_storage.status eq 'suspended' || $rackflow_storage.status eq 'error'}rf-st__badge--warn{/if}">{$rackflow_storage_status_label|escape}</span>
      {/if}
    </div>
    <div class="rf-st__body">
      {if !$rackflow_storage_loaded}
        <p class="rf-st__note">{$rackflow_storage_message|escape}</p>
      {else}
        <div class="rf-st__grid">
          <div class="rf-st__stat">
            <span class="rf-st__label">Size</span>
            <p class="rf-st__value">{$rackflow_storage.size_gb|escape} GB</p>
            <p class="rf-st__help" style="margin:4px 0 0">{$rackflow_storage.allocated_gb|escape} GB allocated to shares</p>
          </div>
          <div class="rf-st__stat">
            <span class="rf-st__label">Used</span>
            <p class="rf-st__value">{$rackflow_storage_used_label|escape} ({$rackflow_storage_used_percent|escape}%)</p>
            <div class="rf-st__bar"><span style="width:{$rackflow_storage_used_percent|escape}%"></span></div>
          </div>
          <div class="rf-st__stat">
            <span class="rf-st__label">Private network</span>
            {if $rackflow_storage.linked}
              <p class="rf-st__value">{$rackflow_storage.network_name|escape}</p>
              <p class="rf-st__help" style="margin:4px 0 0">{$rackflow_storage.network_cidr|escape}, storage at <code>{$rackflow_storage.endpoint_ip|escape}</code></p>
            {else}
              <p class="rf-st__value">Not linked yet</p>
              <p class="rf-st__help" style="margin:4px 0 0">Link it to a private network in the portal; one is created for you if you have none.</p>
            {/if}
          </div>
        </div>
        {if $rackflow_storage.location_name}
          <p class="rf-st__help">Location: {$rackflow_storage.location_name|escape}</p>
        {/if}
        {if $rackflow_storage.status_detail}
          <p class="rf-st__note">{$rackflow_storage.status_detail|escape}</p>
        {/if}
      {/if}
    </div>
  </div>

  {if $rackflow_storage_loaded}
  <div class="rf-st__card">
    <div class="rf-st__head"><p class="rf-st__title">Shares</p></div>
    <div class="rf-st__body">
      {if $rackflow_storage_shares}
        <table class="rf-st__table">
          <thead><tr><th>Share</th><th>Quota</th><th>Used</th><th>SFTP</th><th>Mount from your servers</th></tr></thead>
          <tbody>
          {foreach from=$rackflow_storage_shares item=share}
            <tr>
              <td><strong>{$share.name|escape}</strong>{if $share.read_only} (read only){/if}</td>
              <td>{$share.quota_gb|escape} GB</td>
              <td>{$share.used_label|escape}</td>
              <td>{$share.sftp_label|escape}</td>
              <td>
                {if $share.mount_command}
                  <div class="rf-st__mono">{$share.mount_command|escape}</div>
                  {if $share.fstab_line}<div class="rf-st__mono" style="margin-top:4px">{$share.fstab_line|escape}</div>{/if}
                {else}
                  <span class="rf-st__help">Available once the storage is ready.</span>
                {/if}
              </td>
            </tr>
          {/foreach}
          </tbody>
        </table>
      {else}
        <p class="rf-st__note">No shares yet. Create shares in the RackFlow portal.</p>
      {/if}
    </div>
  </div>

  <div class="rf-st__card">
    <div class="rf-st__head">
      <p class="rf-st__title">SFTP access</p>
      <span class="rf-st__badge {if $rackflow_storage_sftp.state eq 'ready'}rf-st__badge--ready{elseif $rackflow_storage_sftp.state eq 'error' || $rackflow_storage_sftp.state eq 'suspended'}rf-st__badge--warn{/if}">{$rackflow_storage_sftp_state_label|escape}</span>
    </div>
    <div class="rf-st__body">
      {if $rackflow_storage_sftp.available}
        <p class="rf-st__help">Reach your shares from anywhere over SFTP. Turn SFTP on per share and manage logins, SSH keys, passwords and allowed addresses in the RackFlow portal.</p>
        <div class="rf-st__grid">
          <div class="rf-st__stat"><span class="rf-st__label">Host</span><p class="rf-st__value">{$rackflow_storage_sftp.host|escape}</p></div>
          <div class="rf-st__stat"><span class="rf-st__label">Port</span><p class="rf-st__value">{$rackflow_storage_sftp.port|escape}</p></div>
          <div class="rf-st__stat"><span class="rf-st__label">Host key fingerprint</span><p class="rf-st__value" style="font-size:12px">{if $rackflow_storage_sftp.host_key_fingerprint}<code>{$rackflow_storage_sftp.host_key_fingerprint|escape}</code>{else}Not reported yet{/if}</p></div>
        </div>
        {if $rackflow_storage_sftp.users}
          <table class="rf-st__table">
            <thead><tr><th>Login</th><th>Connect with</th></tr></thead>
            <tbody>
            {foreach from=$rackflow_storage_sftp.users item=sftpUser}
              <tr>
                <td><strong>{$sftpUser.login|escape}</strong>{if !$sftpUser.enabled} (disabled){/if}</td>
                <td>{if $sftpUser.sftp_command}<span class="rf-st__mono">{$sftpUser.sftp_command|escape}</span>{/if}</td>
              </tr>
            {/foreach}
            </tbody>
          </table>
        {else}
          <p class="rf-st__note">No SFTP logins yet. Add one in the RackFlow portal.</p>
        {/if}
      {else}
        <p class="rf-st__note">SFTP becomes available once your storage is linked to a network and placed on a storage node.</p>
      {/if}
    </div>
  </div>
  {/if}

  {if $rackflow_portal_open_url}
  <div class="rf-st__card">
    <div class="rf-st__body rf-st__actions">
      <div>
        <p class="rf-st__title" style="font-size:14px">RackFlow portal</p>
        <p class="rf-st__help" style="margin:4px 0 0">Sign in to manage shares, the network and SFTP logins.</p>
      </div>
      <a href="{$rackflow_portal_open_url|escape}" target="_blank" rel="noopener" class="rf-st__btn">Open portal</a>
    </div>
  </div>
  {/if}
</div>
