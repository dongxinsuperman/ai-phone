"use strict";
// Test-only preload: never let this audit read the installed CLI profile or
// contact anything except its explicitly supplied loopback mock server.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const profile = path.join(os.homedir(), ".aiphone", "config.json");
const read = fs.readFileSync;
let profileReads = 0;
let requests = 0;
fs.readFileSync = function (filename, ...args) {
  if (typeof filename === "string" && path.resolve(filename) === profile) {
    profileReads += 1;
    throw new Error("Compatibility audit refuses real CLI profile access");
  }
  return read.call(this, filename, ...args);
};
const actualFetch = globalThis.fetch;
globalThis.fetch = function (input, options) {
  const url = new URL(input);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.port !== process.env.PERSONAL_COMPAT_PORT) {
    throw new Error("Compatibility audit refuses non-mock network target");
  }
  requests += 1;
  return actualFetch(input, options);
};
process.on("exit", () => process.stderr.write("PERSONAL_COMPAT_GUARD " + JSON.stringify({ profileReads, requests, loopbackOnly: true }) + "\n"));
