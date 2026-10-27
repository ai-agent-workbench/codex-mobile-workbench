"""Synthetic read-only listing benchmark (no live filesystem samples)."""
import json
import statistics
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from tests.test_file_operation_safety import FileOperationSafetyTests
from routes import files
report = []
for count in (505, 5000):
    fixture = FileOperationSafetyTests(); fixture.setUp()
    try:
        directory = fixture.root / 'listing'; directory.mkdir()
        for index in range(count): fixture.file(f'listing/file-{index:05d}.txt')
        scanner = getattr(files, '_visible_file_entries', None)
        def measure(call):
            call(); values = []
            for _ in range(7):
                start = time.perf_counter(); call(); values.append((time.perf_counter() - start) * 1000)
            return {'median_ms': round(statistics.median(values), 2), 'p95_ms': round(max(values), 2), 'samples': values}
        from unittest.mock import patch
        def render():
            with patch.object(files, 'FILE_ROOTS', (fixture.root,)):
                return files.files(fixture.request, str(directory))
        response = render()
        report.append({'count': count, 'scan': measure(lambda: scanner(directory, '')) if scanner else None,
                       'route': measure(render), 'html_bytes': len(response.body),
                       'rendered_entries': response.body.count(b"class='file-entry'")})
    finally: fixture.doCleanups()
print(json.dumps({'source': str(root), 'warmup': 1, 'samples': 7, 'rows': report}, indent=2))
