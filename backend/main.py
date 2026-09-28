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
        "is_live": False,
        "bot_token": "",
        "bot_username": "",
        "cache_channel_id": "",
        "delivery_destination": "",
        "support_username": "",
        "pushin_pay_token": ""
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
        "ai_provider": "openai",
        "ai_api_key": ""
    }
}

frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "public")
if os.path.exists(frontend_path):
    app.mount("/dashboard", StaticFiles(directory=frontend_path, html=True), name="dashboard")

@app.get("/")
def redirect_to_dashboard():
    return RedirectResponse(url="/dashboard/")

async def _read_state_from_db() -> Dict[str, Any]:
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        if not config or not config.full_state_json or config.full_state_json == "{}":
            return dict(DEFAULT_SHARKBOT_STATE)
        try:
            saved = json.loads(config.full_state_json)
            return {**DEFAULT_SHARKBOT_STATE, **saved}
        except Exception:
            return dict(DEFAULT_SHARKBOT_STATE)

async def _write_state_to_db(state: Dict[str, Any]) -> Dict[str, Any]:
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
    token = state.get("publish", {}).get("pushin_pay_token", "").strip()
    cents = int(round(req.amount * 100))

    if token:
        try:
            client = PushinPayClient(token=token)
            res = await client.create_pix_charge(value_in_cents=cents)
            return {
                "status": "ok",
                "real": True,
                "qr_code": res.get("qr_code"),
                "qr_code_base64": res.get("qr_code_base64"),
                "id": res.get("id")
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "real": False,
        "qr_code": f"00020126580014br.gov.bcb.pix0136sharkbot-pix-{cents}5204000053039865405{req.amount:.2f}5802BR5913VIP TELEGRAM6009SAO PAULO62070503***6304ABCD"
    }

@app.post("/api/webhook/pushinpay")
async def webhook_pushinpay(payload: Dict[str, Any]):
    transaction_id = str(payload.get("id"))
    status = str(payload.get("status", "")).lower()
    if status == "paid":
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.transaction_id == transaction_id))
            order = result.scalars().first()
            if order:
                order.status = "paid"
                order.paid_at = datetime.utcnow()
                await session.commit()
        return {"status": "success", "action": "approved"}
    return {"status": "received"}
