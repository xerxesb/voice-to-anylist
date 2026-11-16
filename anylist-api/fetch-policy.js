'use strict';

/**
 * When the sidecar is allowed to download the AnyList account.
 *
 * The `anylist` package has no way to read one list.  `getLists()` posts to
 * `data/user-data/get`, which returns everything on the account: every list,
 * recent and favourite items, recipes and the meal plan.  Calling that on
 * every request -- once per bridge poll, plus once per write -- added up to
 * roughly 180 full downloads an hour, and AnyList suspended syncing for the
 * whole account over it.
 *
 * So the sidecar keeps the decoded account in memory and only downloads again
 * when there is a reason to:
 *
 *  - AnyList's websocket said something changed (`invalidate`),
 *  - the websocket is down, so changes could be missed (`disconnectedMaxAgeMs`),
 *  - the copy is simply old (`maxAgeMs`), as a backstop against a missed push.
 *
 * Whatever the reason, three hard limits apply, and they persist across
 * restarts so a crash loop cannot reset them:
 *
 *  - never more often than `minIntervalMs`,
 *  - never more than `hourlyBudget` in any rolling hour,
 *  - never during a cooldown after a failed download, which doubles each time.
 *
 * This module does no I/O except the small ledger file, and takes its clock
 * as a parameter, so every rule can be tested without waiting.
 */

const fs = require('fs');
const path = require('path');

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;

const DEFAULTS = Object.freeze({
  minIntervalMs: MINUTE,
  maxAgeMs: 15 * MINUTE,
  disconnectedMaxAgeMs: 5 * MINUTE,
  hourlyBudget: 40,
  failureBackoffMs: MINUTE,
  maxBackoffMs: HOUR,
});

/** Why a request cannot be served right now, with when to come back. */
class DownloadDeferred extends Error {
  constructor(message, retryAfterMs) {
    super(message);
    this.name = 'DownloadDeferred';
    this.status = 503;
    this.retryAfterMs = Math.max(0, Math.ceil(retryAfterMs));
  }
}

/**
 * Read a Retry-After header as milliseconds, or null.
 *
 * It may be a number of seconds or an HTTP date.
 */
function parseRetryAfter(value, now) {
  if (value === undefined || value === null || value === '') return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const date = Date.parse(String(value));
  if (Number.isFinite(date)) return Math.max(0, date - now);
  return null;
}

class FetchPolicy {
  /**
   * @param {object} [options]
   * @param {() => number} [options.now] clock in epoch milliseconds
   * @param {string|null} [options.ledgerFile] where to persist the history
   */
  constructor(options = {}) {
    const { now = Date.now, ledgerFile = null, ...limits } = options;
    this.now = now;
    this.ledgerFile = ledgerFile;
    this.limits = { ...DEFAULTS };
    for (const [key, value] of Object.entries(limits)) {
      if (value !== undefined && value !== null && !Number.isNaN(value)) this.limits[key] = value;
    }

    /** Epoch ms of each download attempt in the last hour, oldest first. */
    this.attempts = [];
    this.lastSuccessAt = null;
    this.consecutiveFailures = 0;
    this.cooldownUntil = 0;
    this.lastError = null;

    // Per process: a fresh process has no copy, whatever the ledger says.
    this.haveCopy = false;
    this.stale = false;
    this.staleReason = null;
    this.lastRefreshReason = null;
    this.inFlight = false;
    this.invalidatedInFlight = false;

    this._load();
  }

  // -- what the server tells it ------------------------------------------

  /** Something changed upstream; refresh at the next opportunity. */
  invalidate(reason) {
    this.stale = true;
    this.staleReason = reason;
    // A push during a download may describe a change the download's snapshot
    // missed, so it must outlive that download's success.
    if (this.inFlight) this.invalidatedInFlight = true;
  }

  /** The process is about to lose its copy, e.g. a fresh login. */
  forget() {
    this.haveCopy = false;
  }

  /**
   * Decide whether a request may use the copy in memory, must download, or
   * has to be turned away.
   *
   * @param {{connected: boolean}} state whether the websocket is open
   * @return {{download: boolean, reason: string}}
   * @throws {DownloadDeferred} when there is no usable copy and no download
   *   is allowed yet
   */
  decide({ connected }) {
    const now = this.now();
    this._prune(now);

    if (this.cooldownUntil > now) {
      // Even a copy in memory is not served: the bridge would merge against
      // it and write, and writes are what a throttled account rejects.
      throw new DownloadDeferred(
        `AnyList downloads paused after ${this.consecutiveFailures} failure(s): ${this.lastError}`,
        this.cooldownUntil - now,
      );
    }

    const reason = this._reasonToRefresh(now, connected);
    if (!reason) return { download: false, reason: 'fresh' };

    const wait = this._waitBeforeNextDownload(now);
    if (wait > 0) {
      if (this.haveCopy) return { download: false, reason: `deferred:${reason}` };
      throw new DownloadDeferred(
        `AnyList download limit reached (${this._limitDescription(now)})`,
        wait,
      );
    }
    return { download: true, reason };
  }

  /** Record that a download is starting; counts against the limits. */
  started() {
    const now = this.now();
    this.attempts.push(now);
    this.inFlight = true;
    this.invalidatedInFlight = false;
    this._prune(now);
    this._save();
  }

  succeeded(reason) {
    this.lastSuccessAt = this.now();
    this.consecutiveFailures = 0;
    this.cooldownUntil = 0;
    this.lastError = null;
    this.haveCopy = true;
    if (!this.invalidatedInFlight) {
      this.stale = false;
      this.staleReason = null;
    }
    this.inFlight = false;
    this.invalidatedInFlight = false;
    this.lastRefreshReason = reason;
    this._save();
  }

  /**
   * A download failed.  Back off exponentially, or for as long as the server
   * asked if that is longer.
   */
  failed(error) {
    const now = this.now();
    this.inFlight = false;
    this.consecutiveFailures += 1;
    const backoff = Math.min(
      this.limits.failureBackoffMs * 2 ** (this.consecutiveFailures - 1),
      this.limits.maxBackoffMs,
    );
    const asked = parseRetryAfter(retryAfterOf(error), now) || 0;
    this.cooldownUntil = now + Math.max(backoff, asked);
    this.lastError = describe(error);
    this._save();
  }

  /** Milliseconds left in the current cooldown; 0 if none. */
  cooldownRemainingMs() {
    return Math.max(0, this.cooldownUntil - this.now());
  }

  // -- reporting ------------------------------------------------------------

  stats() {
    const now = this.now();
    this._prune(now);
    const iso = (ms) => (ms ? new Date(ms).toISOString() : null);
    return {
      downloads_last_hour: this.attempts.length,
      hourly_budget: this.limits.hourlyBudget,
      last_download: iso(this.attempts[this.attempts.length - 1]),
      last_success: iso(this.lastSuccessAt),
      last_refresh_reason: this.lastRefreshReason,
      stale: this.stale,
      stale_reason: this.staleReason,
      consecutive_failures: this.consecutiveFailures,
      cooldown_until: this.cooldownUntil > now ? iso(this.cooldownUntil) : null,
      last_error: this.lastError,
      limits_ms: { ...this.limits },
    };
  }

  // -- internals ------------------------------------------------------------

  _reasonToRefresh(now, connected) {
    if (!this.haveCopy) return 'no-copy';
    if (this.stale) return this.staleReason || 'stale';
    const age = now - (this.lastSuccessAt || 0);
    if (!connected && age >= this.limits.disconnectedMaxAgeMs) return 'websocket-down';
    if (age >= this.limits.maxAgeMs) return 'max-age';
    return null;
  }

  /** Milliseconds until the hard limits allow another download; 0 if now. */
  _waitBeforeNextDownload(now) {
    let wait = 0;
    const last = this.attempts[this.attempts.length - 1];
    if (last !== undefined) wait = Math.max(wait, last + this.limits.minIntervalMs - now);
    if (this.attempts.length >= this.limits.hourlyBudget) {
      const oldestCounted = this.attempts[this.attempts.length - this.limits.hourlyBudget];
      wait = Math.max(wait, oldestCounted + HOUR - now);
    }
    return wait;
  }

  _limitDescription(now) {
    const used = this.attempts.length;
    if (used >= this.limits.hourlyBudget) return `${used}/${this.limits.hourlyBudget} this hour`;
    return `at most one every ${Math.round(this.limits.minIntervalMs / 1000)}s`;
  }

  _prune(now) {
    while (this.attempts.length && this.attempts[0] <= now - HOUR) this.attempts.shift();
  }

  _load() {
    if (!this.ledgerFile) return;
    let raw;
    try {
      raw = JSON.parse(fs.readFileSync(this.ledgerFile, 'utf8'));
    } catch {
      return; // missing or corrupt: start clean rather than refuse to run
    }
    const now = this.now();
    if (Array.isArray(raw.attempts)) {
      // Drop anything in the future too: a clock that jumped back must not
      // leave the budget permanently spent.
      this.attempts = raw.attempts
        .filter((t) => Number.isFinite(t) && t > now - HOUR && t <= now)
        .sort((a, b) => a - b);
    }
    if (Number.isFinite(raw.lastSuccessAt)) this.lastSuccessAt = raw.lastSuccessAt;
    if (Number.isFinite(raw.consecutiveFailures)) this.consecutiveFailures = raw.consecutiveFailures;
    if (Number.isFinite(raw.cooldownUntil) && raw.cooldownUntil <= now + this.limits.maxBackoffMs) {
      this.cooldownUntil = raw.cooldownUntil;
    }
    if (typeof raw.lastError === 'string') this.lastError = raw.lastError;
  }

  _save() {
    if (!this.ledgerFile) return;
    const body = JSON.stringify({
      attempts: this.attempts,
      lastSuccessAt: this.lastSuccessAt,
      consecutiveFailures: this.consecutiveFailures,
      cooldownUntil: this.cooldownUntil,
      lastError: this.lastError,
    });
    try {
      fs.mkdirSync(path.dirname(this.ledgerFile), { recursive: true });
      const temp = `${this.ledgerFile}.tmp`;
      fs.writeFileSync(temp, body, { mode: 0o600 });
      fs.renameSync(temp, this.ledgerFile);
    } catch (error) {
      // Losing the ledger weakens the crash-loop guard but must not stop
      // syncing; the in-memory limits still apply.
      console.error(`could not write ${this.ledgerFile}: ${error.message}`);
    }
  }
}

/**
 * Stop AnyList's push from downloading the account on the spot.
 *
 * On every `refresh-shopping-lists` message the package's websocket handler
 * calls `this.getLists()`, a full download, and nothing here reads the
 * result.  Our own writes trigger those pushes too, so each write cost a
 * second download.  Shadowing `getLists` on the instance turns a push into a
 * note that the copy is stale; `client.download` keeps the real method for
 * when the policy allows it.
 *
 * This leans on the handler calling `this.getLists()`, which is why
 * package.json pins the exact version and a test drives the real handler.
 */
function divertPushes(client, policy) {
  client.download = client.getLists.bind(client);
  client.getLists = async () => {
    policy.invalidate('push');
    return client.lists;
  };
  return client;
}

function retryAfterOf(error) {
  const headers = error && error.response && error.response.headers;
  return headers ? headers['retry-after'] : undefined;
}

function statusOf(error) {
  return error && error.response && error.response.statusCode;
}

function describe(error) {
  const status = statusOf(error);
  const message = String((error && error.message) || error);
  return status && !message.includes(String(status)) ? `${status}: ${message}` : message;
}

module.exports = {
  FetchPolicy,
  DownloadDeferred,
  divertPushes,
  parseRetryAfter,
  statusOf,
  DEFAULTS,
};
