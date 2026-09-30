import os
import json
import secrets
import hmac
from typing import Dict, Any, List, Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Depends, Header
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

_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=("*" not in _ALLOWED_ORIGINS),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
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

REDACTED_SECRET_MASK = "***configured***"
_RUNTIME_FALLBACK_ADMIN_TOKEN = secrets.token_urlsafe(32)


def _get_expected_admin_token() -> str:
    env_tok = (os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_PASSWORD") or "").strip()
    if env_tok:
        return env_tok
    token_file = os.path.join(os.path.dirname(__file__), ".admin_token")
    try:
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8") as f:
                saved_tok = f.read().strip()
                if saved_tok:
                    return saved_tok
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(_RUNTIME_FALLBACK_ADMIN_TOKEN)
    except Exception:
        pass
    return _RUNTIME_FALLBACK_ADMIN_TOKEN


async def verify_admin_auth(
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")
) -> bool:
    candidate = ""
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    elif isinstance(x_admin_token, str):
        candidate = x_admin_token.strip()

    expected = _get_expected_admin_token()
    if not candidate or not expected or not secrets.compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="Autenticação administrativa obrigatória.")
    return True


def _redact_state_secrets(state: Dict[str, Any]) -> Dict[str, Any]:
    """Remove tokens e chaves sensíveis antes de retornar o estado em respostas da API."""
    copy_state = json.loads(json.dumps(state or {}))
    pub = copy_state.setdefault("publish", {})
    if (pub.get("bot_token") or "").strip():
        pub["has_bot_token"] = True
        pub["bot_token"] = REDACTED_SECRET_MASK
    else:
        pub["has_bot_token"] = False
        pub["bot_token"] = ""

    if (pub.get("pushin_pay_token") or "").strip():
        pub["has_gateway_token"] = True
        pub["pushin_pay_token"] = REDACTED_SECRET_MASK
    else:
        pub["has_gateway_token"] = False
        pub["pushin_pay_token"] = ""

    if (pub.get("gateway_secret") or "").strip():
        pub["has_gateway_secret"] = True
        pub["gateway_secret"] = REDACTED_SECRET_MASK
    else:
        pub["has_gateway_secret"] = False
        pub["gateway_secret"] = ""

    acc = copy_state.setdefault("account", {})
    if (acc.get("ai_api_key") or "").strip():
        acc["has_ai_key"] = True
        acc["ai_api_key"] = REDACTED_SECRET_MASK
    else:
        acc["has_ai_key"] = False
        acc["ai_api_key"] = ""

    for fb in copy_state.get("flow_bots", []):
        if isinstance(fb, dict):
            if (fb.get("token") or "").strip():
                fb["has_token"] = True
                fb["token"] = REDACTED_SECRET_MASK
            else:
                fb["has_token"] = False
                fb["token"] = ""

    return copy_state


def _preserve_existing_secrets(incoming: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """Preserva segredos já salvos no banco quando o frontend envia a máscara REDACTED_SECRET_MASK."""
    if not existing:
        return incoming
    inc_pub = incoming.setdefault("publish", {})
    ex_pub = existing.get("publish", {})
    for key in ("bot_token", "pushin_pay_token", "gateway_secret"):
        val = (inc_pub.get(key) or "").strip()
        if val == REDACTED_SECRET_MASK:
            inc_pub[key] = ex_pub.get(key, "")

    inc_acc = incoming.setdefault("account", {})
    ex_acc = existing.get("account", {})
    if (inc_acc.get("ai_api_key") or "").strip() == REDACTED_SECRET_MASK:
        inc_acc["ai_api_key"] = ex_acc.get("ai_api_key", "")

    ex_bots_by_id = {
        str(b.get("id")): b
        for b in (existing.get("flow_bots") or [])
        if isinstance(b, dict) and b.get("id")
    }
    ex_bots_by_tg_id = {
        str(b.get("bot_id")): b
        for b in (existing.get("flow_bots") or [])
        if isinstance(b, dict) and b.get("bot_id")
    }
    for fb in incoming.get("flow_bots", []):
        if isinstance(fb, dict):
            tok = (fb.get("token") or "").strip()
            if tok == REDACTED_SECRET_MASK or not tok:
                old_bot = (
                    ex_bots_by_id.get(str(fb.get("id")))
                    or ex_bots_by_tg_id.get(str(fb.get("bot_id")))
                    or {}
                )
                old_tok = (old_bot.get("token") or "").strip()
                if old_tok and old_tok != REDACTED_SECRET_MASK:
                    fb["token"] = old_tok
                else:
                    pub_tok = (ex_pub.get("bot_token") or "").strip()
                    fb_tg_id = str(fb.get("bot_id") or "").strip()
                    other_tokens = {
                        str(b.get("token") or "").strip()
                        for b in (incoming.get("flow_bots") or [])
                        if isinstance(b, dict) and b is not fb
                    }
                    if (
                        pub_tok
                        and pub_tok != REDACTED_SECRET_MASK
                        and pub_tok not in other_tokens
                        and (not fb_tg_id or fb_tg_id == "8686296179" or pub_tok.startswith(fb_tg_id + ":"))
                    ):
                        fb["token"] = pub_tok
                    else:
                        fb["token"] = ""

    if "tx_records" not in incoming and "tx_records" in existing:
        incoming["tx_records"] = existing["tx_records"]

    return incoming


def _preserve_media_file_ids(inc_medias: Any, ex_medias: Any) -> None:
    if not isinstance(inc_medias, list) or not isinstance(ex_medias, list):
        return
    ex_by_id: Dict[str, Dict[str, Any]] = {}
    ex_by_key: Dict[str, Dict[str, Any]] = {}
    for m in ex_medias:
        if isinstance(m, dict) and m.get("telegram_file_id"):
            mid = str(m.get("id") or "").strip()
            if mid:
                ex_by_id[mid] = m
            key = f"{mid}|{m.get('url', '')}"
            ex_by_key[key] = m
    for m in inc_medias:
        if isinstance(m, dict) and not m.get("telegram_file_id"):
            mid = str(m.get("id") or "").strip()
            key = f"{mid}|{m.get('url', '')}"
            matched = ex_by_key.get(key) or (ex_by_id.get(mid) if mid else None)
            if matched and matched.get("telegram_file_id"):
                m["telegram_file_id"] = str(matched["telegram_file_id"])
                if matched.get("cached_channel_id"):
                    m["cached_channel_id"] = str(matched["cached_channel_id"])
                if str(matched.get("url") or "").startswith("tg://"):
                    m["url"] = matched["url"]


def _merge_config_preserving_runtime(incoming: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mescla alterações de configuração vindas do painel preservando integralmente o estado
    de execução acumulado pelo bot no servidor (memory.leads, tx_records, learning,
    contadores sales_today/revenue_today e cache de telegram_file_id).
    """
    if not existing:
        return incoming

    incoming = _preserve_existing_secrets(incoming, existing)

    if isinstance(existing.get("memory"), dict) and existing["memory"].get("leads"):
        incoming["memory"] = json.loads(json.dumps(existing["memory"]))

    if isinstance(existing.get("tx_records"), dict) and existing["tx_records"]:
        inc_tx = incoming.get("tx_records") if isinstance(incoming.get("tx_records"), dict) else {}
        incoming["tx_records"] = {**inc_tx, **json.loads(json.dumps(existing["tx_records"]))}

    if isinstance(existing.get("learning"), dict):
        ex_learn = existing["learning"]
        if any(ex_learn.get(k) for k in ("evaluated_results", "confirmed_purchases", "unpaid_checkouts", "top_plans", "common_objections")):
            incoming["learning"] = json.loads(json.dumps(ex_learn))

    _preserve_media_file_ids(incoming.get("medias"), existing.get("medias"))
    if isinstance(incoming.get("upsell"), dict) and isinstance(existing.get("upsell"), dict):
        _preserve_media_file_ids(incoming["upsell"].get("medias"), existing["upsell"].get("medias"))

    ex_bots_by_id = {
        str(b.get("id")): b
        for b in (existing.get("flow_bots") or [])
        if isinstance(b, dict) and b.get("id")
    }
    for fb in (incoming.get("flow_bots") or []):
        if isinstance(fb, dict) and fb.get("id"):
            old_bot = ex_bots_by_id.get(str(fb.get("id")))
            if old_bot:
                fb["sales_today"] = max(int(fb.get("sales_today") or 0), int(old_bot.get("sales_today") or 0))
                fb["revenue_today"] = round(
                    max(float(fb.get("revenue_today") or 0.0), float(old_bot.get("revenue_today") or 0.0)),
                    2
                )
                if isinstance(fb.get("welcome"), dict) and isinstance(old_bot.get("welcome"), dict):
                    _preserve_media_file_ids(fb["welcome"].get("medias"), old_bot["welcome"].get("medias"))
                if isinstance(fb.get("payments"), dict) and isinstance(old_bot.get("payments"), dict):
                    _preserve_media_file_ids(fb["payments"].get("generated_medias"), old_bot["payments"].get("generated_medias"))
                    _preserve_media_file_ids(fb["payments"].get("approved_medias"), old_bot["payments"].get("approved_medias"))
                for ds_key in ("downsell_general", "downsell_pix"):
                    inc_ds = fb.get(ds_key)
                    old_ds = old_bot.get(ds_key)
                    if isinstance(inc_ds, dict) and isinstance(old_ds, dict):
                        old_seqs = {str(s.get("id")): s for s in (old_ds.get("sequences") or []) if isinstance(s, dict)}
                        for s_idx, inc_seq in enumerate(inc_ds.get("sequences") or []):
                            if isinstance(inc_seq, dict):
                                old_seq = old_seqs.get(str(inc_seq.get("id")))
                                if not old_seq and s_idx < len(old_ds.get("sequences") or []):
                                    old_seq = old_ds["sequences"][s_idx]
                                if isinstance(old_seq, dict):
                                    _preserve_media_file_ids(inc_seq.get("medias"), old_seq.get("medias"))
                old_bumps = {str(ob.get("id")): ob for ob in (old_bot.get("order_bumps") or []) if isinstance(ob, dict)}
                for b_idx, inc_ob in enumerate(fb.get("order_bumps") or []):
                    if isinstance(inc_ob, dict):
                        old_ob = old_bumps.get(str(inc_ob.get("id")))
                        if not old_ob and b_idx < len(old_bot.get("order_bumps") or []):
                            old_ob = old_bot["order_bumps"][b_idx]
                        if isinstance(old_ob, dict):
                            _preserve_media_file_ids(inc_ob.get("medias"), old_ob.get("medias"))

    return incoming


frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "public")
if os.path.exists(frontend_path):
    app.mount("/dashboard", StaticFiles(directory=frontend_path, html=True), name="dashboard")

@app.get("/")
def redirect_to_dashboard():
    return RedirectResponse(url="/dashboard/")

@app.get("/favicon.ico")
@app.get("/favicon.png")
def redirect_favicon():
    return RedirectResponse(url="/dashboard/favicon.png?v=3")

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
    cur_bot_id = str(state.get("current_bot_id") or "").strip()
    cur_pub_tok = (pub.get("bot_token") or "").strip()
    active_fb = None
    if cur_bot_id:
        active_fb = next(
            (b for b in flow_bots if isinstance(b, dict) and str(b.get("id")) == cur_bot_id and b.get("active") and b.get("token")),
            None
        )
    if not active_fb and cur_pub_tok and cur_pub_tok != REDACTED_SECRET_MASK:
        active_fb = next(
            (b for b in flow_bots if isinstance(b, dict) and b.get("active") and (b.get("token") == cur_pub_tok or str(b.get("bot_id")) == cur_pub_tok.split(":")[0])),
            None
        )
    if not active_fb:
        active_fb = next((b for b in flow_bots if isinstance(b, dict) and b.get("active") and b.get("token")), None)
    if active_fb:
        pub["bot_token"] = active_fb["token"]
        pub["bot_username"] = active_fb.get("username", pub.get("bot_username", ""))
        pub["is_live"] = True
    elif any(isinstance(b, dict) and b.get("token") for b in flow_bots):
        pub["is_live"] = False

    # Evita que a instância antiga do Render roube o getUpdates da VPS dedicada
    if os.getenv("RENDER"):
        pub["is_live"] = False
        for b in flow_bots:
            b["active"] = False

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

async def _write_state_to_db(state: Dict[str, Any], preserve_runtime: bool = False) -> Dict[str, Any]:
    global _STATE_CACHE
    async with _DB_LOCK:
        async with async_session() as session:
            result = await session.execute(select(BotConfig))
            config = result.scalars().first()
            existing_state: Dict[str, Any] = {}
            if _STATE_CACHE is not None:
                existing_state = _STATE_CACHE
            elif config and config.full_state_json:
                try:
                    existing_state = json.loads(config.full_state_json)
                except Exception:
                    existing_state = {}

            if preserve_runtime:
                state = _merge_config_preserving_runtime(state, existing_state)
            else:
                state = _preserve_existing_secrets(state, existing_state)
            state = _normalize_providers(state)
            _STATE_CACHE = state

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

            # Sincroniza transações em tx_records na tabela relacional Order
            tx_map = state.get("tx_records")
            if isinstance(tx_map, dict):
                for tx_id, tx_rec in list(tx_map.items())[-100:]:
                    if not isinstance(tx_rec, dict) or not tx_id:
                        continue
                    try:
                        ord_res = await session.execute(select(Order).where(Order.transaction_id == str(tx_id)))
                        ord_row = ord_res.scalars().first()
                        if not ord_row:
                            ord_row = Order(
                                transaction_id=str(tx_id),
                                amount=float(tx_rec.get("amount") or 0.0),
                                status=str(tx_rec.get("status") or "pending"),
                            )
                            session.add(ord_row)
                        else:
                            ord_row.status = str(tx_rec.get("status") or ord_row.status)
                            if ord_row.status == "paid" and not ord_row.paid_at:
                                ord_row.paid_at = datetime.utcnow()
                    except Exception:
                        pass

            await session.commit()
        return _STATE_CACHE

async def _startup_warm_medias_task():
    try:
        st = await _read_state_from_db()
        warmed = await bot_manager.warm_all_state_medias(st)
        if warmed > 0:
            await _write_state_to_db(st)
    except Exception:
        pass


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

    # Garante criação/leitura do token administrativo no disco se não definido em variável de ambiente
    _get_expected_admin_token()

    # Se o bot estiver ativo no banco, já inicia o polling automaticamente
    await bot_manager.configure_and_start(_read_state_from_db, _write_state_to_db)
    _asyncio.create_task(_startup_warm_medias_task())

@app.on_event("startup")
async def on_startup():
    await init_db()


class AdminLoginRequest(BaseModel):
    token: str


@app.post("/api/auth/login")
async def admin_login(req: AdminLoginRequest):
    candidate = (req.token or "").strip()
    expected = _get_expected_admin_token()
    if not candidate or not expected or not secrets.compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="Credencial administrativa inválida.")
    return {"status": "ok", "authenticated": True}


@app.get("/api/state", dependencies=[Depends(verify_admin_auth)])
async def get_full_state():
    raw_state = await _read_state_from_db()
    return _redact_state_secrets(raw_state)

@app.post("/api/state", dependencies=[Depends(verify_admin_auth)])
async def save_full_state(state: Dict[str, Any]):
    await _write_state_to_db(state, preserve_runtime=True)
    bot_status = await bot_manager.configure_and_start(_read_state_from_db, _write_state_to_db)
    updated_state = await _read_state_from_db()
    warmed = 0
    try:
        warmed = await bot_manager.warm_all_state_medias(updated_state)
        if warmed > 0:
            updated_state = await _write_state_to_db(updated_state)
    except Exception:
        pass
    return {
        "status": "success",
        "bot_status": bot_status,
        "warmed_medias": warmed,
        "state": _redact_state_secrets(updated_state),
        "updated_at": datetime.utcnow().isoformat()
    }

@app.get("/api/config", dependencies=[Depends(verify_admin_auth)])
async def get_config_compat():
    raw_state = await _read_state_from_db()
    return _redact_state_secrets(raw_state)

@app.post("/api/config", dependencies=[Depends(verify_admin_auth)])
async def post_config_compat(data: Dict[str, Any]):
    return await save_full_state(data)

@app.delete("/api/leads/{telegram_id}", dependencies=[Depends(verify_admin_auth)])
async def delete_lead(telegram_id: str):
    bot_manager.cancel_lead_tasks(str(telegram_id))
    state = await _read_state_from_db()
    memory = state.setdefault("memory", {"leads": []})
    leads = memory.get("leads", [])
    memory["leads"] = [l for l in leads if str(l.get("telegram_id")) != str(telegram_id)]
    learning = state.setdefault("learning", {})
    learning["evaluated_results"] = len(memory["leads"])
    await _write_state_to_db(state)
    return {"status": "success", "state": _redact_state_secrets(state)}

@app.post("/api/leads/{telegram_id}/reset", dependencies=[Depends(verify_admin_auth)])
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
    return {"status": "success", "state": _redact_state_secrets(state)}

@app.delete("/api/leads", dependencies=[Depends(verify_admin_auth)])
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
    return {"status": "success", "state": _redact_state_secrets(state)}

class SimulateChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = []
    state: Optional[Dict[str, Any]] = None

@app.post("/api/simulate-chat", dependencies=[Depends(verify_admin_auth)])
async def simulate_chat(req: SimulateChatRequest):
    db_state = await _read_state_from_db()
    state = _preserve_existing_secrets(req.state, db_state) if req.state else db_state
    result = await generate_ai_reply(state, req.history or [], req.message)
    return result

class CreatePixRequest(BaseModel):
    amount: float
    offer_name: str

@app.post("/api/create-pix", dependencies=[Depends(verify_admin_auth)])
async def create_pix_endpoint(req: CreatePixRequest):
    state = await _read_state_from_db()
    pub = state.get("publish", {})
    token = pub.get("pushin_pay_token", "").strip()
    gw_provider = pub.get("gateway_provider", "syncpay").strip()
    gw_secret = pub.get("gateway_secret", "").strip()
    cents = int(round(req.amount * 100))
    if cents < 50:
        raise HTTPException(status_code=400, detail="Valor inválido para cobrança PIX.")
    if not token or token == REDACTED_SECRET_MASK:
        return {"status": "error", "real": False, "message": "Gateway PIX não configurado."}

    try:
        client = PushinPayClient(token=token, provider=gw_provider, secret=gw_secret)
        res = await client.create_pix_charge(value_in_cents=cents)
        if not res.get("id") or not res.get("qr_code"):
            return {"status": "error", "real": False, "message": "Resposta inválida do gateway PIX."}
        return {
            "status": "ok",
            "real": True,
            "provider": client.provider,
            "qr_code": res.get("qr_code"),
            "qr_code_base64": res.get("qr_code_base64"),
            "id": res.get("id")
        }
    except Exception as e:
        return {"status": "error", "real": False, "message": str(e)}

@app.post("/api/webhook/syncpay")
@app.post("/api/webhook/pushinpay")
async def webhook_gateway(payload: Dict[str, Any], request: Request = None):
    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if webhook_secret:
        headers = request.headers if request and hasattr(request, "headers") else {}
        sig = (
            headers.get("x-webhook-secret")
            or headers.get("x-webhook-token")
            or headers.get("x-signature")
            or ""
        ).strip()
        if not sig or not hmac.compare_digest(sig, webhook_secret):
            raise HTTPException(status_code=401, detail="Assinatura de webhook inválida.")

    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    transaction_id = str(
        data_obj.get("identifier")
        or data_obj.get("reference_id")
        or data_obj.get("id")
        or payload.get("identifier")
        or payload.get("reference_id")
        or payload.get("id")
        or ""
    ).strip()

    if not transaction_id or transaction_id.startswith("sim_"):
        return {"status": "rejected", "action": "invalid_transaction_id", "transaction_id": transaction_id}

    status = str(data_obj.get("status") or payload.get("status") or "").lower()
    if status not in ("paid", "completed", "approved", "settled"):
        return {"status": "received", "action": "ignored_non_paid_status", "transaction_id": transaction_id}

    # Fail-closed: NUNCA confia apenas no JSON do webhook; consulta o status oficial no gateway
    state = await _read_state_from_db()
    pub = state.get("publish", {})
    token = (pub.get("pushin_pay_token") or "").strip()
    gw_provider = (pub.get("gateway_provider") or "syncpay").strip()
    gw_secret = (pub.get("gateway_secret") or "").strip()

    if not token or token == REDACTED_SECRET_MASK:
        return {"status": "rejected", "action": "gateway_not_configured", "transaction_id": transaction_id}

    try:
        client = PushinPayClient(token=token, provider=gw_provider, secret=gw_secret)
        verified = await client.check_transaction_status(transaction_id)
    except Exception:
        return {"status": "error", "action": "gateway_verification_failed", "transaction_id": transaction_id}

    verified_status = str((verified or {}).get("status", "")).lower()
    if verified_status != "paid":
        return {"status": "rejected", "action": "not_paid_at_gateway", "transaction_id": transaction_id}

    # Se o gateway retornar valor pago, confere contra a transação registrada no servidor
    tx_record = (state.get("tx_records") or {}).get(transaction_id) or bot_manager.tx_records.get(transaction_id)
    raw_gw = (verified or {}).get("raw") if isinstance((verified or {}).get("raw"), dict) else verified
    if tx_record and isinstance(raw_gw, dict):
        expected_amount = float(tx_record.get("amount") or 0)
        if "amount" in raw_gw and raw_gw["amount"] is not None:
            paid_amount = float(raw_gw["amount"])
            if paid_amount + 0.01 < expected_amount:
                return {"status": "rejected", "action": "amount_mismatch", "transaction_id": transaction_id}
        elif "value" in raw_gw and raw_gw["value"] is not None:
            paid_cents = int(raw_gw["value"])
            if paid_cents < int(tx_record.get("cents") or round(expected_amount * 100)):
                return {"status": "rejected", "action": "amount_mismatch", "transaction_id": transaction_id}

    delivered = await bot_manager.notify_payment_by_tx_id(transaction_id)
    if not delivered:
        return {"status": "rejected", "action": "unknown_or_already_delivered_tx", "transaction_id": transaction_id}

    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.transaction_id == transaction_id))
        order = result.scalars().first()
        if order:
            order.status = "paid"
            order.paid_at = datetime.utcnow()
            await session.commit()
    return {"status": "success", "action": "approved", "transaction_id": transaction_id}

class DiagnosticsRequest(BaseModel):
    state: Optional[Dict[str, Any]] = None

@app.post("/api/diagnostics", dependencies=[Depends(verify_admin_auth)])
async def run_persona_diagnostics(req: DiagnosticsRequest):
    import asyncio
    db_state = await _read_state_from_db()
    state = _preserve_existing_secrets(req.state, db_state) if req.state else db_state
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

@app.post("/api/flow-bots/verify-token", dependencies=[Depends(verify_admin_auth)])
async def verify_flow_bot_token(req: VerifyTokenRequest):
    from telegram_service import _create_bot_instance
    tok = (req.token or "").strip()
    if tok == REDACTED_SECRET_MASK:
        state = await _read_state_from_db()
        tok = (state.get("publish", {}).get("bot_token") or "").strip()
    if not tok:
        return {"status": "error", "message": "Token vazio"}
    temp_bot = None
    try:
        temp_bot = _create_bot_instance(tok)
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
    bot_id: Optional[str] = ""
    token: Optional[str] = ""
    channel_id: str

@app.post("/api/flow-bots/verify-channel", dependencies=[Depends(verify_admin_auth)])
async def verify_flow_channel(req: VerifyChannelRequest):
    from telegram_service import _create_bot_instance
    state = await _read_state_from_db()
    target_fb = None
    if req.bot_id:
        for b in (state.get("flow_bots") or []):
            if isinstance(b, dict) and str(b.get("id")) == str(req.bot_id):
                target_fb = b
                break
    if not target_fb:
        target_fb = bot_manager._get_active_flow_bot(state)

    tok = (req.token or "").strip()
    if not tok or tok == REDACTED_SECRET_MASK:
        tok = (
            (target_fb.get("token") if isinstance(target_fb, dict) else "")
            or state.get("publish", {}).get("bot_token")
            or bot_manager.current_token
            or ""
        ).strip()
    ch_id = (req.channel_id or "").strip()
    if not ch_id:
        return {"status": "error", "message": "ID do canal vazio"}
    if not tok or tok == REDACTED_SECRET_MASK:
        return {"status": "error", "message": "Token do bot não configurado"}
    temp_bot = None
    try:
        temp_bot = _create_bot_instance(tok)
        parsed_ch = int(ch_id) if ch_id.lstrip("-").isdigit() else ch_id
        chat = await temp_bot.get_chat(chat_id=parsed_ch)
        resolved_id = str(chat.id)
        state.setdefault("publish", {})["cache_channel_id"] = resolved_id
        if isinstance(target_fb, dict):
            target_fb["cache_channel_id"] = resolved_id
        warmed = await bot_manager.warm_all_state_medias(state)
        state = await _write_state_to_db(state)
        return {
            "status": "ok",
            "channel_id": resolved_id,
            "title": chat.title or chat.username or resolved_id,
            "verified_at": datetime.now().strftime("%d/%m, %H:%M"),
            "warmed_medias": warmed,
            "state": _redact_state_secrets(state)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if temp_bot:
            try:
                await temp_bot.session.close()
            except Exception:
                pass


class CacheMediaRequest(BaseModel):
    bot_id: Optional[str] = ""
    channel_id: Optional[str] = ""
    media: Dict[str, Any]


@app.post("/api/flow-bots/cache-media", dependencies=[Depends(verify_admin_auth)])
async def cache_flow_media(req: CacheMediaRequest):
    from telegram_service import _create_bot_instance
    state = await _read_state_from_db()
    fb = None
    for b in (state.get("flow_bots") or []):
        if isinstance(b, dict) and (not req.bot_id or str(b.get("id")) == str(req.bot_id)):
            fb = b
            break
    if not fb:
        fb = bot_manager._get_active_flow_bot(state)

    tok = (
        (fb.get("token") if isinstance(fb, dict) else "")
        or state.get("publish", {}).get("bot_token")
        or bot_manager.current_token
        or ""
    ).strip()
    if tok == REDACTED_SECRET_MASK:
        tok = (state.get("publish", {}).get("bot_token") or bot_manager.current_token or "").strip()
    if not tok or tok == REDACTED_SECRET_MASK:
        return {"status": "error", "message": "Token do bot não configurado"}

    media_copy = dict(req.media or {})
    if req.channel_id and isinstance(fb, dict):
        fb["cache_channel_id"] = req.channel_id.strip()

    temp_bot = None
    bot_to_use = bot_manager._get_bot_by_token(tok) if (bot_manager._primary_bot or bot_manager.extra_bots) else None
    if bot_to_use and tok not in (bot_manager.current_token, *bot_manager.extra_bots.keys()):
        bot_to_use = None
    if not bot_to_use:
        try:
            temp_bot = _create_bot_instance(tok)
            bot_to_use = temp_bot
        except Exception as e:
            return {"status": "error", "message": str(e)}

    try:
        fid = await bot_manager.warm_media_item_in_channel(
            media_copy,
            state,
            fb=fb,
            bot_instance=bot_to_use
        )
        if not fid:
            err_detail = media_copy.get("_cache_error") or "Verifique se o bot é administrador do canal com permissão de postar mensagens."
            return {
                "status": "error",
                "message": f"Falha ao enviar ao canal: {err_detail}"
            }
        return {
            "status": "ok",
            "telegram_file_id": fid,
            "telegram_send_as": media_copy.get("telegram_send_as", ""),
            "cached_channel_id": media_copy.get("cached_channel_id", ""),
            "media": media_copy
        }
    finally:
        if temp_bot:
            try:
                await temp_bot.session.close()
            except Exception:
                pass


class FlowDiagnoseRequest(BaseModel):
    bot_id: Optional[str] = ""


@app.post("/api/flow-bots/diagnose", dependencies=[Depends(verify_admin_auth)])
async def diagnose_flow_bot(req: FlowDiagnoseRequest):
    from telegram_service import _create_bot_instance
    state = await _read_state_from_db()
    fb = None
    if req.bot_id:
        for b in (state.get("flow_bots") or []):
            if isinstance(b, dict) and str(b.get("id")) == str(req.bot_id):
                fb = b
                break
    if not fb:
        fb = bot_manager._get_active_flow_bot(state) or ((state.get("flow_bots") or [None])[0])

    if not isinstance(fb, dict):
        return {"status": "error", "message": "Bot não encontrado."}

    checks: List[Dict[str, Any]] = []
    tok = (fb.get("token") or state.get("publish", {}).get("bot_token") or "").strip()
    temp_bot = None
    bot_me = None

    # 1. Token & Conexão Telegram
    if not tok or tok == REDACTED_SECRET_MASK:
        checks.append({
            "name": "1. Conexão do Bot no Telegram",
            "ok": False,
            "detail": "Token do bot não configurado."
        })
    else:
        try:
            temp_bot = _create_bot_instance(tok)
            bot_me = await temp_bot.get_me()
            is_act = fb.get("active", True) is not False
            checks.append({
                "name": "1. Conexão do Bot no Telegram",
                "ok": is_act,
                "detail": f"Conectado como @{bot_me.username} (ID {bot_me.id}) — {'Bot ligado' if is_act else '⚠️ Bot está desligado no painel'}"
            })
        except Exception as e:
            checks.append({
                "name": "1. Conexão do Bot no Telegram",
                "ok": False,
                "detail": f"Falha ao validar token no Telegram: {e}"
            })

    # 2. Boas-vindas & Cache de Mídias
    try:
        warmed = await bot_manager.warm_all_state_medias(state)
        if warmed > 0:
            state = await _write_state_to_db(state)
    except Exception:
        pass

    welcome = fb.get("welcome") or {}
    has_w_msg = bool((welcome.get("message") or "").strip())
    has_w_sec = bool(welcome.get("secondary_enabled") and (welcome.get("secondary_message") or "").strip())
    w_medias = [m for m in (welcome.get("medias") or []) if isinstance(m, dict)]
    all_medias: List[Dict[str, Any]] = list(w_medias)
    for ds_key in ("downsell_general", "downsell_pix"):
        for seq in ((fb.get(ds_key) or {}).get("sequences") or []):
            if isinstance(seq, dict):
                all_medias.extend([m for m in (seq.get("medias") or []) if isinstance(m, dict)])
    for ob in (fb.get("order_bumps") or []):
        if isinstance(ob, dict):
            all_medias.extend([m for m in (ob.get("medias") or []) if isinstance(m, dict)])
    pay_cfg = fb.get("payments") or {}
    all_medias.extend([m for m in (pay_cfg.get("generated_medias") or []) if isinstance(m, dict)])
    all_medias.extend([m for m in (pay_cfg.get("approved_medias") or []) if isinstance(m, dict)])

    cached_count = sum(1 for m in all_medias if (m.get("telegram_file_id") or "").strip())
    welcome_ok = (has_w_msg or len(w_medias) > 0 or has_w_sec) and (len(all_medias) == 0 or cached_count == len(all_medias))
    if not (has_w_msg or len(w_medias) > 0 or has_w_sec):
        w_detail = "⚠️ Boas-vindas sem mensagem e sem mídia."
    elif len(all_medias) > 0 and cached_count < len(all_medias):
        w_detail = f"⚠️ {cached_count}/{len(all_medias)} mídias sincronizadas no cache. Verifique o ID do Canal de Cache e se o bot é admin."
    elif len(all_medias) > 0:
        w_detail = f"Pronto ({cached_count}/{len(all_medias)} mídias com file_id em cache instantâneo)."
    else:
        w_detail = "Pronto (somente texto configurado, sem mídias anexadas)."
    checks.append({
        "name": "2. Boas-vindas & Cache de Mídias",
        "ok": welcome_ok,
        "detail": w_detail
    })

    # 3. Planos & Entrega VIP
    plans = [p for p in (fb.get("plans") or []) if isinstance(p, dict)]
    if not plans:
        checks.append({
            "name": "3. Planos & Canal/Link de Entrega VIP",
            "ok": False,
            "detail": "Nenhum plano cadastrado na aba Planos."
        })
    else:
        plan_errors: List[str] = []
        plan_ok_details: List[str] = []
        fb_deliv = fb.get("fallback_delivery") or {}
        for p in plans:
            p_name = p.get("name") or "Plano"
            dt = p.get("delivery_dest_type") or "Canal ou grupo"
            if dt == "Herdar do fluxo":
                dt = fb_deliv.get("dest_type") or "Canal ou grupo"
            if dt == "Link externo":
                ext = (p.get("delivery_external_url") or fb_deliv.get("external_url") or "").strip()
                if ext and (ext.startswith("http://") or ext.startswith("https://") or ext.startswith("t.me/")):
                    plan_ok_details.append(f"{p_name}: Link externo OK ({ext})")
                else:
                    plan_errors.append(f"{p_name}: Link externo vazio ou inválido")
            else:
                raw_ch = (p.get("delivery_channel_id") or "").strip()
                fb_ch = (fb_deliv.get("channel_id") or "").strip()
                ch_id = fb_ch if (not raw_ch or raw_ch == "-1003939135982") and fb_ch else (raw_ch or fb_ch)
                if not ch_id or ch_id == "-1003939135982":
                    plan_errors.append(f"{p_name}: ID do Canal VIP ainda não configurado")
                elif temp_bot and bot_me:
                    try:
                        parsed_ch = int(ch_id) if ch_id.lstrip("-").isdigit() else ch_id
                        chat_obj = await temp_bot.get_chat(chat_id=parsed_ch)
                        member = await temp_bot.get_chat_member(chat_id=parsed_ch, user_id=bot_me.id)
                        st_member = str(getattr(member, "status", "")).lower()
                        if st_member in ("administrator", "creator"):
                            plan_ok_details.append(f"{p_name}: Admin verificado no canal '{chat_obj.title or ch_id}'")
                        else:
                            plan_errors.append(f"{p_name}: Bot não é administrador no canal '{chat_obj.title or ch_id}'")
                    except Exception as e:
                        plan_errors.append(f"{p_name}: Sem acesso ao canal {ch_id} ({e})")
                else:
                    plan_ok_details.append(f"{p_name}: Canal {ch_id} configurado")

        checks.append({
            "name": "3. Planos & Canal/Link de Entrega VIP",
            "ok": len(plan_errors) == 0,
            "detail": " | ".join(plan_errors if plan_errors else plan_ok_details)
        })

    # 4. Gateway PIX
    pub = state.get("publish") or {}
    gw_tok = (pub.get("pushin_pay_token") or "").strip()
    gw_prov = (pub.get("gateway_provider") or "syncpay").strip()
    gw_sec = (pub.get("gateway_secret") or "").strip()
    if not gw_tok or gw_tok == REDACTED_SECRET_MASK:
        checks.append({
            "name": "4. Gateway PIX",
            "ok": False,
            "detail": "Chave de API do Gateway PIX não configurada na aba Gateways."
        })
    else:
        try:
            client = PushinPayClient(token=gw_tok, provider=gw_prov, secret=gw_sec)
            if client.provider == "syncpay":
                bearer = await client._get_syncpay_bearer()
                gw_ok = bool(bearer)
                gw_det = "SyncPay autenticada e pronta para gerar PIX." if gw_ok else "Falha ao autenticar na SyncPay."
            else:
                gw_ok = True
                gw_det = f"Gateway {client.provider.upper()} configurado e pronto."
            checks.append({
                "name": "4. Gateway PIX",
                "ok": gw_ok,
                "detail": gw_det
            })
        except Exception as e:
            checks.append({
                "name": "4. Gateway PIX",
                "ok": False,
                "detail": f"Erro ao validar gateway ({gw_prov}): {e}"
            })

    if temp_bot:
        try:
            await temp_bot.session.close()
        except Exception:
            pass

    return {
        "status": "ok",
        "all_ok": all(bool(c.get("ok")) for c in checks),
        "checks": checks,
        "secret_approve_cmd": f"/aprovar {_get_expected_admin_token()}",
        "secret_downsell_cmd": f"/downsell {_get_expected_admin_token()}",
        "state": _redact_state_secrets(state)
    }


def _reset_lead_for_retest(state: Dict[str, Any], user_id: int) -> None:
    if not user_id:
        return
    bot_manager.cancel_lead_tasks(str(user_id))
    for lead in (state.get("memory") or {}).get("leads", []):
        if str(lead.get("telegram_id")) == str(user_id):
            lead["stage"] = "Conversando"
            lead["stage_history"] = ["Conversando"]
            lead["sent_downsell_steps"] = []
            lead["authorized_flow_discounts"] = []
            lead.pop("chosen_plan", None)
            lead.pop("chosen_upsell", None)
            lead.pop("last_tx_id", None)
            lead.pop("last_tx_is_upsell", None)
            lead.pop("accepted_bump_indices", None)
            lead.pop("accepted_bump_deliverable", None)
            break


@app.delete("/api/flow-bots/transactions/{tx_id}", dependencies=[Depends(verify_admin_auth)])
async def delete_flow_transaction(tx_id: str):
    clean_id = (tx_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="ID da transação inválido.")

    state = await _read_state_from_db()
    tx_map = state.setdefault("tx_records", {})
    tx_rec = tx_map.pop(clean_id, None) or bot_manager.tx_records.pop(clean_id, None)
    bot_manager.tx_records.pop(clean_id, None)
    bot_manager.active_tx_map.pop(clean_id, None)
    poll_t = bot_manager.pix_poll_tasks.pop(clean_id, None)
    if poll_t and not poll_t.done():
        poll_t.cancel()

    if isinstance(tx_rec, dict):
        was_paid = (tx_rec.get("status") == "paid" or bool(tx_rec.get("delivered")))
        amt = float(tx_rec.get("amount") or 0.0)
        target_bot_id = str(tx_rec.get("bot_id") or "").strip()
        target_bot_tok = str(tx_rec.get("bot_token") or "").strip()
        if was_paid:
            for fb in (state.get("flow_bots") or []):
                if not isinstance(fb, dict):
                    continue
                if (
                    (target_bot_id and str(fb.get("id")) == target_bot_id)
                    or (target_bot_tok and str(fb.get("token")) == target_bot_tok)
                    or (not target_bot_id and not target_bot_tok)
                ):
                    fb["sales_today"] = max(0, int(fb.get("sales_today") or 0) - 1)
                    fb["revenue_today"] = max(0.0, round(float(fb.get("revenue_today") or 0.0) - amt, 2))
                    break
            learning = state.setdefault("learning", {})
            learning["confirmed_purchases"] = max(0, int(learning.get("confirmed_purchases") or 0) - 1)

        uid = int(tx_rec.get("user_id") or 0)
        if uid:
            _reset_lead_for_retest(state, uid)

    async with async_session() as session:
        try:
            ord_res = await session.execute(select(Order).where(Order.transaction_id == clean_id))
            for ord_row in ord_res.scalars().all():
                await session.delete(ord_row)
            await session.commit()
        except Exception:
            pass

    state = await _write_state_to_db(state, preserve_runtime=False)
    return {"status": "ok", "deleted_tx_id": clean_id, "state": _redact_state_secrets(state)}


class ClearTestDataRequest(BaseModel):
    bot_id: Optional[str] = ""
    clear_all_sales: bool = False


@app.post("/api/flow-bots/clear-test-data", dependencies=[Depends(verify_admin_auth)])
async def clear_flow_test_data(req: ClearTestDataRequest):
    state = await _read_state_from_db()
    tx_map = state.setdefault("tx_records", {})
    target_bot_id = (req.bot_id or "").strip()
    removed_ids: List[str] = []
    affected_users: List[int] = []

    for tid, rec in list(tx_map.items()):
        if not isinstance(rec, dict):
            continue
        rec_bot_id = str(rec.get("bot_id") or "").strip()
        matches_bot = (not target_bot_id or target_bot_id == "all" or not rec_bot_id or rec_bot_id == target_bot_id)
        if not matches_bot:
            continue
        is_test_tx = bool(rec.get("is_test")) or str(tid).startswith("test_")
        if is_test_tx or req.clear_all_sales:
            removed_ids.append(tid)
            tx_map.pop(tid, None)
            bot_manager.tx_records.pop(tid, None)
            bot_manager.active_tx_map.pop(tid, None)
            poll_t = bot_manager.pix_poll_tasks.pop(tid, None)
            if poll_t and not poll_t.done():
                poll_t.cancel()
            uid = int(rec.get("user_id") or 0)
            if uid:
                affected_users.append(uid)
            if rec.get("status") == "paid" or rec.get("delivered"):
                amt = float(rec.get("amount") or 0.0)
                for fb in (state.get("flow_bots") or []):
                    if isinstance(fb, dict) and (not rec_bot_id or str(fb.get("id")) == rec_bot_id):
                        fb["sales_today"] = max(0, int(fb.get("sales_today") or 0) - 1)
                        fb["revenue_today"] = max(0.0, round(float(fb.get("revenue_today") or 0.0) - amt, 2))
                        break
                learning = state.setdefault("learning", {})
                learning["confirmed_purchases"] = max(0, int(learning.get("confirmed_purchases") or 0) - 1)

    if req.clear_all_sales:
        for fb in (state.get("flow_bots") or []):
            if isinstance(fb, dict) and (not target_bot_id or target_bot_id == "all" or str(fb.get("id")) == target_bot_id):
                fb["sales_today"] = 0
                fb["revenue_today"] = 0.0

    for uid in set(affected_users):
        _reset_lead_for_retest(state, uid)

    if removed_ids:
        async with async_session() as session:
            try:
                ord_res = await session.execute(select(Order).where(Order.transaction_id.in_(removed_ids)))
                for ord_row in ord_res.scalars().all():
                    await session.delete(ord_row)
                await session.commit()
            except Exception:
                pass

    state = await _write_state_to_db(state, preserve_runtime=False)
    return {
        "status": "ok",
        "removed_count": len(removed_ids),
        "state": _redact_state_secrets(state)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


