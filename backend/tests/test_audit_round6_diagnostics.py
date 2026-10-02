import asyncio
import copy
import json
import os
import sys
import unittest
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

import main as app_main
import telegram_service as tg_srv
from telegram_service import bot_manager


class TestAuditRound6Diagnostics(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._orig_read_fn = app_main._read_state_from_db
        self._orig_write_fn = app_main._write_state_to_db
        self._orig_state_getter = bot_manager.state_getter
        self._orig_state_saver = bot_manager.state_saver

        self.server_state = json.loads(json.dumps(app_main.DEFAULT_SHARKBOT_STATE))
        self.server_state["flow_bots"] = [
            {
                "id": "bot_flow_1",
                "bot_id": "111",
                "token": "111:TOKEN_BOT_1",
                "username": "@bot1",
                "active": True,
                "mode": "flow",
                "sales_today": 3,
                "revenue_today": 89.70,
                "plans": [{"id": "p1", "name": "VIP & Bônus", "price": 29.90}]
            },
            {
                "id": "bot_flow_2",
                "bot_id": "222",
                "token": "222:TOKEN_BOT_2",
                "username": "@bot2",
                "active": True,
                "mode": "flow",
                "sales_today": 1,
                "revenue_today": 19.90,
                "plans": [{"id": "p2", "name": "VIP Normal", "price": 19.90}]
            }
        ]
        self.server_state["memory"]["leads"] = [
            {
                "telegram_id": "555",
                "first_name": "Lead MultiBot",
                "stage": "Conversando",
                "blocked": False,
                "bot_id": "bot_flow_1",
                "origin_bot_numeric_id": "111"
            },
            {
                "telegram_id": "555",
                "first_name": "Lead MultiBot",
                "stage": "Conversando",
                "blocked": False,
                "bot_id": "bot_flow_2",
                "origin_bot_numeric_id": "222"
            }
        ]
        self.server_state["tx_records"] = {
            "test_old_sale": {
                "tx_id": "test_old_sale",
                "user_id": 555,
                "mode": "flow",
                "bot_id": "bot_flow_1",
                "amount": 29.90,
                "status": "paid",
                "is_test": True,
                "sales_counted": True,
                "created_at": (datetime.now(timezone(timedelta(hours=-3))) - timedelta(days=5)).isoformat(),
                "paid_at": (datetime.now(timezone(timedelta(hours=-3))) - timedelta(days=5)).isoformat()
            }
        }

        async def _mock_read():
            return copy.deepcopy(self.server_state)

        async def _mock_write(st, preserve_runtime=False, delete_tx_ids=None):
            del_set = set(str(x) for x in (delete_tx_ids or []))
            ex_tx_map = self.server_state.get("tx_records") if isinstance(self.server_state.get("tx_records"), dict) else {}
            st_tx_map = st.setdefault("tx_records", {})

            for ex_id, ex_rec in ex_tx_map.items():
                if not isinstance(ex_rec, dict) or str(ex_id) in del_set:
                    continue
                if ex_rec.get("status") == "paid" or ex_rec.get("delivered") or ex_rec.get("sales_counted") is True:
                    st_rec = st_tx_map.get(ex_id)
                    if not st_rec or not isinstance(st_rec, dict):
                        st_tx_map[ex_id] = copy.deepcopy(ex_rec)

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

    async def test_clear_flow_test_data_actually_purges_paid_test_tx_and_preserves_today_counters(self):
        """Verifica se clear_flow_test_data elimina transações de teste pagas e não subtrai de vendas de dias anteriores."""
        req = app_main.ClearTestDataRequest(bot_id="all", clear_all_sales=False)
        res = await app_main.clear_flow_test_data(req)

        self.assertEqual(res["status"], "ok")
        # Deve ter removido test_old_sale
        self.assertNotIn("test_old_sale", self.server_state["tx_records"], "Transação de teste paga foi restaurada pelo reconciliador!")
        # Venda de 5 dias atrás não pode decrementar sales_today de hoje
        fb1 = next(b for b in self.server_state["flow_bots"] if b["id"] == "bot_flow_1")
        self.assertEqual(fb1["sales_today"], 3, "Transação de teste antiga debitou das vendas de hoje!")
        self.assertEqual(fb1["revenue_today"], 89.70, "Transação de teste antiga debitou do faturamento de hoje!")

    async def test_bot_peer_independence_in_lead_block_and_task_cancellation(self):
        """Verifica se bloqueio no Bot 2 não bloqueia o lead no Bot 1 nem cancela suas tarefas."""
        manager = tg_srv.ActiveBotManager()
        manager.state_getter = bot_manager.state_getter
        manager.state_saver = bot_manager.state_saver

        # Cria tarefas simuladas para ambos os bots
        dummy_task_b1 = asyncio.create_task(asyncio.sleep(10))
        dummy_task_b2 = asyncio.create_task(asyncio.sleep(10))
        manager.downsell_tasks["111:555"] = dummy_task_b1
        manager.downsell_tasks["222:555"] = dummy_task_b2

        # Cancela apenas o downsell do bot 222
        manager._cancel_downsell(555, bot_id="222")
        await asyncio.sleep(0)
        self.assertTrue(dummy_task_b2.cancelled(), "Tarefa do Bot 2 deveria ter sido cancelada.")
        self.assertFalse(dummy_task_b1.cancelled(), "Tarefa do Bot 1 foi indevidamente cancelada ao cancelar Bot 2!")

        dummy_task_b1.cancel()

        # Bloqueia no bot 2
        await manager._mark_lead_blocked(555, bot_token="222:TOKEN_BOT_2")
        lead_b1 = self.server_state["memory"]["leads"][0]
        lead_b2 = self.server_state["memory"]["leads"][1]

        self.assertFalse(lead_b1["blocked"], "Lead do Bot 1 foi marcado como bloqueado indevidamente!")
        self.assertTrue(lead_b2["blocked"], "Lead do Bot 2 deveria estar bloqueado.")
        self.assertEqual(lead_b2["stage"], "Bloqueou o bot")

    async def test_resend_access_handles_special_characters_in_plan_name(self):
        """Verifica se o reenvio de acesso escapa caracteres HTML especiais no nome do plano."""
        self.server_state["tx_records"]["tx_paid_esc"] = {
            "tx_id": "tx_paid_esc",
            "user_id": 555,
            "mode": "flow",
            "bot_id": "bot_flow_1",
            "bot_token": "111:TOKEN_BOT_1",
            "amount": 29.90,
            "offer_name": "VIP & Bônus <Ultra>",
            "status": "paid",
            "delivered": False,
            "delivery_dest_type": "Link externo",
            "delivery_external_url": "https://t.me/joinchat/test"
        }

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.session = MagicMock()
        mock_bot.session.close = AsyncMock()

        with patch("telegram_service._create_bot_instance", return_value=mock_bot):
            req = app_main.AccessResendRequest(tx_id="tx_paid_esc")
            res = await app_main.resend_vip_access(req)

            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["sent_count"], 1)
            mock_bot.send_message.assert_awaited_once()
            args, kwargs = mock_bot.send_message.call_args
            # O texto enviado deve ter caracteres escapados
            self.assertIn("VIP &amp; Bônus &lt;Ultra&gt;", kwargs["text"])
