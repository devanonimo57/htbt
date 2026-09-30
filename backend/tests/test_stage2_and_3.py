import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from test_stage1_security import _install_test_stubs

_install_test_stubs()
os.environ.setdefault("ADMIN_TOKEN", "mock-admin-token-for-tests")

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRONTEND_HTML = os.path.abspath(os.path.join(BACKEND_DIR, "..", "frontend", "public", "index.html"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main as app_main  # noqa: E402
import telegram_service as tg_srv  # noqa: E402


class Stage2And3Tests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.server_state = json.loads(json.dumps(app_main.DEFAULT_SHARKBOT_STATE))
        self.server_state["publish"] = {
            "is_live": True,
            "bot_token": "123456:MOCK_BOT_TOKEN_FOR_TEST",
            "bot_username": "@mock_test_bot",
            "delivery_type": "Link de acesso",
            "delivery_destination": "-100555000",
            "support_username": "@suporte_mock",
            "gateway_provider": "syncpay",
            "pushin_pay_token": "mock-client-id",
            "gateway_secret": "mock-client-secret",
        }
        self.server_state["business"]["offers"] = [
            {
                "id": "offer_vip_1",
                "visible": True,
                "name": "VIP Completo",
                "price": 50.00,
                "duration": 30,
                "emoji": "💎",
            }
        ]
        self.server_state["business"]["bargain"] = {
            "enabled": True,
            "max_discount_pct": 40,
            "min_price": 30.00,
            "once_per_visitor": True,
            "validity_minutes": 15,
        }
        self.server_state["flow_bots"] = [
            {
                "id": "bot_flow_1",
                "username": "@mock_flow_bot",
                "bot_id": "123456",
                "first_name": "MockBot",
                "token": "123456:MOCK_BOT_TOKEN_FOR_TEST",
                "active": True,
                "mode": "ai",
                "sales_today": 3,
                "revenue_today": 150.0,
                "welcome": {"message": "Bem-vindo", "medias": []},
                "plans": [
                    {
                        "id": "plan_basic",
                        "name": "Plano Básico",
                        "price": 19.90,
                        "delivery_dest_type": "Canal ou grupo",
                        "delivery_channel_id": "-100111111",
                    }
                ],
                "fallback_delivery": {"dest_type": "Canal ou grupo", "channel_id": "-100111111"},
                "downsell_general": {"enabled": False, "sequences": []},
                "downsell_pix": {"enabled": False, "sequences": []},
                "order_bumps": [],
                "payments": {},
            }
        ]
        self.server_state["memory"] = {
            "leads": [
                {
                    "telegram_id": "1001",
                    "first_name": "Cliente Ativo",
                    "username": "@cliente_ativo",
                    "stage": "Gerou checkout",
                    "stage_history": ["Conversando", "Viu os planos", "Gerou checkout"],
                    "history": [{"role": "user", "content": "quero comprar"}],
                }
            ]
        }
        self.server_state["tx_records"] = {
            "tx_live_1001": {
                "tx_id": "tx_live_1001",
                "user_id": 1001,
                "mode": "ai",
                "offer_id": "offer_vip_1",
                "offer_name": "VIP Completo",
                "amount": 50.0,
                "cents": 5000,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
            }
        }
        self.server_state["learning"] = {
            "evaluated_results": 1,
            "confirmed_purchases": 3,
            "unpaid_checkouts": 1,
            "conversations_without_purchase": 1,
            "common_objections": [],
            "top_plans": [{"name": "VIP Completo", "count": 3}],
        }

    async def _setup_manager(self):
        mgr = tg_srv.ActiveBotManager()
        mgr.bot = tg_srv.Bot(token="123456:MOCK")
        mgr.dp = tg_srv.Dispatcher()

        async def _getter():
            return self.server_state

        async def _saver(st):
            self.server_state = st
            return st

        mgr.state_getter = _getter
        mgr.state_saver = _saver
        mgr._register_handlers()
        return mgr

    async def test_1_config_save_preserves_runtime_leads_tx_and_sales(self):
        """Etapa 2.1: Salvar configuração pelo painel nunca deve apagar leads, transações ou vendas acumulados no servidor."""
        stale_browser_payload = json.loads(json.dumps(self.server_state))
        # Simula navegador com snapshot antigo (sem leads, sem tx_records, sales_today=0) mas editando a bio da persona
        stale_browser_payload["persona"]["bio"] = "Nova bio editada no painel"
        stale_browser_payload["memory"] = {"leads": []}
        stale_browser_payload.pop("tx_records", None)
        stale_browser_payload["learning"] = {"evaluated_results": 0, "confirmed_purchases": 0, "unpaid_checkouts": 0}
        stale_browser_payload["flow_bots"][0]["sales_today"] = 0
        stale_browser_payload["flow_bots"][0]["revenue_today"] = 0.0

        app_main._STATE_CACHE = json.loads(json.dumps(self.server_state))
        with patch.object(app_main.bot_manager, "configure_and_start", AsyncMock(return_value={"status": "running"})):
            await app_main.save_full_state(stale_browser_payload)
            updated = app_main._STATE_CACHE

        self.assertEqual(updated["persona"]["bio"], "Nova bio editada no painel")
        self.assertEqual(
            len(updated.get("memory", {}).get("leads", [])),
            1,
            "Salvar configuração no painel apagou os leads em atendimento no servidor!"
        )
        self.assertIn(
            "tx_live_1001",
            updated.get("tx_records", {}),
            "Salvar configuração apagou tx_records pendentes!"
        )
        self.assertEqual(
            updated["flow_bots"][0]["sales_today"],
            3,
            "Salvar configuração zerou o contador sales_today do bot!"
        )
        self.assertEqual(
            updated["flow_bots"][0]["revenue_today"],
            150.0,
            "Salvar configuração zerou o contador revenue_today do bot!"
        )
        self.assertEqual(
            updated["learning"]["confirmed_purchases"],
            3,
            "Salvar configuração zerou métricas de learning!"
        )

    async def test_2_bargain_discount_validity_minutes_enforced(self):
        """Etapa 3.2: Desconto negociado deve respeitar validity_minutes e expirar após o prazo."""
        mgr = await self._setup_manager()
        buy_handler = mgr.dp.callback_handlers[0]
        user = tg_srv.types.User(id=2002, is_bot=False, first_name="PeBarganhas")
        msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=2002))

        lead = await mgr._get_or_create_lead(self.server_state, user)
        lead["authorized_discount_offer_id"] = "offer_vip_1"
        lead["authorized_discount_price"] = 35.00
        # Simula desconto concedido há 45 minutos (expirado para validity_minutes=15)
        lead["authorized_discount_expires_at"] = (datetime.utcnow() - timedelta(minutes=45)).isoformat()

        captured_cents = []

        async def _mock_create_pix(value_in_cents, **kwargs):
            captured_cents.append(value_in_cents)
            return {"id": "tx_after_expiry", "qr_code": "000201PIX"}

        cb_buy = types.SimpleNamespace(
            data="buy:offer_vip_1:35.00",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=_mock_create_pix)):
            await buy_handler(cb_buy)

        self.assertEqual(
            captured_cents[-1],
            5000,
            f"Aceitou desconto expirado (cobrou {captured_cents[-1]} centavos em vez do preço cheio 5000)!"
        )

        # Agora testa com desconto dentro do prazo de validade (+10 min no futuro)
        lead["authorized_discount_offer_id"] = "offer_vip_1"
        lead["authorized_discount_price"] = 35.00
        lead["authorized_discount_expires_at"] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=_mock_create_pix)):
            await buy_handler(cb_buy)

        self.assertEqual(
            captured_cents[-1],
            3500,
            f"Não aplicou o desconto válido dentro do prazo (cobrou {captured_cents[-1]} em vez de 3500)!"
        )

    async def test_3_lifecycle_tracks_and_cancels_pix_poll_and_upsell_tasks(self):
        """Etapa 2.3: ActiveBotManager deve rastrear e cancelar tasks de PIX polling e Upsell em stop() e cancel_lead_tasks()."""
        mgr = await self._setup_manager()
        self.server_state["upsell"] = {
            "enabled": True,
            "delay_minutes": 5,
            "offer_id": "offer_vip_1",
            "message": "Oferta extra pós-compra!",
            "medias": [],
        }

        # Verifica que deliver_purchase_and_upsell agenda upsell em background sem bloquear por 5 minutos
        start_ts = datetime.utcnow()
        await asyncio.wait_for(
            mgr.deliver_purchase_and_upsell(1001, tx_id="tx_live_1001"),
            timeout=1.0,
        )
        elapsed = (datetime.utcnow() - start_ts).total_seconds()
        self.assertLess(elapsed, 1.0, "deliver_purchase_and_upsell bloqueou esperando o delay do upsell!")
        self.assertTrue(
            hasattr(mgr, "upsell_tasks") and "1001" in mgr.upsell_tasks,
            "ActiveBotManager não rastreou a task de upsell em mgr.upsell_tasks!"
        )

        # Verifica que stop() cancela e limpa todas as tasks pendentes (downsell, pix_poll, upsell)
        await mgr.stop()
        self.assertEqual(len(getattr(mgr, "upsell_tasks", {})), 0)
        self.assertEqual(len(getattr(mgr, "pix_poll_tasks", {})), 0)
        self.assertEqual(len(mgr.downsell_tasks), 0)

    async def test_4_telegram_file_id_caching_avoids_reupload(self):
        """Etapa 3.3: Envio de mídia deve armazenar e reutilizar telegram_file_id nas próximas mensagens."""
        mgr = await self._setup_manager()
        media_item = {
            "id": "m_cache_1",
            "name": "Foto Cache",
            "type": "Fotos",
            "url": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
        }
        fake_photo_resp = types.SimpleNamespace(
            photo=[types.SimpleNamespace(file_id="AgACAgEAAxkB_CACHED_FILE_ID_123")]
        )
        mgr.bot.send_photo = AsyncMock(return_value=fake_photo_resp)

        await mgr._send_flow_medias(1001, [media_item])
        self.assertEqual(
            media_item.get("telegram_file_id"),
            "AgACAgEAAxkB_CACHED_FILE_ID_123",
            "Não salvou telegram_file_id no objeto de mídia após o primeiro envio!"
        )

        # No segundo envio, deve passar a string do file_id diretamente em vez de decodificar Base64 novamente
        await mgr._send_flow_medias(1002, [media_item])
        second_call_photo = mgr.bot.send_photo.call_args.kwargs.get("photo")
        self.assertEqual(
            second_call_photo,
            "AgACAgEAAxkB_CACHED_FILE_ID_123",
            "Segundo envio não reutilizou o telegram_file_id cacheado!"
        )

        # Quando há 2 ou 3 mídias (ex: 1 foto + 1 vídeo), deve enviar lado a lado via send_media_group (Álbum)
        video_item = {
            "id": "m_cache_2",
            "name": "0929(3).mp4",
            "type": "Vídeos",
            "url": "data:video/mp4;base64,AAAAIGZ0eXBpc29t",
        }
        fake_album_resp = [
            types.SimpleNamespace(photo=[types.SimpleNamespace(file_id="AgACAgEAAxkB_ALBUM_PHOTO_1")]),
            types.SimpleNamespace(video=types.SimpleNamespace(file_id="BAACAgEAAxkB_ALBUM_VIDEO_2")),
        ]
        mgr.bot.send_media_group = AsyncMock(return_value=fake_album_resp)
        await mgr._send_flow_medias(1003, [media_item, video_item])
        self.assertTrue(mgr.bot.send_media_group.called, "Não utilizou send_media_group para enviar 2 mídias lado a lado!")
        self.assertEqual(video_item.get("telegram_file_id"), "BAACAgEAAxkB_ALBUM_VIDEO_2")
        self.assertEqual(video_item.get("telegram_send_as"), "video")

    def test_5_frontend_hygiene_and_cors_configuration(self):
        """Etapa 2.1 & 3.1 & 3.4: Sem sobrescrita automática por localStorage, sem checkpix:ok morto no HTML e CORS restrito."""
        with open(FRONTEND_HTML, "r", encoding="utf-8") as f:
            html_src = f.read()

        self.assertNotIn(
            "checkpix:ok",
            html_src,
            "frontend/public/index.html ainda contém o handler morto inseguro checkpix:ok!"
        )
        self.assertNotIn(
            "handleBrowserTelegramUpdate",
            html_src,
            "frontend/public/index.html ainda contém o polling inseguro no navegador (handleBrowserTelegramUpdate)!"
        )

        with open(os.path.join(BACKEND_DIR, "main.py"), "r", encoding="utf-8") as f:
            main_src = f.read()

        self.assertNotIn(
            'allow_origins=["*"]',
            main_src,
            "backend/main.py ainda usa allow_origins=['*'] com allow_credentials=True!"
        )

    async def test_6_additional_audit_findings(self):
        """Verifica os 8 achados complementares: tx_record obrigatório, retry pós-falha de envio, Asaas pixQrCodeId, Pollinations opt-in, Docker context e XSS/Save no painel."""
        mgr = await self._setup_manager()

        # 1. checkpix e deliver_purchase_and_upsell devem rejeitar tx_id inexistente (tx_record=None)
        res_unregistered = await mgr.deliver_purchase_and_upsell(1001, tx_id="tx_unknown_999")
        self.assertFalse(res_unregistered, "deliver_purchase_and_upsell aceitou tx_id sem tx_record registrado!")

        # 2. Se o envio no Telegram falhar, o status não pode ficar 'paid' impedindo nova tentativa
        mgr.bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram network timeout"))
        with self.assertRaises(RuntimeError):
            await mgr.deliver_purchase_and_upsell(1001, tx_id="tx_live_1001")
        self.assertEqual(
            self.server_state["tx_records"]["tx_live_1001"]["status"],
            "pending",
            "Transação foi marcada como 'paid' antes de o envio no Telegram concluir com sucesso!"
        )

        # Na segunda tentativa com Telegram operacional, a entrega deve concluir e marcar 'paid'
        mgr.bot.send_message = AsyncMock()
        res_retry = await mgr.deliver_purchase_and_upsell(1001, tx_id="tx_live_1001")
        self.assertTrue(res_retry)
        self.assertEqual(self.server_state["tx_records"]["tx_live_1001"]["status"], "paid")

        # 3. Privacidade: sem chave de IA e sem opt-in, nunca deve chamar text.pollinations.ai
        import ai_engine
        self.server_state["account"]["ai_api_key"] = ""
        with patch.object(ai_engine, "_call_llm_endpoint", AsyncMock(return_value="resposta externa")) as mock_llm:
            await ai_engine.generate_ai_reply(self.server_state, [], "Oi, tudo bem?")
            self.assertEqual(mock_llm.call_count, 0, "Chamou text.pollinations.ai sem consentimento explícito!")

        # 4. Docker Compose context, XSS de planos e saveFullState no HTML
        compose_path = os.path.abspath(os.path.join(BACKEND_DIR, "..", "docker-compose.yml"))
        with open(compose_path, "r", encoding="utf-8") as f:
            compose_src = f.read()
        self.assertIn("context: .", compose_src)
        self.assertIn("dockerfile: ./backend/Dockerfile", compose_src)

        with open(FRONTEND_HTML, "r", encoding="utf-8") as f:
            html_src = f.read()
        self.assertIn("escapeHtml(p.name || 'Plano')", html_src)
        self.assertNotIn("document.getElementById('chk-bot-live').checked = true;", html_src.split("async function saveFullState()")[1].split("async function loadInitialState()")[0])

    async def test_7_multi_bot_concurrent_routing(self):
        """Múltiplos bots ativos em flow_bots devem rotear /start e configurações de forma independente."""
        from telegram_service import _CURRENT_BOT_TOKEN_CTX
        mgr = await self._setup_manager()
        self.server_state["flow_bots"] = [
            {
                "id": "bot_1",
                "bot_id": "84980111",
                "username": "@botteste33_bot",
                "token": "84980111:AA_TOKEN_BOT_1",
                "active": True,
                "mode": "flow",
                "welcome": {"message": "Boas-vindas Bot 1", "medias": []},
                "plans": [{"id": "p1", "name": "Plano 1", "price": 10.0}],
            },
            {
                "id": "bot_2",
                "bot_id": "73213222",
                "username": "@taxaaprovacao_bot",
                "token": "73213222:AA_TOKEN_BOT_2",
                "active": True,
                "mode": "flow",
                "welcome": {"message": "AAA", "medias": []},
                "plans": [{"id": "p2", "name": "Plano 2", "price": 20.0}],
            },
        ]

        t1 = _CURRENT_BOT_TOKEN_CTX.set("84980111:AA_TOKEN_BOT_1")
        try:
            fb1 = mgr._get_active_flow_bot(self.server_state)
            self.assertEqual(fb1.get("username"), "@botteste33_bot")
            self.assertEqual(fb1.get("welcome", {}).get("message"), "Boas-vindas Bot 1")
        finally:
            _CURRENT_BOT_TOKEN_CTX.reset(t1)

        t2 = _CURRENT_BOT_TOKEN_CTX.set("73213222:AA_TOKEN_BOT_2")
        try:
            fb2 = mgr._get_active_flow_bot(self.server_state)
            self.assertEqual(fb2.get("username"), "@taxaaprovacao_bot")
            self.assertEqual(fb2.get("welcome", {}).get("message"), "AAA")
        finally:
            _CURRENT_BOT_TOKEN_CTX.reset(t2)


if __name__ == "__main__":
    unittest.main()


