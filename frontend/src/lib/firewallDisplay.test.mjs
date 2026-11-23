import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyAllowed, notOffered, switchDisplay } from './firewallDisplay.js';

const notEligible = { state: 'not_eligible', available: false, in_sync: null, client_enabled: true };
const unavailableOther = { state: 'location_disabled', available: false, client_enabled: true };
const enabled = { state: 'active', available: true, client_enabled: true };
const disabled = { state: 'off', available: true, client_enabled: false };

test('a not_eligible VM is not offered and its switch reads as off', () => {
  assert.equal(notOffered(notEligible), true);
  assert.deepEqual(switchDisplay(notEligible), { checked: false, label: 'Not offered' });
  assert.equal(applyAllowed(notEligible), false);
});

test('not_eligible alone is enough even if available is missing', () => {
  const view = { state: 'not_eligible', client_enabled: true };
  assert.equal(notOffered(view), true);
  assert.deepEqual(switchDisplay(view), { checked: false, label: 'Not offered' });
  assert.equal(applyAllowed(view), false);
});

test('available false with another state is not offered', () => {
  assert.equal(notOffered(unavailableOther), true);
  assert.deepEqual(switchDisplay(unavailableOther), { checked: false, label: 'Not offered' });
  assert.equal(applyAllowed(unavailableOther), false);
});

test('a normal enabled VM shows the switch on and allows Apply now', () => {
  assert.equal(notOffered(enabled), false);
  assert.deepEqual(switchDisplay(enabled), { checked: true, label: 'On' });
  assert.equal(applyAllowed(enabled), true);
});

test('a normal disabled VM shows the switch off and allows Apply now', () => {
  assert.equal(notOffered(disabled), false);
  assert.deepEqual(switchDisplay(disabled), { checked: false, label: 'Off' });
  assert.equal(applyAllowed(disabled), true);
});

test('the stored enabled flag is not modified', () => {
  const view = { ...notEligible };
  switchDisplay(view);
  assert.equal(view.client_enabled, true);
});
