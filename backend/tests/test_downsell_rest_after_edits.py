"""A newly active initial card invalidates rest from the previous passage."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
import test_gemini_downsell_contract as fixture
import test_downsell_remaining_blockers as blockers


class RestAfterInitialEdit(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GeminiDownsell.asyncSetUp
    asyncTearDown = fixture.GeminiDownsell.asyncTearDown
    seq = fixture.GeminiDownsell.seq
    config = fixture.GeminiDownsell.config
    start = fixture.GeminiDownsell.start
    checkout = fixture.GeminiDownsell.checkout
    messages = fixture.GeminiDownsell.messages
    cold = fixture.GeminiDownsell.cold
    step = fixture.GeminiDownsell.step
    until = blockers.RemainingDownsell.until

    async def add_card(self, card):
        state = copy.deepcopy(await self.cold())
        state['flow_bots'][0]['downsell_general']['sequences'].append(card)
        with patch.object(self.manager, 'configure_and_start', AsyncMock(return_value={'status': 'running'})), patch.object(self.manager, 'warm_all_state_medias', AsyncMock(return_value=0)):
            response = await self.client.post('/api/state', json=state)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.cold())['flow_bots'][0]['downsell_general'], state['flow_bots'][0]['downsell_general'])

    async def new_initial_card(self, trigger, restart=False):
        await self.config([self.seq('A', loop_enabled=True)])
        await self.start()
        await self.until(['A'])
        old_rest = (await self.cold())['memory']['leads'][0]['flow_downsell']['rest_until']
        card = self.seq('P')
        card['trigger'] = trigger
        await self.add_card(card)
        if trigger == 'pix':
            for _ in range(4):
                self.assertEqual(await self.step(), 60)
            await self.gate.held()
            self.assertEqual(self.messages(), ['A'])
        if restart:
            task = self.manager.downsell_tasks['111:7']
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.manager.downsell_tasks.clear()
            self.manager._resume_flow_downsells(await self.cold())
            await self.gate.held()
        if trigger == 'pix':
            await self.checkout()
        observed = []
        async def send(**kwargs):
            observed.append((kwargs['text'], fixture.Clock.value.timestamp()))
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = send
        await self.gate.wake()
        await self.until(['A', 'P', 'A'])
        self.assertEqual([name for name, _ in observed], ['P', 'A'])
        gap = observed[1][1] - observed[0][1]
        self.assertGreaterEqual(gap, 180, 'Rest must start after the current initial passage completes')
        if trigger == 'pix':
            self.assertGreater(observed[0][1], old_rest)
        progress = (await self.cold())['memory']['leads'][0]['flow_downsell']
        self.assertEqual(progress['sent'], ['A', 'P'])
        self.assertEqual(self.messages().count('P'), 1)

    async def test_late_pix_initial_card_restarts_elapsed_rest(self):
        await self.new_initial_card('pix')

    async def test_start_initial_card_restarts_unexpired_rest(self):
        await self.new_initial_card('start')

    async def test_cold_resume_with_added_pix_card_restarts_elapsed_rest(self):
        await self.new_initial_card('pix', restart=True)
