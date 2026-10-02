import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from sqlalchemy import select, event

import main
from models import BotConfig, ClientRecord, PixRecord


class RowPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await main._create_schema()
        main._STATE_CACHE = {}
        async with main.async_session() as session:
            for model in (BotConfig, ClientRecord, PixRecord):
                for row in (await session.execute(select(model))).scalars().all():
                    await session.delete(row)
            await session.commit()
        state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        state['flow_bots'] = [
            {'id': 'a', 'bot_id': '111', 'token': '111:local', 'active': False},
            {'id': 'b', 'bot_id': '222', 'token': '222:local', 'active': False},
        ]
        state['memory'] = {'leads': [
            {'telegram_id': '7', 'bot_id': 'a', 'origin_bot_numeric_id': '111', 'stage': 'Conversando'},
            {'telegram_id': '7', 'bot_id': 'b', 'origin_bot_numeric_id': '222', 'stage': 'Conversando'},
        ], 'deleted_leads': []}
        state['tx_records'] = {
            'p1': {'user_id': 7, 'bot_id': 'a', 'bot_token': '111:local', 'status': 'paid', 'amount': 10, 'paid_at': datetime.now(timezone.utc).isoformat(), 'sales_counted': True},
            'p2': {'user_id': 7, 'bot_id': 'a', 'bot_token': '111:local', 'status': 'paid', 'amount': 20, 'paid_at': datetime.now(timezone.utc).isoformat(), 'sales_counted': True},
            'p3': {'user_id': 7, 'bot_id': 'b', 'bot_token': '222:local', 'status': 'pending', 'amount': 5},
        }
        await main._write_state_to_db(state)

    async def test_state_is_split_into_independent_rows_and_roundtrips(self):
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().first()
            self.assertNotIn('p1', config.full_state_json)
            self.assertNotIn('telegram_id', config.full_state_json)
            self.assertEqual(len((await session.execute(select(ClientRecord))).scalars().all()), 2)
            self.assertEqual(len((await session.execute(select(PixRecord))).scalars().all()), 3)
        main._STATE_CACHE = None
        state = await main._read_state_from_db()
        self.assertEqual(len(state['memory']['leads']), 2)
        self.assertEqual(set(state['tx_records']), {'p1', 'p2', 'p3'})

    async def test_single_pix_change_only_queries_its_own_row(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['tx_records']['p3']['status'] = 'paid'
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            if any(table in statement.lower() for table in ('pix_records', 'client_records', 'orders')):
                statements.append((statement.lower(), str(parameters)))
        event.listen(main.engine.sync_engine, 'before_cursor_execute', record)
        try:
            await main._write_state_to_db(state)
        finally:
            event.remove(main.engine.sync_engine, 'before_cursor_execute', record)
        self.assertFalse(any('client_records' in sql for sql, _ in statements), statements)
        self.assertFalse(any('p1' in params or 'p2' in params for _, params in statements), statements)
        self.assertTrue(any('p3' in params for _, params in statements), statements)

    async def test_single_lead_change_does_not_query_pix_or_other_leads(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['memory']['leads'][0]['first_name'] = 'Updated'
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            if any(table in statement.lower() for table in ('pix_records', 'client_records', 'orders')):
                statements.append((statement.lower(), str(parameters)))
        event.listen(main.engine.sync_engine, 'before_cursor_execute', record)
        try:
            await main._write_state_to_db(state)
        finally:
            event.remove(main.engine.sync_engine, 'before_cursor_execute', record)
        self.assertFalse(any('pix_records' in sql or 'orders' in sql for sql, _ in statements), statements)
        self.assertFalse(any('222' in params for _, params in statements), statements)
        self.assertTrue(any('111' in params for _, params in statements), statements)

    async def test_selected_bot_soft_delete_then_permanent_removes_only_its_rows(self):
        await main.delete_lead('7', bot_id='a')
        state = await main._read_state_from_db()
        self.assertEqual([lead['bot_id'] for lead in state['memory']['leads']], ['b'])
        self.assertEqual(state['tx_records']['p1']['status'], 'paid')
        await main.permanently_delete_lead('7', bot_id='a')
        main._STATE_CACHE = None
        state = await main._read_state_from_db()
        self.assertEqual(set(state['tx_records']), {'p3'})
        self.assertEqual([lead['bot_id'] for lead in state['memory']['leads']], ['b'])

    async def test_all_bots_scope_applies_only_to_selected_customer(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['memory']['leads'].append({'telegram_id': '8', 'bot_id': 'b', 'origin_bot_numeric_id': '222'})
        state['tx_records']['p4'] = {'user_id': 8, 'bot_id': 'b', 'bot_token': '222:local', 'status': 'paid', 'amount': 3}
        await main._write_state_to_db(state)
        await main.delete_lead('7')
        await main.permanently_delete_lead('7')
        main._STATE_CACHE = None
        result = await main._read_state_from_db()
        self.assertEqual(set(result['tx_records']), {'p4'})
        self.assertEqual([x['telegram_id'] for x in result['memory']['leads']], ['8'])

    async def test_aggregate_counts_distinct_paid_pix_not_users(self):
        result = await main.get_flow_metrics(bot_id='a')
        self.assertEqual(result['paid_count'], 2)
        self.assertEqual(result['paid_amount'], 30)
        self.assertNotIn('tx_records', result)

    async def test_bulk_and_single_pix_delete_routes_are_absent(self):
        routes = {(m, r.path) for r in main.app.routes for m in (getattr(r, 'methods', None) or set())}
        self.assertNotIn(('DELETE', '/api/leads'), routes)
        self.assertNotIn(('DELETE', '/api/flow-bots/transactions/{tx_id}'), routes)

    async def test_stale_state_write_does_not_remove_new_customer(self):
        stale = copy.deepcopy(await main._read_state_from_db())
        fresh = copy.deepcopy(stale)
        fresh['memory']['leads'].append({'telegram_id': '9', 'bot_id': 'a', 'origin_bot_numeric_id': '111'})
        await main._write_state_to_db(fresh)
        await main._write_state_to_db(stale)
        main._STATE_CACHE = None
        state = await main._read_state_from_db()
        self.assertIn('9', [x['telegram_id'] for x in state['memory']['leads']])

    async def test_stale_paid_snapshot_cannot_clear_delivery_marker(self):
        stale = copy.deepcopy(await main._read_state_from_db())
        delivered = copy.deepcopy(stale)
        delivered['tx_records']['p1']['delivered'] = True
        delivered['tx_records']['p1']['delivery_attempts'] = 1
        await main._write_state_to_db(delivered)
        await main._write_state_to_db(stale)
        main._STATE_CACHE = None
        record = (await main._read_state_from_db())['tx_records']['p1']
        self.assertTrue(record['delivered'])
        self.assertEqual(record['delivery_attempts'], 1)

    async def test_live_stale_lead_writer_cannot_reopen_cancelled_pix(self):
        live = await main._read_state_from_db()
        fresh = copy.deepcopy(live)
        fresh['tx_records']['p3']['status'] = 'cancelled'
        await main._write_state_to_db(fresh)
        live['memory']['leads'][0]['first_name'] = 'Late update'
        await main._write_state_to_db(live)
        main._STATE_CACHE = None
        result = await main._read_state_from_db()
        self.assertEqual(result['tx_records']['p3']['status'], 'cancelled')
        self.assertEqual(result['memory']['leads'][0]['first_name'], 'Late update')

    async def test_stale_lead_write_cannot_revert_unrelated_cancelled_pix(self):
        stale = copy.deepcopy(await main._read_state_from_db())
        fresh = copy.deepcopy(await main._read_state_from_db())
        fresh['tx_records']['p3']['status'] = 'cancelled'
        await main._write_state_to_db(fresh)
        stale['memory']['leads'][0]['first_name'] = 'Late update'
        await main._write_state_to_db(stale)
        main._STATE_CACHE = None
        result = await main._read_state_from_db()
        self.assertEqual(result['tx_records']['p3']['status'], 'cancelled')
        self.assertEqual(result['memory']['leads'][0]['first_name'], 'Late update')

    async def test_bulk_sales_wipe_is_rejected(self):
        with self.assertRaises(main.HTTPException) as exc:
            await main.clear_flow_test_data(main.ClearTestDataRequest(bot_id='all', clear_all_sales=True))
        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual((await main.get_flow_metrics())['paid_count'], 2)

    async def test_legacy_aggregate_migrates_without_losing_records(self):
        old = copy.deepcopy(await main._read_state_from_db())
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().first()
            config.full_state_json = json.dumps(old)
            for model in (ClientRecord, PixRecord):
                for row in (await session.execute(select(model))).scalars().all():
                    await session.delete(row)
            await session.commit()
        main._STATE_CACHE = None
        from unittest.mock import patch, AsyncMock
        with patch.object(main, '_startup_warm_medias_task', new_callable=AsyncMock):
            await main.init_db()
            await main.init_db()
        state = await main._read_state_from_db()
        self.assertEqual(len(state['memory']['leads']), 2)
        self.assertEqual(set(state['tx_records']), {'p1', 'p2', 'p3'})
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().first()
            self.assertNotIn('tx_records', json.loads(config.full_state_json))

    async def test_panel_state_does_not_send_pix_records(self):
        state = await main.get_full_state()
        self.assertNotIn('tx_records', state)
