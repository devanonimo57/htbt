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

    async def test_explicit_new_run_retry_is_idempotent_and_later_run_sends_again(self):
        self.release_second.set()
        for run in ('run-one', 'run-two'):
            payload = {'campaign_id':'camp', 'run_id':run, 'start_new_run':True}
            first = await self.client.post('/api/flow-bots/remarketing/send', json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['sent_count'], 3)
            main._STATE_CACHE = None
            retry = await self.client.post('/api/flow-bots/remarketing/send', json=payload)
            self.assertEqual(retry.json()['sent_count'], 0)
        self.assertEqual(self.sent, [41,42,43,41,42,43])
        stale = await self.client.post('/api/flow-bots/remarketing/send', json={
            'campaign_id':'camp', 'run_id':'run-one'})
        self.assertEqual(stale.status_code, 409)
        stale_new = await self.client.post('/api/flow-bots/remarketing/send', json={
            'campaign_id':'camp', 'run_id':'run-one', 'start_new_run':True})
        self.assertEqual(stale_new.status_code, 409)

    async def test_stale_panel_cannot_replace_active_run_identity(self):
        stale = copy.deepcopy(await main._read_state_from_db())
        self.release_second.set()
        await self.client.post('/api/flow-bots/remarketing/send', json={
            'campaign_id':'camp', 'run_id':'run-one', 'start_new_run':True})
        stale['remarketing_campaigns'][0]['message'] = 'Edited configuration only'
        await main._write_state_to_db(stale, preserve_runtime=True)
        saved = await self.persisted()
        self.assertEqual(saved['remarketing_campaigns'][0].get('run_id'), 'run-one')
        self.assertEqual(saved['remarketing_campaigns'][0]['sent_count'], 3)

    async def test_hanging_send_persists_uncertainty_and_never_retries_recipient(self):
        async def hang(**kwargs):
            await asyncio.Event().wait()
        self.bot.send_message = hang
        with patch.object(main, 'REMARKETING_SEND_TIMEOUT_SECONDS', 0.01):
            result = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id':'camp'})
        self.assertFalse(result.json()['has_more'])
        saved = await self.persisted()
        self.assertEqual(saved['remarketing_campaigns'][0]['deliveries']['111:41'], 'uncertain')
        self.release_second.set()
        self.bot.send_message = lambda **kwargs: self._record_send(**kwargs)
        await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id':'camp'})
        self.assertEqual(self.sent, [42,43])

    async def _record_send(self, *, chat_id, **kwargs):
        self.sent.append(chat_id)

    async def test_failed_new_run_commit_does_not_publish_cache_or_send(self):
        before = copy.deepcopy(await main._read_state_from_db())
        with patch.object(main, '_write_state_to_db', side_effect=RuntimeError('commit failed')):
            with self.assertRaises(RuntimeError):
                await self.client.post('/api/flow-bots/remarketing/send', json={
                    'campaign_id':'camp','run_id':'failed-run','start_new_run':True})
        self.assertEqual(self.sent, [])
        self.assertEqual((await main._read_state_from_db())['remarketing_campaigns'], before['remarketing_campaigns'])

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
