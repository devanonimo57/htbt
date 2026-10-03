"""Offline regressions for the seven follow-up findings."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import main
import telegram_service
import test_checkout_webhook_integration as fixture

class SevenFollowup(fixture.CheckoutWebhookIntegration):
    async def test_runtime_write_keeps_verified_identity_metadata(self):
        state = await main._read_state_from_db()
        state['publish']['bot_token'] = self.bot.token
        await main._write_state_to_db(state)
        state = copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['username'] = '@verified'
        state['publish']['bot_username'] = '@verified'
        edited = main._panel_state(await main._read_state_from_db())
        edited['publish']['support_username'] = '@new_support'
        await main._write_state_to_db(edited, preserve_runtime=True)
        await main._write_state_to_db(state, runtime_only=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['flow_bots'][0].get('username'), '@verified')
        self.assertEqual(saved['publish'].get('bot_username'), '@verified')

    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def test_delivery_keeps_config_saved_during_send(self):
        state = await main._read_state_from_db()
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx = self.created[-1][0]
        self.gateway_status[tx] = 'paid'
        started, finish = asyncio.Event(), asyncio.Event()
        async def held_send(**kwargs):
            started.set()
            await finish.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = held_send
        delivery = asyncio.create_task(main.webhook_gateway({'id':tx, 'status':'paid'}))
        try:
            await asyncio.wait_for(started.wait(), 5)
            edited = main._panel_state(await main._read_state_from_db())
            edited['publish']['support_username'] = '@new_support'
            edited['flow_bots'][0]['plans'][0]['delivery_external_url'] = 'https://example.invalid/new'
            await main._write_state_to_db(edited, preserve_runtime=True)
        finally:
            finish.set()
            await delivery
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['publish']['support_username'], '@new_support')
        self.assertEqual(saved['flow_bots'][0]['plans'][0]['delivery_external_url'], 'https://example.invalid/new')
        self.assertTrue(saved['tx_records'][tx]['delivered'])
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 1)

    async def test_manual_retry_completes_purchased_bump_without_duplicate_sale(self):
        state = await main._read_state_from_db()
        bump = {'title':'Extra', 'price':0, 'deliverable':'https://example.invalid/extra'}
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [bump])
        tx = self.created[-1][0]
        self.gateway_status[tx] = 'paid'
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=1), RuntimeError('bump failed'), RuntimeError('bump failed')]
        await main.webhook_gateway({'id':tx, 'status':'paid'})
        manual = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('manual bump failed')), session=SimpleNamespace(close=AsyncMock()))
        with patch.object(telegram_service, '_create_bot_instance', return_value=manual):
            failed = await main.resend_vip_access(main.AccessResendRequest(tx_id=tx))
            main._STATE_CACHE = None
            saved = await main._read_state_from_db()
            self.assertFalse(saved['tx_records'][tx]['delivered'])
            self.assertEqual(failed['sent_count'], 0)
            self.assertEqual(saved['tx_records'][tx].get('manual_delivery_attempts', 0), 1)
            manual.send_message.reset_mock(side_effect=True)
            manual.send_message.return_value = SimpleNamespace(message_id=2)
            result = await main.resend_vip_access(main.AccessResendRequest(tx_ids=[tx,tx]))
        texts = [c.kwargs['text'] for c in manual.send_message.await_args_list]
        self.assertEqual(len(texts), 1)
        self.assertIn(bump['deliverable'], texts[0])
        self.assertEqual(result['sent_count'], 1)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertTrue(saved['tx_records'][tx]['delivered'])
        self.assertTrue(saved['tx_records'][tx]['additional_delivered'])
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 1)
        before = self.bot.send_message.await_count
        self.assertTrue(await self.manager.deliver_purchase_and_upsell(7, tx))
        self.assertEqual(self.bot.send_message.await_count, before)

    async def test_channel_retry_only_sends_unconfirmed_additional(self):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['plans'][0].update(delivery_dest_type='Canal ou grupo', delivery_channel_id='-1001')
        await main._write_state_to_db(state)
        bump = {'title':'Extra','price':0,'deliverable':'Purchased additional'}
        self.bot.create_chat_invite_link.return_value = SimpleNamespace(invite_link='https://t.me/+test')
        await self.manager._generate_flow_checkout(self.callback,state['flow_bots'][0],state,'plan-a',0,[bump])
        tx = self.created[-1][0]
        self.gateway_status[tx] = 'paid'
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=1),RuntimeError('extra failed'),RuntimeError('extra failed')]
        await main.webhook_gateway({'id':tx,'status':'paid'})
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['tx_records'][tx].get('invite_url'), 'https://t.me/+test')
        self.bot.create_chat_invite_link.side_effect = RuntimeError('channel unavailable now')
        self.bot.send_message.reset_mock(side_effect=True)
        self.bot.send_message.return_value = SimpleNamespace(message_id=2)
        from datetime import datetime, timedelta
        class Later(datetime):
            @classmethod
            def utcnow(cls): return datetime.utcnow() + timedelta(minutes=10)
        with patch.object(telegram_service,'datetime',Later):
            self.assertTrue(await self.manager.deliver_purchase_and_upsell(7,tx))
        self.assertEqual(self.bot.create_chat_invite_link.await_count,1)
        self.assertEqual(self.bot.send_message.await_count,1)
        self.assertIn(bump['deliverable'], self.bot.send_message.await_args.kwargs['text'])


