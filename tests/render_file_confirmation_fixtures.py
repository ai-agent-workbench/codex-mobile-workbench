"""Render real file confirmation routes using task-owned files and fake auth."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_file_operation_safety import FileOperationSafetyTests, Upload
from routes import files


fixture = FileOperationSafetyTests()
fixture.setUp()
try:
    source, target = fixture.file("来源.txt"), fixture.file("报告.txt", b"old")
    copy = fixture.prepared_transfer(source, target)
    move = files.move_confirm(fixture.request, str(source), str(fixture.root), target.name)
    move = files.move_execute(fixture.request, fixture.token(move))
    upload = files.upload(fixture.request, str(fixture.root), [Upload("报告.txt")], "/files")
    duplicate = files.upload(fixture.request, str(fixture.root), [Upload("重复.txt"), Upload("重复.txt")], "/files")
    edit = files.edit_confirm(fixture.request, str(target), "edited fixture")
    print(json.dumps({key: response.body.decode() for key, response in (
        ("copy", copy), ("move", move), ("upload", upload),
        ("duplicate", duplicate), ("edit", edit),
    )}, ensure_ascii=False))
finally:
    fixture.doCleanups()
