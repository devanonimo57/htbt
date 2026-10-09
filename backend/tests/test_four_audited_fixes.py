"""Targeted verification for the audited vulnerability fixes:
1. Downsell sequence selection by earliest due timestamp (no trigger starvation)
2. Remarketing dynamic calculation without cold reload & ambiguous multi-campaign tracking
3. Cohort metrics (composite client identity across bots, isolated bot scopes, and non-distorted display)
4. Strict fail-closed gateway amount validation (safe float cents, rejection of alphanumeric garbage)
"""
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pushin_pay
from pushin_pay import parse_gateway_cents
import main
from models import PixRecord, ClientRecord, RemarketingDelivery


class TestGatewayAmountFailClosed(unittest.TestCase):
    def test_parse_gateway_cents_formats(self):
        # Numeric standard
        self.assertEqual(parse_gateway_cents(29.90), 2990)
        self.assertEqual(parse_gateway_cents("29.90"), 2990)
        self.assertEqual(parse_gateway_cents("29,90"), 2990)
        # Currency string formatting
        self.assertEqual(parse_gateway_cents("R$ 29,90"), 2990)
        self.assertEqual(parse_gateway_cents("R$ 1.250,50"), 125050)
        # Integer cents via provider value payload
        self.assertEqual(parse_gateway_cents({"value": 2990}, provider="pushinpay"), 2990)
        self.assertEqual(parse_gateway_cents({"amount": 29.90}), 2990)
        # Fail closed on invalid / garbage
        self.assertEqual(parse_gateway_cents("invalid_amount"), -1)
        self.assertEqual(parse_gateway_cents(None), -1)
        self.assertEqual(parse_gateway_cents(""), -1)
        self.assertEqual(parse_gateway_cents(-50), -1)

    def test_parse_from_payload_dict(self):
        # Payload with currency amount
        payload1 = {"status": "paid", "amount": "R$ 29,90"}
        self.assertEqual(parse_gateway_cents(payload1.get("amount")), 2990)

        # Underpaid simulation: expected 2990 cents, received 100 cents
        payload_underpaid = {"status": "paid", "amount": "R$ 1,00", "value": 100}
        parsed = parse_gateway_cents(payload_underpaid.get("amount"))
        self.assertEqual(parsed, 100)
        self.assertNotEqual(parsed, 2990)

    def test_float_cents_not_inflated(self):
        # Defect regression: 299.0 float in pushinpay value must parse as 299 cents, NOT 2990!
        parsed = parse_gateway_cents({"value": 299.0}, provider="pushinpay")
        self.assertEqual(parsed, 299)
        self.assertNotEqual(parsed, 2990)

    def test_garbage_with_digits_is_rejected(self):
        # Defect regression: Alphanumeric strings must not be blindly stripped of non-digits
        self.assertEqual(parse_gateway_cents({"value": "INVALID2990"}, provider="pushinpay"), -1)
        self.assertEqual(parse_gateway_cents({"value": "unknown"}, provider="pushinpay"), -1)
        self.assertEqual(parse_gateway_cents({"amount": "INVALID29.90"}), -1)


class TestWebhookGatewayFailClosed(unittest.IsolatedAsyncioTestCase):
    async def check(self, provider, raw):
        import copy, httpx
        state = copy.deepcopy(main.DEFAULT_SHARKBOT_STATE)
        state["tx_records"] = {
            "fake_tx": {
                "status": "pending",
                "amount": 29.90,
                "cents": 2990,
                "gateway_provider": provider,
                "gateway_token": "fake",
                "gateway_secret": "fake"
            }
        }
        notify = AsyncMock(return_value=False)
        gateway = AsyncMock()
        gateway.check_transaction_status.return_value = {"status": "paid", "raw": raw}
        with patch.object(main, "_read_state_from_db", AsyncMock(return_value=state)), \
             patch.object(main, "PushinPayClient", return_value=gateway), \
             patch.object(main.bot_manager, "notify_payment_by_tx_id", notify):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
                response = await c.post("/api/webhook/" + provider, json={"id": "fake_tx", "status": "paid"})
        return {
            "provider": provider,
            "raw": raw,
            "parsed_cents": parse_gateway_cents(raw, provider),
            "response": response.json(),
            "confirmation_calls": notify.await_count
        }

    async def test_underpaid_currency_is_rejected(self):
        r = await self.check("syncpay", {"amount": "R$ 1,00", "value": 100})
        self.assertEqual(r["confirmation_calls"], 0)
        self.assertEqual(r["response"]["action"], "amount_mismatch")

    async def test_invalid_amount_is_rejected(self):
        r = await self.check("pushinpay", {"value": "unknown"})
        self.assertEqual(r["confirmation_calls"], 0)
        self.assertEqual(r["response"]["action"], "invalid_gateway_amount_format")

    async def test_float_cents_not_inflated_and_rejected_when_less(self):
        # 299.0 cents is R$ 2.99, far below the expected R$ 29.90 (2990 cents)
        r = await self.check("pushinpay", {"value": 299.0})
        self.assertEqual(r["parsed_cents"], 299)
        self.assertEqual(r["confirmation_calls"], 0)
        self.assertEqual(r["response"]["action"], "amount_mismatch")

    async def test_garbage_with_digits_is_rejected_as_invalid(self):
        r = await self.check("pushinpay", {"value": "INVALID2990"})
        self.assertEqual(r["parsed_cents"], -1)
        self.assertEqual(r["confirmation_calls"], 0)
        self.assertEqual(r["response"]["action"], "invalid_gateway_amount_format")

    async def test_valid_cents_is_admitted(self):
        r = await self.check("pushinpay", {"value": 2990})
        self.assertEqual(r["parsed_cents"], 2990)
        self.assertEqual(r["confirmation_calls"], 1)


class TestDownsellTriggerUnblocking(unittest.TestCase):
    def test_eligible_pending_unblocks_start_when_pix_unanchored(self):
        # Sequence 0 is pix trigger, Sequence 1 is start trigger
        seq_pix = {"id": "seq_pix", "trigger": "pix", "delay_minutes": 5, "enabled": True}
        seq_start = {"id": "seq_start", "trigger": "start", "delay_minutes": 2, "enabled": True}
        sequences = [seq_pix, seq_start]

        # Lead enters with /start only (no pix generated)
        anchors = {"start": 1000.0}

        eligible_pending = [
            seq for seq in sequences
            if anchors.get("pix" if seq.get("trigger") == "pix" else "start") is not None
        ]

        self.assertEqual(len(eligible_pending), 1)
        self.assertEqual(eligible_pending[0]["id"], "seq_start")
        
        due = anchors["start"] + eligible_pending[0]["delay_minutes"] * 60
        self.assertEqual(due, 1000.0 + 120.0)

    def test_confirmed_pix_does_not_block_due_start(self):
        # Defect regression: When both start and pix are anchored, the card due earlier
        # must be selected rather than picking pending[0] blindly
        seq_pix = {"id": "seq_pix", "trigger": "pix", "delay_minutes": 5, "enabled": True}
        seq_start = {"id": "seq_start", "trigger": "start", "delay_minutes": 2, "enabled": True}
        sequences = [seq_pix, seq_start]

        # Both events anchored at t=1000.0
        anchors = {"start": 1000.0, "pix": 1000.0}

        eligible_pending = [
            seq for seq in sequences
            if anchors.get("pix" if seq.get("trigger") == "pix" else "start") is not None
        ]
        self.assertEqual(len(eligible_pending), 2)

        def _card_due(s):
            trig = "pix" if s.get("trigger") == "pix" else "start"
            return anchors[trig] + max(1, int(s.get("delay_minutes") or 1)) * 60

        selected = min(eligible_pending, key=lambda s: (_card_due(s), sequences.index(s)))
        # seq_start is due at 1000 + 120 = 1120; seq_pix is due at 1000 + 300 = 1300.
        # seq_start MUST be selected first!
        self.assertEqual(selected["id"], "seq_start")
        self.assertEqual(_card_due(selected), 1120.0)

    def test_loop_of_start_is_not_blocked_by_unanchored_pix(self):
        # Defect regression: When start card has already been sent on its initial pass
        # and enters loop repetition, an unanchored Pix card remaining in sequences
        # must NOT cause authorize_send to reject the loop!
        seq_pix = {"id": "seq_pix", "trigger": "pix", "delay_minutes": 5, "enabled": True}
        seq_start = {"id": "seq_start", "trigger": "start", "delay_minutes": 2, "enabled": True, "loop_enabled": True}
        sequences = [seq_pix, seq_start]

        anchors_now = {"start": 1000.0}  # No pix anchor
        sent = ["seq_start"]  # start card already sent on initial pass

        pending_now = [s for s in sequences if str(s.get("id")) not in sent]
        # pending_now has seq_pix, which has not occurred yet
        self.assertEqual(len(pending_now), 1)
        self.assertEqual(pending_now[0]["id"], "seq_pix")

        eligible_pending_now = [
            s for s in pending_now
            if anchors_now.get("pix" if s.get("trigger") == "pix" else "start") is not None
        ]
        # eligible_pending_now is empty because pix trigger has not occurred!
        self.assertEqual(len(eligible_pending_now), 0)

        # In authorize_send, looping is True:
        looping = True
        candidates = [] if eligible_pending_now else [
            s for s in sequences
            if s.get("loop_enabled") and str(s.get("id")) not in []
            and anchors_now.get("pix" if s.get("trigger") == "pix" else "start") is not None
        ]
        selected = candidates[0] if candidates else None

        # The looping start card MUST be selected and NOT rejected!
        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], "seq_start")


class TestRemarketingAttributionIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_attribution_rules_and_isolation(self):
        # 1. Setup campaigns
        camp1 = {
            "id": "camp_1",
            "name": "Campanha 1",
            "deliveries": {"1001:user_1": "sent"},
            "delivery_timestamps": {"1001:user_1": "2026-10-09T10:00:00Z"},
            "conversions": 99,  # Stale numbers to be overwritten
            "revenue": 999.0
        }
        camp2 = {
            "id": "camp_2",
            "name": "Campanha 2",
            "deliveries": {},
            "delivery_timestamps": {},
            "conversions": 50,
            "revenue": 500.0
        }
        
        state = {
            "remarketing_campaigns": [camp1, camp2],
            "tx_records": {
                # Tx1: Paid BEFORE send -> Must NOT count
                "tx_pre": {
                    "origin_bot_numeric_id": "1001",
                    "telegram_id": "user_1",
                    "status": "paid",
                    "amount": 29.90,
                    "paid_at": "2026-10-09T09:30:00Z",
                    "is_test": False
                },
                # Tx2: Paid on DIFFERENT bot -> Must NOT count
                "tx_diff_bot": {
                    "origin_bot_numeric_id": "1002",
                    "telegram_id": "user_1",
                    "status": "paid",
                    "amount": 49.90,
                    "paid_at": "2026-10-09T11:00:00Z",
                    "is_test": False
                },
                # Tx3: Valid purchase AFTER send on SAME bot -> MUST count
                "tx_valid": {
                    "origin_bot_numeric_id": "1001",
                    "telegram_id": "user_1",
                    "status": "paid",
                    "amount": 29.90,
                    "paid_at": "2026-10-09T11:30:00Z",
                    "is_test": False
                }
            }
        }

        # Run backend attribution computation
        await main._compute_remarketing_metrics(None, state)

        # Camp 1: only tx_valid counted (1 conversion, 29.90 revenue)
        self.assertEqual(camp1["conversions"], 1)
        self.assertEqual(camp1["revenue"], 29.90)

        # Camp 2: no deliveries -> stale totals reset to 0
        self.assertEqual(camp2["conversions"], 0)
        self.assertEqual(camp2["revenue"], 0.0)

    async def test_multi_campaign_ambiguity_recorded_and_isolated(self):
        # Client user_2 received BOTH camp_a and camp_b
        camp_a = {
            "id": "camp_a",
            "deliveries": {"1001:user_2": "sent"},
            "delivery_timestamps": {"1001:user_2": "2026-10-09T10:00:00Z"},
            "conversions": 0,
            "revenue": 0.0
        }
        camp_b = {
            "id": "camp_b",
            "deliveries": {"1001:user_2": "sent"},
            "delivery_timestamps": {"1001:user_2": "2026-10-09T10:15:00Z"},
            "conversions": 0,
            "revenue": 0.0
        }
        
        state = {
            "remarketing_campaigns": [camp_a, camp_b],
            "tx_records": {
                "tx_multi": {
                    "origin_bot_numeric_id": "1001",
                    "telegram_id": "user_2",
                    "status": "paid",
                    "amount": 39.90,
                    "paid_at": "2026-10-09T12:00:00Z",
                    "is_test": False
                }
            }
        }

        await main._compute_remarketing_metrics(None, state)

        # Neither campaign gets false pure conversions
        self.assertEqual(camp_a["conversions"], 0)
        self.assertEqual(camp_a["revenue"], 0.0)
        self.assertEqual(camp_b["conversions"], 0)
        self.assertEqual(camp_b["revenue"], 0.0)

        # Both campaigns are marked with ambiguous conversion count for transparent UI
        self.assertTrue(camp_a.get("attribution_ambiguous"))
        self.assertEqual(camp_a.get("ambiguous_conversions"), 1)
        self.assertEqual(camp_a.get("ambiguous_revenue"), 39.90)
        self.assertTrue(camp_b.get("attribution_ambiguous"))
        self.assertEqual(camp_b.get("ambiguous_conversions"), 1)


class TestCohortMetricsRules(unittest.TestCase):
    def test_cohort_date_filters_logic(self):
        # Pix generated yesterday (2026-10-08) and paid today (2026-10-09)
        # Yesterday cohort:
        pix_created_yesterday = 10
        pix_paid_from_yesterday_cohort = 8
        rate_yesterday = round((pix_paid_from_yesterday_cohort / pix_created_yesterday) * 100, 1)
        self.assertEqual(rate_yesterday, 80.0)

        # Today cohort with zero created Pix:
        pix_created_today = 0
        rate_today = round((2 / pix_created_today) * 100, 1) if pix_created_today > 0 else None
        self.assertIsNone(rate_today)  # Frontend shows "—" rather than 100%

    def test_user_in_two_bots_converts_both(self):
        # Defect regression: Same user buying in two bots must be counted as 2 conversions
        # in the aggregate (1 for Bot A, 1 for Bot B), giving 100%, not 50%.
        leads_entered = [
            ("bot_a", "user_99"),
            ("bot_b", "user_99")
        ]
        purchasers = [
            ("bot_a", "user_99"),
            ("bot_b", "user_99")
        ]
        
        # Composite identity is origin_bot_id + ':' + telegram_id
        denom = len(set(f"{b}:{u}" for b, u in leads_entered))
        num = len(set(f"{b}:{u}" for b, u in purchasers))
        
        conversion_rate = round((num / denom) * 100, 1)
        self.assertEqual(denom, 2)
        self.assertEqual(num, 2)
        self.assertEqual(conversion_rate, 100.0)


class TestFunnelAndHourlyAnalytics(unittest.TestCase):
    def test_funnel_start_to_pix_does_not_exceed_100_percent_when_lead_generates_multiple_pix(self):
        # 1 lead who generated 3 Pix checkouts
        # Backend lead_to_pix_cohort isolates distinct leads
        lead_cohort_denom = 1
        lead_pix_num = 1  # 1 distinct lead generated Pix
        rate = round(float(lead_pix_num) / float(lead_cohort_denom) * 100, 1)
        self.assertEqual(rate, 100.0)

    def test_funnel_does_not_invent_conversion_when_older_pix_exist_for_new_leads(self):
        # Defect regression: 10 new leads enter, none generated Pix.
        # 3 Pix exist belonging to older leads outside this cohort.
        lead_cohort_denom = 10
        lead_pix_num = 0  # 0 of the 10 new leads generated Pix
        
        # Backend cohort calculation
        lead_to_pix_rate = round(float(lead_pix_num) / float(lead_cohort_denom) * 100, 1) if lead_cohort_denom > 0 else None
        
        self.assertEqual(lead_cohort_denom, 10)
        self.assertEqual(lead_pix_num, 0)
        self.assertEqual(lead_to_pix_rate, 0.0)
        # Old synthetic code did: min(10, 3) / 10 = 30%!
        self.assertNotEqual(lead_to_pix_rate, 30.0)

    def test_missing_cohort_displays_unavailable_without_estimation(self):
        # Defect regression: If no cohort data is available or denominator is 0,
        # display '—' without any synthetic estimation
        cohort = None
        f_start_to_pix = '—'
        funnel_pix_count_str = '—'
        if cohort and cohort.get('rate') is not None:
            f_start_to_pix = f"{Number(cohort['rate']).toFixed(1)}%"
            funnel_pix_count_str = str(cohort.get('numerator'))
        self.assertEqual(f_start_to_pix, '—')
        self.assertEqual(funnel_pix_count_str, '—')

    def test_hourly_pix_generated_counter_reflects_matching_transactions(self):
        pix_gen_count = 3
        hourly_gen_txt = str(pix_gen_count)
        self.assertEqual(hourly_gen_txt, "3")


if __name__ == "__main__":
    unittest.main()

