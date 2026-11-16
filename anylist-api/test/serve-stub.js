'use strict';

/**
 * Boot the real server with the `anylist` package swapped for the stub.
 *
 * Adds `/__stub/*` routes so a test in another process can play the AnyList
 * server: count downloads, push a change, fail a request, drop the websocket.
 */
const fs = require('fs');
const Module = require('module');
const os = require('os');
const path = require('path');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === 'anylist') return path.join(__dirname, 'stub-anylist.js');
  return resolve.call(this, request, ...rest);
};

// Only default when unset, so a test can pass an empty string to exercise
// the unconfigured path rather than being silently given credentials.
if (process.env.ANYLIST_EMAIL === undefined) process.env.ANYLIST_EMAIL = 'test@example.com';
if (process.env.ANYLIST_PASSWORD === undefined) process.env.ANYLIST_PASSWORD = 'test';
process.env.ANYLIST_CREDENTIALS_FILE = '/tmp/stub-credentials';
// A ledger shared between test runs would carry one run's downloads into the
// next one's budget.
if (process.env.ANYLIST_DOWNLOAD_LEDGER_FILE === undefined) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vta-stub-'));
  process.env.ANYLIST_DOWNLOAD_LEDGER_FILE = path.join(dir, 'anylist-downloads.json');
}

const AnyList = require('anylist');
const { FetchPolicy } = require('../fetch-policy');
const { app, policy } = require('../server.js');

const { stub } = AnyList;

app.get('/__stub/state', (req, res) => {
  res.json({ downloads: stub.server.downloads, reconnects: stub.server.reconnects });
});

app.post('/__stub/reset', (req, res) => {
  stub.reset();
  const fresh = new FetchPolicy({ ...policy.limits, now: policy.now });
  Object.assign(policy, fresh, { ledgerFile: policy.ledgerFile });
  res.status(204).end();
});

app.post('/__stub/push', (req, res) => {
  stub.push();
  res.status(204).end();
});

app.post('/__stub/external-edit', (req, res) => {
  stub.externalEdit(req.body);
  res.status(204).end();
});

app.post('/__stub/fail-download', (req, res) => {
  const { statusCode = 500, retryAfter, count = 1 } = req.body || {};
  for (let i = 0; i < count; i += 1) stub.server.failures.push({ statusCode, retryAfter });
  res.status(204).end();
});

app.post('/__stub/fail-write', (req, res) => {
  const { statusCode = 500, retryAfter } = req.body || {};
  stub.server.writeFailures.push({ statusCode, retryAfter });
  res.status(204).end();
});

app.post('/__stub/websocket', (req, res) => {
  stub.server.websocketOpen = Boolean(req.body.open);
  res.status(204).end();
});
