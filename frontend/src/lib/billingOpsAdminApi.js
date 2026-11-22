// Admin API helpers for the billing automation screens: jobs, non-payment policies,
// cancellation requests and email templates. Money is integer cents everywhere in the API.

import { describeDetail, newIdempotencyKey } from './billingAdminApi.js';

const API_BASE = '/api';

export { newIdempotencyKey };

/**
 * A failed response becomes an Error with the API's message. `status` and the raw
 * `detail` are kept; `errors` lists the field, line and message of each template
 * error when the API answers 422 `template_invalid`.
 */
async function request(path, { method = 'GET', body, headers, errorMessage } = {}) {
  const requestHeaders = new Headers(headers);
  if (body !== undefined && !requestHeaders.has('Content-Type')) {
    requestHeaders.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: requestHeaders,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const fallback = response.status >= 500 ? `${errorMessage || 'The request failed'} (server error ${response.status})` : errorMessage;
    const error = new Error(describeDetail(payload.detail, response.status, fallback));
    error.status = response.status;
    error.detail = payload.detail;
    error.errors = payload.detail && typeof payload.detail === 'object' && Array.isArray(payload.detail.errors) ? payload.detail.errors : [];
    error.retryAfter = response.headers.get('Retry-After');
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function query(filters = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, value);
  });
  const text = params.toString();
  return text ? `?${text}` : '';
}

// --- Jobs (app/api/billing_jobs_admin.py) ---

export const listJobs = () =>
  request('/admin/billing/jobs', { errorMessage: 'Failed to load the billing jobs' });
export const changeJobMode = (job, to) =>
  request(`/admin/billing/jobs/${encodeURIComponent(job)}/mode`, {
    method: 'POST',
    body: { to, confirm: true },
    errorMessage: 'Failed to change the job mode',
  });
export const runJob = (job, dryRun = false) =>
  request(`/admin/billing/jobs/${encodeURIComponent(job)}/run`, {
    method: 'POST',
    body: { dry_run: dryRun },
    errorMessage: 'The job run failed',
  });
export const listJobRuns = (job, filters = {}) =>
  request(`/admin/billing/jobs/${encodeURIComponent(job)}/runs${query(filters)}`, {
    errorMessage: 'Failed to load the job runs',
  });
export const getJobRun = (runId, filters = {}) =>
  request(`/admin/billing/jobs/runs/${runId}${query(filters)}`, { errorMessage: 'Failed to load the run' });
export const getJobForecast = (job, days = 7) =>
  request(`/admin/billing/jobs/${encodeURIComponent(job)}/forecast${query({ days })}`, {
    errorMessage: 'Failed to load the forecast',
  });

// --- Non-payment policies (app/api/billing_cancellations.py) ---

export const listNonpaymentPolicies = () =>
  request('/admin/billing/nonpayment-policies', { errorMessage: 'Failed to load the non-payment policies' });
export const saveNonpaymentPolicy = (body) =>
  request('/admin/billing/nonpayment-policies', {
    method: 'PUT',
    body,
    errorMessage: 'Failed to save the policy',
  });
export const listPolicyProducts = () =>
  request('/admin/store/products?limit=500', { errorMessage: 'Failed to list products' });

// --- Cancellation requests (app/api/billing_cancellations.py) ---

export const listCancellationRequests = (filters = {}) =>
  request(`/admin/billing/cancellation-requests${query(filters)}`, {
    errorMessage: 'Failed to load the cancellation requests',
  });
export const requestCancellation = (body, key) =>
  request('/admin/billing/cancellation-requests', {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
    errorMessage: 'Failed to request the cancellation',
  });
export const withdrawCancellation = (id) =>
  request(`/admin/billing/cancellation-requests/${id}/withdraw`, {
    method: 'POST',
    errorMessage: 'Failed to withdraw the cancellation request',
  });
export const searchServices = (q) =>
  request(`/admin/services${query({ q, limit: 15 })}`, { errorMessage: 'Failed to search services' });

// --- Email templates (app/api/email_templates_admin.py) ---

const TEMPLATES = '/admin/commerce/email-templates';

export const listEmailTemplates = (filters = {}) =>
  request(`${TEMPLATES}${query(filters)}`, { errorMessage: 'Failed to load the email templates' });
export const listEmailEvents = () =>
  request(`${TEMPLATES}/events`, { errorMessage: 'Failed to load the email events' });
export const getEmailTemplate = (id) =>
  request(`${TEMPLATES}/${id}`, { errorMessage: 'Failed to load the email template' });
export const createEmailTemplate = (body) =>
  request(TEMPLATES, { method: 'POST', body, errorMessage: 'Failed to create the email template' });
export const saveEmailTemplate = (id, body) =>
  request(`${TEMPLATES}/${id}`, { method: 'PUT', body, errorMessage: 'Failed to save the email template' });
export const previewEmailTemplate = (id, body) =>
  request(`${TEMPLATES}/${id}/preview`, { method: 'POST', body, errorMessage: 'Failed to render the preview' });
export const testSendEmailTemplate = (id, body) =>
  request(`${TEMPLATES}/${id}/test-send`, { method: 'POST', body, errorMessage: 'Failed to send the test email' });
export const resetEmailTemplate = (id) =>
  request(`${TEMPLATES}/${id}/reset`, { method: 'POST', errorMessage: 'Failed to reset the email template' });
export const listEmailTemplateVersions = (id) =>
  request(`${TEMPLATES}/${id}/versions`, { errorMessage: 'Failed to load the version history' });
export const restoreEmailTemplateVersion = (id, version) =>
  request(`${TEMPLATES}/${id}/restore/${version}`, {
    method: 'POST',
    errorMessage: 'Failed to restore the version',
  });
