'use strict';

/**
 * An in-memory stand-in for the `anylist` package.
 *
 * Lets the sidecar's HTTP contract be tested end to end -- including from the
 * Python client that actually consumes it -- without real credentials.  It
 * mirrors the real client's shape: items carry an `identifier`, quantity is a
 * loosely-typed field, and edits only take effect on `save()`.
 */

class Item {
  constructor({ identifier, name, quantity = '', checked = false }) {
    this.identifier = identifier || `item-${Math.random().toString(36).slice(2, 10)}`;
    this.name = name;
    this.quantity = quantity;
    this.checked = checked;
    this.saves = 0;
  }

  async save() {
    this.saves += 1;
  }
}

class List {
  constructor(name, items = []) {
    this.identifier = `list-${name}`;
    this.name = name;
    this.items = items;
  }

  async addItem(item) {
    this.items.push(item);
    return item;
  }

  async removeItem(item) {
    this.items = this.items.filter((i) => i.identifier !== item.identifier);
  }
}

class AnyList {
  constructor(options) {
    this.options = options;
    this.lists = [];
    this.loggedIn = false;
  }

  async login() {
    if (this.options.password === 'wrong') {
      throw new Error('login failed: bad credentials');
    }
    this.loggedIn = true;
  }

  async getLists() {
    if (this.lists.length === 0) {
      this.lists = [new List('Grocery', [new Item({ identifier: 'a1', name: 'milk' })])];
    }
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
  }
}

module.exports = AnyList;
