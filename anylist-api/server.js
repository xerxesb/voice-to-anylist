'use strict';

/**
 * A small REST facade over the unofficial `anylist` client.
 *
 * The bridge is Python -- gkeepapi is the only mature Google Keep client and
 * Keep's sync protocol is not worth reimplementing -- while the best-maintained
 * AnyList client is this Node package.  Rather than pick a weaker library on
 * either side, the two talk over loopback inside one container.
 *
 * This never binds a public interface.  It exposes exactly what the merge
 * engine needs: stable item ids, a separate quantity field, and a checked flag.
 */

const os = require('os');
const path = require('path');

const express = require('express');
const AnyList = require('anylist');

const { FetchPolicy, DownloadDeferred, divertPushes, statusOf } = require('./fetch-policy');

/**
 * Where cached credentials live, matching `config.default_state_dir()` on the
 * Python side.  The launchd job sets ANYLIST_CREDENTIALS_FILE explicitly; this
 * is the default that keeps a bare `node server.js` sane during development.
 */
function defaultStateDir() {
  if (process.env.VTA_STATE_DIR) return process.env.VTA_STATE_DIR;
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'voice-to-anylist');
  }
  if (process.env.XDG_STATE_HOME) {
    return path.join(process.env.XDG_STATE_HOME, 'voice-to-anylist');
  }
  return path.join(os.homedir(), '.local', 'state', 'voice-to-anylist');
}

const PORT = Number(process.env.ANYLIST_API_PORT || 3000);
const HOST = process.env.ANYLIST_API_HOST || '127.0.0.1';
const TOKEN = process.env.ANYLIST_API_TOKEN || '';
const EMAIL = process.env.ANYLIST_EMAIL;
const PASSWORD = process.env.ANYLIST_PASSWORD;
const CREDENTIALS_FILE =
  process.env.ANYLIST_CREDENTIALS_FILE || path.join(defaultStateDir(), '.anylist_credentials');

// Missing credentials are a configuration problem, not a crash.  Exiting here
// earns a launchd restart loop throttled to a ten-minute retry, with this
// message scrolled out of the log; staying up serves the reason on /health and
// leaves the process there for `vta doctor` to talk to.
const MISSING = [
  ['ANYLIST_EMAIL', EMAIL],
  ['ANYLIST_PASSWORD', PASSWORD],
]
  .filter(([, value]) => !value)
  .map(([name]) => name);

if (MISSING.length) {
  console.error(`not configured: ${MISSING.join(' and ')} must be set. Serving 503 until then.`);
}

/** An optional numeric setting in seconds, as milliseconds; unset or junk is the default. */
function secondsSetting(name) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    console.error(`ignoring ${name}=${raw}: not a number of seconds`);
    return undefined;
  }
  return value * 1000;
}

function countSetting(name) {
  const ms = secondsSetting(name);
  return ms === undefined ? undefined : Math.floor(ms / 1000);
}

// Every download is the whole account, so how often they happen is the one
// thing that got this integration's account suspended.  See fetch-policy.js.
const policy = new FetchPolicy({
  ledgerFile:
    process.env.ANYLIST_DOWNLOAD_LEDGER_FILE ||
    path.join(path.dirname(CREDENTIALS_FILE), 'anylist-downloads.json'),
  minIntervalMs: secondsSetting('ANYLIST_MIN_REFRESH_SECONDS'),
  maxAgeMs: secondsSetting('ANYLIST_MAX_AGE_SECONDS'),
  disconnectedMaxAgeMs: secondsSetting('ANYLIST_DISCONNECTED_MAX_AGE_SECONDS'),
  hourlyBudget: countSetting('ANYLIST_HOURLY_DOWNLOAD_BUDGET'),
});

let any = null;
let loginPromise = null;
let downloadPromise = null;

/**
 * Log in once and reuse the session.
 *
 * Concurrent callers share a single in-flight login so a burst of requests
 * after a restart does not fan out into several authentication attempts.
 */
function connect() {
  if (loginPromise) return loginPromise;
  loginPromise = (async () => {
    const client = new AnyList({
      email: EMAIL,
      password: PASSWORD,
      credentialsFile: CREDENTIALS_FILE,
    });
    divertPushes(client, policy);
    await client.login();
    policy.forget(); // a new client has nothing in memory yet
    any = client;
    return client;
  })().catch((error) => {
    loginPromise = null; // allow a later request to retry
    throw error;
  });
  return loginPromise;
}

function websocketOpen(client) {
  return Boolean(client && client.ws && client.ws.readyState === 1);
}

/**
 * The package lets its websocket give up after two reconnect attempts, which
 * would leave the sidecar on its slower disconnected backstop for good.  Nudge
 * it back each time that backstop fires.
 */
function reconnectWebsocket(client) {
  try {
    if (client.ws && typeof client.ws.reconnect === 'function') {
      console.info('AnyList websocket is down; reconnecting');
      client.ws.reconnect();
    }
  } catch (error) {
    console.error(`websocket reconnect failed: ${error.message || error}`);
  }
}

/**
 * The logged-in client, with the account in memory and as fresh as the
 * download policy allows.
 *
 * Concurrent callers share one in-flight download, so a burst of requests
 * costs at most one.
 */
async function freshClient() {
  const client = await connect();
  if (downloadPromise) {
    await downloadPromise;
    return client;
  }
  const { download, reason } = policy.decide({ connected: websocketOpen(client) });
  if (!download) return client;

  downloadPromise = (async () => {
    policy.started();
    try {
      await client.download();
    } catch (error) {
      policy.failed(error);
      const deferred = new DownloadDeferred(
        `AnyList download failed: ${error.message || error}`,
        policy.cooldownRemainingMs(),
      );
      deferred.cause = error;
      throw deferred;
    }
    policy.succeeded(reason);
    if (reason === 'websocket-down') reconnectWebsocket(client);
    const { downloads_last_hour: used, hourly_budget: budget } = policy.stats();
    console.log(`downloaded the AnyList account (${reason}; ${used}/${budget} this hour)`);
  })().finally(() => {
    downloadPromise = null;
  });
  await downloadPromise;
  return client;
}

/** Drop the session so the next call authenticates from scratch. */
function reset() {
  try {
    if (any) any.teardown();
  } catch {
    // teardown is best-effort; the session is being discarded either way
  }
  any = null;
  loginPromise = null;
}

/**
 * Resolve a list by name from the copy in memory.
 *
 * The bridge polls every few seconds, but the copy is only downloaded again
 * when AnyList says something changed, or the policy's backstops say so.
 * Writes update the copy in place, so reading back what was just written
 * needs no download.
 */
async function getList(name) {
  const client = await freshClient();
  const wanted = String(name || '').trim();
  // getListByName matches exactly; fall back to a case-insensitive search so a
  // config saying "grocery" still finds the list called "Grocery".
  const list =
    client.getListByName(wanted) ||
    (client.lists || []).find((l) => (l.name || '').toLowerCase() === wanted.toLowerCase());
  if (!list) {
    const available = (client.lists || []).map((l) => l.name).join(', ');
    const error = new Error(`No AnyList list named "${wanted}". Available: ${available || 'none'}`);
    error.status = 404;
    throw error;
  }
  return { client, list };
}

function serialise(item) {
  return {
    id: item.identifier,
    name: item.name,
    // The client reports a missing quantity inconsistently; normalise to null
    // so the bridge does not read "" and "0" as different from absent.
    quantity: item.quantity ? String(item.quantity) : null,
    checked: Boolean(item.checked),
  };
}

function findItem(list, id) {
  const item = list.items.find((i) => i.identifier === id);
  if (!item) {
    const error = new Error(`No item with id ${id}`);
    error.status = 404;
    throw error;
  }
  return item;
}

const app = express();
app.use(express.json());

// Before authentication: an unconfigured sidecar has no token to check
// against, and the reason is not a secret.
app.use((req, res, next) => {
  if (!MISSING.length) return next();
  return res.status(503).json({
    ok: false,
    unconfigured: MISSING,
    error: `not configured: ${MISSING.join(', ')} must be set`,
  });
});

app.use((req, res, next) => {
  if (TOKEN && req.headers.authorization !== `Bearer ${TOKEN}`) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  next();
});

/** Liveness plus a cheap check that the AnyList credentials still work. */
app.get('/health', async (req, res) => {
  try {
    await connect();
    res.json({ ok: true });
  } catch (error) {
    res.status(503).json({ ok: false, error: String(error.message || error) });
  }
});

/** How often the account is being downloaded, and why.  For `vta`. */
app.get('/stats', (req, res) => {
  res.json({
    ...policy.stats(),
    logged_in: Boolean(any),
    websocket_open: websocketOpen(any),
  });
});

app.get('/lists', async (req, res, next) => {
  try {
    const client = await freshClient();
    res.json({ lists: (client.lists || []).map((l) => l.name) });
  } catch (error) {
    next(error);
  }
});

app.get('/items', async (req, res, next) => {
  try {
    const { list } = await getList(req.query.list);
    res.json({ items: list.items.map(serialise) });
  } catch (error) {
    next(error);
  }
});

app.post('/items', async (req, res, next) => {
  try {
    const { list: listName, name, quantity, checked } = req.body;
    if (!name) return res.status(400).json({ error: 'name is required' });
    const { client, list } = await getList(listName);

    // The official clients revive a checked-off item rather than adding a
    // second one.  The merge engine normally handles this, but a race between
    // cycles could still land here.
    const existing = list.items.find(
      (i) => (i.name || '').trim().toLowerCase() === name.trim().toLowerCase(),
    );
    if (existing) {
      existing.checked = Boolean(checked);
      existing.quantity = quantity || '';
      await existing.save();
      return res.status(200).json(serialise(existing));
    }

    let item = client.createItem({ name, quantity: quantity || '' });
    item = await list.addItem(item);
    if (checked) {
      item.checked = true;
      await item.save();
    }
    res.status(201).json(serialise(item));
  } catch (error) {
    next(error);
  }
});

app.patch('/items/:id', async (req, res, next) => {
  try {
    const { list } = await getList(req.body.list);
    const item = findItem(list, req.params.id);
    if ('quantity' in req.body) item.quantity = req.body.quantity || '';
    if ('checked' in req.body) item.checked = Boolean(req.body.checked);
    await item.save();
    res.json(serialise(item));
  } catch (error) {
    next(error);
  }
});

app.delete('/items/:id', async (req, res, next) => {
  try {
    const { list } = await getList(req.body.list);
    const item = findItem(list, req.params.id);
    await list.removeItem(item);
    res.status(204).end();
  } catch (error) {
    next(error);
  }
});

app.use((error, req, res, next) => {  // eslint-disable-line no-unused-vars
  const upstream = error.cause || error;
  const message = String(error.message || error);

  if (error instanceof DownloadDeferred) {
    const seconds = Math.max(1, Math.ceil(error.retryAfterMs / 1000));
    res.set('Retry-After', String(seconds));
    // The failed download's own reason may be an expired session.
    if (upstream !== error && /auth|login|credential|401|403/i.test(String(upstream.message))) {
      reset();
    }
    console.error(`${req.method} ${req.path} -> 503: ${message} (retry in ${seconds}s)`);
    return res.status(503).json({ error: message, retry_after_seconds: seconds });
  }

  if (upstream.response) {
    // A write AnyList refused leaves the copy unsure of what is really
    // there.  And a 429 means the account is throttled: stop altogether.
    policy.invalidate('write-failed');
    if (statusOf(upstream) === 429) {
      policy.failed(upstream);
      const seconds = Math.max(1, Math.ceil(policy.cooldownRemainingMs() / 1000));
      res.set('Retry-After', String(seconds));
      console.error(`${req.method} ${req.path} -> 503: AnyList throttled a write (retry in ${seconds}s)`);
      return res
        .status(503)
        .json({ error: `AnyList throttled a write: ${message}`, retry_after_seconds: seconds });
    }
  }

  const status = error.status || 500;
  // An auth failure poisons the cached session, so force a fresh login next
  // time rather than serving 500s until the process is restarted.
  if (status >= 500 && /auth|login|credential|401|403/i.test(message)) reset();
  console.error(`${req.method} ${req.path} -> ${status}: ${message}`);
  res.status(status).json({ error: message });
});

module.exports = { app, policy };

const server = app.listen(PORT, HOST, () => {
  console.log(`anylist-api listening on ${HOST}:${PORT}`);
});

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    server.close(() => {
      reset();
      process.exit(0);
    });
  });
}
