<script>
  import VncViewer from './VncViewer.svelte';
  import SerialConsole from './SerialConsole.svelte';
  import { consoleVmPowerAction, refreshVmVncSession } from '../lib/api.js';

  // `session` is the { ws_token, ws_path, vnc_password, expires_in,
  // console_type, guest_username, guest_password } payload from redeem /
  // session mint. `console_type` picks the viewer.
  export let session;

  let viewer;
  let passwordVisible = false;
  let powerBusy = false;
  let chromeMessage = '';
  let copyFlash = false;

  $: hasPassword = !!(session?.guest_password);
  $: guestUser = session?.guest_username || '';

  // Reconnect must mint a fresh Proxmox proxy (old tickets die when the
  // upstream WS closes). Keep the same Rackflow ws_token; update the
  // in-memory session so the viewer gets the new vnc_password.
  async function refreshSession() {
    const fresh = await refreshVmVncSession(session.ws_token);
    session = { ...session, ...fresh };
    return session;
  }

  async function power(action) {
    if (powerBusy || !session?.ws_token) return;
    powerBusy = true;
    chromeMessage = '';
    try {
      await consoleVmPowerAction(session.ws_token, action);
      chromeMessage =
        action === 'on'
          ? 'Power on sent.'
          : action === 'off'
            ? 'Power off sent.'
            : 'Reboot sent.';
    } catch (e) {
      chromeMessage = e.message || String(e);
    } finally {
      powerBusy = false;
    }
  }

  function togglePassword() {
    if (!hasPassword) return;
    passwordVisible = !passwordVisible;
  }

  async function copyPassword() {
    if (!hasPassword) return;
    chromeMessage = '';
    try {
      await navigator.clipboard.writeText(session.guest_password);
      copyFlash = true;
      setTimeout(() => {
        copyFlash = false;
      }, 1500);
    } catch (e) {
      chromeMessage = `Could not copy password: ${e.message || e}`;
    }
  }

  async function pastePassword() {
    if (!hasPassword || !viewer?.typeText) return;
    chromeMessage = '';
    try {
      // submit=true sends Return after the password so macOS/Linux login
      // screens actually attempt unlock (manual Enter is easy to miss when
      // focus is still on the chrome button).
      await viewer.typeText(session.guest_password, { submit: true, delayMs: 25 });
      chromeMessage = 'Password typed into console.';
    } catch (e) {
      chromeMessage = e.message || String(e);
    }
  }
</script>

<div class="console-shell">
  <div class="console-chrome">
    <div class="chrome-group">
      <span class="chrome-label">Power</span>
      <button type="button" class="chrome-btn" disabled={powerBusy} on:click={() => power('on')}>On</button>
      <button type="button" class="chrome-btn" disabled={powerBusy} on:click={() => power('off')}>Off</button>
      <button type="button" class="chrome-btn" disabled={powerBusy} on:click={() => power('reboot')}>Reboot</button>
    </div>

    <div class="chrome-group guest-creds">
      <span class="chrome-label">Guest</span>
      {#if guestUser}
        <code class="guest-user" title="Guest username">{guestUser}</code>
      {/if}
      {#if hasPassword}
        <button
          type="button"
          class="password-field"
          title={passwordVisible ? 'Hide password' : 'Click to reveal password'}
          on:click={togglePassword}
        >
          {passwordVisible ? session.guest_password : '••••••••'}
        </button>
        <button type="button" class="chrome-btn" on:click={copyPassword}>
          {copyFlash ? 'Copied' : 'Copy'}
        </button>
        <button type="button" class="chrome-btn" on:click={pastePassword}>Paste Password</button>
      {:else}
        <span class="chrome-muted">No stored password</span>
      {/if}
    </div>

    {#if chromeMessage}
      <span class="chrome-msg">{chromeMessage}</span>
    {/if}
  </div>

  <div class="console-body">
    {#if session.console_type === 'serial'}
      <SerialConsole
        bind:this={viewer}
        wsToken={session.ws_token}
        wsPath={session.ws_path}
        {refreshSession}
      />
    {:else}
      <VncViewer
        bind:this={viewer}
        wsToken={session.ws_token}
        wsPath={session.ws_path}
        vncPassword={session.vnc_password}
        {refreshSession}
      />
    {/if}
  </div>
</div>

<style>
  .console-shell {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 480px;
  }

  .console-chrome {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 20px;
    padding: 8px 12px;
    background: #16171c;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    color: #e6e6e6;
    font-size: 12px;
  }

  .chrome-group {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }

  .chrome-label {
    font-weight: 600;
    color: #9a9a9a;
    margin-right: 2px;
  }

  .chrome-btn {
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font-size: 12px;
  }

  .chrome-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }

  .chrome-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .guest-user {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
  }

  .password-field {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: rgba(0, 0, 0, 0.35);
    color: inherit;
    cursor: pointer;
    min-width: 7em;
    text-align: left;
  }

  .password-field:hover {
    border-color: rgba(255, 255, 255, 0.35);
  }

  .chrome-muted {
    color: #6a6a6a;
  }

  .chrome-msg {
    margin-left: auto;
    color: #b8b8b8;
    font-size: 12px;
  }

  .console-body {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .console-body :global(.vnc-viewer),
  .console-body :global(.serial-viewer) {
    border-radius: 0;
    min-height: 0;
    flex: 1;
  }
</style>
