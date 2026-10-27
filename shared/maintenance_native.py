"""Read-only native drain inventory, including agents outside local projections.

Only loaded/list, metadata read, goal get and background-terminal list are used.
Never resume, interrupt, clean terminals, pause goals, load archived history,
or take a SQLite writer while doing RPC.
"""
import asyncio


async def busy_counts(client):
    async with asyncio.timeout(4):
        before = set(await client.list_loaded_thread_ids())
        counts = {'native_active_threads': 0, 'native_active_goals': 0,
                  'native_background_terminals': 0,
                  'native_inventory_unstable': 0, 'native_runtime_unknown': 0}
        for thread_id in sorted(before):
            value = await client.read_thread(thread_id, include_turns=False)
            thread = value.get('thread') if isinstance(value, dict) else None
            if not isinstance(thread, dict) or thread.get('id') != thread_id:
                raise RuntimeError('native drain inventory returned another thread')
            state = thread.get('status', {}).get('type')
            if state == 'notLoaded':
                continue
            if state == 'active':
                counts['native_active_threads'] += 1
            elif state != 'idle':
                counts['native_runtime_unknown'] += 1
            goal = await client.get_thread_goal(thread_id)
            if goal is not None and goal.get('status') == 'active':
                counts['native_active_goals'] += 1
            # An idle turn/Goal does not imply that its retained processes exited.
            # The existing client validates and exhausts this read-only inventory.
            counts['native_background_terminals'] += len(
                await client.list_background_terminals(thread_id))
        after = set(await client.list_loaded_thread_ids())
        counts['native_inventory_unstable'] = int(before != after)
        return counts


async def _standalone_counts():
    from engine.codex_app_server import CodexAppServerClient
    from shared.config import CODEX_APP_SERVER_SOCKET
    client = CodexAppServerClient(CODEX_APP_SERVER_SOCKET)
    try:
        async with asyncio.timeout(4):
            await client.connect()
            return await busy_counts(client)
    finally:
        await client.close()


def standalone_counts():
    # The legacy bootstrap is an independent publisher, never the Web event loop.
    return asyncio.run(_standalone_counts())
