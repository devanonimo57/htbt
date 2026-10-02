import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import main as app_main
import telegram_service as tg_srv

BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_HTML = os.path.abspath(os.path.join(BACKEND_DIR, "..", "..", "frontend", "public", "index.html"))


class TestAuditRound4Fixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mgr = tg_srv.ActiveBotManager()
        self.mgr.bot = MagicMock()
        self.mgr.bot.token = "123456:MOCK_TOKEN"
        self.server_state = {
            "flow_bots": [
                {
                    "id": "bot_flow_1",
                    "bot_id": "123456",
                    "token": "123456:TOKEN_BOT_1",
                    "username": "@flow1_bot",
                    "active": True,
                    "mode": "flow",
                    "sales_today": 0,
                    "revenue_today": 0.0,
                    "plans": [{"id": "p1", "name": "VIP Mensal", "price": 19.90}]
                },
                {
                    "id": "bot_flow_2",
                    "bot_id": "999888",
                    "token": "999888:TOKEN_BOT_2",
                    "username": "@flow2_bot",
                    "active": True,
                    "mode": "flow",
                    "sales_today": 0,
                    "revenue_today": 0.0,
                    "plans": [{"id": "p2", "name": "VIP Anual", "price": 99.90}]
                }
            ],
            "tx_records": {},
            "memory": {"leads": [], "deleted_leads": []},
            "learning": {}
        }

    async def test_1_ref_counted_lock_lifecycle(self):
        """Verifica se o lock com contagem de referências nunca é purgado enquanto há tarefas na fila."""
        pool = {}
        lock1 = self.mgr._get_tx_lock("tx_race_1")
        lock2 = self.mgr._get_tx_lock("tx_race_1")

        self.assertIs(lock1, lock2, "Tarefas com mesma chave devem compartilhar a mesma instância de lock.")
        self.assertEqual(lock1.refs, 2, "Contador de referências deve ser 2.")
        self.assertIn("tx_race_1", self.mgr.tx_locks)

        execution_order = []

        async def worker(task_name: str, lk):
            async with lk:
                execution_order.append(f"{task_name}_start")
                await asyncio.sleep(0.05)
                execution_order.append(f"{task_name}_end")

        await asyncio.gather(
            worker("task1", lock1),
            worker("task2", lock2)
        )

        self.assertEqual(execution_order, ["task1_start", "task1_end", "task2_start", "task2_end"])
        self.assertNotIn("tx_race_1", self.mgr.tx_locks, "Lock deve ser purgado da memória após refs chegar a 0.")

    async def test_2_strict_flow_bot_attribution_no_blind_fallback(self):
        """Verifica se buscas por bot inexistente retornam None em vez de pegar bot aleatório."""
        # Busca por bot_id inexistente
        matched = self.mgr._get_active_flow_bot(self.server_state, bot_id="bot_fantasma")
        self.assertIsNone(matched, "Não deve atribuir a outro bot quando bot_id não existe.")

        # Busca com múltiplos bots sem token no contexto -> deve retornar None (ambíguo)
        isolated_mgr = tg_srv.ActiveBotManager()
        matched_ambiguous = isolated_mgr._get_active_flow_bot(self.server_state, bot_id="", bot_token="")
        self.assertIsNone(matched_ambiguous, "Não deve escolher arbitrariamente com múltiplos bots ativos.")

        # Busca por bot_id correto
        matched_real = self.mgr._get_active_flow_bot(self.server_state, bot_id="bot_flow_2")
        self.assertIsNotNone(matched_real)
        self.assertEqual(matched_real["username"], "@flow2_bot")

    async def test_3_ai_delivery_message_in_bot_no_unbound_local_error(self):
        """Verifica se entrega de IA com 'Mensagem no bot' não dispara UnboundLocalError em invite_url."""
        self.server_state["tx_records"]["tx_ai_msg"] = {
            "tx_id": "tx_ai_msg",
            "user_id": 7777,
            "mode": "ai",
            "bot_token": "123456:TOKEN_BOT_1",
            "amount": 29.90,
            "status": "pending",
            "delivered": False,
            "delivery_type": "Mensagem no bot",
            "delivery_destination": "Seu acesso VIP direto por mensagem: https://t.me/secreto"
        }

        fake_bot = MagicMock()
        fake_bot.token = "123456:TOKEN_BOT_1"
        fake_bot.send_message = AsyncMock()

        self.mgr.bot = fake_bot
        self.mgr.state_getter = AsyncMock(return_value=self.server_state)
        self.mgr.state_saver = AsyncMock(side_effect=lambda s, **kw: s)

        ok = await self.mgr.deliver_purchase_and_upsell(7777, "tx_ai_msg")
        self.assertTrue(ok)
        self.assertTrue(self.server_state["tx_records"]["tx_ai_msg"]["delivered"])
        self.assertEqual(self.server_state["tx_records"]["tx_ai_msg"]["invite_url"], "")
        self.assertEqual(fake_bot.send_message.call_count, 1)

    async def test_4_delete_transaction_serialized_with_delivery(self):
        """Verifica se exclusão aguarda término da entrega sob tx_lock e não recria registro deletado."""
        self.server_state["tx_records"]["tx_del_race"] = {
            "tx_id": "tx_del_race",
            "user_id": 8888,
            "bot_id": "bot_flow_1",
            "bot_token": "123456:TOKEN_BOT_1",
            "status": "pending",
            "amount": 19.90,
            "delivered": False
        }

        async def mock_write(s, **kw):
            self.server_state.clear()
            self.server_state.update(s)
            return s

        with patch.object(app_main, "bot_manager", self.mgr), \
             patch.object(app_main, "_read_state_from_db", AsyncMock(side_effect=lambda: self.server_state)), \
             patch.object(app_main, "_write_state_to_db", AsyncMock(side_effect=mock_write)):

            # Executa a exclusão de tx_del_race
            res = await app_main.delete_flow_transaction("tx_del_race")
            self.assertEqual(res["status"], "ok")
            self.assertNotIn("tx_del_race", self.server_state["tx_records"])

            # Tentativa de entrega posterior com transação excluída deve abortar sem recriar
            self.mgr.state_getter = AsyncMock(return_value=self.server_state)
            deliv_res = await self.mgr.deliver_purchase_and_upsell(8888, "tx_del_race")
            self.assertFalse(deliv_res)
            self.assertNotIn("tx_del_race", self.server_state["tx_records"])

    async def test_5_get_full_state_reconciles_daily_counters(self):
        """Verifica se GET /api/state invoca reconcile_daily_counters para viradas de dia."""
        with patch.object(app_main, "_read_state_from_db", AsyncMock(return_value=self.server_state)), \
             patch("main.reconcile_daily_counters") as mock_reconcile:
            mock_reconcile.return_value = {"reconciled_date": "2026-10-01"}
            res = await app_main.get_full_state()
            self.assertTrue(mock_reconcile.called, "get_full_state deve chamar reconcile_daily_counters")

    def test_6_frontend_media_xss_escaping(self):
        """Verifica se m.url, m.signals e m.caption estão sanitizados com escapeHtml no HTML."""
        with open(FRONTEND_HTML, "r", encoding="utf-8") as f:
            src = f.read()

        self.assertIn('<img src="${escapeHtml(m.url || \'\')}"', src)
        self.assertIn('value="${escapeHtml(m.signals || \'\')}"', src)
        self.assertIn('value="${escapeHtml(m.caption || \'\')}"', src)
        self.assertIn('>${escapeHtml(m.caption || \'\')}</textarea>', src)


if __name__ == "__main__":
    unittest.main()
