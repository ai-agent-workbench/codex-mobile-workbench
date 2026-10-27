const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static/reconnect-coordinator.js'), 'utf8');
function assert(value, message) { if (!value) throw new Error(message); }

class Events {
  constructor() { this.listeners = {}; }
  addEventListener(type, callback) { (this.listeners[type] || (this.listeners[type] = [])).push(callback); }
  dispatchEvent(event) { (this.listeners[event.type] || []).slice().forEach(callback => callback(event)); }
}
class Clock {
  constructor() { this.now = 0; this.next = 1; this.tasks = new Map(); }
  setTimeout(callback, delay) { const id = this.next++; this.tasks.set(id, {at: this.now + delay, callback, delay}); return id; }
  clearTimeout(id) { this.tasks.delete(id); }
  advance(delay) {
    const end = this.now + delay;
    while (true) {
      const due = [...this.tasks.entries()].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.tasks.delete(due[0]); this.now = due[1].at; due[1].callback();
    }
    this.now = end;
  }
  delays() { return [...this.tasks.values()].map(task => task.delay); }
}

function helperFixture(random) {
  const events = new Events();
  const document = new Events(); document.visibilityState = 'visible';
  const clock = new Clock();
  const window = events;
  window.document = document;
  window.navigator = {onLine: true};
  const context = {window, document, navigator: window.navigator, Math, Set, console};
  vm.runInNewContext(source, context, {filename: 'reconnect-coordinator.js'});
  return {window, document, clock, events, create: window.CodexReconnectCoordinator.create, random};
}

(function testBackoffAndRecovery() {
  const f = helperFixture(() => 0);
  let connects = 0, recoveries = 0;
  const retry = f.create({clock: f.clock, random: f.random, connect: () => { connects += 1; }, recover: () => { recoveries += 1; }});
  assert(retry.failed() === 2000 && f.clock.delays()[0] === 2000, 'first retry must be 2.5s base with bounded jitter');
  f.clock.advance(2000); assert(connects === 1, 'first retry did not connect');
  assert(retry.failed() === 4000, 'second retry did not grow exponentially');
  assert(retry.failed() === 8000, 'replaced retry did not preserve attempt count');
  retry.failed(); retry.failed();
  assert(f.clock.delays()[0] <= 30000, 'jittered exponential retry exceeded 30s cap');
  const capFixture = helperFixture(() => 1);
  const capped = capFixture.create({clock: capFixture.clock, random: capFixture.random});
  for (let attempt = 0; attempt < 6; attempt += 1) capped.failed();
  assert(capFixture.clock.delays()[0] === 30000, 'maximum jitter did not preserve the 30s cap');

  retry.opened();
  f.clock.advance(9999);
  assert(retry.failed() === 24000, 'short connection incorrectly reset backoff');
  retry.opened(); f.clock.advance(10000);
  assert(retry.state().attempts === 0, '10 second stable connection did not reset backoff');

  f.document.visibilityState = 'hidden'; f.document.dispatchEvent({type: 'visibilitychange'});
  assert(retry.failed() === false && f.clock.delays().length === 0, 'hidden page retained failed retry timer');
  f.clock.advance(30000); assert(connects === 1, 'hidden 30 seconds retried connection');

  f.document.visibilityState = 'visible';
  f.document.dispatchEvent({type: 'visibilitychange'});
  f.window.dispatchEvent({type: 'online'}); f.window.dispatchEvent({type: 'pageshow', persisted: true});
  assert(f.clock.delays().filter(delay => delay === 0).length === 1, 'recovery events did not merge in one tick');
  f.clock.advance(0); assert(recoveries === 1, 'merged recovery did not run once');

  retry.halt();
  f.window.dispatchEvent({type: 'online'}); f.window.dispatchEvent({type: 'pageshow', persisted: true});
  f.clock.advance(30000);
  assert(recoveries === 1 && connects === 1, 'authentication halt was revived by recovery events');
})();

(function testGlobalSocketIdentityAndHiddenHealth() {
  const clock = new Clock();
  const window = new Events(); const document = new Events();
  document.visibilityState = 'visible';
  document.querySelector = selector => selector === '.nav' ? {} : null;
  document.getElementById = () => null;
  window.document = document; window.navigator = {onLine: true};
  window.setTimeout = clock.setTimeout.bind(clock); window.clearTimeout = clock.clearTimeout.bind(clock);
  window.AudioContext = null; window.webkitAudioContext = null;
  window.localStorage = {getItem() { return null; }, setItem() { return true; }};
  window.sessionStorage = {getItem() { return null; }, setItem() {}};
  window.location = {protocol: 'http:', host: 'fixture.test', pathname: '/history'};
  window.CustomEvent = function (type, init) { this.type = type; this.detail = init && init.detail; };
  const sockets = [];
  function Socket() { this.readyState = Socket.CONNECTING; this.closeCalls = 0; sockets.push(this); }
  Socket.CONNECTING = 0; Socket.OPEN = 1; Socket.CLOSING = 2;
  Socket.prototype.close = function () { this.closeCalls += 1; this.readyState = 3; if (this.onclose) this.onclose({code: 1000}); };
  window.WebSocket = Socket;
  const context = {window, document, navigator: window.navigator, location: window.location,
    WebSocket: Socket, CustomEvent: window.CustomEvent, localStorage: window.localStorage,
    sessionStorage: window.sessionStorage, Math, Set, Promise, console};
  vm.runInNewContext(source, context, {filename: 'reconnect-coordinator.js'});
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static/completion-sound.js'), 'utf8'), context, {filename: 'completion-sound.js'});
  assert(sockets.length === 1, 'global listener did not open its initial socket');
  const first = sockets[0]; first.readyState = Socket.OPEN; first.onopen();
  document.visibilityState = 'hidden'; document.dispatchEvent({type: 'visibilitychange'});
  assert(first.closeCalls === 0, 'hiding closed a healthy global socket');
  document.visibilityState = 'visible'; document.dispatchEvent({type: 'visibilitychange'}); clock.advance(0);
  assert(sockets.length === 1, 'recovery rebuilt a healthy global socket');

  first.readyState = 3; first.onclose({code: 1006});
  if (!clock.tasks.size) throw new Error("global close did not schedule a retry");
  clock.advance(3000);
  assert(sockets.length === 2, 'failed global socket did not use bounded retry');
  const second = sockets[1];
  first.onclose({code: 1006}); clock.advance(30000);
  assert(sockets.length === 2, 'old socket callback replaced the current connecting socket');
  second.onclose({code: 4401});
  window.dispatchEvent({type: 'online'}); window.dispatchEvent({type: 'pageshow', persisted: true}); clock.advance(30000);
  assert(sockets.length === 2, 'authentication close was revived by recovery events');
})();

console.log('reconnect coordinator checks passed');
