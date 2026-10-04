"""R2-01: real callback and event writers must persist pending runtime rows."""
import asyncio, copy, json, os, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Dispatcher, types
import main, telegram_service
import test_checkout_webhook_integration as fixture

REPORT=Path(os.environ['HOTBOT_REPORT']) if os.environ.get('HOTBOT_REPORT') else None
if REPORT is not None:
    REPORT.mkdir(parents=True,exist_ok=True)

def evidence(name,data):
    if REPORT is None:
        return
    (REPORT/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')

class CheckoutCommittedBaseline(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK=asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell=lambda *a,**kw:None
        self.manager.dp=Dispatcher()
        self.manager._register_handlers()
    async def cold(self):
        main._STATE_CACHE=None
        return await main._read_state_from_db()
    async def plan_click(self):
        handler=next(h.callback for h in self.manager.dp.callback_query.handlers if h.callback.__name__=='handle_flow_plan_click')
        callback=SimpleNamespace(from_user=self.user,bot=self.bot,message=self.callback.message,
                                 data='fplan:plan-a:0',answer=AsyncMock())
        await handler(callback)
    async def test_actual_new_customer_durable_before_gateway_await(self):
        reached,finish=asyncio.Event(),asyncio.Event()
        async def create(**kw):
            reached.set();await finish.wait()
            return {'id':'independent-new','qr_code':'PIX-independent-new'}
        with patch.object(telegram_service,'PushinPayClient',return_value=SimpleNamespace(create_pix_charge=create)):
            task=asyncio.create_task(self.plan_click())
            try:
                await asyncio.wait_for(reached.wait(),5)
                cache=copy.deepcopy(await main._read_state_from_db())
                async with main.async_session() as session:
                    from sqlalchemy import select
                    from models import ClientRecord
                    persisted=(await session.execute(select(ClientRecord))).scalars().all()
                evidence('new-customer-before-gateway',{'cache_leads':len(cache['memory']['leads']),'sql_client_rows':len(persisted)})
                self.assertEqual(len(persisted),1,'Customer admission must be durable before gateway I/O')
            finally:
                finish.set();await asyncio.wait_for(task,5)
    async def test_actual_new_customer_survives_gateway_failure(self):
        async def fail(**kw):raise RuntimeError('offline fixture gateway failure')
        with patch.object(telegram_service,'PushinPayClient',return_value=SimpleNamespace(create_pix_charge=fail)):
            await self.plan_click()
        cache=copy.deepcopy(await main._read_state_from_db())
        saved=await self.cold()
        evidence('new-customer-gateway-failure',{'cache_leads':len(cache['memory']['leads']),'cold_leads':len(saved['memory']['leads']),'cold_pix':len(saved['tx_records'])})
        self.assertEqual(len(saved['memory']['leads']),1,'Failed gateway must not erase admitted customer on restart')
    async def test_actual_existing_customer_stage_survives_gateway_failure(self):
        state=copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state,self.user,bot_token=self.bot.token)
        await main._write_state_to_db(state,runtime_only=True)
        async def fail(**kw):raise RuntimeError('offline fixture gateway failure')
        with patch.object(telegram_service,'PushinPayClient',return_value=SimpleNamespace(create_pix_charge=fail)):
            await self.plan_click()
        cache=copy.deepcopy(await main._read_state_from_db());saved=await self.cold()
        evidence('existing-customer-gateway-failure',{'cache_stage':cache['memory']['leads'][0]['stage'],'cold_stage':saved['memory']['leads'][0]['stage']})
        self.assertEqual(saved['memory']['leads'][0]['stage'],'Escolheu um plano')
    async def test_gateway_held_replacement_does_not_change_charge_origin(self):
        snapshot=copy.deepcopy(await main._read_state_from_db())
        reached,finish=asyncio.Event(),asyncio.Event()
        original=telegram_service.PushinPayClient
        def gateway(*a,**kw):
            inner=original(*a,**kw)
            async def create(**charge):
                response=await inner.create_pix_charge(**charge)
                reached.set();await finish.wait();return response
            return SimpleNamespace(create_pix_charge=create)
        with patch.object(telegram_service,'PushinPayClient',new=gateway):
            task=asyncio.create_task(self.manager._generate_flow_checkout(self.callback,snapshot['flow_bots'][0],snapshot,'plan-a',0,[]))
            try:
                await asyncio.wait_for(reached.wait(),5)
                fresh=copy.deepcopy(await main._read_state_from_db())
                fresh['flow_bots'][0].update(token='222:test-only',bot_id='222')
                fresh['flow_bots'][0]['plans'][0]['price']=77
                await main._write_state_to_db(fresh)
                finish.set();await asyncio.wait_for(task,5)
            finally:
                finish.set();await asyncio.gather(task,return_exceptions=True)
        saved=await self.cold();record=saved['tx_records'][self.created[0][0]]
        evidence('gateway-held-replacement',{'configured_origin':saved['flow_bots'][0]['bot_id'],'purchase_origin':record['origin_bot_numeric_id'],'purchase_amount':record['amount'],'current_price':saved['flow_bots'][0]['plans'][0]['price']})
        self.assertEqual(record['bot_token'],self.bot.token)
        self.assertEqual(record['origin_bot_numeric_id'],'111')
        self.assertEqual(saved['flow_bots'][0]['token'],'222:test-only')
    async def test_event_helpers_preserve_dirty_pix_under_real_writer_await(self):
        for kind in ('checkout_event','flow_cache_event'):
            with self.subTest(kind=kind):
                state=copy.deepcopy(await main._read_state_from_db())
                lead=await self.manager._get_or_create_lead(state,self.user,bot_token=self.bot.token)
                state['tx_records']['dirty-'+kind]={'tx_id':'dirty-'+kind,'user_id':7,'status':'pending','bot_id':'flow-a','bot_token':self.bot.token,'amount':10}
                await main._write_state_to_db(state)
                live=await main._read_state_from_db()
                bot=copy.deepcopy(live['flow_bots'][0])
                if kind=='flow_cache_event':
                    kwargs={'flow_cache_event':main._flow_cache_event(bot,bot,live)}
                else:
                    rec={'tx_id':'generated-helper-pix','user_id':7,'status':'pending','bot_id':'flow-a','bot_token':self.bot.token,'amount':10}
                    current=copy.deepcopy(live['memory']['leads'][0])
                    kwargs={'runtime_only':True,'checkout_event':{'record':rec,'before':current,'lead':current,'history':{'role':'assistant','tx_id':rec['tx_id'],'content':'HELPER CHECKOUT'}}}
                await main._DB_LOCK.acquire()
                task=asyncio.create_task(main._write_state_to_db(live,**kwargs))
                try:
                    await asyncio.sleep(0);self.assertFalse(task.done())
                    live['tx_records']['dirty-'+kind].update(access_status='CURRENT RUNTIME MARKER')
                    main._DB_LOCK.release()
                    returned=await asyncio.wait_for(task,5)
                    await main._write_state_to_db(returned,runtime_only=True)
                    saved=await self.cold()
                    evidence(kind+'-dirty-pix',{'returned_marker':returned['tx_records']['dirty-'+kind].get('access_status'),'cold_marker':saved['tx_records']['dirty-'+kind].get('access_status')})
                    self.assertEqual(saved['tx_records']['dirty-'+kind].get('access_status'),'CURRENT RUNTIME MARKER')
                finally:
                    if main._DB_LOCK.locked():main._DB_LOCK.release()
                    await asyncio.gather(task,return_exceptions=True)

    async def test_actual_failed_admission_keeps_cache_and_sql_unchanged_without_gateway(self):
        cache = await main._read_state_from_db()
        before = copy.deepcopy(cache)
        committed = copy.deepcopy(main._STATE_COMMITTED)
        with patch.object(main, '_persist_records', side_effect=RuntimeError('offline admission SQL failure')):
            with self.assertRaisesRegex(RuntimeError, 'offline admission SQL failure'):
                await self.plan_click()
        self.assertEqual(self.created, [], 'SQL admission must precede gateway calls')
        self.bot.send_message.assert_not_awaited()
        self.assertIs(await main._read_state_from_db(), cache)
        self.assertEqual(cache, before)
        self.assertEqual(main._STATE_COMMITTED, committed)
        self.assertEqual(cache._row_base, committed)
        self.assertFalse((await self.cold())['memory']['leads'])

    async def test_actual_new_customer_commit_failure_exposes_no_lead_or_gateway(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        cache = await main._read_state_from_db()
        before = copy.deepcopy(cache)
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('offline admission commit failure')):
            with self.assertRaisesRegex(RuntimeError, 'offline admission commit failure'):
                await self.plan_click()
        self.assertEqual(self.created, [])
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(cache, before)
        self.assertEqual(cache._row_base, before._row_base)
        self.assertIs(await main._read_state_from_db(), cache)
        self.assertFalse((await self.cold())['memory']['leads'])

    async def test_actual_existing_customer_commit_failure_keeps_previous_stage(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state)
        cache = await main._read_state_from_db()
        before = copy.deepcopy(cache)
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('offline admission commit failure')):
            with self.assertRaisesRegex(RuntimeError, 'offline admission commit failure'):
                await self.plan_click()
        self.assertEqual(self.created, [])
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(cache, before)
        self.assertEqual(cache._row_base, before._row_base)
        self.assertEqual((await self.cold())['memory']['leads'][0]['stage'], 'Conversando')

    async def test_unchanged_snapshot_cannot_confirm_unwritten_current_pix_rows(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['tx_records']['runtime-pix'] = {
            'tx_id': 'runtime-pix', 'status': 'pending', 'bot_id': 'flow-a',
            'bot_token': self.bot.token, 'user_id': 7, 'amount': 10,
        }
        await main._write_state_to_db(state)
        stale = copy.deepcopy(await main._read_state_from_db())
        live = await main._read_state_from_db()
        live['tx_records']['runtime-pix']['access_status'] = 'PENDING CURRENT MARKER'
        live['tx_records']['new-runtime-pix'] = {
            'tx_id': 'new-runtime-pix', 'status': 'pending', 'bot_id': 'flow-a',
            'bot_token': self.bot.token, 'user_id': 7, 'amount': 20,
        }
        returned = await main._write_state_to_db(stale, runtime_only=True)
        self.assertEqual(returned['tx_records']['runtime-pix']['access_status'], 'PENDING CURRENT MARKER')
        self.assertNotIn('access_status', returned._row_base['tx_records']['runtime-pix'])
        self.assertNotIn('new-runtime-pix', returned._row_base['tx_records'])
        self.assertEqual(main._STATE_COMMITTED, returned._row_base)
        await main._write_state_to_db(returned, runtime_only=True)
        saved = await self.cold()
        self.assertEqual(saved['tx_records']['runtime-pix']['access_status'], 'PENDING CURRENT MARKER')
        self.assertIn('new-runtime-pix', saved['tx_records'])

    async def test_cold_normalization_does_not_confirm_an_unwritten_client_history(self):
        from models import ClientRecord
        from sqlalchemy import select
        state = copy.deepcopy(await main._read_state_from_db())
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state)
        async with main.async_session() as session:
            row = (await session.execute(select(ClientRecord))).scalars().one()
            raw = json.loads(row.data_json)
            raw['stage_history'] = []
            row.data_json = json.dumps(raw)
            await session.commit()
        saved = await self.cold()
        self.assertEqual(saved['memory']['leads'][0]['stage_history'], ['Conversando'])
        self.assertEqual(saved._row_base['memory']['leads'][0]['stage_history'], [])
        await main._write_state_to_db(saved, runtime_only=True)
        async with main.async_session() as session:
            row = (await session.execute(select(ClientRecord))).scalars().one()
            self.assertEqual(json.loads(row.data_json)['stage_history'], ['Conversando'])

    async def test_two_queued_events_keep_current_rows_and_monotonic_paid_metadata(self):
        state = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        for tid in ('existing-a', 'existing-b'):
            state['tx_records'][tid] = {
                'tx_id': tid, 'status': 'paid', 'sales_counted': True, 'delivered': True,
                'automatic_delivery_attempts': 1, 'bot_id': 'flow-a',
                'bot_token': self.bot.token, 'user_id': 7, 'amount': 10,
            }
        await main._write_state_to_db(state)
        stale = copy.deepcopy(await main._read_state_from_db())
        live = await main._read_state_from_db()
        current = copy.deepcopy(live['memory']['leads'][0])
        event = {'record': {'tx_id': 'queued-pix', 'status': 'pending', 'bot_id': 'flow-a',
                           'bot_token': self.bot.token, 'user_id': 7, 'amount': 10},
                 'before': current, 'lead': current,
                 'history': {'role': 'assistant', 'content': 'QUEUED', 'tx_id': 'queued-pix'}}
        bot = copy.deepcopy(live['flow_bots'][0])
        await main._DB_LOCK.acquire()
        tasks = [asyncio.create_task(main._write_state_to_db(stale, runtime_only=True, checkout_event=event)),
                 asyncio.create_task(main._write_state_to_db(stale, flow_cache_event=main._flow_cache_event(bot, bot, stale)))]
        try:
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in tasks))
            live['tx_records']['existing-a'].update(access_status='CURRENT A', automatic_delivery_attempts=2,
                                                    primary_access_delivered=True, last_delivery_attempt_at='2026-10-04T12:00:00')
            live['tx_records']['existing-b'].update(access_status='CURRENT B', invite_url='https://example.invalid/new-invite')
            live['memory']['leads'][0]['history'].append({'role': 'user', 'content': 'CURRENT HISTORY'})
            main._DB_LOCK.release()
            returned = (await asyncio.wait_for(asyncio.gather(*tasks), 5))[-1]
            await main._write_state_to_db(returned, runtime_only=True)
            saved = await self.cold()
            self.assertEqual(saved['tx_records']['existing-a']['access_status'], 'CURRENT A')
            self.assertEqual(saved['tx_records']['existing-a']['automatic_delivery_attempts'], 2)
            self.assertTrue(saved['tx_records']['existing-a']['primary_access_delivered'])
            self.assertEqual(saved['tx_records']['existing-a']['last_delivery_attempt_at'], '2026-10-04T12:00:00')
            self.assertEqual(saved['tx_records']['existing-b']['access_status'], 'CURRENT B')
            self.assertEqual(saved['tx_records']['existing-b']['invite_url'], 'https://example.invalid/new-invite')
            self.assertTrue(all(saved['tx_records'][tid]['sales_counted'] for tid in ('existing-a', 'existing-b')))
            self.assertEqual([h.get('content') for h in saved['memory']['leads'][0]['history']], ['CURRENT HISTORY', 'QUEUED'])
            self.assertEqual(saved['flow_bots'][0]['sales_today'], returned['flow_bots'][0]['sales_today'])
        finally:
            if main._DB_LOCK.locked():
                main._DB_LOCK.release()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_checkout_start_preserves_dirty_rows_under_real_writer_await(self):
        state=copy.deepcopy(await main._read_state_from_db())
        lead=await self.manager._get_or_create_lead(state,self.user,bot_token=self.bot.token)
        state['tx_records']['existing-pix']={'tx_id':'existing-pix','user_id':7,'status':'pending','bot_id':'flow-a','bot_token':self.bot.token,'amount':10}
        await main._write_state_to_db(state)
        live=await main._read_state_from_db()
        await main._DB_LOCK.acquire()
        task=asyncio.create_task(main._write_state_to_db(live,runtime_only=True,checkout_start={'lead':copy.deepcopy(live['memory']['leads'][0]),'allow_create':False}))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            live['memory']['leads'][0]['history'].append({'role':'user','content':'HELD UNCOMMITTED MESSAGE'})
            main._DB_LOCK.release()
            returned=await asyncio.wait_for(task,5)
            # Save the returned current state, as a subsequent normal runtime writer does.
            await main._write_state_to_db(returned,runtime_only=True)
            saved=await self.cold()
            evidence('checkout-start-dirty-row',{'returned_history':returned['memory']['leads'][0]['history'],'cold_history':saved['memory']['leads'][0]['history']})
            self.assertEqual(saved['memory']['leads'][0]['history'],returned['memory']['leads'][0]['history'])
        finally:
            if main._DB_LOCK.locked():main._DB_LOCK.release()
            await asyncio.gather(task,return_exceptions=True)
