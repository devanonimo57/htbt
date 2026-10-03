"""Focused regressions for bounded delivery and bot attribution."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from test_stage1_security import app_main, tg_srv


class PaymentDeliveryRules(unittest.IsolatedAsyncioTestCase):
    def make_flow(self):
        mgr = tg_srv.ActiveBotManager()
        bot = MagicMock()
        bot.token = "111:alpha"
        bot.create_chat_invite_link = AsyncMock(side_effect=RuntimeError("sem permissão"))
        bot.send_message = AsyncMock()
        mgr.bot = bot
        mgr.current_token = "111:alpha"
        mgr._safe_send_html = AsyncMock()
        mgr._send_flow_medias = AsyncMock()
        state = {
            "flow_bots": [{"id": "alpha", "token": "111:alpha", "active": True, "mode": "flow",
                           "sales_today": 0, "revenue_today": 0, "payments": {}}],
            "tx_records": {"pix-1": {"tx_id": "pix-1", "user_id": 42, "mode": "flow",
                                      "bot_id": "alpha", "bot_token": "111:alpha", "status": "pending",
                                      "amount": 19.9, "delivery_dest_type": "Canal ou grupo",
                                      "delivery_channel_id": "-100999"}},
            "memory": {"leads": [{"telegram_id": "42", "bot_id": "alpha", "history": [],
                                 "stage": "Gerou checkout"}]},
            "publish": {}, "learning": {},
        }
        mgr.state_getter = AsyncMock(return_value=state)
        mgr.state_saver = AsyncMock(side_effect=lambda s, **kw: s)
        return mgr, bot, state

    async def test_one_retry_per_pix_then_manual_pending_even_after_restart(self):
        mgr, bot, state = self.make_flow()
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        # Duplicate webhook/poll notification does not spend the sole retry immediately.
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        self.assertEqual(bot.create_chat_invite_link.await_count, 1)
        state["tx_records"]["pix-1"]["last_delivery_attempt_at"] = (datetime.utcnow() - timedelta(minutes=2)).isoformat()
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        self.assertEqual(bot.create_chat_invite_link.await_count, 2)
        self.assertEqual(state["tx_records"]["pix-1"]["status"], "paid")
        self.assertFalse(state["tx_records"]["pix-1"].get("delivered"))
        self.assertEqual(state["tx_records"]["pix-1"].get("automatic_delivery_attempts"), 2)
        restarted = tg_srv.ActiveBotManager()
        restarted.bot = bot
        restarted.current_token = "111:alpha"
        restarted.state_getter = AsyncMock(return_value=state)
        restarted.state_saver = AsyncMock(side_effect=lambda s, **kw: s)
        self.assertFalse(await restarted.deliver_purchase_and_upsell(42, "pix-1"))
        self.assertEqual(bot.create_chat_invite_link.await_count, 2)
    async def test_retry_worker_skips_invalid_and_exhausted_purchases(self):
        mgr = tg_srv.ActiveBotManager()
        state = {"tx_records": {
            "bad": {"user_id": "???", "status": "paid", "delivered": False, "automatic_delivery_attempts": 1},
            "done": {"user_id": 13, "status": "paid", "delivered": False, "automatic_delivery_attempts": 2},
            "valid": {"user_id": 42, "status": "paid", "delivered": False,
                      "automatic_delivery_attempts": 1,
                      "last_delivery_attempt_at": (datetime.utcnow() - timedelta(minutes=2)).isoformat()},
        }}
        mgr.state_getter = AsyncMock(return_value=state)
        mgr.deliver_purchase_and_upsell = AsyncMock(return_value=True)
        with patch.object(tg_srv.asyncio, "sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            await mgr._delivery_retry_loop()
        mgr.deliver_purchase_and_upsell.assert_awaited_once_with(42, tx_id="valid")
    async def test_gateway_confirmation_does_not_spend_retry_immediately(self):
        mgr = tg_srv.ActiveBotManager()
        mgr.deliver_purchase_and_upsell = AsyncMock(return_value=False)
        gateway = MagicMock()
        gateway.check_transaction_status = AsyncMock(return_value={"status": "paid"})
        with patch.object(tg_srv, "PushinPayClient", return_value=gateway), \
             patch.object(tg_srv.asyncio, "sleep", new=AsyncMock(return_value=None)):
            await mgr._poll_pix_payment(42, "pix-1", "fake", "syncpay")
        mgr.deliver_purchase_and_upsell.assert_awaited_once_with(42, tx_id="pix-1")
    async def test_flow_identity_conflict_stays_paid_without_wrong_bot_delivery(self):
        mgr, bot_a, state = self.make_flow()
        bot_b = MagicMock()
        bot_b.token = "222:beta"
        bot_b.send_message = AsyncMock()
        mgr.extra_bots["222:beta"] = bot_b
        state["flow_bots"].append({"id": "beta", "token": "222:beta", "active": True, "mode": "flow"})
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_id": "alpha", "bot_token": "222:beta", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        bot_a.send_message.assert_not_awaited()
        bot_b.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertFalse(tx.get("sales_counted", False))
        self.assertFalse(tx.get("automatic_delivery_attempts", False))
        self.assertEqual(state["flow_bots"][0]["sales_today"], 0)

    async def test_unavailable_origin_bot_never_falls_back_to_primary(self):
        mgr, bot_a, state = self.make_flow()
        state["flow_bots"].append({"id": "beta", "token": "222:beta", "active": True, "mode": "flow"})
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_id": "beta", "bot_token": "222:beta", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        bot_a.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertFalse(tx.get("delivered", False))
    async def test_manual_resend_rejects_conflicting_bot_identity(self):
        _, _, state = self.make_flow()
        tx = state["tx_records"]["pix-1"]
        tx.update({"status": "paid", "bot_token": "222:beta", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        temp_bot = MagicMock()
        temp_bot.send_message = AsyncMock()
        temp_bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance", return_value=temp_bot):
            result = await app_main.resend_vip_access(app_main.AccessResendRequest(tx_id="pix-1"))
        self.assertEqual(result["sent_count"], 0)
        temp_bot.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertFalse(tx.get("delivered", False))

    async def test_manual_resend_remains_available_after_auto_retry_exhausted(self):
        _, _, state = self.make_flow()
        tx = state["tx_records"]["pix-1"]
        tx.update({"status": "paid", "automatic_delivery_attempts": 2,
                   "delivery_dest_type": "Link externo", "delivery_external_url": "https://example.com/access"})
        temp_bot = MagicMock()
        temp_bot.send_message = AsyncMock()
        temp_bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)), \
             patch.object(tg_srv, "_create_bot_instance", return_value=temp_bot):
            result = await app_main.resend_vip_access(app_main.AccessResendRequest(tx_id="pix-1"))
        self.assertEqual(result["sent_count"], 1)
        self.assertTrue(tx["delivered"])
        self.assertEqual(tx["automatic_delivery_attempts"], 2)
        temp_bot.send_message.assert_awaited_once()
    async def test_missing_origin_token_never_uses_replacement_bot(self):
        mgr, primary, state = self.make_flow()
        replacement = MagicMock(token="222:beta", send_message=AsyncMock())
        mgr.extra_bots["222:beta"] = replacement
        state["flow_bots"][0]["token"] = "222:beta"
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_token": "", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        primary.send_message.assert_not_awaited()
        replacement.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertFalse(tx.get("sales_counted", False))

    async def test_ai_purchase_with_unavailable_origin_does_not_use_primary_bot(self):
        mgr, primary, state = self.make_flow()
        tx = state["tx_records"]["pix-1"]
        tx.update({"mode": "ai", "bot_id": "222", "bot_token": "222:beta",
                   "delivery_destination": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        primary.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertFalse(tx.get("delivered", False))

    async def test_ai_link_uses_origin_bot_when_it_is_available(self):
        mgr, primary, state = self.make_flow()
        secondary = MagicMock(token="222:beta", send_message=AsyncMock())
        mgr.extra_bots["222:beta"] = secondary
        tx = state["tx_records"]["pix-1"]
        tx.update({"mode": "ai", "bot_id": "222", "bot_token": "222:beta",
                   "delivery_destination": "https://example.com/access"})
        self.assertTrue(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        primary.send_message.assert_not_awaited()
        secondary.send_message.assert_awaited_once()

    async def test_stale_current_token_cannot_substitute_primary_bot(self):
        mgr, primary, state = self.make_flow()
        mgr.current_token = "222:beta"
        tx = state["tx_records"]["pix-1"]
        tx.update({"mode": "ai", "bot_id": "222", "bot_token": "222:beta",
                   "delivery_destination": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        primary.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")

    async def test_stale_extra_bot_registry_cannot_substitute_another_bot(self):
        mgr, primary, state = self.make_flow()
        mgr.extra_bots["222:beta"] = primary
        tx = state["tx_records"]["pix-1"]
        tx.update({"mode": "ai", "bot_id": "222", "bot_token": "222:beta",
                   "delivery_destination": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        primary.send_message.assert_not_awaited()

    async def test_origin_recovery_runs_initial_send_once_then_stops(self):
        mgr, primary, state = self.make_flow()
        state["flow_bots"].append({"id": "beta", "token": "222:beta", "active": True, "mode": "flow"})
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_id": "beta", "bot_token": "222:beta", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        self.assertEqual(tx["status"], "paid")
        self.assertTrue(tx.get("initial_delivery_pending"))
        self.assertEqual(tx.get("automatic_delivery_attempts", 0), 0)
        recovered = MagicMock(token="222:beta", send_message=AsyncMock())
        mgr.extra_bots["222:beta"] = recovered
        with patch.object(tg_srv.asyncio, "sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            await mgr._delivery_retry_loop()
        self.assertTrue(tx.get("delivered"), tx)
        self.assertIs(mgr._safe_send_html.await_args.kwargs["bot_instance"], recovered)
        primary.send_message.assert_not_awaited()
        self.assertEqual(tx["automatic_delivery_attempts"], 1)

    async def test_manual_resend_refuses_purchase_with_missing_origin_token(self):
        _, _, state = self.make_flow()
        state["flow_bots"][0]["token"] = "222:beta"
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_token": "", "status": "paid", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        temp_bot = MagicMock(send_message=AsyncMock())
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance", return_value=temp_bot):
            result = await app_main.resend_vip_access(app_main.AccessResendRequest(tx_id="pix-1"))
        self.assertEqual(result["sent_count"], 0)
        temp_bot.send_message.assert_not_awaited()

    async def test_manual_resend_does_not_send_stale_invite_when_fresh_creation_fails(self):
        _, _, state = self.make_flow()
        tx = state["tx_records"]["pix-1"]
        tx.update({"status": "paid", "automatic_delivery_attempts": 2,
                   "invite_url": "https://t.me/+expired", "delivered": False})
        temp_bot = MagicMock()
        temp_bot.send_message = AsyncMock()
        temp_bot.create_chat_invite_link = AsyncMock(side_effect=RuntimeError("no permission"))
        temp_bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance", return_value=temp_bot):
            result = await app_main.resend_vip_access(app_main.AccessResendRequest(tx_id="pix-1"))
        self.assertEqual(result["sent_count"], 0)
        temp_bot.send_message.assert_not_awaited()
        self.assertFalse(tx["delivered"])

    async def test_stale_numeric_config_id_cannot_override_replaced_token(self):
        mgr, original_bot, state = self.make_flow()
        cfg = state["flow_bots"][0]
        cfg["bot_id"] = "111"  # stale ID retained after replacing the config token
        cfg["token"] = "222:beta"
        tx = state["tx_records"]["pix-1"]
        tx.update({"delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        original_bot.send_message.assert_not_awaited()
        self.assertEqual(tx["status"], "paid")
        self.assertTrue(tx.get("attribution_uncertain"))
        self.assertFalse(tx.get("sales_counted", False))

    async def test_stale_numeric_config_id_cannot_match_token_only_purchase(self):
        mgr, original_bot, state = self.make_flow()
        cfg = state["flow_bots"][0]
        cfg["bot_id"] = "111"
        cfg["token"] = "222:beta"
        tx = state["tx_records"]["pix-1"]
        tx.update({"bot_id": "", "delivery_dest_type": "Link externo",
                   "delivery_external_url": "https://example.com/access"})
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        original_bot.send_message.assert_not_awaited()
        self.assertTrue(tx.get("attribution_uncertain"))

    async def test_paid_legacy_without_count_marker_stays_unattributed_on_bot_conflict(self):
        mgr, _, state = self.make_flow()
        state["flow_bots"].append({"id": "beta", "token": "222:beta", "active": True, "mode": "flow"})
        tx = state["tx_records"]["pix-1"]
        tx.update({"status": "paid", "bot_token": "222:beta",
                   "paid_at": datetime.now(timezone(timedelta(hours=-3))).isoformat()})
        self.assertNotIn("sales_counted", tx)
        self.assertFalse(await mgr.deliver_purchase_and_upsell(42, "pix-1"))
        report = app_main.reconcile_daily_counters(state)
        self.assertEqual(tx["status"], "paid")
        self.assertNotIn("sales_counted", tx)  # prior increment is unknown
        self.assertEqual(state["flow_bots"][0]["sales_today"], 0)
        self.assertIn("pix-1", [a["tx_id"] for a in report["unreconciled_anomalies"]])

    async def test_daily_sales_count_each_pix_but_flag_explicit_uncounted(self):
        now_brt = datetime.now(timezone(timedelta(hours=-3))).isoformat()
        state = {"flow_bots": [{"id": "alpha", "sales_today": 99, "revenue_today": 999}],
                 "tx_records": {
                     "pix_a": {"bot_id": "alpha", "status": "paid", "sales_counted": True,
                               "amount": 19.9, "paid_at": now_brt},
                     "pix_b": {"bot_id": "alpha", "status": "paid", "sales_counted": True,
                               "amount": 19.9, "paid_at": now_brt},
                     "pix_conflict": {"bot_id": "alpha", "status": "paid", "sales_counted": False,
                                      "amount": 19.9, "paid_at": now_brt},
                 }}
        report = app_main.reconcile_daily_counters(state)
        self.assertEqual(state["flow_bots"][0]["sales_today"], 2)
        self.assertEqual(state["flow_bots"][0]["revenue_today"], 39.8)
        self.assertIn("pix_conflict", [a["tx_id"] for a in report["unreconciled_anomalies"]])


if __name__ == "__main__":
    unittest.main()
