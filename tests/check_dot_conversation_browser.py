"""Real offline Chromium + Jinja/JS/CSS; fetch responses are synthetic.

No listener, connected browser profile, credentials or installation is used.
An optional snapshot can carry the actual parent-dot handshake into the UI.
"""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shared.html_lib import html,nav,templates


HARNESS = r"""
<script>
window.__fixtureSnapshot = SNAPSHOT;
window.__testResult = {browser:'real_chromium',api:'offline_synthetic_fetch',phone:false,checks:{}};
var responseLost=true, postCalls=[], authError=AUTH_CODE, oldSnapshot=false;
var initialSnapshot=JSON.parse(JSON.stringify(window.__fixtureSnapshot));
window.fetch=async function(path,options){
  path=String(path); options=options||{};
  if(!path.includes('/dot/')) return {ok:true,status:200,json:async()=>({})};
  if(path.includes('/conversation/')) throw Error('native API requested');
  await new Promise(resolve=>setTimeout(resolve,15));
  if((options.method||'GET')==='POST'){
    var body=new URLSearchParams(options.body),id=body.get('message_id');
    postCalls.push(id);
    if(authError) return {ok:false,status:authError,json:async()=>({error:{code:authError===401?'authentication_required':'scope_denied'}})};
    var duplicate=window.__fixtureSnapshot.messages.find(m=>m.id===id);
    if(!duplicate){
      window.__fixtureSnapshot.messages.push({id:id,ordinal:window.__fixtureSnapshot.messages.length+1,
        text:body.get('text'),state:'waiting_for_dot',reply:null});
      window.__fixtureSnapshot.conversation.revision++;
    }
    if(responseLost){responseLost=false;throw Error('synthetic lost response after commit');}
  }
  var result=oldSnapshot?initialSnapshot:window.__fixtureSnapshot;
  return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
};
function assertCheck(name,value){window.__testResult.checks[name]=!!value;if(!value)throw Error(name);}
function delay(){return new Promise(resolve=>setTimeout(resolve,70));}
function publish(){var item=document.createElement('script');item.id='dot-browser-test-result';
  item.type='application/json';item.textContent=JSON.stringify(window.__testResult);document.body.append(item);}
document.addEventListener('DOMContentLoaded',async function(){
  try{
    var form=document.getElementById('conversation-composer'), input=form.querySelector('textarea'),
      send=form.querySelector('[type=submit]'), agent=document.querySelector('.native-message.agent .native-message-body'),
      reply=initialSnapshot.messages.find(m=>m.reply).reply.text;
    assertCheck('parent_reply_verbatim_in_dom',agent.textContent===reply);
    assertCheck('real_conversation_template',document.getElementById('native-conversation').dataset.conversationKind==='dot');
    assertCheck('native_controls_absent',!document.getElementById('conversation-model')&&!document.getElementById('stop-conversation'));
    assertCheck('native_script_absent',!Array.from(document.scripts).some(s=>/\/conversation\.js(?:\?|$)/.test(s.src)));
    var authToTest=authError;authError=0;
    input.value='[合成离线浏览器消息] <img src=x onerror=window.PWNED=1>';
    input.dispatchEvent(new Event('input',{bubbles:true}));
    form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
    form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
    await delay();
    assertCheck('single_inflight_send',postCalls.length===1);
    assertCheck('lost_response_keeps_text',input.value.includes('合成离线浏览器消息'));
    var firstId=postCalls[0];
    form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
    await delay();
    assertCheck('retry_same_message_id',postCalls.length===2&&postCalls[1]===firstId);
    assertCheck('retry_single_visible_message',Array.from(document.querySelectorAll('.native-message.user')).filter(n=>n.dataset.messageKey===firstId).length===1);
    assertCheck('accepted_message_clears_draft',input.value==='');
    assertCheck('untrusted_text_not_html',!document.querySelector('.native-message-body img')&&!window.PWNED);
    var count=document.querySelectorAll('.native-message').length;
    oldSnapshot=true;document.getElementById('reconcile-conversation').click();await delay();oldSnapshot=false;
    assertCheck('old_snapshot_does_not_regress_dom',document.querySelectorAll('.native-message').length===count);
    authError=authToTest;
    input.value='[合成失败状态] 保留这份输入';
    form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));await delay();
    assertCheck('auth_failure_preserves_input',input.value==='[合成失败状态] 保留这份输入');
    assertCheck('auth_failure_stops_sending',send.disabled);
    assertCheck('auth_failure_visible',!document.getElementById('conversation-note').hidden);
    window.__testResult.passed=true;
  }catch(error){window.__testResult.passed=false;window.__testResult.error=String(error);}
  publish();
});
</script>
"""


class ResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active=False
        self.parts=[]

    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('id')=='dot-browser-test-result':self.active=True

    def handle_endtag(self,tag):
        if tag=='script':self.active=False

    def handle_data(self,data):
        if self.active:self.parts.append(data)


def render(snapshot):
    content=templates.get_template('conversation.html').render(
        conversation=snapshot['conversation'],initial_snapshot=snapshot,
        back_url='/home',project_name='dot',nav_html=nav(),settings={},models=[],skills=[],
        catalog_error=False,upload_notice_html='',upload_button_html='',upload_panel_html='',
    )
    page=html('dot',content).body.decode('utf-8')
    static=Path(__file__).resolve().parents[1]/'static'
    page=re.sub(r'(["\'])(/static/[^"\']+)\1',
        lambda match:match[1]+(static/match[2].split('?',1)[0][len('/static/'):]).as_uri()+match[1],page)
    # Keep this fixture offline and do not register a service worker/profile.
    page=re.sub(r'<script>if\("serviceWorker".*?</script>','',page)
    return page


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',required=True)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args()
    snapshot=json.loads(Path(args.snapshot).read_text(encoding='utf-8'))
    output=Path(args.output_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    page=render(snapshot)
    summary=[]
    for code in (401,403,503):
        fixture=output/('ui-auth-'+str(code)+'.html')
        encoded=json.dumps(snapshot,ensure_ascii=False).replace('<','\\u003c')
        fixture.write_text(page.replace('<head>','<head>'+HARNESS.replace('SNAPSHOT;',encoded+';').replace('AUTH_CODE',str(code)),1),encoding='utf-8')
        with tempfile.TemporaryDirectory(prefix='dot-browser-profile-') as profile:
            command=[args.browser,'--headless','--no-sandbox','--disable-gpu','--no-first-run',
                     '--disable-dev-shm-usage','--timeout=5000',
                     '--disable-background-networking','--disable-component-update','--disable-sync',
                     '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+profile,
                     '--window-size=390,844','--virtual-time-budget=2500','--dump-dom',fixture.as_uri()]
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=30)
        parser=ResultParser();parser.feed(result.stdout)
        value=json.loads(''.join(parser.parts)) if parser.parts else {'passed':False,'error':'no browser result; '+result.stderr[-400:]}
        value['auth_status_tested']=code
        value['browser_exit_code']=result.returncode
        summary.append(value)
        (output/('result-'+str(code)+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # A separate unmodified page for review, with the actual parent reply.
    review=output/'review.html'
    review.write_text(page,encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='dot-browser-review-profile-') as profile:
        result=subprocess.run([args.browser,'--headless','--no-sandbox','--disable-gpu','--no-first-run',
            '--disable-dev-shm-usage','--timeout=5000',
            '--disable-background-networking','--disable-component-update','--disable-sync',
            '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+profile,
            '--window-size=390,844','--virtual-time-budget=300',
            '--screenshot='+str(output/'dot-mobile-preview.png'),review.as_uri()],
            capture_output=True,text=True,encoding='utf-8',timeout=30)
    report={'ui':'real_jinja_template_and_chromium','api':'synthetic_offline_fetch',
            'real_phone_tested':False,'automatic_wakeup':False,'cases':summary,
            'screenshot_created':(output/'dot-mobile-preview.png').is_file(),
            'passed':all(case.get('passed') and case['browser_exit_code']==0 for case in summary)}
    (output/'browser-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
