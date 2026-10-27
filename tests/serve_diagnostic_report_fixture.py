"""Disposable localhost fixture for the manual fault-report page only."""
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(); port = int(sys.argv[2])
if root.parent != Path('/tmp') or not root.name.startswith('codex-fault-report-'):
    raise SystemExit('disposable /tmp fixture required')
os.environ['CODEX_WEB_STATE_DIR'] = str(root / 'state')
os.environ.pop('CODEX_WEB_DIAGNOSTICS_DIR', None)
sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import routes.diagnostic_reports as reports
from shared.diagnostic_reports import reports_dir

# Fixture authentication is local-only and never imports or starts the live app.
reports.logged_in = lambda request: 'fixture-owner'
reports.require_auth = lambda request: None
reports.emit = lambda *args, **kwargs: True
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(reports.router)
app.mount('/static', StaticFiles(directory=Path(__file__).parents[1] / 'static'))

@app.get('/_fixture/reports')
def observed_reports(request: Request):
    values = []
    directory = reports_dir()
    if directory.exists():
        for entry in sorted(directory.iterdir()):
            metadata = entry / 'metadata.json'
            if entry.is_dir() and metadata.is_file():
                value = json.loads(metadata.read_text())
                values.append({'report_id': value['report_id'], 'fault_time': value['fault_time'],
                               'client_time': value['client_time'], 'screenshot': value['screenshot']['present']})
    return JSONResponse({'count': len(values), 'reports': values})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
