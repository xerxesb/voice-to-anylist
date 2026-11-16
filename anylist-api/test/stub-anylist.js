'use strict';

/**
 * An in-memory stand-in for the `anylist` package.
 *
 * Lets the sidecar's HTTP contract be tested end to end -- including from the
 * Python client that actually consumes it -- without real credentials.  It
 * mirrors the real client's shape: items carry an `identifier`, quantity is a
 * loosely-typed field, and edits only take effect on `save()`.
 *
 * It also mirrors the part that got the real account suspended: what the
 * server holds and what the client has in memory are separate.  `getLists()`
 * is a download, and is counted; writes go to the server and update the local
 * copy, as the real package does; and every server-side change pushes
 * `refresh-shopping-lists` to every connected client, including the writer,
 * which the real package answers by calling `this.getLists()`.
 *
 * `AnyList.stub` is the test's handle on the "server".
 */

const EventEmitter = require('events');

const server = {
  lists: null, // [{ identifier, name, items: [{identifier, name, quantity, checked}] }]
  downloads: 0,
  failures: [], // queued { statusCode, retryAfter } for the next downloads
  writeFailures: [],
  clients: new Set(),
  websocketOpen: true,
  reconnects: 0,
};

function seed() {
  server.lists = [
    { identifier: 'list-Grocery', name: 'Grocery', items: [
      { identifier: 'a1', name: 'milk', quantity: '', checked: false },
    ] },
  ];
}
seed();

function serverList(listId) {
  return server.lists.find((l) => l.identifier === listId);
}

function httpError(statusCode, retryAfter) {
  const error = new Error(`Response code ${statusCode}`);
  error.response = { statusCode, headers: retryAfter ? { 'retry-after': String(retryAfter) } : {} };
  return error;
}

async function serverWrite() {
  const failure = server.writeFailures.shift();
  if (failure) throw httpError(failure.statusCode, failure.retryAfter);
}

/** Tell every connected client something changed, as AnyList's websocket does. */
function push() {
  for (const client of server.clients) client._onPush();
}

class Item {
  constructor({ identifier, name, quantity = '', checked = false }, listId = null) {
    this.identifier = identifier || `item-${Math.random().toString(36).slice(2, 10)}`;
    this.name = name;
    this.quantity = quantity;
    this.checked = checked;
    this.listId = listId;
  }

  async save() {
    await serverWrite();
    const list = serverList(this.listId);
    const stored = list && list.items.find((i) => i.identifier === this.identifier);
    if (stored) Object.assign(stored, { quantity: this.quantity, checked: this.checked });
    push();
  }
}

class List {
  constructor(stored) {
    this.identifier = stored.identifier;
    this.name = stored.name;
    this.items = stored.items.map((i) => new Item({ ...i }, stored.identifier));
  }

  async addItem(item) {
    await serverWrite();
    item.listId = this.identifier;
    serverList(this.identifier).items.push({
      identifier: item.identifier,
      name: item.name,
      quantity: item.quantity,
      checked: item.checked,
    });
    this.items.push(item);
    push();
    return item;
  }

  async removeItem(item) {
    await serverWrite();
    const stored = serverList(this.identifier);
    stored.items = stored.items.filter((i) => i.identifier !== item.identifier);
    this.items = this.items.filter((i) => i.identifier !== item.identifier);
    push();
  }
}

class AnyList extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.lists = [];
    this.loggedIn = false;
    this.ws = undefined;
  }

  async login() {
    if (this.options.password === 'wrong') {
      throw new Error('login failed: bad credentials');
    }
    this.loggedIn = true;
    const stub = this;
    this.ws = {
      get readyState() { return server.websocketOpen && stub.loggedIn ? 1 : 3; },
      reconnect() { server.reconnects += 1; },
    };
    server.clients.add(this);
  }

  /** The real package's websocket handler, verbatim in effect. */
  async _onPush() {
    if (!server.websocketOpen) return;
    this.emit('lists-update', await this.getLists());
  }

  /** A download of the whole account. */
  async getLists() {
    server.downloads += 1;
    const failure = server.failures.shift();
    if (failure) throw httpError(failure.statusCode, failure.retryAfter);
    this.lists = server.lists.map((l) => new List(l));
    return this.lists;
  }

  getListByName(name) {
    return this.lists.find((l) => l.name === name);
  }

  createItem({ name, quantity }) {
    return new Item({ name, quantity });
  }

  teardown() {
    this.loggedIn = false;
    server.clients.delete(this);
  }
}

AnyList.stub = {
  server,
  push,
  reset() {
    seed();
    server.downloads = 0;
    server.failures = [];
    server.writeFailures = [];
    server.websocketOpen = true;
    server.reconnects = 0;
  },
  /** Someone edits the list in the official app. */
  externalEdit({ name, checked }) {
    const list = server.lists[0];
    const stored = list.items.find((i) => i.name === name);
    if (stored) stored.checked = checked;
    else list.items.push({ identifier: `ext-${name}`, name, quantity: '', checked });
    push();
  },
};

module.exports = AnyList;
