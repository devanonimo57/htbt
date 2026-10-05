"""Network-free checkout/webhook regression against real row persistence."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

import main
import telegram_service
from models import PixRecord


class CheckoutWebhookIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_engine, self.original_session = main.engine, main.async_session
        self.original_manager = main.bot_manager
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name, 'checkout.db').as_posix()}")
        main.engine = self.engine
        main.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        main._STATE_CACHE = None
        await main._create_schema()

        self.bot = SimpleNamespace(token="111:test-only", id=111, unban_chat_member=AsyncMock(return_value=True),
                                   send_message=AsyncMock(), create_chat_invite_link=AsyncMock())
        self.manager = telegram_service.ActiveBotManager()
        self.manager.bot = self.bot
        self.manager.current_token = self.bot.token
        self.manager.state_getter = main._read_state_from_db
        self.manager.state_saver = main._write_state_to_db
        main.bot_manager = self.manager
        self.state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        self.state['publish'].update(pushin_pay_token='gateway-test-only', gateway_provider='syncpay')
        self.state['flow_bots'] = [{
            'id': 'flow-a', 'bot_id': '111', 'token': self.bot.token, 'active': True,
            'mode': 'flow', 'sales_today': 0, 'revenue_today': 0,
            'plans': [{'id': 'plan-a', 'name': 'Acesso', 'price': 10,
                       'delivery_dest_type': 'Link externo', 'delivery_external_url': 'https://example.invalid/access'}],
            'fallback_delivery': {}, 'payments': {},
        }]
        await main._write_state_to_db(self.state)
        self.gateway_status = {}
        self.created = []
        def fake_gateway(*args, **kwargs):
            async def create_pix_charge(**charge):
                tx_id = f'gateway-tx-{len(self.created) + 1}'
                self.created.append((tx_id, charge))
                self.gateway_status[tx_id] = 'pending'
                return {'id': tx_id, 'qr_code': f'PIX-{tx_id}'}
            async def check_transaction_status(tx_id):
                return {'status': self.gateway_status[tx_id], 'raw': {'amount': 10}}
            return SimpleNamespace(create_pix_charge=create_pix_charge,
                                   check_transaction_status=check_transaction_status)
        self.gateway_patch = patch.multiple('telegram_service', PushinPayClient=fake_gateway)
        self.api_gateway_patch = patch.multiple('main', PushinPayClient=fake_gateway)
        self.gateway_patch.start()
        self.api_gateway_patch.start()
        self.user = types.User(id=7, is_bot=False, first_name='Cliente')
        self.callback = SimpleNamespace(from_user=self.user, bot=self.bot,
                                        message=SimpleNamespace(chat=SimpleNamespace(id=7)))

    async def asyncTearDown(self):
        self.api_gateway_patch.stop()
        self.gateway_patch.stop()
        main.bot_manager = self.original_manager
        main.engine, main.async_session = self.original_engine, self.original_session
        main._STATE_CACHE = None
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_two_pix_webhooks_count_each_once_and_keep_delivery_markers(self):
        for _ in range(2):
            state = await main._read_state_from_db()
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state,
                                                       'plan-a', 0, [])
        tx_ids = [tx_id for tx_id, _ in self.created]
        self.assertEqual(len(tx_ids), 2)
        self.assertEqual(len(set(tx_ids)), 2)
        main._STATE_CACHE = None
        persisted = await main._read_state_from_db()
        self.assertEqual({tx: persisted['tx_records'][tx]['status'] for tx in tx_ids},
                         dict.fromkeys(tx_ids, 'pending'))
        self.assertEqual((await main.get_panel_metrics(period='Tudo', bot_id='flow-a'))['pending_count'], 2)
        self.bot.send_message.reset_mock()  # Exclude the two checkout QR messages.

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            async def webhook(tx_id):
                response = await client.post('/api/webhook/syncpay', json={'id': tx_id, 'status': 'paid'})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            # A claimed paid webhook alone is not proof of payment.
            self.assertEqual((await webhook(tx_ids[0]))['action'], 'not_paid_at_gateway')
            self.bot.send_message.assert_not_awaited()
            # The newer charge settles before the older still-payable charge.
            for count, tx_id in enumerate(reversed(tx_ids), 1):
                self.gateway_status[tx_id] = 'paid'
                self.assertEqual((await webhook(tx_id))['action'], 'approved')
                metrics = await main.get_panel_metrics(period='Tudo', bot_id='flow-a')
                self.assertEqual(metrics['paid_count'], count)
                self.assertEqual(metrics['pending_count'], len(tx_ids) - count)
            self.assertEqual(self.bot.send_message.await_count, 2)
            before_repeat = await main.get_panel_metrics(period='Tudo', bot_id='flow-a')
            self.assertEqual(before_repeat['paid_count'], 2)
            self.assertEqual(before_repeat['paid_amount'], 20)
            self.assertEqual(before_repeat['pending_count'], 0)
            sends_before_repeat = self.bot.send_message.await_count
            await webhook(tx_ids[0])
            self.assertEqual(self.bot.send_message.await_count, sends_before_repeat)

        main._STATE_CACHE = None  # Assert persisted rows, not merely manager memory.
        saved = await main._read_state_from_db()
        for tx_id in tx_ids:
            rec = saved['tx_records'][tx_id]
            self.assertEqual(rec['status'], 'paid')
            self.assertTrue(rec['sales_counted'])
            self.assertTrue(rec['delivered'])
            self.assertEqual(rec['access_status'], 'Ativo')
            self.assertEqual(rec['automatic_delivery_attempts'], 1)
            self.assertEqual(rec['bot_token'], self.bot.token)
        self.assertEqual((await main.get_panel_metrics(period='Tudo', bot_id='flow-a'))['paid_count'], 2)
        async with main.async_session() as session:
            rows = (await session.execute(select(PixRecord))).scalars().all()
        self.assertEqual({row.tx_id for row in rows}, set(tx_ids))
