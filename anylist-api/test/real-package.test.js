'use strict';

/**
 * The fix leans on internals of the real `anylist` package, so test against
 * the real package, faking only its transport: the websocket class it
 * constructs and the HTTP client it posts through.
 *
 * If an upgrade changes how the push handler refreshes, these fail rather
 * than the account quietly going back to a download per push.
 */

const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');

// Swap the websocket before the package loads, so nothing dials anylist.com.
const sockets = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.listeners = {};
    sockets.push(this);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  async deliver(data) {
    for (const listener of this.listeners.message || []) await listener({ data });
  }

  send() {}

  close() {}
}
const resolve = Module._resolveFilename;
const fakePath = require.resolve('./stub-anylist.js'); // any resolvable file
Module._resolveFilename = function (request, ...rest) {
  if (request === 'reconnecting-websocket') return `${fakePath}#ws`;
  return resolve.call(this, request, ...rest);
};
require.cache[`${fakePath}#ws`] = { id: 'ws', filename: 'ws', loaded: true, exports: FakeSocket };

const AnyList = require('anylist');
const { FetchPolicy, divertPushes } = require('../fetch-policy');

function account(protobuf, items) {
  return new protobuf.PBUserDataResponse({
    shoppingListsResponse: {
      newLists: [{ identifier: 'L1', name: 'Grocery', items }],
    },
    starterListsResponse: {
      recentItemListsResponse: { listResponses: [] },
      favoriteItemListsResponse: { listResponses: [] },
    },
    mealPlanningCalendarResponse: { calendarId: 'cal' },
  }).toBuffer();
}

function realClient() {
  const client = new AnyList({ email: 'x@example.com', password: 'y' });
  const posts = [];
  let items = [{ identifier: 'i1', name: 'milk' }];
  client.client = {
    post: async (url) => {
      posts.push(url);
      return { body: Buffer.from(account(client.protobuf, items)) };
    },
  };
  return { client, posts, setItems: (next) => { items = next; } };
}

const downloads = (posts) => posts.filter((u) => u === 'data/user-data/get').length;

test('without the diversion, a push downloads the whole account', async () => {
  // The behaviour being fixed, demonstrated on the real package.
  const { client, posts } = realClient();
  client._setupWebSocket();
  const socket = sockets[sockets.length - 1];

  await socket.deliver('refresh-shopping-lists');
  await socket.deliver('refresh-shopping-lists');

  assert.equal(downloads(posts), 2);
});

test('with it, a push only marks the copy stale', async () => {
  const { client, posts } = realClient();
  const policy = new FetchPolicy();
  divertPushes(client, policy);
  client._setupWebSocket();
  const socket = sockets[sockets.length - 1];

  await socket.deliver('refresh-shopping-lists');

  assert.equal(downloads(posts), 0);
  assert.equal(policy.stats().stale, true);
  assert.equal(policy.stats().stale_reason, 'push');
});

test('the real download is still there, and fills the lists', async () => {
  const { client, posts, setItems } = realClient();
  divertPushes(client, new FetchPolicy());

  await client.download();
  assert.deepEqual(client.lists[0].items.map((i) => i.name), ['milk']);

  setItems([{ identifier: 'i1', name: 'milk' }, { identifier: 'i2', name: 'eggs' }]);
  await client.download();
  assert.deepEqual(client.lists[0].items.map((i) => i.name), ['milk', 'eggs']);
  assert.equal(downloads(posts), 2);
});

test('the push handler still reports the lists it has, without fetching', async () => {
  const { client, posts } = realClient();
  divertPushes(client, new FetchPolicy());
  await client.download();
  client._setupWebSocket();
  const socket = sockets[sockets.length - 1];
  let emitted = null;
  client.on('lists-update', (lists) => { emitted = lists; });

  await socket.deliver('refresh-shopping-lists');

  assert.equal(emitted, client.lists);
  assert.equal(downloads(posts), 1);
});

test('the websocket exposes readyState, which the sidecar reads', () => {
  const { client } = realClient();
  client._setupWebSocket();
  assert.equal(client.ws.readyState, 1);
});
