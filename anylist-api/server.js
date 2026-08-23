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

let any = null;
let loginPromise = null;

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
    await client.login();
    any = client;
    return client;
  })().catch((error) => {
    loginPromise = null; // allow a later request to retry
    throw error;
  });
  return loginPromise;
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
 * Resolve a list by name, refreshing from the server first.
 *
 * The bridge polls, so every request wants current state rather than whatever
 * the client cached when it started.
 */
async function getList(name) {
  const client = await connect();
  await client.getLists();
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

app.get('/lists', async (req, res, next) => {
  try {
    const client = await connect();
    await client.getLists();
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
  const status = error.status || 500;
  const message = String(error.message || error);
  // An auth failure poisons the cached session, so force a fresh login next
  // time rather than serving 500s until the container is restarted.
  if (status >= 500 && /auth|login|credential|401|403/i.test(message)) reset();
  console.error(`${req.method} ${req.path} -> ${status}: ${message}`);
  res.status(status).json({ error: message });
});

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
