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

