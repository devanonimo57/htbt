"""Checkout boundary regressions with isolated durable rows."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch
import main
import telegram_service
import test_checkout_webhook_integration as fixture

class SevenCheckoutBoundaries(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def test_checkout_unverified_or_mismatched_origin_cannot_cancel_peer_tasks(self):
        state = copy.deepcopy(await main._read_state_from_db())
        with patch.object(self.manager, '_cancel_downsell') as cancel:
            for token in ('***configured***', '222:fake-secondary'):
                callback = SimpleNamespace(from_user=self.user, bot=SimpleNamespace(token=token))
                await self.manager._generate_flow_checkout(callback, state['flow_bots'][0], state, 'plan-a', 0, [])
            cancel.assert_not_called()
        self.assertFalse(self.created)
        self.bot.send_message.assert_not_awaited()
        main._STATE_CACHE = None
        self.assertFalse((await main._read_state_from_db())['tx_records'])

    def gate_gateway(self, total):
        original = telegram_service.PushinPayClient
        reached, finish = asyncio.Event(), asyncio.Event()
        arrived = 0
        def client(*a, **kw):
            underlying = original(*a, **kw)
            async def create(**charge):
                nonlocal arrived
                response = await underlying.create_pix_charge(**charge)
                arrived += 1
                if arrived == total:
                    reached.set()
                await finish.wait()
                return response
            return SimpleNamespace(create_pix_charge=create)
        return patch.object(telegram_service, 'PushinPayClient', new=client), reached, finish

    async def test_simultaneous_checkout_retains_two_distinct_pending_histories(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state, runtime_only=True)
        snapshot = copy.deepcopy(await main._read_state_from_db())
        gate, reached, finish = self.gate_gateway(2)
        tasks = []
        with gate:
            try:
                for _ in range(2):
                    tasks.append(asyncio.create_task(self.manager._generate_flow_checkout(
                        self.callback, snapshot['flow_bots'][0], copy.deepcopy(snapshot), 'plan-a', 0, [])))
                await asyncio.wait_for(reached.wait(), 5)
                finish.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            finally:
                finish.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        checkout_history = [h for h in saved['memory']['leads'][0]['history'] if '[PIX Flow' in h['content']]
        self.assertEqual(len(saved['tx_records']), 2)
        self.assertEqual(len(checkout_history), 2)
        self.assertEqual(len(self.created), 2)
        for tx_id, _ in self.created:
            self.assertTrue(any('PIX-' + tx_id in c.kwargs['text'] for c in self.bot.send_message.call_args_list))
        self.assertEqual(len({h['tx_id'] for h in checkout_history}), 2)

    async def test_checkout_finishing_after_archive_keeps_pix_without_active_lead(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state, runtime_only=True)
        state = copy.deepcopy(await main._read_state_from_db())
        gate, reached, finish = self.gate_gateway(1)
        with gate:
            task = asyncio.create_task(self.manager._generate_flow_checkout(
                self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                await main.delete_lead('7', bot_id='flow-a')
                main._STATE_CACHE = None
                deleted = await main._read_state_from_db()
                self.assertFalse(deleted['memory']['leads'])
                archived = copy.deepcopy(deleted['memory']['deleted_leads'])
                finish.set()
                await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        active = [l for l in saved['memory']['leads'] if l['telegram_id'] == '7']
        self.assertEqual(len(active), 0)
        self.assertEqual(saved['memory']['deleted_leads'][0]['first_name'], archived[0]['first_name'])
        self.assertEqual(saved['memory']['deleted_leads'][0]['history'][-1]['tx_id'], self.created[0][0])
        self.assertEqual(len(saved['tx_records']), 1)
        self.assertTrue(any('PIX-' + self.created[0][0] in c.kwargs['text'] for c in self.bot.send_message.call_args_list))
    async def test_archived_checkout_actual_scheduled_worker_next_stage_does_not_recreate(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        state['flow_bots'][0]['downsell_pix'] = {'enabled': True, 'sequences': [
            {'id': 's1', 'delay_minutes': 1, 'message': 'FOLLOWUP', 'medias': []}]}
        await main._write_state_to_db(state)
        state = copy.deepcopy(await main._read_state_from_db())
        gate, reached, finish = self.gate_gateway(1)
        self.manager._schedule_flow_downsell = type(self.manager)._schedule_flow_downsell.__get__(self.manager)
        self.manager._get_bot_by_token = lambda token: self.bot
        real_sleep = asyncio.sleep
        sleep_reached, next_stage = asyncio.Event(), asyncio.Event()
        async def fake_sleep(delay):
            if delay >= 60:
                sleep_reached.set()
                await next_stage.wait()
            else:
                await real_sleep(0)
        with gate, patch.object(telegram_service.asyncio, 'sleep', new=fake_sleep):
            task = asyncio.create_task(self.manager._generate_flow_checkout(
                self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                await main.delete_lead('7', bot_id='flow-a')
                finish.set()
                await asyncio.wait_for(task, 5)
                await asyncio.wait_for(sleep_reached.wait(), 5)
                worker = self.manager.downsell_tasks['111:7']
                next_stage.set()
                await asyncio.wait_for(worker, 5)
            finally:
                finish.set()
                next_stage.set()
                await asyncio.gather(task, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertFalse(saved['memory']['leads'])
        self.assertEqual(len(saved['memory']['deleted_leads']), 1)
        self.assertEqual(len(saved['tx_records']), 1)
        self.assertFalse(any(c.kwargs.get('text') == 'FOLLOWUP' for c in self.bot.send_message.call_args_list))

    async def test_permanently_deleted_during_checkout_keeps_pix_not_lead(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state)
        state = copy.deepcopy(await main._read_state_from_db())
        gate, reached, finish = self.gate_gateway(1)
        with gate:
            task = asyncio.create_task(self.manager._generate_flow_checkout(
                self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                await main.delete_lead('7', bot_id='flow-a')
                await main.permanently_delete_lead('7', bot_id='flow-a')
                finish.set()
                await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertFalse(saved['memory']['leads'])
        self.assertFalse(saved['memory']['deleted_leads'])
        self.assertEqual(len(saved['tx_records']), 1)
        self.assertEqual(next(iter(saved['tx_records'].values()))['status'], 'pending')

    async def test_failed_checkout_commit_exposes_no_qr_or_pending_cache(self):
        async def failing(st, **kwargs):
            if kwargs.get('checkout_event'):
                raise RuntimeError('isolated commit failure')
            return await main._write_state_to_db(st, **kwargs)
        self.manager.state_saver = failing
        state = await main._read_state_from_db()
        with self.assertRaisesRegex(RuntimeError, 'isolated commit failure'):
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.manager.tx_records)
        self.assertFalse(self.manager.active_tx_map)
        main._STATE_CACHE = None
        self.assertFalse((await main._read_state_from_db())['tx_records'])

    async def test_checkout_reentry_preserves_current_profile_insights_and_both_histories(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state)
        state = copy.deepcopy(await main._read_state_from_db())
        gate, reached, finish = self.gate_gateway(1)
        with gate:
            task = asyncio.create_task(self.manager._generate_flow_checkout(
                self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                await main.delete_lead('7', bot_id='flow-a')
                fresh = copy.deepcopy(await main._read_state_from_db())
                lead = await self.manager._get_or_create_lead(fresh, self.user, bot_token=self.bot.token)
                lead.update(first_name='Reentered profile', preferences='New custom insight', prefs_list=['New preference'],
                            objections='New custom objection', objs_list=['New objection'], stage='Entrega concluída')
                lead['history'] = [{'role':'user','content':'NEW REENTRY MESSAGE'}]
                fresh['memory']['deleted_leads'][0]['history'].append({'role':'user','content':'ARCHIVED HISTORY'})
                await main._write_state_to_db(fresh)
                finish.set()
                await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        lead = saved['memory']['leads'][0]
        self.assertEqual(lead['first_name'],'Reentered profile')
        self.assertEqual(lead['preferences'],'New custom insight')
        self.assertEqual(lead['objections'],'New custom objection')
        self.assertEqual(lead['prefs_list'],['New preference'])
        self.assertEqual(lead['objs_list'],['New objection'])
        self.assertEqual(lead['stage'],'Entrega concluída')
        self.assertEqual(lead['history'][0]['content'],'NEW REENTRY MESSAGE')
        self.assertEqual(lead['history'][-1]['tx_id'],self.created[0][0])
        self.assertEqual(saved['memory']['deleted_leads'][0]['history'][-1]['content'],'ARCHIVED HISTORY')
        self.assertEqual(len(saved['tx_records']),1)

    async def ordered_checkouts(self, reverse):
        state = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        lead['history'] = [{'role': 'assistant', 'content': 'Completed earlier sale'}]
        await main._write_state_to_db(state)
        state = copy.deepcopy(await main._read_state_from_db())
        original = telegram_service.PushinPayClient
        gates = [asyncio.Event(), asyncio.Event()]
        reached = asyncio.Event()
        arrivals = 0
        events = []
        async def saver(st, **kwargs):
            if kwargs.get('checkout_event'):
                events.append(copy.deepcopy(kwargs['checkout_event']))
            return await main._write_state_to_db(st, **kwargs)
        self.manager.state_saver = saver
        def client(*a, **kw):
            underlying = original(*a, **kw)
            async def create(**charge):
                nonlocal arrivals
                idx = arrivals
                arrivals += 1
                response = await underlying.create_pix_charge(**charge)
                if arrivals == 2:
                    reached.set()
                await gates[idx].wait()
                return response
            return SimpleNamespace(create_pix_charge=create)
        tasks = []
        with patch.object(telegram_service, 'PushinPayClient', new=client):
            try:
                for _ in range(2):
                    tasks.append(asyncio.create_task(self.manager._generate_flow_checkout(
                        self.callback, state['flow_bots'][0], copy.deepcopy(state), 'plan-a', 0, [])))
                await asyncio.wait_for(reached.wait(), 5)
                fresh = copy.deepcopy(await main._read_state_from_db())
                fresh['memory']['leads'][0].update(preferences='NEW preferences', first_name='NEW profile', stage='Entrega concluída')
                fresh['flow_bots'][0].update(sales_today=7, revenue_today=70)
                fresh['tx_records']['older-paid'] = {'tx_id': 'older-paid', 'status': 'paid', 'sales_counted': True,
                    'delivered': True, 'automatic_delivery_attempts': 2, 'invite_url': 'https://example.invalid/invite',
                    'bot_id': 'flow-a', 'bot_token': self.bot.token, 'user_id': 7, 'amount': 10}
                await main._write_state_to_db(fresh)
                for idx in ([1, 0] if reverse else [0, 1]):
                    gates[idx].set()
                    await asyncio.wait_for(tasks[idx], 5)
                await main._write_state_to_db(state, runtime_only=True, checkout_event=events[0])
            finally:
                for gate in gates:
                    gate.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        lead = saved['memory']['leads'][0]
        self.assertEqual(len(lead['history']), 3)
        self.assertEqual(len({h['tx_id'] for h in lead['history'] if h.get('tx_id')}), 2)
        self.assertEqual(lead['preferences'], 'NEW preferences')
        self.assertEqual(lead['first_name'], 'NEW profile')
        self.assertEqual(lead['stage'], 'Entrega concluída')
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 7)
        self.assertEqual(saved['flow_bots'][0]['revenue_today'], 70)
        self.assertEqual(saved['tx_records']['older-paid']['automatic_delivery_attempts'], 2)
        self.assertTrue(saved['tx_records']['older-paid']['delivered'])
        self.assertEqual(len(saved['tx_records']), 3)

    async def test_simultaneous_checkout_forward_order_preserves_newer_runtime(self):
        await self.ordered_checkouts(False)

    async def test_simultaneous_checkout_reverse_order_preserves_newer_runtime(self):
        await self.ordered_checkouts(True)

    async def test_checkout_on_a_preserves_b_downsell_and_reschedules_a(self):
        state = copy.deepcopy(await main._read_state_from_db())
        other = copy.deepcopy(state['flow_bots'][0])
        other.update(id='flow-b', token='222:fake-secondary', bot_id='222')
        state['flow_bots'].append(other)
        await main._write_state_to_db(state)
        blocked = asyncio.Event()
        async def waiting_worker(*a, **kw):
            await blocked.wait()
        self.manager._flow_downsell_worker = waiting_worker
        self.manager._schedule_flow_downsell = type(self.manager)._schedule_flow_downsell.__get__(self.manager)
        tasks = []
        try:
            self.manager._schedule_flow_downsell(self.user, bot_token=other['token'])
            task_b = self.manager.downsell_tasks['222:7']
            tasks.append(task_b)
            self.manager._schedule_flow_downsell(self.user, bot_token=self.bot.token)
            task_a = self.manager.downsell_tasks['111:7']
            tasks.append(task_a)
            await asyncio.sleep(0)
            self.assertFalse(task_b.done())
            self.assertFalse(task_a.done())
            current = copy.deepcopy(await main._read_state_from_db())
            await self.manager._generate_flow_checkout(self.callback, current['flow_bots'][0], current, 'plan-a', 0, [])
            await asyncio.sleep(0)
            tasks.extend(self.manager.downsell_tasks.values())
            self.assertFalse(task_b.done())
            self.assertIs(self.manager.downsell_tasks['222:7'], task_b)
            self.assertIn('111:7', self.manager.downsell_tasks)
            self.assertTrue(task_a.cancelled())
        finally:
            blocked.set()
            for task in set(tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*set(tasks), return_exceptions=True)

