import asyncio
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from test_stage1_security import _install_test_stubs

_install_test_stubs()
os.environ.setdefault("ADMIN_TOKEN", "mock-admin-token-for-tests")

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main as app_main  # noqa: E402
import telegram_service as tg_srv  # noqa: E402
from telegram_service import bot_manager  # noqa: E402
from aiogram import types as aiogram_types  # noqa: E402


class AuditRound3Tests(unittest.IsolatedAsyncioTestCase):

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
            "delivery_type": "Link de acesso",
            "delivery_destination": "-100999999",
            "support_username": "@suporte_test",
            "gateway_provider": "syncpay",
            "pushin_pay_token": "mock-token",
            "gateway_secret": "mock-secret",
        }
        self.server_state["flow_bots"] = [
            {
                "id": "fb_alpha",
                "token": "777:MOCK_ALPHA_TOKEN",
                "username": "@alpha_bot",
                "name": "Bot Alpha",
                "active": True,
                "sales_today": 5,
                "revenue_today": 250.00,
                "fallback_delivery": {
                    "dest_type": "Canal ou grupo",
                    "channel_id": "-100777000",
                    "channel_name": "Canal Alpha VIP",
                },
                "plans": [
                    {
                        "id": "plan_alpha_vip",
                        "name": "Plano Alpha VIP",
                        "price": 50.00,
                        "delivery_dest_type": "Herdar do fluxo",
                    }
                ],
                "payments": {
                    "approved_message": "Aprovado Alpha",
                    "approved_medias": [],
                },
            },
            {
                "id": "fb_inactive",
                "token": "888:MOCK_INACTIVE_TOKEN",
                "username": "@inactive_bot",
                "name": "Bot Inativo",
                "active": False,
                "sales_today": 0,
                "revenue_today": 0.0,
                "fallback_delivery": {
                    "dest_type": "Canal ou grupo",
                    "channel_id": "-100888000",
                },
                "plans": [],
            },
        ]
        self.server_state["tx_records"] = {}
        self.server_state["memory"] = {"leads": [], "deleted_leads": []}
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        async def _mock_read():
            return app_main._STATE_CACHE

        async def _mock_write(st, preserve_runtime=True, delete_tx_ids=None):
            app_main._STATE_CACHE = copy.deepcopy(st)
            return app_main._STATE_CACHE

        app_main._read_state_from_db = _mock_read
        app_main._write_state_to_db = _mock_write
        bot_manager.state_getter = _mock_read
        bot_manager.state_saver = _mock_write
        bot_manager.tx_records = {}

    def tearDown(self):
        app_main._read_state_from_db = self._orig_read_fn
        app_main._write_state_to_db = self._orig_write_fn
        bot_manager.state_getter = self._orig_state_getter
        bot_manager.state_saver = self._orig_state_saver

    async def test_1_delete_flow_transaction_no_name_error_and_copy_on_write_db_failure(self):
        """1. Exclusão atômica: sem NameError e com transação única (Order + BotConfig JSON) no banco."""
        tx_id = "tx_fail_commit_1"
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 1001,
            "bot_id": "fb_alpha",
            "bot_token": "777:MOCK_ALPHA_TOKEN",
            "amount": 50.00,
            "status": "paid",
            "delivered": True,
            "sales_counted": True,
            "paid_at": datetime.now(timezone.utc).isoformat(),
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        # Simular erro no banco relacional durante persistência única
        with patch.object(app_main, "_write_state_to_db") as mock_write:
            mock_write.side_effect = RuntimeError("Falha de I/O atômica no banco")

            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                await app_main.delete_flow_transaction(tx_id)
            self.assertEqual(ctx.exception.status_code, 500)

            # Cópia em memória NÃO pode ter sido alterada!
            self.assertIn(tx_id, app_main._STATE_CACHE["tx_records"], "Cache de memória foi corrompido antes do commit!")
            fb = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
            self.assertEqual(fb["sales_today"], 5, "Contador de vendas foi alterado antes do commit!")

        # Agora com commit funcional, validar exclusão e envio de delete_tx_ids para commit único
        res = await app_main.delete_flow_transaction(tx_id)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn(tx_id, app_main._STATE_CACHE["tx_records"])
        fb_after = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
        self.assertEqual(fb_after["sales_today"], 4)
        self.assertEqual(fb_after["revenue_today"], 200.00)

    async def test_2_delete_old_sale_does_not_deduct_today_counters(self):
        """2. Venda de dias anteriores não pode reduzir sales_today nem revenue_today."""
        old_paid_at = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        tx_old = "tx_old_sale"
        self.server_state["tx_records"][tx_old] = {
            "tx_id": tx_old,
            "user_id": 2002,
            "bot_id": "fb_alpha",
            "amount": 50.00,
            "status": "paid",
            "delivered": True,
            "paid_at": old_paid_at,
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        res = await app_main.delete_flow_transaction(tx_old)
        self.assertEqual(res["status"], "ok")
        fb = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
        self.assertEqual(fb["sales_today"], 5, "Venda de 4 dias atrás reduziu sales_today!")
        self.assertEqual(fb["revenue_today"], 250.00, "Venda de 4 dias atrás reduziu revenue_today!")

    async def test_3_concurrent_delivery_retry_idempotency_no_duplicate_sales(self):
        """3. Falha de entrega cria pendência sem duplicar vendas no retry concorrente."""
        fb = next(b for b in self.server_state["flow_bots"] if b["id"] == "fb_alpha")
        fb["sales_today"] = 0
        fb["revenue_today"] = 0.0

        tx_id = "tx_retry_test"
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 3003,
            "mode": "flow",
            "bot_id": "fb_alpha",
            "bot_token": "777:MOCK_ALPHA_TOKEN",
            "plan_id": "plan_alpha_vip",
            "amount": 50.00,
            "status": "pending",
            "delivered": False,
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        mock_bot = AsyncMock()
        mock_bot.token = "777:MOCK_ALPHA_TOKEN"
        # Primeira passagem: Telegram falha ao criar convite (ex: bot não é admin do canal)
        mock_bot.create_chat_invite_link.side_effect = RuntimeError("Bot is not an admin in channel")
        bot_manager.extra_bots["777:MOCK_ALPHA_TOKEN"] = mock_bot

        ok_first = await bot_manager.deliver_purchase_and_upsell(3003, tx_id=tx_id)
        self.assertFalse(ok_first, "Deveria ter retornado False devido à falha de link.")

        # Verificar Fase 1: Venda contabilizada UMA VEZ, mas entrega pendente
        rec = app_main._STATE_CACHE["tx_records"][tx_id]
        self.assertEqual(rec["status"], "paid")
        self.assertTrue(rec.get("sales_counted"))
        self.assertFalse(rec.get("delivered"))
        self.assertEqual(rec.get("access_status"), "Pendente (Admin no Canal)")
        fb_state = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
        self.assertEqual(fb_state["sales_today"], 1)
        self.assertEqual(fb_state["revenue_today"], 50.00)

        # Segunda passagem após o intervalo mínimo: Telegram recuperado, duas notificações simultâneas
        app_main._STATE_CACHE["tx_records"][tx_id]["last_delivery_attempt_at"] = (
            datetime.utcnow() - timedelta(minutes=2)
        ).isoformat()
        mock_invite = SimpleNamespace(invite_link="https://t.me/+valid_vip_link")
        mock_bot.create_chat_invite_link.side_effect = None
        mock_bot.create_chat_invite_link.return_value = mock_invite

        res_a, res_b = await asyncio.gather(
            bot_manager.deliver_purchase_and_upsell(3003, tx_id=tx_id),
            bot_manager.deliver_purchase_and_upsell(3003, tx_id=tx_id),
        )
        self.assertTrue(res_a)
        self.assertTrue(res_b)

        # Garantir que a entrega foi marcada como True e vendas NÃO foram duplicadas
        rec_final = app_main._STATE_CACHE["tx_records"][tx_id]
        self.assertTrue(rec_final.get("delivered"))
        self.assertEqual(rec_final.get("access_status"), "Ativo")
        self.assertEqual(fb_state["sales_today"], 1, "Venda foi duplicada no retry!")
        self.assertEqual(fb_state["revenue_today"], 50.00, "Receita foi duplicada no retry!")

    async def test_4_multi_bot_lead_isolation_and_legacy_unassigned_preservation(self):
        """4. Isolamento estrito de leads por bot: origem incerta não é usurpada."""
        # Lead legado sem bot_id
        legacy_lead = {
            "telegram_id": "4004",
            "first_name": "Antigo",
            "bot_id": "",
            "stage": "Conversando",
            "history": [{"role": "user", "content": "Olá legado"}],
        }
        self.server_state["memory"]["leads"].append(legacy_lead)
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        user_obj = aiogram_types.User(id=4004, is_bot=False, first_name="Carlos", username="carlos4004")

        # Entrada no Bot Alpha
        lead_alpha = await bot_manager._get_or_create_lead(
            app_main._STATE_CACHE,
            user_obj,
            bot_token="777:MOCK_ALPHA_TOKEN"
        )
        self.assertEqual(lead_alpha["bot_id"], "fb_alpha")
        self.assertNotEqual(id(lead_alpha), id(legacy_lead), "Lead do Bot Alpha usurpou o registro legado!")

        # Lead legado deve continuar sem bot_id e com seu histórico intacto
        self.assertEqual(legacy_lead["bot_id"], "")
        self.assertEqual(len(legacy_lead["history"]), 1)

    async def test_5_polling_cancellation_does_not_expire_valid_pix(self):
        """5. Cancelamento do polling não pode marcar cobrança como expired prematuramente."""
        tx_id = "tx_poll_test"
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 5005,
            "status": "pending",
            "delivered": False,
        }
        bot_manager.tx_records[tx_id] = self.server_state["tx_records"][tx_id]
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        # Iniciar polling e cancelar imediatamente
        poll_task = asyncio.create_task(
            bot_manager._poll_pix_payment(5005, tx_id, pushin_token="mock-token")
        )
        bot_manager.pix_poll_tasks[tx_id] = poll_task
        await asyncio.sleep(0.01)
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

        rec = bot_manager.tx_records[tx_id]
        self.assertEqual(rec["status"], "pending", "Status virou 'expired' ao cancelar polling!")

    async def test_6_deactivated_flow_bot_does_not_fallback_to_ia_channel(self):
        """6. Bot de fluxo inativo nunca pode entregar canal genérico de IA."""
        tx_id = "tx_inactive_flow"
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 6006,
            "mode": "flow",
            "bot_id": "fb_inactive",
            "bot_token": "888:MOCK_INACTIVE_TOKEN",
            "amount": 29.90,
            "status": "pending",
            "delivered": False,
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        res = await bot_manager.deliver_purchase_and_upsell(6006, tx_id=tx_id)
        self.assertFalse(res, "Entrega não deve prosseguir com bot de fluxo inativo!")
        rec = app_main._STATE_CACHE["tx_records"][tx_id]
        self.assertEqual(rec["access_status"], "Pendente (Bot de Origem Indisponível)")
        self.assertFalse(rec.get("delivered"))
        self.assertEqual(rec.get("status"), "paid", "Cobrança paga deve registrar pagamento aprovado.")

    async def test_7_reconcile_daily_counters_self_heals_drift(self):
        """7. Reconciliação contábil: reconstrói contadores a partir da verdade do ledger em tx_records."""
        today_iso = datetime.now(timezone.utc).isoformat()
        # Estado com contadores corrompidos/desviados (ex: por crashes antigos)
        fb = next(b for b in self.server_state["flow_bots"] if b["id"] == "fb_alpha")
        fb["sales_today"] = 999  # Corrompido
        fb["revenue_today"] = 88888.00  # Corrompido

        # Ledger real com exatamente 2 vendas pagas hoje de R$ 50 cada
        self.server_state["tx_records"] = {
            "tx_real_1": {
                "tx_id": "tx_real_1",
                "bot_id": "fb_alpha",
                "amount": 50.00,
                "status": "paid",
                "delivered": True,
                "paid_at": today_iso,
            },
            "tx_real_2": {
                "tx_id": "tx_real_2",
                "bot_id": "fb_alpha",
                "amount": 50.00,
                "status": "paid",
                "delivered": True,
                "paid_at": today_iso,
            },
            "tx_old": {
                "tx_id": "tx_old",
                "bot_id": "fb_alpha",
                "amount": 100.00,
                "status": "paid",
                "delivered": True,
                "paid_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            },
            "tx_upsell": {
                "tx_id": "tx_upsell",
                "bot_id": "fb_alpha",
                "amount": 25.00,
                "is_upsell": True,
                "status": "paid",
                "delivered": True,
                "paid_at": today_iso,
            },
        }

        app_main.reconcile_daily_counters(self.server_state)
        self.assertEqual(fb["sales_today"], 2, "Reconciliação não corrigiu o drift de vendas!")
        self.assertEqual(fb["revenue_today"], 100.00, "Reconciliação não corrigiu o drift de receita!")

    async def test_8_process_restart_resilience_preserves_idempotency(self):
        """8. Resiliência a reinício: ausência de locks em memória não causa recontagem se sales_counted=True no disco."""
        tx_id = "tx_restarted_process"
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 8008,
            "mode": "flow",
            "bot_id": "fb_alpha",
            "bot_token": "777:MOCK_ALPHA_TOKEN",
            "amount": 50.00,
            "status": "paid",
            "sales_counted": True,  # Já persistido no disco antes do reinício
            "delivered": False,      # Entrega pendente que disparou retry pós-boot
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)

        # Simular reinício completo do processo: novo ActiveBotManager com locks de RAM vazios
        fresh_mgr = tg_srv.ActiveBotManager()
        fresh_mgr.state_getter = bot_manager.state_getter
        fresh_mgr.state_saver = bot_manager.state_saver
        mock_bot = AsyncMock()
        mock_bot.token = "777:MOCK_ALPHA_TOKEN"
        mock_invite = SimpleNamespace(invite_link="https://t.me/+reboot_vip")
        mock_bot.create_chat_invite_link.return_value = mock_invite
        fresh_mgr.extra_bots["777:MOCK_ALPHA_TOKEN"] = mock_bot

        fb_before = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
        sales_before = fb_before["sales_today"]
        rev_before = fb_before["revenue_today"]

        # Executar retry no processo recém-iniciado
        ok = await fresh_mgr.deliver_purchase_and_upsell(8008, tx_id=tx_id)
        self.assertTrue(ok)

        # A entrega foi concluída, mas a contabilidade NÃO foi duplicada
        rec = app_main._STATE_CACHE["tx_records"][tx_id]
        self.assertTrue(rec.get("delivered"))
        self.assertEqual(fb_before["sales_today"], sales_before, "Reinício do processo causou contagem duplicada!")
        self.assertEqual(fb_before["revenue_today"], rev_before, "Reinício do processo causou receita duplicada!")

    async def test_9_uncounted_transaction_deletion_does_not_decrement_sales(self):
        """9. Excluir transação que não havia sido contabilizada não reduz sales_today."""
        tx_id = "tx_uncounted"
        today_iso = datetime.now(timezone.utc).isoformat()
        self.server_state["tx_records"][tx_id] = {
            "tx_id": tx_id,
            "user_id": 9009,
            "bot_id": "fb_alpha",
            "amount": 50.00,
            "status": "pending",
            "sales_counted": False,
            "delivered": False,
            "created_at": today_iso,
        }
        app_main._STATE_CACHE = copy.deepcopy(self.server_state)
        fb = next(b for b in app_main._STATE_CACHE["flow_bots"] if b["id"] == "fb_alpha")
        sales_before = fb["sales_today"]

        await app_main.delete_flow_transaction(tx_id)
        self.assertEqual(fb["sales_today"], sales_before, "Transação não contabilizada reduziu vendas ao ser apagada!")


if __name__ == "__main__":
    unittest.main()
