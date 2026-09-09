{literal}
<style>
.rf-ca {
  --rf-ink: #1c2430;
  --rf-muted: #5b6775;
  --rf-line: #d5dce5;
  --rf-bg: #f4f6f8;
  --rf-card: #ffffff;
  --rf-accent: #0f6e56;
  --rf-accent-soft: #e7f5ef;
  --rf-radius: 12px;
  width: 100%;
  max-width: none;
  margin: 0;
  text-align: left;
  color: var(--rf-ink);
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
}
.rf-ca *, .rf-ca *::before, .rf-ca *::after { box-sizing: border-box; }
.rf-ca__card {
  background: var(--rf-card);
  border: 1px solid var(--rf-line);
  border-radius: var(--rf-radius);
  overflow: hidden;
}
.rf-ca__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  background: linear-gradient(135deg, #f7faf8 0%, #eef3f7 100%);
  border-bottom: 1px solid var(--rf-line);
}
.rf-ca__title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.rf-ca__subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--rf-muted);
  line-height: 1.4;
}
.rf-ca__badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.rf-ca__badge--on { background: #d8f3e3; color: #0b6b3a; }
.rf-ca__badge--off { background: #e8ecf0; color: #4a5560; }
.rf-ca__badge--warn { background: #fff3cd; color: #7a5b00; }
.rf-ca__badge--unknown { background: #e8ecf0; color: #4a5560; }
.rf-ca__badge-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.rf-ca__body { padding: 18px 20px 20px; }
.rf-ca__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.rf-ca__stat {
  background: var(--rf-bg);
  border: 1px solid var(--rf-line);
  border-radius: 10px;
  padding: 12px 14px;
  min-height: 72px;
}
.rf-ca__stat-label {
  display: block;
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--rf-muted);
}
.rf-ca__stat-value {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  word-break: break-word;
  line-height: 1.35;
}
.rf-ca__stat-value code {
  font-size: 13px;
  background: transparent;
  padding: 0;
  color: inherit;
}
.rf-ca__actions {
  display: grid;
  gap: 10px;
}
.rf-ca__action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--rf-line);
  border-radius: 10px;
  background: #fff;
}
.rf-ca__action-copy { min-width: 0; flex: 1; }
.rf-ca__action-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 700;
}
.rf-ca__action-help {
  margin: 0;
  font-size: 12px;
  color: var(--rf-muted);
  line-height: 1.4;
}
.rf-ca__btn,
a.rf-ca__btn,
button.rf-ca__btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  min-width: 148px;
  height: 36px;
  padding: 0 14px;
  border-radius: 8px;
  border: 1px solid #0f6e56 !important;
  background: #0f6e56 !important;
  background-color: #0f6e56 !important;
  color: #fff !important;
  font-size: 13px;
  font-weight: 650;
  text-decoration: none !important;
  box-shadow: none;
  transition: background .15s, border-color .15s, box-shadow .15s;
}
.rf-ca__btn:hover,
.rf-ca__btn:focus,
.rf-ca__btn:active,
a.rf-ca__btn:hover,
button.rf-ca__btn:hover,
a.rf-ca__btn:focus,
button.rf-ca__btn:focus {
  background: #0c5c48 !important;
  background-color: #0c5c48 !important;
  border-color: #0c5c48 !important;
  color: #fff !important;
  box-shadow: 0 0 0 3px rgba(15, 110, 86, 0.16);
}
.rf-ca__btn--secondary,
a.rf-ca__btn--secondary,
button.rf-ca__btn--secondary {
  background: #fff !important;
  background-color: #fff !important;
  color: #0f6e56 !important;
  border-color: #0f6e56 !important;
}
.rf-ca__btn--secondary:hover,
.rf-ca__btn--secondary:focus,
a.rf-ca__btn--secondary:hover,
button.rf-ca__btn--secondary:hover {
  background: #e7f5ef !important;
  background-color: #e7f5ef !important;
  color: #0f6e56 !important;
}
.rf-ca__creds {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--rf-muted);
}
.rf-ca__creds code {
  font-size: 12px;
  color: var(--rf-ink);
}
.rf-ca__note {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--rf-line);
  border-radius: 10px;
  background: var(--rf-bg);
  font-size: 13px;
  color: var(--rf-muted);
  line-height: 1.45;
}
.rf-ca__controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--rf-line);
}
.rf-ca__btn--ghost,
a.rf-ca__btn--ghost,
button.rf-ca__btn--ghost {
  background: #fff !important;
  background-color: #fff !important;
  color: #1c2430 !important;
  border-color: #d5dce5 !important;
  min-width: 0;
}
.rf-ca__btn--ghost:hover,
.rf-ca__btn--ghost:focus,
a.rf-ca__btn--ghost:hover,
button.rf-ca__btn--ghost:hover {
  background: #f4f6f8 !important;
  background-color: #f4f6f8 !important;
  border-color: #b7c2cf !important;
  color: #1c2430 !important;
  box-shadow: none;
}
.rf-ca__btn--danger,
a.rf-ca__btn--danger,
button.rf-ca__btn--danger {
  background: #fff !important;
  background-color: #fff !important;
  color: #9b1c1c !important;
  border-color: #f0c2c2 !important;
  min-width: 0;
}
.rf-ca__btn--danger:hover,
.rf-ca__btn--danger:focus,
a.rf-ca__btn--danger:hover,
button.rf-ca__btn--danger:hover {
  background: #fdf2f2 !important;
  background-color: #fdf2f2 !important;
  border-color: #e5a4a4 !important;
  color: #9b1c1c !important;
  box-shadow: 0 0 0 3px rgba(155, 28, 28, 0.12);
}
.rf-ca-modal {
  position: fixed;
  inset: 0;
  z-index: 1050;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.rf-ca-modal.is-open { display: flex; }
.rf-ca-modal__backdrop {
  position: absolute;
  inset: 0;
  background: rgba(28, 36, 48, 0.45);
}
.rf-ca-modal__dialog {
  position: relative;
  width: 100%;
  max-width: 420px;
  background: #fff;
  border: 1px solid var(--rf-line);
  border-radius: 12px;
  box-shadow: 0 18px 50px rgba(28, 36, 48, 0.18);
  padding: 20px 22px 18px;
}
.rf-ca-modal__title {
  margin: 0 0 6px;
  font-size: 16px;
  font-weight: 700;
}
.rf-ca-modal__help {
  margin: 0 0 16px;
  font-size: 12px;
  color: var(--rf-muted);
  line-height: 1.4;
}
.rf-ca-modal__field {
  margin: 0 0 12px;
}
.rf-ca-modal__field label {
  display: block;
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 650;
}
.rf-ca-modal__field input {
  width: 100%;
  height: 40px;
  padding: 8px 12px;
  border: 1.5px solid #9aa7b5;
  border-radius: 8px;
  background: #f8fafb;
  color: var(--rf-ink);
  font-size: 13px;
  box-shadow: inset 0 1px 2px rgba(28, 36, 48, 0.06);
}
.rf-ca-modal__field input:hover {
  border-color: #7d8b9a;
  background: #fff;
}
.rf-ca-modal__field input:focus {
  outline: none;
  background: #fff;
  border-color: var(--rf-accent);
  box-shadow: 0 0 0 3px rgba(15, 110, 86, 0.18);
}
.rf-ca-modal__error {
  display: none;
  margin: 0 0 12px;
  font-size: 12px;
  color: #9b1c1c;
}
.rf-ca-modal__error.is-visible { display: block; }
.rf-ca-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}
.rf-ca-modal__footer .rf-ca__btn--primary,
#rackflow-password-save {
  background: #0f6e56 !important;
  background-color: #0f6e56 !important;
  border: 1px solid #0f6e56 !important;
  color: #fff !important;
}
.rf-ca-modal__footer .rf-ca__btn--primary:hover,
.rf-ca-modal__footer .rf-ca__btn--primary:focus,
#rackflow-password-save:hover,
#rackflow-password-save:focus {
  background: #0c5c48 !important;
  background-color: #0c5c48 !important;
  border-color: #0c5c48 !important;
  color: #fff !important;
}
.rf-ca__backups {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--rf-line);
}
.rf-ca__panel-head { margin-bottom: 10px; }
.rf-ca__backup-create {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.rf-ca__backup-create input {
  flex: 1 1 160px;
  min-width: 140px;
  padding: 8px 10px;
  border: 1px solid var(--rf-line);
  border-radius: 8px;
}
.rf-ca__backup-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}
.rf-ca__backup-list li.rf-ca__backup-running {
  border-color: #e6d59a;
  background: #fff9e8;
}
.rf-ca__backup-list li {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  padding: 10px 12px;
  border: 1px solid var(--rf-line);
  border-radius: 8px;
  background: var(--rf-bg);
}
.rf-ca__backup-list li.rf-ca__backup-kind-platform {
  background: #eef4fb;
  border-color: #c5d7eb;
}
.rf-ca__backup-list li.rf-ca__backup-kind-client {
  background: #eef8f3;
  border-color: #b9dcc9;
}
.rf-ca__backup-kind {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.02em;
  text-transform: none;
  color: var(--rf-muted);
  background: rgba(28, 36, 48, 0.06);
}
.rf-ca__backup-kind.is-platform {
  background: #d9e7f7;
  color: #1f4b7a;
}
.rf-ca__backup-kind.is-client {
  background: #d5ebe0;
  color: #1f5c40;
}
.rf-ca__backup-storage { color: var(--rf-muted); }
.rf-ca__backup-template {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(28, 36, 48, 0.06);
  color: var(--rf-muted);
  font-size: 12px;
  font-weight: 600;
}
.rf-ca__backup-notes { font-weight: 700; color: var(--rf-ink); }
.rf-ca__backup-jobs {
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid #e6d59a;
  border-radius: 8px;
  background: #fff9e8;
}
@media (max-width: 640px) {
  .rf-ca__grid { grid-template-columns: 1fr; }
  .rf-ca__head { flex-direction: column; align-items: flex-start; }
  .rf-ca__action { flex-direction: column; align-items: stretch; }
  .rf-ca__btn { width: 100%; min-width: 0; }
  .rf-ca__controls .rf-ca__btn { flex: 1 1 calc(50% - 8px); }
}
</style>
{/literal}

<div class="rf-ca" id="rackflow-client-area">
  <div class="rf-ca__card">
    <div class="rf-ca__head">
      <div>
        <h3 class="rf-ca__title">Server overview</h3>
        <p class="rf-ca__subtitle">Status and console access for this service.</p>
      </div>
      {if $rackflow_power_available && $rackflow_power_status_label}
      <span class="rf-ca__badge {$rackflow_power_badge_class|escape}">
        <span class="rf-ca__badge-dot" aria-hidden="true"></span>
        {$rackflow_power_status_label|escape}
      </span>
      {/if}
    </div>

    <div class="rf-ca__body">
      {if $rackflow_hostname || $rackflow_primary_ip || $rackflow_server_name}
      <div class="rf-ca__grid">
        {if $rackflow_hostname}
        <div class="rf-ca__stat">
          <span class="rf-ca__stat-label">Hostname</span>
          <p class="rf-ca__stat-value"><code>{$rackflow_hostname|escape}</code></p>
        </div>
        {/if}
        {if $rackflow_primary_ip}
        <div class="rf-ca__stat">
          <span class="rf-ca__stat-label">Primary IP</span>
          <p class="rf-ca__stat-value"><code>{$rackflow_primary_ip|escape}</code></p>
        </div>
        {/if}
        {if $rackflow_server_name}
        <div class="rf-ca__stat">
          <span class="rf-ca__stat-label">Server</span>
          <p class="rf-ca__stat-value">{$rackflow_server_name|escape}</p>
        </div>
        {/if}
        {if $rackflow_service_status}
        <div class="rf-ca__stat">
          <span class="rf-ca__stat-label">Service status</span>
          <p class="rf-ca__stat-value">{$rackflow_service_status|escape}</p>
        </div>
        {/if}
        {if $rackflow_installation_status}
        <div class="rf-ca__stat">
          <span class="rf-ca__stat-label">Installation</span>
          <p class="rf-ca__stat-value">{$rackflow_installation_status|escape}</p>
        </div>
        {/if}
      </div>
      {/if}

      {if !$rackflow_power_available && $rackflow_power_message}
      <p class="rf-ca__note">{$rackflow_power_message|escape}</p>
      {/if}

      {if $rackflow_proxy_credentials_available && $rackflow_proxy_assignments|@count}
      <div class="rf-ca__backups">
        <div class="rf-ca__panel-head">
          <p class="rf-ca__action-title">Proxy access</p>
          <p class="rf-ca__action-help">HTTP and SOCKS5 use the same IP/port with these credentials.</p>
        </div>
        {foreach from=$rackflow_proxy_assignments item=a}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title"><code>{$a.ip_address|escape}</code></p>
            {if $a.username || $a.password}
            <p class="rf-ca__creds">
              {if $a.username}<code>{$a.username|escape}</code>{/if}
              {if $a.username && $a.password} / {/if}
              {if $a.password}<code>{$a.password|escape}</code>{/if}
            </p>
            {/if}
            {if $a.http_url}<p class="rf-ca__action-help"><code>{$a.http_url|escape}</code></p>{/if}
            {if $a.socks5_url}<p class="rf-ca__action-help"><code>{$a.socks5_url|escape}</code></p>{/if}
          </div>
        </div>
        {/foreach}
        {if $rackflow_proxy_rotate_available}
        <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-proxy-rotate" data-rf-proxy-action="{$rackflow_proxy_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">Rotate credentials</button>
        <p class="rf-ca__note" id="rackflow-proxy-msg" hidden></p>
        {/if}
      </div>
      {/if}

      {if $rackflow_portal_open_url || $rackflow_ipmi_available || $rackflow_vnc_available || $rackflow_kvm_available}
      <div class="rf-ca__actions">
        {if $rackflow_portal_open_url}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">RackFlow portal</p>
            <p class="rf-ca__action-help">Sign in and open your RackFlow dashboard in a new tab.</p>
          </div>
          <a href="{$rackflow_portal_open_url|escape}" target="_blank" rel="noopener" class="rf-ca__btn" id="rackflow-portal-launch">Open portal</a>
        </div>
        {/if}

        {if $rackflow_ipmi_available}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">IPMI console</p>
            <p class="rf-ca__action-help">Opens in a new tab. The console link is single-use and expires shortly.</p>
            {if $rackflow_ipmi_viewer_username || $rackflow_ipmi_viewer_password}
            <p class="rf-ca__creds">
              Login:
              {if $rackflow_ipmi_viewer_username}<code>{$rackflow_ipmi_viewer_username|escape}</code>{/if}
              {if $rackflow_ipmi_viewer_username && $rackflow_ipmi_viewer_password} / {/if}
              {if $rackflow_ipmi_viewer_password}<code>{$rackflow_ipmi_viewer_password|escape}</code>{/if}
            </p>
            {/if}
          </div>
          <a href="{$rackflow_ipmi_open_url|escape}" target="_blank" rel="noopener" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-ipmi-launch">Open IPMI</a>
        </div>
        {/if}

        {if $rackflow_vnc_available}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">VNC console</p>
            <p class="rf-ca__action-help">Opens in a popup. The console link is single-use and expires shortly.</p>
          </div>
          <a href="{$rackflow_vnc_open_url|escape}" rel="noopener" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-vnc-launch">Open VNC</a>
        </div>
        {/if}

        {if $rackflow_kvm_available}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">HTML5 KVM</p>
            <p class="rf-ca__action-help">Opens in a popup. The console link is single-use and expires shortly.</p>
          </div>
          <a href="{$rackflow_kvm_open_url|escape}" rel="noopener" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-kvm-launch">Open KVM</a>
        </div>
        {/if}
      </div>
      {/if}

      {if $rackflow_power_controls_allowed || $rackflow_change_password_allowed}
      <div class="rf-ca__controls">
        {if $rackflow_change_password_allowed}
        <button type="button" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-password-open">Change password</button>
        {/if}
        {if $rackflow_power_controls_allowed}
        <a href="{$rackflow_power_on_url|escape}" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-power-on">Power on</a>
        <a href="{$rackflow_power_off_url|escape}" class="rf-ca__btn rf-ca__btn--danger" id="rackflow-power-off" data-rf-confirm="Power off this server?">Power off</a>
        <a href="{$rackflow_reboot_url|escape}" class="rf-ca__btn rf-ca__btn--danger" id="rackflow-reboot" data-rf-confirm="Reboot this server?">Reboot</a>
        {/if}
      </div>
      {/if}

      {if $rackflow_backups_allowed}
      <div class="rf-ca__backups">
        <div class="rf-ca__panel-head">
          <p class="rf-ca__action-title">Backups</p>
          <p class="rf-ca__action-help">Scheduled backups are read-only. Customer backups count toward your quota and can be deleted.</p>
        </div>
        <div class="rf-ca__backup-create" data-rf-backup-action="{$rackflow_backup_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">
          <input type="text" id="rackflow-backup-notes" name="rf_notes" placeholder="Backup name / notes" autocomplete="off" />
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-backup-create"{if $rackflow_backup_jobs|@count} disabled{/if}>Create backup</button>
        </div>
        <p class="rf-ca__note" id="rackflow-backup-msg" hidden></p>
        {if $rackflow_backup_jobs|@count}
        <div class="rf-ca__backup-jobs" data-rf-auto-refresh="15">
          <p class="rf-ca__action-title">Running jobs</p>
          <ul class="rf-ca__backup-list">
            {foreach from=$rackflow_backup_jobs item=j}
            <li class="rf-ca__backup-running">
              <span class="rf-ca__backup-kind">{if $j.scope == 'client'}Customer{elseif $j.scope == 'platform'}Scheduled{elseif $j.scope}{$j.scope|escape|ucfirst}{elseif $j.kind == 'client'}Customer{elseif $j.kind == 'platform'}Scheduled{else}{$j.kind|escape|ucfirst}{/if}</span>
              <span>{$j.status|escape}</span>
              <span>{if $j.starttime}{$j.starttime|date_format:"%Y-%m-%d %H:%M"}{else}—{/if}</span>
              {if $j.storage}<span class="rf-ca__backup-storage">{$j.storage|escape}</span>{/if}
            </li>
            {/foreach}
          </ul>
          <p class="rf-ca__note">This page refreshes while a backup or restore is running.</p>
        </div>
        {/if}
        {if $rackflow_backups_error}
        <p class="rf-ca__note">{$rackflow_backups_error|escape}</p>
        {elseif !$rackflow_backups|@count}
        <p class="rf-ca__note">No backups yet.</p>
        {else}
        <ul class="rf-ca__backup-list">
          {foreach from=$rackflow_backups item=b}
          <li class="{if $b.running}rf-ca__backup-running {/if}rf-ca__backup-kind-{$b.kind|escape}">
            <span class="rf-ca__backup-kind is-{$b.kind|escape}">{if $b.kind_label}{$b.kind_label|escape}{elseif $b.kind == 'client'}Customer{elseif $b.kind == 'platform'}Scheduled{else}{$b.kind|escape|ucfirst}{/if}</span>
            <span>{if $b.ctime}{$b.ctime|date_format:"%Y-%m-%d %H:%M"}{else}—{/if}</span>
            {if $b.template_name}<span class="rf-ca__backup-template">{$b.template_name|escape}</span>{/if}
            {if $b.notes_display}<span class="rf-ca__backup-notes">{$b.notes_display|escape}</span>{/if}
            <span class="rf-ca__backup-storage">{$b.storage|escape}</span>
            {if $b.running}
            <span class="rf-ca__backup-kind">Running</span>
            {else}
            <button type="button" class="rf-ca__btn rf-ca__btn--ghost" data-rf-backup-op="restore" data-rf-volid="{$b.volid|escape}" data-rf-storage="{$b.storage|escape}" data-rf-confirm="Restore this backup onto the current VM? The guest will be overwritten.">Restore</button>
            {if $b.deletable}
            <button type="button" class="rf-ca__btn rf-ca__btn--danger" data-rf-backup-op="delete" data-rf-volid="{$b.volid|escape}" data-rf-storage="{$b.storage|escape}" data-rf-confirm="Delete this customer backup?">Delete</button>
            {/if}
            {/if}
          </li>
          {/foreach}
        </ul>
        {/if}
      </div>
      {/if}
    </div>
  </div>
</div>

{if $rackflow_change_password_allowed}
<div class="rf-ca-modal" id="rackflow-password-modal" aria-hidden="true">
  <div class="rf-ca-modal__backdrop" data-rf-close="1"></div>
  <div class="rf-ca-modal__dialog" role="dialog" aria-modal="true" aria-labelledby="rackflow-password-title">
    <h3 class="rf-ca-modal__title" id="rackflow-password-title">Change password</h3>
    <p class="rf-ca-modal__help">Updates the password stored in WHMCS and applies it on the server via RackFlow.</p>
    <form method="post" action="clientarea.php?action=productdetails" id="rackflow-password-form" autocomplete="off">
      <input type="hidden" name="id" value="{$rackflow_whmcs_service_id|escape}" />
      <input type="hidden" name="modulechangepassword" value="true" />
      <div class="rf-ca-modal__field">
        <label for="rackflow-newpw">New password</label>
        <input type="password" id="rackflow-newpw" name="newpw" required minlength="6" autocomplete="new-password" />
      </div>
      <div class="rf-ca-modal__field">
        <label for="rackflow-confirmpw">Confirm password</label>
        <input type="password" id="rackflow-confirmpw" name="confirmpw" required minlength="6" autocomplete="new-password" />
      </div>
      <p class="rf-ca-modal__error" id="rackflow-password-error">Passwords do not match.</p>
      <div class="rf-ca-modal__footer">
        <button type="button" class="rf-ca__btn rf-ca__btn--ghost" data-rf-close="1">Cancel</button>
        <button type="submit" class="rf-ca__btn rf-ca__btn--primary" id="rackflow-password-save">Save password</button>
      </div>
    </form>
  </div>
</div>
{/if}

{literal}
<script>
(function () {
  var vnc = document.getElementById('rackflow-vnc-launch');
  if (vnc) {
    vnc.addEventListener('click', function (e) {
      e.preventDefault();
      window.open(vnc.href, 'rackflow_vnc', 'width=1024,height=768,resizable=yes,scrollbars=yes');
    });
  }
  var kvm = document.getElementById('rackflow-kvm-launch');
  if (kvm) {
    kvm.addEventListener('click', function (e) {
      e.preventDefault();
      window.open(kvm.href, 'rackflow_kvm', 'width=1024,height=768,resizable=yes,scrollbars=yes');
    });
  }

  var autoRefresh = document.querySelector('#rackflow-client-area [data-rf-auto-refresh]');
  if (autoRefresh) {
    var secs = parseInt(autoRefresh.getAttribute('data-rf-auto-refresh'), 10) || 15;
    window.setTimeout(function () { window.location.reload(); }, secs * 1000);
  }

  function backupActionUrl() {
    var box = document.querySelector('#rackflow-client-area [data-rf-backup-action]');
    return box ? box.getAttribute('data-rf-backup-action') : '';
  }
  function backupServiceId() {
    var box = document.querySelector('#rackflow-client-area [data-rf-backup-action]');
    return box ? box.getAttribute('data-rf-service-id') : '';
  }
  function setBackupMsg(text, isError) {
    var el = document.getElementById('rackflow-backup-msg');
    if (!el) { return; }
    if (!text) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.style.color = isError ? '#b42318' : '#0a7a32';
  }
  function runBackupOp(op, extra) {
    var url = backupActionUrl();
    var sid = backupServiceId();
    if (!url || !sid) {
      setBackupMsg('Backup action URL missing. Reload the page.', true);
      return;
    }
    var body = new URLSearchParams();
    body.set('serviceid', sid);
    body.set('op', op);
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        if (extra[k] != null && extra[k] !== '') { body.set(k, extra[k]); }
      });
    }
    setBackupMsg('Working…', false);
    fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: body.toString(),
      credentials: 'same-origin'
    }).then(function (res) {
      return res.json().catch(function () {
        return { ok: false, error: 'Unexpected response (' + res.status + ')' };
      });
    }).then(function (data) {
      if (data && data.ok) {
        setBackupMsg(data.message || 'Done.', false);
        window.setTimeout(function () { window.location.reload(); }, 600);
        return;
      }
      setBackupMsg((data && data.error) ? data.error : 'Backup request failed.', true);
      if (createBtn) { createBtn.disabled = false; }
    }).catch(function (err) {
      setBackupMsg(err && err.message ? err.message : 'Backup request failed.', true);
      if (createBtn) { createBtn.disabled = false; }
    });
  }

  var createBtn = document.getElementById('rackflow-backup-create');
  if (createBtn) {
    createBtn.addEventListener('click', function () {
      if (createBtn.disabled) { return; }
      var notesEl = document.getElementById('rackflow-backup-notes');
      createBtn.disabled = true;
      runBackupOp('create', { rf_notes: notesEl ? notesEl.value : '' });
    });
  }

  var confirms = document.querySelectorAll('#rackflow-client-area [data-rf-confirm]');
  for (var i = 0; i < confirms.length; i++) {
    confirms[i].addEventListener('click', function (e) {
      var msg = this.getAttribute('data-rf-confirm');
      if (msg && !window.confirm(msg)) {
        e.preventDefault();
        return;
      }
      var op = this.getAttribute('data-rf-backup-op');
      if (!op) { return; }
      e.preventDefault();
      runBackupOp(op, {
        volid: this.getAttribute('data-rf-volid') || '',
        storage: this.getAttribute('data-rf-storage') || ''
      });
    });
  }

  var proxyRotateBtn = document.getElementById('rackflow-proxy-rotate');
  if (proxyRotateBtn) {
    proxyRotateBtn.addEventListener('click', function () {
      if (!window.confirm('Rotate proxy credentials? The old username/password stop working immediately.')) {
        return;
      }
      var url = proxyRotateBtn.getAttribute('data-rf-proxy-action') || '';
      var sid = proxyRotateBtn.getAttribute('data-rf-service-id') || '';
      var msgEl = document.getElementById('rackflow-proxy-msg');
      function setProxyMsg(text, isError) {
        if (!msgEl) { return; }
        if (!text) { msgEl.hidden = true; msgEl.textContent = ''; return; }
        msgEl.hidden = false;
        msgEl.textContent = text;
        msgEl.style.color = isError ? '#b42318' : '#0a7a32';
      }
      if (!url || !sid) {
        setProxyMsg('Rotate action URL missing. Reload the page.', true);
        return;
      }
      proxyRotateBtn.disabled = true;
      setProxyMsg('Working…', false);
      var body = new URLSearchParams();
      body.set('serviceid', sid);
      body.set('op', 'rotate');
      fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: body.toString(),
        credentials: 'same-origin'
      }).then(function (res) {
        return res.json().catch(function () {
          return { ok: false, error: 'Unexpected response (' + res.status + ')' };
        });
      }).then(function (data) {
        proxyRotateBtn.disabled = false;
        if (data && data.ok) {
          setProxyMsg(data.message || 'Rotated. Reloading…', false);
          window.setTimeout(function () { window.location.reload(); }, 600);
          return;
        }
        setProxyMsg((data && data.error) ? data.error : 'Rotate failed.', true);
      }).catch(function (e) {
        proxyRotateBtn.disabled = false;
        setProxyMsg(e && e.message ? e.message : 'Rotate failed.', true);
      });
    });
  }

  var modal = document.getElementById('rackflow-password-modal');
  var openBtn = document.getElementById('rackflow-password-open');
  var form = document.getElementById('rackflow-password-form');
  var err = document.getElementById('rackflow-password-error');
  if (!modal || !openBtn || !form) { return; }

  // Keep the modal on <body> so it is not trapped inside a display:none tab
  // pane after the RackFlow card is unwrapped from Server Information.
  if (modal.parentNode !== document.body) {
    document.body.appendChild(modal);
  }

  function openModal(e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    if (modal.parentNode !== document.body) {
      document.body.appendChild(modal);
    }
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    var input = document.getElementById('rackflow-newpw');
    if (input) { input.focus(); }
  }
  function closeModal() {
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    if (err) { err.classList.remove('is-visible'); }
  }

  openBtn.addEventListener('click', openModal);
  modal.addEventListener('click', function (e) {
    if (e.target && e.target.getAttribute('data-rf-close') === '1') {
      closeModal();
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.classList.contains('is-open')) {
      closeModal();
    }
  });
  form.addEventListener('submit', function (e) {
    var a = document.getElementById('rackflow-newpw');
    var b = document.getElementById('rackflow-confirmpw');
    if (!a || !b || a.value !== b.value) {
      e.preventDefault();
      if (err) { err.classList.add('is-visible'); }
      return;
    }
    if (err) { err.classList.remove('is-visible'); }
  });
})();
</script>
{/literal}
