// Admin API helpers for the billing tax screens (billing phase 2).
// Money is integer cents; rates and exchange rates travel as decimal strings.

import { describeDetail } from './billingAdminApi.js';

const API_BASE = '/api';
const TAX = '/admin/billing/tax';

async function request(path, { method = 'GET', body, errorMessage } = {}) {
  const headers = new Headers();
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const fallback = response.status >= 500 ? `${errorMessage || 'The request failed'} (server error ${response.status})` : errorMessage;
    const error = new Error(describeDetail(payload.detail, response.status, fallback));
    error.status = response.status;
    error.detail = payload.detail;
    error.code = payload.detail && typeof payload.detail === 'object' ? payload.detail.code : undefined;
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

// --- Classes ---

export const listTaxClasses = () => request(`${TAX}/classes`, { errorMessage: 'Failed to load tax classes' });
export const createTaxClass = (body) =>
  request(`${TAX}/classes`, { method: 'POST', body, errorMessage: 'Failed to create the tax class' });
export const updateTaxClass = (id, body) =>
  request(`${TAX}/classes/${id}`, { method: 'PUT', body, errorMessage: 'Failed to update the tax class' });
export const deleteTaxClass = (id) =>
  request(`${TAX}/classes/${id}`, { method: 'DELETE', errorMessage: 'Failed to delete the tax class' });

// --- Rules ---

export const listTaxRules = (filters = {}) =>
  request(`${TAX}/rules${query(filters)}`, { errorMessage: 'Failed to load tax rules' });
export const getTaxRule = (id) => request(`${TAX}/rules/${id}`, { errorMessage: 'Failed to load the tax rule' });
export const createTaxRule = (body) =>
  request(`${TAX}/rules`, { method: 'POST', body, errorMessage: 'Failed to create the tax rule' });
export const updateTaxRule = (id, body) =>
  request(`${TAX}/rules/${id}`, { method: 'PUT', body, errorMessage: 'Failed to update the tax rule' });
export const supersedeTaxRule = (id, body) =>
  request(`${TAX}/rules/${id}/supersede`, { method: 'POST', body, errorMessage: 'Failed to supersede the tax rule' });
export const getTaxRuleUsage = (id) =>
  request(`${TAX}/rules/${id}/usage`, { errorMessage: 'Failed to load the rule usage' });

// --- Registrations ---

export const listTaxRegistrations = () =>
  request(`${TAX}/registrations`, { errorMessage: 'Failed to load tax registrations' });
export const createTaxRegistration = (body) =>
  request(`${TAX}/registrations`, { method: 'POST', body, errorMessage: 'Failed to create the registration' });
export const updateTaxRegistration = (id, body) =>
  request(`${TAX}/registrations/${id}`, { method: 'PUT', body, errorMessage: 'Failed to update the registration' });

// --- Exemptions and the account flag ---

export const listTaxExemptions = (filters = {}) =>
  request(`${TAX}/exemptions${query(filters)}`, { errorMessage: 'Failed to load tax exemptions' });
export const createTaxExemption = (body) =>
  request(`${TAX}/exemptions`, { method: 'POST', body, errorMessage: 'Failed to create the exemption' });
export const revokeTaxExemption = (id) =>
  request(`${TAX}/exemptions/${id}/revoke`, { method: 'POST', errorMessage: 'Failed to revoke the exemption' });
export const updateAccountTax = (accountId, body) =>
  request(`/admin/billing/accounts/${accountId}/tax`, {
    method: 'PUT',
    body,
    errorMessage: 'Failed to update the account tax settings',
  });
export const getCommerceBillingAccount = (id) =>
  request(`/admin/commerce/billing-accounts/${id}`, { errorMessage: 'Failed to load the billing account' });
export const searchCommerceBillingAccounts = (filters = {}) =>
  request(`/admin/commerce/billing-accounts${query(filters)}`, { errorMessage: 'Failed to list billing accounts' });

// --- Packs ---

export const listTaxPacks = () => request(`${TAX}/packs`, { errorMessage: 'Failed to load tax rule packs' });
export const previewTaxPack = (packId, body) =>
  request(`${TAX}/packs/${encodeURIComponent(packId)}/preview`, {
    method: 'POST',
    body,
    errorMessage: 'Failed to preview the pack',
  });
export const applyTaxPack = (packId, body) =>
  request(`${TAX}/packs/${encodeURIComponent(packId)}/apply`, {
    method: 'POST',
    body,
    errorMessage: 'Failed to apply the pack',
  });

// --- Legal notes, tax ID checks, calculator ---

export const listTaxLegalNotes = () =>
  request(`${TAX}/legal-notes`, { errorMessage: 'Failed to load legal notes' });
export const saveTaxLegalNote = (body) =>
  request(`${TAX}/legal-notes`, { method: 'PUT', body, errorMessage: 'Failed to save the legal note' });
export const listTaxIdChecks = (filters = {}) =>
  request(`${TAX}/id-checks${query(filters)}`, { errorMessage: 'Failed to load tax ID checks' });
export const recheckTaxId = (billingAccountId) =>
  request(`${TAX}/id-checks/recheck`, {
    method: 'POST',
    body: { billing_account_id: billingAccountId },
    errorMessage: 'Failed to recheck the tax ID',
  });
export const previewTax = (body) =>
  request(`${TAX}/preview`, { method: 'POST', body, errorMessage: 'The tax calculation failed' });

// --- Exchange rates (sterling VAT) ---

export const listExchangeRates = (filters = {}) =>
  request(`${TAX}/exchange-rates${query(filters)}`, { errorMessage: 'Failed to load exchange rates' });
export const createExchangeRate = (body) =>
  request(`${TAX}/exchange-rates`, { method: 'POST', body, errorMessage: 'Failed to store the exchange rate' });
export const deleteExchangeRate = (id) =>
  request(`${TAX}/exchange-rates/${id}`, { method: 'DELETE', errorMessage: 'Failed to delete the exchange rate' });

// --- Product tax class ---

export const setProductTaxClass = (storeProductId, taxClassId) =>
  request(`/admin/store/products/${storeProductId}/tax-class`, {
    method: 'PUT',
    body: { tax_class_id: taxClassId },
    errorMessage: 'Failed to set the product tax class',
  });

// --- Settings (read only here) ---

export const getTaxSettings = async () => {
  const data = await request('/admin/settings', { errorMessage: 'Failed to load settings' });
  return Object.fromEntries((data.items || []).filter((item) => item.key.startsWith('billing_tax')).map((item) => [item.key, item.value]));
};
