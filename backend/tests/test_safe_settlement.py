"""SQLite regressions for snapshots competing at the payment persistence boundary."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main
from models import BotConfig, PixRecord, Order


class SafeSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (main.engine, main.async_session, main._STATE_CACHE,
                         main._STATE_COMMITTED, main._STATE_COMMITTED_OWNER)
        main.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.temp.name) / 'settlement.db').as_posix())
        main.async_session = sessionmaker(main.engine, class_=AsyncSession, expire_on_commit=False)
        main._STATE_CACHE = None
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await main._create_schema()

    async def asyncTearDown(self):
        await main.engine.dispose()
        (main.engine, main.async_session, main._STATE_CACHE,
         main._STATE_COMMITTED, main._STATE_COMMITTED_OWNER) = self.original
        self.temp.cleanup()

    async def seed(self, bots=('a',), ids=('one', 'two')):
        main._STATE_CACHE = None
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        async with main.async_session() as session:
            for model in (BotConfig, PixRecord, Order):
                for row in (await session.execute(select(model))).scalars().all():
                    await session.delete(row)
            await session.commit()
        state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        state['flow_bots'] = [dict(id=bot, bot_id=str(111 + index), token=f'{111 + index}:test',
                                   active=True, sales_today=0, revenue_today=0)
                              for index, bot in enumerate(bots)]
        state['tx_records'] = {tid: dict(tx_id=tid, user_id=index + 7, mode='flow',
                                         bot_id=bots[index % len(bots)], amount=10 + index * 5,
                                         status='pending', created_at='2026-10-02T12:00:00')
                               for index, tid in enumerate(ids)}
        await main._write_state_to_db(state)
        main._STATE_CACHE = None
        return copy.deepcopy(await main._read_state_from_db())

    async def disk_state(self):
        main._STATE_CACHE = None
        state = await main._read_state_from_db()
        async with main.async_session() as session:
            config = (await session.execute(select(BotConfig))).scalars().one()
            rows = {r.tx_id: r for r in (await session.execute(select(PixRecord))).scalars().all()}
            self.assertNotIn('tx_records', json.loads(config.full_state_json))
            self.assertEqual(set(rows), set(state['tx_records']))
            for tid, row in rows.items():
                self.assertEqual(json.loads(row.data_json), state['tx_records'][tid])
        return copy.deepcopy(state)

    async def test_distinct_paid_pix_from_stale_snapshots_accumulate_per_bot_and_learning(self):
        for bots in (('a',), ('a', 'b')):
            for order in (('one', 'two'), ('two', 'one')):
                with self.subTest(bots=bots, order=order):
                    base = await self.seed(bots)
                    snapshots = {tid: copy.deepcopy(base) for tid in order}
                    for tid in order:
                        snapshot = snapshots[tid]
                        rec = snapshot['tx_records'][tid]
                        rec.update(status='paid', sales_counted=True, paid_at='2026-10-02T13:00:00')
                        bot = next(b for b in snapshot['flow_bots'] if b['id'] == rec['bot_id'])
                        bot['sales_today'] += 1
                        bot['revenue_today'] += rec['amount']
                        snapshot['learning']['confirmed_purchases'] += 1
                        await main._write_state_to_db(snapshot)
                    result = await self.disk_state()
                    self.assertEqual([result['tx_records'][tid]['status'] for tid in order], ['paid', 'paid'])
                    self.assertEqual(result['learning']['confirmed_purchases'], 2)
                    for bot in result['flow_bots']:
                        expected = [r for r in result['tx_records'].values() if r['bot_id'] == bot['id']]
                        self.assertEqual(bot['sales_today'], len(expected))
                        self.assertEqual(bot['revenue_today'], sum(r['amount'] for r in expected))

    async def test_duplicate_paid_snapshot_does_not_count_twice(self):
        base = await self.seed(ids=('one',))
        first = copy.deepcopy(base)
        second = copy.deepcopy(base)
        for snapshot in (first, second):
            snapshot['tx_records']['one'].update(status='paid', sales_counted=True)
            snapshot['flow_bots'][0]['sales_today'] = 1
            snapshot['flow_bots'][0]['revenue_today'] = 10
            snapshot['learning']['confirmed_purchases'] = 1
            await main._write_state_to_db(snapshot)
        result = await self.disk_state()
        self.assertEqual(result['flow_bots'][0]['sales_today'], 1)
        self.assertEqual(result['flow_bots'][0]['revenue_today'], 10)
        self.assertEqual(result['learning']['confirmed_purchases'], 1)

    async def test_paid_to_paid_stale_delivery_write_retains_durable_metadata(self):
        base = await self.seed(ids=('one',))
        base['tx_records']['one'].update(status='paid', sales_counted=True)
        await main._write_state_to_db(base)
        main._STATE_CACHE = None
        old = copy.deepcopy(await main._read_state_from_db())
        fresh = copy.deepcopy(old)
        fresh['tx_records']['one'].update(delivered=True, invite_url='https://example.invalid/a',
                                          expires_at='2026-11-01T00:00:00', delivery_attempts=2,
                                          automatic_delivery_attempts=2,
                                          last_delivery_attempt_at='2026-10-02T13:05:00')
        await main._write_state_to_db(fresh)
        old['tx_records']['one'].update(delivery_error='stale attempt', delivery_attempts=1,
                                        automatic_delivery_attempts=1,
                                        last_delivery_attempt_at='2026-10-02T13:04:00')
        await main._write_state_to_db(old)
        rec = (await self.disk_state())['tx_records']['one']
        for key in ('delivered', 'invite_url', 'expires_at', 'delivery_attempts',
                    'automatic_delivery_attempts', 'last_delivery_attempt_at'):
            self.assertEqual(rec[key], fresh['tx_records']['one'][key], key)

    async def test_deleted_pending_pix_cannot_be_resurrected_by_late_paid_snapshot(self):
        base = await self.seed(ids=('one',))
        late = copy.deepcopy(base)
        deletion = copy.deepcopy(base)
        deletion['tx_records'].pop('one')
        await main._write_state_to_db(deletion, delete_tx_ids=['one'])
        late['tx_records']['one'].update(status='paid', sales_counted=True)
        late['flow_bots'][0].update(sales_today=1, revenue_today=10)
        late['learning']['confirmed_purchases'] = 1
        await main._write_state_to_db(late)
        result = await self.disk_state()
        self.assertNotIn('one', result['tx_records'])
        self.assertEqual(result['flow_bots'][0]['sales_today'], 0)
        self.assertEqual(result['learning']['confirmed_purchases'], 0)

    async def test_explicit_deletion_and_stale_snapshot_do_not_recreate_paid_pix(self):
        base = await self.seed(ids=('one',))
        base['tx_records']['one'].update(status='paid', sales_counted=True)
        base['flow_bots'][0].update(sales_today=1, revenue_today=10)
        base['learning']['confirmed_purchases'] = 1
        await main._write_state_to_db(base)
        main._STATE_CACHE = None
        stale = copy.deepcopy(await main._read_state_from_db())
        deletion = copy.deepcopy(stale)
        deletion['tx_records'].pop('one')
        deletion['flow_bots'][0].update(sales_today=0, revenue_today=0)
        deletion['learning']['confirmed_purchases'] = 0
        await main._write_state_to_db(deletion, delete_tx_ids=['one'])
        stale['memory']['leads'].append({'telegram_id': '42', 'bot_id': 'a'})
        await main._write_state_to_db(stale)
        result = await self.disk_state()
        self.assertNotIn('one', result['tx_records'])
        self.assertEqual(result['flow_bots'][0]['sales_today'], 0)
        self.assertEqual(result['learning']['confirmed_purchases'], 0)
