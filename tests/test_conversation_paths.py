import tempfile
import unittest
from pathlib import Path

from shared.conversation_paths import conversation_cwd


class ConversationCwdTests(unittest.TestCase):
    def test_accepts_existing_absolute_directory_without_file_browser_roots(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as root:
            directory = Path(root) / 'outside-file-roots'
            directory.mkdir()
            self.assertEqual(conversation_cwd(str(directory)), directory.resolve())

    def test_rejects_non_absolute_missing_and_non_directory_values(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as root:
            file = Path(root) / 'file'
            file.write_text('x')
            for value in ('relative', str(Path(root) / 'missing'), str(file), None):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        conversation_cwd(value)


if __name__ == '__main__':
    unittest.main()
