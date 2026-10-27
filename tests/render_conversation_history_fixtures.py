"""Synthetic paged API payloads from the real store/service security projection."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_conversation_browser_view import BrowserViewTests, seed_history
from tests.store_fixtures import seeded_store


def read_pages(service):
    pages, cursor = [], ""
    while True:
        page = service.public_browser_view("history-fixture", before=cursor)
        pages.append(page)
        cursor = page["history"]["cursor"]
        if not cursor:
            return pages


def main():
    fixture = BrowserViewTests()
    fixture.setUp()
    try:
        service, store = fixture.service, fixture.store
        pages = read_pages(service)
        tokens = [page["history"]["token"] for page in pages]
        initial = service.public_snapshot("history-fixture")
        store._db.execute("UPDATE conversations SET active_turn_id='turn-65',state='active',revision=revision+1 WHERE id='history-fixture'")
        race_older = service.public_browser_view("history-fixture", before=pages[0]["history"]["cursor"])
        store._db.execute("UPDATE items SET text=text||' 尾部增量',revision=revision+1 WHERE turn_id='turn-65'")
        store._db.execute("UPDATE conversations SET revision=revision+1 WHERE id='history-fixture'")
        tail = service.public_browser_view("history-fixture", pages=tokens)
        store._db.execute("UPDATE items SET text=text||' 旧页修正',revision=revision+1 WHERE turn_id='turn-30'")
        store._db.execute("UPDATE conversations SET revision=revision+1 WHERE id='history-fixture'")
        corrected = service.public_browser_view("history-fixture", pages=tokens + [tail["history"]["token"]])
        full = service.public_snapshot("history-fixture")
        # A fresh store instance with more turns simulates a reconnect after a
        # long absence; old ranges are explicitly invalidated and remain reachable.
        fixture.store.close()
        fixture.store = seeded_store(fixture.path / "gap.sqlite3")
        service.store = fixture.store
        seed_history(fixture.store, 100)
        fixture.store._db.execute("UPDATE conversations SET revision=10 WHERE id='history-fixture'")
        gap_pages = read_pages(service)
        all_tokens = tokens + [tail["history"]["token"], corrected["history"]["token"]]
        all_tokens += [replacement["token"] for replacement in corrected["history"]["replacements"]]
        gap = service.public_browser_view("history-fixture", pages=all_tokens)
        print(json.dumps({"initial": initial, "pages": pages, "race_older": race_older, "tail": tail,
            "corrected": corrected, "full": full, "gap": gap, "gap_pages": gap_pages,
            "gap_full": service.public_snapshot("history-fixture")}, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
