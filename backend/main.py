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

def _normalize_providers(state: Dict[str, Any]) -> Dict[str, Any]:
    pub = state.setdefault("publish", {})
    if pub.get("gateway_secret") and pub.get("gateway_provider") in ("pushinpay", "suitpay", "", None):
        pub["gateway_provider"] = "syncpay"
    acc = state.setdefault("account", {})
    key = (acc.get("ai_api_key") or "").strip()
    if key.startswith("sk-") and len(key) == 35 and acc.get("ai_provider") in ("openai", "", None):
        acc["ai_provider"] = "deepseek"
    return state

async def _read_state_from_db() -> Dict[str, Any]:
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        if not config or not config.full_state_json or config.full_state_json == "{}":
            return dict(DEFAULT_SHARKBOT_STATE)
        try:
            saved = json.loads(config.full_state_json)
            merged = {**DEFAULT_SHARKBOT_STATE, **saved}
            return _normalize_providers(merged)
        except Exception:
            return dict(DEFAULT_SHARKBOT_STATE)

async def _write_state_to_db(state: Dict[str, Any]) -> Dict[str, Any]:
    state = _normalize_providers(state)
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
    return state

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
            lead["sent_downsell_steps"] = []
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
