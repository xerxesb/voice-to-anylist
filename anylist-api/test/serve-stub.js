'use strict';

/** Boot the real server with the `anylist` package swapped for the stub. */
const Module = require('module');
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

require('../server.js');
