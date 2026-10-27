import contextlib
import datetime as dt
import io
import json
import gzip
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared import diagnostics_read as reader

class DiagnosticsReadTests(unittest.TestCase):
    def test_bounded_stall_evidence_is_in_the_existing_diagnostics_timeline(self):
        from shared.web_stall_monitor import _StallWriter
        with tempfile.TemporaryDirectory() as directory:
            writer=_StallWriter(directory,max_file_bytes=256)
            for i in range(6):
                self.assertTrue(writer.write({'ts':f'2026-10-20T01:00:0{i}Z','kind':'web_stall_snapshot',
                                              'release':'fixture-release','process_id':42}))
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                code=reader.main(['--sources','diagnostics','--diagnostics-dir',directory,
                                  '--since','2026-10-20T01:00:00Z','--until','2026-10-20T01:01:00Z','--limit','0'])
            self.assertEqual(code,0)
            rows=[json.loads(line) for line in out.getvalue().splitlines()]
            self.assertTrue(any(row.get('kind')=='web_stall_snapshot' for row in rows))
            files={row['file'] for row in rows if row.get('source_status')=='read'}
            self.assertEqual(files,{'web-stalls/stalls.jsonl','web-stalls/stalls.jsonl.1','web-stalls/stalls.jsonl.2'})

    def test_optional_stall_directory_and_files_never_follow_links(self):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as outside:
            root=Path(directory)
            (root/'events.jsonl').write_text('{"ts":"2026-10-20T01:00:00Z","kind":"normal"}\n')
            (Path(outside)/'stalls.jsonl').write_text('{"ts":"2026-10-20T01:00:00Z","kind":"never-read"}\n')
            (root/'web-stalls').symlink_to(outside,target_is_directory=True)
            rows=list(reader.local_events(root))
            self.assertFalse(any(row.get('kind')=='never-read' for row in rows))
            self.assertTrue(any(row.get('file')=='web-stalls' and row.get('source_status')=='unavailable' for row in rows))
            (root/'web-stalls').unlink()
            (root/'web-stalls').mkdir()
            (root/'web-stalls/stalls.jsonl').symlink_to(Path(outside)/'stalls.jsonl')
            rows=list(reader.local_events(root))
            self.assertFalse(any(row.get('kind')=='never-read' for row in rows))
            self.assertTrue(any(row.get('file')=='web-stalls/stalls.jsonl' and row.get('source_status')=='unavailable' for row in rows))

    def test_truncated_compressed_source_does_not_abort_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'events.jsonl.gz').write_bytes(gzip.compress(b'{"ts":"2026-09-30T01:00:00Z"}\n')[:-7])
            (root/'errors.jsonl').write_text('{"ts":"2026-09-30T01:00:01Z","kind":"still-readable"}\n')
            rows=list(reader.local_events(root))
            self.assertTrue(any(e.get('kind')=='still-readable' for e in rows))
            self.assertTrue(any(e.get('source_status')=='unavailable' for e in rows))
    def test_native_gateway_json_precise_time_rotation_and_id_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            row={'schema':'workbench-gateway-v1','ts':'2026-09-30T10:30:19+08:00',
                 'end_msec':'1790735419.123','edge_id':'edge-test-123','request_id':'app-test-123',
                 'operation_id':'op-test-123','connection_id':'101','tls_session_reused':'r',
                 'url':'/projects/history?page=2'}
            p=Path(directory)/'codex-web-trace.log.1.gz'
            with gzip.open(p,'wt') as f:f.write(json.dumps(row)+'\n')
            now=dt.datetime(2026,9,30,tzinfo=dt.timezone.utc)
            rows=list(reader.nginx_events(now,Path(directory)))
            event=next(e for e in rows if e.get('schema'))
            self.assertEqual(reader.instant(event['ts']).microsecond,123000)
            self.assertEqual(event['tls_session_reused'],'r')
            out=io.StringIO()
            with patch.object(reader,'nginx_events',return_value=iter(rows)),contextlib.redirect_stdout(out):
                code=reader.main(['--sources','nginx','--since','2026-09-30T00:00:00Z','--until','2026-10-01T00:00:00Z',
                                  '--request-id','app-test-123','--edge-id','edge-test-123','--connection-id','101','--operation-id','op-test-123'])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(out.getvalue().splitlines()[0])['matched'],1)

    def test_late_browser_replay_can_query_client_clock_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'events.jsonl.gz'
            row={'ts':'2026-10-01T01:00:00Z','client_ts':'2026-09-30T01:00:00Z','kind':'browser'}
            with gzip.open(p,'wt') as f:f.write(json.dumps(row)+'\n')
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                code=reader.main(['--sources','diagnostics','--diagnostics-dir',directory,'--client-time',
                                  '--since','2026-09-30T00:00:00Z','--until','2026-09-30T02:00:00Z'])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(out.getvalue().splitlines()[0])['matched'],1)

    def test_window_filter_and_explicit_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'events.jsonl').write_text('\n'.join(json.dumps({'ts':f'2026-09-30T01:00:0{i}Z','request_id':'a'*32,'kind':'request_end'}) for i in range(3)))
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                code=reader.main(['--diagnostics-dir',directory,'--sources','diagnostics','--since','2026-09-30T01:00:00Z','--until','2026-09-30T01:00:10Z','--limit','1','--request-id','a'*32])
            self.assertEqual(code,0)
            lines=[json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(lines[0]['omitted_by_limit'],2)
            self.assertTrue(any(line.get('request_id')=='a'*32 for line in lines))
    def test_journal_uses_existing_sudo_and_reports_failure(self):
        now=dt.datetime.now(dt.timezone.utc)
        with patch.object(reader.os,'geteuid',return_value=1000), patch.object(reader.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','sudo: no new privileges')) as run:
            result=list(reader.journal_events(now,now))
            self.assertEqual(run.call_args.args[0][:3],['sudo','-n','journalctl'])
            self.assertEqual(result[0]['source_status'],'unavailable')
    def test_corrupt_file_is_partial_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'events.jsonl').write_text('not-json\n')
            result=list(reader.local_events(Path(directory)))
            self.assertTrue(any(e.get('source_status')=='partial' for e in result))
    def test_nginx_timezones_and_secret_scrub(self):
        stamp=reader.nginx_stamp('local - - [30/Sep/2026:10:30:19 +0800] "GET / HTTP/1.1" 200')
        self.assertEqual(stamp.hour,2)
        value=reader.safe('GET /'+'a'*40+'/home?token=SECRETVALUE HTTP/1.1')
        self.assertNotIn('SECRETVALUE',value)
        self.assertNotIn('a'*40,value)
    def test_source_missing_is_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reader.main(['--sources','diagnostics','--diagnostics-dir',directory]),2)

    def test_gateway_with_no_recent_requests_still_reports_source(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'codex-web-access.log').write_text('')
            result=list(reader.nginx_events(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1),Path(directory)))
            self.assertEqual(result[-1]['source_status'],'read')
            self.assertEqual(result[-1]['files_in_window'],0)

    def test_windows_discovery_failure_is_visible(self):
        with patch.dict(reader.os.environ,{},clear=True), patch.object(reader.subprocess,'run',side_effect=OSError('interop unavailable')):
            result=list(reader.windows_events(dt.datetime.now(dt.timezone.utc)))
            self.assertEqual(result[0]['source'],'windows_gateway')
            self.assertEqual(result[0]['source_status'],'unavailable')

    def test_audit_timestamp_and_corruption_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'audit.jsonl'
            p.write_text('{"time":"2026-09-30T01:00:00Z","event":"login_failed"}\ninvalid\n')
            values=list(reader.audit_events(p))
            self.assertEqual(values[0]['source'],'audit')
            self.assertEqual(values[0]['ts'],'2026-09-30T01:00:00Z')
            self.assertEqual(values[-1]['source_status'],'partial')

class FaultReportReaderTests(unittest.TestCase):
    def test_report_id_attaches_private_receipt_and_bounded_timeline_not_screenshot(self):
        import shared.diagnostic_reports as reports
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            fields=reports.validate_fields({'category':'page_crashed','fault_time':dt.datetime.now(dt.timezone.utc).isoformat(),
                'client_page_id':'fixture-page-id','source_path':'/home'})
            with patch.object(reports,'STATE',root):
                reports.persist_report('a'*32,'private-owner',fields,None,reports.image_info(None,None))
            diagnostics=root/'diagnostics'
            (diagnostics/'events.jsonl').write_text(json.dumps({'ts':fields['fault_time'],'kind':'browser','page_id':'fixture-page-id'})+'\n')
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                code=reader.main(['--sources','diagnostics','--diagnostics-dir',str(diagnostics),'--report-id','a'*32,'--limit','0'])
            self.assertEqual(code,0)
            rows=[json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(rows[0]['matched'],2)
            self.assertTrue(rows[0]['report_metadata_included'])
            self.assertEqual((reader.instant(rows[0]['until'])-reader.instant(rows[0]['since'])).total_seconds(),600)
            receipt=next(row for row in rows if row.get('kind')=='fault_report_context')
            self.assertEqual(receipt['client_page_id'],'fixture-page-id')
            self.assertNotIn('owner',receipt)
            self.assertNotIn('private-owner',out.getvalue())

    def test_unknown_or_linked_report_is_not_silently_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(reader.main(['--sources','diagnostics','--diagnostics-dir',directory,'--report-id','b'*32]),2)
            self.assertIn('unavailable',out.getvalue())
