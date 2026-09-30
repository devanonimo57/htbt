"""Archive/restore API regressions; run with unittest discovery."""
import copy
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

from test_stage1_security import app_main, tg_srv


class ArchivedLeadsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lead = {"telegram_id": "42", "first_name": "Ada", "history": [{"message": "hello"}],
                     "stage": "Comprou", "chosen_plan": {"id": "vip"}}
        self.state = copy.deepcopy(app_main.DEFAULT_SHARKBOT_STATE)
        self.state["memory"]["leads"] = [copy.deepcopy(self.lead), {"telegram_id": "43"}]
        self.state["tx_records"] = {"tx1": {"telegram_id": "42", "amount": 49}}
        self.state["learning"]["confirmed_purchases"] = 8
        self.state["flow_bots"] = [{"id": "bot1", "sales_today": 8, "revenue_today": 392}]
        self.read = patch.object(app_main, "_read_state_from_db", AsyncMock(side_effect=lambda: self.state))
        self.write = patch.object(app_main, "_write_state_to_db", AsyncMock(side_effect=self._save))
        self.cancel = patch.object(app_main.bot_manager, "cancel_lead_tasks")
        self.read.start()
        self.write_mock = self.write.start()
        self.cancel_mock = self.cancel.start()

    def tearDown(self):
        self.cancel.stop()
        self.write.stop()
        self.read.stop()

    async def _save(self, state, **kwargs):
        self.state = copy.deepcopy(state)
        return self.state

    async def test_delete_archives_full_lead_without_changing_sales_or_transactions(self):
        result = await app_main.delete_lead("42")
        self.assertEqual([l["telegram_id"] for l in self.state["memory"]["leads"]], ["43"])
        archived = self.state["memory"]["deleted_leads"][0]
        self.assertEqual({k: v for k, v in archived.items() if k != "deleted_at"}, self.lead)
        self.assertTrue(archived["deleted_at"])
        self.assertEqual(datetime.fromisoformat(archived["deleted_at"]).utcoffset().total_seconds(), 0)
        self.assertEqual(self.state["tx_records"], {"tx1": {"telegram_id": "42", "amount": 49}})
        self.assertEqual(self.state["learning"]["confirmed_purchases"], 8)
        self.assertEqual(self.state["flow_bots"][0]["sales_today"], 8)
        self.assertEqual(self.state["flow_bots"][0]["revenue_today"], 392)
        self.assertEqual(result["state"]["memory"]["deleted_leads"][0]["telegram_id"], "42")
        self.cancel_mock.assert_called_once_with("42")

    async def test_restore_recovers_full_lead_without_archive_metadata(self):
        await app_main.delete_lead("42")
        result = await app_main.restore_lead("42")
        self.assertEqual(self.state["memory"]["leads"][0], self.lead)
        self.assertEqual(self.state["memory"]["deleted_leads"], [])
        self.assertEqual(result["state"]["memory"]["leads"][0], self.lead)
        self.assertEqual(self.state["tx_records"]["tx1"]["telegram_id"], "42")

    async def test_permanent_removes_only_archived_lead(self):
        await app_main.delete_lead("42")
        result = await app_main.permanently_delete_lead("42")
        self.assertEqual(self.state["memory"]["deleted_leads"], [])
        self.assertEqual(self.state["memory"]["leads"], [{"telegram_id": "43"}])
        self.assertIn("tx1", self.state["tx_records"])
        self.assertEqual(result["state"]["memory"]["deleted_leads"], [])

    async def test_restore_collision_leaves_both_versions_untouched(self):
        await app_main.delete_lead("42")
        self.state["memory"]["leads"].append({"telegram_id": "42", "first_name": "New conversation"})
        before = copy.deepcopy(self.state["memory"])
        writes = self.write_mock.await_count
        with self.assertRaises(app_main.HTTPException) as error:
            await app_main.restore_lead("42")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.state["memory"], before)
        self.assertEqual(self.write_mock.await_count, writes)

    async def test_bulk_delete_archives_every_active_lead_and_preserves_metrics(self):
        await app_main.clear_all_leads()
        self.assertEqual(self.state["memory"]["leads"], [])
        self.assertEqual({lead["telegram_id"] for lead in self.state["memory"]["deleted_leads"]}, {"42", "43"})
        self.assertEqual(self.state["learning"]["confirmed_purchases"], 8)
        self.assertEqual(self.state["flow_bots"][0]["sales_today"], 8)
        self.assertIn("tx1", self.state["tx_records"])

    def test_config_save_cannot_erase_archive_when_no_active_leads(self):
        existing = copy.deepcopy(self.state)
        existing["memory"] = {"leads": [], "deleted_leads": [{**self.lead, "deleted_at": "2026-09-30T10:00:00Z"}]}
        incoming = copy.deepcopy(self.state)
        incoming["memory"] = {"leads": []}
        merged = app_main._merge_config_preserving_runtime(incoming, existing)
        self.assertEqual(merged["memory"], existing["memory"])

    async def test_archive_secrets_are_masked_in_api_but_retained_for_restore(self):
        self.state["memory"]["leads"][0]["bot_token"] = "private-bot-token"
        self.state["memory"]["leads"][0]["details"] = {"api_key": "private-api-key"}
        await app_main.delete_lead("42")
        response = await app_main.get_full_state()
        self.assertNotIn("private-bot-token", str(response))
        self.assertNotIn("private-api-key", str(response))
        self.assertEqual(self.state["memory"]["deleted_leads"][0]["bot_token"], "private-bot-token")

    async def test_missing_ids_do_not_write_or_cancel_tasks(self):
        for operation in (app_main.delete_lead, app_main.restore_lead, app_main.permanently_delete_lead):
            with self.assertRaises(app_main.HTTPException) as error:
                await operation("missing")
            self.assertEqual(error.exception.status_code, 404)
        self.write_mock.assert_not_awaited()
        self.cancel_mock.assert_not_called()

    async def test_rearchive_returning_user_preserves_old_and_new_sessions(self):
        self.state["memory"]["deleted_leads"] = [{"telegram_id": "42", "first_name": "Earlier", "history": [{"content": "old"}], "deleted_at": "yesterday"}]
        self.state["memory"]["leads"][0]["history"] = [{"content": "new"}]
        await app_main.delete_lead("42")
        self.assertEqual([l["telegram_id"] for l in self.state["memory"]["leads"]], ["43"])
        archived = self.state["memory"]["deleted_leads"][0]
        self.assertEqual([h["content"] for h in archived["history"]], ["old", "new"])
        self.assertEqual(archived["first_name"], "Ada")
        self.assertEqual(archived["previous_records"][0]["first_name"], "Earlier")
        self.assertEqual(len(self.state["memory"]["deleted_leads"]), 1)

    async def test_restore_at_active_capacity_does_not_discard_another_lead(self):
        await app_main.delete_lead("42")
        self.state["memory"]["leads"] = [{"telegram_id": str(i + 1000)} for i in range(app_main.MAX_ACTIVE_LEADS)]
        await app_main.restore_lead("42")
        app_main._normalize_providers(self.state)
        self.assertEqual(len(self.state["memory"]["leads"]), app_main.MAX_ACTIVE_LEADS + 1)
        self.assertEqual(self.state["memory"]["leads"][-1]["telegram_id"], str(app_main.MAX_ACTIVE_LEADS - 1 + 1000))
        self.assertEqual(self.state["memory"]["deleted_leads"], [])

    async def test_new_user_at_active_capacity_does_not_discard_existing_lead(self):
        self.state["memory"]["leads"] = [{"telegram_id": str(i + 1000)} for i in range(app_main.MAX_ACTIVE_LEADS)]
        manager = tg_srv.ActiveBotManager()
        await manager._get_or_create_lead(self.state, tg_srv.types.User(id=42, first_name="New"))
        app_main._normalize_providers(self.state)
        self.assertEqual(len(self.state["memory"]["leads"]), app_main.MAX_ACTIVE_LEADS + 1)
        self.assertEqual(self.state["memory"]["leads"][-1]["telegram_id"], str(app_main.MAX_ACTIVE_LEADS - 1 + 1000))

    async def test_archived_lead_can_return_without_erasing_archived_history(self):
        await app_main.delete_lead("42")
        manager = tg_srv.ActiveBotManager()
        user = tg_srv.types.User(id=42, first_name="Ada voltou", username="ada")
        fresh = await manager._get_or_create_lead(self.state, user)
        self.assertEqual(fresh["first_name"], "Ada voltou")
        self.assertEqual(len([l for l in self.state["memory"]["leads"] if str(l.get("telegram_id")) == "42"]), 1)
        self.assertEqual(self.state["memory"]["deleted_leads"][0]["history"], self.lead["history"])
        fresh["history"].append({"role": "user", "content": "New visit"})
        await app_main.delete_lead("42")
        archived = self.state["memory"]["deleted_leads"][0]
        self.assertEqual(len(archived["history"]), 2)
        self.assertEqual(archived["history"][0], self.lead["history"][0])
        self.assertEqual(len(self.state["memory"]["leads"]), 1)

    async def test_payment_notification_does_not_restore_excluded_client(self):
        await app_main.delete_lead("42")
        self.state["tx_records"] = {"tx_paid": {"user_id": 42, "mode": "ai", "status": "pending", "amount": 49}}
        self.state["publish"]["delivery_destination"] = "-100555000"
        manager = tg_srv.ActiveBotManager()
        manager.bot = tg_srv.Bot(token="123456:MOCK")
        async def getter():
            return self.state
        manager.state_getter = getter
        manager.state_saver = self._save
        await manager.deliver_purchase_and_upsell(42, tx_id="tx_paid")
        self.assertEqual([l["telegram_id"] for l in self.state["memory"]["leads"]], ["43"])
        self.assertEqual(self.state["memory"]["deleted_leads"][0]["telegram_id"], "42")

    def test_new_routes_require_admin_auth(self):
        for method, path in (("POST", "/api/leads/{telegram_id}/restore"),
                             ("DELETE", "/api/leads/{telegram_id}/permanent")):
            route = next(r for r in app_main.app.routes if getattr(r, "path", "") == path and method in getattr(r, "methods", set())) if not isinstance(app_main.app.routes, dict) else None
            dependencies = route.dependencies if route is not None else app_main.app.route_dependencies[(method, path)]
            self.assertTrue(any(dep.dependency is app_main.verify_admin_auth for dep in dependencies))
