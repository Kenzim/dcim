{literal}
<style>
.rfr-ca {
  --rfr-ink: #1c2430;
  --rfr-muted: #5b6775;
  --rfr-line: #d5dce5;
  --rfr-bg: #f4f6f8;
  --rfr-card: #ffffff;
  --rfr-accent: #0f6e56;
  --rfr-accent-soft: #e7f5ef;
  --rfr-radius: 12px;
  width: 100%;
  max-width: none;
  margin: 0;
  text-align: left;
  color: var(--rfr-ink);
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
}
.rfr-ca *, .rfr-ca *::before, .rfr-ca *::after { box-sizing: border-box; }
.rfr-ca__card {
  background: var(--rfr-card);
  border: 1px solid var(--rfr-line);
  border-radius: var(--rfr-radius);
  overflow: hidden;
}
.rfr-ca__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  background: linear-gradient(135deg, #f7faf8 0%, #eef3f7 100%);
  border-bottom: 1px solid var(--rfr-line);
}
.rfr-ca__title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.rfr-ca__subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--rfr-muted);
  line-height: 1.4;
}
.rfr-ca__badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
  line-height: 1.2;
}
.rfr-ca__badge--on { background: #d8f3e3; color: #0b6b3a; }
.rfr-ca__badge--off { background: #e8ecf0; color: #4a5560; }
.rfr-ca__badge--warn { background: #fff3cd; color: #7a5b00; }
.rfr-ca__badge--unknown { background: #e8ecf0; color: #4a5560; }
.rfr-ca__badge-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.rfr-ca__body { padding: 18px 20px 20px; }
.rfr-ca__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.rfr-ca__stat {
  background: var(--rfr-bg);
  border: 1px solid var(--rfr-line);
  border-radius: 10px;
  padding: 12px 14px;
  min-height: 72px;
}
.rfr-ca__stat-label {
  display: block;
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--rfr-muted);
}
.rfr-ca__stat-value {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  word-break: break-word;
  line-height: 1.35;
}
.rfr-ca__stat-value code {
  font-size: 13px;
  background: transparent;
  padding: 0;
  color: inherit;
}
.rfr-ca__actions {
  display: grid;
  gap: 10px;
  margin-bottom: 4px;
}
.rfr-ca__action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--rfr-line);
  border-radius: 10px;
  background: #fff;
}
.rfr-ca__action-copy { min-width: 0; flex: 1; }
.rfr-ca__action-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 700;
}
.rfr-ca__action-help {
  margin: 0;
  font-size: 12px;
  color: var(--rfr-muted);
  line-height: 1.4;
}
.rfr-ca__btn,
a.rfr-ca__btn,
button.rfr-ca__btn {
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
.rfr-ca__btn:hover,
.rfr-ca__btn:focus,
.rfr-ca__btn:active,
a.rfr-ca__btn:hover,
a.rfr-ca__btn:focus {
  background: #0c5c48 !important;
  background-color: #0c5c48 !important;
  border-color: #0c5c48 !important;
  color: #fff !important;
  box-shadow: 0 0 0 3px rgba(15, 110, 86, 0.16);
  text-decoration: none !important;
}
.rfr-ca__controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--rfr-line);
}
.rfr-ca__btn--ghost,
a.rfr-ca__btn--ghost {
  background: #fff !important;
  background-color: #fff !important;
  color: #1c2430 !important;
  border-color: #d5dce5 !important;
  min-width: 110px;
}
.rfr-ca__btn--ghost:hover,
.rfr-ca__btn--ghost:focus,
a.rfr-ca__btn--ghost:hover,
a.rfr-ca__btn--ghost:focus {
  background: #f4f6f8 !important;
  background-color: #f4f6f8 !important;
  border-color: #b7c2cf !important;
  color: #1c2430 !important;
  box-shadow: none;
}
.rfr-ca__error {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #e8c7c7;
  border-radius: 10px;
  background: #fff3f3;
  color: #842029;
  font-size: 13px;
  line-height: 1.45;
}
.rfr-ca__note {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--rfr-line);
  border-radius: 10px;
  background: var(--rfr-bg);
  font-size: 13px;
  color: var(--rfr-muted);
  line-height: 1.45;
}
@media (max-width: 720px) {
  .rfr-ca__grid { grid-template-columns: 1fr; }
  .rfr-ca__head { flex-direction: column; align-items: flex-start; }
  .rfr-ca__action { flex-direction: column; align-items: stretch; }
  .rfr-ca__btn { width: 100%; min-width: 0; }
  .rfr-ca__controls .rfr-ca__btn { flex: 1 1 calc(50% - 8px); }
}
</style>
{/literal}

<div class="rfr-ca" id="rackflow-reseller-client-area">
  <div class="rfr-ca__card">
    <div class="rfr-ca__head">
      <div>
        <h3 class="rfr-ca__title">RackFlow Service</h3>
        <p class="rfr-ca__subtitle">Status and controls for this reseller-managed service.</p>
      </div>
      <span class="rfr-ca__badge {$rackflow_reseller_status_badge|escape}">
        <span class="rfr-ca__badge-dot" aria-hidden="true"></span>
        {$rackflow_reseller_status|escape}
      </span>
    </div>

    <div class="rfr-ca__body">
      {if $rackflow_reseller_error}
        <p class="rfr-ca__error">{$rackflow_reseller_error|escape}</p>
      {else}
        <div class="rfr-ca__grid">
          <div class="rfr-ca__stat">
            <span class="rfr-ca__stat-label">Service</span>
            <p class="rfr-ca__stat-value">{$rackflow_reseller_status|escape}</p>
          </div>
          <div class="rfr-ca__stat">
            <span class="rfr-ca__stat-label">Power</span>
            <p class="rfr-ca__stat-value">{$rackflow_reseller_power_state|escape}</p>
          </div>
          <div class="rfr-ca__stat">
            <span class="rfr-ca__stat-label">Primary IP</span>
            <p class="rfr-ca__stat-value">
              {if $rackflow_reseller_primary_ip}
                <code>{$rackflow_reseller_primary_ip|escape}</code>
              {else}
                Pending
              {/if}
            </p>
          </div>
          {if $rackflow_reseller_hostname}
          <div class="rfr-ca__stat">
            <span class="rfr-ca__stat-label">Hostname</span>
            <p class="rfr-ca__stat-value"><code>{$rackflow_reseller_hostname|escape}</code></p>
          </div>
          {/if}
        </div>

        {if $rackflow_reseller_portal_url}
        <div class="rfr-ca__actions">
          <div class="rfr-ca__action">
            <div class="rfr-ca__action-copy">
              <p class="rfr-ca__action-title">RackFlow portal</p>
              <p class="rfr-ca__action-help">Sign in and open your RackFlow dashboard in a new tab.</p>
            </div>
            <a class="rfr-ca__btn" href="{$rackflow_reseller_portal_url|escape}" target="_blank" rel="noopener">Open portal</a>
          </div>
        </div>
        {/if}

        {if $rackflow_reseller_power_available}
        <div class="rfr-ca__controls">
          <a class="rfr-ca__btn rfr-ca__btn--ghost" href="{$rackflow_reseller_power_on_url|escape}">Power on</a>
          <a class="rfr-ca__btn rfr-ca__btn--ghost" href="{$rackflow_reseller_power_off_url|escape}">Power off</a>
          <a class="rfr-ca__btn rfr-ca__btn--ghost" href="{$rackflow_reseller_reboot_url|escape}">Reboot</a>
        </div>
        {elseif !$rackflow_reseller_error}
        <p class="rfr-ca__note">Power controls are not available for this service type.</p>
        {/if}
      {/if}
    </div>
  </div>
</div>
