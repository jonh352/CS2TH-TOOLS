'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {createRequire} = require('node:module');
const test = require('node:test');
const vm = require('node:vm');
const protobuf = require('protobufjs');
const SteamUser = require('steam-user');
const GlobalOffensive = require('globaloffensive');
const ByteBuffer = require('bytebuffer');
const {encodeCraftRequest} = require('../steam_gc_bridge/craft_protocol');

const bridgeFile = path.resolve(__dirname, '../steam_gc_bridge/bridge.js');
const requireBridge = createRequire(bridgeFile);
const wireSchema = protobuf.parse(`syntax = "proto2";
  message CMsgCraftItemsRequest {
    optional uint32 recipe_def = 1;
    repeated uint64 craft_items = 2;
  }`).root.lookupType('CMsgCraftItemsRequest');
const ids = Array.from({length: 10}, (_, index) => String(54131753598n + BigInt(index)));

function decode(body) {
  return wireSchema.toObject(wireSchema.decode(body), {longs: String});
}

test('craft request preserves uint64 IDs beyond Number precision', () => {
  const largeIds = ['9007199254740993', '9007199254740995', '18446744073709551615', '4', '5'];
  assert.deepEqual(decode(encodeCraftRequest(largeIds, 15)), {recipeDef: 15, craftItems: largeIds});
});

test('craft request rejects unsafe numeric IDs, duplicates and invalid counts', () => {
  assert.throws(() => encodeCraftRequest([1, '2', '3', '4', '5'], 5));
  assert.throws(() => encodeCraftRequest(['1', '1', '3', '4', '5'], 5));
  assert.throws(() => encodeCraftRequest(['1', '2', '3', '4', '18446744073709551616'], 5));
  assert.throws(() => encodeCraftRequest(['1'], 2));
  assert.throws(() => encodeCraftRequest(ids, -1));
});

function makeBridge(response = 'success', options = {}) {
  let client, gc;
  const requests = [];
  const events = [];
  const timers = new Set();
  class FakeSteam extends EventEmitter {
    constructor() {
      super(); client = this;
      this.steamID = {getSteamID64: () => '76561198000000001'};
    }
    logOn() { queueMicrotask(() => this.emit('loggedOn')); }
    logOff() {}
    gamesPlayed(apps) {
      if (apps.includes(730)) queueMicrotask(() => gc.emit('connectedToGC'));
    }
    sendToGC(...args) { return SteamUser.prototype.sendToGC.apply(this, args); }
    _send(outer, message) {
      requests.push(message);
      if (options.transportError) throw new Error('Transport failure');
      queueMicrotask(() => {
        // A repeated welcome must not submit the same irreversible operation twice.
        gc.emit('connectedToGC');
        if (response === 'timeout') {
          [...timers].find(timer => timer.ms === 60000).callback();
          return;
        }
        const packet = Buffer.alloc(response === 'rejected' ? 8 : 16);
        packet.writeInt16LE(response === 'rejected' ? -1 : 2, 0);
        packet.writeUInt16LE(response === 'rejected' ? 0 : 1, 6);
        if (response !== 'rejected') packet.writeBigUInt64LE(9007199254740993n, 8);
        GlobalOffensive.prototype._handlers[1003].call(gc, ByteBuffer.wrap(packet, ByteBuffer.LITTLE_ENDIAN));
      });
    }
  }
  class FakeGC extends EventEmitter {
    constructor() {
      super(); gc = this;
      this.inventory = ids.map(id => ({id, rarity: 3, quality: 4}));
      this.inventory.push({id: '9007199254740993', paint_index: 123, paint_wear: 0.125, rarity: 4, quality: 4});
      if (options.missingMaterial) this.inventory.shift();
    }
    craft() { throw new Error('Legacy binary craft must never be used'); }
  }
  const sandbox = {
    module: {exports: {}},
    require: name => name === 'steam-user' ? FakeSteam : name === 'globaloffensive' ? FakeGC : requireBridge(name),
    process: {stdout: {write: line => events.push(JSON.parse(line))}},
    setTimeout: (callback, ms) => { const timer = {callback, ms}; timers.add(timer); return timer; },
    clearTimeout: timer => timers.delete(timer),
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(bridgeFile, 'utf8'), sandbox, {filename: bridgeFile});
  const result = sandbox.module.exports.craftWithToken({assetIds: ids, expectedSteamId: '76561198000000001'}, 'fake-token');
  return {result, requests, events, timers};
}

test('bridge sends Protobuf envelope and handles the real binary success response', async () => {
  const run = makeBridge();
  const result = await run.result;
  assert.equal(run.requests.length, 1);
  const message = run.requests[0];
  assert.equal(message.appid, 730);
  assert.equal(message.msgtype, 0x800003ea);
  const headerLength = message.payload.readInt32LE(4);
  assert.deepEqual(decode(message.payload.subarray(8 + headerLength)), {recipeDef: 2, craftItems: ids});
  assert.equal(result.outputAssetIds[0], '9007199254740993');
  assert.equal(result.outputItems[0].paintWear, 0.125);
  assert.equal(run.timers.size, 0);
});

test('server rejection reports its original result and material summary without crashing', async () => {
  const run = makeBridge('rejected');
  await assert.rejects(run.result, error => {
    assert.equal(error.code, 'CRAFT_REJECTED');
    assert.match(error.message, /recipe=-1/);
    assert.match(error.message, /rarity=3/);
    assert.match(error.message, /10\/10/);
    return true;
  });
  assert.equal(run.requests.length, 1);
  assert.equal(run.timers.size, 0);
});

test('timeout does not resend or fall back to the legacy protocol', async () => {
  const run = makeBridge('timeout');
  await assert.rejects(run.result, error => error.code === 'CRAFT_TIMEOUT');
  assert.equal(run.requests.length, 1);
  assert.equal(run.timers.size, 0);
});

test('missing live material prevents sending any craft request', async () => {
  const run = makeBridge('success', {missingMaterial: true});
  await assert.rejects(run.result, error => error.code === 'VALIDATION');
  assert.equal(run.requests.length, 0);
  assert.equal(run.timers.size, 0);
});

test('synchronous transport failure is reported without fallback or a hanging timer', async () => {
  const run = makeBridge('success', {transportError: true});
  await assert.rejects(run.result, error => error.code === 'CRAFT_SEND' && /Transport failure/.test(error.message));
  assert.equal(run.requests.length, 1);
  assert.equal(run.timers.size, 0);
});
