import json
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, Text, DateTime, ForeignKey
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class BotConfig(Base):
    """Configurações centrais do Bot e da Persona"""
    __tablename__ = "bot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    is_active = Column(Boolean, default=False)
    telegram_bot_token = Column(String, default="")
    vip_channel_id = Column(String, default="")
    media_cache_channel_id = Column(String, default="")
    support_username = Column(String, default="")

    # API Keys
    pushin_pay_token = Column(String, default="")
    ai_api_key = Column(String, default="")
    ai_provider = Column(String, default="gemini") # gemini, openai, groq

    # Persona & Identidade
    persona_name = Column(String, default="Atendimento VIP")
    persona_age = Column(Integer, default=21)
    persona_gender = Column(String, default="Feminino") # Feminino, Masculino, Neutro
    persona_city = Column(String, default="São Paulo")
    persona_bio = Column(Text, default="Criadora de conteúdo adulto exclusiva. Conversa envolvente no chat privado.")
    persona_traits = Column(String, default="atenciosa, sensual, provocante, bem-humorada")

    # Jeito de Conversar
    sales_strategy = Column(String, default="equilibrado") # conexao, equilibrado, direto
    offer_level = Column(String, default="moderada") # nenhum, leve, moderada, direta
    slang_level = Column(String, default="moderada") # nenhuma, moderada, marcante
    cadence = Column(String, default="gradual") # agil, gradual, calma
    use_lead_name = Column(Boolean, default=True)
    voice_tone = Column(String, default="Provocante") # Acolhedor, Direto, Descontraido, Provocante
    response_length = Column(String, default="curto") # curto, medio, detalhado
    emoji_frequency = Column(String, default="leve") # nenhum, leve, moderado
    enable_audio_answers = Column(Boolean, default=False)

    # Hard rules e abertura
    welcome_message = Column(Text, default="oii 💜 chegou agora por aqui?")
    welcome_variations = Column(Text, default="oii tudo bem? viu meu canal hoje?|oii meu bem, quer conversar um pouquinho?")
    sales_objective = Column(Text, default="Entender o interesse, despertar desejo e fechar o acesso ao meu VIP privado.")
    custom_rules = Column(Text, default="Nunca repita pergunta já respondida. Quando o cliente demonstrar intenção clara de compra, envie a chave Pix imediatamente sem rodeios.")
    handoff_keywords = Column(String, default="suporte, atendente, problema no pagamento, falar com humano")

    # Negociação
    allow_bargain = Column(Boolean, default=True)
    min_bargain_price = Column(Float, default=14.90)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Offer(Base):
    """Ofertas e Planos de Assinatura"""
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False) # Ex: Mensal VIP
    price = Column(Float, nullable=False) # Ex: 29.90
    description = Column(Text, default="")
    emoji = Column(String, default="💎")
    duration_days = Column(Integer, default=30) # 9999 para vitalício
    is_highlight = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

class MediaAsset(Base):
    """Mídias da conversa (Amostras contextuais)"""
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    internal_name = Column(String, default="")
    media_type = Column(String, default="photo") # photo, video, audio
    file_path = Column(String, default="")
    telegram_file_id = Column(String, default="") # Cache instantâneo
    description = Column(Text, default="") # O que a mídia mostra
    objective = Column(String, default="aumentar_desejo") # criar_conexao, gerar_confianca, aumentar_desejo, responder_objecao, apoiar_oferta
    trigger_signals = Column(String, default="amostra, previa, foto, video, corpo")
    is_spoiler = Column(Boolean, default=False)
    send_once = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)

class DownsellStep(Base):
    """Etapas de recuperação por inatividade (Downsell)"""
    __tablename__ = "downsell_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    step_order = Column(Integer, default=1)
    delay_minutes = Column(Integer, default=5) # 1, 5, 15, 60, 1440
    message = Column(Text, default="sumiu por que? 🥺 quer que eu te mostre as opções de acesso?")
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=True)
    is_active = Column(Boolean, default=True)

class UpsellConfig(Base):
    """Configuração de Upsell pós-compra"""
    __tablename__ = "upsell_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    is_active = Column(Boolean, default=False)
    delay_minutes = Column(Integer, default=1)
    message = Column(Text, default="agora que seu acesso foi liberado, tenho uma opção extra que combina com você! 😈")
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=True)

class Lead(Base):
    """CRM de Leads do Telegram"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String, unique=True, index=True)
    first_name = Column(String, default="")
    username = Column(String, default="")
    funnel_stage = Column(String, default="em_conversa") # em_conversa, pix_gerado, pago, abandonou
    last_interaction = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, default="")
    media_sent_ids = Column(Text, default="[]") # JSON com IDs das mídias já enviadas

class Order(Base):
    """Registro de Pedidos e Pagamentos Pix"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"))
    offer_id = Column(Integer, ForeignKey("offers.id"))
    transaction_id = Column(String, unique=True, index=True) # ID da Pushin Pay
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending") # pending, paid, expired
    pix_copy_paste = Column(Text, default="")
    qr_code_base64 = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
