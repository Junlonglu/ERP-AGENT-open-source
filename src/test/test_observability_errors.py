"""Tracing failures must be reported, and must never break the request path.

Every ``except`` in :mod:`src.agent.observability` exists so that observability can
never take down a request. That tradeoff is correct; what was wrong is that the
failure was *invisible* -- a langfuse upgrade turned the module into zero spans
without a single line in the log.
"""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

from src.agent import observability as o


class Span:
    trace_id = 'a' * 32
    id = 'b' * 16

    def __init__(self, fail_update_after=None):
        self.fail_update_after = fail_update_after
        self.updates = 0

    def update(self, **kw):
        self.updates += 1
        if self.fail_update_after is not None and self.updates >= self.fail_update_after:
            raise RuntimeError('span update boom')

    def end(self):
        pass


class Client:
    def __init__(self, fail_after=None, fail_update_after=None):
        self.fail_after = fail_after
        self.fail_update_after = fail_update_after
        self.calls = 0

    def start_observation(self, **kw):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError('span start boom')
        return Span(fail_update_after=self.fail_update_after)


class SuppressionTests(unittest.TestCase):
    def setUp(self):
        o._suppressed.clear()
        o._client = None
        self.addCleanup(o._suppressed.clear)
        self.addCleanup(setattr, o, '_client', None)

    def test_disabled_tracing_is_silent(self):
        with patch.dict(os.environ, {'ERP_LANGFUSE_ENABLED': 'false'}):
            self.assertIsNone(o.client())
        self.assertEqual(o.suppressed_errors(), {})

    def test_enabled_without_credentials_is_reported(self):
        env = {'ERP_LANGFUSE_ENABLED': 'true'}
        for key in ('LANGFUSE_SECRET_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_BASE_URL', 'ERP_TRACE_HASH_KEY'):
            env[key] = ''
        with patch.dict(os.environ, env), patch.object(o._logger, 'warning') as warn:
            self.assertIsNone(o.client())
            self.assertIsNone(o.client())
        self.assertEqual(o.suppressed_errors(), {'langfuse.config': 2})
        self.assertEqual(warn.call_count, 1, 'repeat failures must be counted, not re-logged')
        message = str(warn.call_args)
        self.assertIn('langfuse.config', message)
        self.assertIn('LANGFUSE_SECRET_KEY', message)

    def test_root_span_failure_is_reported(self):
        # A langfuse build without propagate_attributes is exactly the upgrade that
        # used to produce silent zero-span traces.
        stub = types.ModuleType('langfuse')
        with patch.dict(sys.modules, {'langfuse': stub}), \
                patch.object(o, 'client', return_value=Client()), \
                patch.dict(os.environ, {'ERP_TRACE_HASH_KEY': 'test-key'}), \
                patch.object(o._logger, 'warning') as warn:
            trace = o.Trace('user', 'thread')
        self.assertIsNone(trace.root)
        self.assertEqual(o.suppressed_errors(), {'langfuse.root_span': 1})
        self.assertEqual(warn.call_args[0][1], 'ImportError')
        self.assertEqual(warn.call_args[0][2], 'langfuse.root_span')
        self.assertEqual(warn.call_args[1]['exc_info'].__class__.__name__, 'ImportError')

    def test_span_failures_do_not_escape(self):
        with patch.object(o, 'client', return_value=Client(fail_after=1)), \
                patch.dict(os.environ, {'ERP_TRACE_HASH_KEY': 'test-key'}), \
                patch.object(o._logger, 'warning'):
            trace = o.Trace('user', 'thread')
            trace.event('step', duration_ms=1)
            trace.event('step', duration_ms=2)
            trace.finish('completed')
        self.assertEqual(o.suppressed_errors(), {'langfuse.span_start': 2})

    def test_finish_failure_is_reported(self):
        # The root span accepts its initial metadata write, then fails on finish.
        with patch.object(o, 'client', return_value=Client(fail_update_after=2)), \
                patch.dict(os.environ, {'ERP_TRACE_HASH_KEY': 'test-key'}), \
                patch.object(o._logger, 'warning'):
            trace = o.Trace('user', 'thread')
            trace.finish('completed')
        self.assertEqual(o.suppressed_errors(), {'langfuse.finish': 1})

    def test_counters_survive_a_broken_logger(self):
        broken = Mock()
        broken.warning.side_effect = RuntimeError('logger boom')
        with patch.object(o, '_logger', broken):
            o._suppressed_error('langfuse.event', RuntimeError('original'))
            o._suppressed_error('langfuse.event', RuntimeError('original'))
        self.assertEqual(o.suppressed_errors(), {'langfuse.event': 2})


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        o._suppressed.clear()
        o._client = None
        self.addCleanup(o._suppressed.clear)
        self.addCleanup(setattr, o, '_client', None)

    def test_shutdown_flush_failure_is_reported_and_summarised(self):
        flaky = Mock()
        flaky.flush.side_effect = RuntimeError('flush boom')
        with patch.object(o, '_logger', Mock()) as logger:
            o._client = flaky
            asyncio.run(o.shutdown())
        self.assertEqual(o.suppressed_errors(), {'langfuse.flush': 1})
        summary = [call for call in logger.warning.call_args_list if 'tracing error(s)' in str(call)]
        self.assertEqual(len(summary), 1)
        self.assertIn('langfuse.flush=1', str(summary[0]))

    def test_shutdown_without_failures_stays_quiet(self):
        with patch.object(o, '_logger', Mock()) as logger:
            asyncio.run(o.shutdown())
        logger.warning.assert_not_called()


if __name__ == '__main__':
    unittest.main()
