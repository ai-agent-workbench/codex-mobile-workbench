"""Synthetic history navigation: no dot/native data mixing or production I/O."""
import json
import unittest
from unittest import mock
from contextlib import ExitStack
from html.parser import HTMLParser
from fastapi.responses import RedirectResponse
import projects
from routes import home, dot_conversations


class EntryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.entry_tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'section':
            if 'dot-history-entry' in attrs.get('class', ''):
                self.depth = 1
            elif self.depth:
                self.depth += 1
        if self.depth:
            self.entry_tags.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == 'section' and self.depth:
            self.depth -= 1


class DotNavigationFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.service = mock.Mock()
        self.service.list_public.return_value = []
        self.service.list_public_page.return_value = {
            'values': [], 'next_cursor': None, 'order_version': 'synthetic-order'}
        self.service.search_public_page.return_value = {'values': [], 'next_cursor': None}
        self.service.public_index_by_ids.return_value = []
        self.service.public_order_version.return_value = 'synthetic-order'
        for target, value in [(home, 'require_auth'), (home, 'conversation_service'),
                              (projects, 'recent')]:
            result = None if value == 'require_auth' else self.service if value == 'conversation_service' else []
            self.stack.enter_context(mock.patch.object(target, value, return_value=result))
        self.owner = self.stack.enter_context(mock.patch.object(home, 'logged_in', return_value='synthetic-owner'))
        self.stack.enter_context(mock.patch.object(dot_conversations, 'DOT_CONVERSATION_ENABLED', True))
        self.stack.enter_context(mock.patch.object(dot_conversations, 'DOT_OWNER_ACCOUNT_ID', 'synthetic-owner'))
        self.stack.enter_context(mock.patch.object(dot_conversations, 'DOT_PARENT_REF', 'synthetic-parent'))
        self.mailbox = self.stack.enter_context(mock.patch.object(dot_conversations, 'dot_service', side_effect=AssertionError('navigation must not open mailbox')))


class DotEntryHiddenTests(DotNavigationFixture):
    def test_home_and_history_hide_entries_even_when_dot_is_available(self):
        self.assertFalse(home._DOT_ENTRY_VISIBLE)
        with mock.patch.object(home, 'dot_available', return_value=True) as available:
            for response in (home.home(object()), home.history(object()),
                             home.history(object(), q='synthetic-search')):
                body = response.body.decode()
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("href='/settings/dot'", body)
                self.assertNotIn('dot-history-entry', body)
                self.assertNotIn('data-dot-history-card', body)
                self.assertNotIn('/static/dot-history.js', body)
                self.assertIn("href='/quick'", body)
            available.assert_not_called()
        self.mailbox.assert_not_called()
        self.service.set_pinned.assert_not_called()
        self.service.delete_many.assert_not_called()


class DotHistoryTests(DotNavigationFixture):
    def setUp(self):
        super().setUp()
        # Preserve explicit opt-in coverage for a future navigation restore.
        self.stack.enter_context(mock.patch.object(home, '_DOT_ENTRY_VISIBLE', True))

    def test_owner_gets_fixed_entry_outside_native_list_and_management(self):
        response = home.history(object())
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("href='/settings/dot'", body)
        self.assertLess(body.index('dot-history-entry'), body.index("data-status-endpoint="))
        parser = EntryParser(); parser.feed(body)
        self.assertTrue(parser.entry_tags)
        self.assertFalse(any(tag == 'input' or 'data-id' in attrs or 'data-status-endpoint' in attrs
                             or 'manage-card' in attrs.get('class', '') for tag, attrs in parser.entry_tags))
        self.mailbox.assert_not_called()
        self.service.set_pinned.assert_not_called()
        self.service.delete_many.assert_not_called()
        self.service.list_public_page.assert_called_once_with(account_id='synthetic-owner')

    def test_disabled_and_other_owner_have_no_entry(self):
        self.owner.return_value = 'synthetic-other'
        self.assertNotIn('dot-history-entry', home.history(object()).body.decode())
        self.owner.return_value = 'synthetic-owner'
        with mock.patch.object(dot_conversations, 'DOT_CONVERSATION_ENABLED', False):
            self.assertNotIn('dot-history-entry', home.history(object()).body.decode())

    def test_pagination_and_status_remain_native_only(self):
        for response in (home.history_page(object(), cursor=None),
                         home.history_status(object(), ids=[], check_order=True)):
            self.assertNotIn('dot-history-entry', json.loads(response.body)['html'])
        self.mailbox.assert_not_called()

    def test_login_denied_does_not_inspect_availability(self):
        with mock.patch.object(home, 'require_auth', return_value=RedirectResponse('/login', 303)), \
             mock.patch.object(home, 'dot_available', side_effect=AssertionError('auth first')):
            self.assertEqual(home.history(object()).status_code, 303)
        self.service.list_public_page.assert_not_called()
