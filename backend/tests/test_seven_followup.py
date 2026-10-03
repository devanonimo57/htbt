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
