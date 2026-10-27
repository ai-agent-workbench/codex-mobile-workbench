/* Local-only diagnostic timeline. No bodies, input values or credentials. */
(function () {
  'use strict';
  if (window.__codexDiagnostics) return;
  window.__codexDiagnostics = true;
  var nativeFetch = window.fetch, NativeWebSocket = window.WebSocket;
  var prefix = (location.pathname.match(/^\/[a-f0-9]{40,}(?=\/)/i) || [''])[0];
  var endpoint = prefix + '/settings/diagnostics/client-events';
  var page = window.CodexBootTrace ? window.CodexBootTrace.pageId : uid(), tab = uid(), action = uid(), seq = 0, timer, timerDue = 0, flushing = false;
  var retryNotBefore = 0, retry = 1000, maxBytes = 3 * 1024 * 1024;
  var staged = new Map(), memory = new Map(), storageWarned = false;
  var backupPrefix = 'codex-diagnostics-backup-v2:';
  var dbPromise, writes = Promise.resolve();
  function uid() {
    try { return crypto.randomUUID(); }
    catch (_) { return 'd-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2); }
  }
  function decoded(value) {
    for(var i=0;i<3;i++){try{var next=decodeURIComponent(value.replace(/\+/g,' '));if(next===value)break;value=next;}catch(_){break;}}
    return value;
  }
  function safeUserinfo(text) {
    return text.replace(/(?:https?|wss?):\/\/[^\s'"<>]+/ig,function(raw){
      try{var u=new URL(raw);if(!u.username&&!u.password)return raw;return u.protocol+'//[REDACTED]@'+u.host+u.pathname+u.search+u.hash;}
      catch(_){return raw.includes('@')?'[REDACTED_URL]':raw;}
    });
  }
  function sensitiveValue(value,depth) {
    value=decoded(value);depth=depth||0;
    if(/(^|(?:https?|wss?):\/\/[^\s/?#]+)\/+(?:[A-Za-z0-9_-]|%[0-9a-f]{2}){24,}(?=\/|[\s?#]|$)/i.test(value)||
      /(?:https?|wss?):\/\/[^\s]*ntfy/i.test(value)||/\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+/i.test(value)||
      /(?:https?|wss?):\/\/[^\s/@]+:[^\s/@]+@/i.test(value)||/%[0-9a-f]{2}/i.test(value))return true;
    if(/^(?:https?|wss?):\/\//i.test(value)){try{var u=new URL(value);if(u.username||u.password)return true;}catch(_){return true;}}
    var query=value.includes('?')?value.slice(value.indexOf('?')+1):value;
    if(!query.includes('='))return false;
    if(depth>=4)return true;
    var pairs=new URLSearchParams(query),count=0,secret=false;
    pairs.forEach(function(nested,key){
      if(++count>250||secretKey(decoded(key).replace(/[_-]/g,''))||sensitiveValue(nested,depth+1))secret=true;
    });
    return secret;
  }
  function secretKey(name){return /^(?:.*(?:token|secret|password|passwd|authorization|cookie|credential)|auth|apikey|key|code|signature|session(?:id|key)?|entry|ntfy.*)$/i.test(name);}
  function safeQueries(value) {
    return value.replace(/(^|[?&])([^\s?&#=]+)=([^\s&#]*)/g,function(all,separator,key,raw){
      var name=decoded(key).replace(/[_-]/g,'');
      if(secretKey(name)||name.includes('%')||sensitiveValue(raw))return separator+key+'=[REDACTED]';
      return all;
    });
  }
  function safeText(value) {
    var s = safeUserinfo(safeQueries(String(value == null ? '' : value)));
    s = s.replace(/(?:https?|wss?):\/\/[^\s'"<>]*ntfy[^\s'"<>]*/ig, '[REDACTED_NTFY_URL]')
      .replace(/\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+/ig, '[REDACTED_AUTH]')
      .replace(/((?:token|auth(?:orization)?|password|passwd|secret|cookie|api[_-]?key)["']?\s*[=:]\s*)["']?[^\s,;"']+/ig, '$1[REDACTED]')
      .replace(/((?:https?|wss?):\/\/)[^\s/@]+:[^\s/@]+@/ig, '$1[REDACTED]@')
      .replace(/([?&](?:key|code|signature|access_token|refresh_token)=)[^\s&#)]+/ig, '$1[REDACTED]')
      .replace(/(^|(?:https?|wss?):\/\/[^\s/?#]+|\s(?:GET|POST|HEAD|PUT|DELETE|PATCH|OPTIONS)\s)\/+(?:[A-Za-z0-9_-]|%[0-9a-f]{2}){24,}(?=\/|[\s?#]|$)/ig, '$1/[ENTRY]');
    return s.length > 48000 ? s.slice(0, 48000) + ' [TRUNCATED original_chars=' + s.length + ']' : s;
  }
  function safePath(value) {
    try { var u = new URL(value, location.href); return safeText((u.origin === location.origin ? '' : u.origin) + u.pathname + u.search); }
    catch (_) { return '[invalid-url]'; }
  }
  function project(data) {
    var result = {}, keys = ['path','target','tag','id','x','y','hit','hit_id','duration_ms','status','method','url','request_id','operation_id','code','message','stack','online','visibility','reason','count','phase','was_clean','width','height','default_prevented','release','persisted','script_version','user_agent','service_worker'];
    keys = keys.concat(['context','edge_id','wsl_id','page_id','client_id','resulting_client_id','worker_version','response_stage',
      'effective_type','connection_type','rtt','downlink','save_data','time_origin_ms','initiator_type','start_ms','worker_start_ms',
      'dns_start_ms','dns_end_ms','connect_start_ms','connect_end_ms','tls_start_ms','request_start_ms','response_start_ms',
      'response_end_ms','transfer_size','encoded_body_size','decoded_body_size','next_hop_protocol','delivery_type','retry_ms','accepted','previous_phase','previous_ts','line_number','column_number','version','was_discarded']);
    keys.forEach(function (key) {
      var v = data && data[key];
      if (typeof v === 'string') result[key] = safeText(v);
      else if (typeof v === 'number' && Number.isFinite(v) || typeof v === 'boolean') result[key] = v;
    });
    return result;
  }
  function errorData(error) {
    try { return {message: safeText(error && error.message || error), stack: safeText(error && error.stack || '')}; }
    catch (_) { return {message: '[unreadable error]'}; }
  }
  function make(type, data, actionId) {
    // Keep wire types understood by healthy rollback releases. New detail is
    // additive context, not a queue-poisoning unknown type on an older server.
    var contexts={resource_timing:'resource_timing',connection:'network_information',diagnostic_delivery:'diagnostics_delivery'};
    if(contexts[type]){data=Object.assign({},data,{context:contexts[type]});type=type==='connection'?'online':'fetch';}
    return {event_id: uid(), ts: new Date().toISOString(), type:type, page_id:page,
      action_id:actionId || action, tab_id:tab, seq:++seq, data:project(data)};
  }
  function size(event) { return new TextEncoder().encode(JSON.stringify(event)).length; }
/* DIAGNOSTIC_QUEUE_HELPER_START */
// Keep the v1 store self-describing and compatible with records left by the
// former navigation worker, without a second store or schema upgrade.
function diagnosticQueueBytes(value) { return new TextEncoder().encode(JSON.stringify(value)).length; }
function diagnosticQueueValid(event) {
  return !!(event && event._dq && Object.keys(event._dq).length === 1 &&
    Number.isSafeInteger(event._dq.bytes) && event._dq.bytes > 0);
}
function diagnosticQueuePrepare(event) {
  var stored=Object.assign({},event);delete stored._dq;
  var bytes=0;
  for(var i=0;i<12;i++) {
    stored._dq={bytes:bytes};
    var actual=diagnosticQueueBytes(stored);
    if(actual===bytes)return stored;
    bytes=actual;
  }
  stored._dq={bytes:diagnosticQueueBytes(stored)};
  return stored;
}
function diagnosticQueueWire(event) { var wire=Object.assign({},event);delete wire._dq;return wire; }
function diagnosticQueueReconcile(records,limit,makeGap,put,remove) {
  var total=0,dropped=0;
  for(var i=0;i<records.length;i++) {
    var event=records[i];
    if(!diagnosticQueueValid(event)) { event=diagnosticQueuePrepare(event);records[i]=event;put(event); }
    total+=event._dq.bytes;
  }
  while(total>limit&&records.length) { var old=records.shift();total-=old._dq.bytes;remove(old.event_id);dropped++; }
  if(dropped) {
    var gap=diagnosticQueuePrepare(makeGap(dropped));
    while(total+gap._dq.bytes>limit&&records.length) {
      old=records.shift();total-=old._dq.bytes;remove(old.event_id);dropped++;
      gap=diagnosticQueuePrepare(makeGap(dropped));
    }
    // A normal projected event is bounded well below this limit. Still do
    // not let an unexpectedly large capacity marker violate the hard cap.
    if(gap._dq.bytes<=limit) { put(gap);records.push(gap);total+=gap._dq.bytes; }
  }
  return {bytes:total,dropped:dropped};
}
/* DIAGNOSTIC_QUEUE_HELPER_END */
  var writeTimer = null, writing = false, activeBatch = [], activeBytes = 0;
  var stagedBytes = 0, memoryBytes = 0, stagedDropped = 0;
  function cachedSize(event) { return diagnosticQueueValid(event) ? event._dq.bytes : size(event); }
  function putMemory(event) {
    event = diagnosticQueuePrepare(event);
    var old = memory.get(event.event_id); if (old) memoryBytes -= cachedSize(old);
    memory.set(event.event_id, event); memoryBytes += cachedSize(event);
    var dropped = 0;
    while (memoryBytes > maxBytes - 1024 && memory.size) {
      var first = memory.keys().next().value, removed = memory.get(first);
      memory.delete(first); memoryBytes -= cachedSize(removed); dropped++;
    }
    if (dropped) {
      var gap = diagnosticQueuePrepare(make('gap', {reason:'memory_queue_capacity',count:dropped}));
      memory.set(gap.event_id,gap);memoryBytes+=cachedSize(gap);
    }
  }
  function warning(reason) {
    if (storageWarned) return;
    storageWarned = true;
    putMemory(make('gap', {reason:reason}));
    try { console.warn('Workbench diagnostics: ' + reason + '; retaining a bounded in-memory queue.'); } catch (_) {}
  }
  function openDatabase() {
    return new Promise(function (resolve) {
      var finished=false, request;
      function done(db) { if(finished){if(db)db.close();return;}finished=true;clearTimeout(timer);resolve(db); }
      var timer=setTimeout(function(){warning('indexeddb_open_timeout');done(null);},2000);
      try {
        request = indexedDB.open('codex-workbench-diagnostics-v2', 1);
        request.onupgradeneeded = function () {
          var store = request.result.createObjectStore('events', {keyPath:'event_id'});
          store.createIndex('ts', 'ts');
        };
        request.onsuccess = function () { done(request.result); };
        request.onerror = request.onblocked = function () { warning('indexeddb_unavailable');done(null); };
      } catch (_) { warning('indexeddb_unavailable');done(null); }
    });
  }
  function transaction(db, mode, work) {
    return new Promise(function(resolve,reject){
      var tx, settled=false, value;
      function done(error) { if(settled)return;settled=true;clearTimeout(timer);if(error)reject(error);else resolve(value); }
      var timer=setTimeout(function(){
        try{if(tx)tx.abort();}catch(_){}
        dbPromise=Promise.resolve(null);try{db.close();}catch(_){}
        done(Error('indexeddb_transaction_timeout'));
      },2000);
      try{
        tx=db.transaction('events',mode);
        tx.oncomplete=function(){done();};tx.onabort=tx.onerror=function(){done(tx.error||Error('indexeddb_transaction_failed'));};
        work(tx.objectStore('events'),function(result){value=result;});
      }catch(error){done(error);}
    });
  }
  // One read/modify/write per batch, never one pending Promise per event.
  function persist(events) {
    return dbPromise.then(function (db) {
      if (!db) throw Error('indexeddb_unavailable');
      return transaction(db,'readwrite',function(store){
        events.forEach(function(event){store.put(diagnosticQueueValid(event)?event:diagnosticQueuePrepare(event));});
        var read=store.index('ts').getAll();
        read.onsuccess=function(){diagnosticQueueReconcile(read.result,maxBytes-1024,function(count){
          return make('gap',{reason:'queue_capacity',count:count});
        },function(event){store.put(event);},function(id){store.delete(id);});};
      });
    });
  }
  function stage(event) {
    event=diagnosticQueuePrepare(event);
    var old=staged.get(event.event_id);if(old)stagedBytes-=cachedSize(old);
    staged.set(event.event_id,event);stagedBytes+=cachedSize(event);
    while(stagedBytes+activeBytes>maxBytes-1024&&staged.size){
      var first=staged.keys().next().value,removed=staged.get(first);
      staged.delete(first);stagedBytes-=cachedSize(removed);stagedDropped++;
    }
  }
  function scheduleWrites() {
    if(writing||writeTimer!==null)return;
    writeTimer=setTimeout(function(){writeTimer=null;drainWrites();},32);
  }
  function drainWrites() {
    if(writing)return writes;
    if(!staged.size&&!stagedDropped)return writes;
    if(writeTimer!==null){clearTimeout(writeTimer);writeTimer=null;}
    writing=true;
    writes=(async function(){
      // Yield between bounded groups. Continuous resource events must not keep
      // an upload waiting forever or build an unbounded chain of closures.
      for(var turn=0;turn<8&&(staged.size||stagedDropped);turn++){
        if(stagedDropped){var lost=stagedDropped;stagedDropped=0;stage(make('gap',{reason:'staged_queue_capacity',count:lost}));}
        activeBatch=[];activeBytes=0;
        for(var pair of staged){
          var bytes=cachedSize(pair[1]);
          if(activeBatch.length&&(activeBytes+bytes>240000||activeBatch.length>=60))break;
          activeBatch.push(pair[1]);activeBytes+=bytes;staged.delete(pair[0]);stagedBytes-=bytes;
        }
        try{await persist(activeBatch);}
        catch(_){warning('diagnostic_storage_write_failed');activeBatch.forEach(putMemory);}
        activeBatch=[];activeBytes=0;
      }
    })().catch(function(){warning('diagnostic_queue_failed');}).finally(function(){
      writing=false;if(staged.size||stagedDropped)scheduleWrites();schedule();
    });
    return writes;
  }
  function queuePersist(event) { stage(event);scheduleWrites(); }
  dbPromise = openDatabase();
  function record(type, data, actionId) {
    try { queuePersist(make(type,data,actionId)); schedule(250); } catch (_) { warning('diagnostic_record_failed'); }
  }
  window.codexDiagnostic = record;
  function retryClock() {
    // The retry gate must not move when wall-clock time is corrected.
    try { return performance.now(); } catch (_) { return Date.now(); }
  }
  function retryDelay() { return Math.max(0, retryNotBefore - retryClock()); }
  function schedule(delay) {
    var wait = Math.max(delay == null ? 250 : delay, retryDelay());
    var due = retryClock() + wait;
    // A short timer created while a request is in flight must not wake us
    // before the failure backoff that request establishes.
    if (timer && timerDue >= retryNotBefore && timerDue <= due) return;
    if (timer) clearTimeout(timer);
    timerDue = due;
    timer=setTimeout(function(){timer=null;timerDue=0;flush();}, wait);
  }
  async function pending() {
    await drainWrites();
    var db=await dbPromise, events=[];
    if(db) {
      try { events = await new Promise(function(resolve,reject){
        transaction(db,'readonly',function(store,done){
          var r=store.index('ts').openCursor(),bytes=0;
          r.onsuccess=function(){var cursor=r.result;if(!cursor){done(events);return;}
            var event=diagnosticQueueWire(cursor.value),next=size(event);
            if(events.length&&bytes+next>240000){events.more=true;done(events);return;}
            events.push(event);bytes+=next;
            if(events.length>=60){events.more=true;done(events);return;}
            cursor.continue();
          };r.onerror=function(){reject(r.error);};
        }).then(resolve,reject);
      }); }
      catch(_){warning('diagnostic_storage_read_failed');}
    }
    var seen=new Set(),bounded=[],used=0;
    function addPending(e) {
      if(seen.has(e.event_id))return false;seen.add(e.event_id);
      var bytes=size(e);
      if(bounded.length&&used+bytes>240000){bounded.more=true;return true;}
      bounded.push(e);used+=bytes;if(bounded.length>=60){bounded.more=true;return true;}return false;
    }
    for(var i=0;i<events.length&&!addPending(events[i]);i++) {}
    if(!bounded.more)for(var entry of memory.values())if(addPending(diagnosticQueueWire(entry)))break;
    if(events.more)bounded.more=true;return bounded;
  }
  async function acknowledge(ids) {
    var db=await dbPromise;
    if(db) await transaction(db,'readwrite',function(store){ids.forEach(function(id){store.delete(id);});});
    ids.forEach(function(id){var old=memory.get(id);if(old)memoryBytes-=cachedSize(old);memory.delete(id);});
    if(window.CodexBootTrace)window.CodexBootTrace.acknowledge(ids);
  }
  async function flush() {
    if(flushing || !nativeFetch || navigator.onLine===false) return;
    if(retryDelay()>0){schedule(retryDelay());return;}
    flushing=true;
    var deliveryPhase='queue_read', timeout;
    try {
      var events=await pending(), batch=[], bytes=0;
      for(var i=0;i<events.length && batch.length<60;i++) {var n=size(events[i]);if(batch.length && bytes+n>240000)break;batch.push(events[i]);bytes+=n;}
      if(!batch.length)return;
      var controller=new AbortController(), response;
      timeout=setTimeout(function(){controller.abort();},15000);
      deliveryPhase='response_headers';
      response=await nativeFetch.call(window,endpoint,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Diagnostics-Page-ID':page},body:JSON.stringify({events:batch}),signal:controller.signal});
      var authUnavailable=response.status===401 || response.status===403;
      if(authUnavailable)throw Error('diagnostic_auth_unavailable');
      deliveryPhase='ack_body';
      var result=await response.json(), sent=new Set(batch.map(function(e){return e.event_id;}));
      clearTimeout(timeout);timeout=null;
      deliveryPhase='queue_delete';
      var accepted=(result.accepted_event_ids||[]).filter(function(id){return sent.has(id);});
      if(accepted.length)await acknowledge(accepted);
      if(!response.ok || accepted.length!==batch.length)throw Error('diagnostic_partial_ack');
      if(retryNotBefore)record('diagnostic_delivery',{phase:'recovered',accepted:accepted.length});
      retry=1000;retryNotBefore=0;if(events.more||events.length>batch.length)schedule(250);
    }catch(error){
      var delay=retry;
      if(error && error.message==='diagnostic_auth_unavailable')delay=Math.max(delay,60000);
      retryNotBefore=retryClock()+delay;
      record('diagnostic_delivery',Object.assign({phase:deliveryPhase,retry_ms:delay},errorData(error)));
      schedule(delay);retry=Math.min(60000,retry*2);
    }
    finally{if(timeout)clearTimeout(timeout);flushing=false;}
  }
  // Only an emergency tail is synchronously serialized on navigation. Large
  // queues belong in asynchronous IDB, never in a pagehide main-thread dump.
  function emergencyBackup() {
    try {
      var pending=new Map();
      staged.forEach(function(e,id){pending.set(id,e);});
      activeBatch.forEach(function(e){pending.set(e.event_id,e);});
      memory.forEach(function(e,id){pending.set(id,e);});
      var candidates=Array.from(pending.values()), events=[],used=0,dropped=0,limit=64*1024-2048;
      for(var i=candidates.length-1;i>=0;i--){
        var e=diagnosticQueueWire(candidates[i]),bytes=cachedSize(candidates[i]);
        if(used+bytes<=limit){events.unshift(e);used+=bytes;}else dropped++;
      }
      if(dropped){var gap=make('gap',{reason:'emergency_backup_capacity',count:dropped});events.push(gap);queuePersist(gap);schedule();}
      if(events.length)localStorage.setItem(backupPrefix+page,JSON.stringify(events));
    }catch(_){warning('diagnostic_emergency_backup_failed');}
  }
  async function recoverBackups() {
    try {
      var keys=[];
      for(var i=0;i<localStorage.length;i++){var key=localStorage.key(i);if(key&&key.indexOf(backupPrefix)===0)keys.push(key);}
      for(var key of keys){
        var raw=localStorage.getItem(key);
        if(!raw)continue;
        if(raw.length>maxBytes){record('gap',{reason:'diagnostic_backup_too_large'});continue;}
        try{
          var values=JSON.parse(raw);
          if(!Array.isArray(values))throw Error('invalid_backup');
          var events=values.filter(function(e){return e&&e.event_id&&e.data;}).map(function(e){return diagnosticQueuePrepare(Object.assign({},e,{data:project(e.data)}));});
          if(events.reduce(function(sum,e){return sum+cachedSize(e);},0)>maxBytes)throw Error('oversized_backup');
          // Removing a backup is conditional on its exact bytes still being
          // current AND on a real IDB commit. Another tab may be replaying it.
          try{await persist(events);if(localStorage.getItem(key)===raw)localStorage.removeItem(key);}
          catch(_){events.forEach(putMemory);warning('diagnostic_backup_storage_failed');}
          schedule();
        }catch(_){record('gap',{reason:'diagnostic_backup_invalid'});}
        await new Promise(function(resolve){setTimeout(resolve,0);});
      }
    }catch(_){warning('diagnostic_backup_read_failed');}
  }
  function target(el) {return el&&el.nodeType===1?{tag:el.tagName,id:el.id||''}:{};}
  function listen(object,type,fn,capture){if(!object||typeof object.addEventListener!=='function')return;object.addEventListener(type,function(e){try{fn(e);}catch(_){warning('diagnostic_observer_failed');}},capture);}
  listen(window,'error',function(e){if(e.target&&e.target!==window)record('resource_error',{target:safePath(e.target.src||e.target.href||e.target.tagName)});else record('js_error',Object.assign({path:safePath(location.href)},errorData(e.error||e.message)));},true);
  listen(window,'unhandledrejection',function(e){record('unhandled_rejection',errorData(e.reason));});
  function documentLoaded(){var n=performance.getEntriesByType('navigation')[0],id='';(n&&n.serverTiming||[]).forEach(function(t){if(t.name==='diagnostics')id=t.description.replace(/^request-id=/,'');});record('page_load',{path:safePath(location.href),request_id:id,width:innerWidth,height:innerHeight});}
  if(document.readyState==='complete')documentLoaded();else listen(window,'load',documentLoaded);
  listen(window,'pageshow',function(e){record('page_show',{path:safePath(location.href),persisted:e.persisted});schedule();});
  listen(window,'pagehide',function(){record('page_hide',{path:safePath(location.href)});emergencyBackup();});
  listen(document,'visibilitychange',function(){record('visibility',{visibility:document.visibilityState});if(document.hidden)emergencyBackup();else schedule();});
  listen(window,'online',function(){record('online',{online:true});schedule();});
  listen(window,'offline',function(){record('offline',{online:false});});
  function connection() {
    var c=navigator.connection||navigator.mozConnection||navigator.webkitConnection;
    record('connection',{online:navigator.onLine,effective_type:c&&c.effectiveType,connection_type:c&&c.type,
      rtt:c&&c.rtt,downlink:c&&c.downlink,save_data:c&&c.saveData});
  }
  connection();
  var network=navigator.connection||navigator.mozConnection||navigator.webkitConnection;
  if(network&&network.addEventListener)listen(network,'change',connection);
  listen(window,'online',connection);listen(window,'offline',connection);
  function resourceTiming(e) {
    try {
      var u=new URL(e.name,location.href);
      if(u.origin!==location.origin||u.pathname===endpoint)return;
      var data={url:safePath(e.name),initiator_type:e.initiatorType||e.entryType,time_origin_ms:performance.timeOrigin,
        start_ms:e.startTime,duration_ms:e.duration,worker_start_ms:e.workerStart,dns_start_ms:e.domainLookupStart,
        dns_end_ms:e.domainLookupEnd,connect_start_ms:e.connectStart,connect_end_ms:e.connectEnd,tls_start_ms:e.secureConnectionStart,
        request_start_ms:e.requestStart,response_start_ms:e.responseStart,response_end_ms:e.responseEnd,
        transfer_size:e.transferSize,encoded_body_size:e.encodedBodySize,decoded_body_size:e.decodedBodySize,
        next_hop_protocol:e.nextHopProtocol,delivery_type:e.deliveryType,status:e.responseStatus};
      (e.serverTiming||[]).forEach(function(t){if(t.name==='diagnostics')data.request_id=t.description.replace(/^request-id=/,'');});
      record('resource_timing',data);
    }catch(_){}
  }
  if(window.PerformanceObserver){
    ['resource','navigation'].forEach(function(type){try{new PerformanceObserver(function(list){list.getEntries().forEach(resourceTiming);}).observe({type:type,buffered:true});}catch(_){}});
  }
  listen(document,'click',function(e){
    action=uid(); var hit=target(document.elementFromPoint(e.clientX,e.clientY)),link=e.target.closest&&e.target.closest('a[href]');
    record('click',Object.assign({x:e.clientX,y:e.clientY,hit:hit.tag||'',hit_id:hit.id||''},target(e.target)));
    if(link){var a=action;record('navigation',{phase:'intent',path:safePath(link.href)},a);setTimeout(function(){record('navigation',{phase:'dispatch',path:safePath(link.href),default_prevented:e.defaultPrevented},a);},0);}
  },true);
  listen(document,'submit',function(e){action=uid();record('submit',target(e.target));},true);
  if(typeof nativeFetch==='function')window.fetch=function(){
    var args=Array.from(arguments),input=args[0],init=args[1],a=action,op=uid(),url,method,start=performance.now(),observe=true;
    try {
      var raw=input&&input.url||input,u=new URL(raw,location.href);
      observe=!(u.origin===location.origin && u.pathname===endpoint);
      url=safePath(raw);method=init&&init.method||input&&input.method||'GET';
      // Only same-origin, CORS-capable requests. Do not clone/read bodies or
      // mutate the caller's Headers/Request; cross-origin must not preflight.
      if(observe&&u.origin===location.origin&&(init&&init.mode||input&&input.mode)!=='no-cors'){
        var headers=new Headers(init&&init.headers!==undefined?init.headers:input&&input.headers);
        headers.set('X-Diagnostics-Page-ID',page);headers.set('X-Diagnostics-Action-ID',a);headers.set('X-Diagnostics-Operation-ID',op);
        args[1]=Object.assign({},init||{},{headers:headers});
      }
    }catch(_){warning('diagnostic_fetch_observer_failed');}
    var promise=nativeFetch.apply(this,args);
    try {
      if(!observe)return promise;
      record('fetch',{phase:'start',url:url,method:method,operation_id:op},a);
      promise.then(function(r){record('fetch',{phase:'end',response_stage:'headers',url:url,method:method,operation_id:op,status:r.status,request_id:r.headers.get('x-request-id')||'',edge_id:r.headers.get('x-diagnostics-edge-id')||'',wsl_id:r.headers.get('x-diagnostics-wsl-id')||'',duration_ms:performance.now()-start},a);},function(e){record('fetch',Object.assign({phase:'error',url:url,method:method,operation_id:op,duration_ms:performance.now()-start},errorData(e)),a);}).catch(function(){});
    }catch(_){warning('diagnostic_fetch_observer_failed');}
    return promise;
  };
  if(NativeWebSocket){
    function DiagnosticWebSocket(){
      if(!new.target)throw TypeError("WebSocket constructor requires 'new'");
      var ws=Reflect.construct(NativeWebSocket,Array.from(arguments),new.target===DiagnosticWebSocket?NativeWebSocket:new.target);
      try{var url=safePath(arguments[0]),op=uid(),start=performance.now();record('ws',{phase:'connecting',url:url,operation_id:op});
        if(ws.addEventListener){listen(ws,'open',function(){record('ws',{phase:'open',url:url,operation_id:op,duration_ms:performance.now()-start});});listen(ws,'close',function(e){record('ws',{phase:'close',url:url,operation_id:op,code:e.code,was_clean:e.wasClean,reason:safeText(e.reason)});});listen(ws,'error',function(){record('ws',{phase:'error',url:url,operation_id:op});});}
      }catch(_){warning('diagnostic_ws_observer_failed');}return ws;
    }
    DiagnosticWebSocket.prototype=NativeWebSocket.prototype;Object.setPrototypeOf(DiagnosticWebSocket,NativeWebSocket);window.WebSocket=DiagnosticWebSocket;
  }
  var scriptVersion='';try{scriptVersion=new URL(document.currentScript.src).searchParams.get('v')||'';}catch(_){}
  if(window.CodexBootTrace)window.CodexBootTrace.subscribe(function(event){queuePersist(Object.assign({},event,{data:project(event.data)}));schedule();});
  recoverBackups();record('page_show',{phase:'script_start',path:safePath(location.href),online:navigator.onLine,visibility:document.visibilityState,script_version:scriptVersion,user_agent:navigator.userAgent,service_worker:!!(navigator.serviceWorker&&navigator.serviceWorker.controller)});
  setInterval(function(){schedule();},15000);
})();
