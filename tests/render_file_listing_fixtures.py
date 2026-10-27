"""Real read-only listing HTML over task-owned files; no production service."""
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_file_operation_safety import FileOperationSafetyTests
from routes import files

fixture = FileOperationSafetyTests()
fixture.setUp()
try:
    for number in range(505):
        fixture.file(f"文件-{number:04d}.txt")
    pages = {}
    start = files._files_url(fixture.root)
    address = start
    while True:
        query = parse_qs(urlsplit(address).query)
        response = fixture.list_files(cursor=query.get("cursor", [""])[0])
        if response.status_code != 200:
            raise AssertionError("fixture listing failed")
        pages[address] = response.body.decode()
        next_link = re.search(r"rel='next' href='([^']+)'", pages[address])
        if not next_link:
            break
        address = html.unescape(next_link[1])
    search = files._files_url(fixture.root, "文件-0504")
    pages[search] = fixture.list_files(search="文件-0504").body.decode()
    print(json.dumps({"start": start, "pages": pages}, ensure_ascii=False))
finally:
    fixture.doCleanups()
