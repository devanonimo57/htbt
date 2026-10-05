"""NEW-02 first admission: registered handlers, real ASGI and cold SQL.

Import fixture modules, not TestCase classes, to avoid duplicate collection.
"""
import asyncio
import copy
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from aiogram import Dispatcher
import main
import telegram_service
import test_checkout_webhook_integration as fixture


class FirstStartCollision(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.bot.id = 111
        self.manager._schedule_flow_downsell = lambda *a, **kw: None
        self.manager.dp = Dispatcher()
        self.manager._register_handlers()
        telegram_service._CURRENT_BOT_TOKEN_CTX.set('')
        telegram_service._CURRENT_BOT_CTX.set(None)
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')
        current = await main._read_state_from_db()
        self.assertEqual(current['memory']['leads'], [])
        current['flow_bots'][0]['welcome'] = {'message': 'FIRST HELD START'}
        await main._write_state_to_db(current)

    async def asyncTearDown(self):
        await self.client.aclose()
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    def start(self, answer):
        handler = next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__ == 'handle_start')
        return handler(SimpleNamespace(bot=self.bot, from_user=self.user, chat=SimpleNamespace(id=7), answer=answer, text='/start'))

    async def cold(self):
        main._STATE_CACHE = None
        return await main._read_state_from_db()

    async def checkout_paid(self):
        state = await main._read_state_from_db()
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx = self.created[-1][0]
        self.gateway_status[tx] = 'paid'
        response = await self.client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['action'], 'approved')
        return tx

    async def reentry(self, permanent):
        reached, finish = asyncio.Event(), asyncio.Event()
        async def answer(*a, **kw):
            reached.set()
            await finish.wait()
            return SimpleNamespace(message_id=1)
        task = asyncio.create_task(self.start(answer))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            held = await main._read_state_from_db()
            self.assertNotIn(('111', '7', False), main._client_map(held._row_base))
            old_created = held['memory']['leads'][0]['created_at']
            response = await self.client.delete('/api/leads/7?bot_id=flow-a')
            self.assertEqual(response.status_code, 200, response.text)
            if permanent:
                response = await self.client.delete('/api/leads/7/permanent?bot_id=flow-a')
                self.assertEqual(response.status_code, 200, response.text)
            current = await main._read_state_from_db()
            current['flow_bots'][0]['welcome']['message'] = 'NEW REAL START'
            await main._write_state_to_db(current)
            await self.start(AsyncMock(return_value=SimpleNamespace(message_id=2)))
            tx = await self.checkout_paid()
            before = copy.deepcopy(await main._read_state_from_db())
            self.assertEqual(before['memory']['leads'][0]['stage'], 'Entrega concluída')
            self.assertNotEqual(before['memory']['leads'][0]['created_at'], old_created)
            finish.set()
            await asyncio.wait_for(task, 5)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        await main._write_state_to_db(await main._read_state_from_db(), runtime_only=True)
        cold = await self.cold()
        if os.environ.get('HOTBOT_REPORT'):
            Path(os.environ['HOTBOT_REPORT'], 'empty-first-start-' + ('permanent' if permanent else 'archive') + '.json').write_text(
                json.dumps({'before': dict(before), 'cold': dict(cold)}, ensure_ascii=False, indent=2), encoding='utf8')
        self.assertEqual(cold['memory'], before['memory'])
        self.assertEqual(cold['tx_records'], before['tx_records'])
        self.assertEqual(cold['flow_bots'], before['flow_bots'])
        self.assertEqual(cold['learning'], before['learning'])
        for marker in ('delivered', 'sales_counted'):
            self.assertTrue(cold['tx_records'][tx][marker])
        self.assertEqual(cold['flow_bots'][0]['sales_today'], 1)
        self.assertEqual(cold['learning']['confirmed_purchases'], 1)

    async def test_empty_first_start_archive_reentry_paid(self):
        await self.reentry(False)

    async def test_empty_first_start_permanent_reentry_paid(self):
        await self.reentry(True)

    async def test_ordinary_first_start_history_is_durable(self):
        await self.start(AsyncMock(return_value=SimpleNamespace(message_id=1)))
        lead = (await self.cold())['memory']['leads'][0]
        self.assertEqual(lead['stage'], 'Viu os planos')
        self.assertEqual([h['content'] for h in lead['history']], ['FIRST HELD START'])

    async def test_concurrent_first_start_same_incarnation_keeps_both_histories(self):
        reached, finish = asyncio.Event(), asyncio.Event()
        async def answer(*a, **kw):
            reached.set()
            await finish.wait()
            return SimpleNamespace(message_id=1)
        task = asyncio.create_task(self.start(answer))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            held = await main._read_state_from_db()
            created = held['memory']['leads'][0]['created_at']
            self.assertNotIn(('111', '7', False), main._client_map(held._row_base))
            # A normal cache publisher detaches the old handler without changing
            # its customer incarnation; the old welcome must still be admitted.
            held['flow_bots'][0]['welcome']['message'] = 'SECOND CURRENT START'
            await main._write_state_to_db(copy.deepcopy(held))
            await self.start(AsyncMock(return_value=SimpleNamespace(message_id=2)))
            finish.set()
            await asyncio.wait_for(task, 5)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        lead = (await self.cold())['memory']['leads'][0]
        self.assertEqual(lead['created_at'], created)
        contents = [h['content'] for h in lead['history']]
        self.assertEqual(contents.count('FIRST HELD START'), 1)
        self.assertEqual(contents.count('SECOND CURRENT START'), 1)

    async def test_absent_baseline_queued_same_creation_preserves_paid_current_fields(self):
        live = await main._read_state_from_db()
        await self.manager._get_or_create_lead(live, self.user, bot_token=self.bot.token)
        stale = copy.deepcopy(live)
        self.assertNotIn(('111', '7', False), main._client_map(stale._row_base))
        stale_lead = stale['memory']['leads'][0]
        stale_lead['history'].append({'role': 'assistant', 'content': 'QUEUED FIRST HISTORY'})
        stale_lead['stage'] = 'Viu os planos'
        tx = await self.checkout_paid()
        before = copy.deepcopy(await main._read_state_from_db())
        expected = copy.deepcopy(before['memory'])
        expected['leads'][0]['history'].append({'role': 'assistant', 'content': 'QUEUED FIRST HISTORY'})
        await main._write_state_to_db(stale, runtime_only=True)
        await main._write_state_to_db(await main._read_state_from_db(), runtime_only=True)
        cold = await self.cold()
        self.assertEqual(cold['memory'], expected)
        self.assertEqual(cold['tx_records'], before['tx_records'])
        self.assertEqual(cold['flow_bots'], before['flow_bots'])
        self.assertEqual(cold['learning'], before['learning'])
        self.assertTrue(cold['tx_records'][tx]['delivered'])

    async def test_unique_first_insert_without_creation_time_is_not_lost(self):
        state = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        lead.pop('created_at')
        lead['history'].append({'role': 'user', 'content': 'UNIQUE FIRST INSERT'})
        await main._write_state_to_db(state, runtime_only=True)
        cold = await self.cold()
        self.assertEqual(cold['memory']['leads'][0], lead)
        self.assertNotIn('created_at', cold['memory']['leads'][0])
