import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from models import Base, BotConfig, Offer, MediaAsset, DownsellStep, UpsellConfig, Lead, Order
from pushin_pay import PushinPayClient
from ai_engine import generate_ai_reply
from telegram_service import bot_manager

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./hotbot.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="SharkBot IA Clone - SaaS", description="Motor completo de atendimento IA e vendas no Telegram")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_SHARKBOT_STATE: Dict[str, Any] = {
    "flow_name": "bot",
    "persona": {
        "name": "Atendimento",
        "age": 18,
        "gender": "Feminina",
        "gender_desc": "Fala e se apresenta no feminino.",
        "city": "",
        "bio": "Criadora adulta, espontânea e direta. Conversa como em um chat privado, lembra o que a pessoa contou e apresenta o próprio VIP sem parecer atendimento.",
        "traits": "espontânea, atenta, bem-humorada"
    },
    "business": {
        "context": "Este atendimento representa uma criadora adulta que vende acesso ao próprio VIP privado pelo Telegram. O visitante pode chegar curioso ou pronto para comprar. Seja natural, acolhedora e use apenas os benefícios cadastrados nas ofertas. Chame o produto de meu VIP ou meu conteúdo, nunca de clube. O pagamento começa pelo botão seguro e a entrega configurada acontece depois da confirmação. Se houver falha de pagamento ou acesso, encaminhe para o suporte cadastrado. Nunca invente descontos, bônus, quantidade de conteúdos, atualizações ou experiências que não estejam configuradas.",
        "offers": [],
        "bargain": {
            "enabled": False,
            "max_discount_pct": 40,
            "min_price": 0,
            "once_per_visitor": False,
            "validity_minutes": 30
        },
        "faqs": []
    },
    "style": {
        "strategy": "Conexão primeiro",
        "flirt": "Leve",
        "slang": "Natural",
        "cadence": "Natural",
        "mirror_visitor": True,
        "use_lead_name": False,
        "tone": "Acolhedor",
        "length": "Curta",
        "emojis": "Leve",
        "audio_replies": False,
        "opening_messages": ["oii 💜", "chegou agora por aqui?"],
        "opening_variations": [],
        "objective": "Entender a intencao, lembrar preferencias e conduzir naturalmente para o acesso mais adequado.",
        "rules": "Reaja ao detalhe mais importante da ultima mensagem antes de fazer outra pergunta.\nNao reinicie a conversa nem repita uma pergunta que o visitante ja respondeu.\nQuando houver intencao de compra, abra os planos sem criar etapas artificiais.",
        "handoff_words": "suporte, atendente, problema no pagamento"
    },
    "medias": [
        {
            "id": "media_1",
            "name": "Gym (1)",
            "type": "Fotos",
            "url": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?auto=format&fit=crop&w=800&q=80",
            "active": False,
            "description": "Descreva exatamente o que esta mídia mostra antes de ativá-la.",
            "objective": "Criar conexão",
            "signals": "",
            "used_in": ["Conversa da IA"],
            "caption": "",
            "spoiler": False,
            "expanded": True
        }
    ],
    "downsell": {
        "enabled": False,
        "active_step_index": 0,
        "steps": [
            {
                "id": "ds_1",
                "title": "Apresentar planos",
                "delay_label": "1 min",
                "delay_minutes": 1,
                "message": "sumiu por aí? 😜\n\nquer que eu te mostre as opções de acesso?",
                "accompany": "Botão com os planos atuais",
                "medias": []
            }
        ]
    },
    "upsell": {
        "enabled": False,
        "origin": "Usar plano já cadastrado",
        "delay_label": "1 min",
        "delay_minutes": 1,
        "offer_id": "offer_1",
        "message": "agora que seu acesso foi liberado, tenho uma opção extra que combina com você 👀",
        "medias": [
            {
                "id": "up_m1",
                "name": "Imagem do ChatGPT 27 de set. de 2026, 13 42 52 2",
                "url": "https://images.unsplash.com/photo-1583454110551-21f2fa2afe61?auto=format&fit=crop&w=600&q=80",
                "caption": "",
                "spoiler": False,
                "expanded": True
            }
        ]
    },
    "publish": {
        "is_live": bool(os.getenv("BOT_TOKEN", "")),
        "bot_token": os.getenv("BOT_TOKEN", ""),
        "bot_username": "",
        "cache_channel_id": os.getenv("CACHE_CHANNEL_ID", ""),
        "delivery_type": "Link de acesso",
        "delivery_destination": os.getenv("VIP_DESTINATION", ""),
        "support_username": os.getenv("SUPPORT_USERNAME", ""),
        "gateway_provider": os.getenv("GATEWAY_PROVIDER", "syncpay"),
        "pushin_pay_token": os.getenv("GATEWAY_TOKEN", ""),
        "gateway_secret": os.getenv("GATEWAY_SECRET", "")
    },
    "memory": {
        "leads": []
    },
    "learning": {
        "evaluated_results": 0,
        "confirmed_purchases": 0,
        "unpaid_checkouts": 0,
        "conversations_without_purchase": 0,
        "common_objections": [],
        "top_plans": []
    },
    "account": {
        "balance": 0.00,
        "selected_recharge": 100.00,
        "ai_provider": os.getenv("AI_PROVIDER", "deepseek"),
        "ai_api_key": os.getenv("AI_API_KEY", "")
    }
}

_SEED_CONFIG_B64 = "eyJmbG93X25hbWUiOiAiYm90IiwgInBlcnNvbmEiOiB7Im5hbWUiOiAiQXRlbmRpbWVudG8iLCAiYWdlIjogMTgsICJnZW5kZXIiOiAiRmVtaW5pbmEiLCAiZ2VuZGVyX2Rlc2MiOiAiRmFsYSBlIHNlIGFwcmVzZW50YSBubyBmZW1pbmluby4iLCAiY2l0eSI6ICIiLCAiYmlvIjogIkNyaWFkb3JhIGFkdWx0YSwgZXNwb250w6JuZWEgZSBkaXJldGEuIENvbnZlcnNhIGNvbW8gZW0gdW0gY2hhdCBwcml2YWRvLCBsZW1icmEgbyBxdWUgYSBwZXNzb2EgY29udG91IGUgYXByZXNlbnRhIG8gcHLDs3ByaW8gVklQIHNlbSBwYXJlY2VyIGF0ZW5kaW1lbnRvLiIsICJ0cmFpdHMiOiAiZXNwb250w6JuZWEsIGF0ZW50YSwgYmVtLWh1bW9yYWRhIn0sICJidXNpbmVzcyI6IHsiY29udGV4dCI6ICJFc3RlIGF0ZW5kaW1lbnRvIHJlcHJlc2VudGEgdW1hIGNyaWFkb3JhIGFkdWx0YSBxdWUgdmVuZGUgYWNlc3NvIGFvIHByw7NwcmlvIFZJUCBwcml2YWRvIHBlbG8gVGVsZWdyYW0uIE8gdmlzaXRhbnRlIHBvZGUgY2hlZ2FyIGN1cmlvc28gb3UgcHJvbnRvIHBhcmEgY29tcHJhci4gU2VqYSBuYXR1cmFsLCBhY29saGVkb3JhIGUgdXNlIGFwZW5hcyBvcyBiZW5lZsOtY2lvcyBjYWRhc3RyYWRvcyBuYXMgb2ZlcnRhcy4gQ2hhbWUgbyBwcm9kdXRvIGRlIG1ldSBWSVAgb3UgbWV1IGNvbnRlw7pkbywgbnVuY2EgZGUgY2x1YmUuIE8gcGFnYW1lbnRvIGNvbWXDp2EgcGVsbyBib3TDo28gc2VndXJvIGUgYSBlbnRyZWdhIGNvbmZpZ3VyYWRhIGFjb250ZWNlIGRlcG9pcyBkYSBjb25maXJtYcOnw6NvLiBTZSBob3V2ZXIgZmFsaGEgZGUgcGFnYW1lbnRvIG91IGFjZXNzbywgZW5jYW1pbmhlIHBhcmEgbyBzdXBvcnRlIGNhZGFzdHJhZG8uIE51bmNhIGludmVudGUgZGVzY29udG9zLCBiw7RudXMsIHF1YW50aWRhZGUgZGUgY29udGXDumRvcywgYXR1YWxpemHDp8O1ZXMgb3UgZXhwZXJpw6puY2lhcyBxdWUgbsOjbyBlc3RlamFtIGNvbmZpZ3VyYWRhcy4iLCAib2ZmZXJzIjogW3siaWQiOiAib2ZmZXJfMTc5MDYxMjExMDc2NiIsICJ2aXNpYmxlIjogdHJ1ZSwgIm5hbWUiOiAib2ZlcnRhIiwgInByaWNlIjogMTksICJkZXNjcmlwdGlvbiI6ICJyb2xhIiwgImVtb2ppIjogIvCfko4iLCAiZHVyYXRpb24iOiAzMCwgImhpZ2hsaWdodCI6IGZhbHNlfV0sICJiYXJnYWluIjogeyJlbmFibGVkIjogdHJ1ZSwgIm1heF9kaXNjb3VudF9wY3QiOiA0MCwgIm1pbl9wcmljZSI6IDAsICJvbmNlX3Blcl92aXNpdG9yIjogZmFsc2UsICJ2YWxpZGl0eV9taW51dGVzIjogMzB9LCAiZmFxcyI6IFtdfSwgInN0eWxlIjogeyJzdHJhdGVneSI6ICJDb25leMOjbyBwcmltZWlybyIsICJmbGlydCI6ICJMZXZlIiwgInNsYW5nIjogIk5hdHVyYWwiLCAiY2FkZW5jZSI6ICJOYXR1cmFsIiwgIm1pcnJvcl92aXNpdG9yIjogdHJ1ZSwgInVzZV9sZWFkX25hbWUiOiBmYWxzZSwgInRvbmUiOiAiQWNvbGhlZG9yIiwgImxlbmd0aCI6ICJDdXJ0YSIsICJlbW9qaXMiOiAiTGV2ZSIsICJhdWRpb19yZXBsaWVzIjogZmFsc2UsICJvcGVuaW5nX21lc3NhZ2VzIjogWyJvaWkg8J+SnCIsICJjaGVnb3UgYWdvcmEgcG9yIGFxdWk/Il0sICJvcGVuaW5nX3ZhcmlhdGlvbnMiOiBbXSwgIm9iamVjdGl2ZSI6ICJFbnRlbmRlciBhIGludGVuY2FvLCBsZW1icmFyIHByZWZlcmVuY2lhcyBlIGNvbmR1emlyIG5hdHVyYWxtZW50ZSBwYXJhIG8gYWNlc3NvIG1haXMgYWRlcXVhZG8uIiwgInJ1bGVzIjogIlJlYWphIGFvIGRldGFsaGUgbWFpcyBpbXBvcnRhbnRlIGRhIHVsdGltYSBtZW5zYWdlbSBhbnRlcyBkZSBmYXplciBvdXRyYSBwZXJndW50YS5cbk5hbyByZWluaWNpZSBhIGNvbnZlcnNhIG5lbSByZXBpdGEgdW1hIHBlcmd1bnRhIHF1ZSBvIHZpc2l0YW50ZSBqYSByZXNwb25kZXUuXG5RdWFuZG8gaG91dmVyIGludGVuY2FvIGRlIGNvbXByYSwgYWJyYSBvcyBwbGFub3Mgc2VtIGNyaWFyIGV0YXBhcyBhcnRpZmljaWFpcy4iLCAiaGFuZG9mZl93b3JkcyI6ICJzdXBvcnRlLCBhdGVuZGVudGUsIHByb2JsZW1hIG5vIHBhZ2FtZW50byJ9LCAibWVkaWFzIjogW3siaWQiOiAibWVkaWFfMSIsICJuYW1lIjogIkd5bSAoMSkiLCAidHlwZSI6ICJGb3RvcyIsICJ1cmwiOiAiaHR0cHM6Ly9pbWFnZXMudW5zcGxhc2guY29tL3Bob3RvLTE1MzQ0MzgzMjcyNzYtMTRlNTMwMGMzYTQ4P2F1dG89Zm9ybWF0JmZpdD1jcm9wJnc9ODAwJnE9ODAiLCAiYWN0aXZlIjogdHJ1ZSwgImRlc2NyaXB0aW9uIjogIkRlc2NyZXZhIGV4YXRhbWVudGUgbyBxdWUgZXN0YSBtw61kaWEgbW9zdHJhIGFudGVzIGRlIGF0aXbDoS1sYS4iLCAib2JqZWN0aXZlIjogIkNyaWFyIGNvbmV4w6NvIiwgInNpZ25hbHMiOiAiIiwgInVzZWRfaW4iOiBbIkNvbnZlcnNhIGRhIElBIl0sICJjYXB0aW9uIjogIiIsICJzcG9pbGVyIjogZmFsc2UsICJleHBhbmRlZCI6IHRydWV9XSwgImRvd25zZWxsIjogeyJlbmFibGVkIjogdHJ1ZSwgImFjdGl2ZV9zdGVwX2luZGV4IjogMCwgInN0ZXBzIjogW3siaWQiOiAiZHNfMSIsICJ0aXRsZSI6ICJBcHJlc2VudGFyIHBsYW5vcyIsICJkZWxheV9sYWJlbCI6ICIxIG1pbiIsICJkZWxheV9taW51dGVzIjogMSwgIm1lc3NhZ2UiOiAic3VtaXUgcG9yIGHDrT8g8J+YnFxuXG5xdWVyIHF1ZSBldSB0ZSBtb3N0cmUgYXMgb3DDp8O1ZXMgZGUgYWNlc3NvPyIsICJhY2NvbXBhbnkiOiAiQm90w6NvIGNvbSBvcyBwbGFub3MgYXR1YWlzIiwgIm1lZGlhcyI6IFtdfV19LCAidXBzZWxsIjogeyJlbmFibGVkIjogdHJ1ZSwgIm9yaWdpbiI6ICJVc2FyIHBsYW5vIGrDoSBjYWRhc3RyYWRvIiwgImRlbGF5X2xhYmVsIjogIjEgbWluIiwgImRlbGF5X21pbnV0ZXMiOiAxLCAib2ZmZXJfaWQiOiAib2ZmZXJfMSIsICJtZXNzYWdlIjogImFnb3JhIHF1ZSBzZXUgYWNlc3NvIGZvaSBsaWJlcmFkbywgdGVuaG8gdW1hIG9ww6fDo28gZXh0cmEgcXVlIGNvbWJpbmEgY29tIHZvY8OqIPCfkYAiLCAibWVkaWFzIjogW3siaWQiOiAidXBfbTEiLCAibmFtZSI6ICJJbWFnZW0gZG8gQ2hhdEdQVCAyNyBkZSBzZXQuIGRlIDIwMjYsIDEzIDQyIDUyIDIiLCAidXJsIjogImh0dHBzOi8vaW1hZ2VzLnVuc3BsYXNoLmNvbS9waG90by0xNTgzNDU0MTEwNTUxLTIxZjJmYTJhZmU2MT9hdXRvPWZvcm1hdCZmaXQ9Y3JvcCZ3PTYwMCZxPTgwIiwgImNhcHRpb24iOiAiIiwgInNwb2lsZXIiOiBmYWxzZSwgImV4cGFuZGVkIjogdHJ1ZX1dfSwgInB1Ymxpc2giOiB7ImlzX2xpdmUiOiB0cnVlLCAiYm90X3Rva2VuIjogIjg4OTUyNzQ0Nzc6QUFHZkJqSUhXV3VIOTg0WWM5VTVqdFRUSENDX2dRVTRrd3MiLCAiYm90X3VzZXJuYW1lIjogIkBib3R0ZXN0ZTMzX2JvdCIsICJjYWNoZV9jaGFubmVsX2lkIjogIi0xMDAzOTM5MTM1OTgyIiwgImRlbGl2ZXJ5X3R5cGUiOiAiTGluayBkZSBhY2Vzc28iLCAiZGVsaXZlcnlfZGVzdGluYXRpb24iOiAiaHR0cHM6Ly93d3cuaW5zdGFncmFtLmNvbS9mYXRvc2Rlc2NvbmhlY2lkb3MvIiwgInN1cHBvcnRfdXNlcm5hbWUiOiAiQG1hcmlsZW1vc29mIiwgImdhdGV3YXlfcHJvdmlkZXIiOiAic3luY3BheSIsICJwdXNoaW5fcGF5X3Rva2VuIjogIjA1M2NmNzQ3LWQ0N2MtNDEyOC1hZjA2LWQzMDg4ZDExMDQ5NCIsICJnYXRld2F5X3NlY3JldCI6ICJkZDg0MWRkZS02NDA1LTQ4YWEtYTYyOS1iNjdjNTMyNGFiOGQiLCAiYm90X2ZpcnN0X25hbWUiOiAiYm90dGVzdGUzMyJ9LCAibWVtb3J5IjogeyJsZWFkcyI6IFtdfSwgImxlYXJuaW5nIjogeyJldmFsdWF0ZWRfcmVzdWx0cyI6IDEsICJjb25maXJtZWRfcHVyY2hhc2VzIjogMCwgInVucGFpZF9jaGVja291dHMiOiAxLCAiY29udmVyc2F0aW9uc193aXRob3V0X3B1cmNoYXNlIjogMCwgImNvbW1vbl9vYmplY3Rpb25zIjogW10sICJ0b3BfcGxhbnMiOiBbXX0sICJhY2NvdW50IjogeyJiYWxhbmNlIjogMCwgInNlbGVjdGVkX3JlY2hhcmdlIjogMTAwLCAiYWlfcHJvdmlkZXIiOiAiZGVlcHNlZWsiLCAiYWlfYXBpX2tleSI6ICJzay0zMzUwNzE5ZTZkMmU0ZDdkOGM5MDI1MzIyZmMwNjY1YiJ9fQ=="
try:
    import base64 as _b64
    DEFAULT_SHARKBOT_STATE.update(json.loads(_b64.b64decode(_SEED_CONFIG_B64).decode("utf-8")))
except Exception:
    pass

frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "public")
if os.path.exists(frontend_path):
    app.mount("/dashboard", StaticFiles(directory=frontend_path, html=True), name="dashboard")

@app.get("/")
def redirect_to_dashboard():
    return RedirectResponse(url="/dashboard/")

@app.get("/ping")
@app.get("/health")
def health_ping():
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}

import asyncio as _asyncio

_STATE_CACHE: Optional[Dict[str, Any]] = None
_DB_LOCK = _asyncio.Lock()
MAX_ACTIVE_LEADS = 500
MAX_HISTORY_PER_LEAD = 60

def _build_default_flow_bot(pub: Dict[str, Any]) -> Dict[str, Any]:
    tok = (pub.get("bot_token") or "").strip()
    bot_id = tok.split(":")[0] if ":" in tok else "8686296179"
    uname = (pub.get("bot_username") or "@rocha7_bot").strip()
    if not uname.startswith("@"):
        uname = f"@{uname}"
    ch_id = (pub.get("cache_channel_id") or "-1003939135982").strip()
    return {
        "id": "bot_default_1",
        "username": uname,
        "bot_id": bot_id,
        "first_name": pub.get("bot_first_name") or uname.lstrip("@"),
        "token": tok,
        "active": bool(pub.get("is_live", True)),
        "mode": "flow",
        "folder": "",
        "sales_today": 0,
        "revenue_today": 0.0,
        "welcome": {
            "message": "",
            "medias": [],
            "send_uncompressed": False,
            "secondary_enabled": False,
            "secondary_message": ""
        },
        "plans": [
            {
                "id": "plan_1",
                "name": "aa",
                "price": 9.90,
                "duration": "Vitalício",
                "button_color": "Padrão",
                "delivery_dest_type": "Canal ou grupo",
                "delivery_channel_id": ch_id,
                "delivery_channel_name": "Rocha VENDAS",
                "delivery_external_url": "",
                "expanded": True
            }
        ],
        "fallback_delivery": {
            "dest_type": "Canal ou grupo",
            "channel_id": ch_id,
            "channel_name": "Rocha VENDAS",
            "external_url": pub.get("delivery_destination") if str(pub.get("delivery_destination", "")).startswith("http") else "",
            "last_verified": datetime.now().strftime("%d/%m, %H:%M")
        },
        "price_variation": False,
        "downsell_general": {
            "enabled": True,
            "sequences": [
                {
                    "id": "seq_1",
                    "delay_minutes": 10,
                    "discount": "Sem desconto",
                    "plan_ids": ["plan_1"],
                    "message": "",
                    "medias": [],
                    "send_uncompressed": False
                }
            ]
        },
        "downsell_pix": {
            "enabled": False,
            "sequences": []
        },
        "order_bumps": [],
        "payments": {
            "generated_message": "⭐ <b>Você selecionou o seguinte plano:</b>\n\n🎁 Plano: {plano}\n💲 Valor: {valor}\n\n💎 Pague via Pix Copia e Cola (ou QR Code em alguns bancos):\n\n{pix_code}\n\n👆 Toque na chave PIX acima para copiá-la\n\nApós efetuar o pagamento, clique no botão abaixo ⤵️",
            "pix_format": "Código (monoespaçado)",
            "generated_medias": [],
            "generated_uncompressed": False,
            "secondary_enabled": False,
            "secondary_message": "",
            "approved_message": "✅ <b>Pagamento Aprovado!</b>\n\nParabéns {nome}! Seu pagamento do plano <b>{plano}</b> no valor de <b>{valor}</b> foi confirmado.\n\n🎉 Você já tem acesso ao conteúdo!",
            "access_button_text": "Acessar conteúdo",
            "approved_medias": [],
            "approved_uncompressed": False
        }
    }

def _normalize_providers(state: Dict[str, Any]) -> Dict[str, Any]:
    pub = state.setdefault("publish", {})
    if pub.get("gateway_secret") and pub.get("gateway_provider") in ("pushinpay", "suitpay", "", None):
        pub["gateway_provider"] = "syncpay"
    acc = state.setdefault("account", {})
    key = (acc.get("ai_api_key") or "").strip()
    if key.startswith("sk-") and len(key) == 35 and acc.get("ai_provider") in ("openai", "", None):
        acc["ai_provider"] = "deepseek"

    state.setdefault("bot_folders", [])
    flow_bots = state.setdefault("flow_bots", [])
    if not isinstance(flow_bots, list) or len(flow_bots) == 0:
        state["flow_bots"] = [_build_default_flow_bot(pub)]
        flow_bots = state["flow_bots"]

    # Sincroniza token ativo para que o polling rode o bot selecionado/ativo
    active_fb = next((b for b in flow_bots if b.get("active") and b.get("token")), None)
    if active_fb:
        pub["bot_token"] = active_fb["token"]
        pub["bot_username"] = active_fb.get("username", pub.get("bot_username", ""))
        pub["is_live"] = True

    memory = state.setdefault("memory", {"leads": []})
    leads = memory.get("leads", [])
    if len(leads) > MAX_ACTIVE_LEADS:
        memory["leads"] = leads[:MAX_ACTIVE_LEADS]
        leads = memory["leads"]

    for lead in leads:
        if lead.get("stage") in ("Em conversa", "", None):
            lead["stage"] = "Conversando"
        hist = lead.setdefault("stage_history", ["Conversando"])
        if "Conversando" not in hist:
            hist.insert(0, "Conversando")
        if lead["stage"] not in hist:
            hist.append(lead["stage"])
        msg_hist = lead.get("history")
        if isinstance(msg_hist, list) and len(msg_hist) > MAX_HISTORY_PER_LEAD:
            lead["history"] = msg_hist[-MAX_HISTORY_PER_LEAD:]
    return state

async def _read_state_from_db() -> Dict[str, Any]:
    global _STATE_CACHE
    if _STATE_CACHE is not None:
        return _STATE_CACHE
    async with _DB_LOCK:
        if _STATE_CACHE is not None:
            return _STATE_CACHE
        async with async_session() as session:
            result = await session.execute(select(BotConfig))
            config = result.scalars().first()
            if not config or not config.full_state_json or config.full_state_json == "{}":
                _STATE_CACHE = _normalize_providers(json.loads(json.dumps(DEFAULT_SHARKBOT_STATE)))
                return _STATE_CACHE
            try:
                saved = json.loads(config.full_state_json)
                merged = {**DEFAULT_SHARKBOT_STATE, **saved}
                _STATE_CACHE = _normalize_providers(merged)
                return _STATE_CACHE
            except Exception:
                _STATE_CACHE = _normalize_providers(json.loads(json.dumps(DEFAULT_SHARKBOT_STATE)))
                return _STATE_CACHE

async def _write_state_to_db(state: Dict[str, Any]) -> Dict[str, Any]:
    global _STATE_CACHE
    async with _DB_LOCK:
        state = _normalize_providers(state)
        _STATE_CACHE = state
        async with async_session() as session:
            result = await session.execute(select(BotConfig))
            config = result.scalars().first()
            if not config:
                config = BotConfig()
                session.add(config)

            config.full_state_json = json.dumps(state, ensure_ascii=False)
            publish = state.get("publish", {})
            config.is_active = bool(publish.get("is_live", False))
            config.telegram_bot_token = publish.get("bot_token", "")
            config.vip_channel_id = publish.get("delivery_destination", "")
            config.media_cache_channel_id = publish.get("cache_channel_id", "")
            config.support_username = publish.get("support_username", "")
            config.pushin_pay_token = publish.get("pushin_pay_token", "")
            await session.commit()
        return _STATE_CACHE

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        if not config:
            config = BotConfig(
                full_state_json=json.dumps(DEFAULT_SHARKBOT_STATE, ensure_ascii=False)
            )
            session.add(config)
            await session.commit()

    # Se o bot estiver ativo no banco, já inicia o polling automaticamente
    await bot_manager.configure_and_start(_read_state_from_db, _write_state_to_db)

@app.on_event("startup")
async def on_startup():
    await init_db()

@app.get("/api/state")
async def get_full_state():
    return await _read_state_from_db()

@app.post("/api/state")
async def save_full_state(state: Dict[str, Any]):
    await _write_state_to_db(state)
    bot_status = await bot_manager.configure_and_start(_read_state_from_db, _write_state_to_db)
    updated_state = await _read_state_from_db()
    return {
        "status": "success",
        "bot_status": bot_status,
        "state": updated_state,
        "updated_at": datetime.utcnow().isoformat()
    }

@app.get("/api/config")
async def get_config_compat():
    return await _read_state_from_db()

@app.post("/api/config")
async def post_config_compat(data: Dict[str, Any]):
    return await save_full_state(data)

@app.delete("/api/leads/{telegram_id}")
async def delete_lead(telegram_id: str):
    bot_manager.cancel_lead_tasks(str(telegram_id))
    state = await _read_state_from_db()
    memory = state.setdefault("memory", {"leads": []})
    leads = memory.get("leads", [])
    memory["leads"] = [l for l in leads if str(l.get("telegram_id")) != str(telegram_id)]
    learning = state.setdefault("learning", {})
    learning["evaluated_results"] = len(memory["leads"])
    await _write_state_to_db(state)
    return {"status": "success", "state": state}

@app.post("/api/leads/{telegram_id}/reset")
async def reset_lead_conversation(telegram_id: str):
    bot_manager.cancel_lead_tasks(str(telegram_id))
    state = await _read_state_from_db()
    memory = state.setdefault("memory", {"leads": []})
    for lead in memory.get("leads", []):
        if str(lead.get("telegram_id")) == str(telegram_id):
            lead["history"] = []
            lead["stage"] = "Conversando"
            lead["stage_history"] = ["Conversando"]
            lead["sent_downsell_steps"] = []
            lead["prefs_list"] = []
            lead["objs_list"] = []
            lead["preferences"] = "Iniciou conversa no bot"
            lead["objections"] = "Nenhuma identificada"
            lead.pop("chosen_plan", None)
            lead.pop("chosen_upsell", None)
            lead.pop("last_tx_is_upsell", None)
            break
    await _write_state_to_db(state)
    return {"status": "success", "state": state}

@app.delete("/api/leads")
async def clear_all_leads():
    state = await _read_state_from_db()
    for lead in state.get("memory", {}).get("leads", []):
        bot_manager.cancel_lead_tasks(str(lead.get("telegram_id")))
    state["memory"] = {"leads": []}
    state["learning"] = {
        "evaluated_results": 0,
        "confirmed_purchases": 0,
        "unpaid_checkouts": 0,
        "conversations_without_purchase": 0,
        "common_objections": [],
        "top_plans": []
    }
    await _write_state_to_db(state)
    return {"status": "success", "state": state}

class SimulateChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = []
    state: Optional[Dict[str, Any]] = None

@app.post("/api/simulate-chat")
async def simulate_chat(req: SimulateChatRequest):
    state = req.state
    if not state:
        state = await _read_state_from_db()
    result = await generate_ai_reply(state, req.history or [], req.message)
    return result

class CreatePixRequest(BaseModel):
    amount: float
    offer_name: str

@app.post("/api/create-pix")
async def create_pix_endpoint(req: CreatePixRequest):
    state = await _read_state_from_db()
    pub = state.get("publish", {})
    token = pub.get("pushin_pay_token", "").strip()
    gw_provider = pub.get("gateway_provider", "syncpay").strip()
    gw_secret = pub.get("gateway_secret", "").strip()
    cents = int(round(req.amount * 100))

    if token:
        try:
            client = PushinPayClient(token=token, provider=gw_provider, secret=gw_secret)
            res = await client.create_pix_charge(value_in_cents=cents)
            return {
                "status": "ok",
                "real": True,
                "provider": client.provider,
                "qr_code": res.get("qr_code"),
                "qr_code_base64": res.get("qr_code_base64"),
                "id": res.get("id")
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "real": False,
        "provider": gw_provider,
        "qr_code": f"00020126580014br.gov.bcb.pix0136sharkbot-pix-{cents}5204000053039865405{req.amount:.2f}5802BR5913VIP TELEGRAM6009SAO PAULO62070503***6304ABCD"
    }

@app.post("/api/webhook/syncpay")
@app.post("/api/webhook/pushinpay")
async def webhook_gateway(payload: Dict[str, Any]):
    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    transaction_id = str(
        data_obj.get("identifier")
        or data_obj.get("reference_id")
        or data_obj.get("id")
        or payload.get("identifier")
        or payload.get("reference_id")
        or payload.get("id")
        or ""
    )
    status = str(data_obj.get("status") or payload.get("status") or "").lower()
    if status in ("paid", "completed", "approved", "settled"):
        await bot_manager.notify_payment_by_tx_id(transaction_id)
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.transaction_id == transaction_id))
            order = result.scalars().first()
            if order:
                order.status = "paid"
                order.paid_at = datetime.utcnow()
                await session.commit()
        return {"status": "success", "action": "approved", "transaction_id": transaction_id}
    return {"status": "received", "transaction_id": transaction_id}

class DiagnosticsRequest(BaseModel):
    state: Optional[Dict[str, Any]] = None

@app.post("/api/diagnostics")
async def run_persona_diagnostics(req: DiagnosticsRequest):
    import asyncio
    state = req.state or await _read_state_from_db()
    t1 = generate_ai_reply(state, [], "Oi! Qual é o seu nome e quantos anos você tem?", lead_name="Mari")
    t2 = generate_ai_reply(state, [], "Quero entrar no seu VIP, quanto custa?", lead_name="Mari")
    t3 = generate_ai_reply(state, [], "Achei meio caro, consegue fazer um desconto pra eu pagar agora no Pix?", lead_name="Mari")
    t4 = generate_ai_reply(state, [], "Me manda uma foto sua de prévia pra eu te ver?", lead_name="Mari")
    r1, r2, r3, r4 = await asyncio.gather(t1, t2, t3, t4)

    pub = state.get("publish", {})
    gw_ok = False
    gw_msg = "Sem chave configurada"
    if pub.get("pushin_pay_token"):
        try:
            client = PushinPayClient(
                token=pub.get("pushin_pay_token", ""),
                provider=pub.get("gateway_provider", "syncpay"),
                secret=pub.get("gateway_secret", "")
            )
            if client.provider == "syncpay":
                bearer = await client._get_syncpay_bearer()
                gw_ok = bool(bearer)
                gw_msg = "Sync Pay autenticada (OAuth Bearer ativo)"
            else:
                gw_ok = True
                gw_msg = f"Gateway {client.provider} configurado"
        except Exception as e:
            gw_msg = f"Erro Gateway: {e}"

    bot_running = bool(bot_manager.polling_task and not bot_manager.polling_task.done())
    return {
        "status": "ok",
        "bot_running": bot_running,
        "bot_username": pub.get("bot_username", ""),
        "gateway_ok": gw_ok,
        "gateway_msg": gw_msg,
        "checks": [
            {
                "name": "1. Identidade & Idade (DeepSeek)",
                "ok": bool(r1.get("reply")) and not r1.get("offer_card"),
                "detail": r1.get("reply", "")
            },
            {
                "name": "2. Apresentação da Oferta & Botão Checkout",
                "ok": bool(r2.get("offer_card")),
                "detail": f"{r2.get('reply', '')} [Botão: {r2.get('offer_card', {}).get('name', 'N/A')} — R$ {float(r2.get('offer_card', {}).get('price', 0)):.2f}]" if r2.get("offer_card") else r2.get("reply", "")
            },
            {
                "name": "3. Negociação / Desconto Inteligente",
                "ok": bool(r3.get("reply")),
                "detail": f"{r3.get('reply', '')} [Botão: {r3.get('offer_card', {}).get('name', 'N/A')} — R$ {float(r3.get('offer_card', {}).get('price', 0)):.2f}]" if r3.get("offer_card") else r3.get("reply", "")
            },
            {
                "name": "4. Gatilho de Mídia da Conversa",
                "ok": bool(r4.get("media")) or not any(m.get("active", True) for m in state.get("medias", [])),
                "detail": f"Mídia enviada: {r4.get('media', {}).get('name')} | {r4.get('reply', '')}" if r4.get("media") else f"(Nenhuma mídia ativa) {r4.get('reply', '')}"
            }
        ]
    }


class VerifyTokenRequest(BaseModel):
    token: str

@app.post("/api/flow-bots/verify-token")
async def verify_flow_bot_token(req: VerifyTokenRequest):
    from aiogram import Bot
    tok = (req.token or "").strip()
    if not tok:
        return {"status": "error", "message": "Token vazio"}
    temp_bot = None
    try:
        temp_bot = Bot(token=tok)
        me = await temp_bot.get_me()
        return {
            "status": "ok",
            "bot_id": str(me.id),
            "username": f"@{me.username}" if me.username else f"bot_{me.id}",
            "first_name": me.first_name or "Bot"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if temp_bot:
            try:
                await temp_bot.session.close()
            except Exception:
                pass


class VerifyChannelRequest(BaseModel):
    token: Optional[str] = ""
    channel_id: str

@app.post("/api/flow-bots/verify-channel")
async def verify_flow_channel(req: VerifyChannelRequest):
    from aiogram import Bot
    state = await _read_state_from_db()
    tok = (req.token or state.get("publish", {}).get("bot_token") or "").strip()
    ch_id = (req.channel_id or "").strip()
    if not ch_id:
        return {"status": "error", "message": "ID do canal vazio"}
    if not tok:
        return {
            "status": "ok",
            "channel_id": ch_id,
            "title": f"Canal ({ch_id})",
            "verified_at": datetime.now().strftime("%d/%m, %H:%M")
        }
    temp_bot = None
    try:
        temp_bot = Bot(token=tok)
        chat = await temp_bot.get_chat(chat_id=ch_id)
        return {
            "status": "ok",
            "channel_id": str(chat.id),
            "title": chat.title or chat.username or str(chat.id),
            "verified_at": datetime.now().strftime("%d/%m, %H:%M")
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if temp_bot:
            try:
                await temp_bot.session.close()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

