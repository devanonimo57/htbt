"""Finite offline acceptance tests; real handlers, checkout, workers and SQL.
Run only through overnight offline-runner.py (outbound socket guard).
"""
import asyncio
import copy
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Dispatcher, types
import main
import telegram_service as service
import test_checkout_webhook_integration as fixture

REPORT = Path(os.environ.get('HOTBOT_REPORT', str(Path(__file__).resolve().parent / 'report')))
REPORT.mkdir(parents=True, exist_ok=True)


class Clock(datetime):
    value = datetime(2030, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.value.replace(tzinfo=None)


class Gate:
    def __init__(self):
        self.waits = asyncio.Queue()
        self.release = asyncio.Queue()
        self.delays = []
        self.completed = 0
        self.asleep = set()
        self.real_sleep = asyncio.sleep

    async def sleep(self, seconds):
        if seconds < 1:
            return await self.real_sleep(0)
        self.delays.append(seconds)
        task = asyncio.current_task()
        self.asleep.add(task)
        await self.waits.put(seconds)
        try:
            await self.release.get()
            Clock.value += timedelta(seconds=seconds)
            self.completed += 1
        finally:
            self.asleep.discard(task)

    async def held(self):
        return await asyncio.wait_for(self.waits.get(), 3)

    async def wake(self):
        self.release.put_nowait(True)
        await self.real_sleep(0)


class GeminiDownsell(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Clock.value = datetime(2030, 1, 1, tzinfo=timezone.utc)
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        import tempfile
        import uuid
        temporary_directory = tempfile.TemporaryDirectory
        with patch.object(fixture, 'create_async_engine', main.create_application_engine), patch.object(
            fixture.tempfile, 'TemporaryDirectory',
            lambda: temporary_directory(prefix='downsell-' + uuid.uuid4().hex + '-', ignore_cleanup_errors=True)
        ):
            await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.manager.dp = Dispatcher()
        self.manager._register_handlers()
        self.manager._get_bot_by_token = lambda token: self.bot if token.startswith('111:') else self.peer
        self.peer = SimpleNamespace(token='222:test-only', id=222, send_message=AsyncMock())
        service._CURRENT_BOT_TOKEN_CTX.set('')
        service._CURRENT_BOT_CTX.set(None)
        self.gate = Gate()
        self.sleep_patch = patch.object(service.asyncio, 'sleep', self.gate.sleep)
        self.clock_patch = patch.object(service, 'datetime', Clock)
        self.sleep_patch.start()
        self.clock_patch.start()
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        tasks = set(self.manager.downsell_tasks.values()) | {
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            and getattr(t.get_coro(), '__name__', '') == '_flow_downsell_worker'
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sleep_patch.stop()
        self.clock_patch.stop()
        main.app.dependency_overrides.clear()
        await self.client.aclose()
        Path(REPORT, self._testMethodName + '.json').write_text(json.dumps({
            'wait_seconds': self.gate.delays,
            'A_messages': self.messages(),
            'B_messages': [c.kwargs.get('text') for c in self.peer.send_message.call_args_list],
        }, indent=2), encoding='utf8')
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    def seq(self, sid, delay=1, **kw):
        return dict(id=sid, message=sid, delay_minutes=delay, trigger='start', target_audience='all', **kw)

    async def config(self, seqs, peer=False, **kw):
        state = await main._read_state_from_db()
        fb = state['flow_bots'][0]
        fb['welcome'] = {'message':'WELCOME'}
        fb['downsell_general'] = dict(enabled=True, sequences=seqs, loop_rest_minutes=3, **kw)
        if peer:
            other = copy.deepcopy(fb)
            other.update(id='flow-b', bot_id='222', token=self.peer.token)
            state['flow_bots'].append(other)
        await main._write_state_to_db(state)

    async def start(self, bot=None):
        handler = next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__ == 'handle_start')
        await handler(SimpleNamespace(bot=bot or self.bot, from_user=self.user,
            chat=SimpleNamespace(id=7), answer=AsyncMock(), text='/start'))

    async def checkout(self):
        handler = next(h.callback for h in self.manager.dp.callback_query.handlers if h.callback.__name__ == 'handle_flow_plan_click')
        await handler(SimpleNamespace(bot=self.bot, from_user=self.user, message=self.callback.message,
            data='fplan:plan-a:0', answer=AsyncMock()))

    def messages(self):
        return [c.kwargs.get('text') for c in self.bot.send_message.call_args_list
                if c.kwargs.get('text') in ('A','B','C','D','P','NEW','ALL')]

    async def step(self):
        delay = await self.gate.held()
        await self.gate.wake()
        return delay

    async def until(self, expected, limit=20):
        async def committed():
            for _ in range(300):
                workers = [t for t in set(self.manager.downsell_tasks.values()) if not t.done()]
                if all(t in self.gate.asleep for t in workers):
                    return
                await self.gate.real_sleep(0.01)
            self.fail('Worker did not reach post-commit sleep/completion boundary')
        for _ in range(limit):
            for _ in range(100):
                if self.messages() == expected:
                    await committed()
                    return
                if not self.gate.waits.empty():
                    break
                await self.gate.real_sleep(0.01)
            if self.messages() == expected:
                await committed()
                return
            await self.step()
        self.assertEqual(self.messages(), expected)

    async def cold(self):
        main._STATE_CACHE = None
        return await main._read_state_from_db()

    async def test_first_start_and_first_pix_registered_handlers_keep_one_worker_and_anchors(self):
        pix = self.seq('P', 2); pix['trigger'] = 'pix'
        await self.config([self.seq('A', 2), pix])
        await self.start()
        await self.gate.held()
        worker = self.manager.downsell_tasks['111:7']
        await self.start()
        self.assertIs(self.manager.downsell_tasks['111:7'], worker, 'Repeated /start must not reset first entry')
        await self.checkout()
        self.assertIs(self.manager.downsell_tasks['111:7'], worker, 'Pix must not cancel start cards')
        await self.checkout()
        self.assertIs(self.manager.downsell_tasks['111:7'], worker, 'Extra Pix must not reset first Pix')
        await self.gate.wake()
        await self.until(['A','P'])
        lead = (await self.cold())['memory']['leads'][0]
        self.assertEqual(len(lead['flow_downsell']['anchors']), 2)
        self.assertEqual(len((await self.cold())['tx_records']), 2)

    async def test_ordered_initial_selected_loop_and_rest(self):
        await self.config([self.seq('A', 2, loop_enabled=True), self.seq('B', 1), self.seq('C', 1, loop_enabled=True)])
        await self.start()
        await self.until(['A','B','C'])
        waits_before = self.gate.completed
        await self.until(['A','B','C','A','C'])
        self.assertGreaterEqual(sum(self.gate.delays[waits_before:]), 180, 'Cycle consumes configured rest')
        self.assertNotIn('B', self.messages()[3:])

    async def test_live_reorder_delete_and_disable_while_real_worker_sleeps(self):
        await self.config([self.seq('A'), self.seq('B'), self.seq('C')])
        await self.start()
        await self.gate.held()
        state = await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general']['sequences'] = [self.seq('C'), self.seq('B')]
        await main._write_state_to_db(state)
        await self.gate.wake()
        await self.until(['C','B'])
        self.assertNotIn('A', self.messages())
        await self.config([self.seq('D', 10, loop_enabled=True)])
        await self.start()
        await self.gate.held()
        state = await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general']['enabled'] = False
        await main._write_state_to_db(state)
        worker = self.manager.downsell_tasks['111:7']
        await self.gate.wake()
        await asyncio.wait_for(worker, 3)
        self.assertNotIn('D', self.messages())

    async def test_new_audience_uses_same_immutable_origin_paid_history_not_stage_or_pending(self):
        new = self.seq('NEW'); new['target_audience'] = 'new'
        await self.config([new, self.seq('ALL')])
        state = await main._read_state_from_db()
        state['tx_records']['old'] = dict(tx_id='old', user_id=7, bot_id='old-panel-id',
            bot_token=self.bot.token, origin_bot_numeric_id='111', status='paid', amount=10,
            created_at='2029-01-01T00:00:00', paid_at='2029-01-01T00:00:00')
        await main._write_state_to_db(state)
        await self.start()
        await self.until(['ALL'])
        self.assertNotIn('NEW', self.messages())

    async def test_other_bot_paid_and_pending_pix_do_not_exclude_new_customer(self):
        new = self.seq('NEW'); new['target_audience'] = 'new'
        await self.config([new])
        state = await main._read_state_from_db()
        for txid, tok, status in [('peer','222:test-only','paid'), ('pending',self.bot.token,'pending')]:
            state['tx_records'][txid] = dict(tx_id=txid, user_id=7, bot_id='flow-a', bot_token=tok,
                status=status, amount=10, created_at='2029-01-01T00:00:00', paid_at='2029-01-01T00:00:00')
        await main._write_state_to_db(state)
        await self.start()
        await self.until(['NEW'])

    async def test_payment_any_distinct_pix_stops_real_worker_and_preserves_peer(self):
        await self.config([self.seq('A', loop_enabled=True)], peer=True)
        await self.start(); await self.gate.held()
        task_a = self.manager.downsell_tasks['111:7']
        await self.start(self.peer); await self.gate.held()
        task_b = self.manager.downsell_tasks['222:7']
        await self.checkout(); await self.checkout()
        txid = self.created[0][0]
        self.gateway_status[txid] = 'paid'
        response = await self.client.post('/api/webhook/syncpay', json={'id':txid,'status':'paid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['action'], 'approved')
        await self.gate.wake(); await self.gate.wake()
        await asyncio.wait_for(task_a, 3)
        self.assertFalse(task_b.done(), 'Peer bot cycle must remain independent')
        self.assertEqual(self.messages(), [])
        self.assertEqual((await self.cold())['tx_records'][txid]['status'], 'paid')

    async def cancellation(self, operation):
        await self.config([self.seq('A', loop_enabled=True)], peer=True)
        await self.start(); await self.gate.held()
        task = self.manager.downsell_tasks['111:7']
        await self.start(self.peer); await self.gate.held()
        peer = self.manager.downsell_tasks['222:7']
        if operation == 'block':
            await self.manager._mark_lead_blocked(7, bot_token=self.bot.token)
        else:
            response = await self.client.delete('/api/leads/7?bot_id=flow-a')
            self.assertEqual(response.status_code, 200)
            if operation == 'permanent':
                response = await self.client.delete('/api/leads/7/permanent?bot_id=flow-a')
                self.assertEqual(response.status_code, 200)
        await self.gate.wake()
        await asyncio.wait_for(task, 3)
        self.assertFalse(peer.done())
        self.assertEqual(self.messages(), [])

    async def test_block_cancel_only_origin_worker_held_then_woken(self):
        await self.cancellation('block')

    async def test_archive_cancel_only_origin_worker_held_then_woken(self):
        await self.cancellation('archive')

    async def test_permanent_delete_cancel_only_origin_worker_held_then_woken(self):
        await self.cancellation('permanent')

    async def test_restart_cold_state_resumes_without_duplicate_initial_send(self):
        await self.config([self.seq('A'), self.seq('B', 2, loop_enabled=True)])
        await self.start(); await self.until(['A'])
        task = self.manager.downsell_tasks['111:7']
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.manager.downsell_tasks.clear()
        saved = await self.cold()
        self.manager._resume_flow_downsells(saved)
        await self.until(['A','B'])
        self.assertEqual(self.messages().count('A'), 1)
        self.assertEqual((await self.cold())['memory']['leads'][0]['flow_downsell']['sent'], ['A','B'])

    async def test_actual_start_anchor_precedes_held_welcome_transport(self):
        await self.config([self.seq('A', 2)])
        reached, finish = asyncio.Event(), asyncio.Event()
        async def answer(*args, **kw):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set(); await finish.wait()
        handler = next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__ == 'handle_start')
        task = asyncio.create_task(handler(SimpleNamespace(bot=self.bot, from_user=self.user,
            chat=SimpleNamespace(id=7), answer=answer, text='/start')))
        try:
            await asyncio.wait_for(reached.wait(),3)
            anchor = Clock.value.timestamp()
            Clock.value += timedelta(minutes=5)
            finish.set(); await asyncio.wait_for(task,3)
            await self.until(['A'])
            lead = (await self.cold())['memory']['leads'][0]
            self.assertEqual(lead['flow_downsell']['anchors']['start'],anchor)
            self.assertEqual(self.gate.delays, [], 'A slow welcome cannot postpone the first event anchor')
        finally:
            finish.set(); await asyncio.gather(task,return_exceptions=True)

    async def test_actual_first_pix_anchor_precedes_held_checkout_transport(self):
        pix = self.seq('P', 2); pix['trigger']='pix'
        await self.config([pix])
        reached, finish=asyncio.Event(),asyncio.Event()
        async def send(**kw):
            if 'PIX-' in kw['text']:
                self.assertFalse(main._DB_LOCK.locked())
                reached.set();await finish.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect=send
        task=asyncio.create_task(self.checkout())
        try:
            await asyncio.wait_for(reached.wait(),3)
            saved=await self.cold()
            anchor=service.ActiveBotManager._downsell_timestamp(saved['tx_records'][self.created[0][0]]['created_at'])
            Clock.value+=timedelta(minutes=5)
            finish.set();await asyncio.wait_for(task,3)
            await self.until(['P'])
            lead=(await self.cold())['memory']['leads'][0]
            self.assertEqual(lead['flow_downsell']['anchors']['pix'],anchor)
            self.assertNotIn('start',lead['flow_downsell']['anchors'])
            self.assertEqual(self.gate.delays, [], 'A slow QR send cannot postpone first Pix creation')
        finally:
            finish.set();await asyncio.gather(task,return_exceptions=True)

    async def test_stale_media_await_permanent_delete_new_incarnation_cannot_send_or_write_old_cycle(self):
        seq=self.seq('A');seq['medias']=[{'id':'photo','type':'photo','url':'data:image/png;base64,AAAA'}]
        await self.config([seq])
        reached,finish=asyncio.Event(),asyncio.Event()
        async def photo(**kw):
            reached.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()  # a transport that finishes despite cancellation
            return SimpleNamespace(photo=[SimpleNamespace(file_id='offline-photo')])
        self.bot.send_photo=AsyncMock(side_effect=photo)
        await self.start()
        await self.step()
        await asyncio.wait_for(reached.wait(),3)
        old=self.manager.downsell_tasks['111:7']
        original=(await main._read_state_from_db())['memory']['leads'][0]['created_at']
        response=await self.client.delete('/api/leads/7?bot_id=flow-a');self.assertEqual(response.status_code,200)
        response=await self.client.delete('/api/leads/7/permanent?bot_id=flow-a');self.assertEqual(response.status_code,200)
        Clock.value+=timedelta(seconds=1)
        await self.config([self.seq('B',10)])
        await self.start()
        await self.gate.held()
        before=copy.deepcopy((await main._read_state_from_db())['memory']['leads'][0])
        self.assertNotEqual(before['created_at'],original)
        finish.set();await asyncio.wait_for(old,3)
        after=(await self.cold())['memory']['leads'][0]
        self.assertEqual(after,before,'Stale transport completion must not merge old customer data')
        self.assertEqual(self.messages(),[])

    async def test_payment_cold_wakeup_no_cancel_shortcut_stops_all_audience_and_loop(self):
        await self.config([self.seq('A',loop_enabled=True)])
        await self.start();await self.gate.held()
        task=self.manager.downsell_tasks['111:7']
        state=await main._read_state_from_db()
        state['tx_records']['independent-paid']=dict(tx_id='independent-paid',user_id=7,bot_id='flow-a',
            bot_token=self.bot.token,status='paid',amount=10,created_at=Clock.utcnow().isoformat(),paid_at=Clock.utcnow().isoformat())
        await main._write_state_to_db(state)
        await self.cold()
        await self.gate.wake();await asyncio.wait_for(task,3)
        self.assertEqual(self.messages(),[])
        self.assertFalse(task.cancelled())

    async def test_live_selected_loop_reorder_then_empty_loop_finishes_without_spin(self):
        await self.config([self.seq('A',loop_enabled=True),self.seq('B'),self.seq('C',loop_enabled=True)])
        await self.start();await self.until(['A','B','C'])
        state=await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general']['sequences']=[self.seq('C',loop_enabled=True),self.seq('B',loop_enabled=True)]
        await main._write_state_to_db(state)
        await self.until(['A','B','C','C','B'])
        state=await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general']['sequences']=[self.seq('C'),self.seq('B')]
        await main._write_state_to_db(state)
        task=self.manager.downsell_tasks['111:7']
        await self.step();await asyncio.wait_for(task,3)
        self.assertEqual(self.messages(),['A','B','C','C','B'])

    async def test_restart_pix_only_does_not_invent_start_anchor(self):
        pix=self.seq('P',loop_enabled=True);pix['trigger']='pix'
        await self.config([pix]);await self.checkout();await self.until(['P'])
        task=self.manager.downsell_tasks['111:7'];task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.manager.downsell_tasks.clear()
        saved=await self.cold();self.manager._resume_flow_downsells(saved)
        await self.until(['P','P'])
        anchors=(await self.cold())['memory']['leads'][0]['flow_downsell']['anchors']
        self.assertEqual(set(anchors),{'pix'})

    async def paid_reentry_held_text(self, permanent):
        await self.config([self.seq('A',loop_enabled=True)])
        await self.start();await self.gate.held()
        await self.checkout()
        txid=self.created[0][0]
        reached,finish=asyncio.Event(),asyncio.Event()
        async def send(**kw):
            if kw['text']=='A':
                reached.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    await finish.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect=send
        old=self.manager.downsell_tasks['111:7']
        old_created=(await main._read_state_from_db())['memory']['leads'][0]['created_at']
        await self.gate.wake();await asyncio.wait_for(reached.wait(),3)
        response=await self.client.delete('/api/leads/7?bot_id=flow-a');self.assertEqual(response.status_code,200)
        if permanent:
            response=await self.client.delete('/api/leads/7/permanent?bot_id=flow-a');self.assertEqual(response.status_code,200)
        self.assertEqual((await self.cold())['memory']['leads'],[])
        Clock.value+=timedelta(seconds=1)
        # Exclusion explicitly cancels old Pix (existing archived-leads
        # contract). Paid REENTRY is a newly admitted checkout, not settlement
        # of that intentionally cancelled/deleted record.
        await self.start()
        await self.checkout()
        txid=self.created[-1][0]
        self.gateway_status[txid]='paid'
        response=await self.client.post('/api/webhook/syncpay',json={'id':txid,'status':'paid'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['action'],'approved')
        before=copy.deepcopy((await self.cold())['memory'])
        self.assertEqual(len(before['leads']),1)
        self.assertNotEqual(before['leads'][0]['created_at'],old_created)
        self.assertEqual(before['leads'][0].get('flow_downsell',{}).get('sent',[]),[])
        finish.set();await asyncio.wait_for(old,3)
        after=(await self.cold())['memory']
        self.assertEqual(after,before,'Paid reentry must not admit old worker completion')
        self.assertTrue((await self.cold())['tx_records'][txid]['delivered'])

    async def test_archive_paid_reentry_with_real_held_text_worker_cannot_resurrect_active(self):
        await self.paid_reentry_held_text(False)

    async def test_permanent_delete_paid_reentry_with_real_held_text_worker_preserves_new_incarnation(self):
        await self.paid_reentry_held_text(True)

    async def test_real_configure_start_restores_durable_checkpoint_without_live_polling(self):
        await self.config([self.seq('A'),self.seq('B',2,loop_enabled=True)])
        await self.start();await self.until(['A'])
        old=self.manager.downsell_tasks['111:7'];old.cancel();await asyncio.gather(old,return_exceptions=True)
        self.manager.downsell_tasks.clear()
        state=await self.cold()
        state['publish'].update(bot_token=self.bot.token,is_live=True)
        await main._write_state_to_db(state)
        self.bot.session=SimpleNamespace(close=AsyncMock(return_value=None))
        self.bot.get_me=AsyncMock(return_value=SimpleNamespace(id=111,username='offline',first_name='Offline'))
        with patch.object(service,'_create_bot_instance',return_value=self.bot),patch.object(self.manager,'_run_polling',new=AsyncMock(return_value=None)),patch.object(self.manager,'_sync_extra_bots',new=AsyncMock(return_value=None)),patch.object(self.manager,'_delivery_retry_loop',new=AsyncMock(return_value=None)):
            result=await self.manager.configure_and_start(main._read_state_from_db,main._write_state_to_db)
            self.assertEqual(result['status'],'running')
            self.assertIn('111:7',self.manager.downsell_tasks)
            await self.until(['A','B'])
            self.assertEqual(self.messages().count('A'),1)
            self.assertEqual(self.manager.pix_poll_tasks,{})
            await asyncio.gather(self.manager.polling_task,self.manager._delivery_retry_task)
