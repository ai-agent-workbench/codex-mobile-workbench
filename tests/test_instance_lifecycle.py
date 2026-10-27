"""Current readiness must not be inferred from old release metadata."""
import unittest
from unittest import mock
from shared import instance_lifecycle as lifecycle


class InstanceReadinessTests(unittest.TestCase):
    def check(self, state, counts=None, error=None):
        gate=mock.Mock()
        gate.read.return_value=state
        with mock.patch.object(lifecycle,'MaintenanceAdmission',return_value=gate), \
                mock.patch.object(lifecycle,'attest',side_effect=error,return_value={'busy':counts or {}}) as attest:
            value=lifecycle.ready('18080')
        return value,gate,attest

    def test_live_busy_native_is_ready_without_stopping_work(self):
        value,gate,attest=self.check({'mode':'open','owner':None},
            {'native_runtime_unknown':0,'native_inventory_unstable':0,'native_active_threads':1})
        self.assertEqual(value,{'ready':True})
        gate.change.assert_not_called()
        self.assertFalse(attest.call_args.kwargs['require_idle'])
        self.assertEqual(attest.call_args.kwargs['expected_mode'],'open')

    def test_unknown_native_or_missing_proof_is_not_ready(self):
        for counts in ({},{'native_runtime_unknown':1,'native_inventory_unstable':0},
                       {'native_runtime_unknown':0,'native_inventory_unstable':0,'native_inventory_unavailable':1}):
            with self.subTest(counts=counts):
                value,_,_=self.check({'mode':'open','owner':None},counts)
                self.assertEqual(value,{'ready':False})

    def test_closed_or_unknown_gate_is_never_reopened_by_readiness(self):
        for mode in ('quiescing','unavailable'):
            value,gate,attest=self.check({'mode':mode,'owner':'other-operation'})
            self.assertEqual(value,{'ready':False})
            gate.change.assert_not_called();gate._ensure_token.assert_not_called();attest.assert_not_called()

    def test_absent_or_wrong_server_attestation_does_not_claim_ready(self):
        value,gate,_=self.check({'mode':'open','owner':None},error=RuntimeError('wrong app or absent socket'))
        self.assertEqual(value,{'ready':False});gate.change.assert_not_called()
