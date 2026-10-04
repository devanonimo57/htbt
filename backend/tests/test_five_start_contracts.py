"""Expected-correct registered /start regressions; fake transports, real SQL."""
import asyncio, copy, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Dispatcher, types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import main, telegram_service
import test_checkout_webhook_integration as fixture
from models import ClientRecord, BotConfig, PixRecord

import os
OUT = Path(os.environ['HOTBOT_REPORT'])
OUT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = 'C:/Users/Yago/AppData/Local/hermes/cache/scratch'

def evidence(name, value):
    (OUT/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

class FiveStartContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.manager._schedule_flow_downsell = lambda *a, **kw: None
        self.manager.dp = Dispatcher()
        self.manager._register_handlers()
        telegram_service._CURRENT_BOT_TOKEN_CTX.set('')
        telegram_service._CURRENT_BOT_CTX.set(None)
    async def asyncTearDown(self):
        for t in set(self.manager.downsell_tasks.values()) | set(self.manager.upsell_tasks.values()):
            if not t.done(): t.cancel()
        await asyncio.gather(*set(self.manager.downsell_tasks.values()), *set(self.manager.upsell_tasks.values()), return_exceptions=True)
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)
    def handler(self, name):
        return next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__ == name)
    def start_message(self, answer, user=None):
        return SimpleNamespace(bot=self.bot, from_user=user or self.user, chat=SimpleNamespace(id=7), answer=answer, text='/start')
    async def cold(self):
        main._STATE_CACHE = None
        return await main._read_state_from_db()
    async def seed_lead(self):
        state = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state, runtime_only=True)
        return lead

    async def test_old_start_cannot_contaminate_paid_reentry(self):
        await self.seed_lead()
        state = await main._read_state_from_db()
        state['flow_bots'][0]['welcome'] = {'message':'OLD SESSION WELCOME'}
        await main._write_state_to_db(state)
        old_created = (await main._read_state_from_db())['memory']['leads'][0]['created_at']
        reached, release = asyncio.Event(), asyncio.Event()
        async def old_answer(*a, **kw):
            reached.set(); await release.wait(); return SimpleNamespace(message_id=1)
        old_task = asyncio.create_task(self.handler('handle_start')(self.start_message(old_answer)))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            await main.delete_lead('7', bot_id='flow-a')
            state = await main._read_state_from_db()
            state['flow_bots'][0]['welcome']['message'] = 'NEW SESSION WELCOME'
            await main._write_state_to_db(state)
            new_user = types.User(id=7, is_bot=False, first_name='NEW PROFILE')
            await self.handler('handle_start')(self.start_message(AsyncMock(return_value=SimpleNamespace(message_id=2)), new_user))
            # A real newly paid checkout belongs to the new incarnation.
            checkout_state = await main._read_state_from_db()
            await self.manager._generate_flow_checkout(self.callback, checkout_state['flow_bots'][0], checkout_state, 'plan-a', 0, [])
            tx = self.created[-1][0]
            self.gateway_status[tx] = 'paid'
            self.assertEqual((await main.webhook_gateway({'id':tx, 'status':'paid'}))['action'], 'approved')
            before = copy.deepcopy((await main._read_state_from_db())['memory'])
            self.assertEqual(before['leads'][0]['stage'], 'Entrega concluída')
            self.assertNotEqual(before['leads'][0]['created_at'], old_created)
            release.set(); await asyncio.wait_for(old_task, 5)
        finally:
            release.set(); await asyncio.gather(old_task, return_exceptions=True)
        cold = await self.cold()
        active = cold['memory']['leads'][0]; archived = cold['memory']['deleted_leads'][0]
        evidence('NEW-02-stale-start-reentry', {'before_completion':before,
                 'cold_active':active, 'cold_archived':archived, 'old_created':old_created,
                 'cold_pix':cold['tx_records'][tx]})
        self.assertEqual(cold['tx_records'][tx]['status'], 'paid')
        self.assertTrue(cold['tx_records'][tx]['delivered'])
        self.assertEqual(active['stage'], 'Entrega concluída')
        self.assertEqual(active['created_at'], before['leads'][0]['created_at'])
        self.assertNotIn('OLD SESSION WELCOME', [h['content'] for h in active['history']])
        self.assertIn('NEW SESSION WELCOME', [h['content'] for h in active['history']])
        self.assertNotIn('OLD SESSION WELCOME', [h['content'] for h in archived['history']])

    async def test_queued_writer_fails_closed_on_missing_creation_time(self):
        await self.seed_lead()
        live = await main._read_state_from_db()
        live['memory']['leads'][0].pop('created_at')
        await main._write_state_to_db(live, runtime_only=True)
        stale = copy.deepcopy(await main._read_state_from_db())
        fresh = await main._read_state_from_db()
        fresh['memory']['leads'][0]['history'].append({'role':'user','content':'CURRENT'})
        await main._write_state_to_db(fresh, runtime_only=True)
        stale['memory']['leads'][0]['history'].append({'role':'assistant','content':'UNPROVEN'})
        await main._write_state_to_db(stale, runtime_only=True)
        histories = [h['content'] for h in (await self.cold())['memory']['leads'][0]['history']]
        self.assertIn('CURRENT', histories)
        self.assertNotIn('UNPROVEN', histories)

    async def test_start_rejects_missing_or_ambiguous_transport(self):
        for bot in (None, SimpleNamespace(token='opaque'), SimpleNamespace(token='111:test', id=222)):
            with self.subTest(bot=bot):
                answer = AsyncMock()
                task = asyncio.create_task(asyncio.Event().wait())
                self.manager.downsell_tasks['222:7'] = task
                msg = self.start_message(answer); msg.bot = bot
                await self.handler('handle_start')(msg)
                self.assertFalse(task.cancelled())
                answer.assert_not_awaited()
                self.assertEqual((await self.cold())['memory']['leads'], [])
                task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_start_keeps_peer_downsell_and_upsell(self):
        state = copy.deepcopy(await main._read_state_from_db())
        peer = copy.deepcopy(state['flow_bots'][0]); peer.update(id='flow-b', token='222:test-only', bot_id='222')
        state['flow_bots'].append(peer)
        await self.manager._get_or_create_lead(state, self.user, bot_token=peer['token'])
        await main._write_state_to_db(state, runtime_only=True)
        blocked = asyncio.Event()
        async def worker(*a, **kw): await blocked.wait()
        self.manager._flow_downsell_worker = worker
        self.manager._schedule_flow_downsell = type(self.manager)._schedule_flow_downsell.__get__(self.manager)
        self.manager._schedule_flow_downsell(self.user, bot_token=peer['token'])
        downsell_b = self.manager.downsell_tasks['222:7']
        upsell_b = asyncio.create_task(worker())
        self.manager.upsell_tasks['222:7'] = upsell_b
        old_a = asyncio.create_task(worker())
        self.manager.downsell_tasks['111:7'] = old_a
        upsell_a = asyncio.create_task(worker())
        self.manager.upsell_tasks['111:7'] = upsell_a
        await asyncio.sleep(0)
        await self.handler('handle_start')(self.start_message(AsyncMock(return_value=SimpleNamespace(message_id=1))))
        await asyncio.sleep(0)
        cold = await self.cold()
        result = {'downsell_B_cancelled':downsell_b.cancelled(), 'upsell_B_cancelled':upsell_b.cancelled(),
                  'remaining_downsell_keys':sorted(self.manager.downsell_tasks),
                  'cold_lead_origins':sorted(l['origin_bot_numeric_id'] for l in cold['memory']['leads'])}
        evidence('NEW-01-start-peer', result)
        self.assertTrue(old_a.cancelled())
        self.assertTrue(upsell_a.cancelled())
        self.assertFalse(downsell_b.cancelled())
        self.assertFalse(upsell_b.cancelled())
        self.assertEqual(result['cold_lead_origins'], ['111','222'])
        downsell_b.cancel(); upsell_b.cancel()
        await asyncio.gather(old_a, upsell_a, return_exceptions=True)
        await asyncio.gather(downsell_b, upsell_b, return_exceptions=True)

