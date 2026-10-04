"""Deterministic held-transport regressions with real temporary SQLite."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import main
import telegram_service
import test_checkout_webhook_integration as fixture


class EightFollowup(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def held_checkout(self):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['payments'].update(secondary_enabled=True, secondary_message='SECONDARY')
        await main._write_state_to_db(state)
        started, finish = asyncio.Event(), asyncio.Event()
        self.texts = []
        async def send(**kw):
            self.texts.append(kw['text'])
            if kw['text'] == 'SECONDARY':
                started.set()
                await finish.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = send
        task = asyncio.create_task(self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
        await asyncio.wait_for(started.wait(), 5)
        return task, finish, self.created[-1][0]

    async def test_checkout_durable_before_qr_survives_cancellation(self):
        task, finish, tx = await self.held_checkout()
        self.assertTrue(any('PIX-' + tx in t for t in self.texts))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.manager.tx_records.clear()
        self.manager.active_tx_map.clear()
        main._STATE_CACHE = None
        self.assertIn(tx, (await main._read_state_from_db())['tx_records'])
        self.gateway_status[tx] = 'paid'
        self.bot.send_message.reset_mock(side_effect=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
        self.assertEqual(response.json()['action'], 'approved')
        self.assertEqual(self.bot.send_message.await_count, 1)
        main._STATE_CACHE = None
        self.assertTrue((await main._read_state_from_db())['tx_records'][tx]['delivered'])

    async def test_checkout_failed_commit_does_not_expose_pix(self):
        state = await main._read_state_from_db()
        self.manager.state_saver = AsyncMock(side_effect=RuntimeError('fake commit failure'))
        with self.assertRaisesRegex(RuntimeError, 'fake commit failure'):
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.manager.tx_records)
        self.assertFalse(self.manager.active_tx_map)
        self.assertFalse((await main._read_state_from_db())['tx_records'])
        main._STATE_CACHE = None
        self.assertFalse((await main._read_state_from_db())['tx_records'])

    async def test_webhook_while_secondary_message_pending_is_not_regressed(self):
        task, finish, tx = await self.held_checkout()
        self.gateway_status[tx] = 'paid'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
            self.assertEqual(response.json()['action'], 'approved')
            finish.set()
            await task
            before = self.bot.send_message.await_count
            await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
            self.assertEqual(self.bot.send_message.await_count, before)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['tx_records'][tx]['status'], 'paid')
        self.assertTrue(saved['tx_records'][tx]['delivered'])
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 1)
        self.assertEqual(sum('Entrega concluída' in h['content'] for h in saved['memory']['leads'][0]['history']), 1)
