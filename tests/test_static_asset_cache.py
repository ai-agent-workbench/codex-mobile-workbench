import unittest
from pathlib import Path
from types import SimpleNamespace

from routes import misc
from shared.html_lib import static_url


ROOT = Path(__file__).resolve().parent.parent


class StaticAssetCacheTests(unittest.TestCase):
    def response(self, version=None):
        query = {} if version is None else {"v": version}
        return misc.static_asset(
            "css/app.css", SimpleNamespace(query_params=query)
        )

    def test_template_version_is_immutable(self):
        url = static_url("/static/css/app.css")
        version = url.partition("?v=")[2]
        expected = str(int((ROOT / "static/css/app.css").stat().st_mtime))

        self.assertEqual(version, expected)
        self.assertEqual(
            self.response(version).headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    def test_unversioned_asset_must_revalidate(self):
        self.assertEqual(
            self.response().headers["cache-control"], "no-cache"
        )

    def test_stale_version_must_revalidate(self):
        self.assertEqual(
            self.response("stale").headers["cache-control"], "no-cache"
        )


if __name__ == "__main__":
    unittest.main()
