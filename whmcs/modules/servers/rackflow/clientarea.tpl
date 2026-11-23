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
.rf-ca__input {
  display: block;
  width: 100%;
  margin: 8px 0;
  padding: 8px 10px;
  border: 1px solid var(--rf-line);
  border-radius: 8px;
  background: #fff;
  color: var(--rf-ink);
  font-size: 13px;
}
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
.rf-ca__slot-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 6px 0 10px; color: var(--rf-muted); }
.rf-ca__slot-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
.rf-ca__slot-form select, .rf-ca__slot-form input { min-width: 180px; }
.rf-ca__slot-warning { color: #b42318; font-weight: 600; }
.rf-ca__fw-table { width: 100%; border-collapse: collapse; margin: 8px 0; }
.rf-ca__fw-table th, .rf-ca__fw-table td { padding: 6px 8px; border-bottom: 1px solid var(--rf-line); text-align: left; vertical-align: middle; }
.rf-ca__fw-table--baseline { color: var(--rf-muted); }
.rf-ca__fw-table .rf-ca__btn { min-width: 0; padding: 2px 8px; }
.rf-ca__fw-off td { opacity: 0.55; }
.rf-ca__fw-actions { white-space: nowrap; }

.rf-ca__panel-head--row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.rf-ca__sr-only {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
.rf-ca__proxy-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 8px; }
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
        <h3 class="rf-ca__title">{if $rackflow_slot_panel}Backup slot{else}Server overview{/if}</h3>
        <p class="rf-ca__subtitle">{if $rackflow_slot_panel}Keeps one backup of your VMs for as long as you hold this slot.{else}Status and console access for this service.{/if}</p>
      </div>
      {if $rackflow_power_available && $rackflow_power_status_label && !$rackflow_slot_panel}
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
        <div class="rf-ca__panel-head rf-ca__panel-head--row">
          <div>
            <p class="rf-ca__action-title">Proxy access</p>
            <p class="rf-ca__action-help">HTTP and SOCKS5 use the same IP/port with these credentials.</p>
          </div>
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-proxy-copy-all">Copy all (ip:port:user:pass)</button>
        </div>
        <textarea id="rackflow-proxy-endpoint-lines" class="rf-ca__sr-only" readonly aria-hidden="true">{$rackflow_proxy_endpoint_lines|escape}</textarea>
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
        <div class="rf-ca__proxy-actions">
          {if $rackflow_proxy_rotate_available}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-proxy-rotate" data-rf-proxy-action="{$rackflow_proxy_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">Rotate credentials</button>
          {/if}
          <p class="rf-ca__note" id="rackflow-proxy-msg" hidden></p>
        </div>
      </div>
      {/if}

      {if $rackflow_fast_path_available}
      <div class="rf-ca__backups" id="rackflow-fast-path">
        <div class="rf-ca__panel-head rf-ca__panel-head--row">
          <div>
            <p class="rf-ca__action-title">Private proxy access</p>
            <p class="rf-ca__action-help">
              Reach these proxies from your servers over a private network instead of the internet. Use the same
              username and password; traffic still leaves from the proxy's public IP. Your servers get an address in
              <code>{$rackflow_fast_path_cidr|escape}</code> once connected to the "Proxy fast path" network in the portal.
            </p>
          </div>
          {if $rackflow_fast_path_lines}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-fast-path-copy">{if $rackflow_fast_path_has_credentials}Copy all fast path (ip:port:user:pass){else}Copy addresses{/if}</button>
          {/if}
        </div>
        <textarea id="rackflow-fast-path-lines" class="rf-ca__sr-only" readonly aria-hidden="true">{$rackflow_fast_path_lines|escape}</textarea>
        {foreach from=$rackflow_fast_path_locations item=loc}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">{$loc.location_name|escape}:
              {if $loc.enabled}On{elseif $loc.status == 'deleting'}Turning off{else}Off{/if}</p>
            {if !$loc.eligible && !$loc.enabled && $loc.reason}
            <p class="rf-ca__action-help">{$loc.reason|escape}.</p>
            {/if}
            {if $loc.enabled && $loc.servers_attached > 0}
            <p class="rf-ca__action-help">{$loc.servers_attached|escape} server(s) connected. Disconnect them in the portal before turning this off.</p>
            {/if}
          </div>
        </div>
        {/foreach}
        {foreach from=$rackflow_fast_path_endpoints item=ep}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title"><code>{if $ep.line}{$ep.line|escape}{else}{$ep.fast_path_ip|escape}:{$ep.port|escape}{/if}</code></p>
            <p class="rf-ca__action-help">For <code>{$ep.public_ip|escape}</code>{if !$ep.active} (turn private access on to use it){/if}</p>
          </div>
        </div>
        {/foreach}
        <div class="rf-ca__proxy-actions">
          {if $rackflow_fast_path_can_enable}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" data-rf-fast-path-op="on" data-rf-fast-path-action="{$rackflow_fast_path_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">Turn private access on</button>
          {/if}
          {if $rackflow_fast_path_can_disable}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" data-rf-fast-path-op="off" data-rf-fast-path-action="{$rackflow_fast_path_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">Turn private access off</button>
          {/if}
          <p class="rf-ca__note" id="rackflow-fast-path-msg" hidden></p>
        </div>
      </div>
      {/if}

      {if $rackflow_portal_open_url || $rackflow_ipmi_available || $rackflow_vnc_available || $rackflow_kvm_available || $rackflow_sol_available || $rackflow_virtual_media_available}
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

        {if $rackflow_sol_available}
        <div class="rf-ca__action">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">Serial-over-LAN</p>
            <p class="rf-ca__action-help">Opens in a popup. The console link is single-use and expires shortly.</p>
          </div>
          <a href="{$rackflow_sol_open_url|escape}" rel="noopener" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-sol-launch">Open Serial</a>
        </div>
        {/if}

        {if $rackflow_virtual_media_available}
        <div class="rf-ca__action" id="rackflow-virtual-media" data-rf-virtual-media-action="{$rackflow_virtual_media_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">
          <div class="rf-ca__action-copy">
            <p class="rf-ca__action-title">Virtual CD</p>
            <p class="rf-ca__action-help" id="rackflow-virtual-media-status">
              {if $rackflow_virtual_media_inserted}Mounted: {$rackflow_virtual_media_image|escape}{else}No virtual CD inserted{/if}
            </p>
            <select id="rackflow-virtual-media-iso" class="rf-ca__input">
              <option value="">Select ISO</option>
              {foreach from=$rackflow_virtual_media_isos item=iso}
              <option value="{$iso.filename|escape}">{$iso.filename|escape}</option>
              {/foreach}
            </select>
            <label class="rf-ca__action-help"><input type="checkbox" id="rackflow-virtual-media-boot-once" /> Set next boot to CD-ROM</label>
            <p class="rf-ca__note" id="rackflow-virtual-media-msg" hidden></p>
          </div>
          <div class="rf-ca__action-copy">
            <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-virtual-media-mount">Mount</button>
            <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-virtual-media-eject">Eject</button>
          </div>
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
        <a href="{$rackflow_power_on_url|escape}" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-power-on" data-rf-confirm="Power on {if $rackflow_server_name}{$rackflow_server_name|escape}{else}this server{/if}?">Power on</a>
        <a href="{$rackflow_power_off_url|escape}" class="rf-ca__btn rf-ca__btn--danger" id="rackflow-power-off" data-rf-confirm="Power off {if $rackflow_server_name}{$rackflow_server_name|escape}{else}this server{/if}?">Power off</a>
        <a href="{$rackflow_reboot_url|escape}" class="rf-ca__btn rf-ca__btn--danger" id="rackflow-reboot" data-rf-confirm="Reboot {if $rackflow_server_name}{$rackflow_server_name|escape}{else}this server{/if}?">Reboot</a>
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

      {if $rackflow_backups_allowed && $rackflow_vm_slots|@count}
      <div class="rf-ca__backups" data-rf-slot-box data-rf-slot-action="{$rackflow_slot_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">
        <div class="rf-ca__panel-head">
          <p class="rf-ca__action-title">Keep a backup in a slot</p>
          <p class="rf-ca__action-help">A backup kept in a slot is protected from clean-up, stays after this VM is cancelled, and can be restored onto any of your VMs.</p>
        </div>
        <div class="rf-ca__slot-form">
          <select id="rackflow-slot-backup" aria-label="Backup to keep">
            {foreach from=$rackflow_backups item=b}
            {if $b.kind == 'client' && !$b.running && !$b.retained}
            <option value="{$b.volid|escape}">{if $b.ctime}{$b.ctime|date_format:"%Y-%m-%d %H:%M"}{/if} {if $b.notes_display}{$b.notes_display|escape}{else}backup{/if}</option>
            {/if}
            {/foreach}
          </select>
          <select id="rackflow-slot-choice" aria-label="Slot">
            {foreach from=$rackflow_vm_slots item=sl}
            <option value="{$sl.service_id|escape}" data-occupied="{if $sl.occupied}1{else}0{/if}">{$sl.name|escape} ({$sl.size_gb|escape} GB){if $sl.occupied} - holds a backup{/if}</option>
            {/foreach}
          </select>
          <input type="text" id="rackflow-slot-name" placeholder="Label (optional)" autocomplete="off" />
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" data-rf-slot-op="retain">Keep in slot</button>
        </div>
        <p class="rf-ca__note" id="rackflow-slot-msg" hidden></p>
      </div>
      {/if}

      {if $rackflow_slot_panel}
      <div class="rf-ca__backups" data-rf-slot-box data-rf-slot-action="{$rackflow_slot_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}">
        {if $rackflow_slot_error}
        <p class="rf-ca__note">{$rackflow_slot_error|escape}</p>
        {else}
        <div class="rf-ca__slot-meta">
          <span>Size: <strong>{$rackflow_slot.size_gb|escape} GB</strong></span>
          <span>Status: <strong>{$rackflow_slot.status|escape}</strong></span>
        </div>
        {if $rackflow_slot.status == 'suspended'}
        <p class="rf-ca__note">This slot is suspended. Your backup is kept, but it cannot be restored until the slot is active again.</p>
        {/if}
        {if $rackflow_slot.retained}
        <ul class="rf-ca__backup-list">
          <li class="rf-ca__backup-kind-client">
            <span class="rf-ca__backup-kind is-client">Kept</span>
            <span>{if $rackflow_slot.retained.backup_created_at}{$rackflow_slot.retained.backup_created_at|escape}{else}-{/if}</span>
            {if $rackflow_slot.retained.name}<span class="rf-ca__backup-notes">{$rackflow_slot.retained.name|escape}</span>{/if}
            {if $rackflow_slot.retained.source_service_name}<span class="rf-ca__backup-template">from {$rackflow_slot.retained.source_service_name|escape}</span>{/if}
            {if $rackflow_slot.retained.notes_display}<span class="rf-ca__backup-notes">{$rackflow_slot.retained.notes_display|escape}</span>{/if}
          </li>
        </ul>
        {if $rackflow_slot.status == 'active' && $rackflow_slot.restore_targets|@count}
        <div class="rf-ca__slot-form">
          <select id="rackflow-slot-target" aria-label="Restore onto">
            {foreach from=$rackflow_slot.restore_targets item=t}
            <option value="{$t.service_id|escape}">{$t.name|escape}{if $t.is_source} (original VM){/if}</option>
            {/foreach}
          </select>
          <button type="button" class="rf-ca__btn rf-ca__btn--ghost" data-rf-slot-op="restore">Restore onto...</button>
        </div>
        <p class="rf-ca__slot-warning">Restoring replaces everything on the chosen VM. Its address, size and login details stay as they are. A backup can only be restored onto the same type of VM. On a Mac, Apple account sign-ins may need redoing, because they are tied to the VM's serial number.</p>
        {/if}
        <div class="rf-ca__slot-form">
          <button type="button" class="rf-ca__btn rf-ca__btn--danger" data-rf-slot-op="release" data-rf-confirm-text="Delete the backup kept in this slot? This cannot be undone.">Release and delete backup</button>
        </div>
        {else}
        <p class="rf-ca__note">This slot is empty. Open one of your VMs and use "Keep a backup in a slot".</p>
        {/if}
        {/if}
        <p class="rf-ca__note" id="rackflow-slot-msg" hidden></p>
      </div>
      {/if}

      {if $rackflow_firewall_panel}
      <div class="rf-ca__backups" id="rackflow-firewall" data-rf-fw="{$rackflow_firewall_json|escape}" data-rf-fw-action="{$rackflow_firewall_action_url|escape}" data-rf-service-id="{$rackflow_whmcs_service_id|escape}" data-rf-can-edit="{if $rackflow_firewall_can_edit}1{else}0{/if}">
        <div class="rf-ca__panel-head">
          <p class="rf-ca__action-title">Firewall</p>
          <p class="rf-ca__action-help">Rules for traffic to and from this server. The provider's baseline always applies first and cannot be changed here.</p>
        </div>
        {if $rackflow_firewall_error}
        <p class="rf-ca__note">{$rackflow_firewall_error|escape}</p>
        {else}
        <div class="rf-ca__slot-meta">
          <span>Firewall: <strong>{if $rackflow_firewall.client_enabled}On{else}Off{/if}</strong></span>
          <span>State: <strong>{$rackflow_firewall.state|escape}</strong></span>
        </div>
        {if $rackflow_firewall.message}
        <p class="rf-ca__note">{$rackflow_firewall.message|escape}</p>
        {/if}
        {if $rackflow_firewall_can_edit}
        <div class="rf-ca__slot-form">
          {if $rackflow_firewall.client_enabled}
          {if $rackflow_firewall.baseline.client_may_disable}
          <button type="button" class="rf-ca__btn rf-ca__btn--danger" data-rf-fw-op="disable">Turn firewall off</button>
          {/if}
          {else}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" data-rf-fw-op="enable" data-rf-needs-interruption="{if $rackflow_firewall.needs_interruption}1{else}0{/if}">Turn firewall on</button>
          {/if}
        </div>
        {else}
        <p class="rf-ca__note">Your account cannot change the firewall of this server.</p>
        {/if}

        {if $rackflow_firewall.baseline.rules|@count}
        <p class="rf-ca__action-title">Provider baseline (read only)</p>
        <table class="rf-ca__fw-table rf-ca__fw-table--baseline">
          <thead><tr><th>Direction</th><th>Action</th><th>Protocol</th><th>Ports</th><th>Note</th></tr></thead>
          <tbody>
            {foreach from=$rackflow_firewall.baseline.rules item=b}
            <tr>
              <td>{if $b.kind == 'group'}Group {if $b.group_name}{$b.group_name|escape}{/if}{else}{if $b.direction == 'out'}Outgoing{else}Incoming{/if}{/if}</td>
              <td>{$b.action|escape}</td>
              <td>{if $b.protocol}{$b.protocol|escape}{else}any{/if}</td>
              <td>{if $b.ports}{$b.ports|escape}{else}-{/if}</td>
              <td>{$b.comment|escape}</td>
            </tr>
            {/foreach}
          </tbody>
        </table>
        {/if}
        {if $rackflow_firewall.smtp_block_help}
        <p class="rf-ca__note">{$rackflow_firewall.smtp_block_help|escape}</p>
        {/if}

        <div class="rf-ca__slot-form">
          <label for="rackflow-fw-policy-in">Incoming traffic with no matching rule</label>
          <select id="rackflow-fw-policy-in"{if !$rackflow_firewall_can_edit} disabled{/if}>
            {foreach from=['ACCEPT', 'DROP', 'REJECT'] item=p}
            <option value="{$p|escape}"{if $rackflow_firewall.policy_in == $p} selected{/if}>{$p|lower|ucfirst|escape}</option>
            {/foreach}
          </select>
          <label for="rackflow-fw-policy-out">Outgoing</label>
          <select id="rackflow-fw-policy-out"{if !$rackflow_firewall_can_edit} disabled{/if}>
            {foreach from=['ACCEPT', 'DROP', 'REJECT'] item=p}
            <option value="{$p|escape}"{if $rackflow_firewall.policy_out == $p} selected{/if}>{$p|lower|ucfirst|escape}</option>
            {/foreach}
          </select>
          {if $rackflow_firewall_can_edit}
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" data-rf-fw-op="policy">Save policies</button>
          {/if}
        </div>

        <p class="rf-ca__action-title">Your rules</p>
        <p class="rf-ca__action-help">Rules are checked from the top; the first match wins.</p>
        <noscript><p class="rf-ca__note">Turn on JavaScript to see and edit your rules.</p></noscript>
        <table class="rf-ca__fw-table" id="rackflow-fw-rules">
          <thead><tr><th>Order</th><th>On</th><th>Direction</th><th>Action</th><th>Protocol</th><th>Ports</th><th>Address</th><th>Note</th><th></th></tr></thead>
          <tbody id="rackflow-fw-rules-body"></tbody>
        </table>

        {if $rackflow_firewall_can_edit}
        <div class="rf-ca__slot-form">
          <select id="rackflow-fw-new-direction" aria-label="Direction"><option value="in">Incoming</option><option value="out">Outgoing</option></select>
          <select id="rackflow-fw-new-action" aria-label="Action"><option value="ACCEPT">Accept</option><option value="DROP">Drop</option><option value="REJECT">Reject</option></select>
          <select id="rackflow-fw-new-protocol" aria-label="Protocol"><option value="">Any protocol</option><option value="tcp">TCP</option><option value="udp">UDP</option><option value="icmp">ICMP</option></select>
          <input type="text" id="rackflow-fw-new-ports" placeholder="Ports, e.g. 80,443" autocomplete="off" />
          <input type="text" id="rackflow-fw-new-cidr" placeholder="Address, e.g. 203.0.113.0/24" autocomplete="off" />
          <input type="text" id="rackflow-fw-new-comment" placeholder="Note (optional)" autocomplete="off" />
          <button type="button" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-fw-add">Add rule</button>
        </div>
        {if $rackflow_firewall.available_groups|@count}
        <div class="rf-ca__slot-form">
          <select id="rackflow-fw-group" aria-label="Rule group">
            {foreach from=$rackflow_firewall.available_groups item=g}
            <option value="{$g.id|escape}">{$g.name|escape}</option>
            {/foreach}
          </select>
          <button type="button" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-fw-add-group">Add group of rules</button>
        </div>
        {/if}
        <div class="rf-ca__slot-form">
          <button type="button" class="rf-ca__btn rf-ca__btn--secondary" id="rackflow-fw-save" data-rf-fw-op="rules" disabled>Save rules</button>
          <span id="rackflow-fw-presets"></span>
        </div>
        {/if}
        {/if}
        <p class="rf-ca__note" id="rackflow-fw-msg" hidden></p>
        <button type="button" class="rf-ca__btn rf-ca__btn--ghost" id="rackflow-fw-reload" hidden>Reload the page</button>
      </div>
      {/if}
    </div>
  </div>
  {if $rackflow_plugin_panels_html}
  <div class="rf-ca__plugins">{$rackflow_plugin_panels_html}</div>
  {/if}
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
      window.open(vnc.href, 'rackflow_vnc', 'width=1400,height=960,resizable=yes,scrollbars=yes');
    });
  }
  var kvm = document.getElementById('rackflow-kvm-launch');
  if (kvm) {
    kvm.addEventListener('click', function (e) {
      e.preventDefault();
      window.open(kvm.href, 'rackflow_kvm', 'width=1400,height=960,resizable=yes,scrollbars=yes');
    });
  }
  var sol = document.getElementById('rackflow-sol-launch');
  if (sol) {
    sol.addEventListener('click', function (e) {
      e.preventDefault();
      window.open(sol.href, 'rackflow_sol', 'width=1400,height=960,resizable=yes,scrollbars=yes');
    });
  }

  var vmBox = document.getElementById('rackflow-virtual-media');
  if (vmBox) {
    function setVmMsg(text, isError) {
      var el = document.getElementById('rackflow-virtual-media-msg');
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
    function runVirtualMedia(op) {
      var url = vmBox.getAttribute('data-rf-virtual-media-action') || '';
      var sid = vmBox.getAttribute('data-rf-service-id') || '';
      var iso = document.getElementById('rackflow-virtual-media-iso');
      var boot = document.getElementById('rackflow-virtual-media-boot-once');
      if (!url || !sid) {
        setVmMsg('Virtual media URL missing. Reload the page.', true);
        return;
      }
      if (op === 'insert' && (!iso || !iso.value)) {
        setVmMsg('Select an ISO.', true);
        return;
      }
      var body = new URLSearchParams();
      body.set('serviceid', sid);
      body.set('op', op);
      if (iso && iso.value) { body.set('filename', iso.value); }
      if (boot && boot.checked) { body.set('boot_once', '1'); }
      setVmMsg('Working…', false);
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
        if (!data || !data.ok) {
          setVmMsg((data && data.error) ? data.error : 'Request failed.', true);
          return;
        }
        setVmMsg(data.message || 'Done.', false);
        var statusEl = document.getElementById('rackflow-virtual-media-status');
        if (statusEl && data.data) {
          statusEl.textContent = data.data.inserted
            ? ('Mounted: ' + (data.data.image_name || 'ISO'))
            : 'No virtual CD inserted';
        }
      }).catch(function (err) {
        setVmMsg(err && err.message ? err.message : 'Request failed.', true);
      });
    }
    var mountBtn = document.getElementById('rackflow-virtual-media-mount');
    var ejectBtn = document.getElementById('rackflow-virtual-media-eject');
    if (mountBtn) { mountBtn.addEventListener('click', function () { runVirtualMedia('insert'); }); }
    if (ejectBtn) { ejectBtn.addEventListener('click', function () { runVirtualMedia('eject'); }); }
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

  function rfCopyText(text, btn, doneLabel) {
    if (!text) { return; }
    var label = doneLabel || 'Copied';
    var original = btn ? btn.textContent : '';
    function markDone() {
      if (!btn) { return; }
      btn.textContent = label;
      window.setTimeout(function () { btn.textContent = original; }, 2000);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(markDone).catch(function () {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e) {}
        document.body.removeChild(ta);
        markDone();
      });
      return;
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    markDone();
  }

  var proxyCopyBtn = document.getElementById('rackflow-proxy-copy-all');
  if (proxyCopyBtn) {
    proxyCopyBtn.addEventListener('click', function () {
      var linesEl = document.getElementById('rackflow-proxy-endpoint-lines');
      rfCopyText(linesEl ? linesEl.value : '', proxyCopyBtn, 'Copied');
    });
  }

  var fastPathCopyBtn = document.getElementById('rackflow-fast-path-copy');
  if (fastPathCopyBtn) {
    fastPathCopyBtn.addEventListener('click', function () {
      var linesEl = document.getElementById('rackflow-fast-path-lines');
      rfCopyText(linesEl ? linesEl.value : '', fastPathCopyBtn, 'Copied');
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-rf-fast-path-op]'), function (btn) {
    btn.addEventListener('click', function () {
      var op = btn.getAttribute('data-rf-fast-path-op');
      if (op === 'off' && !window.confirm('Turn private proxy access off? Your servers lose the private route to these proxies.')) {
        return;
      }
      var url = btn.getAttribute('data-rf-fast-path-action') || '';
      var sid = btn.getAttribute('data-rf-service-id') || '';
      var msgEl = document.getElementById('rackflow-fast-path-msg');
      function setMsg(text, isError) {
        if (!msgEl) { return; }
        msgEl.hidden = false;
        msgEl.textContent = text;
        msgEl.style.color = isError ? '#b42318' : '#0a7a32';
      }
      if (!url || !sid) {
        setMsg('Action URL missing. Reload the page.', true);
        return;
      }
      btn.disabled = true;
      setMsg('Working…', false);
      var body = new URLSearchParams();
      body.set('serviceid', sid);
      body.set('op', op);
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
        btn.disabled = false;
        if (data && data.ok) {
          setMsg((data.message || 'Done.') + ' Reloading…', false);
          window.setTimeout(function () { window.location.reload(); }, 600);
          return;
        }
        setMsg((data && data.error) ? data.error : 'Request failed.', true);
      }).catch(function (e) {
        btn.disabled = false;
        setMsg(e && e.message ? e.message : 'Request failed.', true);
      });
    });
  });

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

{if $rackflow_firewall_panel && !$rackflow_firewall_error}
{literal}
<script>
(function () {
  var box = document.getElementById('rackflow-firewall');
  if (!box) { return; }
  var data;
  try { data = JSON.parse(box.getAttribute('data-rf-fw')); } catch (e) { return; }
  var url = box.getAttribute('data-rf-fw-action');
  var sid = box.getAttribute('data-rf-service-id');
  var canEdit = box.getAttribute('data-rf-can-edit') === '1';
  var revision = data.revision;
  var rules = (data.rules || []).map(function (r) { return JSON.parse(JSON.stringify(r)); });
  var groupNames = {};
  (data.groups || []).forEach(function (g) { groupNames[g.id] = g.name; });
  var body = document.getElementById('rackflow-fw-rules-body');
  var saveBtn = document.getElementById('rackflow-fw-save');
  var busy = false;

  function msg(text, isError) {
    var el = document.getElementById('rackflow-fw-msg');
    if (!el) { return; }
    el.hidden = !text;
    el.textContent = text || '';
    el.style.color = isError ? '#b42318' : '#0a7a32';
  }
  function val(id) {
    var el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }
  function cell(text) {
    var td = document.createElement('td');
    td.textContent = text;
    return td;
  }
  function button(label, title, handler, disabled) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'rf-ca__btn rf-ca__btn--ghost';
    b.textContent = label;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.disabled = !!disabled;
    b.addEventListener('click', handler);
    return b;
  }
  function changed() {
    if (saveBtn) { saveBtn.disabled = false; }
    msg('You have unsaved changes to your rules.', false);
    render();
  }
  function render() {
    while (body.firstChild) { body.removeChild(body.firstChild); }
    if (!rules.length) {
      var empty = document.createElement('tr');
      var td = cell('You have no rules yet.');
      td.colSpan = 9;
      empty.appendChild(td);
      body.appendChild(empty);
      return;
    }
    rules.forEach(function (rule, i) {
      var tr = document.createElement('tr');
      if (rule.enabled === false) { tr.className = 'rf-ca__fw-off'; }
      var order = document.createElement('td');
      order.className = 'rf-ca__fw-actions';
      if (canEdit) {
        order.appendChild(button('Up', 'Move rule ' + (i + 1) + ' up', function () {
          rules.splice(i - 1, 0, rules.splice(i, 1)[0]); changed();
        }, i === 0));
        order.appendChild(button('Down', 'Move rule ' + (i + 1) + ' down', function () {
          rules.splice(i + 1, 0, rules.splice(i, 1)[0]); changed();
        }, i === rules.length - 1));
      } else {
        order.textContent = String(i + 1);
      }
      tr.appendChild(order);
      var on = document.createElement('td');
      var box2 = document.createElement('input');
      box2.type = 'checkbox';
      box2.checked = rule.enabled !== false;
      box2.disabled = !canEdit;
      box2.setAttribute('aria-label', 'Rule ' + (i + 1) + ' on');
      box2.addEventListener('change', function () { rule.enabled = box2.checked; changed(); });
      on.appendChild(box2);
      tr.appendChild(on);
      if (rule.kind === 'group') {
        var label = cell('Group: ' + (groupNames[rule.group_id] || ('#' + rule.group_id)));
        label.colSpan = 6;
        tr.appendChild(label);
      } else {
        tr.appendChild(cell(rule.direction === 'out' ? 'Outgoing' : 'Incoming'));
        tr.appendChild(cell(String(rule.action || '')));
        tr.appendChild(cell(rule.protocol || 'any'));
        tr.appendChild(cell(rule.ports || (rule.icmp_type || '-')));
        var addr = (rule.remote_cidrs && rule.remote_cidrs.length) ? rule.remote_cidrs.join(', ') : (rule.remote_ipset_id ? 'IP set #' + rule.remote_ipset_id : 'any');
        tr.appendChild(cell(addr));
        tr.appendChild(cell(rule.comment || ''));
      }
      var del = document.createElement('td');
      if (canEdit) {
        del.appendChild(button('Delete', 'Delete rule ' + (i + 1), function () { rules.splice(i, 1); changed(); }));
      }
      tr.appendChild(del);
      body.appendChild(tr);
    });
  }

  function send(op, extra, confirmed) {
    if (busy) { return; }
    var form = new URLSearchParams();
    form.set('serviceid', sid);
    form.set('op', op);
    form.set('revision', String(revision));
    Object.keys(extra).forEach(function (k) { form.set(k, extra[k]); });
    if (confirmed) { form.set('confirm_interruption', '1'); }
    busy = true;
    msg('Working...', false);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' },
      body: form.toString(),
      credentials: 'same-origin'
    }).then(function (res) {
      return res.json().catch(function () { return { ok: false, error: 'Unexpected response (' + res.status + ')' }; });
    }).then(function (reply) {
      busy = false;
      if (reply && reply.ok) {
        msg(reply.message || 'Done.', false);
        window.setTimeout(function () { window.location.reload(); }, 800);
        return;
      }
      if (reply && reply.code === 'interruption_confirmation_required') {
        msg('', false);
        if (window.confirm('Turning the firewall on briefly interrupts this server\'s network, for well under a second. Go ahead?')) { send(op, extra, true); }
        return;
      }
      if (reply && reply.code === 'firewall_changed') {
        msg('Your firewall was changed somewhere else, for example in the RackFlow portal, so nothing was saved. Reload the page to see the latest rules, then make your change again.', true);
        var reload = document.getElementById('rackflow-fw-reload');
        if (reload) { reload.hidden = false; }
        return;
      }
      msg((reply && reply.error) ? reply.error : 'Request failed.', true);
    }).catch(function (err) {
      busy = false;
      msg(err && err.message ? err.message : 'Request failed.', true);
    });
  }

  var reloadBtn = document.getElementById('rackflow-fw-reload');
  if (reloadBtn) { reloadBtn.addEventListener('click', function () { window.location.reload(); }); }
  render();
  if (!canEdit) { return; }

  var addBtn = document.getElementById('rackflow-fw-add');
  if (addBtn) {
    addBtn.addEventListener('click', function () {
      var rule = {
        kind: 'rule',
        direction: val('rackflow-fw-new-direction'),
        action: val('rackflow-fw-new-action'),
        protocol: val('rackflow-fw-new-protocol') || null,
        ports: val('rackflow-fw-new-ports') || null,
        remote_cidrs: val('rackflow-fw-new-cidr') ? [val('rackflow-fw-new-cidr')] : null,
        comment: val('rackflow-fw-new-comment') || null,
        enabled: true
      };
      rules.push(rule);
      ['ports', 'cidr', 'comment'].forEach(function (k) { var el = document.getElementById('rackflow-fw-new-' + k); if (el) { el.value = ''; } });
      changed();
    });
  }
  var addGroup = document.getElementById('rackflow-fw-add-group');
  if (addGroup) {
    addGroup.addEventListener('click', function () {
      var id = parseInt(val('rackflow-fw-group'), 10);
      if (!id) { return; }
      if (rules.some(function (r) { return r.kind === 'group' && r.group_id === id; })) {
        msg('That group is already in your rules.', true);
        return;
      }
      rules.push({ kind: 'group', group_id: id, enabled: true, comment: null });
      changed();
    });
  }
  var presetBox = document.getElementById('rackflow-fw-presets');
  if (presetBox) {
    (data.presets || []).forEach(function (p) {
      var b = button('Add ' + p.label, 'Add the ' + p.label + ' rules', function () {
        if (saveBtn && !saveBtn.disabled && !window.confirm('Adding this set saves it now and drops your unsaved changes. Continue?')) { return; }
        send('preset', { preset: p.key }, false);
      });
      presetBox.appendChild(b);
    });
  }
  box.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-rf-fw-op]') : null;
    if (!btn || btn.disabled) { return; }
    var op = btn.getAttribute('data-rf-fw-op');
    if (op === 'enable') {
      if (btn.getAttribute('data-rf-needs-interruption') === '1') {
        if (!window.confirm('Turning the firewall on briefly interrupts this server\'s network, for well under a second. Go ahead?')) { return; }
        send('enable', {}, true);
      } else {
        send('enable', {}, false);
      }
    } else if (op === 'disable') {
      if (!window.confirm('Turn the client firewall off? Your rules are kept, but they stop applying.')) { return; }
      send('disable', {}, false);
    } else if (op === 'policy') {
      send('policy', { policy_in: val('rackflow-fw-policy-in'), policy_out: val('rackflow-fw-policy-out') }, false);
    } else if (op === 'rules') {
      if (!window.confirm('Replace your rules with the list shown?')) { return; }
      send('rules', { rules: JSON.stringify(rules) }, false);
    }
  });
})();
</script>
{/literal}
{/if}

{if $rackflow_slot_panel || $rackflow_vm_slots|@count}
{literal}
<script>
(function () {
  var box = document.querySelector('#rackflow-client-area [data-rf-slot-box]');
  if (!box) { return; }
  var url = box.getAttribute('data-rf-slot-action');
  var sid = box.getAttribute('data-rf-service-id');
  function msg(text, isError) {
    var el = document.getElementById('rackflow-slot-msg');
    if (!el) { return; }
    el.hidden = !text;
    el.textContent = text || '';
    el.style.color = isError ? '#b42318' : '#0a7a32';
  }
  function val(id) {
    var el = document.getElementById(id);
    return el ? el.value : '';
  }
  function send(op, extra, onFail) {
    var body = new URLSearchParams();
    body.set('serviceid', sid);
    body.set('op', op);
    Object.keys(extra).forEach(function (k) { body.set(k, extra[k]); });
    msg('Working...', false);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' },
      body: body.toString(),
      credentials: 'same-origin'
    }).then(function (res) {
      return res.json().catch(function () { return { ok: false, error: 'Unexpected response (' + res.status + ')' }; });
    }).then(function (data) {
      if (data && data.ok) {
        msg(data.message || 'Done.', false);
        window.setTimeout(function () { window.location.reload(); }, 800);
        return;
      }
      msg((data && data.error) ? data.error : 'Request failed.', true);
      if (onFail) { onFail(); }
    }).catch(function (err) {
      msg(err && err.message ? err.message : 'Request failed.', true);
      if (onFail) { onFail(); }
    });
  }
  var buttons = box.querySelectorAll('[data-rf-slot-op]');
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener('click', function () {
      var btn = this;
      var op = btn.getAttribute('data-rf-slot-op');
      var extra = {};
      if (op === 'retain') {
        var slotSel = document.getElementById('rackflow-slot-choice');
        extra.slot_id = val('rackflow-slot-choice');
        extra.volid = val('rackflow-slot-backup');
        extra.name = val('rackflow-slot-name');
        if (!extra.volid) { msg('There is no customer backup to keep yet. Create one first.', true); return; }
        var occupied = slotSel && slotSel.selectedOptions.length && slotSel.selectedOptions[0].getAttribute('data-occupied') === '1';
        if (occupied) {
          if (!window.confirm('This slot already holds a backup. Replace it? The old one is deleted.')) { return; }
          extra.replace = '1';
        }
      } else if (op === 'restore') {
        extra.target_id = val('rackflow-slot-target');
        var sel = document.getElementById('rackflow-slot-target');
        var label = sel && sel.selectedOptions.length ? sel.selectedOptions[0].textContent : 'the chosen VM';
        if (!window.confirm('Restore this backup onto ' + label + '? Everything on that VM will be replaced and cannot be recovered.')) { return; }
        extra.confirm_data_loss = '1';
      } else if (op === 'release') {
        if (!window.confirm(btn.getAttribute('data-rf-confirm-text') || 'Release this backup?')) { return; }
      }
      btn.disabled = true;
      send(op, extra, function () { btn.disabled = false; });
    });
  }
})();
</script>
{/literal}
{/if}
