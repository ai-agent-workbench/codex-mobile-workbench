"""Admission proof executed from the instance's verified application only."""
import json
from pathlib import Path
import sqlite3
import time

from shared.config import CONVERSATION_DB
from shared.maintenance_admission import MaintenanceAdmission, MODULE_SHA256, attest, busy_counts


def owner(instance_id):
    if not isinstance(instance_id, str) or len(instance_id) != 12 or any(c not in '0123456789abcdef' for c in instance_id):
        raise ValueError('invalid instance identity')
    return 'local-panel-' + instance_id


def drain(instance_id: str, port: str, web_state: str) -> dict:
    gate = MaintenanceAdmission(CONVERSATION_DB)
    previous = gate.read()
    identity = owner(instance_id)
    gate.change('quiescing', identity)
    try:
        deadline = time.monotonic() + 20
        while True:
            if web_state == 'active':
                data = attest(gate, identity, MODULE_SHA256,
                              base_url='http://127.0.0.1:' + str(int(port)), require_idle=False)
                counts = data['busy']
            else:
                counts = {}
                if CONVERSATION_DB.exists():
                    with sqlite3.connect(CONVERSATION_DB.absolute().as_uri() + '?mode=ro', uri=True) as connection:
                        connection.row_factory = sqlite3.Row
                        counts.update(busy_counts(connection))
                from shared.maintenance_native import standalone_counts
                counts.update(standalone_counts())
            if not any(counts.values()):
                return {'idle': True}
            if time.monotonic() >= deadline:
                break
            time.sleep(.5)
        if previous['mode'] == 'open':
            gate.change('open', identity)
        return {'idle': False, 'busy': counts}
    except Exception:
        # No service was stopped. Restore only the admission state this call
        # closed; unknown/another owner's state is never overwritten.
        if previous['mode'] == 'open':
            gate.change('open', identity)
        return {'idle': False, 'inventory': 'not_verified'}


def reopen(instance_id: str) -> dict:
    gate = MaintenanceAdmission(CONVERSATION_DB)
    if gate.read()['mode'] == 'quiescing':
        gate.change('open', owner(instance_id))
    elif gate.read()['mode'] != 'open':
        raise RuntimeError('unknown admission state')
    return {'admission': 'open'}


def ready(port: str) -> dict:
    """Prove THIS live app/database/native link, not historical healthy bytes.

    Reading readiness does not quiesce or reopen ingress and does not require
    existing tasks to be idle. The owner-only attestation is already shared
    with maintenance, not a new public health/control API.
    """
    gate = MaintenanceAdmission(CONVERSATION_DB)
    state = gate.read()
    if state['mode'] != 'open':
        return {'ready': False}
    try:
        gate._ensure_token()
        proof = attest(gate, state['owner'], MODULE_SHA256,
                       base_url='http://127.0.0.1:' + str(int(port)),
                       expected_mode='open', require_idle=False)
        busy = proof['busy']
        available = (busy.get('native_inventory_unavailable', 0) == 0
                     and busy.get('native_runtime_unknown', 1) == 0
                     and busy.get('native_inventory_unstable', 1) == 0)
        return {'ready': available}
    except Exception:
        return {'ready': False}
