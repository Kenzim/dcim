// Every HTTP call of the VM firewall screens. Paths and shapes follow docs/vm-firewall.md section 10;
// anything found to differ from the finished routes is fixed here and nowhere else.

const API_BASE = '/api';
const IMPERSONATION_TOKEN_KEY = 'rf_impersonate_token';

function impersonationHeaders() {
  try {
    const token = window.sessionStorage.getItem(IMPERSONATION_TOKEN_KEY);
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch (_) {
    return {};
  }
}

function query(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value);
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

/**
 * A failed firewall call. The server answers `{code, field, message, ...}`, either as the whole body
 * or under `detail`; `preview` (confirmation_required), `reasons` (not_eligible) and
 * `current_revision` (firewall_changed) are carried over when present.
 */
export class FirewallApiError extends Error {
  constructor(message, { status = 0, code = '', field = null, body = null } = {}) {
    super(message);
    this.name = 'FirewallApiError';
    this.status = status;
    this.code = code;
    this.field = field;
    this.body = body;
    this.preview = body?.preview ?? null;
    this.reasons = Array.isArray(body?.reasons) ? body.reasons : [];
    this.currentRevision = body?.current_revision ?? null;
  }
}

function errorFrom(status, body, fallback) {
  const source = body && typeof body.detail === 'object' && body.detail !== null && !Array.isArray(body.detail) ? body.detail : body;
  let message = source?.message;
  if (!message && typeof body?.detail === 'string') message = body.detail;
  if (!message && Array.isArray(body?.detail)) message = body.detail.map((item) => item?.msg).filter(Boolean).join('; ');
  return new FirewallApiError(message || fallback, {
    status,
    code: source?.code || '',
    field: source?.field ?? null,
    body: source,
  });
}

async function request(path, { method = 'GET', body, params, client = false, fallback = 'The firewall request failed' } = {}) {
  const headers = client ? { ...impersonationHeaders() } : {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`${API_BASE}${path}${query(params)}`, {
    method,
    headers,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw errorFrom(response.status, data, fallback);
  return data;
}

const isStaff = (audience) => audience === 'staff';

/** Route prefix of one VM's firewall. */
function servicePath(audience, serviceId) {
  return isStaff(audience) ? `/admin/vm-firewall/services/${serviceId}` : `/client/services/${serviceId}/firewall`;
}

/** Route prefix of groups and IP sets. */
function objectPath(audience) {
  return isStaff(audience) ? '/admin/vm-firewall' : '/client/firewall';
}

const asList = (data, key) => (Array.isArray(data) ? data : Array.isArray(data?.[key]) ? data[key] : Array.isArray(data?.items) ? data.items : []);

/** One firewall rule as the screens use it: a group reference's id is always `group_id`. */
export function normaliseRule(rule) {
  return { ...rule, group_id: rule.group_id ?? rule.ref_group_id ?? null };
}

/** The firewall view of one VM, with the optional parts defaulted so screens need no guards. */
export function normaliseView(raw) {
  const view = raw || {};
  return {
    ...view,
    rules: (view.rules || []).map(normaliseRule),
    groups: view.groups || [],
    ipsets: view.ipsets || null,
    suggested_presets: view.suggested_presets || [],
    limits: view.limits || {},
    baseline: { rules: [], ...(view.baseline || {}) },
    reasons: view.reasons || view.render_reasons || view.not_eligible_reasons || [],
    admin_overrides: view.admin_overrides || {},
  };
}

// --- One VM ---

export async function getFirewall(audience, serviceId) {
  return normaliseView(
    await request(servicePath(audience, serviceId), { client: !isStaff(audience), fallback: 'Failed to load the firewall' }),
  );
}

/** `body` carries `revision` and any of client_enabled, policy_in, policy_out, log_level_in, log_level_out, confirm_interruption. */
export async function updateFirewall(audience, serviceId, body) {
  return normaliseView(
    await request(servicePath(audience, serviceId), {
      method: 'PUT',
      body,
      client: !isStaff(audience),
      fallback: 'Failed to save the firewall',
    }),
  );
}

export async function replaceRules(audience, serviceId, revision, rules) {
  return normaliseView(
    await request(`${servicePath(audience, serviceId)}/rules`, {
      method: 'PUT',
      body: { revision, rules },
      client: !isStaff(audience),
      fallback: 'Failed to save the rules',
    }),
  );
}

export async function applyPreset(audience, serviceId, preset, revision) {
  return normaliseView(
    await request(`${servicePath(audience, serviceId)}/presets/${encodeURIComponent(preset)}`, {
      method: 'POST',
      body: { revision },
      client: !isStaff(audience),
      fallback: 'Failed to add the preset',
    }),
  );
}

export async function listPresets() {
  const data = await request('/client/firewall/presets', { client: true, fallback: 'Failed to load the presets' });
  return asList(data, 'presets');
}

/** Staff only: managed, excluded, enforce_baseline, admin_overrides (plus the client fields). */
export async function updateAdminFields(serviceId, body) {
  return normaliseView(
    await request(servicePath('staff', serviceId), { method: 'PUT', body, fallback: 'Failed to save the admin settings' }),
  );
}

export function applyNow(serviceId) {
  return request(`${servicePath('staff', serviceId)}/apply`, { method: 'POST', fallback: 'Failed to apply the firewall' });
}

export function checkNow(serviceId) {
  return request(`${servicePath('staff', serviceId)}/check`, { method: 'POST', fallback: 'Failed to check the firewall' });
}

// --- Groups and IP sets (client: the caller's own; staff: admin ones) ---

function objectApi(kind, listKey) {
  return {
    list: async (audience) =>
      asList(await request(`${objectPath(audience)}/${kind}`, { client: !isStaff(audience), fallback: `Failed to load ${kind}` }), listKey),
    get: (audience, id) =>
      request(`${objectPath(audience)}/${kind}/${id}`, { client: !isStaff(audience), fallback: `Failed to load ${kind}` }),
    create: (audience, body) =>
      request(`${objectPath(audience)}/${kind}`, { method: 'POST', body, client: !isStaff(audience), fallback: `Failed to create` }),
    update: (audience, id, body) =>
      request(`${objectPath(audience)}/${kind}/${id}`, { method: 'PUT', body, client: !isStaff(audience), fallback: 'Failed to save' }),
    remove: (audience, id, { detach = false } = {}) =>
      request(`${objectPath(audience)}/${kind}/${id}`, {
        method: 'DELETE',
        params: { detach: detach ? 'true' : undefined },
        client: !isStaff(audience),
        fallback: 'Failed to delete',
      }),
  };
}

const groupApi = objectApi('groups', 'groups');
const ipsetApi = objectApi('ipsets', 'ipsets');

export const listGroups = groupApi.list;
export const getGroup = groupApi.get;
export const createGroup = groupApi.create;
export const updateGroup = groupApi.update;
export const deleteGroup = groupApi.remove;
export const listIpsets = ipsetApi.list;
export const getIpset = ipsetApi.get;
export const createIpset = ipsetApi.create;
export const updateIpset = ipsetApi.update;
export const deleteIpset = ipsetApi.remove;

// --- Admin page ---

const ADMIN = '/admin/vm-firewall';

export async function listLocations() {
  return asList(await request(`${ADMIN}/locations`, { fallback: 'Failed to load the locations' }), 'locations');
}

/** Any of enabled, nic_flag_on_provision, enforce_baseline_on_all, smtp_block_new_vms, anti_spoof_new_vms, ipv6_allowed_prefixes, staging, confirm_existing_vms. */
export function updateLocation(locationId, body) {
  return request(`${ADMIN}/locations/${locationId}`, { method: 'PUT', body, fallback: 'Failed to save the location' });
}

export function recheckReadiness(locationId) {
  return request(`${ADMIN}/locations/${locationId}/readiness`, { method: 'POST', fallback: 'Failed to re-check the location' });
}

/** What switching on enforce_baseline_on_all would change: {changed, service_ids, blip, blip_service_ids, nic_flag_unknown, nic_flag_unknown_service_ids}. */
export function previewRollout(locationId) {
  return request(`${ADMIN}/locations/${locationId}/rollout`, {
    method: 'POST',
    body: { dry_run: true },
    fallback: 'Failed to preview the rollout',
  });
}

export async function listBaselines() {
  return asList(await request(`${ADMIN}/baselines`, { fallback: 'Failed to load the baselines' }), 'baselines');
}

export const getBaseline = (id) => request(`${ADMIN}/baselines/${id}`, { fallback: 'Failed to load the baseline' });
export const createBaseline = (body) => request(`${ADMIN}/baselines`, { method: 'POST', body, fallback: 'Failed to create the baseline' });
export const updateBaseline = (id, body) =>
  request(`${ADMIN}/baselines/${id}`, { method: 'PUT', body, fallback: 'Failed to save the baseline' });
export const deleteBaseline = (id) => request(`${ADMIN}/baselines/${id}`, { method: 'DELETE', fallback: 'Failed to delete the baseline' });

export async function listStatus({ state, locationId, q } = {}) {
  return asList(
    await request(`${ADMIN}/services`, {
      params: { state, location_id: locationId, q },
      fallback: 'Failed to load the firewall status list',
    }),
    'services',
  );
}
