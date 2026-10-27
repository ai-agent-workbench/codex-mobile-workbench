"""Read-only, time-bounded workbench evidence; never changes permissions/services.

Usage: .venv/bin/python -m shared.diagnostics_read --since '2026-09-30 10:27' --until '2026-09-30 10:35'
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import zlib
from pathlib import Path
from typing import Iterator

from shared.config import STATE
from shared.diagnostics import redact

UTC = dt.timezone.utc


def instant(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.astimezone(UTC)


def safe(value):
    # Correlation IDs in structured fields survive. Raw nginx URLs must not
    # disclose opaque authentication entry paths, userinfo or query values.
    if isinstance(value, str):
        value = re.sub(r'(?i)(\b(?:GET|POST|HEAD|PUT|DELETE|OPTIONS)\s+)/[A-Za-z0-9_-]{24,}(?=/|[?\s])', r'\1/[ENTRY]', value)
    return redact(value, limit=200_000)


def source_event(name, status, **data):
    return {'source':name, 'source_status':status, **data}


def local_events(directory: Path) -> Iterator[dict]:
    files = sorted(directory.glob('*.jsonl*')) if directory.exists() else []
    stalls = directory / 'web-stalls'
    # The optional bounded evidence directory is not an arbitrary recursive log
    # search. Never follow a replacement link into another directory.
    if stalls.is_symlink() or (stalls.exists() and not stalls.is_dir()):
        yield source_event('diagnostics', 'unavailable', file='web-stalls', reason='not_regular_directory')
    elif stalls.is_dir():
        for name in ('stalls.jsonl', 'stalls.jsonl.1', 'stalls.jsonl.2'):
            path = stalls / name
            if path.exists() or path.is_symlink():
                files.append(path)
    if not files:
        yield source_event('diagnostics', 'unavailable', reason='no_diagnostic_files'); return
    for path in files:
        filename = path.relative_to(directory).as_posix()
        try:
            if path.is_symlink() or not path.is_file():
                yield source_event('diagnostics', 'unavailable', file=filename, reason='not_regular_file'); continue
            opener = gzip.open if path.suffix == '.gz' else open
            with opener(path, 'rt', encoding='utf-8', errors='replace') as handle:
                corrupt = 0
                for line in handle:
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict): raise ValueError()
                        yield {'source':'diagnostics', **value}
                    except (ValueError, TypeError): corrupt += 1
                if corrupt: yield source_event('diagnostics', 'partial', file=filename, malformed_lines=corrupt)
            yield source_event('diagnostics', 'read', file=filename)
        except (OSError, EOFError, zlib.error) as exc:
            yield source_event('diagnostics', 'unavailable', file=filename, reason=str(exc))


def audit_events(path=STATE/'audit.jsonl'):
    try:
        malformed=0
        with path.open(encoding='utf-8',errors='replace') as handle:
            for line in handle:
                try:
                    value=json.loads(line)
                    yield {**value,'source':'audit','ts':value['time']}
                except (ValueError,KeyError,TypeError):malformed+=1
        yield source_event('audit','partial' if malformed else 'read',malformed_lines=malformed)
    except OSError as exc:yield source_event('audit','unavailable',reason=str(exc))


def journal_events(since: dt.datetime, until: dt.datetime) -> Iterator[dict]:
    # journalctl can return exit 0 with an empty result when a user cannot see
    # system units. Explicitly use the already-authorized sudo path instead of
    # treating that empty result as proof of health.
    cmd = ['journalctl','--no-pager','-o','json','--since',since.isoformat(),'--until',until.isoformat(),
           '-u','codex-web.service','-u','codex-web-app-server.service','-u','nginx.service','-u','logrotate.service']
    if os.geteuid() != 0: cmd = ['sudo','-n',*cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode:
            yield source_event('journal', 'unavailable', reason=safe(result.stderr.strip()), exit_code=result.returncode); return
        count = 0
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
                stamp = dt.datetime.fromtimestamp(int(item['__REALTIME_TIMESTAMP'])/1_000_000, UTC)
                yield {'source':'journal','ts':stamp.isoformat(),'unit':item.get('_SYSTEMD_UNIT'),
                       'level':item.get('PRIORITY'), 'message':safe(item.get('MESSAGE',''))}
                count += 1
            except (ValueError, KeyError, TypeError):
                yield source_event('journal','partial',reason='malformed_record')
        yield source_event('journal','read',records=count,privilege='root' if os.geteuid()==0 else 'existing_sudo')
    except (OSError, subprocess.TimeoutExpired) as exc:
        yield source_event('journal','unavailable',reason=str(exc))


def nginx_stamp(line: str):
    if line.lstrip().startswith('{'):
        try:
            value=json.loads(line)
            return dt.datetime.fromtimestamp(float(value['end_msec']),UTC) if value.get('end_msec') else instant(value['ts'])
        except (ValueError, KeyError, TypeError, OverflowError, OSError): return None
    match = re.search(r'\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]',line)
    if match: return dt.datetime.strptime(match[1], '%d/%b/%Y:%H:%M:%S %z').astimezone(UTC)
    match = re.match(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}', line)
    return dt.datetime.strptime(match[0], '%Y/%m/%d %H:%M:%S').astimezone(UTC) if match else None


def nginx_record(line: str, source: str, filename: str):
    """Read native JSON or legacy access/error records without losing evidence."""
    stamp = nginx_stamp(line)
    if stamp is None: return None
    if line.lstrip().startswith('{'):
        value = json.loads(line)
        if not isinstance(value, dict): return None
        # Source and filename are reader-owned, not controlled by log fields.
        return {**value, 'source':source, 'ts':stamp.isoformat(), 'file':filename}
    event = {'source':source, 'ts':stamp.isoformat(), 'file':filename, 'message':safe(line.rstrip())}
    access = re.search(r'"(?:GET|POST|HEAD|PUT|DELETE|PATCH|OPTIONS) [^\"]*" (\d{3}) (\d+|-)', line)
    if access:
        event.update(status=int(access[1]), body_bytes_sent=int(access[2]) if access[2] != '-' else None,
                     schema='legacy-access')
    return event


def nginx_events(since: dt.datetime, directory=Path('/var/log/nginx'), source='nginx') -> Iterator[dict]:
    files = sorted(set(directory.glob('codex-web-*.log*')) | set(directory.glob('codex-web-*.jsonl*')) |
                   (set(directory.glob('error.log*')) if source == 'windows_gateway' else set()))
    if not files:
        yield source_event(source,'unavailable',reason='no_gateway_log_files'); return
    scanned = 0
    for path in files:
        try:
            if path.is_symlink() or not path.is_file(): continue
            if path.stat().st_mtime < since.timestamp(): continue
            scanned += 1
            parse_stats = {"unparsed": 0}
            def rows(lines):
                for line in lines:
                    try: event = nginx_record(line, source, path.name)
                    except (ValueError, TypeError): event = None
                    if event is None: parse_stats["unparsed"] += 1
                    if event and instant(event['ts']) >= since:
                        yield event
            if os.access(path, os.R_OK):
                opener = gzip.open if path.suffix == '.gz' else open
                with opener(path, 'rt', encoding='utf-8', errors='replace') as handle:
                    yield from rows(handle)
            else:
                cmd = ['sudo','-n',*(['gzip','-cd','--',str(path)] if path.suffix=='.gz' else ['cat','--',str(path)])]
                result = subprocess.run(cmd,capture_output=True,text=True,timeout=30)
                if result.returncode:
                    yield source_event(source,'unavailable',file=path.name,reason=safe(result.stderr));continue
                yield from rows(result.stdout.splitlines())
            yield source_event(source,'partial' if parse_stats['unparsed'] else 'read',file=path.name,unparsed_lines=parse_stats['unparsed'])
        except (OSError,EOFError,zlib.error,ValueError,subprocess.TimeoutExpired) as exc:
            yield source_event(source,'unavailable',file=path.name,reason=str(exc))
    yield source_event(source,'read',files_in_window=scanned)


def windows_events(since):
    # Read-only profile discovery; no UAC, service change or credential access.
    configured = os.environ.get('CODEX_WEB_WINDOWS_NGINX_DIR')
    try:
        if configured:
            directory = Path(configured) / 'logs'
        else:
            powershell = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command',
                '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; [Environment]::GetFolderPath("UserProfile")'],
                capture_output=True,text=True,timeout=15)
            profile=result.stdout.strip().lstrip('\ufeff')
            if result.returncode or not re.match(r'^[A-Za-z]:[\\/]', profile):
                yield source_event('windows_gateway','unavailable',reason='windows_profile_discovery_failed');return
            directory=Path('/mnt')/profile[0].lower()/profile[3:].replace('\\','/')/'codex-web'/'nginx'/'logs'
        yield from nginx_events(since,directory,'windows_gateway')
    except (OSError,subprocess.TimeoutExpired) as exc:
        yield source_event('windows_gateway','unavailable',reason=str(exc))


def publish_events(path=STATE/'releases'/'publish.log'):
    try:
        stamp=None
        for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
            try: stamp=instant(line.split()[0])
            except (ValueError,IndexError):
                match=re.search(r'发布 (\d{8}-\d{6})',line)
                if match: stamp=dt.datetime.strptime(match[1],'%Y%m%d-%H%M%S').astimezone(UTC)
            if stamp: yield {'source':'publish','ts':stamp.isoformat(),'message':safe(line)}
        yield source_event('publish','read')
    except OSError as exc: yield source_event('publish','unavailable',reason=str(exc))


def fault_report_context(directory: Path, report_id: str) -> dict:
    """Read one explicitly selected private receipt; never image/owner data."""
    from shared.diagnostic_reports import _private_dir, _load_existing, ReportError
    if not re.fullmatch(r'[a-f0-9]{32}', report_id):
        raise ReportError('invalid_report_id')
    _private_dir(directory)
    _private_dir(directory / 'reports')
    value = _load_existing(directory / 'reports' / report_id)
    keys = ('report_id', 'created_at', 'fault_time', 'category', 'client_page_id',
            'source_path', 'last_phase', 'client_time', 'client_version', 'release',
            'request_id', 'screenshot', 'receipt')
    return {'source': 'diagnostics', 'kind': 'fault_report_context',
            'ts': value['created_at'], **{k: value[k] for k in keys if k in value}}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since',help='ISO time; no timezone means local time. Default: last 30 minutes')
    parser.add_argument('--until',help='ISO time; default now')
    parser.add_argument('--report-id', help='Private manual report ID; include receipt and a 10-minute fault-time timeline')
    parser.add_argument('--request-id');parser.add_argument('--page-id')
    parser.add_argument('--operation-id');parser.add_argument('--edge-id');parser.add_argument('--connection-id')
    parser.add_argument('--client-time',action='store_true',help='Use browser client_ts for window filtering/sorting; client/server clocks may differ')
    parser.add_argument('--sources',default='diagnostics,audit,journal,nginx,windows_gateway,publish')
    parser.add_argument('--limit',type=int,default=2000,help='Maximum output events; 0 means all. Skipped count is explicit.')
    parser.add_argument('--diagnostics-dir',type=Path,default=Path(os.environ.get('CODEX_WEB_DIAGNOSTICS_DIR',str(STATE/'diagnostics'))))
    args=parser.parse_args(argv)
    report = None
    if args.report_id:
        try:
            report = fault_report_context(args.diagnostics_dir, args.report_id)
            if not args.since and not args.until:
                focus = instant(report['fault_time'])
                args.since = (focus-dt.timedelta(minutes=5)).isoformat()
                args.until = (focus+dt.timedelta(minutes=5)).isoformat()
        except (OSError, ValueError, KeyError) as exc:
            print(json.dumps(source_event('diagnostics', 'unavailable',
                report_id=args.report_id, reason=str(exc)),ensure_ascii=False))
            return 2
    try:
        until=instant(args.until) if args.until else dt.datetime.now(UTC)
        since=instant(args.since) if args.since else until-dt.timedelta(minutes=30)
        if since>until or args.limit<0: raise ValueError('invalid time range or limit')
    except ValueError as exc: parser.error(str(exc))
    sources=args.sources.split(',');unknown=set(sources)-{'diagnostics','audit','journal','nginx','windows_gateway','publish'}
    if unknown: parser.error('unknown sources: '+','.join(sorted(unknown)))
    records=[];reports=[]
    for source in sources:
        stream = {'diagnostics':lambda:local_events(args.diagnostics_dir),'audit':audit_events,'journal':lambda:journal_events(since,until),'nginx':lambda:nginx_events(since),'windows_gateway':lambda:windows_events(since),'publish':publish_events}[source]()
        for event in stream:
            if 'source_status' in event: reports.append(event);continue
            try: stamp=instant(event.get('client_ts', event['ts']) if args.client_time else event['ts'])
            except (KeyError,ValueError): reports.append(source_event(source,'partial',reason='invalid_timestamp'));continue
            if not since<=stamp<=until:continue
            # Look in both the enclosing intake request and the browser's
            # original request metadata. The two request IDs have different roles.
            data = event.get('data', {})
            if not isinstance(data, dict): data = {}
            def matches(value, *keys):
                return not value or any(value == str(row.get(key, '')) for row in (event, data) for key in keys)
            if not matches(args.request_id,'request_id','client_request_id'):continue
            if not matches(args.page_id,'page_id','client_page_id'):continue
            if not matches(args.operation_id,'operation_id'):continue
            if not matches(args.edge_id,'edge_id'):continue
            if not matches(args.connection_id,'connection_id'):continue
            records.append((stamp,event))
    if report is not None:
        records.append((instant(report['ts']), report))
    records.sort(key=lambda item:item[0]); total=len(records)
    if args.limit:records=records[-args.limit:]
    print(json.dumps({'kind':'query','since':since.isoformat(),'until':until.isoformat(),'matched':total,'emitted':len(records),'omitted_by_limit':total-len(records),'sources':sources,'timeline_basis':'client_ts_when_available' if args.client_time else 'server_ts',
        'report_id':args.report_id,'report_metadata_included':report is not None},ensure_ascii=False))
    for _,event in records: print(json.dumps(safe(event),ensure_ascii=False))
    for event in reports: print(json.dumps(safe(event),ensure_ascii=False))
    if 'windows_gateway' not in sources:
        print(json.dumps(source_event('windows_gateway','not_queried',reason='Not selected in this query; phone OS evidence is outside this reader.'),ensure_ascii=False))
    return 2 if any(r['source_status'] in {'unavailable','partial'} for r in reports) else 0

if __name__=='__main__':raise SystemExit(main())
