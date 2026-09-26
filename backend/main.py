import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from models import Base, BotConfig, Offer, MediaAsset, DownsellStep, UpsellConfig, Lead, Order
from pushin_pay import PushinPayClient
from ai_engine import build_system_prompt

DATABASE_URL = "sqlite+aiosqlite:///./hotbot.db"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="HotBot AI SaaS", description="Servidor central e Webhooks de Vendas")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Monta o frontend estático para acesso direto via navegador (ex: https://seudominio.com/)
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "public")
if os.path.exists(frontend_path):
    app.mount("/dashboard", StaticFiles(directory=frontend_path, html=True), name="dashboard")

@app.get("/")
def redirect_to_dashboard():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard/")

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Cria configuração inicial padrão se não existir
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        if not config:
            new_config = BotConfig()
            session.add(new_config)
            
            # Adiciona plano padrão
            default_offer1 = Offer(name="Acesso Mensal VIP", price=29.90, duration_days=30, is_highlight=True, description="Acesso completo ao canal VIP privado com fotos e vídeos diários sem censura.")
            default_offer2 = Offer(name="Acesso Vitalício VIP", price=69.90, duration_days=9999, is_highlight=False, description="Acesso vitalício sem mensalidades futuras.")
            session.add(default_offer1)
            session.add(default_offer2)

            # Adiciona etapas de downsell
            session.add(DownsellStep(step_order=1, delay_minutes=5, message="sumiu por que meu bem? 🥺 quer que eu te mostre as fotos que postei no VIP hoje?"))
            session.add(DownsellStep(step_order=2, delay_minutes=60, message="consegui um cupom especial de R$ 19,90 só pra você entrar no VIP agora... quer o link antes que expire? 💜"))
            
            await session.commit()

@app.on_event("startup")
async def on_startup():
    await init_db()

# --- MODELOS PYDANTIC ---
class ChatMessage(BaseModel):
    message: str

class ConfigUpdate(BaseModel):
    persona_name: Optional[str] = None
    persona_age: Optional[int] = None
    persona_gender: Optional[str] = None
    persona_city: Optional[str] = None
    persona_bio: Optional[str] = None
    persona_traits: Optional[str] = None
    sales_strategy: Optional[str] = None
    offer_level: Optional[str] = None
    voice_tone: Optional[str] = None
    response_length: Optional[str] = None
    welcome_message: Optional[str] = None
    custom_rules: Optional[str] = None
    pushin_pay_token: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    vip_channel_id: Optional[str] = None
    support_username: Optional[str] = None

# --- ROTAS DE CONFIGURAÇÃO ---
@app.get("/api/config")
async def get_configuration():
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        return config

@app.post("/api/config")
async def update_configuration(data: ConfigUpdate):
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()
        if not config:
            config = BotConfig()
            session.add(config)
        
        for key, value in data.dict(exclude_unset=True).items():
            setattr(config, key, value)
        
        await session.commit()
        return {"status": "success", "message": "Configurações salvas no banco com sucesso!"}

# --- ROTAS DE OFERTAS ---
@app.get("/api/offers")
async def list_offers():
    async with async_session() as session:
        result = await session.execute(select(Offer))
        return result.scalars().all()

# --- SIMULADOR DE CHAT DA IA ---
@app.post("/api/simulate-chat")
async def simulate_chat(chat: ChatMessage):
    async with async_session() as session:
        result = await session.execute(select(BotConfig))
        config = result.scalars().first()

    text = chat.message.lower()
    
    # Resposta contextual simulada caso não tenha API externa configurada
    if any(k in text for k in ["preço", "valor", "quanto", "custa", "plano"]):
        reply = f"Meu acesso mensal tá só R$ 29,90 hoje! Tem foto e vídeo novo todo dia sem censura. Se quiser, já te mando o Pix pra liberar seu acesso no mesmo segundo! 💜"
    elif any(k in text for k in ["foto", "previa", "amostra", "corpo", "video"]):
        reply = "rsrs você quer ver uma amostrinha antes? Olha o que eu postei hoje mais cedo... [SEND_MEDIA:1] Quer entrar no VIP pra ver tudo sem censura?"
    elif any(k in text for k in ["oi", "oii", "olá", "ola", "boa"]):
        reply = f"oii amor! tava com saudades de conversar... me conta, o que você mais gosta de ver? 😈"
    else:
        reply = f"adoro conversar com você assim... no meu canal VIP eu sou ainda mais solta rsrs. Quer dar uma olhadinha nos conteúdos de hoje?"

    return {"reply": reply}

# --- WEBHOOK PUSHIN PAY ---
@app.post("/api/webhook/pushinpay")
async def webhook_pushinpay(payload: Dict[str, Any]):
    """
    Recebe notificação automática de pagamento da Pushin Pay
    """
    transaction_id = str(payload.get("id"))
    status = payload.get("status")

    if status == "paid":
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.transaction_id == transaction_id))
            order = result.scalars().first()
            if order:
                order.status = "paid"
                order.paid_at = datetime.utcnow()
                await session.commit()
                print(f"[PAGAMENTO APROVADO] Pedido {order.id} liberado! Gerando link de convite VIP...")
        return {"status": "success", "action": "approved"}

    return {"status": "received"}
