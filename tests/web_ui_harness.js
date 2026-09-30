#!/usr/bin/env node
// Node harness for the web UI (tankie/data/script.js) — issue #32.
//
// Loads the REAL script.js in a VM sandbox with a stub WebSocket + stub
// DOM/joystick, then asserts:
//   1. every outgoing message is valid JSON (no key=value strings)
//   2. the outgoing messages are exactly the protocol-contract shapes
//      (drive / pan / tilt / stop)
//   3. the incoming state/ack/error/watchdog/hello messages are handled
//      (fields rendered, status hints shown, no exceptions)
//
// Usage: node tests/web_ui_harness.js
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const scriptSrc = fs.readFileSync(
  path.join(__dirname, '..', 'tankie', 'data', 'script.js'), 'utf8');

// --- stubs -----------------------------------------------------------------
const sent = [];                 // every websocket.send() payload
let ws;                          // the stub WebSocket instance

class StubWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = StubWebSocket.OPEN;
    StubWebSocket.last = this;
  }
  send(payload) { sent.push(payload); }
  // test hook: deliver an incoming frame
  _receive(text) {
    if (this.onmessage) this.onmessage({ data: text });
  }
}
StubWebSocket.CONNECTING = 0;
StubWebSocket.OPEN = 1;
StubWebSocket.CLOSING = 2;
StubWebSocket.CLOSED = 3;

const elements = {};
function el(id) {
  if (!elements[id]) elements[id] = { id, value: '', textContent: '', src: '' };
  return elements[id];
}

const joy1 = { x: 0, y: 0 };
const joy2 = { x: 0, y: 0 };
function JoyStick(div, param) {
  return {
    GetX: () => (div === 'joy1Div' ? joy1.x : joy2.x),
    GetY: () => (div === 'joy1Div' ? joy1.y : joy2.y),
  };
}

const timers = [];
let loadListener = null;
const sandbox = {
  console,
  // `battery` is a free global in script.js (browser implicit global by id);
  // expose the same object the DOM stub returns for it.
  battery: el('battery'),
  window: {
    addEventListener: (ev, fn) => { if (ev === 'load') loadListener = fn; },
    location: { hostname: 'tankie.local' },
  },
  document: { getElementById: (id) => el(id) },
  WebSocket: StubWebSocket,
  JoyStick,
  setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  setTimeout: (fn, ms) => 0,
  clearTimeout: () => {},
};
sandbox.window.WebSocket = StubWebSocket;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(scriptSrc, sandbox, { filename: 'script.js' });
if (loadListener) loadListener();   // fires initWebSocket() (stub connects)

// --- helpers ----------------------------------------------------------------
let failures = 0;
function check(cond, what) {
  if (cond) {
    console.log('  PASS  ' + what);
  } else {
    console.error('  FAIL  ' + what);
    failures++;
  }
}
function tick() {
  for (const t of timers) t.fn();
}
function lastSent() { return sent[sent.length - 1]; }
function assertAllJson() {
  let ok = true;
  for (const p of sent) { try { JSON.parse(p); } catch (e) { ok = false; } }
  check(ok, 'every sent payload is valid JSON');
}
function assertNoKeyValue() {
  let ok = true;
  for (const p of sent) {
    if (/^[a-z]+=(-?\d+)/i.test(p)) { console.error('    key=value: ' + p); ok = false; }
  }
  check(ok, 'no key=value messages sent');
}

// --- T1: joy1 drive -> {"cmd":"drive",...} ---------------------------------
console.log('T1: joy1 drive -> contract drive object');
sent.length = 0;
joy1.x = 50; joy1.y = 50;   // steer=127, speed=127
tick();
const driveMsgs = sent.filter((p) => p.startsWith('{"cmd":"drive"'));
assertAllJson();
check(driveMsgs.length === 1, 'exactly one drive message sent');
check(driveMsgs[0] === '{"cmd":"drive","speed":127,"steer":127}',
      'drive object is exact contract shape: ' + driveMsgs[0]);
assertNoKeyValue();

// --- T2: joy1 partial change -> new drive object ----------------------------
console.log('T2: joy1 change -> new drive object');
sent.length = 0;
joy1.y = 0;                  // speed=0, steer=127 (still non-zero -> drive)
tick();
const d2 = sent.filter((p) => p.startsWith('{"cmd":"drive"'));
check(d2.length === 1 && d2[0] === '{"cmd":"drive","speed":0,"steer":127}',
      'drive object after change: ' + d2[0]);

// --- T3: joy1 returns to center -> {"cmd":"stop"} ---------------------------
console.log('T3: joy1 center-return -> stop command');
sent.length = 0;
joy1.x = 0;                  // steer=0, speed=0 (was non-zero -> stop)
tick();
check(lastSent() === '{"cmd":"stop"}', 'stop object sent: ' + lastSent());
// A second tick at center must NOT re-send stop (single emission).
sent.length = 0;
tick();
check(sent.length === 0, 'no duplicate stop on repeated center ticks');

// --- T4: joy2 pan/tilt -> {"cmd":"pan"/"tilt","angle":N} --------------------
console.log('T4: joy2 pan/tilt -> contract pan/tilt objects');
sent.length = 0;
joy2.x = 50; joy2.y = 50;    // pan=135, tilt=45
tick();
check(sent.length === 2, 'two messages (pan + tilt)');
check(sent[0] === '{"cmd":"pan","angle":135}', 'pan object: ' + sent[0]);
check(sent[1] === '{"cmd":"tilt","angle":45}', 'tilt object: ' + sent[1]);
assertNoKeyValue();

// --- T5: incoming state -> all fields rendered ------------------------------
console.log('T5: incoming state -> fields rendered');
ws = StubWebSocket.last;
ws._receive('{"type":"state","seq":7,"battery":7.42,"speed":50,"steer":0,"pan":90,"tilt":30}');
check(el('battery').value === 7.42, 'battery rendered: ' + el('battery').value);
check(String(el('joy1Speed').value) === '50', 'speed rendered: ' + el('joy1Speed').value);
check(String(el('joy1Steer').value) === '0', 'steer rendered: ' + el('joy1Steer').value);
check(String(el('joy2Pan').value) === '90', 'pan rendered: ' + el('joy2Pan').value);
check(String(el('joy2Tilt').value) === '30', 'tilt rendered: ' + el('joy2Tilt').value);

// --- T6: incoming ack/error/watchdog/hello ----------------------------------
console.log('T6: incoming ack/error/watchdog/hello');
ws._receive('{"type":"ack","seq":8}');
ws._receive('{"type":"error","seq":9,"code":"range","field":"speed"}');
check(el('status').textContent.indexOf('error: range') === 0,
      'error hint shown: ' + el('status').textContent);
ws._receive('{"type":"watchdog"}');
check(el('status').textContent === 'watchdog: motors stopped',
      'watchdog hint shown: ' + el('status').textContent);
ws._receive('{"type":"hello","proto":1,"fw":"v0.1-serial"}');
ws._receive('not json at all');          // must not throw
ws._receive('{"type":"unknown"}');       // must not throw
console.log('  PASS  no exceptions on ack/error/watchdog/hello/garbage');

// --- T7: send() during CLOSED is a no-op (reconnect window) -----------------
console.log('T7: send() while socket closed is a no-op');
ws.readyState = StubWebSocket.CLOSED;
sent.length = 0;   // clear T4 leftovers before asserting on this tick
let threw = false;
try { joy1.x = 10; tick(); } catch (e) { threw = true; }
check(!threw, 'no exception thrown');
if (sent.length) console.log('  debug: sent while closed:', JSON.stringify(sent));
check(sent.length === 0, 'nothing sent while closed');

// --- final ------------------------------------------------------------------
console.log('');
if (failures === 0) {
  console.log('ALL WEB-UI HARNESS CHECKS PASSED');
  process.exit(0);
} else {
  console.error(failures + ' CHECK(S) FAILED');
  process.exit(1);
}
