import asyncio
import importlib
import json
import os
import re
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _install_test_stubs():
    """Instala stubs leves em sys.modules caso o ambiente local não tenha FastAPI/Aiogram instalados."""
    try:
        import fastapi  # noqa: F401
        import aiogram  # noqa: F401
        import sqlalchemy  # noqa: F401
        import httpx  # noqa: F401
        return
    except ImportError:
        pass

    # Stub pydantic
    pydantic_mod = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    pydantic_mod.BaseModel = BaseModel
    sys.modules["pydantic"] = pydantic_mod

    # Stub fastapi
    fastapi_mod = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Depends:
        def __init__(self, dependency=None):
            self.dependency = dependency

    class Header:
        def __init__(self, default=None, alias=None):
            self.default = default
            self.alias = alias

    class Request:
        def __init__(self, headers=None):
            self.headers = headers or {}

    class FastAPI:
        def __init__(self, **kwargs):
            self.routes = {}
            self.route_dependencies = {}
            self.dependencies = kwargs.get("dependencies", [])

        def add_middleware(self, *args, **kwargs):
            pass

        def mount(self, *args, **kwargs):
            pass

        def on_event(self, event_name):
            def decorator(fn):
                return fn
            return decorator

        def _register(self, method, path, dependencies=None):
            def decorator(fn):
                self.routes[(method, path)] = fn
                self.route_dependencies[(method, path)] = list(dependencies or [])
                return fn
            return decorator

        def get(self, path, dependencies=None, **kwargs):
            return self._register("GET", path, dependencies)

        def post(self, path, dependencies=None, **kwargs):
            return self._register("POST", path, dependencies)

        def delete(self, path, dependencies=None, **kwargs):
            return self._register("DELETE", path, dependencies)

    fastapi_mod.FastAPI = FastAPI
    fastapi_mod.Request = Request
    fastapi_mod.HTTPException = HTTPException
    fastapi_mod.Depends = Depends
    fastapi_mod.Header = Header
    sys.modules["fastapi"] = fastapi_mod

    cors_mod = types.ModuleType("fastapi.middleware.cors")
    cors_mod.CORSMiddleware = object
    sys.modules["fastapi.middleware.cors"] = cors_mod

    static_mod = types.ModuleType("fastapi.staticfiles")
    static_mod.StaticFiles = MagicMock()
    sys.modules["fastapi.staticfiles"] = static_mod

    resp_mod = types.ModuleType("fastapi.responses")
    resp_mod.RedirectResponse = MagicMock()
    resp_mod.JSONResponse = MagicMock()
    sys.modules["fastapi.responses"] = resp_mod

    # Stub sqlalchemy
    sa_mod = types.ModuleType("sqlalchemy")
    sa_mod.Column = MagicMock()
    sa_mod.Integer = MagicMock()
    sa_mod.String = MagicMock()
    sa_mod.Boolean = MagicMock()
    sa_mod.Float = MagicMock()
    sa_mod.Text = MagicMock()
    sa_mod.DateTime = MagicMock()
    sa_mod.ForeignKey = MagicMock()

    class _SelectStub:
        def where(self, *args, **kwargs):
            return self

    sa_mod.select = lambda *args, **kwargs: _SelectStub()
    sys.modules["sqlalchemy"] = sa_mod

    sa_ext_mod = types.ModuleType("sqlalchemy.ext")
    sa_async_mod = types.ModuleType("sqlalchemy.ext.asyncio")
    sa_async_mod.create_async_engine = MagicMock()
    sa_async_mod.AsyncSession = object
    sys.modules["sqlalchemy.ext"] = sa_ext_mod
    sys.modules["sqlalchemy.ext.asyncio"] = sa_async_mod

    class _AsyncSessionStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, *args, **kwargs):
            res = MagicMock()
            res.scalars.return_value.first.return_value = None
            return res

        def add(self, *args, **kwargs):
            pass

        async def commit(self):
            pass

    sa_orm_mod = types.ModuleType("sqlalchemy.orm")
    sa_orm_mod.sessionmaker = lambda *a, **kw: (lambda: _AsyncSessionStub())
    sa_orm_mod.declarative_base = lambda: type("Base", (), {"metadata": MagicMock()})
    sa_orm_mod.relationship = MagicMock()
    sys.modules["sqlalchemy.orm"] = sa_orm_mod

    # Stub httpx
    httpx_mod = types.ModuleType("httpx")
    httpx_mod.AsyncClient = MagicMock()
    sys.modules["httpx"] = httpx_mod

    # Stub aiogram
    aiogram_mod = types.ModuleType("aiogram")

    class DummyBot:
        def __init__(self, token=""):
            self.token = token
            self.session = MagicMock()
            self.session.close = AsyncMock()
            self.send_message = AsyncMock()
            self.send_photo = AsyncMock()
            self.send_video = AsyncMock()
            self.send_chat_action = AsyncMock()
            self.create_chat_invite_link = AsyncMock(
                side_effect=lambda chat_id, **kw: types.SimpleNamespace(invite_link=f"https://t.me/+invite_{chat_id}")
            )

    class DummyDispatcher:
        def __init__(self):
            self.message_handlers = []
            self.callback_handlers = []

        def message(self, *args, **kwargs):
            def dec(fn):
                self.message_handlers.append(fn)
                return fn
            return dec

        def callback_query(self, *args, **kwargs):
            def dec(fn):
                self.callback_handlers.append(fn)
                return fn
            return dec

    class _FStub:
        def __getattr__(self, name):
            return self

        def startswith(self, prefix):
            return prefix

        def __or__(self, other):
            return self

    class User:
        def __init__(self, id=1, is_bot=False, first_name="Lead", username="leaduser"):
            self.id = id
            self.is_bot = is_bot
            self.first_name = first_name
            self.username = username

    types_mod = types.ModuleType("aiogram.types")
    types_mod.User = User
    types_mod.Update = object
    types_mod.Message = object
    types_mod.CallbackQuery = object

    class InlineKeyboardButton:
        def __init__(self, text="", callback_data=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard=None):
            self.inline_keyboard = inline_keyboard or []

    types_mod.InlineKeyboardButton = InlineKeyboardButton
    types_mod.InlineKeyboardMarkup = InlineKeyboardMarkup
    types_mod.URLInputFile = lambda url: url
    types_mod.BufferedInputFile = lambda b, filename="": (b, filename)

    filters_mod = types.ModuleType("aiogram.filters")
    filters_mod.CommandStart = lambda: "start"

    aiogram_mod.Bot = DummyBot
    aiogram_mod.Dispatcher = DummyDispatcher
    aiogram_mod.types = types_mod
    aiogram_mod.F = _FStub()

    sys.modules["aiogram"] = aiogram_mod
    sys.modules["aiogram.types"] = types_mod
    sys.modules["aiogram.filters"] = filters_mod


_install_test_stubs()
os.environ.setdefault("ADMIN_TOKEN", "mock-admin-token-for-tests")

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRONTEND_HTML = os.path.abspath(os.path.join(BACKEND_DIR, "..", "frontend", "public", "index.html"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main as app_main  # noqa: E402
import telegram_service as tg_srv  # noqa: E402
import ai_engine  # noqa: E402


class Stage1SecurityTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_state = json.loads(json.dumps(app_main.DEFAULT_SHARKBOT_STATE))
        self.mock_state["publish"] = {
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
        self.mock_state["account"] = {
            "balance": 0,
            "selected_recharge": 100,
            "ai_provider": "deepseek",
            "ai_api_key": "sk-mock-ai-key",
        }
        self.mock_state["business"]["offers"] = [
            {
                "id": "offer_vip_1",
                "visible": True,
                "name": "VIP Completo",
                "price": 49.90,
                "duration": 30,
                "emoji": "💎",
            }
        ]
        self.mock_state["flow_bots"] = [
            {
                "id": "bot_flow_1",
                "username": "@mock_flow_bot",
                "bot_id": "123456",
                "first_name": "MockBot",
                "token": "123456:MOCK_BOT_TOKEN_FOR_TEST",
                "active": True,
                "mode": "flow",
                "sales_today": 0,
                "revenue_today": 0.0,
                "welcome": {"message": "Bem-vindo", "medias": []},
                "plans": [
                    {
                        "id": "plan_basic",
                        "name": "Plano Básico",
                        "price": 19.90,
                        "delivery_dest_type": "Canal ou grupo",
                        "delivery_channel_id": "-100111111",
                    },
                    {
                        "id": "plan_premium",
                        "name": "Plano Premium",
                        "price": 99.90,
                        "delivery_dest_type": "Canal ou grupo",
                        "delivery_channel_id": "-100999999",
                    },
                ],
                "fallback_delivery": {
                    "dest_type": "Canal ou grupo",
                    "channel_id": "-100111111",
                },
                "downsell_general": {"enabled": False, "sequences": []},
                "downsell_pix": {"enabled": False, "sequences": []},
                "order_bumps": [],
                "payments": {},
            }
        ]

    async def _setup_manager(self):
        mgr = tg_srv.ActiveBotManager()
        bot = MagicMock()
        bot.token = "123456:MOCK"
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_chat_action = AsyncMock()
        bot.create_chat_invite_link = AsyncMock(
            side_effect=lambda chat_id, **kw: types.SimpleNamespace(invite_link=f"https://t.me/+invite_{chat_id}")
        )
        mgr.bot = bot
        mgr.dp = tg_srv.Dispatcher()

        async def _getter():
            return self.mock_state

        async def _saver(st):
            self.mock_state = st
            return st

        mgr.state_getter = _getter
        mgr.state_saver = _saver
        mgr._register_handlers()
        return mgr

    async def test_1_no_hardcoded_secrets_and_api_redacts_credentials(self):
        """1. Sem _SEED_CONFIG_B64 no código e /api/state não deve vazar tokens/chaves em texto puro."""
        with open(os.path.join(BACKEND_DIR, "main.py"), "r", encoding="utf-8") as f:
            main_src = f.read()

        self.assertNotIn("_SEED_CONFIG_B64", main_src, "main.py ainda contém _SEED_CONFIG_B64 com credenciais embutidas")

        # Verifica que o estado padrão em memória não traz credenciais preenchidas sem variáveis de ambiente
        default_pub = app_main.DEFAULT_SHARKBOT_STATE.get("publish", {})
        default_acc = app_main.DEFAULT_SHARKBOT_STATE.get("account", {})
        if not os.getenv("BOT_TOKEN"):
            self.assertEqual(default_pub.get("bot_token", ""), "")
        if not os.getenv("GATEWAY_TOKEN"):
            self.assertEqual(default_pub.get("pushin_pay_token", ""), "")
        if not os.getenv("GATEWAY_SECRET"):
            self.assertEqual(default_pub.get("gateway_secret", ""), "")
        if not os.getenv("AI_API_KEY"):
            self.assertEqual(default_acc.get("ai_api_key", ""), "")

        # Verifica que a resposta de get_full_state mascara segredos
        with patch.object(app_main, "_read_state_from_db", AsyncMock(return_value=self.mock_state)):
            public_state = await app_main.get_full_state()
            pub_out = public_state.get("publish", {})
            acc_out = public_state.get("account", {})
            self.assertNotEqual(pub_out.get("bot_token"), "123456:MOCK_BOT_TOKEN_FOR_TEST", "bot_token vazou na API")
            self.assertNotEqual(pub_out.get("pushin_pay_token"), "mock-client-id", "pushin_pay_token vazou na API")
            self.assertNotEqual(pub_out.get("gateway_secret"), "mock-client-secret", "gateway_secret vazou na API")
            self.assertNotEqual(acc_out.get("ai_api_key"), "sk-mock-ai-key", "ai_api_key vazou na API")

    async def test_2_admin_routes_require_authentication(self):
        """2. Rotas administrativas devem exigir autenticação."""
        self.assertTrue(
            hasattr(app_main, "verify_admin_auth"),
            "main.py não define dependência de autenticação verify_admin_auth para proteger as rotas administrativas"
        )
        with self.assertRaises(app_main.HTTPException) as ctx:
            await app_main.verify_admin_auth(authorization=None, x_admin_token=None)
        self.assertIn(ctx.exception.status_code, (401, 403))

    async def test_3_webhook_rejects_unverified_forged_paid_payload(self):
        """3. Um POST forjado no webhook com status='paid' não pode liberar acesso sem confirmação no gateway."""
        mgr = await self._setup_manager()
        self.mock_state["memory"] = {
            "leads": [
                {
                    "telegram_id": "777",
                    "first_name": "Tester",
                    "stage": "Gerou checkout",
                    "last_tx_id": "tx_forged_999",
                }
            ]
        }

        with patch.object(app_main, "bot_manager", mgr), \
             patch.object(app_main, "_read_state_from_db", AsyncMock(return_value=self.mock_state)), \
             patch.object(app_main.PushinPayClient, "check_transaction_status", AsyncMock(return_value={"status": "pending"})):
            res = await app_main.webhook_gateway({"id": "tx_forged_999", "status": "paid"})
            self.assertNotEqual(res.get("action"), "approved", "Webhook aceitou pagamento forjado sem confirmar no gateway!")
            self.assertEqual(mgr.bot.send_message.call_count, 0, "Bot entregou produto para webhook não confirmado!")

    async def test_4_fail_closed_on_gateway_error_or_simulated_tx(self):
        """4. Falha no gateway ou transação sim_ nunca deve gerar PIX falso nem aprovar pagamento grátis."""
        mgr = await self._setup_manager()

        # Encontra os handlers registrados para buy: e checkpix:
        buy_handler = mgr.dp.callback_handlers[0]
        check_handler = mgr.dp.callback_handlers[1]

        user = tg_srv.types.User(id=888, is_bot=False, first_name="Visitante")
        msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=888))

        # Parte A: Se o gateway falhar na criação do PIX, não pode enviar código PIX sim_
        cb_buy = types.SimpleNamespace(
            data="buy:offer_vip_1:49.90",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=RuntimeError("Gateway offline"))):
            await buy_handler(cb_buy)
            lead = self.mock_state["memory"]["leads"][0]
            self.assertFalse(
                str(lead.get("last_tx_id", "")).startswith("sim_"),
                "Criou transação sim_ mesmo quando o gateway falhou!"
            )

        # Parte B: Clicar em verificar pagamento com erro de rede ou id sim_ nunca deve entregar o produto
        cb_check = types.SimpleNamespace(
            data="checkpix:sim_123456",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        mgr.deliver_purchase_and_upsell = AsyncMock()
        with patch.object(tg_srv.PushinPayClient, "check_transaction_status", AsyncMock(side_effect=RuntimeError("Timeout"))):
            await check_handler(cb_check)
            mgr.deliver_purchase_and_upsell.assert_not_called()

    async def test_5_server_side_price_discount_and_immutable_tx_binding(self):
        """5. Preço/desconto adulterados no callback devem ser ignorados e cada entrega deve ser vinculada ao tx_id pago."""
        mgr = await self._setup_manager()
        buy_handler = mgr.dp.callback_handlers[0]
        fplan_handler = mgr.dp.callback_handlers[2]

        user = tg_srv.types.User(id=999, is_bot=False, first_name="Comprador")
        msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=999))

        # Parte A: Adulteração de preço em buy:offer_vip_1:1.00 (preço real no servidor = 49.90 -> 4990 centavos)
        captured_cents = []

        async def _mock_create_pix(value_in_cents, **kwargs):
            captured_cents.append(value_in_cents)
            return {"id": f"tx_real_{len(captured_cents)}", "qr_code": "000201PIXREAL"}

        cb_tampered_buy = types.SimpleNamespace(
            data="buy:offer_vip_1:1.00",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=_mock_create_pix)):
            await buy_handler(cb_tampered_buy)
            self.assertEqual(captured_cents[-1], 4990, "O callback buy: aceitou preço adulterado pelo cliente (100 centavos em vez de 4990)!")

        # Parte B: Adulteração de desconto em fplan:plan_premium:90 sem downsell autorizado (preço real = 99.90 -> 9990 centavos)
        cb_tampered_fplan = types.SimpleNamespace(
            data="fplan:plan_premium:90",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=_mock_create_pix)):
            await fplan_handler(cb_tampered_fplan)
            self.assertEqual(captured_cents[-1], 9990, "O callback fplan: aceitou desconto de 90% adulterado no callback_data!")

        # Parte C: Vinculação imutável por transação (gera Checkout A básico, depois gera Checkout B premium, e paga Checkout A)
        cb_plan_a = types.SimpleNamespace(
            data="fplan:plan_basic:0",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        cb_plan_b = types.SimpleNamespace(
            data="fplan:plan_premium:0",
            from_user=user,
            message=msg,
            answer=AsyncMock(),
        )
        with patch.object(tg_srv.PushinPayClient, "create_pix_charge", AsyncMock(side_effect=[
            {"id": "tx_basic_A", "qr_code": "000201PIX_A"},
            {"id": "tx_premium_B", "qr_code": "000201PIX_B"},
        ])):
            await fplan_handler(cb_plan_a)
            await fplan_handler(cb_plan_b)

        # Agora confirma o pagamento da cobrança ANTIGA (tx_basic_A -> canal -100111111, R$ 19,90)
        mgr.bot.create_chat_invite_link.reset_mock()
        await mgr.notify_payment_by_tx_id("tx_basic_A")
        mgr.bot.create_chat_invite_link.assert_called_once()
        called_chat_id = mgr.bot.create_chat_invite_link.call_args.kwargs.get("chat_id")
        self.assertEqual(
            called_chat_id,
            "-100111111",
            f"Pagou tx_basic_A (-100111111), mas entregou o canal do checkout mais recente ({called_chat_id})!"
        )

    def test_6_xss_escaped_in_leads_panel_and_bargain_rules(self):
        """6. Painel de leads não deve interpolar first_name, username ou h.content sem escapeHtml."""
        with open(FRONTEND_HTML, "r", encoding="utf-8") as f:
            html_src = f.read()

        self.assertNotIn(
            "${l.first_name}",
            html_src,
            "XSS: ${l.first_name} inserido diretamente em innerHTML sem escapeHtml()"
        )
        self.assertNotIn(
            "${lead.first_name}",
            html_src,
            "XSS: ${lead.first_name} inserido diretamente em innerHTML sem escapeHtml()"
        )
        self.assertNotIn(
            "${h.content}",
            html_src,
            "XSS: ${h.content} inserido diretamente em innerHTML sem escapeHtml()"
        )

        # Verifica também regra de piso de desconto + teto percentual e once_per_visitor no fallback da IA
        offers = [{"id": "o1", "name": "VIP", "price": 100.0, "visible": True}]
        bargain = {"enabled": True, "max_discount_pct": 20, "min_price": 50.0, "once_per_visitor": True}
        eff = ai_engine._compute_effective_min_price(offers, bargain)
        self.assertGreaterEqual(eff, 80.0, "O cálculo de preço mínimo ignorou o teto máximo de desconto de 20%!")


if __name__ == "__main__":
    unittest.main()
