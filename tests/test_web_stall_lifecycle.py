"""Web lifecycle wiring without launching threads or touching production state."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import app as web
from shared import diagnostics
from shared.web_stall_monitor import WebStallMonitor


class WebStallLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = web.app.state._state.pop('web_stall_monitor', None)
        self.addCleanup(self.restore)

    def restore(self):
        web.app.state._state.pop('web_stall_monitor', None)
        if self.previous is not None:
            web.app.state.web_stall_monitor = self.previous

    async def test_last_startup_hook_starts_once_with_configured_context(self):
        self.assertIs(web.app.router.on_startup[-1], web.startup_stall_monitor)
        with tempfile.TemporaryDirectory() as directory:
            writer = diagnostics.DiagnosticsWriter(Path(directory))
            monitor = Mock(spec=WebStallMonitor)
            monitor.start.return_value = True
            monitor.status.return_value = {'enabled': True}
            with patch.dict('os.environ', {'CODEX_WEB_STALL_MONITOR': '1'}), \
                 patch.object(diagnostics, 'get_writer', return_value=writer), \
                 patch.object(diagnostics, 'current_release', return_value='test-release'), \
                 patch.object(diagnostics, 'emit') as emit, \
                 patch('shared.web_stall_monitor.WebStallMonitor', return_value=monitor) as factory:
                await web.startup_stall_monitor()
                await web.startup_stall_monitor()
                factory.assert_called_once_with(Path(directory), release='test-release')
                monitor.start.assert_called_once_with(asyncio.get_running_loop())
                emit.assert_called_once_with('web_stall_monitor_started', enabled=True)
                web.stop_stall_monitor()
                web.stop_stall_monitor()
                monitor.stop.assert_called_once()

    async def test_disabled_or_invalid_configuration_never_creates_a_monitor(self):
        for value in ('0', 'false', 'unexpected-private-value'):
            with self.subTest(value=value), patch.dict('os.environ', {'CODEX_WEB_STALL_MONITOR': value}), \
                 patch.object(diagnostics, 'emit') as emit, \
                 patch('shared.web_stall_monitor.WebStallMonitor') as factory:
                await web.startup_stall_monitor()
                factory.assert_not_called()
                self.assertEqual(emit.call_args.args, ('web_stall_monitor_disabled',))
                self.assertNotIn(value, str(emit.call_args.kwargs))

    async def test_failed_start_and_failed_cleanup_do_not_fail_web_startup(self):
        monitor = Mock(spec=WebStallMonitor)
        monitor.start.side_effect = RuntimeError('private startup details')
        monitor.stop.side_effect = RuntimeError('private cleanup details')
        writer = Mock(directory=Path('/unused'))
        with patch.dict('os.environ', {'CODEX_WEB_STALL_MONITOR': '1'}), \
             patch.object(diagnostics, 'get_writer', return_value=writer), \
             patch.object(diagnostics, 'emit') as emit, \
             patch('shared.web_stall_monitor.WebStallMonitor', return_value=monitor):
            await web.startup_stall_monitor()
            self.assertIsNone(web.app.state.web_stall_monitor)
            emit.assert_called_once_with('web_stall_monitor_disabled', level='warning', reason='start_error')

    async def test_monitor_stops_before_normal_service_shutdown(self):
        order = []
        monitor = Mock(spec=WebStallMonitor)
        monitor.stop.side_effect = lambda: order.append('monitor')
        web.app.state.web_stall_monitor = monitor
        with patch.object(diagnostics, 'emit', side_effect=lambda *a, **k: order.append('stopping')), \
             patch('engine.dot_notifications.shutdown_dot_notifications', new=AsyncMock(side_effect=lambda: order.append('dot'))), \
             patch('engine.conversation_service.shutdown_conversation_service', new=AsyncMock(side_effect=lambda: order.append('conversation'))):
            await web.shutdown_conversations()
        self.assertEqual(order, ['monitor', 'stopping', 'dot', 'conversation'])


if __name__ == '__main__':
    unittest.main()
