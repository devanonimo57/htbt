"""HTTP resend scope against temporary SQLite, with Telegram fully faked."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main


class ResendNumericScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth_patch = patch.dict(os.environ, {'ADMIN_TOKEN': 'test-resend-admin-token'})
        self.auth_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.original = (main.engine, main.async_session, main._STATE_CACHE,
                         main._STATE_COMMITTED, main._STATE_COMMITTED_OWNER)
        main.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.tmp.name, 'resend.db').as_posix()}")
        main.async_session = sessionmaker(main.engine, class_=AsyncSession, expire_on_commit=False)
        main._STATE_CACHE = None
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await main._create_schema()
        self.state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        self.state['flow_bots'] = [
            {'id': 'flow-a', 'bot_id': '111', 'token': '111:test-a', 'payments': {}},
            {'id': 'flow-b', 'bot_id': '222', 'token': '222:test-b', 'payments': {}},
        ]
        self.state['tx_records'] = {
            'a': {'bot_id': 'flow-a', 'bot_token': '111:test-a', 'origin_bot_numeric_id': '111',
                  'user_id': 7, 'status': 'paid', 'delivery_dest_type': 'Link externo',
                  'delivery_external_url': 'https://example.invalid/a'},
            'b': {'bot_id': 'flow-b', 'bot_token': '222:test-b', 'origin_bot_numeric_id': '222',
                  'user_id': 8, 'status': 'paid', 'delivery_dest_type': 'Link externo',
                  'delivery_external_url': 'https://example.invalid/b'},
        }
        self.bots = []
        def fake_bot(token):
            bot = SimpleNamespace(token=token, send_message=AsyncMock(),
                                  session=SimpleNamespace(close=AsyncMock()))
            self.bots.append(bot)
            return bot
        self.bot_patch = patch('telegram_service._create_bot_instance', side_effect=fake_bot)
        self.bot_patch.start()

    async def asyncTearDown(self):
        self.bot_patch.stop()
        self.auth_patch.stop()
        await main.engine.dispose()
        (main.engine, main.async_session, main._STATE_CACHE,
         main._STATE_COMMITTED, main._STATE_COMMITTED_OWNER) = self.original
        self.tmp.cleanup()

    async def request(self, payload):
        await main._write_state_to_db(self.state)
        main._STATE_CACHE = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url='http://test') as client:
            response = await client.post('/api/flow-bots/access/resend', json=payload,
                                         headers={'Authorization': f'Bearer {main._get_expected_admin_token()}'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_numeric_bot_id_scopes_direct_api_to_original_telegram_bot(self):
        result = await self.request({'bot_id': '111'})
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual([(bot.token, bot.send_message.await_args.kwargs['chat_id'])
                          for bot in self.bots], [('111:test-a', 7)])

    async def test_explicit_tx_ids_remain_authoritative_even_with_numeric_filter(self):
        result = await self.request({'bot_id': '111', 'tx_ids': ['b']})
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual([(bot.token, bot.send_message.await_args.kwargs['chat_id'])
                          for bot in self.bots], [('222:test-b', 8)])

    async def test_numeric_scope_refuses_legacy_purchase_without_origin_token(self):
        self.state['tx_records']['a']['bot_token'] = ''
        result = await self.request({'bot_id': '111'})
        self.assertEqual(result['sent_count'], 0)
        self.assertEqual(self.bots, [])

    async def test_numeric_scope_refuses_ambiguous_bot_config(self):
        self.state['flow_bots'].append({'id': 'flow-a-copy', 'bot_id': '111',
                                        'token': '111:test-a', 'payments': {}})
        result = await self.request({'bot_id': '111'})
        self.assertEqual(result['sent_count'], 0)
        self.assertEqual(self.bots, [])

    async def test_numeric_scope_uses_purchase_origin_after_config_id_reuse(self):
        self.state['flow_bots'][0]['id'] = 'flow-a-new'
        self.state['flow_bots'][1]['id'] = 'flow-a'
        result = await self.request({'bot_id': '111'})
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual([(bot.token, bot.send_message.await_args.kwargs['chat_id'])
                          for bot in self.bots], [('111:test-a', 7)])

    async def test_numeric_scope_refuses_conflicting_origin(self):
        self.state['tx_records']['a']['origin_bot_numeric_id'] = '222'
        result = await self.request({'bot_id': '111'})
        self.assertEqual(result['sent_count'], 0)
        self.assertEqual(self.bots, [])
