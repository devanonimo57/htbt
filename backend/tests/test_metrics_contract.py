import unittest
from tests.test_row_persistence import RowPersistenceTests
import main
import httpx
from sqlalchemy import select, event
from models import PixRecord


class MetricsContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RowPersistenceTests.asyncSetUp
    async def test_http_contract_is_auth_guarded_and_bounded(self):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            self.assertEqual((await client.get('/api/metrics')).status_code, 401)
            main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
            try:
                response = await client.get('/api/metrics?period=Total&bot_id=all')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['paid_amount'], 30)
                self.assertNotIn('tx_records', response.text)
                page = await client.get('/api/transactions?period=Total&page=1&page_size=1')
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.json()['total'], 3)
                self.assertEqual(len(page.json()['items']), 1)
                self.assertEqual((await client.get('/api/transactions?page_size=101')).status_code, 422)
            finally:
                main.app.dependency_overrides.pop(main.verify_admin_auth, None)
    async def test_metrics_and_paged_transactions_are_consistent_without_pix_payloads(self):
        metrics = await main.get_panel_metrics(period='Total', bot_id='all')
        self.assertEqual(metrics['paid_count'], 2)
        self.assertEqual(metrics['paid_amount'], 30)
        self.assertEqual(metrics['pending_count'], 1)
        self.assertEqual(metrics['generated_amount'], 35)
        self.assertNotIn('tx_records', metrics)
        page = await main.list_panel_transactions(period='Total', bot_id='all', status='all', page=1, page_size=2)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len(page['items']), 2)
        self.assertNotIn('data_json', page['items'][0])
        self.assertEqual((await main.list_panel_transactions(period='Total', bot_id='a', status='paid', page=1, page_size=2))['total'], 2)

    async def test_metrics_use_current_rows_after_payment_and_delete(self):
        state = await main._read_state_from_db()
        state['tx_records']['p3']['status'] = 'paid'
        await main._write_state_to_db(state)
        self.assertEqual((await main.get_panel_metrics(period='Total', bot_id='all'))['paid_count'], 3)
        await main.delete_lead('7', bot_id='a')
        await main.permanently_delete_lead('7', bot_id='a')
        self.assertEqual((await main.get_panel_metrics(period='Total', bot_id='all'))['paid_count'], 1)

    async def test_calendar_day_filters_metrics(self):
        self.assertEqual((await main.get_panel_metrics(period='Total', bot_id='all', date='2020-01-01'))['paid_count'], 0)
        with self.assertRaises(main.HTTPException):
            await main.get_panel_metrics(period='Total', bot_id='all', date='not-a-date')

    async def test_breakdowns_are_bounded_and_reconcile_with_paid_totals(self):
        state = await main._read_state_from_db()
        state['tx_records']['p1'].update(created_at='2026-09-20T10:00:00-03:00', paid_at='2026-09-20T10:00:00-03:00')
        state['tx_records']['p2'].update(created_at='2026-09-20T11:00:00-03:00', paid_at='2026-09-20T11:00:00-03:00')
        state['tx_records']['p3']['created_at'] = '2026-09-20T12:00:00-03:00'
        await main._write_state_to_db(state)
        metrics = await main.get_panel_metrics(period='Tudo', bot_id='all')
        self.assertEqual(metrics['paid_count'], 2)
        self.assertEqual(metrics['by_day'], [{'day': '2026-09-20', 'paid_count': 2, 'paid_amount': 30}])
        self.assertEqual(metrics['by_hour'], [
            {'hour': 10, 'generated_count': 1, 'paid_count': 1},
            {'hour': 11, 'generated_count': 1, 'paid_count': 1},
            {'hour': 12, 'generated_count': 1, 'paid_count': 0},
        ])
        self.assertEqual(metrics['by_bot'], [{'origin_bot_id': '111', 'paid_count': 2, 'paid_amount': 30}])
        self.assertEqual(metrics['by_ticket'], [
            {'amount': 20, 'paid_count': 1}, {'amount': 10, 'paid_count': 1}])
        self.assertNotIn('data_json', str(metrics))

    async def test_paid_period_uses_payment_date_not_checkout_date(self):
        state = await main._read_state_from_db()
        state['tx_records']['p1'].update(created_at='2026-09-19T10:00:00-03:00', paid_at='2026-09-20T10:00:00-03:00')
        state['tx_records']['p2'].update(created_at='2026-09-20T10:00:00-03:00', paid_at='2026-09-21T10:00:00-03:00')
        state['tx_records']['p3']['created_at'] = '2026-09-20T10:00:00-03:00'
        await main._write_state_to_db(state)
        metrics = await main.get_panel_metrics(period='Tudo', bot_id='a', date='2026-09-20')
        self.assertEqual(metrics['paid_count'], 1)
        self.assertEqual(metrics['paid_amount'], 10)
        self.assertEqual(metrics['generated_count'], 1)
        self.assertEqual(metrics['by_bot'][0]['origin_bot_id'], '111')

    async def test_flow_filter_restricts_both_totals_and_breakdowns(self):
        state = await main._read_state_from_db()
        state['flow_bots'][1]['mode'] = 'ai'
        state['tx_records']['p3']['status'] = 'paid'
        await main._write_state_to_db(state)
        flow = await main.get_panel_metrics(period='Tudo', bot_id='all', flow='flow')
        self.assertEqual(flow['paid_count'], 2)
        self.assertEqual(sum(x['paid_count'] for x in flow['by_bot']), 2)
        ai = await main.get_panel_metrics(period='Tudo', bot_id='all', flow='ai')
        self.assertEqual(ai['paid_count'], 1)
        with self.assertRaises(main.HTTPException):
            await main.get_panel_metrics(period='Tudo', bot_id='all', flow='invalid')

    async def test_metrics_only_select_aggregate_columns_and_duplicate_payment_stays_one_row(self):
        state = await main._read_state_from_db()
        state['tx_records']['p3']['status'] = 'paid'
        await main._write_state_to_db(state)
        await main._write_state_to_db(state)  # webhook replay for the same gateway ID
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            if 'pix_records' in statement.lower():
                statements.append(statement.lower())
        event.listen(main.engine.sync_engine, 'before_cursor_execute', observe)
        try:
            result = await main.get_panel_metrics(period='Tudo', bot_id='all')
        finally:
            event.remove(main.engine.sync_engine, 'before_cursor_execute', observe)
        self.assertEqual(result['paid_count'], 3)
        self.assertTrue(statements)
        self.assertTrue(all('data_json' not in sql for sql in statements), statements)

    async def test_cold_cache_bot_filter_does_not_hydrate_pix_payloads(self):
        main._STATE_CACHE = None
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            if 'pix_records' in statement.lower():
                statements.append(statement.lower())
        event.listen(main.engine.sync_engine, 'before_cursor_execute', observe)
        try:
            result = await main.get_panel_metrics(period='Tudo', bot_id='a')
        finally:
            event.remove(main.engine.sync_engine, 'before_cursor_execute', observe)
        self.assertEqual(result['paid_count'], 2)
        self.assertTrue(all('data_json' not in sql for sql in statements), statements)

    async def test_reused_configured_bot_id_cannot_claim_historical_pix(self):
        state = await main._read_state_from_db()
        state['tx_records']['p1']['bot_token'] = '222:new'
        await main._write_state_to_db(state)
        async with main.async_session() as session:
            row = (await session.execute(select(PixRecord).where(PixRecord.tx_id == 'p1'))).scalar_one()
            self.assertEqual(row.origin_bot_id, '111')
        metrics = await main.get_panel_metrics(period='Tudo', bot_id='a')
        self.assertEqual(metrics['by_bot'][0]['origin_bot_id'], '111')
