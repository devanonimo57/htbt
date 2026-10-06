"""Measured finite ACTUAL handler/checkout/ASGI/worker workload, fake services only."""
import asyncio
import copy
import json
import os
import sqlite3
import time
import unittest
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from aiogram import Dispatcher, types
from sqlalchemy import text
import main
import telegram_service as service
import test_checkout_webhook_integration as fixture
from sqlite_policy import create_application_engine

REPORT = Path(os.environ.get('HOTBOT_REPORT', str(Path(__file__).resolve().parent / 'report')))
REPORT.mkdir(parents=True, exist_ok=True)


class MixedWalLoad(unittest.IsolatedAsyncioTestCase):
    async def test_levels_10_25_50_burst_and_three_finite_waves(self):
        measurements = []
        for concurrency in (10,25,50):
            main._DB_LOCK = asyncio.Lock()
            main._STATE_COMMITTED = {}
            main._STATE_COMMITTED_OWNER = None
            with patch.object(fixture, 'create_async_engine', create_application_engine):
                await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
            original_service_asyncio = service.asyncio
            timings = []
            counts = Counter()
            errors = []
            start_events, pix_events = {}, {}
            charge_counter = 0
            gateway_status = {}
            delivery_calls = Counter()
            sent = Counter()
            async def send_message(*args, **kwargs):
                uid = kwargs.get('chat_id', args[0] if args else None)
                message = kwargs.get('text', '')
                await asyncio.sleep(.001)
                sent[message] += 1
                if message == 'DS-START':
                    start_events[int(uid)].set()
                if message == 'DS-PIX':
                    pix_events[int(uid)].set()
                keyboard = kwargs.get('reply_markup')
                urls = [getattr(button, 'url', '') for row in getattr(keyboard, 'inline_keyboard', []) for button in row]
                if 'https://example.invalid/access' in urls:
                    delivery_calls[int(uid)] += 1
                return SimpleNamespace(message_id=1)
            self.bot.send_message = send_message
            self.manager.dp = Dispatcher()
            self.manager._register_handlers()
            self.manager._get_bot_by_token = lambda token:self.bot if token==self.bot.token else None
            start_handler = next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__=='handle_start')
            checkout_handler = next(h.callback for h in self.manager.dp.callback_query.handlers if h.callback.__name__=='handle_flow_plan_click')
            state=await main._read_state_from_db()
            state['flow_bots'][0]['welcome']={'message':'WELCOME'}
            state['flow_bots'][0]['downsell_general']={'enabled':True,'loop_rest_minutes':3,'sequences':[
                {'id':'start','message':'DS-START','delay_minutes':1,'trigger':'start','target_audience':'all','loop_enabled':False},
                {'id':'pix','message':'DS-PIX','delay_minutes':1,'trigger':'pix','target_audience':'new','loop_enabled':False}]}
            await main._write_state_to_db(state)
            def fake_gateway(*args,**kwargs):
                async def create_pix_charge(**charge):
                    nonlocal charge_counter
                    await asyncio.sleep(.001)
                    charge_counter+=1
                    tx='mixed-'+uuid.uuid4().hex
                    gateway_status[tx]='pending'
                    return {'id':tx,'qr_code':'PIX-'+tx}
                async def check_transaction_status(tx):
                    await asyncio.sleep(.001)
                    return {'status':gateway_status[tx],'raw':{'amount':10}}
                return SimpleNamespace(create_pix_charge=create_pix_charge,check_transaction_status=check_transaction_status)
            async def controlled_sleep(seconds):
                # Worker wakeups compressed; SQL/handlers retain genuine awaits.
                await asyncio.sleep(.005 if seconds else 0)
            class AsyncioProxy:
                sleep=staticmethod(controlled_sleep)
                def __getattr__(self,name): return getattr(original_service_asyncio,name)
            async def measured(name, awaitable):
                started=time.perf_counter()
                try:
                    value=await awaitable
                    counts[name]+=1
                    return value
                except Exception as exc:
                    errors.append({'operation':name,'error':repr(exc)})
                    raise
                finally:
                    timings.append((name,time.perf_counter()-started))
            main.app.dependency_overrides[main.verify_admin_auth]=lambda:None
            started=time.perf_counter()
            try:
                with patch.object(service,'PushinPayClient',fake_gateway), patch.object(main,'PushinPayClient',fake_gateway), patch.object(service,'asyncio',AsyncioProxy()):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                        async def reads():
                            for _ in range(concurrency*3):
                                result=await measured('metrics_read',client.get('/api/metrics?period=Tudo&bot_id=flow-a'))
                                self.assertEqual(result.status_code,200,result.text)
                                result=await measured('panel_read',client.get('/api/state'))
                                self.assertEqual(result.status_code,200,result.text)
                                await asyncio.sleep(.005)
                        async def customer(uid, paid):
                            user=types.User(id=uid,is_bot=False,first_name='Carga')
                            start_events[uid]=asyncio.Event()
                            pix_events[uid]=asyncio.Event()
                            await measured('start',start_handler(SimpleNamespace(bot=self.bot,from_user=user,chat=SimpleNamespace(id=uid),answer=send_message,text='/start')))
                            await asyncio.wait_for(start_events[uid].wait(),120)
                            for _ in range(2):
                                await measured('pix_checkout',checkout_handler(SimpleNamespace(bot=self.bot,from_user=user,message=SimpleNamespace(chat=SimpleNamespace(id=uid)),data='fplan:plan-a:0',answer=send_message)))
                            await asyncio.wait_for(pix_events[uid].wait(),120)
                            if paid:
                                current=await main._read_state_from_db()
                                txs=[tx for tx,r in current['tx_records'].items() if str(r['user_id'])==str(uid)]
                                self.assertEqual(len(txs),2)
                                for tx in reversed(txs):
                                    gateway_status[tx]='paid'
                                    result=await measured('verified_webhook',client.post('/api/webhook/syncpay',json={'id':tx,'status':'paid'}))
                                    self.assertEqual(result.status_code,200,result.text)
                                    self.assertEqual(result.json()['action'],'approved',result.text)
                                pad_dup_tx = txs[0]
                                result=await measured('duplicate_webhook',client.post('/api/webhook/syncpay',json={'id':pad_dup_tx,'status':'paid'}))
                                self.assertEqual(result.status_code,200,result.text)
                        read_task=asyncio.create_task(reads())
                        paid_customers=0
                        for wave in range(3):
                            customers=[]
                            for i in range(concurrency):
                                uid=100000+wave*concurrency+i
                                paid=(i%2==0)
                                paid_customers+=paid
                                customers.append(customer(uid,paid))
                            await asyncio.wait_for(asyncio.gather(*customers),max(180,concurrency*8))
                            await asyncio.sleep(.05)
                        await read_task
                        tasks=set(self.manager.downsell_tasks.values())
                        if tasks:
                            await asyncio.wait_for(asyncio.gather(*tasks),60)
                elapsed=time.perf_counter()-started
                main._STATE_CACHE=None
                cold=await main._read_state_from_db()
                total_customers=concurrency*3
                expected_pix=total_customers*2
                expected_paid=paid_customers*2
                self.assertEqual(len(cold['memory']['leads']),total_customers)
                self.assertEqual(len(cold['tx_records']),expected_pix)
                paid_rows=[r for r in cold['tx_records'].values() if r['status']=='paid']
                self.assertEqual(len(paid_rows),expected_paid)
                self.assertTrue(all(r['sales_counted'] and r['delivered'] and r['automatic_delivery_attempts']==1 for r in paid_rows))
                self.assertEqual(sum(r['amount'] for r in paid_rows),expected_paid*10)
                metrics=await main.get_panel_metrics(period='Tudo',bot_id='flow-a')
                self.assertEqual(metrics['paid_count'],expected_paid)
                self.assertEqual(metrics['paid_amount'],expected_paid*10)
                self.assertEqual(metrics['pending_count'],expected_pix-expected_paid)
                self.assertEqual(sum(delivery_calls.values()),expected_paid)
                self.assertEqual(sent['DS-START'],total_customers)
                self.assertEqual(sent['DS-PIX'],total_customers)
                async with self.engine.connect() as conn:
                    sql_clients=(await conn.execute(text('SELECT COUNT(*) FROM client_records'))).scalar()
                    sql_pix=(await conn.execute(text('SELECT COUNT(*) FROM pix_records'))).scalar()
                    mode=(await conn.execute(text('PRAGMA journal_mode'))).scalar()
                self.assertEqual(sql_clients,total_customers)
                self.assertEqual(sql_pix,expected_pix)
                self.assertEqual(mode,'wal')
                latency={}
                for operation in counts:
                    values=sorted(t for name,t in timings if name==operation)
                    latency[operation]={'count':len(values),'mean_ms':sum(values)/len(values)*1000,'p95_ms':values[min(len(values)-1,int(len(values)*.95))]*1000,'max_ms':max(values)*1000}
                db=Path(self.engine.url.database)
                measurement={'concurrency':concurrency,'waves':3,'wave_gap_seconds':.05,'elapsed_seconds':elapsed,'operations':dict(counts),'latency':latency,'errors':errors,'lock_errors':sum('locked' in x['error'] for x in errors),'stored_customers':sql_clients,'stored_pix':sql_pix,'distinct_sales':expected_paid,'revenue':expected_paid*10,'delivery_attempts':sum(r['automatic_delivery_attempts'] for r in paid_rows),'delivery_calls':sum(delivery_calls.values()),'downsell_sends':sent['DS-START']+sent['DS-PIX'],'database_bytes':db.stat().st_size,'wal_bytes':Path(str(db)+'-wal').stat().st_size if Path(str(db)+'-wal').exists() else 0,'persisted_payload_bytes':len(json.dumps(cold).encode()),'cold_reconciliation':True,'sqlite':sqlite3.sqlite_version,'fake_transport_latency_seconds':.001,'scheduler_clock':'compressed finite wakeups, not live wall-clock capacity','uuid_database':str(db)}
                measurements.append(measurement)
                Path(REPORT,'mixed-load.json').write_text(json.dumps(measurements,indent=2),encoding='utf-8')
            finally:
                if 'read_task' in locals() and not read_task.done():
                    read_task.cancel()
                    try:
                        await read_task
                    except Exception:
                        pass
                tasks=set(self.manager.downsell_tasks.values())
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                main.app.dependency_overrides.clear()
                await fixture.CheckoutWebhookIntegration.asyncTearDown(self)
