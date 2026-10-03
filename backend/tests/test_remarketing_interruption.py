"""Network-free ASGI regression for interrupted remarketing deliveries."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main
import telegram_service


class RemarketingInterruption(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_engine, self.original_session = main.engine, main.async_session
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'remarketing.db').as_posix())
        main.engine = self.engine
        main.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        main._STATE_CACHE = None
        await main._create_schema()
        state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        state['flow_bots'] = [{'id': 'flow', 'token': '111:fake'}]
        state['remarketing_campaigns'] = [{'id': 'camp', 'bot_id': 'flow', 'segment': 'all',
                                           'message': 'Oferta', 'include_plans': False}]
        state['memory']['leads'] = [
            {'telegram_id': str(uid), 'bot_id': 'flow', 'origin_bot_numeric_id': '111', 'history': []}
            for uid in (41, 42, 43)
        ]
        await main._write_state_to_db(state)
        self.sent = []
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()
        async def send_message(*, chat_id, **kwargs):
            if chat_id == 42 and not self.release_second.is_set():
                self.second_started.set()
                await self.release_second.wait()
            self.sent.append(chat_id)
        self.bot = SimpleNamespace(send_message=send_message,
                                   session=SimpleNamespace(close=self._close))
        self.bot_patch = patch.object(telegram_service, '_create_bot_instance', return_value=self.bot)
        self.bot_patch.start()
        self.auth_patch = patch.object(main, 'verify_admin_auth', return_value=True)
        self.auth_patch.start()
        # FastAPI resolves the dependency through dependency_overrides, not patching the symbol.
        self.auth_dependency = next(d.call for route in main.app.routes
                                    if getattr(route, 'path', None) == '/api/flow-bots/remarketing/send'
                                    for d in route.dependant.dependencies)
        main.app.dependency_overrides[self.auth_dependency] = lambda: True
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def _close(self):
        pass

    async def asyncTearDown(self):
        self.release_second.set()
        await self.client.aclose()
        main.app.dependency_overrides.pop(self.auth_dependency, None)
        self.auth_patch.stop()
        self.bot_patch.stop()
        main.engine, main.async_session = self.original_engine, self.original_session
        main._STATE_CACHE = None
        await self.engine.dispose()
        self.tmp.cleanup()

    async def persisted(self):
        main._STATE_CACHE = None
        return await main._read_state_from_db()

    async def test_rate_limited_recipient_remains_retryable_without_false_finalization(self):
        original_send = self.bot.send_message
        attempts = []
        async def rate_limited(*, chat_id, **kwargs):
            attempts.append(chat_id)
            if chat_id == 42 and attempts.count(42) == 1:
                raise RuntimeError('Retry after 3')
            self.release_second.set()
            await original_send(chat_id=chat_id, **kwargs)
        self.bot.send_message = rate_limited
        first = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(first.status_code, 200, first.text)
        interim = await self.persisted()
        self.assertEqual(interim['remarketing_campaigns'][0]['sent_count'], 2)
        self.assertEqual(interim['remarketing_campaigns'][0]['status'], 'Parcial')
        second = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(second.json()['sent_count'], 1)
        self.assertEqual(attempts, [41, 42, 43, 42])
        self.assertEqual((await self.persisted())['remarketing_campaigns'][0]['sent_count'], 3)

    async def test_stale_panel_save_cannot_erase_delivery_claims(self):
        state = await main._read_state_from_db()
        stale_panel = copy.deepcopy(state)
        state['remarketing_campaigns'][0]['deliveries'] = {'111:41': 'sent'}
        state['remarketing_campaigns'][0]['sent_count'] = 1
        await main._write_state_to_db(state, preserve_runtime=False)
        stale_panel['remarketing_campaigns'][0]['message'] = 'Nova oferta'
        await main._write_state_to_db(stale_panel, preserve_runtime=True)
        saved = await self.persisted()
        self.assertEqual(saved['remarketing_campaigns'][0]['deliveries'], {'111:41': 'sent'})
        self.assertEqual(saved['remarketing_campaigns'][0]['sent_count'], 1)
        self.assertEqual(saved['remarketing_campaigns'][0]['message'], 'Nova oferta')

    async def test_proxy_timeout_does_not_cancel_live_request_and_retry_is_serialized(self):
        request = asyncio.create_task(self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'}))
        await asyncio.wait_for(self.second_started.wait(), 5)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(request), 0.01)
        retry = asyncio.create_task(self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'}))
        self.release_second.set()
        first, second = await asyncio.gather(request, retry)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(self.sent, [41, 42, 43])
        self.assertEqual(second.json()['sent_count'], 0)
        final = await self.persisted()
        self.assertEqual(final['remarketing_campaigns'][0]['sent_count'], 3)
        self.assertEqual(final['remarketing_campaigns'][0]['status'], 'Finalizada')

    async def test_cancel_after_first_delivery_persists_progress_and_retry_skips_it(self):
        request = asyncio.create_task(self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'}))
        await asyncio.wait_for(self.second_started.wait(), 5)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        state = await self.persisted()
        camp = state['remarketing_campaigns'][0]
        self.assertEqual(camp['sent_count'], 1)
        self.assertEqual(camp['status'], 'Interrompida')
        self.assertEqual([l['telegram_id'] for l in state['memory']['leads'] if l.get('from_remarketing')], ['41'])
        self.release_second.set()
        response = await self.client.post('/api/flow-bots/remarketing/send', json={'campaign_id': 'camp'})
        self.assertEqual(response.status_code, 200, response.text)
        # The canceled in-flight Telegram call is ambiguous: don't blindly resend 42.
        self.assertEqual(self.sent, [41, 43])
        final = await self.persisted()
        self.assertEqual(final['remarketing_campaigns'][0]['sent_count'], 2)
        self.assertEqual(final['remarketing_campaigns'][0]['deliveries']['111:42'], 'sending')
        self.assertEqual(final['remarketing_campaigns'][0]['status'], 'Revisão necessária')
        self.assertEqual([len(l['history']) for l in final['memory']['leads']], [1, 0, 1])
