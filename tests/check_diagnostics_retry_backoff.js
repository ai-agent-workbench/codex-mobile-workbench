/* Deterministic browser-API fixture: no server, account, or IndexedDB required. */
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm'), crypto = require('crypto'), {TextEncoder} = require('util');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'diagnostics.js'), 'utf8');

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      const values = listeners.get(type) || []; values.push(listener); listeners.set(type, values);
    },
    dispatch(type, event = {}) { for (const listener of listeners.get(type) || []) listener(event); },
  };
}
function response(status, ids) {
  return {status, ok: status >= 200 && status < 300, json: async () => ({accepted_event_ids: ids || []})};
}
function harness(plans) {
  let now = 0, nextTimer = 1;
  const timers = new Map(), intervals = [], requests = [], window = eventTarget(), document = eventTarget();
  const storage = new Map(), navigator = {onLine: true};
  document.visibilityState = 'visible'; document.hidden = false;
  document.currentScript = {src: 'http://fixture/static/diagnostics.js?v=test'};
  const setTimeout = (fn, delay = 0) => {
    const id = nextTimer++; timers.set(id, {fn, due: now + Number(delay)}); return id;
  };
  const clearTimeout = (id) => timers.delete(id);
  window.fetch = (_url, options) => {
    const events = JSON.parse(options.body).events;
    requests.push(events);
    const plan = plans.shift();
    assert(plan, 'unexpected diagnostics upload');
    return typeof plan === 'function' ? plan(events, options) : plan;
  };
  window.WebSocket = undefined;
  window.console = {warn() {}};
  const context = {
    window, document, navigator, location: {href: 'http://fixture/history', pathname: '/history'},
    performance: {now: () => now, getEntriesByType: () => []}, crypto: {randomUUID: crypto.randomUUID},
    URL, URLSearchParams, AbortController, TextEncoder, console: window.console, setTimeout, clearTimeout, setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    localStorage: {get length() { return storage.size; }, key: (index) => [...storage.keys()][index] || null,
      getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key)},
  };
  vm.runInNewContext(source, context, {filename: 'diagnostics.js'});
  async function settle() { for (let i = 0; i < 24; i++) await Promise.resolve(); await new Promise((resolve) => setImmediate(resolve)); }
  async function advance(milliseconds) {
    const target = now + milliseconds;
    while (true) {
      let chosenId, chosen;
      for (const [id, timer] of timers) if (timer.due <= target && (!chosen || timer.due < chosen.due)) { chosenId = id; chosen = timer; }
      if (!chosen) break;
      now = chosen.due; timers.delete(chosenId); chosen.fn(); await settle();
    }
    now = target; await settle();
  }
  async function tickIntervals() { for (const interval of intervals) interval(); await settle(); }
  return {window, document, navigator, requests, advance, settle, tickIntervals};
}

async function checkAuthBackoff(status, includeLifecycleTriggers) {
  let release;
  const first = new Promise((resolve) => { release = resolve; });
  const h = harness([first, async (events) => response(200, events.map((event) => event.event_id))]);
  await h.advance(250);
  assert.strictEqual(h.requests.length, 1, 'initial event should start one upload');
  h.window.codexDiagnostic('js_error', {message: 'queued-during-inflight'});
  release(response(status));
  await h.settle();
  if (includeLifecycleTriggers) {
    h.navigator.onLine = false; h.window.dispatch('offline');
    h.document.hidden = false; h.document.visibilityState = 'visible'; h.document.dispatch('visibilitychange');
    h.navigator.onLine = true; h.window.dispatch('online');
  }
  await h.tickIntervals();
  await h.advance(59999);
  assert.strictEqual(h.requests.length, 1, `${status}/lifecycle must not bypass 60-second retry gate`);
  await h.advance(1);
  assert.strictEqual(h.requests.length, 2, `${status} retry should resume at the retry deadline`);
  await h.settle();
  return h;
}

(async () => {
  // The first check also verifies a pre-existing 250 ms timer from an event
  // recorded in-flight cannot replace an auth failure's longer backoff.
  await checkAuthBackoff(401, true);
  await checkAuthBackoff(403, false);

  const sustained = harness([async (events) => response(200, events.map((event) => event.event_id))]);
  await sustained.advance(100);
  sustained.window.codexDiagnostic('js_error', {message: 'steady-event-one'});
  await sustained.advance(100);
  sustained.window.codexDiagnostic('js_error', {message: 'steady-event-two'});
  await sustained.advance(50);
  assert.strictEqual(sustained.requests.length, 1, 'normal events must not keep postponing an already scheduled upload');
  await sustained.settle();

  const partialHarness = harness([
    async (events) => response(200, [events[0].event_id]),
    async (events) => response(200, events.map((event) => event.event_id)),
  ]);
  partialHarness.window.codexDiagnostic('js_error', {message: 'partial-ack-first'});
  partialHarness.window.codexDiagnostic('js_error', {message: 'partial-ack-second'});
  await partialHarness.advance(250);
  assert.strictEqual(partialHarness.requests.length, 1, 'partial ACK request missing');
  const acknowledged = partialHarness.requests[0][0].event_id;
  await partialHarness.settle();
  await partialHarness.advance(999);
  assert.strictEqual(partialHarness.requests.length, 1, 'partial ACK must obey retry deadline');
  await partialHarness.advance(1);
  assert.strictEqual(partialHarness.requests.length, 2, 'unconfirmed partial ACK event must retry');
  assert(!partialHarness.requests[1].some((event) => event.event_id === acknowledged), 'confirmed partial ACK event was resent');
  await partialHarness.settle();

  async function checkOrdinaryFailureBackoff(status) {
    let release;
    const finalFailure = new Promise((resolve) => { release = resolve; });
    const h = harness([
      response(status, []), response(status, []), response(status, []),
      response(status, []), response(status, []), response(status, []),
      finalFailure,
      async (events) => response(200, events.map((event) => event.event_id)),
    ]);
    await h.advance(250); await h.settle();
    for (const delay of [1000, 2000, 4000, 8000, 16000, 32000]) { await h.advance(delay); await h.settle(); }
    assert.strictEqual(h.requests.length, 7, `${status} should reach the 60-second retry tier exponentially`);
    h.window.codexDiagnostic('js_error', {message: `queued-during-${status}-inflight`});
    release(response(status, []));
    await h.settle();
    await h.advance(59999);
    assert.strictEqual(h.requests.length, 7, `${status} inflight event must not bypass the 60-second retry gate`);
    await h.advance(1);
    assert.strictEqual(h.requests.length, 8, `${status} should retry at the 60-second deadline`);
    await h.settle();
  }
  await checkOrdinaryFailureBackoff(404);
  await checkOrdinaryFailureBackoff(503);
  const hangingBody=harness([
    (_events,options)=>Promise.resolve({status:200,ok:true,json:()=>new Promise((_resolve,reject)=>{
      options.signal.addEventListener('abort',()=>reject(Error('fixture-ack-body-aborted')));
    })}),
    async events=>response(200,events.map(e=>e.event_id)),
  ]);
  await hangingBody.advance(250);
  await hangingBody.advance(14999);
  assert.strictEqual(hangingBody.requests.length,1,'ACK body must retain full timeout');
  await hangingBody.advance(1);await hangingBody.advance(999);
  assert.strictEqual(hangingBody.requests.length,1,'ACK body timeout must obey backoff');
  await hangingBody.advance(1);
  assert.strictEqual(hangingBody.requests.length,2,'headers without an ACK body must not wedge flushing forever');
  assert(hangingBody.requests[1].some(e=>e.data.context==='diagnostics_delivery'&&e.data.phase==='ack_body'),'missing ACK stage evidence');
  console.log('diagnostics retry backoff passed: 401/403 gate, lifecycle gate, sustained upload, partial ACK, 404/503 exponential retry, recovery');
})().catch((error) => { console.error(error); process.exitCode = 1; });
