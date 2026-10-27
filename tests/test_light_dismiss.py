"""Verify that shared light dismissal is explicit, not applied to every form."""
import json
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

from shared.html_lib import templates, upload_panel

ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def by_id(self, element_id):
        return next(attrs for _, attrs in self.elements if attrs.get('id') == element_id)


class LightDismissWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(subprocess.check_output(
            [sys.executable, str(ROOT / 'tests/render_conversation_ui_fixtures.py')],
            cwd=ROOT, text=True,
        ))

    def test_base_loads_versioned_shared_module_once(self):
        page = templates.get_template('base.html').render(title='fixture', content='')
        scripts = [attrs for tag, attrs in Elements(page).elements if tag == 'script']
        module = [attrs for attrs in scripts if attrs.get('src', '').startswith('/static/light-dismiss.js?v=')]
        self.assertEqual(len(module), 1)
        self.assertIn('defer', module[0])

    def test_only_target_conversation_overlays_opt_in(self):
        page = Elements(self.fixtures['conversation'])
        for element_id in ('conversation-diagnostics', 'conversation-context',
                           'conversation-export-dialog', 'conversation-search-dialog',
                           'conversation-audio-settings-dialog'):
            self.assertEqual(page.by_id(element_id).get('data-light-dismiss'), 'dialog')
        self.assertEqual(page.by_id('conversation-settings').get('data-light-dismiss'), 'details')
        self.assertEqual(page.by_id('conversation-upload').get('data-light-dismiss'), 'empty-upload')
        self.assertNotIn('data-light-dismiss', page.by_id('conversation-artifact-viewer'))
        for tag, attrs in page.elements:
            if tag == 'details' and 'skill-advanced' in attrs.get('class', ''):
                self.assertNotIn('data-light-dismiss', attrs)
            if tag == 'details' and 'native-context-details' in attrs.get('class', ''):
                self.assertNotIn('data-light-dismiss', attrs)

    def test_shared_uploads_opt_in_with_file_selection_guard(self):
        for dialog_id in ('upload-dialog', 'project-upload', 'conversation-upload'):
            page = Elements(upload_panel('/tmp/fixture', '/home', dialog_id))
            self.assertEqual(page.by_id(dialog_id).get('data-light-dismiss'), 'empty-upload')
        self.assertEqual(Elements(self.fixtures['project']).by_id('project-upload').get('data-light-dismiss'), 'empty-upload')

    def test_settings_confirmation_and_reading_sections_are_not_opted_in(self):
        page = Elements(templates.get_template('settings_center.html').render(nav_html=''))
        self.assertNotIn('data-light-dismiss', page.by_id('sc-dialog'))
        for tag, attrs in page.elements:
            if tag == 'details':
                self.assertNotIn('data-light-dismiss', attrs)


if __name__ == '__main__':
    unittest.main()
