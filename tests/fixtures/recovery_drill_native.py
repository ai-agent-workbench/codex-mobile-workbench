"""Disposable loopback/UDS-only App Server peer for the recovery drill.
It implements only the RPCs exercised by the drill and never executes a model.
"""
import asyncio, json, os, sys
from pathlib import Path
import websockets

SOCKET = Path(sys.argv[1]); COUNTS = Path(sys.argv[2])
if SOCKET.parent != COUNTS.parent or not SOCKET.parent.is_dir():
    raise SystemExit("fixture socket and counter must share private directory")
MODEL = {"id":"fixture","model":"fixture","displayName":"Fixture","description":"isolated recovery drill","hidden":False,"isDefault":True,"defaultReasoningEffort":"medium","supportedReasoningEfforts":[{"reasoningEffort":"medium","description":"fixture"}],"serviceTiers":[]}
threads = {}; counts = {}
def record(method):
    counts[method] = counts.get(method, 0) + 1
    pending=COUNTS.with_suffix(".pending"); pending.write_text(json.dumps(counts), encoding="utf-8"); os.chmod(pending,0o600); pending.replace(COUNTS)
def reply(message, result=None, error=None):
    value={"id":message["id"]}
    value["error" if error else "result"] = error or result
    return value
async def handler(ws):
    async for raw in ws:
        message=json.loads(raw); method=message.get("method"); record(method)
        if method == "initialized": continue
        if method == "initialize": result={"codexHome":"/fixture/native-home","platformFamily":"unix","platformOs":"linux","userAgent":"codex_app_server/0.160.0"}
        elif method == "model/list": result={"data":[MODEL]}
        elif method == "skills/list": result={"data":[{"cwd":cwd,"errors":[],"skills":[]} for cwd in message.get("params",{}).get("cwds",[])]}
        elif method == "thread/start":
            ident="thread-drill"; threads[ident]={"id":ident,"cwd":message["params"]["cwd"],"status":{"type":"idle"},"turns":[]}; result={"thread":threads[ident]}
        elif method == "turn/start":
            ident="turn-drill"
            thread=threads[message["params"]["threadId"]]
            user={"id":"input-drill","type":"userMessage","clientUserMessageId":message["params"]["clientUserMessageId"],"content":message["params"]["input"]}
            turn={"id":ident,"status":"inProgress","items":[user]}
            thread["turns"].append(turn)
            await ws.send(json.dumps(reply(message,{"turn":turn})))
            # Match the native RPC contract: accepted response first, terminal later.
            await asyncio.sleep(.15)
            await ws.send(json.dumps({"method":"turn/started","params":{"threadId":thread["id"],"turn":turn}}))
            answer={"id":"answer-drill","type":"agentMessage","phase":"final_answer","text":"合成恢复演练完成"}
            turn["items"].append(answer)
            await ws.send(json.dumps({"method":"item/completed","params":{"threadId":thread["id"],"turnId":ident,"item":answer}}))
            turn["status"]="completed"
            await ws.send(json.dumps({"method":"turn/completed","params":{"threadId":thread["id"],"turn":turn}}))
            continue
        elif method in {"thread/read","thread/resume"}: result={"thread":threads.get(message.get("params",{}).get("threadId"),{"id":"thread-drill","turns":[]})}
        elif method == "thread/turns/list": result={"data":threads.get(message["params"]["threadId"],{}).get("turns",[]),"nextCursor":None}
        elif method == "thread/list": result={"data":list(threads.values()),"nextCursor":None}
        elif method == "thread/name": result={}
        else: return await ws.send(json.dumps(reply(message,error={"code":-32601,"message":"fixture method not implemented"})))
        await ws.send(json.dumps(reply(message,result)))
async def main():
    if SOCKET.exists(): raise SystemExit("refusing existing UDS")
    server=await websockets.unix_serve(handler,str(SOCKET)); os.chmod(SOCKET,0o600)
    try: await asyncio.Future()
    finally: server.close(); await server.wait_closed()
asyncio.run(main())
