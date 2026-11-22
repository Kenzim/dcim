// Admin API helpers for billing phase 1 (series, engines, notes, refunds, credit, cycles).
// Money is integer cents everywhere in the API.

const API_BASE = '/api';

const ERROR_TEXT = {
  invoice_payment_pending: (d) =>
    `A payment on this invoice is still pending at the gateway${d.payment_id ? ` (payment #${d.payment_id})` : ''}. Wait for it to settle or expire, then try again.`,
  insufficient_credit: (d) =>
    `Not enough credit: ${cents(d.available_cents)} available, ${cents(d.requested_cents)} requested.`,
  already_in_mode: () => 'The engine is already in that mode.',
  switch_criterion_not_met: (d) => d.message || 'The switch criterion is not met.',
  rollback_refused: (d) => d.message || 'The engine cannot go back to that mode.',
  credit_engine_v2: (d) => d.message || 'Not allowed once the credit engine is v2.',
  setting_managed_elsewhere: (d) => d.message || 'This setting is managed elsewhere.',
  idempotency_conflict: () => 'A refund with this request key already exists with different details. Close the dialog and start again.',
  gateway_unavailable: (d) => d.message || 'The payment gateway is unavailable. The refund was not sent.',
  not_open: (d) => d.message || 'This refund is no longer open.',
  already_resolved: (d) => d.message || 'This item has already been resolved.',
  gateway_refund_ref_required: () => 'Enter the gateway refund reference to confirm a gateway refund as succeeded.',
  note_required: () => 'A note is needed to dismiss a review.',
  payment_required: () => 'Choose the payment to refund.',
  amount_exceeds_refundable: (d) => d.message || 'The amount is more than what is left to refund on this payment.',
  not_refundable: (d) => d.message || 'This payment cannot be refunded that way.',
  credit_note_not_allowed: (d) => d.message || 'A credit note cannot be issued for this refund.',
  insufficient_balance: (d) => d.message || 'The credit balance is too low to refund this top-up.',
  credit_unavailable: (d) => d.message || 'Account credit is not available yet.',
};

function cents(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n / 100).toFixed(2)}` : '?';
}

/** A readable message for the body of a failed API response. */
export function describeDetail(detail, status, fallback) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((item) => {
        const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
        return where ? `${where}: ${item.msg}` : item.msg;
      })
      .join('; ');
  }
  if (detail && typeof detail === 'object') {
    const render = ERROR_TEXT[detail.code];
    if (render) return render(detail);
    if (detail.message) return detail.message;
    if (detail.code) return String(detail.code).replaceAll('_', ' ');
  }
  if (status === 403) return 'You do not have permission to do that.';
  if (status === 404) return 'Not found.';
  return fallback || 'The request failed.';
}

async function request(path, { method = 'GET', body, headers, raw = false, errorMessage } = {}) {
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
    const error = new Error(describeDetail(payload.detail, response.status, errorMessage));
    error.status = response.status;
    error.detail = payload.detail;
    error.code = payload.detail && typeof payload.detail === 'object' ? payload.detail.code : undefined;
    throw error;
  }
  if (raw) return response;
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

/** A fresh key for one dialog; sent as Idempotency-Key so a repeated submit cannot double-issue. */
export function newIdempotencyKey(prefix = 'ui') {
  const random = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

/** Parse a decimal amount ("12.50") into integer cents; null when empty or invalid. */
export function parseCents(value, { allowZero = false, allowNegative = false } = {}) {
  const text = String(value ?? '').trim();
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(text);
  if (!match) return null;
  const negative = match[1] === '-';
  if (negative && !allowNegative) return null;
  const total = Number(match[2]) * 100 + Number((match[3] || '').padEnd(2, '0') || 0);
  if (!Number.isSafeInteger(total)) return null;
  if (total === 0 && !allowZero) return null;
  return negative ? -total : total;
}

export function centsToInput(value) {
  const n = Number(value);
  return Number.isFinite(n) ? (n / 100).toFixed(2) : '';
}

// --- Invoices (commerce admin) ---

export const getInvoice = (id) =>
  request(`/admin/commerce/invoices/${id}`, { errorMessage: 'Failed to load the invoice' });
export const listInvoices = (filters = {}) =>
  request(`/admin/commerce/invoices${query(filters)}`, { errorMessage: 'Failed to list invoices' });
export const markInvoicePaid = (id, body) =>
  request(`/admin/commerce/invoices/${id}/mark-paid`, { method: 'POST', body, errorMessage: 'Failed to mark the invoice paid' });
export const voidInvoice = (id, reason) =>
  request(`/admin/commerce/invoices/${id}/void`, {
    method: 'POST',
    body: reason ? { reason } : {},
    errorMessage: 'Failed to void the invoice',
  });
export const changeInvoiceDueDate = (id, dueAt) =>
  request(`/admin/commerce/invoices/${id}/due-date`, {
    method: 'PATCH',
    body: { due_at: dueAt },
    errorMessage: 'Failed to change the due date',
  });
export const listInvoiceAudit = (filters = {}) =>
  request(`/admin/commerce/audit-events${query(filters)}`, { errorMessage: 'Failed to load the invoice history' });
export const listBillingAccounts = (filters = {}) =>
  request(`/admin/commerce/billing-accounts${query(filters)}`, { errorMessage: 'Failed to list billing accounts' });
export const getBillingAccount = (id) =>
  request(`/admin/commerce/billing-accounts/${id}`, { errorMessage: 'Failed to load the billing account' });

// --- Credit and debit notes ---

export const issueCreditNote = (invoiceId, body, key) =>
  request(`/admin/billing/invoices/${invoiceId}/credit-notes`, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
    errorMessage: 'Failed to issue the credit note',
  });
export const issueDebitNote = (invoiceId, body, key) =>
  request(`/admin/billing/invoices/${invoiceId}/debit-notes`, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
    errorMessage: 'Failed to issue the debit note',
  });
export const listNotes = (filters = {}) =>
  request(`/admin/billing/adjustments${query(filters)}`, { errorMessage: 'Failed to list credit and debit notes' });
export const getNote = (id) =>
  request(`/admin/billing/adjustments/${id}`, { errorMessage: 'Failed to load the note' });
export async function downloadNotePdf(id) {
  const response = await request(`/admin/billing/adjustments/${id}/pdf`, {
    raw: true,
    errorMessage: 'Failed to download the note PDF',
  });
  return response.blob();
}

// --- Refunds ---

export const listRefunds = (filters = {}) =>
  request(`/admin/billing/refunds${query(filters)}`, { errorMessage: 'Failed to list refunds' });
export const createRefund = (body, key) =>
  request('/admin/billing/refunds', {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
    errorMessage: 'Failed to request the refund',
  });
export const dispatchRefund = (id) =>
  request(`/admin/billing/refunds/${id}/dispatch`, { method: 'POST', errorMessage: 'Failed to send the refund' });
export const resolveRefund = (id, body) =>
  request(`/admin/billing/refunds/${id}/resolve`, { method: 'POST', body, errorMessage: 'Failed to resolve the refund' });
export const listRefundReviews = (openOnly = true, limit = 200) =>
  request(`/admin/billing/refund-reviews${query({ open: openOnly, limit })}`, { errorMessage: 'Failed to load the refund review queue' });
export const resolveRefundReview = (body, key) =>
  request('/admin/billing/refund-reviews/resolve', {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
    errorMessage: 'Failed to resolve the refund review',
  });

// --- Engines ---

export const getEngines = () =>
  request('/admin/billing/engines', { errorMessage: 'Failed to load the billing engines' });
export const listMismatches = (filters = {}) =>
  request(`/admin/billing/engines/mismatches${query(filters)}`, { errorMessage: 'Failed to load mismatches' });
export const resolveMismatch = (id, note) =>
  request(`/admin/billing/engines/mismatches/${id}/resolve`, {
    method: 'POST',
    body: { note },
    errorMessage: 'Failed to resolve the mismatch',
  });
export const runScan = (dryRun = false) =>
  request('/admin/billing/engines/scan', {
    method: 'POST',
    body: { dry_run: dryRun },
    errorMessage: 'The scan failed',
  });
export const switchEngine = (engine, to, confirmOverpayments = false) =>
  request(`/admin/billing/engines/${engine}/switch`, {
    method: 'POST',
    body: { to, confirm_overpayments_to_reseller_cache: confirmOverpayments },
    errorMessage: 'The engine switch failed',
  });

// --- Document series ---

export const listSeries = () =>
  request('/admin/billing/series', { errorMessage: 'Failed to load document series' });
export const createSeries = (body) =>
  request('/admin/billing/series', { method: 'POST', body, errorMessage: 'Failed to create the series' });
export const updateSeries = (id, body) =>
  request(`/admin/billing/series/${id}`, { method: 'PATCH', body, errorMessage: 'Failed to update the series' });
export const makeSeriesDefault = (id) =>
  request(`/admin/billing/series/${id}/make-default`, { method: 'POST', errorMessage: 'Failed to set the default series' });
export const deleteSeries = (id) =>
  request(`/admin/billing/series/${id}`, { method: 'DELETE', errorMessage: 'Failed to delete the series' });

// --- Reseller credit (the only admin credit endpoints that exist) ---

export const listResellerAccounts = (filters = {}) =>
  request(`/admin/resellers${query(filters)}`, { errorMessage: 'Failed to list resellers' });
export const getResellerLedger = (resellerId, filters = {}) =>
  request(`/admin/resellers/${resellerId}/ledger${query(filters)}`, { errorMessage: 'Failed to load the credit ledger' });
export const adjustResellerCredit = (resellerId, body) =>
  request(`/admin/resellers/${resellerId}/credit-adjustments`, {
    method: 'POST',
    body,
    errorMessage: 'Failed to adjust the credit',
  });
export const listResellerInvoices = (filters = {}) =>
  request(`/admin/resellers/invoices${query(filters)}`, { errorMessage: 'Failed to list reseller invoices' });
export const applyResellerCredit = (invoiceId, body) =>
  request(`/admin/resellers/invoices/${invoiceId}/apply-credit`, {
    method: 'POST',
    body,
    errorMessage: 'Failed to apply the credit',
  });

// --- Store cycles ---

export const listStoreProducts = () =>
  request('/admin/store/products', { errorMessage: 'Failed to list store products' });
export const getStoreProduct = (id) =>
  request(`/admin/store/products/${id}`, { errorMessage: 'Failed to load the product' });
export const createCycle = (planId, body) =>
  request(`/admin/store/plans/${planId}/cycles`, { method: 'POST', body, errorMessage: 'Failed to add the cycle' });
export const updateCycle = (cycleId, body) =>
  request(`/admin/store/cycles/${cycleId}`, { method: 'PUT', body, errorMessage: 'Failed to update the cycle' });
export const deleteCycle = (cycleId) =>
  request(`/admin/store/cycles/${cycleId}`, { method: 'DELETE', errorMessage: 'Failed to delete the cycle' });
export const getAdminSettings = () =>
  request('/admin/settings', { errorMessage: 'Failed to load settings' });
