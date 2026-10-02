import unittest
from tests.test_row_persistence import RowPersistenceTests
import main
import httpx
from sqlalchemy import select
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

    async def test_reused_configured_bot_id_cannot_claim_historical_pix(self):
        state = await main._read_state_from_db()
        state['tx_records']['p1']['bot_token'] = '222:new'
        await main._write_state_to_db(state)
        async with main.async_session() as session:
            row = (await session.execute(select(PixRecord).where(PixRecord.tx_id == 'p1'))).scalar_one()
            self.assertEqual(row.origin_bot_id, '111')
