"""Intentionally RED probes of preserved base worker, never suite-discovered.
Explicit runner invocation only; candidate product files remain unchanged.
"""
import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import main
import test_gemini_downsell_contract as controls

SOURCE=Path('C:/Users/Yago/hotbot-overnight-audit/downsell-original-red/telegram_service.original.py')
spec=importlib.util.spec_from_file_location('preserved_gemini_downsell',SOURCE)
original=importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)

class OriginalDownsellWorker(unittest.IsolatedAsyncioTestCase):
    worker_class=original.ActiveBotManager
    asyncSetUp=controls.GeminiDownsell.asyncSetUp
    asyncTearDown=controls.GeminiDownsell.asyncTearDown
    seq=controls.GeminiDownsell.seq
    config=controls.GeminiDownsell.config
    messages=controls.GeminiDownsell.messages
    step=controls.GeminiDownsell.step
    cold=controls.GeminiDownsell.cold

    async def worker(self, seqs):
        await self.config(seqs)
        state=await main._read_state_from_db()
        lead=await self.manager._get_or_create_lead(state,self.user,bot_token=self.bot.token)
        await main._write_state_to_db(state,runtime_only=True)
        task=asyncio.create_task(self.worker_class._flow_downsell_worker(
            self.manager,self.user,'general',bot_token=self.bot.token))
        self.manager.downsell_tasks['111:7']=task
        return task

    async def test_original_rest_interval_not_consumed(self):
        task=await self.worker([self.seq('A',loop_enabled=True)])
        await self.step()
        delay=await self.gate.held()
        self.assertGreaterEqual(delay,180,'Selected-loop first repetition must consume 3-minute rest')

    async def test_original_all_audience_still_sends_after_cold_paid_worker_wakeup(self):
        task=await self.worker([self.seq('A')])
        await self.gate.held()
        state=await main._read_state_from_db()
        lead=state['memory']['leads'][0]
        lead['stage']='Entrega concluída';lead['paid']=True
        state['tx_records']['any-paid']=dict(tx_id='any-paid',user_id=7,bot_token=self.bot.token,
            origin_bot_numeric_id='111',bot_id='flow-a',status='paid',amount=10)
        await main._write_state_to_db(state);await self.cold()
        await self.gate.wake();await asyncio.wait_for(task,3)
        self.assertEqual(self.messages(),[],'Payment must stop all audiences, not just new')

    async def test_original_disabled_deleted_card_snapshot_sends_on_real_wakeup(self):
        task=await self.worker([self.seq('A')])
        await self.gate.held()
        state=await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general'].update(enabled=False,sequences=[])
        await main._write_state_to_db(state)
        await self.gate.wake();await asyncio.wait_for(task,3)
        self.assertEqual(self.messages(),[],'Worker must reload saved disable/delete after sleeping')

    async def test_original_repetition_before_rest_elapsed(self):
        task=await self.worker([self.seq('A',loop_enabled=True)])
        await self.step()
        await self.gate.held()  # initial send committed, first loop wait held
        self.assertEqual(self.messages(),['A'])
        first=controls.Clock.value.timestamp()
        await self.gate.wake()
        await self.gate.held()  # actual worker has completed this wakeup
        self.assertLess(controls.Clock.value.timestamp()-first,180)
        self.assertEqual(self.messages(),['A'],'No repeat before the configured 3-minute rest elapses')


class CandidateDownsellWorker(unittest.IsolatedAsyncioTestCase):
    worker_class=controls.service.ActiveBotManager
    asyncSetUp=OriginalDownsellWorker.asyncSetUp
    asyncTearDown=OriginalDownsellWorker.asyncTearDown
    seq=OriginalDownsellWorker.seq
    config=OriginalDownsellWorker.config
    messages=OriginalDownsellWorker.messages
    step=OriginalDownsellWorker.step
    cold=OriginalDownsellWorker.cold
    worker=OriginalDownsellWorker.worker
    test_payment_same_assertion=OriginalDownsellWorker.test_original_all_audience_still_sends_after_cold_paid_worker_wakeup
    test_disable_delete_same_assertion=OriginalDownsellWorker.test_original_disabled_deleted_card_snapshot_sends_on_real_wakeup
    test_rest_elapsed_same_assertion=OriginalDownsellWorker.test_original_repetition_before_rest_elapsed
