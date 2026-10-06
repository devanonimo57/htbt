"""Retained writer inputs must never own the next publication's mutable rows."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import main
import test_checkout_webhook_integration as fixture
from models import BotConfig


class WriterPublicationOwnership(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def asyncTearDown(self):
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    async def seed(self):
        state = copy.deepcopy(await main._read_state_from_db())
        peer = copy.deepcopy(state['flow_bots'][0])
        peer.update(id='flow-b', bot_id='222', token='222:test-only')
        state['flow_bots'] = [state['flow_bots'][0], peer]
        state['memory']['leads'] = [
            {'telegram_id': str(uid), 'bot_id': bid, 'origin_bot_numeric_id': origin,
             'created_at': '2026-10-01T12:00:00', 'stage': 'Conversando',
             'history': [{'role': 'user', 'content': 'original-' + origin}]}
            for uid, bid, origin in ((7, 'flow-a', '111'), (7, 'flow-b', '222'), (8, 'flow-a', '111'))]
        state['tx_records'] = {
            tid: {'tx_id': tid, 'bot_id': 'flow-a', 'bot_token': self.bot.token,
                  'user_id': 7, 'status': 'pending', 'amount': 10, 'mode': 'flow'}
            for tid in ('a', 'b', 'unrelated')}
        return await main._write_state_to_db(state)

    async def sql(self):
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().one()
            return await main._hydrate_records(session, json.loads(config.full_state_json))

    async def test_failed_reused_detached_snapshot_delete_keeps_published_cache_unchanged(self):
        # Exact retained-input/real-SQL-commit sequence from the independent probe.
        live = await self.seed()
        detached = copy.deepcopy(live)
        detached['tx_records']['a']['note'] = 'first successful write'
        published = await main._write_state_to_db(detached, runtime_only=True)
        before = copy.deepcopy(published)
        baseline = published._row_base
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('second SQL commit failed')):
            with self.assertRaisesRegex(RuntimeError, 'second SQL commit failed'):
                await main._write_state_to_db(detached, runtime_only=True,
                    delete_client_keys={('111', '8', False)})
        self.assertIs(main._STATE_CACHE, published)
        self.assertIs(published._row_base, baseline)
        sql = await self.sql()
        self.assertIn(('111', '8', False), main._client_map(sql), 'Rolled-back SQL still contains lead')
        self.assertEqual(published, before, 'Failed reused-snapshot deletion must not remove a published lead')

    async def test_retained_detached_input_remains_independent_after_repeated_success_then_failure(self):
        retained = copy.deepcopy(await self.seed())
        caller_base = retained._row_base
        caller_seen = retained._seen_client_keys
        for note in ('first save', 'second save'):
            retained['tx_records']['a']['note'] = note
            published = await main._write_state_to_db(retained, runtime_only=True)
            self.assertEqual(published['tx_records']['a']['note'], note)
        before = copy.deepcopy(published)
        sql_before = await self.sql()
        baseline = published._row_base
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('retained SQL failure')):
            with self.assertRaisesRegex(RuntimeError, 'retained SQL failure'):
                await main._write_state_to_db(retained, runtime_only=True,
                    delete_client_keys={('111', '8', False)}, delete_tx_ids=['b'])
        self.assertIs(main._STATE_CACHE, published)
        self.assertEqual(published, before)
        self.assertIs(published._row_base, baseline)
        self.assertIs(main._STATE_COMMITTED, baseline)
        self.assertEqual(await self.sql(), sql_before)
        self.assertIs(retained._row_base, caller_base)
        self.assertEqual(retained._seen_client_keys, caller_seen)
        self.assertIn(('111', '8', False), main._client_map(retained))

    async def test_retained_live_saved_again_cannot_mutate_new_publication(self):
        retained = await self.seed()
        caller_base = retained._row_base
        retained['tx_records']['a']['note'] = 'current-live write'
        await main._write_state_to_db(retained, runtime_only=True)
        retained['tx_records']['a']['note'] = 'now an old live input'
        published = await main._write_state_to_db(retained, runtime_only=True)
        before = copy.deepcopy(published)
        retained['tx_records']['a']['amount'] = 999
        retained['memory']['leads'][0]['history'].append({'content': 'caller only'})
        self.assertEqual(published, before)
        self.assertIs(retained._row_base, caller_base)
        self.assertEqual((await self.sql())['tx_records']['a']['amount'], 10)

    async def test_retained_snapshot_and_legacy_rows_do_not_alias_successful_publication(self):
        for kind in ('snapshot', 'plain', 'shallow', 'shallow_snapshot'):
            with self.subTest(kind=kind):
                live = await self.seed()
                if kind == 'snapshot':
                    incoming = copy.deepcopy(live)
                elif kind == 'plain':
                    incoming = copy.deepcopy(dict(live))
                elif kind == 'shallow':
                    incoming = dict(live)
                else:
                    incoming = copy.copy(live)
                incoming['tx_records']['a']['note'] = 'force actual save'
                published = await main._write_state_to_db(incoming, runtime_only=True)
                before = copy.deepcopy(published)
                baseline = published._row_base
                incoming['tx_records']['a']['amount'] = 999
                incoming['memory']['leads'][0]['history'][0]['content'] = 'caller only'
                incoming['memory']['leads'][0]['history'].append({'content': 'caller only'})
                self.assertEqual(published, before)
                self.assertIs(published._row_base, baseline)
                caller_pix_before = copy.deepcopy(incoming['tx_records']['b'])
                published['tx_records']['b']['note'] = 'publication only-' + kind
                self.assertEqual(incoming['tx_records']['b'], caller_pix_before)

    async def test_shallow_snapshot_preparation_cannot_mutate_cache_before_failed_sql_commit(self):
        live = await self.seed()
        live['memory']['leads'][0].update(stage='Em conversa', stage_history=[])
        incoming = copy.copy(live)
        before = copy.deepcopy(live)
        baseline = live._row_base
        sql_before = await self.sql()
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('shallow SQL failure')):
            with self.assertRaisesRegex(RuntimeError, 'shallow SQL failure'):
                await main._write_state_to_db(incoming, runtime_only=True,
                    delete_client_keys={('111', '8', False)})
        self.assertIs(main._STATE_CACHE, live)
        self.assertEqual(live, before)
        self.assertIs(live._row_base, baseline)
        self.assertIs(main._STATE_COMMITTED, baseline)
        self.assertEqual(await self.sql(), sql_before)

    async def test_internally_prepared_checkout_event_does_not_publish_caller_payload_aliases(self):
        live = await self.seed()
        before = copy.deepcopy(live['memory']['leads'][0])
        after = copy.deepcopy(before)
        after['last_tx_id'] = 'event-pix'
        record = copy.deepcopy(live['tx_records']['a'])
        record['tx_id'] = 'event-pix'
        history = {'role': 'assistant', 'tx_id': 'event-pix', 'content': 'event checkout'}
        event = {'record': record, 'before': before, 'lead': after, 'history': history}
        published = await main._write_state_to_db(live, runtime_only=True, checkout_event=event)
        saved = copy.deepcopy(published)
        record['amount'] = 999
        history['content'] = 'caller payload edit'
        after['history'].append({'content': 'caller payload edit'})
        live['tx_records']['b']['amount'] = 999
        live['memory']['leads'][2]['history'].append({'content': 'old live edit'})
        self.assertEqual(published, saved)
        self.assertEqual((await self.sql())['tx_records']['event-pix']['amount'], 10)
