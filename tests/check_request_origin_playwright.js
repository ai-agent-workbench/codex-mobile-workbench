/* Actual cookie-bearing, same-site cross-port attacks against real auth + WS. */
const {chromium} = require('playwright');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const net = require('net');
const https = require('https');
const cp = require('child_process');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync('/tmp/codex-pwa-browser-');
let service, browser, attacker;
const freePort = () => new Promise(resolve => {
  const s = net.createServer(); s.listen(0, '127.0.0.1', () => {const p=s.address().port;s.close(()=>resolve(p));});
});
const get = url => new Promise((resolve,reject) => {
  const r=https.get(url,{rejectUnauthorized:false,family:4},response=>{
    let text='';response.on('data',part=>text+=part);response.on('end',()=>resolve(JSON.parse(text)));
  });r.on('error',reject);r.setTimeout(1000,()=>r.destroy());
});
(async()=>{
  const port=await freePort(), origin=`https://localhost:${port}`;
  cp.execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',path.join(temporary,'key.pem'),'-out',path.join(temporary,'cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],{stdio:'ignore'});
  service=cp.spawn((process.env.CODEX_WEB_PYTHON || path.join(root,'.venv/bin/python')),[path.join(__dirname,'serve_admin_pwa_fixture.py'),temporary,String(port)],{cwd:root,stdio:'ignore'});
  let fixture;
  for(let i=0;i<80;i++){try{fixture=await get(origin+'/fixture/recovery-code');break;}catch(_){await new Promise(r=>setTimeout(r,100));}}
  assert(fixture && fixture.code,'fixture started');
  attacker=https.createServer({key:fs.readFileSync(path.join(temporary,'key.pem')),cert:fs.readFileSync(path.join(temporary,'cert.pem'))},(_req,res)=>{if(_req.url==='/redirect'){res.writeHead(307,{Location:origin+'/fixture/change','Referrer-Policy':'no-referrer'});return res.end();}res.writeHead(200,{'Content-Type':'text/html','Referrer-Policy':'no-referrer'});res.end('<h1>Same-site attacker</h1>');});
  await new Promise(r=>attacker.listen(0,'127.0.0.1',r));
  const hostile=`https://localhost:${attacker.address().port}`;
  browser=await chromium.launch({headless:true,args:['--ignore-certificate-errors']});
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const page=await context.newPage(),cdp=await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
  page.once('dialog',d=>d.accept(fixture.code));
  await page.goto(origin+'/auth/passkey/recover');await page.locator('.fingerprint-action').click();await page.locator('#fixture-authenticated').waitFor();
  assert((await context.cookies()).some(c=>c.name==='__Host-codex_admin_session'),'actual secure auth cookie');
  // Legitimate browser JSON, multipart uploads and no-JS form POST are accepted.
  assert.strictEqual(await page.evaluate(()=>fetch('/fixture/change',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.status)),200);
  assert.strictEqual(await page.evaluate(()=>{const data=new FormData();data.append('file',new File(['fixture'],'fixture.txt'));return fetch('/fixture/change',{method:'POST',body:data}).then(r=>r.status);}),200);
  await page.evaluate(()=>{const f=document.createElement('form');f.action='/fixture/change';f.method='post';document.body.append(f);f.submit();});
  await page.waitForURL(origin+'/fixture/change');
  const formResult=await page.evaluate(()=>JSON.parse(document.body.innerText));
  if(!formResult.ok)console.log('failed_native_form_observation',(await context.request.get(origin+'/fixture/effects').then(r=>r.json())).attempts.at(-1));
  assert.strictEqual(formResult.ok,true);
  await page.goto(origin+'/home');
  const effects=()=>context.request.get(origin+'/fixture/effects').then(r=>r.json());
  assert.strictEqual((await effects()).changes,3);
  assert((await effects()).attempts.some(a=>a.kind==='http' && a.privacy_redacted && a.site==='same-origin' && a.cookie && a.status===200),
    'real no-referrer native form sent Origin:null and succeeded using same-origin metadata');
  assert.strictEqual(await page.evaluate(()=>new Promise(resolve=>{const w=new WebSocket(location.origin.replace('https:','wss:')+'/ws');w.onopen=()=>w.send('ping');w.onmessage=e=>{w.close();resolve(JSON.parse(e.data).type);};w.onerror=()=>resolve('error');})), 'pong');
  // On a different HTTPS port the cookie is still sent (same-site), but the
  // complete Origin differs. Exercise simple fetch, native form and WebSocket.
  await page.goto(hostile);
  await page.evaluate(target=>fetch(target+'/fixture/change',{method:'POST',credentials:'include',headers:{'Content-Type':'text/plain'},body:'attack'}).catch(()=>{}),origin);
  const afterFetch=await effects();
  assert(afterFetch.attempts.some(a=>a.kind==='http' && a.foreign && a.cookie && a.status===403),'server observed actual cookie-bearing cross-port fetch blocked before handler');
  const formResponse=page.waitForResponse(r=>r.url()===origin+'/fixture/change' && r.request().method()==='POST');
  await page.evaluate(target=>{const frame=document.createElement('iframe');frame.name='sink';document.body.append(frame);const f=document.createElement('form');f.target='sink';f.action=target+'/fixture/change';f.method='post';const input=document.createElement('input');input.name='command';input.value='attack';f.append(input);document.body.append(f);f.submit();},origin);
  assert.strictEqual((await formResponse).status(),403);
  assert.strictEqual(await page.evaluate(target=>new Promise(resolve=>{let opened=false;const w=new WebSocket(target.replace('https:','wss:')+'/ws');w.onopen=()=>{opened=true;w.close();};w.onclose=()=>resolve(opened);w.onerror=()=>{};setTimeout(()=>{w.close();resolve(opened);},3000);}),origin),false);
  const afterAttacks=await effects();
  assert.strictEqual(afterAttacks.changes,3);assert.strictEqual(afterAttacks.sockets,0);
  assert(afterAttacks.attempts.filter(a=>a.kind==='http' && a.foreign && a.cookie && a.status===403).length===2,'fetch and form attacks carried cookies');
  assert(afterAttacks.attempts.some(a=>a.kind==='websocket' && a.foreign && a.cookie && a.close===4403 && !a.accepted),'actual cookie-bearing WS rejected before accept');
  // Opaque sandbox and an attacker-controlled 307 cannot turn null into a
  // same-origin proof, even when the victim Service Worker forwards the POST.
  for(const scenario of ['sandbox','redirect']){
    const rejected=page.waitForResponse(r=>r.url()===origin+'/fixture/change' && r.request().method()==='POST');
    await page.evaluate(({target,hostile,scenario})=>{
      if(scenario==='sandbox'){
        const f=document.createElement('iframe');f.sandbox='allow-scripts allow-forms';
        f.srcdoc='<form method="post" action="'+target+'/fixture/change"><input name="field" value="attack"></form><script>document.forms[0].submit()<\/script>';
        document.body.append(f);
      }else{
        const form=document.createElement('form');form.method='post';form.action=hostile+'/redirect';form.target='sink';
        document.body.append(form);form.submit();
      }
    },{target:origin,hostile,scenario});
    assert.strictEqual((await rejected).status(),403,scenario+' bypassed guard');
    assert.strictEqual((await effects()).changes,3);
  }
  // Existing authenticated session survived rejected attacks; no auth mutation.
  await page.goto(origin+'/home');await page.locator('#fixture-authenticated').waitFor();
  console.log('request_origin_playwright=passed actual_auth_cookie=passed same_site_cross_port=blocked json_form_upload_ws=passed mobile_width=390');
  await context.close();
})().catch(error=>{console.error(error.message);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();if(attacker)await new Promise(r=>attacker.close(r));
  if(service && service.exitCode===null){service.kill('SIGTERM');await new Promise(r=>service.once('exit',r));}
  fs.rmSync(temporary,{recursive:true,force:true});
});
