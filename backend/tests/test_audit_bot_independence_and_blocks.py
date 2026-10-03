import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from telegram_service import ActiveBotManager, TelegramForbiddenError, TelegramRetryAfter


class TestAuditBotIndependenceAndBlocks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mgr = ActiveBotManager()
        self.primary_bot = MagicMock()
        self.primary_bot.token = "111111:PRIMARY_TOKEN"
        self.primary_bot.send_message = AsyncMock()

        self.extra_bot = MagicMock()
        self.extra_bot.token = "222222:EXTRA_TOKEN"
        self.extra_bot.send_message = AsyncMock()

        self.mgr.bot = self.primary_bot
        self.mgr._primary_bot = self.primary_bot
        self.mgr.current_token = self.primary_bot.token
        self.mgr.extra_bots = {"222222:EXTRA_TOKEN": self.extra_bot}

        self.state = {
            "publish": {
                "bot_token": self.primary_bot.token,
                "pushin_pay_token": "mock_token",
                "gateway_provider": "syncpay",
                "gateway_secret": "mock_sec"
            },
            "flow_bots": [
                {
                    "id": "fb_extra",
                    "token": "222222:EXTRA_TOKEN",
                    "active": True
                }
            ],
            "memory": {
                "leads": [
                    {
                        "telegram_id": "777",
                        "first_name": "LeadExtra",
                        "stage": "Conversando",
                        "blocked": False,
                        "origin_bot_numeric_id": "222222",
                        "bot_token": "222222:EXTRA_TOKEN"
                    }
                ]
            },
            "downsell": {
                "enabled": True,
                "steps": [
                    {"id": "step_1", "delay_minutes": 1, "message": "Downsell test"}
                ]
            },
            "upsell": {
                "enabled": True,
                "delay_minutes": 1,
                "message": "Upsell test",
                "offer_id": "off_1"
            },
            "business": {
                "offers": [
                    {"id": "off_1", "name": "VIP Offer", "price": 29.9}
                ]
            },
            "tx_records": {}
        }

        async def _getter():
            return self.state

        async def _saver(new_st, **kwargs):
            self.state = new_st

        self.mgr.state_getter = _getter
        self.mgr.state_saver = _saver

    async def asyncTearDown(self):
        await self.mgr.stop()

    async def test_blocked_lead_cancels_tasks_and_flags_state(self):
        """Quando o Telegram informa bloqueio (Forbidden), o lead é marcado como bloqueado."""
        lead = self.state["memory"]["leads"][0]
        self.assertFalse(lead["blocked"])

        # Dispara _mark_lead_blocked
        await self.mgr._mark_lead_blocked(777)

        self.assertTrue(lead["blocked"])
        self.assertEqual(lead["stage"], "Bloqueou o bot")

    async def test_safe_send_html_handles_forbidden_error(self):
        """_safe_send_html intercepta TelegramForbiddenError e marca lead como bloqueado sem crash."""
        self.extra_bot.send_message.side_effect = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")

        lead = self.state["memory"]["leads"][0]
        self.assertFalse(lead["blocked"])

        res = await self.mgr._safe_send_html(777, "Olá", bot_instance=self.extra_bot)
        self.assertIsNone(res)
        self.assertTrue(lead["blocked"])
        self.assertEqual(lead["stage"], "Bloqueou o bot")

    async def test_downsell_does_not_send_to_blocked_lead(self):
        """Leads marcados como blocked=True são imediatamente ignorados no downsell."""
        lead = self.state["memory"]["leads"][0]
        lead["blocked"] = True

        # Executa worker
        await self.mgr._downsell_worker(777, "LeadExtra", bot_token="222222:EXTRA_TOKEN")
        self.extra_bot.send_message.assert_not_called()
        self.primary_bot.send_message.assert_not_called()

    async def test_upsell_uses_origin_bot_instance(self):
        """Worker de upsell despacha via bot de origem e não via self.bot."""
        # Modo IA: sem flow_bots para o bot extra
        self.state["flow_bots"] = []
        self.state["upsell"]["delay_minutes"] = 0

        # Simula envio com sucesso
        self.extra_bot.send_message = AsyncMock(return_value=MagicMock())

        # Executa _upsell_worker com bot_token do bot extra
        # sobrescrevendo sleep para não aguardar
        original_sleep = asyncio.sleep
        try:
            asyncio.sleep = AsyncMock()
            await self.mgr._upsell_worker(777, bot_token="222222:EXTRA_TOKEN")
        finally:
            asyncio.sleep = original_sleep

        # O bot extra deve ter sido chamado, e o bot primário NÃO deve ter sido chamado para o lead do bot extra
        self.extra_bot.send_message.assert_called()
        self.primary_bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
