'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const { FetchPolicy, DownloadDeferred, parseRetryAfter } = require('../fetch-policy');

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

function clock(start = Date.UTC(2026, 10, 16, 9)) {
  const c = { t: start, now: () => c.t, advance: (ms) => { c.t += ms; } };
  return c;
}

/** Run one request the way the server does, returning whether it downloaded. */
function request(policy, { connected = true, fail = null } = {}) {
  const { download, reason } = policy.decide({ connected });
  if (!download) return false;
  policy.started();
  if (fail) policy.failed(fail);
  else policy.succeeded(reason);
  return true;
}

function countDownloads(policy, c, { every, for: duration, connected = true }) {
  let n = 0;
  for (let elapsed = 0; elapsed < duration; elapsed += every) {
    if (request(policy, { connected })) n += 1;
    c.advance(every);
  }
  return n;
}

test('the first request downloads, then the copy is reused', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });

  assert.equal(request(policy), true);
  c.advance(20 * SECOND);
  assert.equal(request(policy), false);
});

test('a bridge polling every 20s with nothing changing costs 4 downloads an hour', () => {
  // The old sidecar downloaded on every request: 180 an hour.
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });

  assert.equal(countDownloads(policy, c, { every: 20 * SECOND, for: HOUR }), 4);
});

test('a push is honoured on the next request once the floor has passed', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  request(policy);

  c.advance(2 * MINUTE);
  policy.invalidate('push');
  assert.equal(request(policy), true);
  assert.equal(policy.stats().last_refresh_reason, 'push');
  assert.equal(request(policy), false, 'a push is consumed by one download');
});

test('a push straight after a download waits out the floor, serving the copy meanwhile', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  request(policy);

  c.advance(10 * SECOND);
  policy.invalidate('push');
  assert.equal(request(policy), false);
  assert.equal(policy.stats().stale, true, 'still owed a refresh');

  c.advance(MINUTE);
  assert.equal(request(policy), true);
});

test('a push that lands mid-download is still owed afterwards', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  const { reason } = policy.decide({ connected: true });
  policy.started();
  policy.invalidate('push'); // arrives while the download is on the wire
  policy.succeeded(reason);

  assert.equal(policy.stats().stale, true);
  c.advance(MINUTE);
  assert.equal(request(policy), true);
  assert.equal(policy.stats().stale, false);
});

test('a storm of pushes cannot exceed one download a minute', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now, hourlyBudget: 1000 });
  let n = 0;
  for (let i = 0; i < 360; i += 1) {
    policy.invalidate('push');
    if (request(policy)) n += 1;
    c.advance(10 * SECOND);
  }
  assert.equal(n, 60);
});

test('the hourly budget caps downloads whatever the floor allows', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now, minIntervalMs: 0, hourlyBudget: 40 });
  let n = 0;
  for (let i = 0; i < 360; i += 1) {
    policy.invalidate('push');
    if (request(policy)) n += 1;
    c.advance(10 * SECOND);
  }
  assert.equal(n, 40);
});

test('with the websocket down it polls every five minutes instead', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });

  assert.equal(countDownloads(policy, c, { every: 20 * SECOND, for: HOUR, connected: false }), 12);
});

test('with no copy and the floor not yet passed it refuses with a retry time', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  request(policy);
  policy.forget();

  c.advance(15 * SECOND);
  assert.throws(
    () => policy.decide({ connected: true }),
    (error) => error instanceof DownloadDeferred && error.retryAfterMs === 45 * SECOND,
  );
});

test('a failure stops everything, even a copy in memory, and the pause doubles', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  request(policy);
  c.advance(2 * MINUTE);
  policy.invalidate('push');

  request(policy, { fail: new Error('boom') });
  assert.throws(() => policy.decide({ connected: true }), DownloadDeferred);

  c.advance(MINUTE);
  request(policy, { fail: new Error('boom') });
  c.advance(MINUTE);
  assert.throws(
    () => policy.decide({ connected: true }),
    (error) => error.retryAfterMs === MINUTE,
    'second failure pauses for two minutes',
  );
});

test('the pause tops out at an hour', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  for (let i = 0; i < 12; i += 1) policy.failed(new Error('boom'));

  assert.equal(new Date(policy.stats().cooldown_until).getTime() - c.t, HOUR);
});

test('a longer Retry-After from AnyList wins over the backoff', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  const error = Object.assign(new Error('Too Many Requests'), {
    response: { statusCode: 429, headers: { 'retry-after': '3600' } },
  });

  policy.failed(error);

  assert.equal(new Date(policy.stats().cooldown_until).getTime() - c.t, HOUR);
  assert.match(policy.stats().last_error, /429/);
});

test('success clears the failure count', () => {
  const c = clock();
  const policy = new FetchPolicy({ now: c.now });
  request(policy, { fail: new Error('boom') });
  c.advance(MINUTE);

  assert.equal(request(policy), true);
  assert.equal(policy.stats().consecutive_failures, 0);
  assert.equal(policy.stats().cooldown_until, null);
});

test('the ledger survives a restart, so a crash loop cannot reset the limits', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vta-policy-'));
  const ledgerFile = path.join(dir, 'ledger.json');
  const c = clock();

  // A process that crashes ten seconds after each start, restarted by launchd.
  let n = 0;
  for (let i = 0; i < 360; i += 1) {
    const policy = new FetchPolicy({ now: c.now, ledgerFile });
    try {
      if (request(policy)) n += 1;
    } catch (error) {
      assert.ok(error instanceof DownloadDeferred);
    }
    c.advance(10 * SECOND);
  }
  assert.equal(n, 40, 'held to the hourly budget across 360 restarts');
});

test('a cooldown survives a restart', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vta-policy-'));
  const ledgerFile = path.join(dir, 'ledger.json');
  const c = clock();
  new FetchPolicy({ now: c.now, ledgerFile }).failed(new Error('boom'));

  const restarted = new FetchPolicy({ now: c.now, ledgerFile });
  assert.throws(() => restarted.decide({ connected: true }), DownloadDeferred);
});

test('a corrupt ledger is ignored rather than fatal', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vta-policy-'));
  const ledgerFile = path.join(dir, 'ledger.json');
  fs.writeFileSync(ledgerFile, '{not json');

  const policy = new FetchPolicy({ now: clock().now, ledgerFile });
  assert.equal(request(policy), true);
});

test('timestamps from the future are discarded, so a clock change cannot spend the budget', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vta-policy-'));
  const ledgerFile = path.join(dir, 'ledger.json');
  const c = clock();
  fs.writeFileSync(ledgerFile, JSON.stringify({ attempts: Array(50).fill(c.t + HOUR) }));

  assert.equal(request(new FetchPolicy({ now: c.now, ledgerFile })), true);
});

test('Retry-After is read as seconds or as a date', () => {
  const now = Date.UTC(2026, 0, 1);
  assert.equal(parseRetryAfter('120', now), 120 * SECOND);
  assert.equal(parseRetryAfter(new Date(now + MINUTE).toUTCString(), now), MINUTE);
  assert.equal(parseRetryAfter(undefined, now), null);
  assert.equal(parseRetryAfter('soon', now), null);
});
