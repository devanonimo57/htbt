"""Checkout boundary regressions with isolated durable rows."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch
import main
import telegram_service
import test_checkout_webhook_integration as fixture

class SevenCheckoutBoundaries(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    def gate_gateway(self, total):
        original = telegram_service.PushinPayClient
        reached, finish = asyncio.Event(), asyncio.Event()
        arrived = 0
        def client(*a, **kw):
            underlying = original(*a, **kw)
            async def create(**charge):
                nonlocal arrived
                response = await underlying.create_pix_charge(**charge)
                arrived += 1
                if arrived == total:
                    reached.set()
                await finish.wait()
                return response
            return SimpleNamespace(create_pix_charge=create)
        return patch.object(telegram_service, 'PushinPayClient', new=client), reached, finish

    async def test_checkout_on_a_cancels_b_downsell_for_same_user(self):
        state = copy.deepcopy(await main._read_state_from_db())
        other = copy.deepcopy(state['flow_bots'][0])
        other.update(id='flow-b', token='222:fake-secondary', bot_id='222')
        state['flow_bots'].append(other)
        await main._write_state_to_db(state)
        blocked = asyncio.Event()
        async def waiting_worker(*a, **kw):
            await blocked.wait()
        self.manager._flow_downsell_worker = waiting_worker
        self.manager._schedule_flow_downsell = type(self.manager)._schedule_flow_downsell.__get__(self.manager)
        tasks = []
        try:
            self.manager._schedule_flow_downsell(self.user, bot_token=other['token'])
            task_b = self.manager.downsell_tasks['222:7']
            tasks.append(task_b)
            self.manager._schedule_flow_downsell(self.user, bot_token=self.bot.token)
            task_a = self.manager.downsell_tasks['111:7']
            tasks.append(task_a)
            await asyncio.sleep(0)
            self.assertFalse(task_b.done())
            self.assertFalse(task_a.done())
            current = copy.deepcopy(await main._read_state_from_db())
            await self.manager._generate_flow_checkout(self.callback, current['flow_bots'][0], current, 'plan-a', 0, [])
            await asyncio.sleep(0)
            tasks.extend(self.manager.downsell_tasks.values())
            self.assertFalse(task_b.done())
            self.assertIs(self.manager.downsell_tasks['222:7'], task_b)
            self.assertIn('111:7', self.manager.downsell_tasks)
            self.assertTrue(task_a.cancelled())
        finally:
            blocked.set()
            for task in set(tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*set(tasks), return_exceptions=True)

