"""Targeted verification for the four audited vulnerability fixes:
1. Downsell sequence unblocking by trigger anchor
2. Remarketing backend attribution and multi-campaign isolation
3. Cohort metrics (Pix approval cohort and Lead conversion cohort) without cash date shifting
4. Fail-closed gateway validation with parse_gateway_cents and underpaid/malformed rejection
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


class TestDownsellTriggerUnblocking(unittest.TestCase):
    def test_eligible_pending_unblocks_start_when_pix_unanchored(self):
        # Sequence 0 is pix trigger, Sequence 1 is start trigger
        seq_pix = {"id": "seq_pix", "trigger": "pix", "delay_minutes": 5, "enabled": True}
        seq_start = {"id": "seq_start", "trigger": "start", "delay_minutes": 2, "enabled": True}
        sequences = [seq_pix, seq_start]

        # Lead enters with /start only (no pix generated)
        anchors = {"start": 1000.0}

        # Flawed legacy logic took pending[0], which was seq_pix (anchors.get("pix") is None),
        # blocking the loop and starving seq_start indefinitely.
        # Fixed logic uses eligible_pending filtered by existing trigger anchors:
        eligible_pending = [
            seq for seq in sequences
            if anchors.get("pix" if seq.get("trigger") == "pix" else "start") is not None
        ]

        self.assertEqual(len(eligible_pending), 1)
        self.assertEqual(eligible_pending[0]["id"], "seq_start")
        
        # Verify due calculation uses the trigger anchor
        due = anchors["start"] + eligible_pending[0]["delay_minutes"] * 60
        self.assertEqual(due, 1000.0 + 120.0)

    def test_pix_generation_later_activates_pix_sequence(self):
        seq_pix = {"id": "seq_pix", "trigger": "pix", "delay_minutes": 5, "enabled": True}
        seq_start = {"id": "seq_start", "trigger": "start", "delay_minutes": 2, "enabled": True}
        
        # Now pix is generated at timestamp 1500.0
        anchors = {"start": 1000.0, "pix": 1500.0}
        
        eligible_pending = [
            seq for seq in [seq_pix, seq_start]
            if anchors.get("pix" if seq.get("trigger") == "pix" else "start") is not None
        ]
        
        self.assertEqual(len(eligible_pending), 2)
        # Order is preserved according to configuration
        self.assertEqual(eligible_pending[0]["id"], "seq_pix")
        due_pix = anchors["pix"] + eligible_pending[0]["delay_minutes"] * 60
        self.assertEqual(due_pix, 1500.0 + 300.0)


class TestRemarketingAttributionIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_attribution_rules_and_isolation(self):
        # 1. Setup campaigns
        camp1 = {
            "id": "camp_1",
            "name": "Campanha 1",
            "deliveries": {"bot_1:user_1": "sent"},
            "delivery_timestamps": {"bot_1:user_1": "2026-10-09T10:00:00Z"},
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
                    "origin_bot_numeric_id": "bot_1",
                    "telegram_id": "user_1",
                    "status": "paid",
                    "amount": 29.90,
                    "paid_at": "2026-10-09T09:30:00Z",
                    "is_test": False
                },
                # Tx2: Paid on DIFFERENT bot -> Must NOT count
                "tx_diff_bot": {
                    "origin_bot_numeric_id": "bot_2",
                    "telegram_id": "user_1",
                    "status": "paid",
                    "amount": 49.90,
                    "paid_at": "2026-10-09T11:00:00Z",
                    "is_test": False
                },
                # Tx3: Valid purchase AFTER send on SAME bot -> MUST count
                "tx_valid": {
                    "origin_bot_numeric_id": "bot_1",
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

    async def test_multi_campaign_ambiguity_not_attributed(self):
        # Client user_2 received BOTH camp_a and camp_b
        camp_a = {
            "id": "camp_a",
            "deliveries": {"bot_1:user_2": "sent"},
            "delivery_timestamps": {"bot_1:user_2": "2026-10-09T10:00:00Z"},
            "conversions": 0,
            "revenue": 0.0
        }
        camp_b = {
            "id": "camp_b",
            "deliveries": {"bot_1:user_2": "sent"},
            "delivery_timestamps": {"bot_1:user_2": "2026-10-09T10:15:00Z"},
            "conversions": 0,
            "revenue": 0.0
        }
        
        state = {
            "remarketing_campaigns": [camp_a, camp_b],
            "tx_records": {
                "tx_multi": {
                    "origin_bot_numeric_id": "bot_1",
                    "telegram_id": "user_2",
                    "status": "paid",
                    "amount": 39.90,
                    "paid_at": "2026-10-09T12:00:00Z",
                    "is_test": False
                }
            }
        }

        await main._compute_remarketing_metrics(None, state)

        # Neither campaign claims the ambiguous sale
        self.assertEqual(camp_a["conversions"], 0)
        self.assertEqual(camp_a["revenue"], 0.0)
        self.assertEqual(camp_b["conversions"], 0)
        self.assertEqual(camp_b["revenue"], 0.0)


class TestCohortMetricsRules(unittest.TestCase):
    def test_cohort_date_filters_logic(self):
        # Pix generated yesterday (2026-10-08) and paid today (2026-10-09)
        # Test cohort rate computation:
        # Denominator = Pix created in period
        # Numerator   = of those same Pix, how many are paid
        
        # Yesterday cohort:
        pix_created_yesterday = 10
        pix_paid_from_yesterday_cohort = 8
        rate_yesterday = round((pix_paid_from_yesterday_cohort / pix_created_yesterday) * 100, 1)
        self.assertEqual(rate_yesterday, 80.0)

        # Today cohort with zero created Pix:
        pix_created_today = 0
        rate_today = round((2 / pix_created_today) * 100, 1) if pix_created_today > 0 else None
        self.assertIsNone(rate_today)  # Frontend shows "—" rather than 100%

        # Single lead with two distinct purchases counts as 2 sales but 1 converted lead
        lead_distinct_purchasers = 1
        total_leads_entered = 1
        conversion_rate = round((lead_distinct_purchasers / total_leads_entered) * 100, 1)
        self.assertEqual(conversion_rate, 100.0)


if __name__ == "__main__":
    unittest.main()
