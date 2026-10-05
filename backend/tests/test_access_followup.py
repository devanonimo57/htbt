"""Eight focused offline contracts for F01/F02 (no inherited test IDs)."""
import asyncio, copy, json, os, subprocess, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Dispatcher
import main, telegram_service
import test_seven_frontend_contracts as fixtures

REPORT = Path(os.environ.get('HOTBOT_REPORT') or __import__('tempfile').mkdtemp(prefix='access-contract-'))
REPORT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parents[2]

class AccessFollowup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._STATE_COMMITTED = {}; main._STATE_COMMITTED_OWNER = None
        await fixtures.SevenFrontendContracts.asyncSetUp(self)
        self.bot.id = 111
        self.banned = set(); self.joined = set(); self.calls = []; self.links = {}
        self.serial = 0
        async def ban(chat_id, user_id, **kwargs):
            key = (str(chat_id), user_id)
            self.calls.append(('ban', key, kwargs)); self.banned.add(key); self.joined.discard(key)
            return True
        async def unban(chat_id, user_id, **kwargs):
            key = (str(chat_id), user_id)
            self.calls.append(('unban', key, kwargs))
            # Bot API: default unban can remove an existing member. only_if_banned
            # prevents that; ban blocks invites until unban is confirmed.
            if not kwargs.get('only_if_banned'): self.joined.discard(key)
            self.banned.discard(key)
            return True
        async def invite(chat_id, **kwargs):
            self.serial += 1; url = f'https://example.invalid/invite/{self.serial}'
            self.links[url] = str(chat_id); self.calls.append(('invite', str(chat_id), kwargs))
            return SimpleNamespace(invite_link=url)
        async def send(**kwargs):
            self.calls.append(('send', kwargs['chat_id'], kwargs.get('reply_markup')))
            return SimpleNamespace(message_id=len(self.calls))
        self.bot.ban_chat_member = AsyncMock(side_effect=ban)
        self.bot.unban_chat_member = AsyncMock(side_effect=unban)
        self.bot.create_chat_invite_link = AsyncMock(side_effect=invite)
        self.bot.send_message = AsyncMock(side_effect=send)
        state = copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['plans'][0].update(delivery_dest_type='Canal ou grupo', delivery_channel_id='-100800')
        await main._write_state_to_db(state)
        self.manager.dp = Dispatcher(); self.manager._register_handlers()
    asyncTearDown = fixtures.SevenFrontendContracts.asyncTearDown
    cold = fixtures.SevenFrontendContracts.cold
    async def purchase(self, delivered=True):
        handler = next(h.callback for h in self.manager.dp.callback_query.handlers if h.callback.__name__ == 'handle_flow_plan_click')
        callback = SimpleNamespace(from_user=self.user, bot=self.bot, message=self.callback.message,
                                   data='fplan:plan-a:0', answer=AsyncMock())
        await handler(callback)
        tx = self.created[-1][0]; self.gateway_status[tx] = 'paid'
        response = await self.client.post('/api/webhook/syncpay', json={'id':tx, 'status':'paid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['action'], 'approved' if delivered else 'unknown_or_already_delivered_tx', response.text)
        if delivered: self.assertTrue((await self.cold())['tx_records'][tx]['delivered'])
        return tx
    async def remove(self, tx):
        response = await self.client.post('/api/flow-bots/access/update-status', json={'tx_id':tx, 'access_status':'Removido', 'status':'Removido'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.cold())['tx_records'][tx]['access_status'], 'Removido')
        self.assertIn(('-100800',7), self.banned)
    async def resend(self, tx):
        response = await self.client.post('/api/flow-bots/access/resend', json={'tx_id':tx, 'tx_ids':[tx], 'group_id':'all', 'plan_name':'all'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()
    def consume(self, url):
        key = (self.links[url], 7)
        if key in self.banned: return False
        self.joined.add(key); return True
    async def test_01_removed_manual_resend_restores_usable_access(self):
        tx = await self.purchase(); await self.remove(tx); self.calls.clear()
        result = await self.resend(tx)
        rec = (await self.cold())['tx_records'][tx]
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual(rec['access_status'], 'Ativo')
        self.assertTrue(self.consume(rec['invite_url']), 'Ativo must admit the previously banned customer')
        self.assertEqual(self.calls[0], ('unban', ('-100800',7), {'only_if_banned':True}))
        self.assertEqual([c[0] for c in self.calls], ['unban','invite','send'])
        self.factory_mock.assert_called_with('111:test-only')
    async def test_02_removed_new_paid_pix_restores_access_counts_twice(self):
        first = await self.purchase(); await self.remove(first); self.calls.clear()
        second = await self.purchase()
        saved = await self.cold(); rec = saved['tx_records'][second]
        self.assertNotEqual(first, second)
        self.assertEqual(rec['access_status'], 'Ativo')
        self.assertTrue(self.consume(rec['invite_url']), 'New paid Pix must admit the banned customer')
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 2)
        self.assertEqual((await main.get_panel_metrics(period='Tudo', bot_id='flow-a'))['paid_count'], 2)
        self.assertEqual(self.calls[-3][0], 'unban')
        before = len(self.calls)
        for tx in (first, second):
            response = await self.client.post('/api/webhook/syncpay', json={'id':tx, 'status':'paid'})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls), before)
        saved = await self.cold()
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 2)
        self.assertEqual(saved['tx_records'][first]['access_status'], 'Removido')
    async def test_03_manual_unban_false_or_exception_fails_closed(self):
        tx = await self.purchase(); await self.remove(tx)
        for mode in ('false', 'exception'):
            with self.subTest(mode=mode):
                self.bot.unban_chat_member = AsyncMock(return_value=False) if mode == 'false' else AsyncMock(side_effect=RuntimeError('denied unban'))
                self.bot.create_chat_invite_link.reset_mock(); self.bot.send_message.reset_mock()
                result = await self.resend(tx); rec = (await self.cold())['tx_records'][tx]
                self.assertEqual(result['sent_count'], 0)
                self.assertEqual(rec['access_status'], 'Removido')
                self.assertTrue(rec['delivered'])  # Preserve the previous paid receipt, not a new success.
                self.assertTrue(rec['delivery_error'])
                self.bot.create_chat_invite_link.assert_not_awaited(); self.bot.send_message.assert_not_awaited()
                self.assertIn(('-100800',7), self.banned)
    async def retry_tick(self, offset):
        from datetime import datetime, timedelta
        class AdvancedClock(datetime):
            @classmethod
            def utcnow(cls): return datetime.utcnow() + timedelta(seconds=offset)
            @classmethod
            def now(cls, tz=None): return datetime.now(tz) + timedelta(seconds=offset)
        original_sleep = asyncio.sleep; ticks = 0
        async def sleep(delay, *args, **kwargs):
            nonlocal ticks
            if delay == 45:
                ticks += 1
                if ticks > 1: raise asyncio.CancelledError()
                return
            return await original_sleep(delay, *args, **kwargs)
        with patch.object(telegram_service, 'datetime', AdvancedClock), patch.object(telegram_service.asyncio, 'sleep', sleep):
            await self.manager._delivery_retry_loop()
    async def test_04_auto_unban_failure_paid_not_delivered_bounded_retry(self):
        first = await self.purchase(); await self.remove(first)
        self.bot.unban_chat_member = AsyncMock(side_effect=RuntimeError('denied unban'))
        self.bot.create_chat_invite_link.reset_mock()
        second = await self.purchase(delivered=False)
        rec = (await self.cold())['tx_records'][second]
        self.assertEqual(rec['status'], 'paid'); self.assertTrue(rec['sales_counted'])
        self.assertFalse(rec['delivered']); self.assertNotEqual(rec['access_status'], 'Ativo')
        self.assertTrue(rec['delivery_error']); self.assertEqual(rec['automatic_delivery_attempts'], 1)
        self.bot.create_chat_invite_link.assert_not_awaited()
        self.bot.unban_chat_member = AsyncMock(return_value=False)
        await self.retry_tick(60)
        saved = await self.cold(); rec = saved['tx_records'][second]
        self.assertEqual(rec['automatic_delivery_attempts'], 2); self.assertFalse(rec['delivered'])
        self.assertTrue(saved['tx_records'][first]['delivered'])
        before = self.bot.unban_chat_member.await_count
        await self.retry_tick(120)
        self.assertEqual(self.bot.unban_chat_member.await_count, before)
        self.bot.create_chat_invite_link.assert_not_awaited()
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 2)
        self.assertFalse(any(c[0] == 'send' and c[2] is not None for c in self.calls[-1:]))
    async def test_05_existing_member_not_expelled_external_destination_untouched(self):
        with self.subTest(path='automatic'):
            self.joined.add(('-100800',7)); tx = await self.purchase()
            self.assertIn(('-100800',7), self.joined)
        with self.subTest(path='manual'):
            await self.resend(tx)
            self.assertIn(('-100800',7), self.joined)
            self.assertTrue(all(c[2] == {'only_if_banned':True} for c in self.calls if c[0] == 'unban'))
        with self.subTest(path='external'):
            state = copy.deepcopy(await main._read_state_from_db())
            state['flow_bots'][0]['plans'][0].update(delivery_dest_type='Link externo', delivery_external_url='https://example.invalid/content')
            await main._write_state_to_db(state)
            self.bot.unban_chat_member.reset_mock(); self.bot.create_chat_invite_link.reset_mock()
            external = await self.purchase(); result = await self.resend(external)
            self.assertEqual(result['sent_count'], 1)
            self.bot.unban_chat_member.assert_not_awaited(); self.bot.create_chat_invite_link.assert_not_awaited()
            self.assertEqual((await self.cold())['tx_records'][external]['invite_url'], 'https://example.invalid/content')
        with self.subTest(path='partial_additional_manual'):
            state = copy.deepcopy(await main._read_state_from_db())
            state['flow_bots'][0]['plans'][0].update(delivery_dest_type='Canal ou grupo', delivery_channel_id='-100800')
            await main._write_state_to_db(state)
            # The purchased primary was accepted, but its additional failed.
            handler = next(h.callback for h in self.manager.dp.callback_query.handlers if h.callback.__name__ == 'handle_flow_plan_click')
            await handler(SimpleNamespace(from_user=self.user, bot=self.bot, message=self.callback.message,
                                          data='fplan:plan-a:0', answer=AsyncMock()))
            partial = self.created[-1][0]
            state = copy.deepcopy(await main._read_state_from_db())
            state['tx_records'][partial]['deliverable'] = 'Purchased additional'
            await main._write_state_to_db(state)
            self.bot.send_message.side_effect = [SimpleNamespace(message_id=1), RuntimeError('additional failed'), RuntimeError('additional failed')]
            self.gateway_status[partial] = 'paid'
            await self.client.post('/api/webhook/syncpay', json={'id':partial,'status':'paid'})
            before = (await self.cold())['tx_records'][partial]
            self.assertTrue(before['primary_access_delivered']); self.assertFalse(before['delivered'])
            self.bot.send_message.reset_mock(side_effect=True); self.bot.send_message.return_value = SimpleNamespace(message_id=2)
            self.bot.unban_chat_member = AsyncMock(side_effect=RuntimeError('channel unavailable for unused invite'))
            self.bot.create_chat_invite_link.reset_mock()
            result = await self.resend(partial)
            self.assertEqual(result['sent_count'], 1)
            saved = (await self.cold())['tx_records'][partial]
            self.assertEqual(saved['invite_url'], before['invite_url']); self.assertTrue(saved['additional_delivered'])
            self.bot.unban_chat_member.assert_not_awaited(); self.bot.create_chat_invite_link.assert_not_awaited()
            self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_06_two_peers_captured_target_and_identity_changes(self):
        tx = await self.purchase(); await self.remove(tx)
        peer = SimpleNamespace(token='222:test-only', id=222, unban_chat_member=AsyncMock(return_value=True),
                               create_chat_invite_link=AsyncMock(), send_message=AsyncMock())
        self.manager.extra_bots[peer.token] = peer
        original_unban = self.bot.unban_chat_member
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                reached, finish = asyncio.Event(), asyncio.Event()
                async def held(**kwargs):
                    reached.set(); await finish.wait(); return await original_unban(**kwargs)
                self.bot.unban_chat_member = AsyncMock(side_effect=held)
                self.bot.create_chat_invite_link.reset_mock(); self.bot.send_message.reset_mock()
                task = asyncio.create_task(self.resend(tx))
                try:
                    await asyncio.wait_for(reached.wait(), 5)
                    edited = (await self.client.get('/api/state')).json()
                    edited['flow_bots'][0]['plans'][0]['delivery_channel_id'] = '-100999'
                    if replacement: edited['flow_bots'][0].update(token='333:test-only', bot_id='333')
                    response = await self.client.post('/api/state', json=edited)
                    self.assertEqual(response.status_code, 200, response.text)
                    finish.set(); result = await asyncio.wait_for(task, 5)
                finally:
                    finish.set(); await asyncio.gather(task, return_exceptions=True)
                self.bot.unban_chat_member.assert_awaited_once_with(chat_id=-100800, user_id=7, only_if_banned=True)
                if replacement:
                    self.assertEqual(result['sent_count'], 0)
                    self.bot.create_chat_invite_link.assert_not_awaited(); self.bot.send_message.assert_not_awaited()
                else:
                    self.assertEqual(result['sent_count'], 1)
                    self.assertEqual(str(self.bot.create_chat_invite_link.await_args.kwargs['chat_id']), '-100800')
                saved = await self.cold()
                self.assertEqual(saved['flow_bots'][0]['plans'][0]['delivery_channel_id'], '-100999')
                self.assertEqual(saved['flow_bots'][1]['token'], peer.token)
        peer.unban_chat_member.assert_not_awaited(); peer.send_message.assert_not_awaited()
        with self.subTest(path='wrong_transport'):
            edited = (await self.client.get('/api/state')).json()
            edited['flow_bots'][0].update(token='111:test-only', bot_id='111')
            response = await self.client.post('/api/state', json=edited)
            self.assertEqual(response.status_code, 200, response.text)
            self.factory_mock.return_value = peer
            result = await self.resend(tx)
            self.assertEqual(result['sent_count'], 0)
            peer.unban_chat_member.assert_not_awaited()
    def node_contract(self, mode, contract):
        source = REPORT / f'access-{mode}-input.json'; output = REPORT / f'access-{mode}-output.json'
        source.write_text(json.dumps(contract, ensure_ascii=False), encoding='utf8')
        result = subprocess.run(['node', str(ROOT/'frontend/tests/access_followup_contract.cjs'), mode, str(source), str(output)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(output.read_text(encoding='utf8'))
    async def frontend_contract(self, mode):
        tx = await self.purchase()
        state = copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['welcome']['medias'] = [{'id':'delete-media','type':'photo','url':'https://example.invalid/old'}]
        await main._write_state_to_db(state)
        initial = (await self.client.get('/api/state')).json()
        contract = {'initial':initial, 'tx_id':tx}
        request = self.node_contract('request', contract)
        self.assertEqual(request['url'], '/api/flow-bots/access/resend')
        self.assertEqual(request['body'], {'tx_id':tx,'tx_ids':[tx],'group_id':'all','plan_name':'all'})
        response = await self.client.post('/api/flow-bots/leads/action', json={
            'user_id':'7', 'bot_id':'flow-a', 'origin_bot_numeric_id':'111',
            'action':'send_message', 'message':'RUNTIME WHILE RESEND'})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.request(request['method'], request['url'], json=request['body'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['sent_count'], 1)
        contract.update(action=response.json(), http_status=response.status_code)
        observed = self.node_contract(mode, contract)
        saved = await self.client.post('/api/state', json=observed['post'])
        self.assertEqual(saved.status_code, 200, saved.text)
        cold = await self.cold()
        self.assertEqual(cold['tx_records'][tx]['manual_delivery_attempts'], 1)
        self.assertEqual(cold['tx_records'][tx]['access_status'], 'Ativo')
        return cold, contract
    async def test_07_real_resend_response_preserves_late_edits_next_save(self):
        cold, contract = await self.frontend_contract('late')
        self.assertEqual(cold['flow_bots'][0]['welcome']['message'], 'NEW WELCOME WHILE RESEND')
        self.assertEqual(cold['publish']['gateway_secret'], 'NEW LOCAL TEST SECRET')
        self.assertFalse(cold['flow_bots'][0]['welcome']['medias'])
        self.assertFalse(cold['flow_bots'][1]['plans'])
    async def test_08_ordinary_resend_fresh_runtime_no_dirty_error_preserves_edit(self):
        cold, contract = await self.frontend_contract('ordinary')
        self.assertEqual(cold['flow_bots'][0]['welcome']['message'], 'ORIGINAL')
        self.node_contract('error', contract)

