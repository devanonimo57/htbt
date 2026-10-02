from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, Text, DateTime, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class BotConfig(Base):
    """Configurações centrais do Bot, Persona e Estado Completo de todas as 13 abas do Sharkbot"""
    __tablename__ = "bot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    is_active = Column(Boolean, default=False)
    telegram_bot_token = Column(String, default="")
    vip_channel_id = Column(String, default="")
    media_cache_channel_id = Column(String, default="")
    support_username = Column(String, default="")
    pushin_pay_token = Column(String, default="")
    ai_api_key = Column(String, default="")
    ai_provider = Column(String, default="gemini")

    # JSON completo que armazena fielmente todas as 13 abas (Persona, Ofertas, FAQs, Mídias, Downsell, Upsell, Regras)
    full_state_json = Column(Text, default="{}")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ClientRecord(Base):
    """One customer per immutable Telegram bot identity and Telegram user."""
    __tablename__ = "client_records"
    origin_bot_id = Column(String, primary_key=True)
    telegram_id = Column(String, primary_key=True)
    archived = Column(Boolean, primary_key=True, default=False)
    data_json = Column(Text, nullable=False)


class PixRecord(Base):
    """One payment per gateway transaction ID; indexed for dashboard aggregates."""
    __tablename__ = "pix_records"
    tx_id = Column(String, primary_key=True)
    origin_bot_id = Column(String, index=True, nullable=False, default="")
    bot_id = Column(String, index=True, nullable=False, default="")
    telegram_id = Column(String, index=True, nullable=False, default="")
    status = Column(String, index=True, nullable=False, default="pending")
    amount = Column(Float, nullable=False, default=0)
    is_test = Column(Boolean, nullable=False, default=False)
    is_upsell = Column(Boolean, nullable=False, default=False)
    paid_at = Column(String, nullable=False, default="")
    data_json = Column(Text, nullable=False)
    __table_args__ = (Index("ix_pix_bot_user", "origin_bot_id", "telegram_id"),)


class Offer(Base):
    """Ofertas e Planos de Assinatura"""
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    description = Column(Text, default="")
    emoji = Column(String, default="💎")
    duration_days = Column(Integer, default=30)
    is_highlight = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

class MediaAsset(Base):
    """Mídias da conversa (Amostras contextuais)"""
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    internal_name = Column(String, default="")
    media_type = Column(String, default="photo")
    file_path = Column(String, default="")
    telegram_file_id = Column(String, default="")
    description = Column(Text, default="")
    objective = Column(String, default="Criar conexão")
    trigger_signals = Column(String, default="vídeo, confiança, prévia")
    is_spoiler = Column(Boolean, default=False)
    send_once = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)

class DownsellStep(Base):
    """Etapas de recuperação por inatividade (Downsell)"""
    __tablename__ = "downsell_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    step_order = Column(Integer, default=1)
    delay_minutes = Column(Integer, default=1)
    message = Column(Text, default="sumiu por ai? 😜\n\nquer que eu te mostre as opções de acesso?")
    accompany_type = Column(String, default="Botão com os planos atuais")
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=True)
    is_active = Column(Boolean, default=True)

class UpsellConfig(Base):
    """Configuração de Upsell pós-compra"""
    __tablename__ = "upsell_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    is_active = Column(Boolean, default=True)
    delay_minutes = Column(Integer, default=1)
    message = Column(Text, default="agora que seu acesso foi liberado, tenho uma opção extra que combina com você! 😉")
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=True)

class Lead(Base):
    """CRM de Leads do Telegram"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String, unique=True, index=True)
    first_name = Column(String, default="")
    username = Column(String, default="")
    funnel_stage = Column(String, default="em_conversa")
    last_interaction = Column(DateTime, default=datetime.utcnow)
    preferences = Column(Text, default="")
    objections = Column(Text, default="")
    history_json = Column(Text, default="[]")
    media_sent_ids = Column(Text, default="[]")

class Order(Base):
    """Registro de Pedidos e Pagamentos Pix"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"))
    offer_id = Column(Integer, ForeignKey("offers.id"))
    transaction_id = Column(String, unique=True, index=True)
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending")
    pix_copy_paste = Column(Text, default="")
    qr_code_base64 = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
