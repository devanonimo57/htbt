"""Regression tests: CRM actions must distinguish identical Telegram users by bot."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_stage1_security import app_main, tg_srv


class CrmBotIsolation(unittest.IsolatedAsyncioTestCase):
    def committed_block(self, state, **kwargs):
        # Model the fresh writer intent, not the old pre-commit live mutation.
        event = kwargs.get('lead_block_event')
        if event:
            lead = app_main._client_map(state)[(event['origin'],event['uid'],False)]
            lead['blocked'] = not bool(lead.get('blocked',False))
            self.mock_published['memory']['leads'] = state['memory']['leads']
        return state

    def make_state(self):
        alpha = {"telegram_id": "42", "bot_id": "alpha", "origin_bot_numeric_id": "111", "first_name": "A",
                 "blocked": False, "history": []}
        beta = {"telegram_id": "42", "bot_id": "beta", "origin_bot_numeric_id": "222", "first_name": "B",
                "blocked": False, "history": []}
        state = {"memory": {"leads": [alpha, beta]},
                 "flow_bots": [{"id": "alpha", "token": "111:alpha"},
                               {"id": "beta", "token": "222:beta"}],
                 "publish": {}}
        return state, alpha, beta

    async def test_block_targets_only_selected_bot(self):
        state, alpha, beta = self.make_state()
        self.mock_published = state
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=self.committed_block)):
            result = await app_main.handle_lead_action(
                app_main.LeadActionRequest(user_id="42", bot_id="beta", action="toggle_block")
            )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(alpha["blocked"])
        self.assertTrue(app_main._client_map(state)[("222","42",False)]["blocked"])

    async def test_ambiguous_user_without_bot_rejected(self):
        state, alpha, beta = self.make_state()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)):
            with self.assertRaises(app_main.HTTPException) as caught:
                await app_main.handle_lead_action(
                    app_main.LeadActionRequest(user_id="42", action="toggle_block")
                )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(alpha["blocked"])
        self.assertFalse(beta["blocked"])

    async def test_message_uses_selected_bot_and_history(self):
        state, alpha, beta = self.make_state()
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)), \
             patch.object(app_main, "_record_crm_message", new=AsyncMock(return_value=True)) as record_message, \
             patch.object(tg_srv, "_create_bot_instance", return_value=bot) as make_bot:
            result = await app_main.handle_lead_action(
                app_main.LeadActionRequest(user_id="42", bot_id="beta",
                                               action="send_message", message="Mensagem de suporte")
            )
        self.assertEqual(result["status"], "ok")
        make_bot.assert_called_once_with("222:beta")
        bot.send_message.assert_awaited_once()
        self.assertEqual(alpha["history"], [])
        record_message.assert_awaited_once_with("222", "42", "beta", "", "Mensagem de suporte")
        self.assertEqual(beta["history"], [])

    async def test_replaced_config_token_does_not_send_via_new_bot(self):
        state, _, beta = self.make_state()
        state["flow_bots"][1]["token"] = "333:replacement"
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance") as make_bot:
            result = await app_main.handle_lead_action(
                app_main.LeadActionRequest(user_id="42", bot_id="beta",
                                               action="send_message", message="Oi")
            )
        self.assertEqual(result["status"], "error")
        make_bot.assert_not_called()
        self.assertEqual(beta["history"], [])

    async def test_legacy_lead_requires_verified_purchase_origin_for_send(self):
        state, _, beta = self.make_state()
        beta.pop("origin_bot_numeric_id")
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance") as make_bot:
            result = await app_main.handle_lead_action(
                app_main.LeadActionRequest(user_id="42", bot_id="beta",
                                               action="send_message", message="Oi")
            )
        self.assertEqual(result["status"], "error")
        make_bot.assert_not_called()

    async def test_new_flow_lead_records_immutable_bot_identity(self):
        mgr = tg_srv.ActiveBotManager()
        state = {"flow_bots": [{"id": "beta", "token": "222:beta", "active": True, "mode": "flow"}],
                 "memory": {"leads": []}, "publish": {}, "learning": {}}
        lead = await mgr._get_or_create_lead(
            state, tg_srv.types.User(id=42, is_bot=False, first_name="Bia"), bot_token="222:beta"
        )
        self.assertEqual(lead["origin_bot_numeric_id"], "222")

    def test_redacted_tx_exposes_only_public_numeric_bot_id(self):
        state = {"tx_records": {"pix": {"bot_token": "222:beta"}}}
        result = app_main._redact_state_secrets(state)
        rec = result["tx_records"]["pix"]
        self.assertEqual(rec["origin_bot_numeric_id"], "222")
        self.assertNotEqual(rec["bot_token"], "222:beta")
        self.assertEqual(state["tx_records"]["pix"]["bot_token"], "222:beta")

    async def test_legacy_purchase_cannot_prove_origin_of_old_lead(self):
        state, _, beta = self.make_state()
        beta.pop("origin_bot_numeric_id")
        beta["history"].append({"role": "user", "content": "Conversa do bot anterior"})
        # A transaction sharing a user/configuration ID may belong to a replacement bot.
        state["tx_records"] = {"pix": {"user_id": "42", "bot_id": "beta", "bot_token": "222:beta"}}
        mgr = tg_srv.ActiveBotManager()
        lead = await mgr._get_or_create_lead(
            state, tg_srv.types.User(id=42, is_bot=False, first_name="Bia"), bot_token="222:beta"
        )
        self.assertIsNot(lead, beta)
        self.assertEqual(lead["origin_bot_numeric_id"], "222")
        self.assertEqual(beta["history"][0]["content"], "Conversa do bot anterior")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)), \
             patch.object(app_main, "_record_crm_message", new=AsyncMock(return_value=True)) as record_message, \
             patch.object(tg_srv, "_create_bot_instance", return_value=bot) as make_bot:
            result = await app_main.handle_lead_action(app_main.LeadActionRequest(
                user_id="42", bot_id="beta", origin_bot_numeric_id="222",
                action="send_message", message="Oi"
            ))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(beta["history"][0]["content"], "Conversa do bot anterior")
        make_bot.assert_called_once()

    async def test_rotated_config_can_select_new_origin_without_touching_old_lead(self):
        state, _, beta = self.make_state()
        new_lead = {"telegram_id": "42", "bot_id": "beta", "origin_bot_numeric_id": "333",
                    "blocked": False, "history": []}
        state["memory"]["leads"].append(new_lead)
        state["flow_bots"][1]["token"] = "333:replacement"
        self.mock_published = state
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=self.committed_block)):
            await app_main.handle_lead_action(app_main.LeadActionRequest(
                user_id="42", bot_id="beta", origin_bot_numeric_id="333", action="toggle_block"
            ))
        self.assertTrue(app_main._client_map(state)[("333","42",False)]["blocked"])
        self.assertFalse(beta["blocked"])

    async def test_legacy_lead_purchase_cannot_authorize_send(self):
        state, _, beta = self.make_state()
        beta.pop("origin_bot_numeric_id")
        state["tx_records"] = {"pix": {"user_id": "42", "bot_id": "beta", "bot_token": "222:beta"}}
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance") as make_bot:
            result = await app_main.handle_lead_action(app_main.LeadActionRequest(
                user_id="42", bot_id="beta", action="send_message", message="Oi"
            ))
        self.assertEqual(result["status"], "error")
        make_bot.assert_not_called()

    async def test_legacy_lead_with_unverified_origin_is_not_reused_on_new_bot(self):
        state, _, beta = self.make_state()
        beta.pop("origin_bot_numeric_id")
        state["flow_bots"][1]["token"] = "333:replacement"
        mgr = tg_srv.ActiveBotManager()
        new_lead = await mgr._get_or_create_lead(
            state, tg_srv.types.User(id=42, is_bot=False, first_name="Nova"), bot_token="333:replacement"
        )
        self.assertIsNot(new_lead, beta)
        self.assertEqual(new_lead["origin_bot_numeric_id"], "333")
        self.assertEqual(beta.get("origin_bot_numeric_id"), None)

    async def test_remarketing_does_not_fallback_to_wrong_bot(self):
        state, alpha, beta = self.make_state()
        state["remarketing_campaigns"] = [{"id": "camp", "bot_id": "beta", "segment": "all",
                                            "message": "Oferta", "include_plans": False}]
        alpha["bot_id"] = ""  # unknown origin must not be put into beta campaign
        beta["origin_bot_numeric_id"] = "999"  # stored origin conflicts with beta config
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)), \
             patch.object(app_main, "_project_remarketing_deliveries", new=AsyncMock()), \
             patch.object(tg_srv, "_create_bot_instance") as make_bot:
            result = await app_main.send_remarketing_campaign(
                app_main.RemarketingSendRequest(campaign_id="camp")
            )
        self.assertEqual(result["sent_count"], 0)
        make_bot.assert_not_called()
        self.assertEqual(alpha["history"], [])
        self.assertEqual(beta["history"], [])

    async def test_remarketing_sent_only_by_verified_lead_origin(self):
        state, alpha, beta = self.make_state()
        state["remarketing_campaigns"] = [{"id": "camp", "bot_id": "all", "segment": "all",
                                            "message": "Oferta", "include_plans": False}]
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session.close = AsyncMock()
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(app_main, "_write_state_to_db", new=AsyncMock(side_effect=lambda s, **kw: s)), \
             patch.object(app_main, "_claim_remarketing_recipient", new=AsyncMock(return_value=True)), \
             patch.object(app_main, "_mark_remarketing_recipient", new=AsyncMock()), \
             patch.object(app_main, "_project_remarketing_deliveries", new=AsyncMock()), \
             patch.object(tg_srv, "_create_bot_instance", return_value=bot) as make_bot:
            result = await app_main.send_remarketing_campaign(
                app_main.RemarketingSendRequest(campaign_id="camp")
            )
        self.assertEqual(result["sent_count"], 2)
        self.assertEqual({c.args[0] for c in make_bot.call_args_list}, {"111:alpha", "222:beta"})
        self.assertEqual(len(alpha["history"]), 1)
        self.assertEqual(len(beta["history"]), 1)

    async def test_paid_pix_updates_only_its_original_lead_after_bot_reuse(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                newer = {"telegram_id": "42", "bot_id": "slot", "origin_bot_numeric_id": "222",
                         "stage": "Conversando", "history": []}
                original = {"telegram_id": "42", "bot_id": "slot", "origin_bot_numeric_id": "111",
                            "stage": "Conversando", "history": []}
                state = {
                    "memory": {"leads": [newer] if archived else [newer, original],
                               "deleted_leads": [original] if archived else []},
                    "flow_bots": [{"id": "slot", "bot_id": "111", "token": "111:original",
                                   "active": True, "mode": "flow", "plans": [], "payments": {}}],
                    "tx_records": {"pix-original": {"tx_id": "pix-original", "user_id": 42,
                                    "bot_id": "slot", "bot_token": "111:original", "mode": "flow",
                                    "amount": 20.0, "status": "pending", "delivered": False,
                                    "delivery_dest_type": "Link externo",
                                    "delivery_external_url": "https://example.org/access"}},
                    "publish": {"support_username": "@support"}, "learning": {},
                }
                mgr = tg_srv.ActiveBotManager()
                bot = MagicMock()
                bot.token = "111:original"
                bot.send_message = AsyncMock()
                mgr.extra_bots["111:original"] = bot
                mgr.state_getter = AsyncMock(return_value=state)
                mgr.state_saver = AsyncMock(side_effect=lambda s, **kw: s)
                mgr._send_flow_medias = AsyncMock()
                result = await mgr.deliver_purchase_and_upsell(42, tx_id="pix-original")
                self.assertTrue(result)
                self.assertEqual(newer["history"], [])
                self.assertEqual(newer["stage"], "Conversando")
                self.assertNotEqual(original["stage"], "Conversando")

    async def test_unassigned_origin_cannot_guess_primary_bot(self):
        state, _, _ = self.make_state()
        state["memory"]["leads"] = [{"telegram_id": "99", "bot_id": "", "history": []}]
        with patch.object(app_main, "_read_state_from_db", new=AsyncMock(return_value=state)), \
             patch.object(tg_srv, "_create_bot_instance") as make_bot:
            result = await app_main.handle_lead_action(
                app_main.LeadActionRequest(user_id="99", action="send_message", message="Oi")
            )
        self.assertEqual(result["status"], "error")
        make_bot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
