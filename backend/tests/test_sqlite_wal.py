"""Payment SQLite connections must use durable WAL on a patched runtime."""
import unittest
from sqlalchemy import text
import main
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch
import httpx
import test_checkout_webhook_integration as fixtures
from sqlite_policy import create_application_engine, sqlite_wal_runtime_is_patched

REPORT = Path(os.environ.get('HOTBOT_REPORT', str(Path(__file__).resolve().parent / 'report')))
REPORT.mkdir(parents=True, exist_ok=True)


class SQLiteRuntimeSafety(unittest.TestCase):
    def test_fixed_releases_and_unsafe_releases(self):
        for version in ((3,51,3), (3,53,4), (3,50,7), (3,44,6)):
            self.assertTrue(sqlite_wal_runtime_is_patched(version), version)
        for version in ((3,45,1), (3,51,2), (3,50,6), (3,44,5)):
            self.assertFalse(sqlite_wal_runtime_is_patched(version), version)

    def test_unsafe_file_runtime_fails_closed_and_other_urls_stay_intact(self):
        with patch('sqlite_policy.sqlite3.sqlite_version_info', (3,45,1)):
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):
                create_application_engine('sqlite+aiosqlite:///unused.db')
            with patch('sqlite_policy.create_async_engine', return_value='untouched') as factory:
                url = 'postgresql+asyncpg://example.invalid/hotbot'
                self.assertEqual(create_application_engine(url, echo=False), 'untouched')
                factory.assert_called_once_with(url, echo=False)
                factory.reset_mock()
                self.assertEqual(create_application_engine('sqlite+aiosqlite:///:memory:'), 'untouched')


class WalPaymentLifecycle(fixtures.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        with patch.object(fixtures, 'create_async_engine', create_application_engine):
            await super().asyncSetUp()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None

    async def test_reader_beyond_old_timeout_does_not_block_verified_payment(self):
        state = await main._read_state_from_db()
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx = self.created[-1][0]
        self.gateway_status[tx] = 'paid'
        reader = sqlite3.connect(self.engine.url.database)
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM bot_configs').fetchall()
        try:
            await asyncio.sleep(5.2)
            started = time.perf_counter()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/api/webhook/syncpay', json={'id':tx,'status':'paid'})
            elapsed = time.perf_counter()-started
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['action'], 'approved')
            self.assertLess(elapsed, 5)
            main._STATE_CACHE = None
            saved = await main._read_state_from_db()
            self.assertTrue(saved['tx_records'][tx]['sales_counted'])
            self.assertTrue(saved['tx_records'][tx]['delivered'])
            Path(REPORT, 'long-reader.json').write_text(json.dumps({'reader_held_seconds_before_payment':5.2,'payment_seconds':elapsed,'reader_open_through_commit':True,'action':response.json()['action'],'sqlite':sqlite3.sqlite_version}))
        finally:
            reader.rollback()
            reader.close()

    async def test_restart_and_online_backup_retain_populated_paid_rows(self):
        await self.test_two_pix_webhooks_count_each_once_and_keep_delivery_markers()
        before = {k:dict(v) for k,v in (await main._read_state_from_db())['tx_records'].items()}
        await main._create_schema()
        await main._create_schema()
        main._STATE_CACHE = None
        self.assertEqual((await main._read_state_from_db())['tx_records'], before)
        backup_path = Path(self.tmp.name, 'safe-backup.db')
        source = sqlite3.connect(self.engine.url.database)
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
            self.assertEqual(dest.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(dest.execute('SELECT COUNT(*) FROM pix_records').fetchone()[0], 2)
            rows = dest.execute('SELECT data_json FROM pix_records').fetchall()
            self.assertTrue(all(json.loads(r[0])['delivered'] for r in rows))
        finally:
            dest.close()
            source.close()

    async def test_competing_write_wait_is_finite(self):
        blocker = sqlite3.connect(self.engine.url.database)
        blocker.execute('BEGIN IMMEDIATE')
        started = time.perf_counter()
        try:
            with self.assertRaisesRegex(Exception, 'database is locked'):
                async with self.engine.begin() as conn:
                    await conn.execute(text('UPDATE bot_configs SET full_state_json=full_state_json'))
            elapsed = time.perf_counter()-started
            self.assertGreaterEqual(elapsed, 4.5)
            self.assertLess(elapsed, 8)
        finally:
            blocker.rollback()
            blocker.close()

class SQLiteWalConnection(unittest.IsolatedAsyncioTestCase):
    async def test_application_connection_uses_wal_full_and_finite_busy_wait(self):
        async with main.engine.connect() as first:
            async with main.engine.connect() as second:
                for connection in (first, second):
                    mode = (await connection.execute(text('PRAGMA journal_mode'))).scalar()
                    self.assertEqual(mode, 'wal')
                    self.assertEqual((await connection.execute(text('PRAGMA synchronous'))).scalar(), 2)
                    timeout = (await connection.execute(text('PRAGMA busy_timeout'))).scalar()
                    self.assertEqual(timeout, 5000)
