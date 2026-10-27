"""New conversations follow advertised Sol; existing choices are preserved."""
import unittest
from types import SimpleNamespace
from unittest import mock

from routes import conversation_api, conversations
from shared.conversation_catalog import (
    catalog_models, new_conversation_model, resolve_new_conversation_model,
)


def catalog(*names):
    return {'models': [{'model': name} for name in names]}


class NewModelDefaultTests(unittest.IsolatedAsyncioTestCase):
    def test_preferred_and_rollback_catalogs(self):
        self.assertEqual(new_conversation_model(catalog('gpt-6-astra', 'gpt-5.6-sol')), 'gpt-5.6-sol')
        self.assertEqual(new_conversation_model(catalog('gpt-5.6-sol', 'gpt-6-sol')), 'gpt-6-sol')
        self.assertEqual(new_conversation_model(catalog('gpt-6.1-sol', 'gpt-6-sol')), 'gpt-6-sol')
        self.assertEqual(new_conversation_model({}), 'gpt-5.6-sol')
        settings = {'model': 'gpt-5.6-sol', 'effort': 'medium'}
        values = catalog_models(catalog('gpt-5.6-sol', 'gpt-6-sol'), settings)
        self.assertEqual(settings['model'], 'gpt-5.6-sol')
        self.assertIn('gpt-5.6-sol', [row['model'] for row in values])

    async def test_catalog_outage_keeps_legacy_default(self):
        service = mock.Mock(catalog=mock.AsyncMock(side_effect=OSError('offline')))
        self.assertEqual(await resolve_new_conversation_model(service, None), 'gpt-5.6-sol')

    async def test_new_entrypoints_use_catalog_but_never_replace_explicit_choice(self):
        for advertised, expected in [('gpt-6-sol', 'gpt-6-sol'), ('gpt-5.6-sol', 'gpt-5.6-sol')]:
            for entry in ('quick', 'draft', 'start'):
                for explicit in ('', 'gpt-5.6-luna') if entry != 'quick' else ('',):
                    with self.subTest(advertised=advertised, entry=entry, explicit=explicit):
                        service = mock.MagicMock()
                        service.catalog = mock.AsyncMock(return_value=catalog(advertised))
                        service.create_draft.return_value = {'id': 'default-test'}
                        service.public_snapshot.return_value = {'conversation': {}}
                        service.start = mock.AsyncMock(return_value={'command': {'state': 'accepted'}, 'snapshot': {}})
                        module = conversation_api if entry == 'draft' else conversations
                        auth = '_auth' if entry == 'draft' else 'require_auth'
                        request = SimpleNamespace(cookies={})
                        with mock.patch.object(module, auth, return_value=None), mock.patch.object(module, 'conversation_service', return_value=service), mock.patch.object(module, 'classify_client_origin', return_value=('mobile_web', {}, {})), mock.patch.object(module, 'logged_in', return_value='owner'), mock.patch.object(module, 'audit'):
                            if entry == 'quick':
                                response = await module.quick_conversation(request)
                            else:
                                params = dict(project_id='', model=explicit, effort='medium', permission='read-only', service_tier='', completion_notification=None)
                                if entry == 'start':
                                    response = await module.create_and_start_conversation(request, prompt='hello', skills=[], **params)
                                else:
                                    response = await module.create_conversation_draft(request, **params)
                        self.assertIn(response.status_code, (201, 303))
                        self.assertEqual(service.create_draft.call_args.kwargs['model'], explicit or expected)
                        if entry == 'quick':
                            self.assertEqual(service.create_draft.call_args.kwargs['effort'], 'high')
                        else:
                            self.assertEqual(service.create_draft.call_args.kwargs['effort'], 'medium')
                        if explicit:
                            service.catalog.assert_not_called()
                        else:
                            service.catalog.assert_awaited_once()
