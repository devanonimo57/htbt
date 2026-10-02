import unittest
import asyncio
import copy
from datetime import datetime, timezone
from sqlalchemy import select

from main import (
    _write_state_to_db,
    _read_state_from_db,
    delete_lead,
    permanently_delete_lead,
    delete_flow_transaction,
    async_session,
    Order,
    BotConfig,
    bot_manager,
    DEFAULT_SHARKBOT_STATE
)


class TestItem2Concurrency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Reset memory cache and database state
        import main
        main._STATE_CACHE = None
        base_state = copy.deepcopy(DEFAULT_SHARKBOT_STATE)
        base_state["memory"] = {
            "leads": [
                {
                    "telegram_id": "1001",
                    "first_name": "Lead Teste A",
                    "stage": "Conversando",
                    "flow_plan_price": 19.90
                },
                {
                    "telegram_id": "2002",
                    "first_name": "Cliente Real B",
                    "stage": "Checkout",
                    "flow_plan_price": 49.90
                }
            ],
            "deleted_leads": []
        }
        base_state["tx_records"] = {
            "tx_a": {
                "user_id": 1001,
                "amount": 19.90,
                "status": "pending",
                "delivered": False,
                "sales_counted": False,
                "created_at": datetime.now(timezone.utc).isoformat()
            },
            "tx_b": {
                "user_id": 2002,
                "amount": 49.90,
                "status": "pending",
                "delivered": False,
                "sales_counted": False,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
        }
        await _write_state_to_db(base_state)

    async def test_concurrent_settlement_never_reverts_to_pending(self):
        """
        Cenário Crítico do Item 2:
        Enquanto a exclusão do Lead A prepara um snapshot desatualizado onde tx_b era 'pending',
        o webhook liquida tx_b como 'paid' no banco de dados.
        A gravação da exclusão NUNCA pode rebaixar tx_b de volta para 'pending'.
        """
        # 1. Simula a captura de snapshot antes da liquidação
        stale_snapshot = copy.deepcopy(await _read_state_from_db())
        self.assertEqual(stale_snapshot["tx_records"]["tx_b"]["status"], "pending")

        # 2. Concorrentemente, a transação tx_b é liquidada como 'paid'
        live_state = copy.deepcopy(await _read_state_from_db())
        live_state["tx_records"]["tx_b"]["status"] = "paid"
        live_state["tx_records"]["tx_b"]["sales_counted"] = True
        live_state["tx_records"]["tx_b"]["delivered"] = True
        live_state["tx_records"]["tx_b"]["paid_at"] = datetime.now(timezone.utc).isoformat()
        live_state.setdefault("learning", {})["confirmed_purchases"] = 1
        await _write_state_to_db(live_state)

        # Verifica que no banco tx_b está de fato 'paid'
        current_db = await _read_state_from_db()
        self.assertEqual(current_db["tx_records"]["tx_b"]["status"], "paid")

        # 3. Agora a rotina de exclusão grava o seu snapshot antigo
        # No snapshot antigo, tx_b constava como 'pending', e Lead A foi removido
        stale_snapshot["memory"]["leads"] = [
            l for l in stale_snapshot["memory"]["leads"] if l["telegram_id"] != "1001"
        ]
        stale_snapshot["tx_records"]["tx_a"]["status"] = "cancelled"
        
        # Grava o snapshot antigo
        saved = await _write_state_to_db(stale_snapshot)

        # 4. Verificação de integridade estrita (Regra Monotônica):
        # tx_b DEVE permanecer 'paid', 'sales_counted: True' e 'delivered: True'
        self.assertEqual(saved["tx_records"]["tx_b"]["status"], "paid")
        self.assertTrue(saved["tx_records"]["tx_b"]["sales_counted"])
        self.assertTrue(saved["tx_records"]["tx_b"]["delivered"])
        self.assertEqual(saved["learning"]["confirmed_purchases"], 1)

        # Verifica persistência no cache global do banco
        fresh = await _read_state_from_db()
        self.assertEqual(fresh["tx_records"]["tx_b"]["status"], "paid")
        self.assertTrue(fresh["tx_records"]["tx_b"]["sales_counted"])

    async def test_delete_lead_flow_maintains_concurrency_locks(self):
        """
        Verifica o endpoint delete_lead executando com cópia desacoplada e commit seguro.
        """
        # Exclui Lead A (1001)
        resp = await delete_lead("1001")
        self.assertEqual(resp["status"], "success")

        # Lead A foi para deleted_leads e não está mais em leads ativos
        db_state = await _read_state_from_db()
        active_uids = [l["telegram_id"] for l in db_state["memory"]["leads"]]
        archived_uids = [l["telegram_id"] for l in db_state["memory"]["deleted_leads"]]
        self.assertNotIn("1001", active_uids)
        self.assertIn("1001", archived_uids)
        self.assertIn("2002", active_uids)

        # Transação de A foi cancelada, mas transação de B permaneceu pending
        self.assertEqual(db_state["tx_records"]["tx_a"]["status"], "cancelled")
        self.assertEqual(db_state["tx_records"]["tx_b"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
