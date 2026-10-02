"""Daily sales counters must not change when old purchases are purged."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from test_stage1_security import app_main


BRT = timezone(timedelta(hours=-3))


class DeleteOldSalesCounterTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self, *, paid_at, amount=10.0, counted=True, origin="111:BOT_TEST"):
        state = copy.deepcopy(app_main.DEFAULT_SHARKBOT_STATE)
        state["memory"]["leads"] = []
        state["memory"]["deleted_leads"] = [{"telegram_id": "42", "deleted_at": "2020-01-01T00:00:00Z"}]
        state["flow_bots"] = [{"id": "slot", "token": "111:BOT_TEST",
                               "sales_today": 1, "revenue_today": 20.0}]
        state["learning"]["confirmed_purchases"] = 2
        state["tx_records"] = {
            "pix-old": {"user_id": 42, "bot_id": "slot", "bot_token": origin,
                        "mode": "flow", "status": "paid", "sales_counted": counted,
                        "amount": amount, "paid_at": paid_at},
        }
        return state

    async def test_permanent_delete_of_old_sale_does_not_change_today(self):
        state = self.make_state(paid_at="2020-01-01T12:00:00Z")
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            result = await app_main.permanently_delete_lead("42")
        updated = result["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (1, 20.0))
        self.assertEqual(updated["learning"]["confirmed_purchases"], 1)
        self.assertNotIn("pix-old", updated["tx_records"])
    async def test_counted_sale_paid_today_decreases_today(self):
        state = self.make_state(paid_at=datetime.now(BRT).isoformat(), amount=20.0)
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (0, 0.0))

    async def test_unknown_paid_date_does_not_guess_from_checkout_date(self):
        state = self.make_state(paid_at=None)
        state["tx_records"]["pix-old"]["created_at"] = datetime.now(BRT).isoformat()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (1, 20.0))

    async def test_uncounted_sale_does_not_decrease_today(self):
        state = self.make_state(paid_at=datetime.now(BRT).isoformat(), counted=False)
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (1, 20.0))

    async def test_reused_config_id_does_not_subtract_another_bots_sale(self):
        state = self.make_state(paid_at=datetime.now(BRT).isoformat(), amount=20.0, origin="222:OLD_BOT")
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (1, 20.0))

    async def test_utc_date_today_can_still_be_yesterday_in_brasilia(self):
        yesterday_brt = datetime.now(BRT).date() - timedelta(days=1)
        paid_at = datetime.combine(yesterday_brt, datetime.min.time(), BRT).replace(hour=23, minute=30)
        state = self.make_state(paid_at=paid_at.astimezone(timezone.utc).isoformat())
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (1, 20.0))

    async def test_rotated_token_same_bot_still_subtracts_counted_sale(self):
        state = self.make_state(paid_at=datetime.now(BRT).isoformat(), amount=20.0,
                                origin="111:PREVIOUS_TOKEN")
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)):
            updated = (await app_main.permanently_delete_lead("42"))["state"]
        bot = updated["flow_bots"][0]
        self.assertEqual((bot["sales_today"], bot["revenue_today"]), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
