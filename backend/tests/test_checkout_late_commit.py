"""R2-02: late live-cache deltas survive SQL-confirmed event publication."""
import asyncio
import copy
import json
from unittest.mock import patch
from sqlalchemy.ext.asyncio import AsyncSession
import main
import test_checkout_webhook_integration as fixture


class CheckoutLateCommit(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def seed(self):
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        state['tx_records']['existing-pix'] = {
            'tx_id': 'existing-pix', 'user_id': 7, 'status': 'pending',
            'bot_id': 'flow-a', 'bot_token': self.bot.token, 'amount': 10,
        }
        await main._write_state_to_db(state)
        return await main._read_state_from_db()

    def event(self, live, kind):
        before = copy.deepcopy(live['memory']['leads'][0])
        if kind == 'checkout_start':
            after = copy.deepcopy(before)
            after['stage'] = 'Escolheu um plano'
            return {'runtime_only': True, 'checkout_start': {
                'lead': after, 'before': before, 'allow_create': False}}
        if kind == 'checkout_event':
            after = copy.deepcopy(before)
            after['last_tx_id'] = 'generated-pix'
            return {'runtime_only': True, 'checkout_event': {
                'record': {'tx_id': 'generated-pix', 'user_id': 7, 'status': 'pending',
                           'bot_id': 'flow-a', 'bot_token': self.bot.token, 'amount': 10},
                'before': before, 'lead': after,
                'history': {'role': 'assistant', 'tx_id': 'generated-pix', 'content': 'CHECKOUT'}}}
        bot = copy.deepcopy(live['flow_bots'][0])
        return {'flow_cache_event': main._flow_cache_event(bot, bot, live, channel_id='-100999')}

    async def held(self, live, kwargs, mutate, fail=False):
        reached, finish = asyncio.Event(), asyncio.Event()
        real_commit = AsyncSession.commit
        async def commit(session):
            reached.set()
            await finish.wait()
            if fail:
                raise RuntimeError('offline late commit failure')
            return await real_commit(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            task = asyncio.create_task(main._write_state_to_db(live, **kwargs))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                self.assertIs(await main._read_state_from_db(), live)
                mutate(live)
                finish.set()
                return await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)

    async def late_deltas(self, kind):
        live = await self.seed()
        def mutate(state):
            state['tx_records']['existing-pix']['access_status'] = 'LATE PENDING MARKER'
            state['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'LATE USER MESSAGE'})
            state['tx_records']['late-new-pix'] = {
                'tx_id': 'late-new-pix', 'user_id': 7, 'status': 'pending',
                'bot_id': 'flow-a', 'bot_token': self.bot.token, 'amount': 20}
        returned = await self.held(live, self.event(live, kind), mutate)
        self.assertIs(await main._read_state_from_db(), returned)
        self.assertEqual(returned['tx_records']['existing-pix'].get('access_status'), 'LATE PENDING MARKER')
        self.assertIn('late-new-pix', returned['tx_records'])
        self.assertIn('LATE USER MESSAGE', [h.get('content') for h in returned['memory']['leads'][0]['history']])
        self.assertNotIn('access_status', returned._row_base['tx_records']['existing-pix'])
        self.assertNotIn('late-new-pix', returned._row_base['tx_records'])
        self.assertNotIn('LATE USER MESSAGE', [h.get('content') for h in returned._row_base['memory']['leads'][0]['history']])
        self.assertEqual(main._STATE_COMMITTED, returned._row_base)
        from models import BotConfig
        from sqlalchemy import select
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().one()
            sql = await main._hydrate_records(session, json.loads(config.full_state_json))
        self.assertEqual(main._client_map(sql), main._client_map(returned._row_base))
        self.assertEqual(sql['tx_records'], returned._row_base['tx_records'])
        self.assertNotIn('late-new-pix', sql['tx_records'])
        if kind == 'checkout_event':
            self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in returned['memory']['leads'][0]['history']), 1)
            self.assertIn('generated-pix', returned._row_base['tx_records'])
        elif kind == 'checkout_start':
            self.assertEqual(returned._row_base['memory']['leads'][0]['stage'], 'Escolheu um plano')
        else:
            self.assertEqual(returned._row_base['flow_bots'][0]['cache_channel_id'], '-100999')
        returned['memory']['leads'][0]['first_name'] = 'NEXT WRITER'
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['tx_records']['existing-pix']['access_status'], 'LATE PENDING MARKER')
        self.assertIn('late-new-pix', cold['tx_records'])
        self.assertIn('LATE USER MESSAGE', [h.get('content') for h in cold['memory']['leads'][0]['history']])
        if kind == 'checkout_event':
            self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in cold['memory']['leads'][0]['history']), 1)

    async def test_checkout_start_late_deltas_stay_dirty_until_next_save(self):
        await self.late_deltas('checkout_start')

    async def test_checkout_event_late_deltas_stay_dirty_until_next_save(self):
        await self.late_deltas('checkout_event')

    async def test_flow_cache_event_late_deltas_stay_dirty_until_next_save(self):
        await self.late_deltas('flow_cache_event')

    async def test_old_baseline_queued_writer_preserves_event_history_and_paid_progress(self):
        live = await self.seed()
        live['tx_records']['existing-pix'].update(status='paid', sales_counted=True,
                                                automatic_delivery_attempts=1, mode='flow')
        live['flow_bots'][0].update(sales_today=1, revenue_today=10)
        live['learning']['confirmed_purchases'] = 1
        await main._write_state_to_db(live, runtime_only=True)
        live = await main._read_state_from_db()
        stale = copy.deepcopy(live)
        stale['memory']['leads'][0]['first_name'] = 'QUEUED OLD BASELINE'
        def mutate(state):
            state['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'LATE MESSAGE'})
            state['tx_records']['existing-pix'].update(automatic_delivery_attempts=2,
                primary_access_delivered=True, delivered=True, access_status='Ativo',
                last_delivery_attempt_at='2026-10-04T12:00:00',
                invite_url='https://example.invalid/current-invite')
        current = await self.held(live, self.event(live, 'checkout_event'), mutate)
        # A second writer had already captured the earlier SQL ancestry.
        returned = await main._write_state_to_db(stale, runtime_only=True)
        history = returned['memory']['leads'][0]['history']
        self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in history), 1)
        self.assertIn('LATE MESSAGE', [h.get('content') for h in history])
        self.assertNotIn('LATE MESSAGE', [h.get('content') for h in current._row_base['memory']['leads'][0]['history']])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        rec = cold['tx_records']['existing-pix']
        self.assertEqual(rec['automatic_delivery_attempts'], 2)
        self.assertTrue(rec['delivered'] and rec['primary_access_delivered'] and rec['sales_counted'])
        self.assertEqual(rec['access_status'], 'Ativo')
        self.assertEqual(rec['last_delivery_attempt_at'], '2026-10-04T12:00:00')
        self.assertEqual(rec['invite_url'], 'https://example.invalid/current-invite')
        self.assertEqual(cold['flow_bots'][0]['sales_today'], 1)
        self.assertEqual(cold['flow_bots'][0]['revenue_today'], 10)
        self.assertEqual(cold['learning']['confirmed_purchases'], 1)

    async def test_late_source_and_bot_replacements_never_inherit_event_cache_ids(self):
        for replacement in ('source', 'bot', 'remove'):
            with self.subTest(replacement=replacement):
                live = await self.seed()
                live['flow_bots'][0]['welcome'] = {'medias': [
                    {'id': 'media-1', 'url': 'https://example.invalid/old', 'type': 'photo', 'name': 'old'}]}
                await main._write_state_to_db(live)
                live = await main._read_state_from_db()
                before = copy.deepcopy(live['flow_bots'][0])
                after = copy.deepcopy(before)
                after['welcome']['medias'][0]['telegram_file_id'] = 'EVENT CACHE ID'
                event = main._flow_cache_event(before, after, live)
                def mutate(state):
                    bot = state['flow_bots'][0]
                    if replacement == 'source':
                        bot['welcome']['medias'][0]['url'] = 'https://example.invalid/replacement'
                    elif replacement == 'bot':
                        bot.update(token='222:test-only', bot_id='222')
                    else:
                        bot['welcome']['medias'].clear()
                returned = await self.held(live, {'flow_cache_event': event}, mutate)
                medias = returned['flow_bots'][0]['welcome']['medias']
                if replacement == 'remove':
                    self.assertEqual(medias, [])
                else:
                    self.assertNotIn('telegram_file_id', medias[0])
                self.assertEqual(returned._row_base['flow_bots'][0]['welcome']['medias'][0]['telegram_file_id'], 'EVENT CACHE ID')
                await main._write_state_to_db(returned)
                main._STATE_CACHE = None
                cold = await main._read_state_from_db()
                self.assertEqual(cold['flow_bots'][0]['welcome']['medias'], medias)

    async def test_late_archive_preserves_event_history_without_active_resurrection(self):
        live = await self.seed()
        def mutate(state):
            lead = state['memory']['leads'].pop()
            lead['history'].append({'role': 'user', 'content': 'ARCHIVED LATE'})
            state['memory']['deleted_leads'].append(lead)
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        self.assertEqual(returned['memory']['leads'], [])
        archived = returned['memory']['deleted_leads'][0]
        self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in archived['history']), 1)
        self.assertIn('ARCHIVED LATE', [h.get('content') for h in archived['history']])
        self.assertTrue(returned._row_base['memory']['leads'])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'], [])
        self.assertEqual(cold['memory']['deleted_leads'][0]['history'], archived['history'])
        self.assertIn('generated-pix', cold['tx_records'])

    async def test_late_permanent_deletion_stays_pending_then_is_durable(self):
        live = await self.seed()
        def mutate(state):
            state['memory']['leads'].clear()
            state['tx_records'].pop('existing-pix')
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        self.assertEqual(returned['memory']['leads'], [])
        self.assertNotIn('existing-pix', returned['tx_records'])
        self.assertIn('existing-pix', returned._row_base['tx_records'])
        self.assertTrue(returned._row_base['memory']['leads'])
        self.assertIn('generated-pix', returned['tx_records'])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'], [])
        self.assertNotIn('existing-pix', cold['tx_records'])
        self.assertIn('generated-pix', cold['tx_records'])

    async def test_queued_detached_runtime_writer_keeps_live_delta_during_its_own_commit(self):
        live = await self.seed()
        stale = copy.deepcopy(live)
        stale['memory']['leads'][0]['first_name'] = 'STALE WRITER CHANGE'
        def mutate(state):
            state['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'SECOND COMMIT MESSAGE'})
            state['tx_records']['existing-pix']['access_status'] = 'SECOND COMMIT MARKER'
        reached, finish = asyncio.Event(), asyncio.Event()
        real_commit = AsyncSession.commit
        async def commit(session):
            reached.set()
            await finish.wait()
            return await real_commit(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            task = asyncio.create_task(main._write_state_to_db(stale, runtime_only=True))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                mutate(live)
                finish.set()
                returned = await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(returned['tx_records']['existing-pix'].get('access_status'), 'SECOND COMMIT MARKER')
        self.assertIn('SECOND COMMIT MESSAGE', [h.get('content') for h in returned['memory']['leads'][0]['history']])
        self.assertNotIn('access_status', returned._row_base['tx_records']['existing-pix'])
        self.assertEqual(returned['memory']['leads'][0]['first_name'], 'STALE WRITER CHANGE')
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['tx_records']['existing-pix']['access_status'], 'SECOND COMMIT MARKER')

    async def test_old_baseline_cannot_resurrect_late_deleted_customer(self):
        live = await self.seed()
        stale = copy.deepcopy(live)
        stale['memory']['leads'][0]['first_name'] = 'OLD DELETED CUSTOMER'
        def mutate(state):
            state['memory']['leads'].clear()
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        await main._write_state_to_db(stale, runtime_only=True)
        self.assertEqual((await main._read_state_from_db())['memory']['leads'], [])
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'], [])
        self.assertIn('generated-pix', cold['tx_records'])
        self.assertTrue(returned._row_base['memory']['leads'])

    async def test_late_row_get_delta_does_not_change_exact_serialized_baseline(self):
        from models import PixRecord
        live = await self.seed()
        kwargs = self.event(live, 'checkout_event')
        reached, finish = asyncio.Event(), asyncio.Event()
        real_get = AsyncSession.get
        async def get(session, model, identity, **kw):
            if model is PixRecord and identity == 'generated-pix':
                reached.set()
                await finish.wait()
            return await real_get(session, model, identity, **kw)
        with patch.object(AsyncSession, 'get', new=get):
            task = asyncio.create_task(main._write_state_to_db(live, **kwargs))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                live['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'DURING SQL GET'})
                live['tx_records']['existing-pix']['access_status'] = 'DURING SQL GET'
                finish.set()
                returned = await asyncio.wait_for(task, 5)
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(returned['tx_records']['existing-pix']['access_status'], 'DURING SQL GET')
        self.assertNotIn('access_status', returned._row_base['tx_records']['existing-pix'])
        self.assertNotIn('DURING SQL GET', [h.get('content') for h in returned._row_base['memory']['leads'][0]['history']])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['tx_records']['existing-pix']['access_status'], 'DURING SQL GET')
        self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in cold['memory']['leads'][0]['history']), 1)

    async def test_late_reused_origin_gets_no_old_checkout_history(self):
        live = await self.seed()
        def mutate(state):
            state['memory']['leads'][0].update(origin_bot_numeric_id='222', first_name='NEW ORIGIN', history=[])
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        self.assertEqual(returned['memory']['leads'][0]['origin_bot_numeric_id'], '222')
        self.assertEqual(returned['memory']['leads'][0]['history'], [])
        self.assertEqual(returned._row_base['memory']['leads'][0]['origin_bot_numeric_id'], '111')
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(len(cold['memory']['leads']), 1)
        self.assertEqual(cold['memory']['leads'][0]['origin_bot_numeric_id'], '222')
        self.assertEqual(cold['memory']['leads'][0]['history'], [])
        self.assertEqual(cold['tx_records']['generated-pix']['bot_token'], self.bot.token)

    async def test_late_first_history_field_keeps_checkout_owned_entry(self):
        live = await self.seed()
        live['memory']['leads'][0].pop('history', None)
        await main._write_state_to_db(live)
        live = await main._read_state_from_db()
        def mutate(state):
            state['memory']['leads'][0]['history'] = [{'role': 'user', 'content': 'FIRST LATE MESSAGE'}]
        returned = await self.held(live, self.event(live, 'checkout_event'), mutate)
        history = returned['memory']['leads'][0]['history']
        self.assertEqual(sum(h.get('tx_id') == 'generated-pix' for h in history), 1)
        self.assertIn('FIRST LATE MESSAGE', [h.get('content') for h in history])
        self.assertEqual(len(returned._row_base['memory']['leads'][0]['history']), 1)
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['memory']['leads'][0]['history'], history)

    async def test_failed_commit_keeps_original_live_pending_deltas_and_baseline(self):
        live = await self.seed()
        baseline = copy.deepcopy(live._row_base)
        def mutate(state):
            state['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'PENDING ON FAILURE'})
        with self.assertRaisesRegex(RuntimeError, 'offline late commit failure'):
            await self.held(live, self.event(live, 'checkout_event'), mutate, fail=True)
        self.assertIs(await main._read_state_from_db(), live)
        self.assertEqual(live._row_base, baseline)
        self.assertEqual(main._STATE_COMMITTED, baseline)
        self.assertNotIn('generated-pix', live['tx_records'])
        self.assertEqual(live['memory']['leads'][0]['history'][-1]['content'], 'PENDING ON FAILURE')
        self.bot.send_message.assert_not_awaited()
