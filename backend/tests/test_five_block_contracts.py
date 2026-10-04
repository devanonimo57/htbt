"""Expected-correct CRM block commit and follow-up worker contracts."""
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
OUT = Path(os.environ.get('HOTBOT_REPORT') or tempfile.mkdtemp(prefix='five-contract-'))
OUT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = 'C:/Users/Yago/AppData/Local/hermes/cache/scratch'

def evidence(name, value):
    (OUT/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

class FiveBlockContracts(unittest.IsolatedAsyncioTestCase):
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

    async def test_successful_toggle_reads_fresh_intent_and_retains_late_history(self):
        await self.seed_lead()
        state = copy.deepcopy(await main._read_state_from_db())
        peer = copy.deepcopy(state['flow_bots'][0]); peer.update(id='flow-b',token='222:test-only',bot_id='222')
        state['flow_bots'].append(peer)
        await self.manager._get_or_create_lead(state, self.user, bot_token=peer['token'])
        await main._write_state_to_db(state)
        req = main.LeadActionRequest(user_id='7',bot_id='flow-a',origin_bot_numeric_id='111',action='toggle_block')
        # Request queues behind the real writer: latest stage/config must survive.
        await main._DB_LOCK.acquire()
        task = asyncio.create_task(main.handle_lead_action(req))
        await asyncio.sleep(0.01)
        live = await main._read_state_from_db()
        live['flow_bots'][0].setdefault('welcome', {})['message'] = 'NEW SERVER EDIT'
        main._client_map(live)[('111','7',False)]['stage'] = 'UNREAD CURRENT STAGE'
        main._client_map(live)[('111','7',False)]['history'].append({'role':'user','content':'BEFORE LOCK RELEASE'})
        main._DB_LOCK.release()
        self.assertTrue((await task)['blocked'])
        live = await main._read_state_from_db()
        reached, finish = asyncio.Event(), asyncio.Event()
        real_commit = AsyncSession.commit
        async def held(session):
            reached.set(); await finish.wait(); return await real_commit(session)
        with patch.object(AsyncSession, 'commit', new=held):
            task = asyncio.create_task(main.handle_lead_action(req))
            try:
                await asyncio.wait_for(reached.wait(),5)
                self.assertTrue(main._client_map(live)[('111','7',False)]['blocked'])
                main._client_map(live)[('111','7',False)]['history'].append({'role':'user','content':'DURING COMMIT'})
                live['flow_bots'][0]['welcome']['message'] = 'LATE EDIT'
                finish.set(); result = await asyncio.wait_for(task,5)
            finally:
                finish.set(); await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(result['blocked'])
        await main._write_state_to_db(await main._read_state_from_db(),runtime_only=True)
        cold = await self.cold()
        a = main._client_map(cold)[('111','7',False)]
        b = main._client_map(cold)[('222','7',False)]
        self.assertFalse(a['blocked']); self.assertFalse(b['blocked'])
        self.assertEqual(a['stage'],'UNREAD CURRENT STAGE')
        self.assertEqual(cold['flow_bots'][0]['welcome']['message'],'LATE EDIT')
        self.assertIn('DURING COMMIT',[h['content'] for h in a['history']])
        self.assertIn('BEFORE LOCK RELEASE',[h['content'] for h in a['history']])

    async def test_failed_toggle_commit_preserves_live_sql_and_worker(self):
        await self.seed_lead()
        state = await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general'] = {'enabled':True, 'sequences':[
            {'id':'s1','delay_minutes':1,'message':'EXPECTED FOLLOWUP','medias':[]}]}
        await main._write_state_to_db(state)
        req = main.LeadActionRequest(telegram_id='7', bot_id='flow-a', origin_bot_numeric_id='111', action='toggle_block')
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('offline forced block commit failure')):
            with self.assertRaisesRegex(RuntimeError, 'offline forced block commit failure'):
                await main.handle_lead_action(req)
        live = copy.deepcopy(await main._read_state_from_db())
        async with main.async_session() as session:
            row = await session.get(ClientRecord, ('111','7',False))
            sql = json.loads(row.data_json)
        # The actual follow-up consumes the uncommitted block in cache.
        reached, release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep
        async def delayed(delay):
            if delay >= 60:
                reached.set(); await release.wait()
            else: await real_sleep(0)
        self.manager._schedule_flow_downsell = type(self.manager)._schedule_flow_downsell.__get__(self.manager)
        self.manager._get_bot_by_token = lambda token:self.bot
        self.bot.send_message.reset_mock()
        with patch.object(telegram_service.asyncio, 'sleep', new=delayed):
            self.manager._schedule_flow_downsell(self.user, bot_token=self.bot.token)
            worker = self.manager.downsell_tasks['111:7']
            await asyncio.wait_for(reached.wait(),5)
            release.set(); await asyncio.wait_for(worker,5)
        evidence('NEW-03-block-before-commit', {'live_blocked':main._client_map(live)[('111','7',False)]['blocked'],
                 'sql_blocked':sql['blocked'], 'actual_worker_transport_calls':self.bot.send_message.await_count})
        self.assertFalse(main._client_map(live)[('111','7',False)]['blocked'])
        self.assertFalse(sql['blocked'])
        self.bot.send_message.assert_awaited_once()
        self.assertFalse((await self.cold())['memory']['leads'][0]['blocked'])
