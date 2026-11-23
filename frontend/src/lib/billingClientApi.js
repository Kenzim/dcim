import { downloadPdfBlob } from './api.js';
import { formatMoney } from './resellerMoney.js';

// Client and reseller billing calls: retail credit, apply credit, credit and
// debit notes, and cancellation requests. Money is integer cents on every endpoint.

const API_BASE = '/api';
// Same sessionStorage key api.js uses to keep an admin "sign in as" token for this tab.
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

/** A fresh key for one user action; send the same key again only to retry that action. */
export function newIdempotencyKey() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `ui-${crypto.randomUUID()}`;
  }
  const random = Array.from({ length: 4 }, () => Math.random().toString(36).slice(2, 10)).join('');
  return `ui-${Date.now().toString(36)}-${random}`;
}

function humanise(code) {
  return String(code || '').replaceAll('_', ' ');
}

/**
 * A readable sentence for a failed billing call. Works on errors thrown by the
 * helpers here and on the structured errors api.js throws (`status` and `detail`).
 * `currency` formats the amounts named in a refusal.
 */
export function billingErrorMessage(err, fallback = 'The request failed.', { currency = 'USD' } = {}) {
  if (!err) return fallback;
  const detail = err.detail;
  const status = err.status;
  if (status === undefined && detail === undefined) {
    return err.message || fallback;
  }
  if (typeof detail === 'string') {
    if (status === 403 && /impersonat/i.test(detail)) {
      return 'Money actions are blocked while you are signed in on someone else\'s behalf.';
    }
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : '';
        return item?.msg ? (field && field !== 'body' ? `${field}: ${item.msg}` : item.msg) : '';
      })
      .filter(Boolean);
    return messages.length ? messages.join('. ') : fallback;
  }
  if (detail && typeof detail === 'object') {
    switch (detail.code) {
      case 'invoice_payment_pending':
        return 'A payment on this invoice is still pending. Wait for it to finish, then try again.';
      case 'insufficient_credit':
        return `Not enough credit: ${formatMoney(detail.available_cents, currency)} available, `
          + `${formatMoney(detail.requested_cents, currency)} requested.`;
      case 'credit_unavailable':
        return detail.message || 'Account credit is not available yet.';
      case 'credit_requires_fulfilled_order':
        return detail.message || 'Account credit is available after your first order is fulfilled.';
      case 'invalid_topup_amount':
        return `Enter an amount between ${formatMoney(detail.min_cents, currency)} and ${formatMoney(detail.max_cents, currency)}.`;
      case 'credit_balance_limit':
        return `This top-up would take your balance over the ${formatMoney(detail.max_balance_cents, currency)} limit.`;
      case 'payment_action_required':
        return 'The card needs extra authentication before this payment can complete.';
      case 'payment_failed':
        return 'The payment was declined. Try another payment method.';
      default:
        return detail.message || (detail.code ? humanise(detail.code) : fallback);
    }
  }
  return err.message || fallback;
}

async function request(base, path, { method = 'GET', body, headers: extra, errorMessage, impersonation = false } = {}) {
  const headers = new Headers(extra);
  if (body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  if (impersonation) {
    Object.entries(impersonationHeaders()).forEach(([key, value]) => headers.set(key, value));
  }
  const response = await fetch(`${API_BASE}${base}${path}`, {
    method,
    headers,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(errorMessage || 'Billing request failed');
    error.status = response.status;
    error.detail = payload.detail;
    error.message = billingErrorMessage(error, errorMessage || 'Billing request failed');
    throw error;
  }
  if (response.status === 204) return null;
  return await response.json();
}

async function download(base, path, filename, errorMessage, { impersonation = false } = {}) {
  const headers = new Headers();
  if (impersonation) {
    Object.entries(impersonationHeaders()).forEach(([key, value]) => headers.set(key, value));
  }
  const response = await fetch(`${API_BASE}${base}${path}`, { method: 'GET', headers, credentials: 'include' });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(errorMessage);
    error.status = response.status;
    error.detail = payload.detail;
    error.message = billingErrorMessage(error, errorMessage);
    throw error;
  }
  downloadPdfBlob(await response.blob(), filename);
}

function noteFilename(note) {
  const prefix = note.kind === 'debit' ? 'debit-note' : 'credit-note';
  return `${prefix}-${String(note.number_display || note.id).replace(/[^A-Za-z0-9._-]/g, '_')}.pdf`;
}

function applyCreditRequest(base, path, amountCents, idempotencyKey, options) {
  const body = amountCents ? { amount_cents: amountCents } : undefined;
  return request(base, path, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
    errorMessage: 'Credit could not be applied',
    ...options,
  });
}

// --- Client (retail) ---

const CLIENT = '/client/commerce';

/** Credit summary: `enabled`, `balance_cents`, `available_cents`, top-up limits and ledger `entries`. */
export const clientGetCredit = (limit = 25) =>
  request(CLIENT, `/credit${query({ limit })}`, { errorMessage: 'Failed to load account credit', impersonation: true });

/** Creates a credit top-up invoice and returns it; the client then pays it from the invoice view. */
export const clientCreateCreditTopUp = (amountCents) =>
  request(CLIENT, '/credit/top-up-invoices', {
    method: 'POST',
    body: { amount_cents: amountCents },
    errorMessage: 'Top-up invoice could not be created',
    impersonation: true,
  });

/** Applies credit to an invoice; omit `amountCents` for as much as the balance and invoice allow. */
export const clientApplyCredit = (invoiceId, amountCents, idempotencyKey) =>
  applyCreditRequest(CLIENT, `/invoices/${invoiceId}/apply-credit`, amountCents, idempotencyKey, { impersonation: true });

export const clientListAdjustments = (filters = {}) =>
  request(CLIENT, `/adjustments${query(filters)}`, { errorMessage: 'Failed to load credit and debit notes', impersonation: true });

export const clientGetAdjustment = (id) =>
  request(CLIENT, `/adjustments/${id}`, { errorMessage: 'Failed to load the note', impersonation: true });

export const clientDownloadAdjustmentPdf = (note) =>
  download(CLIENT, `/adjustments/${note.id}/pdf`, noteFilename(note), 'Failed to download the note', { impersonation: true });

/** The client's cancellation requests, newest first. Filters: `status`, `limit`, `offset`. */
export const clientListCancellationRequests = (filters = {}) =>
  request(CLIENT, `/cancellation-requests${query(filters)}`, {
    errorMessage: 'Failed to load cancellation requests',
    impersonation: true,
  });

/** Withdraws a scheduled request; answers the updated request. */
export const clientWithdrawCancellationRequest = (id) =>
  request(CLIENT, `/cancellation-requests/${id}/withdraw`, {
    method: 'POST',
    errorMessage: 'The cancellation could not be withdrawn',
    impersonation: true,
  });

/**
 * Starts a cancellation. `when` is `end_of_cycle` or `immediate`. The answer carries
 * `request` (the cancellation request), `request_id`, `cancel_at` and `termination_pending`.
 */
export const clientCancelService = (serviceId, { when = 'end_of_cycle', reason } = {}) =>
  request(CLIENT, `/services/${serviceId}/cancel`, {
    method: 'POST',
    body: { when, reason: reason || null },
    errorMessage: 'The cancellation could not be requested',
    impersonation: true,
  });

// --- Reseller panel ---

const RESELLER = '/reseller-panel';

/** `{ balance_cents, currency }` of the reseller's credit. */
export const resellerGetBalance = () =>
  request(RESELLER, '/balance', { errorMessage: 'Failed to load your credit balance' });

export const resellerApplyCredit = (invoiceId, amountCents, idempotencyKey) =>
  applyCreditRequest(RESELLER, `/invoices/${invoiceId}/apply-credit`, amountCents, idempotencyKey);

export const resellerListAdjustments = (filters = {}) =>
  request(RESELLER, `/adjustments${query(filters)}`, { errorMessage: 'Failed to load credit and debit notes' });

export const resellerGetAdjustment = (id) =>
  request(RESELLER, `/adjustments/${id}`, { errorMessage: 'Failed to load the note' });

export const resellerDownloadAdjustmentPdf = (note) =>
  download(RESELLER, `/adjustments/${note.id}/pdf`, noteFilename(note), 'Failed to download the note');

export const resellerListCancellationRequests = (filters = {}) =>
  request(RESELLER, `/cancellation-requests${query(filters)}`, { errorMessage: 'Failed to load cancellation requests' });

/**
 * Requests a cancellation of one of the reseller's services. `type` is `end_of_period` or
 * `immediate`; `invoiceAction` is `cancel_unpaid` or `keep`. Send the same key again only to
 * retry the same request.
 */
export const resellerRequestCancellation = ({ serviceId, type, reason, invoiceAction }, idempotencyKey) =>
  request(RESELLER, '/cancellation-requests', {
    method: 'POST',
    body: {
      service_id: serviceId,
      type,
      reason: reason || null,
      invoice_action: invoiceAction,
    },
    headers: { 'Idempotency-Key': idempotencyKey },
    errorMessage: 'The cancellation could not be requested',
  });

export const resellerWithdrawCancellationRequest = (id) =>
  request(RESELLER, `/cancellation-requests/${id}/withdraw`, {
    method: 'POST',
    errorMessage: 'The cancellation could not be withdrawn',
  });
