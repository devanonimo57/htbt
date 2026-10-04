"""R2-02-REENTRY: active and archived incarnations retain independent rows."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import main
from models import BotConfig
import test_checkout_late_commit as late_fixture
import test_checkout_webhook_integration as fixture


class CheckoutReentryCommit(unittest.IsolatedAsyncioTestCase):
    # Reuse real offline setup/barriers without collecting inherited test cases.
    seed = late_fixture.CheckoutLateCommit.seed
    event = late_fixture.CheckoutLateCommit.event
    held = late_fixture.CheckoutLateCommit.held

    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def asyncTearDown(self):
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    async def establish_reentry(self):
        live = await self.seed()
        live['memory']['leads'][0].update(
            created_at='2026-10-01T12:00:00', last_tx_id='old-checkout',
            flow_plan_id='old-plan', chosen_plan='OLD PLAN', flow_plan_price=5,
            history=[{'role': 'user', 'content': 'OLD ARCHIVED HISTORY'},
                     {'role': 'assistant', 'tx_id': 'old-checkout', 'content': 'OLD QR'}])
        await main._write_state_to_db(live, runtime_only=True)
        await main.delete_lead('7', bot_id='flow-a')
        state = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        lead.update(created_at='2026-10-04T12:00:00', first_name='REENTRY',
                    history=[{'role': 'user', 'content': 'NEW ACTIVE HISTORY'}])
        await main._write_state_to_db(state, runtime_only=True)
        live = await main._read_state_from_db()
        self.assertEqual(len(live['memory']['leads']), 1)
        self.assertEqual(len(live['memory']['deleted_leads']), 1)
        self.assertNotEqual(live['memory']['leads'][0]['created_at'],
                            live['memory']['deleted_leads'][0]['created_at'])
        return live

    async def assert_exact_sql_baseline(self, returned):
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().one()
            sql = await main._hydrate_records(session, json.loads(config.full_state_json))
        self.assertEqual(main._client_map(sql), main._client_map(returned._row_base))
        self.assertEqual(sql['tx_records'], returned._row_base['tx_records'])
        self.assertEqual(main._STATE_COMMITTED, returned._row_base)
        return sql

    async def registered_checkout(self, mutate=None, *, fail=False, captured=None):
        self.manager.dp = Dispatcher()
        self.manager._register_handlers()
        handler = next(h.callback for h in self.manager.dp.callback_query.handlers
                       if h.callback.__name__ == 'handle_flow_plan_click')
        captured = {} if captured is None else captured
        async def saver(state, **kwargs):
            if not kwargs.get('checkout_event'):
                return await main._write_state_to_db(state, **kwargs)
            live = await main._read_state_from_db()
            captured['live'] = live
            captured['before_baseline'] = copy.deepcopy(live._row_base)
            returned = await self.held(live, kwargs, mutate or (lambda state: None), fail=fail)
            captured['returned'] = returned
            captured['event'] = copy.deepcopy(kwargs['checkout_event'])
            return returned
        self.manager.state_saver = saver
        callback = SimpleNamespace(from_user=self.user, bot=self.bot, message=self.callback.message,
                                   data='fplan:plan-a:0', answer=AsyncMock())
        await handler(callback)
        return captured['returned'], captured['event']

    async def test_registered_callback_reentry_late_message_preserves_checkout_and_archive(self):
        live = await self.establish_reentry()
        archive = copy.deepcopy(live['memory']['deleted_leads'])
        pending = {'role': 'user', 'content': 'LATE ACTIVE MESSAGE'}
        def mutate(state):
            state['memory']['leads'][0]['history'].append(pending)
        returned, event = await self.registered_checkout(mutate)
        txid = event['record']['tx_id']
        sql = await self.assert_exact_sql_baseline(returned)
        sql_active = sql['memory']['leads'][0]
        active = returned['memory']['leads'][0]
        self.assertEqual(sql_active['history'][-1]['tx_id'], txid)
        self.assertNotIn(pending, sql_active['history'])
        self.assertIn(pending, active['history'])
        for field in ('last_tx_id', 'flow_plan_id', 'flow_plan_price', 'chosen_plan',
                      'stage', 'stage_history', 'prefs_list', 'objs_list', 'preferences', 'objections'):
            self.assertEqual(active.get(field), sql_active.get(field), field)
        self.assertEqual(active['stage'], 'Gerou checkout')
        self.assertEqual(active['last_tx_id'], txid)
        self.assertEqual(active['history'], sql_active['history'] + [pending])
        self.assertEqual(returned['memory']['deleted_leads'], archive)
        self.assertEqual(sql['memory']['deleted_leads'], archive)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn(f'PIX-{txid}', str(self.bot.send_message.await_args))
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'][0], active)
        self.assertEqual(cold['memory']['deleted_leads'], archive)
        self.assertEqual(cold['tx_records'][txid]['status'], 'pending')

    async def test_archive_then_real_reentry_during_commit_never_inherits_old_checkout(self):
        live = await self.seed()
        live['memory']['leads'][0]['created_at'] = '2026-10-01T12:00:00'
        live['memory']['leads'][0]['history'] = [{'role': 'user', 'content': 'ORIGINAL INCARNATION'}]
        await main._write_state_to_db(live, runtime_only=True)
        live = await main._read_state_from_db()
        kwargs = self.event(live, 'checkout_event')
        kwargs['checkout_event']['lead'].update(flow_plan_id='plan-a', flow_plan_price=10,
                                               chosen_plan='Acesso')
        reached, finish = asyncio.Event(), asyncio.Event()
        real_commit = AsyncSession.commit
        async def commit(session):
            reached.set()
            await finish.wait()
            return await real_commit(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            task = asyncio.create_task(main._write_state_to_db(live, **kwargs))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                old = live['memory']['leads'].pop()
                old['history'].append({'role': 'user', 'content': 'ARCHIVED DURING COMMIT'})
                live['memory']['deleted_leads'].append(old)
                new = await self.manager._get_or_create_lead(live, self.user, bot_token=self.bot.token)
                new.update(created_at='2026-10-04T13:00:00',
                           history=[{'role': 'user', 'content': 'REAL NEW INCARNATION'}])
                expected_new = copy.deepcopy(new)
                finish.set()
                returned = await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
        await self.assert_exact_sql_baseline(returned)
        self.assertEqual(returned['memory']['leads'], [expected_new])
        archived = returned['memory']['deleted_leads'][0]
        self.assertEqual(archived['created_at'], '2026-10-01T12:00:00')
        self.assertEqual(archived['last_tx_id'], 'generated-pix')
        self.assertEqual(archived['flow_plan_id'], 'plan-a')
        self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in archived['history']), 1)
        self.assertIn({'role': 'user', 'content': 'ARCHIVED DURING COMMIT'}, archived['history'])
        self.assertEqual(returned._row_base['memory']['deleted_leads'], [])
        self.assertEqual(returned._row_base['memory']['leads'][0]['last_tx_id'], 'generated-pix')
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'], [expected_new])
        self.assertEqual(cold['memory']['deleted_leads'], [archived])
        self.assertIn('generated-pix', cold['tx_records'])

    async def test_late_messages_on_both_rows_remain_independent_and_pending(self):
        live = await self.establish_reentry()
        archived_before = copy.deepcopy(live['memory']['deleted_leads'][0])
        active_message = {'role': 'user', 'content': 'LATE ACTIVE'}
        archived_message = {'role': 'user', 'content': 'LATE ARCHIVED'}
        def mutate(state):
            state['memory']['leads'][0]['history'].append(active_message)
            state['memory']['deleted_leads'][0]['history'].append(archived_message)
            state['memory']['deleted_leads'][0]['first_name'] = 'ARCHIVED EDIT'
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        sql = await self.assert_exact_sql_baseline(returned)
        self.assertEqual(returned['memory']['leads'][0]['history'],
                         sql['memory']['leads'][0]['history'] + [active_message])
        archived = returned['memory']['deleted_leads'][0]
        self.assertEqual(archived['history'], archived_before['history'] + [archived_message])
        self.assertEqual(archived['first_name'], 'ARCHIVED EDIT')
        self.assertEqual(archived['last_tx_id'], 'old-checkout')
        self.assertEqual(archived['flow_plan_id'], 'old-plan')
        self.assertEqual(sql['memory']['deleted_leads'], [archived_before])
        active = copy.deepcopy(returned['memory']['leads'][0])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'], [active])
        self.assertEqual(cold['memory']['deleted_leads'], [archived])

    async def test_registered_callback_reentry_without_delta_is_exactly_committed(self):
        live = await self.establish_reentry()
        archived = copy.deepcopy(live['memory']['deleted_leads'])
        returned, event = await self.registered_checkout()
        sql = await self.assert_exact_sql_baseline(returned)
        self.assertEqual(main._client_map(returned), main._client_map(sql))
        self.assertEqual(returned['memory']['deleted_leads'], archived)
        self.assertEqual(returned['memory']['leads'][0]['last_tx_id'], event['record']['tx_id'])
        self.assertEqual(returned['memory']['leads'][0]['stage'], 'Gerou checkout')
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(main._client_map(cold), main._client_map(sql))

    async def test_failed_registered_checkout_keeps_both_rows_baseline_and_pending_message(self):
        live = await self.establish_reentry()
        archived = copy.deepcopy(live['memory']['deleted_leads'])
        captured = {}
        message = {'role': 'user', 'content': 'PENDING FAILED CHECKOUT'}
        def mutate(state):
            state['memory']['leads'][0]['history'].append(message)
        with self.assertRaisesRegex(RuntimeError, 'offline late commit failure'):
            await self.registered_checkout(mutate, fail=True, captured=captured)
        current = await main._read_state_from_db()
        self.assertIs(current, captured['live'])
        self.assertEqual(current._row_base, captured['before_baseline'])
        sql = await self.assert_exact_sql_baseline(current)
        self.assertNotIn(message, sql['memory']['leads'][0]['history'])
        self.assertIn(message, current['memory']['leads'][0]['history'])
        self.assertEqual(current['memory']['deleted_leads'], archived)
        self.assertNotIn(self.created[0][0], current['tx_records'])
        self.assertNotIn(self.created[0][0], self.manager.tx_records)
        self.bot.send_message.assert_not_awaited()
        await main._write_state_to_db(current, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertIn(message, cold['memory']['leads'][0]['history'])
        self.assertEqual(cold['memory']['deleted_leads'], archived)
        self.assertNotIn(self.created[0][0], cold['tx_records'])

    async def test_different_origin_reentry_cannot_receive_archived_checkout(self):
        live = await self.seed()
        live['memory']['leads'][0]['history'] = [{'role': 'user', 'content': 'ORIGIN 111'}]
        await main._write_state_to_db(live, runtime_only=True)
        await main.delete_lead('7', bot_id='flow-a')
        state = copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0].update(token='222:test-only', bot_id='222')
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token='222:test-only')
        lead.update(created_at='2026-10-04T14:00:00', history=[{'role': 'user', 'content': 'ORIGIN 222'}])
        await main._write_state_to_db(state, runtime_only=True)
        live = await main._read_state_from_db()
        archived = copy.deepcopy(live['memory']['deleted_leads'])
        kwargs = self.event(live, 'checkout_event')
        kwargs['checkout_event']['record']['bot_token'] = '222:test-only'
        message = {'role': 'user', 'content': 'LATE ORIGIN 222'}
        def mutate(state):
            state['memory']['leads'][0]['history'].append(message)
        returned = await self.held(live, kwargs, mutate)
        sql = await self.assert_exact_sql_baseline(returned)
        self.assertEqual(returned['memory']['leads'][0]['origin_bot_numeric_id'], '222')
        self.assertEqual(returned['memory']['leads'][0]['history'],
                         sql['memory']['leads'][0]['history'] + [message])
        self.assertEqual(returned['memory']['deleted_leads'], archived)
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['deleted_leads'], archived)
        self.assertEqual(cold['memory']['leads'][0]['history'], returned['memory']['leads'][0]['history'])
        self.assertEqual(cold['tx_records']['generated-pix']['bot_token'], '222:test-only')

    async def test_late_restore_moves_only_the_same_archived_incarnation(self):
        await self.establish_reentry()
        state = copy.deepcopy(await main._read_state_from_db())
        # Remove only the new active row; keep the archived incarnation.
        state['memory']['leads'].clear()
        await main._write_state_to_db(state, delete_client_keys={('111', '7', False)})
        live = await main._read_state_from_db()
        before = copy.deepcopy(live['memory']['deleted_leads'][0])
        kwargs = {'runtime_only': True, 'checkout_event': {
            'record': {'tx_id': 'archived-pix', 'user_id': 7, 'status': 'pending',
                       'bot_id': 'flow-a', 'bot_token': self.bot.token, 'amount': 10},
            'before': before, 'lead': before,
            'history': {'role': 'assistant', 'tx_id': 'archived-pix', 'content': 'ARCHIVED CHECKOUT'}}}
        def mutate(state):
            restored = state['memory']['deleted_leads'].pop()
            restored.pop('deleted_at', None)
            restored['history'].append({'role': 'user', 'content': 'RESTORED LATE'})
            state['memory']['leads'].append(restored)
        returned = await self.held(live, kwargs, mutate)
        await self.assert_exact_sql_baseline(returned)
        self.assertEqual(returned['memory']['deleted_leads'], [])
        active = copy.deepcopy(returned['memory']['leads'][0])
        self.assertEqual(sum(h.get('tx_id') == 'archived-pix' for h in active['history']), 1)
        self.assertEqual(active['created_at'], before['created_at'])
        self.assertIn({'role': 'user', 'content': 'RESTORED LATE'}, active['history'])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['deleted_leads'], [])
        self.assertEqual(cold['memory']['leads'], [active])

    async def test_unproven_cross_placement_never_transfers_checkout(self):
        for case in ('missing_created_at', 'different_created_at', 'source_still_active'):
            with self.subTest(case=case):
                original = {'telegram_id': '7', 'origin_bot_numeric_id': '111',
                            'created_at': '2026-10-01T12:00:00', 'history': []}
                if case == 'missing_created_at':
                    original.pop('created_at')
                before = {'memory': {'leads': [original], 'deleted_leads': []}}
                committed = copy.deepcopy(before)
                committed['memory']['leads'][0].update(last_tx_id='generated-pix',
                    history=[{'role': 'assistant', 'tx_id': 'generated-pix'}])
                archived = copy.deepcopy(original)
                if case == 'different_created_at':
                    archived['created_at'] = '2026-10-04T12:00:00'
                live = {'memory': {'leads': [copy.deepcopy(original)] if case == 'source_still_active' else [],
                                   'deleted_leads': [archived]}}
                returned = main._merge_late_cache_delta(committed, before, live)
                self.assertEqual(returned['memory']['deleted_leads'], [archived])
                self.assertNotIn('last_tx_id', returned['memory']['deleted_leads'][0])
