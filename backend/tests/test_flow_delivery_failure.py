"""A paid flow purchase is delivered only after Telegram accepts all access sends."""
import asyncio
import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import telegram_service

import main
from test_checkout_webhook_integration import CheckoutWebhookIntegration


class FlowDeliveryFailure(CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *args, **kwargs: None

    async def exercise_failure(self, destination='Link externo', bump=False):
        state = await main._read_state_from_db()
        plan = state['flow_bots'][0]['plans'][0]
        if destination == 'Canal ou grupo':
            plan.update(delivery_dest_type=destination, delivery_channel_id='-100111')
            self.bot.create_chat_invite_link.return_value = SimpleNamespace(invite_link='https://example.invalid/invite')
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx = self.created[-1][0]
        if bump:
            state = copy.deepcopy(await main._read_state_from_db())
            state['tx_records'][tx]['deliverable'] = 'Additional purchased access'
            await main._write_state_to_db(state)
        self.bot.send_message.reset_mock()
        # Bump-only failure proves accepting the main link alone is insufficient.
        self.bot.send_message.side_effect = ([SimpleNamespace(message_id=1), RuntimeError('offline'), RuntimeError('offline')]
                                              if bump else RuntimeError('offline'))
        self.gateway_status[tx] = 'paid'
        await main.webhook_gateway({'id': tx, 'status': 'paid'})
        main._STATE_CACHE = None
        rec = (await main._read_state_from_db())['tx_records'][tx]
        self.assertEqual(rec['status'], 'paid')
        self.assertTrue(rec['sales_counted'])
        self.assertFalse(rec['delivered'])
        self.assertNotEqual(rec['access_status'], 'Ativo')
        self.assertEqual(rec['automatic_delivery_attempts'], 1)
        self.assertTrue(rec.get('delivery_error'))
        sends = self.bot.send_message.await_count
        await main.webhook_gateway({'id': tx, 'status': 'paid'})
        self.assertEqual(self.bot.send_message.await_count, sends)
        # Simulate restart and expiry of the automatic delivery cooldown.
        class RetryTime(datetime):
            @classmethod
            def utcnow(cls):
                return datetime.utcnow() + timedelta(seconds=60)
        self.manager.tx_records.clear()
        self.bot.send_message.side_effect = RuntimeError('offline')
        with patch.object(telegram_service, 'datetime', RetryTime):
            await self.manager.deliver_purchase_and_upsell(self.user.id, tx)
        main._STATE_CACHE = None
        rec = (await main._read_state_from_db())['tx_records'][tx]
        self.assertFalse(rec['delivered'])
        self.assertEqual(rec['automatic_delivery_attempts'], 2)
        sends = self.bot.send_message.await_count
        await self.manager.deliver_purchase_and_upsell(self.user.id, tx)
        self.assertEqual(self.bot.send_message.await_count, sends)
        metrics = await main.get_panel_metrics(period='Tudo', bot_id='flow-a')
        self.assertEqual((metrics['paid_count'], metrics['paid_amount']), (1, 10))

    async def test_external_access_send_failure_keeps_paid_purchase_pending(self):
        await self.exercise_failure()

    async def test_channel_access_send_failure_keeps_paid_purchase_pending(self):
        await self.exercise_failure('Canal ou grupo')

    async def test_bump_send_failure_keeps_paid_purchase_pending(self):
        await self.exercise_failure(bump=True)
