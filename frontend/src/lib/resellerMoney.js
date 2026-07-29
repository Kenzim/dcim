export function parseUsdToCents(value) {
  const text = String(value ?? '').trim();
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(text);
  if (!match) {
    throw new Error('Enter a USD amount with no more than two decimal places.');
  }
  const cents = (BigInt(match[1]) * 100n) + BigInt((match[2] || '').padEnd(2, '0') || '0');
  if (cents <= 0n || cents > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error('Enter a valid amount greater than zero.');
  }
  return Number(cents);
}

export function formatMoney(cents, currency = 'USD') {
  const value = Number.isSafeInteger(Number(cents)) ? Number(cents) : 0;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
  }).format(value / 100);
}
