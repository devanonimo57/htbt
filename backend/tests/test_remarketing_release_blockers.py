"""Release-blocking probes; intentionally red on the pending remarketing branch.
All Telegram sends are fake; each probe uses an isolated SQLite database.
"""
import asyncio
import copy
import unittest
from unittest.mock import patch

import main
from tests import test_remarketing_interruption as fixtures


class RemarketingReleaseBlockers(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RemarketingInterruption.asyncSetUp
    asyncTearDown = fixtures.RemarketingInterruption.asyncTearDown
    _close = fixtures.RemarketingInterruption._close
    persisted = fixtures.RemarketingInterruption.persisted

    async def test_rate_limit_stops_continuation_with_more_than_one_batch(self):
        state = await main._read_state_from_db()
        state['memory']['leads'].extend(
            {'telegram_id': str(uid), 'bot_id': 'flow', 'origin_bot_numeric_id': '111', 'history': []}
            for uid in range(100, 112))
        await main._write_state_to_db(state)
        attempts = []
        async def reject(*, chat_id, **kwargs):
            attempts.append(chat_id)
            raise RuntimeError('Retry after 60')
        self.bot.send_message = reject
        response = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['has_more'],
                         f'Immediate frontend continuation retries rate-limited recipients: {attempts}')

    async def test_campaign_edit_allows_new_intentional_send(self):
        self.release_second.set()
        first = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(first.json()['sent_count'], 3)
        state = await main._read_state_from_db()
        state['remarketing_campaigns'][0]['message'] = 'Nova oferta aprovada pelo operador'
        await main._write_state_to_db(state, preserve_runtime=True)
        second = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(second.json()['sent_count'], 3,
                         'Campaign lifetime claims permanently suppress later intentional runs')

    async def test_stale_worker_cache_preserves_discount_committed_by_another_worker(self):
        stale_worker = copy.deepcopy(await main._read_state_from_db())
        writer = copy.deepcopy(stale_worker)
        writer['memory']['leads'][0]['authorized_flow_discounts'] = [20]
        await main._write_state_to_db(writer)
        # Restore another process's stale cache/baseline; the DB contains [20].
        main._STATE_CACHE = stale_worker
        main._STATE_COMMITTED = copy.deepcopy(dict(stale_worker))
        main._STATE_COMMITTED_OWNER = stale_worker
        stale_worker['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'oi'})
        await main._write_state_to_db(stale_worker)
        saved = await self.persisted()
        self.assertIn(20, saved['memory']['leads'][0].get('authorized_flow_discounts', []))

    async def test_send_has_walltime_bound_not_only_recipient_bound(self):
        async def hanging_send(**kwargs):
            await asyncio.Event().wait()
        self.bot.send_message = hanging_send
        # A 200ms outer watchdog proves an unbounded await without a live network.
        # A future implementation must inject a short test send/batch deadline.
        with patch.object(main, 'REMARKETING_SEND_TIMEOUT_SECONDS', 0.01, create=True), \
             patch.object(main, 'REMARKETING_BATCH_TIMEOUT_SECONDS', 0.05, create=True):
            try:
                response = await asyncio.wait_for(
                    self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'}), 0.2)
            except asyncio.TimeoutError:
                self.fail('An awaited send has no enforced deadline; even a one-recipient batch can hang')
            self.assertEqual(response.status_code, 200, response.text)
