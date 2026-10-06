import asyncio
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from test_stage1_security import _install_test_stubs

_install_test_stubs()
os.environ.setdefault("ADMIN_TOKEN", "mock-admin-token-for-tests")

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main as app_main  # noqa: E402
import telegram_service as tg_srv  # noqa: E402
from telegram_service import bot_manager  # noqa: E402


class AuditRound5Tests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._orig_read_fn = app_main._read_state_from_db
        self._orig_write_fn = app_main._write_state_to_db
        self._orig_state_getter = bot_manager.state_getter
        self._orig_state_saver = bot_manager.state_saver

        self.server_state = json.loads(json.dumps(app_main.DEFAULT_SHARKBOT_STATE))
        self.server_state["publish"] = {
            "is_live": True,
            "bot_token": "123456:MOCK_PRIMARY_TOKEN",
            "bot_username": "@mock_primary_bot",
            "gateway_provider": "syncpay",
            "pushin_pay_token": "mock-token",
            "gateway_secret": "mock-secret",
        }
        self.server_state["memory"]["leads"] = [
            {"telegram_id": "111", "first_name": "Lead 1", "stage": "Iniciou"},
            {"telegram_id": "222", "first_name": "Lead 2", "stage": "Gerou PIX"}
        ]

        async def _mock_read():
            return copy.deepcopy(self.server_state)

        async def _mock_write(st, delete_tx_ids=None, delete_client_keys=None, **kwargs):
            self.server_state = copy.deepcopy(st)
            return self.server_state

        app_main._read_state_from_db = _mock_read
        app_main._write_state_to_db = _mock_write
        bot_manager.state_getter = _mock_read
        bot_manager.state_saver = _mock_write

    def tearDown(self):
        app_main._read_state_from_db = self._orig_read_fn
        app_main._write_state_to_db = self._orig_write_fn
        bot_manager.state_getter = self._orig_state_getter
        bot_manager.state_saver = self._orig_state_saver

    async def test_checkpix_button_is_pure_static_without_db_or_gateway_queries(self):
        manager = tg_srv.ActiveBotManager()
        manager.bot = tg_srv.Bot(token="123456:MOCK")
        manager.dp = tg_srv.Dispatcher()
        manager.state_getter = AsyncMock()
        manager.state_saver = AsyncMock()
        manager._register_handlers()

        # Handler 1 is checkpix:
        check_handler = manager.dp.callback_handlers[1]
        callback = SimpleNamespace(
            data="checkpix:tx_live_999",
            from_user=SimpleNamespace(id=111, first_name="User"),
            answer=AsyncMock()
        )

        with patch.object(tg_srv.PushinPayClient, "check_transaction_status", AsyncMock()) as gw_mock, \
             patch.object(manager, "deliver_purchase_and_upsell", AsyncMock()) as deliv_mock:
            await check_handler(callback)
            callback.answer.assert_awaited_once()
            args, kwargs = callback.answer.call_args
            self.assertIn("processamento", args[0])
            self.assertTrue(kwargs.get("show_alert"))
            manager.state_getter.assert_not_called()
            gw_mock.assert_not_called()
            deliv_mock.assert_not_called()

    async def test_start_pix_poll_task_does_not_spawn_background_polling(self):
        manager = tg_srv.ActiveBotManager()
        manager.pix_poll_tasks = {}
        manager.active_tx_map = {}

        manager._start_pix_poll_task(
            user_id=111,
            tx_id="tx_test_abc",
            pushin_token="test_token",
            gw_provider="syncpay"
        )

        self.assertEqual(manager.active_tx_map.get("tx_test_abc"), 111)
        self.assertNotIn("tx_test_abc", manager.pix_poll_tasks)

    async def test_resume_pending_pix_polls_is_noop(self):
        manager = tg_srv.ActiveBotManager()
        manager.pix_poll_tasks = {}
        state = {
            "publish": {"pushin_pay_token": "token123"},
            "tx_records": {
                "tx_pending_1": {
                    "user_id": 111,
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
            }
        }
        manager._resume_pending_pix_polls(state)
        self.assertEqual(len(manager.pix_poll_tasks), 0)

    async def test_delete_lead_updates_evaluated_results_metric(self):
        self.assertEqual(len(self.server_state["memory"]["leads"]), 2)
        res = await app_main.delete_lead("111")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(self.server_state["memory"]["leads"]), 1)
        self.assertEqual(self.server_state["learning"]["evaluated_results"], 1)

    async def test_resolve_default_webhook_preserves_https_scheme(self):
        with patch.dict(os.environ, {"VPS_HOST": "https://secure-vps.com", "WEBHOOK_BASE_URL": ""}, clear=False):
            url = app_main.PushinPayClient._resolve_default_webhook("/api/webhook/syncpay")
            self.assertEqual(url, "https://secure-vps.com/api/webhook/syncpay")

    async def test_webhook_uses_transaction_credentials_when_dashboard_changes(self):
        tx_id = "tx_sync_isolate_1"
        self.server_state["tx_records"] = {
            tx_id: {
                "user_id": 111,
                "amount": 29.9,
                "cents": 2990,
                "status": "pending",
                "gateway_provider": "syncpay",
                "gateway_token": "client_id_sync_persisted",
                "gateway_secret": "client_secret_sync_persisted"
            }
        }
        # Painel global agora está configurado como PushinPay
        self.server_state["publish"] = {
            "gateway_provider": "pushinpay",
            "pushin_pay_token": "token_pushin_dashboard",
            "gateway_secret": ""
        }
        captured_kwargs = {}
        def mock_client_factory(*args, **kwargs):
            captured_kwargs.update(kwargs)
            mock_client = AsyncMock()
            mock_client.check_transaction_status = AsyncMock(return_value={"status": "paid", "raw": {"amount": 29.9}})
            return mock_client

        req = SimpleNamespace(headers={}, url=SimpleNamespace(path="/api/webhook/syncpay"))
        with patch.object(app_main, "PushinPayClient", side_effect=mock_client_factory), \
             patch.object(app_main.bot_manager, "notify_payment_by_tx_id", AsyncMock(return_value=True)):
            res = await app_main.webhook_gateway({"id": tx_id, "status": "paid"}, request=req)
            self.assertEqual(res["status"], "success")
            self.assertEqual(captured_kwargs["provider"], "syncpay")
            self.assertEqual(captured_kwargs["token"], "client_id_sync_persisted")
            self.assertEqual(captured_kwargs["secret"], "client_secret_sync_persisted")

    async def test_webhook_route_mismatch_rejected(self):
        tx_id = "tx_sync_route_mismatch"
        self.server_state["tx_records"] = {
            tx_id: {
                "user_id": 111,
                "amount": 10.0,
                "cents": 1000,
                "status": "pending",
                "gateway_provider": "syncpay",
                "gateway_token": "tok_sync",
                "gateway_secret": "sec_sync"
            }
        }
        # Notificação recebida na rota errada (/api/webhook/pushinpay)
        req = SimpleNamespace(headers={}, url=SimpleNamespace(path="/api/webhook/pushinpay"))
        res = await app_main.webhook_gateway({"id": tx_id, "status": "paid"}, request=req)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["action"], "provider_mismatch")


if __name__ == "__main__":
    unittest.main()
