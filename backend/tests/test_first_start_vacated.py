"""NEW-02: removal of a cache-only first admission is not another entry."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Dispatcher
import main
import telegram_service
import test_checkout_webhook_integration as fixture


class FirstStartVacated(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.bot.id = 111
        self.manager.dp = Dispatcher()
        self.manager._register_handlers()
        telegram_service._CURRENT_BOT_TOKEN_CTX.set('')
        telegram_service._CURRENT_BOT_CTX.set(None)
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')
        state = await main._read_state_from_db()
        state['flow_bots'][0].update(welcome={'message': 'FIRST WELCOME'}, downsell_general={
            'enabled': False, 'sequences': [{'id': 'first-followup', 'delay_minutes': 1, 'message': 'FOLLOWUP'}]})
        await main._write_state_to_db(state)

    async def asyncTearDown(self):
        tasks = set(self.manager.downsell_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        main.app.dependency_overrides.clear()
        await self.client.aclose()
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    def start(self, answer):
        handler = next(h.callback for h in self.manager.dp.message.handlers if h.callback.__name__ == 'handle_start')
        return handler(SimpleNamespace(bot=self.bot, from_user=self.user, chat=SimpleNamespace(id=7), answer=answer, text='/start'))

    async def remove(self, permanent):
        response = await self.client.delete('/api/leads/7?bot_id=flow-a')
        self.assertEqual(response.status_code, 200, response.text)
        if permanent:
            response = await self.client.delete('/api/leads/7/permanent?bot_id=flow-a')
            self.assertEqual(response.status_code, 200, response.text)

    async def cold(self):
        main._STATE_CACHE = None
        return await main._read_state_from_db()

    async def vacated(self, permanent):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['downsell_general']['enabled'] = True
        await main._write_state_to_db(state)
        real_sleep = asyncio.sleep
        reached, finish, sleeping, wake = (asyncio.Event() for _ in range(4))
        async def answer(*args, **kwargs):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            await finish.wait()
            return SimpleNamespace(message_id=1)
        async def sleep(seconds):
            if seconds != 60:
                return await real_sleep(seconds)
            sleeping.set()
            await wake.wait()
        with patch.object(telegram_service.asyncio, 'sleep', new=sleep):
            task = asyncio.create_task(self.start(answer))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                held = await main._read_state_from_db()
                self.assertNotIn(('111', '7', False), main._client_map(held._row_base))
                await self.remove(permanent)
                before = copy.deepcopy((await main._read_state_from_db())['memory'])
                finish.set()
                await asyncio.wait_for(task, 5)
                # Exercise the actual scheduler installed by completion and its
                # worker, rather than substituting a no-op in this regression.
                worker = self.manager.downsell_tasks['111:7']
                await asyncio.wait_for(sleeping.wait(), 5)
                wake.set()
                await asyncio.wait_for(worker, 5)
            finally:
                finish.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)
        await main._write_state_to_db(await main._read_state_from_db(), runtime_only=True)
        self.assertEqual((await self.cold())['memory'], before)
        self.bot.send_message.assert_not_awaited()

    async def test_archive_no_reentry_old_first_answer_and_real_worker(self):
        await self.vacated(False)

    async def test_permanent_no_reentry_old_first_answer_and_real_worker(self):
        await self.vacated(True)

    async def test_detached_first_completion_cannot_admit_vacated_row(self):
        live = await main._read_state_from_db()
        await self.manager._get_or_create_lead(live, self.user, bot_token=self.bot.token)
        stale = copy.deepcopy(live)
        await self.remove(True)
        stale['memory']['leads'][0]['history'].append({'role': 'assistant', 'content': 'LATE FIRST'})
        await main._write_state_to_db(stale, runtime_only=True)
        self.assertEqual((await self.cold())['memory'], {'leads': [], 'deleted_leads': []})

    async def test_legitimate_fresh_start_after_permanent_is_durable(self):
        await self.start(AsyncMock(return_value=SimpleNamespace(message_id=1)))
        original = (await main._read_state_from_db())['memory']['leads'][0]['created_at']
        await self.remove(True)
        await self.start(AsyncMock(return_value=SimpleNamespace(message_id=2)))
        lead = (await self.cold())['memory']['leads'][0]
        self.assertNotEqual(lead['created_at'], original)
        self.assertEqual([h['content'] for h in lead['history']], ['FIRST WELCOME'])
        self.assertEqual(lead['stage'], 'Viu os planos')

    async def test_lost_flow_lookup_first_completion_and_fallback_worker_do_not_recreate(self):
        state = await main._read_state_from_db()
        # Faithful fallback: the originating flow bot no longer resolves as a
        # flow bot. Do not substitute the handler or the actual AI worker.
        state['flow_bots'][0]['mode'] = 'ai'
        state['style'].update(opening_messages=['FALLBACK FIRST'], opening_variations=[])
        state['downsell'] = {'enabled': True, 'steps': [{'id': 'fallback-step', 'delay_minutes': 1, 'message': 'STALE FOLLOWUP'}]}
        await main._write_state_to_db(state)
        self.manager._get_cadence_delay = lambda state: 0
        self.bot.send_chat_action = AsyncMock()
        reached, finish, sleeping, wake = (asyncio.Event() for _ in range(4))
        real_sleep = asyncio.sleep
        async def answer(*args, **kwargs):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            await finish.wait()
        async def sleep(seconds):
            if seconds != 60:
                return await real_sleep(seconds)
            sleeping.set()
            await wake.wait()
        with patch.object(telegram_service.asyncio, 'sleep', new=sleep):
            task = asyncio.create_task(self.start(answer))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                response = await self.client.delete('/api/leads/7')
                self.assertEqual(response.status_code, 200, response.text)
                response = await self.client.delete('/api/leads/7/permanent')
                self.assertEqual(response.status_code, 200, response.text)
                finish.set()
                await asyncio.wait_for(task, 5)
                worker = self.manager.downsell_tasks['111:7']
                # Give the real worker a chance to start. Correct behavior may
                # finish before sleeping because the customer was removed.
                worker_started = asyncio.create_task(sleeping.wait())
                await asyncio.wait([worker, worker_started], timeout=5, return_when=asyncio.FIRST_COMPLETED)
                wake.set()
                await asyncio.wait_for(worker, 5)
                worker_started.cancel()
                await asyncio.gather(worker_started, return_exceptions=True)
            finally:
                finish.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)
        await main._write_state_to_db(await main._read_state_from_db(), runtime_only=True)
        self.assertEqual((await self.cold())['memory'], {'leads': [], 'deleted_leads': []})
        self.bot.send_message.assert_not_awaited()

    async def send_path_removed(self, permanent, path):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['welcome'] = {
            'message': '' if path == 'menu' else 'MEDIA OR FALLBACK',
            'medias': [] if path == 'fallback' else [
                {'id': 'welcome-photo', 'type': 'photo', 'url': 'data:image/png;base64,AAAA'}]}
        await main._write_state_to_db(state)
        reached, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def hold():
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            await finish.wait()
        async def photo(*args, **kwargs):
            if path == 'media':
                await hold()
                return SimpleNamespace(photo=[SimpleNamespace(file_id='welcome-native-id')])
            raise RuntimeError('media cannot attach menu')
        async def answer(*args, **kwargs):
            calls.append(kwargs)
            if path == 'fallback' and kwargs.get('parse_mode'):
                raise RuntimeError('HTML rejected')
            if path != 'media':
                await hold()
            return SimpleNamespace(message_id=1)
        self.bot.send_photo = AsyncMock(side_effect=photo)
        task = asyncio.create_task(self.start(answer))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            held = await main._read_state_from_db()
            self.assertNotIn(('111', '7', False), main._client_map(held._row_base))
            await self.remove(permanent)
            before = copy.deepcopy((await main._read_state_from_db())['memory'])
            finish.set()
            await asyncio.wait_for(task, 5)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        worker = self.manager.downsell_tasks['111:7']
        await asyncio.wait_for(worker, 5)  # disabled follow-up still uses real worker
        await main._write_state_to_db(await main._read_state_from_db(), runtime_only=True)
        cold = await self.cold()
        self.assertEqual(cold['memory'], before)
        if path == 'media':
            self.assertEqual(cold['flow_bots'][0]['welcome']['medias'][0]['telegram_file_id'], 'welcome-native-id')
        if path == 'menu':
            self.assertIn('reply_markup', calls[-1])
        if path == 'fallback':
            self.assertEqual(len(calls), 2)
            self.assertNotIn('parse_mode', calls[-1])

    async def test_archive_media_nested_writer_no_reentry(self):
        await self.send_path_removed(False, 'media')

    async def test_permanent_media_nested_writer_no_reentry(self):
        await self.send_path_removed(True, 'media')

    async def test_archive_failed_media_plan_menu_no_reentry(self):
        await self.send_path_removed(False, 'menu')

    async def test_permanent_failed_html_fallback_reply_no_reentry(self):
        await self.send_path_removed(True, 'fallback')

    async def test_staged_first_insert_and_copies_without_creation_time_remain_admissible(self):
        staged = copy.deepcopy(await main._read_state_from_db())
        lead = await self.manager._get_or_create_lead(staged, self.user, bot_token=self.bot.token)
        lead.pop('created_at')
        lead['history'].append({'role': 'user', 'content': 'GENUINE NEW ADMISSION'})
        staged = copy.deepcopy(staged)
        await main._write_state_to_db(staged, runtime_only=True)
        self.assertEqual((await self.cold())['memory']['leads'][0], lead)
