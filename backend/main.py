import os
import re
import html
import json
import logging
import secrets
import hmac
import copy
import contextlib
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("SharkBotAPI")

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func

from models import Base, BotConfig, Offer, MediaAsset, DownsellStep, UpsellConfig, Lead, Order, ClientRecord, PixRecord
from pushin_pay import PushinPayClient
from ai_engine import generate_ai_reply
from telegram_service import bot_manager

_DEFAULT_DB_PATH = "/var/data/hotbot.db" if os.path.isdir("/var/data") and os.access("/var/data", os.W_OK) else "./hotbot.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}")

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

    tx_recs = copy_state.get("tx_records")
    if isinstance(tx_recs, dict):
        for rec in tx_recs.values():
            if isinstance(rec, dict) and "bot_token" in rec:
                raw_token = str(rec.get("bot_token") or "").strip()
                numeric_id = raw_token.split(":", 1)[0] if ":" in raw_token else ""
                if numeric_id.isdecimal():
                    rec["origin_bot_numeric_id"] = numeric_id
                if raw_token:
                    rec["bot_token"] = REDACTED_SECRET_MASK
                else:
                    rec["bot_token"] = ""

    def _mask_archived_secrets(value: Any) -> None:
        sensitive = {"token", "bot_token", "pushin_pay_token", "gateway_secret", "secret", "password", "api_key", "ai_api_key"}
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in sensitive:
                    value[key] = REDACTED_SECRET_MASK if item else ""
                else:
                    _mask_archived_secrets(item)
        elif isinstance(value, list):
            for item in value:
                _mask_archived_secrets(item)

    _mask_archived_secrets(copy_state.get("memory", {}).get("deleted_leads", []))
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
    elif isinstance(incoming.get("tx_records"), dict) and isinstance(existing.get("tx_records"), dict):
        ex_tx = existing["tx_records"]
        for tid, rec in incoming["tx_records"].items():
            if isinstance(rec, dict) and rec.get("bot_token") == REDACTED_SECRET_MASK:
                old_rec = ex_tx.get(tid)
                if isinstance(old_rec, dict):
                    rec["bot_token"] = old_rec.get("bot_token", "")

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

    if isinstance(existing.get("memory"), dict):
        incoming["memory"] = json.loads(json.dumps(existing["memory"]))

    if isinstance(existing.get("tx_records"), dict):
        incoming["tx_records"] = json.loads(json.dumps(existing["tx_records"]))

    if isinstance(existing.get("learning"), dict):
        incoming["learning"] = json.loads(json.dumps(existing["learning"]))

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
                fb["sales_today"] = int(old_bot.get("sales_today") or 0)
                fb["revenue_today"] = round(float(old_bot.get("revenue_today") or 0.0), 2)
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

    memory = state.setdefault("memory", {"leads": []})
    leads = memory.get("leads", [])
    # Nunca descarte clientes silenciosamente: um novo /start ou uma
    # restauração podem ultrapassar a antiga limitação visual de 500.
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

def _origin_for_lead(lead: Dict[str, Any], state: Dict[str, Any]) -> str:
    numeric = str(lead.get("origin_bot_numeric_id") or "").strip()
    if numeric.isdecimal():
        return numeric
    bot_id = str(lead.get("bot_id") or "").strip()
    # A configured ID can be reassigned: historical records without public
    # numeric origin stay unattributed instead of inheriting the new bot.
    return f"legacy:{bot_id}"


def _origin_for_tx(rec: Dict[str, Any]) -> str:
    numeric = str(rec.get("origin_bot_numeric_id") or "").strip()
    token = str(rec.get("bot_token") or "").split(":", 1)[0]
    if not numeric.isdecimal() and token.isdecimal():
        numeric = token
    return numeric if numeric.isdecimal() else f"legacy:{rec.get('bot_id') or ''}"


async def _hydrate_records(session: AsyncSession, state: Dict[str, Any]) -> Dict[str, Any]:
    clients = (await session.execute(select(ClientRecord))).scalars().all()
    pix = (await session.execute(select(PixRecord))).scalars().all()
    memory = state.setdefault("memory", {})
    memory["leads"] = [json.loads(row.data_json) for row in clients if not row.archived]
    memory["deleted_leads"] = [json.loads(row.data_json) for row in clients if row.archived]
    state["tx_records"] = {row.tx_id: json.loads(row.data_json) for row in pix}
    return state


async def _persist_records(session: AsyncSession, state: Dict[str, Any], delete_tx_ids: Optional[List[str]], delete_client_keys=None) -> None:
    for archived, key in ((False, "leads"), (True, "deleted_leads")):
        for lead in state.get("memory", {}).get(key, []):
            uid = str(lead.get("telegram_id") or "").strip()
            if not uid:
                continue
            origin = _origin_for_lead(lead, state)
            row = await session.get(ClientRecord, (origin, uid, archived))
            data = json.dumps(lead, ensure_ascii=False)
            if row is None:
                session.add(ClientRecord(origin_bot_id=origin, telegram_id=uid, archived=archived, data_json=data))
            elif row.data_json != data:
                row.data_json = data
    for origin, uid, archived in delete_client_keys or []:
        row = await session.get(ClientRecord, (origin, uid, archived))
        if row:
            await session.delete(row)
    for tid in delete_tx_ids or []:
        row = await session.get(PixRecord, str(tid))
        if row:
            await session.delete(row)
    for tid, rec in state.get("tx_records", {}).items():
        if not tid or not isinstance(rec, dict) or str(tid) in (delete_tx_ids or []):
            continue
        row = await session.get(PixRecord, str(tid))
        data = json.dumps(rec, ensure_ascii=False)
        if row is None:
            row = PixRecord(tx_id=str(tid))
            session.add(row)
        if row.data_json != data:
            row.data_json = data
            row.origin_bot_id = _origin_for_tx(rec)
            row.bot_id = str(rec.get("bot_id") or "")
            row.telegram_id = str(rec.get("user_id") or rec.get("telegram_id") or "")
            row.status = str(rec.get("status") or "pending")
            row.amount = float(rec.get("amount") or 0)
            row.is_test = bool(rec.get("is_test") or str(tid).startswith("test_"))
            row.is_upsell = bool(rec.get("is_upsell"))
            row.paid_at = str(rec.get("paid_at") or "")


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
                saved = {}
            else:
                saved = json.loads(config.full_state_json)
            try:
                merged = {**DEFAULT_SHARKBOT_STATE, **saved}
                _STATE_CACHE = _normalize_providers(await _hydrate_records(session, merged))
                return _STATE_CACHE
            except Exception:
                _STATE_CACHE = _normalize_providers(json.loads(json.dumps(DEFAULT_SHARKBOT_STATE)))
                return _STATE_CACHE

async def _write_state_to_db(state: Dict[str, Any], preserve_runtime: bool = False, delete_tx_ids: Optional[List[str]] = None, delete_client_keys=None) -> Dict[str, Any]:
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
                    existing_state = await _hydrate_records(session, json.loads(config.full_state_json))
                except Exception:
                    existing_state = {}

            if preserve_runtime:
                state = _merge_config_preserving_runtime(state, existing_state)
            else:
                state = _preserve_existing_secrets(state, existing_state)
                # Reconciliação monotônica de transações financeiras (Item 2)
                del_set = set(str(x) for x in (delete_tx_ids or []))
                ex_tx_map = existing_state.get("tx_records") if isinstance(existing_state.get("tx_records"), dict) else {}
                st_tx_map = state.setdefault("tx_records", {})

                for ex_id, ex_rec in ex_tx_map.items():
                    if not isinstance(ex_rec, dict) or str(ex_id) in del_set:
                        continue
                    if ex_rec.get("status") == "paid" or ex_rec.get("delivered") or ex_rec.get("sales_counted") is True:
                        st_rec = st_tx_map.get(ex_id)
                        if not st_rec or not isinstance(st_rec, dict):
                            st_tx_map[ex_id] = copy.deepcopy(ex_rec)
                        elif st_rec.get("status") != "paid":
                            st_rec["status"] = "paid"
                            st_rec["paid_at"] = ex_rec.get("paid_at") or st_rec.get("paid_at") or datetime.utcnow().isoformat()
                            if ex_rec.get("sales_counted"):
                                st_rec["sales_counted"] = True
                            if ex_rec.get("delivered"):
                                st_rec["delivered"] = True
                            if ex_rec.get("access_status"):
                                st_rec["access_status"] = ex_rec.get("access_status")
                            if ex_rec.get("invite_url"):
                                st_rec["invite_url"] = ex_rec.get("invite_url")

                        if isinstance(st_rec, dict):
                            # Delivery and accounting markers are monotonic even if
                            # both snapshots already say paid. A stale webhook must
                            # never schedule a second access grant.
                            if ex_rec.get("delivered"):
                                st_rec["delivered"] = True
                            if ex_rec.get("sales_counted"):
                                st_rec["sales_counted"] = True
                            st_rec["delivery_attempts"] = max(
                                int(st_rec.get("delivery_attempts") or 0),
                                int(ex_rec.get("delivery_attempts") or 0),
                            )
                            for field in ("access_status", "invite_url", "delivered_at"):
                                if ex_rec.get(field) and not st_rec.get(field):
                                    st_rec[field] = ex_rec[field]

                ex_learning = existing_state.get("learning") if isinstance(existing_state.get("learning"), dict) else {}
                st_learning = state.setdefault("learning", {})
                if not delete_tx_ids and isinstance(ex_learning, dict):
                    ex_confirmed = int(ex_learning.get("confirmed_purchases") or 0)
                    st_confirmed = int(st_learning.get("confirmed_purchases") or 0)
                    if ex_confirmed > st_confirmed:
                        st_learning["confirmed_purchases"] = ex_confirmed

            # Missing rows in a stale snapshot do not imply deletion. The
            # selected client's rows are removed only by explicit keys.
            deleted_keys = set(delete_client_keys or [])
            for archived, key in ((False, "leads"), (True, "deleted_leads")):
                incoming = state.setdefault("memory", {}).setdefault(key, [])
                known = {(_origin_for_lead(x, state), str(x.get("telegram_id")), archived) for x in incoming}
                for old in existing_state.get("memory", {}).get(key, []):
                    identity = (_origin_for_lead(old, existing_state), str(old.get("telegram_id")), archived)
                    if identity not in known and identity not in deleted_keys:
                        incoming.append(copy.deepcopy(old))
            state = _normalize_providers(state)

            if not config:
                config = BotConfig()
                session.add(config)

            await _persist_records(session, state, delete_tx_ids, delete_client_keys)
            config_state = copy.deepcopy(state)
            config_state.pop("tx_records", None)
            config_state.setdefault("memory", {}).pop("leads", None)
            config_state["memory"].pop("deleted_leads", None)
            config.full_state_json = json.dumps(config_state, ensure_ascii=False)
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
                            if ord_row.status == "paid" and tx_rec.get("status") != "paid":
                                pass
                            else:
                                ord_row.status = str(tx_rec.get("status") or ord_row.status)
                            if ord_row.status == "paid" and not ord_row.paid_at:
                                ord_row.paid_at = datetime.utcnow()
                    except Exception:
                        pass

            if delete_tx_ids:
                order_result = await session.execute(select(Order).where(Order.transaction_id.in_(delete_tx_ids)))
                for order in order_result.scalars().all():
                    await session.delete(order)
            await session.commit()
            _STATE_CACHE = state
        return _STATE_CACHE

async def _startup_warm_medias_task():
    try:
        await bot_manager.configure_and_start(_read_state_from_db, _write_state_to_db)
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
        else:
            # Atomic, idempotent migration of the pre-row aggregate. Keep legacy
            # JSON until both row sets have been committed in the same transaction.
            old = json.loads(config.full_state_json or "{}")
            if "tx_records" in old or "leads" in old.get("memory", {}):
                await _persist_records(session, old, None)
                old.pop("tx_records", None)
                old.setdefault("memory", {}).pop("leads", None)
                old["memory"].pop("deleted_leads", None)
                config.full_state_json = json.dumps(old, ensure_ascii=False)
                await session.commit()

    # Garante criação/leitura do token administrativo no disco se não definido em variável de ambiente
    _get_expected_admin_token()

    # Inicia o polling dos bots e aquecimento de mídias em background para não travar o startup do Uvicorn
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


def _panel_state(state: Dict[str, Any]) -> Dict[str, Any]:
    result = _redact_state_secrets(state)
    result.pop("tx_records", None)
    return result


@app.get("/api/state", dependencies=[Depends(verify_admin_auth)])
async def get_full_state():
    raw_state = await _read_state_from_db()
    reconcile_daily_counters(raw_state)
    return _panel_state(raw_state)

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
        "state": _panel_state(updated_state),
        "updated_at": datetime.utcnow().isoformat()
    }

@app.get("/api/flow-bots/metrics", dependencies=[Depends(verify_admin_auth)])
async def get_flow_metrics(bot_id: str = "all"):
    """Indexed aggregates: no payment payload is fetched or sent to the browser."""
    async with async_session() as session:
        query = select(
            func.count(PixRecord.tx_id),
            func.coalesce(func.sum(PixRecord.amount), 0),
        ).where(PixRecord.status == "paid", PixRecord.is_test == False)
        if bot_id and bot_id != "all":
            state = await _read_state_from_db()
            bot = next((b for b in state.get("flow_bots", []) if str(b.get("id")) == bot_id), None)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot não encontrado.")
            query = query.where(PixRecord.origin_bot_id == str(bot.get("bot_id")))
        paid_count, paid_amount = (await session.execute(query)).one()
        return {"paid_count": paid_count, "paid_amount": round(float(paid_amount), 2)}


@app.get("/api/config", dependencies=[Depends(verify_admin_auth)])
async def get_config_compat():
    raw_state = await _read_state_from_db()
    return _panel_state(raw_state)

@app.post("/api/config", dependencies=[Depends(verify_admin_auth)])
async def post_config_compat(data: Dict[str, Any]):
    return await save_full_state(data)

def _matches_lead_scope(lead: Dict[str, Any], uid: str, bot_id: str, state: Dict[str, Any]) -> bool:
    if str(lead.get("telegram_id")) != uid:
        return False
    if not bot_id or bot_id == "all":
        return True
    bot = next((b for b in state.get("flow_bots", []) if str(b.get("id")) == bot_id), None)
    return bool(bot and _origin_for_lead(lead, state) == str(bot.get("bot_id")))


def _matches_tx_scope(rec: Dict[str, Any], uid: str, bot_id: str, state: Dict[str, Any]) -> bool:
    if str(rec.get("user_id") or rec.get("telegram_id")) != uid:
        return False
    if not bot_id or bot_id == "all":
        return True
    bot = next((b for b in state.get("flow_bots", []) if str(b.get("id")) == bot_id), None)
    return bool(bot and _origin_for_tx(rec) == str(bot.get("bot_id")))


def _archive_lead(archived: List[Dict[str, Any]], lead: Dict[str, Any]) -> None:
    """Arquiva sem perder sessões anteriores se o mesmo usuário voltou ao bot."""
    telegram_id = str(lead.get("telegram_id"))
    old = next((item for item in archived if str(item.get("telegram_id")) == telegram_id and
                str(item.get("origin_bot_numeric_id") or item.get("bot_id") or "") ==
                str(lead.get("origin_bot_numeric_id") or lead.get("bot_id") or "")), None)
    new_record = json.loads(json.dumps(lead))
    new_record["deleted_at"] = datetime.now(timezone.utc).isoformat()
    if old is None:
        archived.append(new_record)
        return
    old_snapshot = json.loads(json.dumps(old))
    earlier = old_snapshot.get("previous_records") or []
    previous_version = {k: v for k, v in old_snapshot.items() if k != "previous_records"}
    new_record["previous_records"] = [*earlier, previous_version]
    old_history = old_snapshot.get("history") if isinstance(old_snapshot.get("history"), list) else []
    new_history = new_record.get("history") if isinstance(new_record.get("history"), list) else []
    new_record["history"] = [*old_history, *new_history]
    old.clear()
    old.update(new_record)


def _cancel_pending_lead_transactions(state: Dict[str, Any], telegram_id: str, bot_id: str = "") -> List[str]:
    """Interrompe monitoramento local no estado desacoplado; não cancela a cobrança no gateway."""
    cancelled: List[str] = []
    for tx_id, record in (state.get("tx_records") or {}).items():
        if not isinstance(record, dict) or not _matches_tx_scope(record, str(telegram_id), bot_id, state):
            continue
        if record.get("status") != "pending" or record.get("delivered"):
            continue
        record["status"] = "cancelled"
        record["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        cancelled.append(str(tx_id))
    return cancelled


@app.delete("/api/leads/{telegram_id}", dependencies=[Depends(verify_admin_auth)])
async def delete_lead(telegram_id: str, bot_id: str = ""):
    clean_uid = str(telegram_id or "").strip()
    if not clean_uid:
        raise HTTPException(status_code=400, detail="ID de lead inválido.")

    current = await _read_state_from_db()
    lead_tx_ids = [
        str(tx_id) for tx_id, rec in (current.get("tx_records") or {}).items()
        if isinstance(rec, dict) and _matches_tx_scope(rec, clean_uid, bot_id, current)
    ]

    async with bot_manager._get_lead_lock(clean_uid):
        locks = [bot_manager._get_tx_lock(tid) for tid in sorted(lead_tx_ids)]
        async with contextlib.AsyncExitStack() as stack:
            for lk in locks:
                await stack.enter_async_context(lk)

            fresh_state = await _read_state_from_db()
            staged = copy.deepcopy(fresh_state)
            memory = staged.setdefault("memory", {"leads": []})
            leads = memory.get("leads", [])
            matched = [lead for lead in leads if _matches_lead_scope(lead, clean_uid, bot_id, staged)]
            if not matched:
                raise HTTPException(status_code=404, detail="Lead não encontrado.")
            archived = memory.setdefault("deleted_leads", [])

            cancelled_tx_ids = _cancel_pending_lead_transactions(staged, clean_uid, bot_id)
            for lead in matched:
                _archive_lead(archived, lead)
            memory["leads"] = [lead for lead in leads if not _matches_lead_scope(lead, clean_uid, bot_id, staged)]
            staged.setdefault("learning", {})["evaluated_results"] = len(memory["leads"])

            # 1. Commit no banco de dados PRIMEIRO (conforme diretriz AGENTS.md)
            removed_keys = [(_origin_for_lead(lead, staged), clean_uid, False) for lead in matched]
            saved_state = await _write_state_to_db(staged, delete_client_keys=removed_keys)

            # 2. Apenas após confirmação no banco, limpa runtime em memória
            bot_manager.cancel_lead_tasks(clean_uid)
            for tx_id in cancelled_tx_ids:
                bot_manager.active_tx_map.pop(tx_id, None)
                poll = bot_manager.pix_poll_tasks.pop(tx_id, None)
                if poll and not poll.done():
                    poll.cancel()
                cached = bot_manager.tx_records.get(tx_id)
                if isinstance(cached, dict) and cached.get("status") != "paid":
                    cached["status"] = "cancelled"
                    cached["cancelled_at"] = datetime.now(timezone.utc).isoformat()

            return {"status": "success", "state": _panel_state(saved_state)}

@app.post("/api/leads/{telegram_id}/restore", dependencies=[Depends(verify_admin_auth)])
async def restore_lead(telegram_id: str, bot_id: str = ""):
    clean_uid = str(telegram_id or "").strip()
    if not clean_uid:
        raise HTTPException(status_code=400, detail="ID de lead inválido.")

    async with bot_manager._get_lead_lock(clean_uid):
        current = await _read_state_from_db()
        staged = copy.deepcopy(current)
        memory = staged.setdefault("memory", {"leads": []})
        archived = memory.setdefault("deleted_leads", [])
        matched = [lead for lead in archived if _matches_lead_scope(lead, clean_uid, bot_id, staged)]
        if not matched:
            raise HTTPException(status_code=404, detail="Lead arquivado não encontrado.")
        leads = memory.setdefault("leads", [])
        if any(_origin_for_lead(lead, staged) == _origin_for_lead(item, staged)
               and str(lead.get("telegram_id")) == clean_uid for lead in leads for item in matched):
            raise HTTPException(status_code=409, detail="Lead ativo já existe.")
        memory["deleted_leads"] = [lead for lead in archived if not _matches_lead_scope(lead, clean_uid, bot_id, staged)]
        leads[:0] = [{key: value for key, value in lead.items() if key != "deleted_at"} for lead in matched]
        # Commit no banco de dados primeiro
        removed_keys = [(_origin_for_lead(lead, staged), clean_uid, True) for lead in matched]
        saved_state = await _write_state_to_db(staged, delete_client_keys=removed_keys)
        return {"status": "success", "state": _panel_state(saved_state)}


def _remove_lead_transactions(state: Dict[str, Any], telegram_id: str, bot_id: str = "") -> List[str]:
    """Exclui lançamentos do painel e desfaz apenas os totais que foram somados."""
    tx_map = state.setdefault("tx_records", {})
    bots = state.get("flow_bots") or []
    removed: List[str] = []
    for tx_id, record in list(tx_map.items()):
        if not isinstance(record, dict) or not _matches_tx_scope(record, str(telegram_id), bot_id, state):
            continue
        was_paid = record.get("status") == "paid" or bool(record.get("delivered"))
        if was_paid:
            learning = state.setdefault("learning", {})
            learning["confirmed_purchases"] = max(0, int(learning.get("confirmed_purchases") or 0) - 1)
            if record.get("mode") == "flow" and not record.get("is_upsell") and record.get("sales_counted") is True:
                # created_at is the Pix creation time, not proof of the payment date.
                paid_at_raw = str(record.get("paid_at") or "").strip()
                try:
                    paid_at = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00"))
                    paid_brt = (paid_at - timedelta(hours=3) if paid_at.tzinfo is None
                                else paid_at.astimezone(timezone(timedelta(hours=-3))))
                    is_paid_today = paid_brt.date() == datetime.now(timezone(timedelta(hours=-3))).date()
                except (ValueError, TypeError):
                    is_paid_today = False
                if is_paid_today:
                    bot_id = str(record.get("bot_id") or "").strip()
                    bot_token = str(record.get("bot_token") or "").strip()
                    origin_id = bot_token.split(":", 1)[0] if ":" in bot_token else ""
                    matches = [b for b in bots if isinstance(b, dict) and origin_id.isdecimal()
                               and (not bot_id or str(b.get("id") or "") == bot_id)
                               and str(b.get("token") or "").startswith(origin_id + ":")]
                    if len(matches) == 1:
                        matched = matches[0]
                        matched["sales_today"] = max(0, int(matched.get("sales_today") or 0) - 1)
                        matched["revenue_today"] = max(0.0, round(float(matched.get("revenue_today") or 0.0) - float(record.get("amount") or 0.0), 2))
        removed.append(tx_id)
        tx_map.pop(tx_id)
    return removed


@app.delete("/api/leads/{telegram_id}/permanent", dependencies=[Depends(verify_admin_auth)])
async def permanently_delete_lead(telegram_id: str, bot_id: str = ""):
    clean_uid = str(telegram_id or "").strip()
    if not clean_uid:
        raise HTTPException(status_code=400, detail="ID de lead inválido.")

    current = await _read_state_from_db()
    lead_tx_ids = [
        str(tx_id) for tx_id, rec in (current.get("tx_records") or {}).items()
        if isinstance(rec, dict) and _matches_tx_scope(rec, clean_uid, bot_id, current)
    ]

    async with bot_manager._get_lead_lock(clean_uid):
        locks = [bot_manager._get_tx_lock(tid) for tid in sorted(lead_tx_ids)]
        async with contextlib.AsyncExitStack() as stack:
            for lk in locks:
                await stack.enter_async_context(lk)

            fresh_state = await _read_state_from_db()
            staged = copy.deepcopy(fresh_state)
            memory = staged.setdefault("memory", {"leads": []})
            archived = memory.get("deleted_leads", [])
            remaining = [lead for lead in archived if not _matches_lead_scope(lead, clean_uid, bot_id, staged)]
            if len(remaining) == len(archived):
                raise HTTPException(status_code=404, detail="Lead arquivado não encontrado.")
            if any(_origin_for_lead(lead, staged) == _origin_for_lead(item, staged)
                   and str(lead.get("telegram_id")) == clean_uid
                   for lead in memory.get("leads", []) for item in archived
                   if _matches_lead_scope(item, clean_uid, bot_id, staged)):
                raise HTTPException(status_code=409, detail="O mesmo cliente voltou ao bot. Exclua o cadastro ativo antes de apagar permanentemente.")
            memory["deleted_leads"] = remaining
            removed_tx_ids = _remove_lead_transactions(staged, clean_uid, bot_id)
            removed_keys = [(_origin_for_lead(lead, staged), clean_uid, True)
                            for lead in archived if _matches_lead_scope(lead, clean_uid, bot_id, staged)]
            saved_state = await _write_state_to_db(staged, delete_tx_ids=removed_tx_ids, delete_client_keys=removed_keys)
            for tx_id in removed_tx_ids:
                bot_manager.tx_records.pop(tx_id, None)
                bot_manager.active_tx_map.pop(tx_id, None)
                task = bot_manager.pix_poll_tasks.pop(tx_id, None)
                if task and not task.done():
                    task.cancel()
            return {"status": "success", "state": _panel_state(saved_state)}


@app.post("/api/leads/{telegram_id}/reset", dependencies=[Depends(verify_admin_auth)])
async def reset_lead_conversation(telegram_id: str):
    clean_uid = str(telegram_id or "").strip()
    if not clean_uid:
        raise HTTPException(status_code=400, detail="ID de lead inválido.")

    async with bot_manager._get_lead_lock(clean_uid):
        current = await _read_state_from_db()
        staged = copy.deepcopy(current)
        memory = staged.setdefault("memory", {"leads": []})
        for lead in memory.get("leads", []):
            if str(lead.get("telegram_id")) == clean_uid:
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
        saved = await _write_state_to_db(staged)
        bot_manager.cancel_lead_tasks(clean_uid)
        return {"status": "success", "state": _panel_state(saved)}

async def clear_all_leads():
    state = await _read_state_from_db()
    memory = state.setdefault("memory", {"leads": []})
    leads = memory.get("leads", [])
    archived = memory.setdefault("deleted_leads", [])
    for lead in leads:
        bot_manager.cancel_lead_tasks(str(lead.get("telegram_id")))
        _cancel_pending_lead_transactions(state, str(lead.get("telegram_id")))
        _archive_lead(archived, lead)
    memory["leads"] = []
    await _write_state_to_db(state)
    return {"status": "success", "state": _panel_state(state)}

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

    # Cobranças locais canceladas por exclusão não devem consultar o gateway
    # novamente nem liberar acesso. O QR externo pode continuar pagável.
    state = await _read_state_from_db()
    tx_record = (state.get("tx_records") or {}).get(transaction_id) or bot_manager.tx_records.get(transaction_id)
    if isinstance(tx_record, dict) and tx_record.get("status") in ("cancelled", "canceled"):
        return {"status": "received", "action": "archived_or_cancelled_tx", "transaction_id": transaction_id}

    # Fail-closed: NUNCA confia apenas no JSON do webhook; consulta o status oficial no gateway
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
            "state": _panel_state(state)
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


class DownsellTestSendRequest(BaseModel):
    bot_id: Optional[str] = ""
    track: str = "general"
    seq_idx: int = -1
    channel_id: Optional[str] = ""


@app.post("/api/flow-bots/downsell/test-send", dependencies=[Depends(verify_admin_auth)])
async def test_send_downsell(req: DownsellTestSendRequest):
    from aiogram import types
    from telegram_service import _create_bot_instance

    state = await _read_state_from_db()
    flow_bots = [b for b in (state.get("flow_bots") or []) if isinstance(b, dict)]
    target_fb = None
    if req.bot_id:
        target_fb = next((b for b in flow_bots if str(b.get("id")) == str(req.bot_id)), None)
    if not target_fb:
        target_fb = bot_manager._get_active_flow_bot(state) or (flow_bots[0] if flow_bots else None)

    if not target_fb:
        raise HTTPException(status_code=404, detail="Nenhum bot encontrado para teste.")

    tok = (
        str(target_fb.get("token") or "").strip()
        or str(state.get("publish", {}).get("bot_token") or "").strip()
        or bot_manager.current_token
        or ""
    )
    if not tok or tok == REDACTED_SECRET_MASK or ":" not in tok:
        raise HTTPException(status_code=400, detail="Token do bot não está configurado.")

    cache_channels = []
    if req.channel_id and str(req.channel_id).strip():
        cache_channels = [str(req.channel_id).strip()]
    else:
        cache_channels = bot_manager._get_candidate_cache_channels(state, target_fb)

    if not cache_channels:
        raise HTTPException(
            status_code=400,
            detail="Nenhum canal de cache configurado. Configure o Canal de Cache na aba Configurações ou Planos deste bot."
        )

    target_channel = cache_channels[0]
    parsed_channel = int(target_channel) if str(target_channel).lstrip("-").isdigit() else str(target_channel)

    temp_bot = None
    bot_inst = bot_manager._get_bot_by_token(tok) if (bot_manager._primary_bot or bot_manager.extra_bots) else None
    if bot_inst and tok not in (bot_manager.current_token, *bot_manager.extra_bots.keys()):
        bot_inst = None
    if not bot_inst:
        temp_bot = _create_bot_instance(tok)
        bot_inst = temp_bot

    try:
        me = await bot_inst.get_me()
        bot_uname = f"@{me.username}" if me and me.username else f"ID {me.id}"

        track_key = "downsell_general" if req.track == "general" else "downsell_pix"
        track_title = "Downsell Geral (Sem gerar PIX)" if req.track == "general" else "Downsell PIX (Gerou e não pagou)"
        ds_obj = target_fb.get(track_key) or {}
        seqs = ds_obj.get("sequences") or []

        if not seqs:
            raise HTTPException(status_code=400, detail=f"Nenhuma sequência cadastrada em {track_title}.")

        if req.seq_idx >= 0:
            if req.seq_idx >= len(seqs):
                raise HTTPException(status_code=400, detail=f"Sequência #{req.seq_idx + 1} não encontrada.")
            to_test = [(req.seq_idx, seqs[req.seq_idx])]
        else:
            to_test = list(enumerate(seqs))

        sent_count = 0
        warmed_medias = 0

        for idx, seq in to_test:
            msg_raw = str(seq.get("message") or "").strip()
            medias = [m for m in (seq.get("medias") or []) if isinstance(m, dict)]
            delay_min = int(seq.get("delay_minutes") or 10)
            disc_str = str(seq.get("discount") or "Sem desconto")
            plan_ids = seq.get("plan_ids")

            # Aquece mídias no canal de cache sob a identidade deste bot específico
            for m in medias:
                fid = await bot_manager.warm_media_item_in_channel(m, state, fb=target_fb, bot_instance=bot_inst)
                if fid:
                    warmed_medias += 1

            fake_user = types.User(id=me.id, is_bot=False, first_name="Cliente", username="cliente")
            rendered_msg = bot_manager._render_flow_vars(
                msg_raw or "⏰ <b>Oferta Especial de Recuperação!</b>\nVocê recebeu uma condição exclusiva para finalizar seu plano agora:",
                fake_user,
                target_fb
            )
            kb = bot_manager._build_flow_plans_kb(target_fb, filter_plan_ids=plan_ids, discount_str=disc_str)

            if not msg_raw and medias:
                attached_kb = await bot_manager._send_flow_medias(
                    chat_id=parsed_channel,
                    medias=medias,
                    reply_markup=kb,
                    bot_instance=bot_inst
                )
                if not attached_kb and kb:
                    try:
                        await bot_inst.send_message(
                            chat_id=parsed_channel,
                            text="👇 <b>Escolha seu plano abaixo:</b>",
                            parse_mode="HTML",
                            reply_markup=kb,
                            disable_notification=True
                        )
                    except Exception:
                        await bot_inst.send_message(
                            chat_id=parsed_channel,
                            text="👇 Escolha seu plano abaixo:",
                            reply_markup=kb,
                            disable_notification=True
                        )
            else:
                if medias:
                    await bot_manager._send_flow_medias(
                        chat_id=parsed_channel,
                        medias=medias,
                        bot_instance=bot_inst
                    )
                try:
                    await bot_inst.send_message(
                        chat_id=parsed_channel,
                        text=rendered_msg,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_notification=True
                    )
                except Exception:
                    await bot_inst.send_message(
                        chat_id=parsed_channel,
                        text=rendered_msg,
                        reply_markup=kb,
                        disable_notification=True
                    )
            sent_count += 1

        # Salva o estado atualizado com os novos file_ids no banco
        await _write_state_to_db(state)

        return {
            "status": "success",
            "bot_username": bot_uname,
            "channel_id": str(target_channel),
            "sent_count": sent_count,
            "warmed_medias": warmed_medias,
            "message": f"{sent_count} sequência(s) disparada(s) no canal de cache pelo bot {bot_uname} com sucesso!"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao disparar teste de downsell: {e}")
        return {"status": "error", "message": f"Falha ao enviar teste ao canal de cache: {e}"}
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
    plans_status_map: Dict[str, Dict[str, Any]] = {}
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
        for idx_p, p in enumerate(plans):
            p_id = str(p.get("id") or f"idx_{idx_p}")
            p_name = p.get("name") or f"Plano {idx_p + 1}"
            dt = p.get("delivery_dest_type") or "Canal ou grupo"
            if dt == "Herdar do fluxo":
                dt = fb_deliv.get("dest_type") or "Canal ou grupo"
            if dt == "Link externo":
                ext = (p.get("delivery_external_url") or fb_deliv.get("external_url") or "").strip()
                if ext and (ext.startswith("http://") or ext.startswith("https://") or ext.startswith("t.me/")):
                    msg_p = f"Link externo válido ({ext})"
                    plan_ok_details.append(f"{p_name}: {msg_p}")
                    plans_status_map[p_id] = {"ok": True, "detail": msg_p}
                else:
                    msg_p = "Link externo vazio ou inválido"
                    plan_errors.append(f"{p_name}: {msg_p}")
                    plans_status_map[p_id] = {"ok": False, "detail": msg_p}
            else:
                raw_ch = (p.get("delivery_channel_id") or "").strip()
                fb_ch = (fb_deliv.get("channel_id") or "").strip()
                ch_id = fb_ch if (not raw_ch or raw_ch == "-1003939135982") and fb_ch else (raw_ch or fb_ch)
                if not ch_id or ch_id == "-1003939135982":
                    msg_p = "ID do Canal VIP não configurado"
                    plan_errors.append(f"{p_name}: {msg_p}")
                    plans_status_map[p_id] = {"ok": False, "detail": msg_p}
                elif temp_bot and bot_me:
                    try:
                        parsed_ch = int(ch_id) if ch_id.lstrip("-").isdigit() else ch_id
                        chat_obj = await temp_bot.get_chat(chat_id=parsed_ch)
                        member = await temp_bot.get_chat_member(chat_id=parsed_ch, user_id=bot_me.id)
                        raw_status = getattr(member, "status", "")
                        st_member = str(getattr(raw_status, "value", raw_status)).lower()
                        is_admin_member = (
                            st_member in ("administrator", "creator", "owner")
                            or "administrator" in st_member
                            or "creator" in st_member
                            or bool(getattr(member, "can_invite_users", False))
                            or bool(getattr(member, "can_post_messages", False))
                        )
                        if is_admin_member:
                            msg_p = f"Admin verificado no canal '{chat_obj.title or ch_id}'"
                            plan_ok_details.append(f"{p_name}: {msg_p}")
                            plans_status_map[p_id] = {"ok": True, "detail": msg_p}
                        else:
                            msg_p = f"Bot não é admin no canal '{chat_obj.title or ch_id}' (status: {st_member})"
                            plan_errors.append(f"{p_name}: {msg_p}")
                            plans_status_map[p_id] = {"ok": False, "detail": msg_p}
                    except Exception as e:
                        msg_p = f"Sem acesso ao canal {ch_id} ({e})"
                        plan_errors.append(f"{p_name}: {msg_p}")
                        plans_status_map[p_id] = {"ok": False, "detail": msg_p}
                else:
                    msg_p = f"Canal {ch_id} configurado"
                    plan_ok_details.append(f"{p_name}: {msg_p}")
                    plans_status_map[p_id] = {"ok": True, "detail": msg_p}

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
    gw_ok = False
    gw_det = ""
    if not gw_tok or gw_tok == REDACTED_SECRET_MASK:
        gw_det = "Chave de API do Gateway PIX não configurada na aba Gateways."
        checks.append({
            "name": "4. Gateway PIX",
            "ok": False,
            "detail": gw_det
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
            gw_det = f"Erro ao validar gateway ({gw_prov}): {e}"
            checks.append({
                "name": "4. Gateway PIX",
                "ok": False,
                "detail": gw_det
            })

    if temp_bot:
        try:
            await temp_bot.session.close()
        except Exception:
            pass

    # 5. Order Bump (opcional, mas se ativo valida título/preço)
    active_bumps = [o for o in (fb.get("order_bumps") or []) if isinstance(o, dict) and o.get("active") is not False]
    if not active_bumps:
        ob_status = {"ok": True, "status": "ok", "detail": "Etapa OK (nenhum Order Bump ativo — segue direto ao PIX)"}
    else:
        bad_bumps = [o.get("title") or "Order Bump" for o in active_bumps if float(o.get("price") or 0) <= 0]
        if bad_bumps:
            ob_status = {"ok": False, "status": "error", "detail": f"Preço inválido em: {', '.join(bad_bumps)}"}
            checks.append({"name": "5. Order Bump", "ok": False, "status": "error", "detail": ob_status["detail"]})
        else:
            ob_status = {"ok": True, "status": "ok", "detail": f"Etapa OK ({len(active_bumps)} Order Bump(s) configurado(s))"}

    # 6. Downsell (Geral e PIX)
    dg = fb.get("downsell_general") or {}
    dp = fb.get("downsell_pix") or {}
    dg_en = bool(dg.get("enabled"))
    dp_en = bool(dp.get("enabled"))
    ds_errors: List[str] = []
    total_ds_seqs = 0
    if dg_en:
        dg_seqs = [s for s in (dg.get("sequences") or []) if isinstance(s, dict)]
        total_ds_seqs += len(dg_seqs)
        if not dg_seqs:
            ds_errors.append("Downsell Geral ativado mas sem nenhuma sequência criada")
        for idx_s, s in enumerate(dg_seqs):
            has_msg = bool((s.get("message") or "").strip())
            has_med = bool(s.get("medias") or s.get("media_url"))
            if not has_msg and not has_med:
                ds_errors.append(f"Downsell Geral #{idx_s + 1}: adicione texto ou mídia na sequência")
    if dp_en:
        dp_seqs = [s for s in (dp.get("sequences") or []) if isinstance(s, dict)]
        total_ds_seqs += len(dp_seqs)
        if not dp_seqs:
            ds_errors.append("Downsell PIX ativado mas sem nenhuma sequência criada")
        for idx_s, s in enumerate(dp_seqs):
            has_msg = bool((s.get("message") or "").strip())
            has_med = bool(s.get("medias") or s.get("media_url"))
            if not has_msg and not has_med:
                ds_errors.append(f"Downsell PIX #{idx_s + 1}: adicione texto ou mídia na sequência")

    if ds_errors:
        ds_status = {"ok": False, "status": "error", "detail": " | ".join(ds_errors)}
        checks.append({"name": "6. Recuperação / Downsell", "ok": False, "status": "error", "detail": ds_status["detail"]})
    elif dg_en or dp_en:
        ds_status = {"ok": True, "status": "ok", "detail": f"Etapa OK ({total_ds_seqs} sequência(s) de recuperação pronta(s))"}
    else:
        ds_status = {"ok": True, "status": "ok", "detail": "Etapa OK (nenhum Downsell ativo)"}

    for c in checks:
        c["status"] = "ok" if c.get("ok") else "error"
    for p_key, p_val in plans_status_map.items():
        p_val["status"] = "ok" if p_val.get("ok") else "error"

    plans_check = checks[2] if len(checks) > 2 else {"ok": False, "status": "error", "detail": ""}
    app_msg_ok = bool((pay_cfg.get("approved_message") or "").strip())
    approved_ok = bool(plans_check.get("ok")) and app_msg_ok
    approved_detail = (
        "Etapa OK · Mensagem de aprovação e entrega verificadas!"
        if approved_ok
        else (plans_check.get("detail") if not plans_check.get("ok") else "Mensagem de pagamento aprovado está vazia na aba Pagamentos.")
    )

    stage_status = {
        "start": checks[0] if len(checks) > 0 else {"ok": False, "status": "error", "detail": ""},
        "welcome": checks[1] if len(checks) > 1 else {"ok": False, "status": "error", "detail": ""},
        "plans": plans_check,
        "plans_map": plans_status_map,
        "orderbump": ob_status,
        "pix": {"ok": gw_ok, "status": "ok" if gw_ok else "error", "detail": gw_det},
        "downsell": ds_status,
        "approved": {
            "ok": approved_ok,
            "status": "ok" if approved_ok else "error",
            "detail": approved_detail
        }
    }

    all_ok = all(bool(c.get("ok")) for c in checks)
    if temp_bot:
        try:
            await temp_bot.session.close()
        except Exception:
            pass
    return {
        "status": "ok",
        "ok": all_ok,
        "all_ok": all_ok,
        "checks": checks,
        "stage_status": stage_status,
        "secret_approve_cmd": f"/aprovar {_get_expected_admin_token()}",
        "secret_downsell_cmd": f"/downsell {_get_expected_admin_token()}",
        "state": _panel_state(state)
    }


def _reset_lead_for_retest(state: Dict[str, Any], user_id: int) -> None:
    if not user_id:
        return
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


def reconcile_daily_counters(state: Dict[str, Any], target_bot_id: Optional[str] = None) -> Dict[str, Any]:
    """Reconcilia os contadores diários do painel (sales_today e revenue_today) a partir das transações em tx_records pagas hoje (horário de Brasília).
    Registros sem data confiável, sem bot_id associado ou purgados do log interno são catalogados como anomalias não reconciliadas.
    Não substitui nem reivindica conciliação bancária externa de extrato.
    """
    now_brt = datetime.now(timezone(timedelta(hours=-3)))
    today_date = now_brt.date()
    tx_map = state.get("tx_records") or {}

    sales_by_bot: Dict[str, int] = {}
    rev_by_bot: Dict[str, float] = {}
    unreconciled_anomalies: List[Dict[str, Any]] = []

    for tid, rec in tx_map.items():
        if not isinstance(rec, dict):
            continue
        if bool(rec.get("is_upsell")):
            continue
        if bool(rec.get("is_test")) or str(tid).startswith("test_"):
            continue
        status = str(rec.get("status") or "")
        delivered = bool(rec.get("delivered"))
        was_paid = (status == "paid" or delivered)
        if not was_paid:
            continue
        if rec.get("attribution_uncertain"):
            unreconciled_anomalies.append({"tx_id": tid, "reason": "uncertain_bot_identity"})
            continue
        if rec.get("sales_counted") is False:
            # An explicit uncounted flag can mean the originating bot is
            # ambiguous. Preserve the paid record without inventing attribution.
            unreconciled_anomalies.append({"tx_id": tid, "reason": "paid_but_not_attributed"})
            continue

        paid_at_raw = str(rec.get("paid_at") or rec.get("created_at") or "").strip()
        if not paid_at_raw:
            unreconciled_anomalies.append({"tx_id": tid, "reason": "missing_timestamp"})
            continue
        try:
            dt_p = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00"))
            if dt_p.tzinfo is None:
                dt_p_brt = dt_p - timedelta(hours=3)
            else:
                dt_p_brt = dt_p.astimezone(timezone(timedelta(hours=-3)))
            if dt_p_brt.date() != today_date:
                continue
        except Exception:
            unreconciled_anomalies.append({"tx_id": tid, "reason": "invalid_timestamp_format"})
            continue

        bid = str(rec.get("bot_id") or "").strip()
        if not bid:
            unreconciled_anomalies.append({
                "tx_id": tid,
                "reason": "unassigned_bot_id",
                "amount": float(rec.get("amount") or 0.0)
            })
            continue

        amt = float(rec.get("amount") or 0.0)
        sales_by_bot[bid] = sales_by_bot.get(bid, 0) + 1
        rev_by_bot[bid] = round(rev_by_bot.get(bid, 0.0) + amt, 2)

    for fb in (state.get("flow_bots") or []):
        if not isinstance(fb, dict):
            continue
        fbid = str(fb.get("id") or "").strip()
        if target_bot_id and target_bot_id != "all" and fbid != target_bot_id:
            continue
        fb["sales_today"] = sales_by_bot.get(fbid, 0)
        fb["revenue_today"] = rev_by_bot.get(fbid, 0.0)

    return {
        "reconciled_date": str(today_date),
        "sales_by_bot": sales_by_bot,
        "revenue_by_bot": rev_by_bot,
        "unreconciled_anomalies": unreconciled_anomalies,
    }


async def delete_flow_transaction(tx_id: str):
    clean_id = (tx_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="ID da transação inválido.")

    async with bot_manager._get_tx_lock(clean_id):
        # 1. Carregar estado original e clonar profundamente para isolamento transacional
        state = await _read_state_from_db()
        staged_state = copy.deepcopy(state)

        tx_map = staged_state.setdefault("tx_records", {})
        tx_rec = tx_map.pop(clean_id, None) or bot_manager.tx_records.get(clean_id)

        if isinstance(tx_rec, dict):
            was_paid = (tx_rec.get("status") == "paid" or bool(tx_rec.get("delivered")))
            amt = float(tx_rec.get("amount") or 0.0)
            target_bot_id = str(tx_rec.get("bot_id") or "").strip()
            target_bot_tok = str(tx_rec.get("bot_token") or "").strip()
            is_upsell = bool(tx_rec.get("is_upsell"))

            # Validar se o pagamento foi realizado hoje (horário de Brasília UTC-3)
            paid_at_raw = str(tx_rec.get("paid_at") or tx_rec.get("created_at") or "").strip()
            is_paid_today = False
            if paid_at_raw:
                try:
                    dt_p = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00"))
                    if dt_p.tzinfo is None:
                        dt_p_brt = dt_p - timedelta(hours=3)
                    else:
                        dt_p_brt = dt_p.astimezone(timezone(timedelta(hours=-3)))
                    now_brt = datetime.now(timezone(timedelta(hours=-3)))
                    is_paid_today = (dt_p_brt.date() == now_brt.date())
                except Exception:
                    is_paid_today = False

            # Prova estrita: apenas desconta se houver registro explícito de que a venda foi contabilizada pelo sistema
            was_counted = bool(tx_rec.get("sales_counted"))

            if was_paid and was_counted:
                # Apenas desconta do contador diário se comprovadamente contabilizada e com pagamento ocorrido hoje (BRT)
                if not is_upsell and is_paid_today:
                    for fb in (staged_state.get("flow_bots") or []):
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
                learning = staged_state.setdefault("learning", {})
                learning["confirmed_purchases"] = max(0, int(learning.get("confirmed_purchases") or 0) - 1)

            uid = int(tx_rec.get("user_id") or 0)
            if uid:
                _reset_lead_for_retest(staged_state, uid)

        # 2. Exclusão unificada e atômica em transação única no banco de dados (Order + BotConfig JSON)
        try:
            staged_state = await _write_state_to_db(staged_state, preserve_runtime=False, delete_tx_ids=[clean_id])
        except Exception as e:
            logger.error(f"Erro ao persistir exclusão atômica de {clean_id}: {e}")
            raise HTTPException(status_code=500, detail="Falha ao excluir transação no banco de dados.")

        # 4. Limpar referências voláteis em memória e cancelar polling ativo
        bot_manager.tx_records.pop(clean_id, None)
        bot_manager.active_tx_map.pop(clean_id, None)
        poll_t = bot_manager.pix_poll_tasks.pop(clean_id, None)
        if poll_t and not poll_t.done():
            poll_t.cancel()
        if uid:
            bot_manager.cancel_lead_tasks(str(uid))

        return {"status": "ok", "deleted_tx_id": clean_id, "state": _panel_state(staged_state)}


class ClearTestDataRequest(BaseModel):
    bot_id: Optional[str] = ""
    clear_all_sales: bool = False


@app.post("/api/flow-bots/clear-test-data", dependencies=[Depends(verify_admin_auth)])
async def clear_flow_test_data(req: ClearTestDataRequest):
    if req.clear_all_sales:
        raise HTTPException(status_code=400, detail="Exclusão em massa de vendas desabilitada.")
    state = await _read_state_from_db()
    tx_map = state.setdefault("tx_records", {})
    target_bot_id = (req.bot_id or "").strip()
    removed_ids: List[str] = []
    affected_users: List[int] = []

    for tid, rec in list(tx_map.items()):
        if not isinstance(rec, dict):
            continue
        rec_bot_id = str(rec.get("bot_id") or "").strip()
        matches_bot = (not target_bot_id or target_bot_id == "all" or rec_bot_id == target_bot_id)
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
                paid_at_raw = str(rec.get("paid_at") or rec.get("created_at") or "").strip()
                is_paid_today = False
                if paid_at_raw:
                    try:
                        dt_p = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00"))
                        if dt_p.tzinfo is None:
                            dt_p_brt = dt_p - timedelta(hours=3)
                        else:
                            dt_p_brt = dt_p.astimezone(timezone(timedelta(hours=-3)))
                        now_brt = datetime.now(timezone(timedelta(hours=-3)))
                        is_paid_today = (dt_p_brt.date() == now_brt.date())
                    except Exception:
                        is_paid_today = False
                was_counted = bool(rec.get("sales_counted"))
                if was_counted and is_paid_today:
                    for fb in (state.get("flow_bots") or []):
                        if isinstance(fb, dict) and (not rec_bot_id or str(fb.get("id")) == rec_bot_id):
                            fb["sales_today"] = max(0, int(fb.get("sales_today") or 0) - 1)
                            fb["revenue_today"] = max(0.0, round(float(fb.get("revenue_today") or 0.0) - amt, 2))
                            break
                if was_counted:
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

    state = await _write_state_to_db(state, preserve_runtime=False, delete_tx_ids=removed_ids)
    return {
        "status": "ok",
        "removed_count": len(removed_ids),
        "state": _panel_state(state)
    }


class AccessResendRequest(BaseModel):
    tx_id: Optional[str] = ""
    tx_ids: Optional[List[str]] = None
    bot_id: Optional[str] = ""
    group_id: Optional[str] = ""
    plan_name: Optional[str] = ""


@app.post("/api/flow-bots/access/resend", dependencies=[Depends(verify_admin_auth)])
async def resend_vip_access(req: AccessResendRequest):
    from datetime import timedelta
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from telegram_service import _create_bot_instance

    state = await _read_state_from_db()
    tx_map = state.get("tx_records") or {}

    single_tid = (req.tx_id or "").strip()
    if isinstance(req.tx_ids, list):
        candidate_ids = [str(x).strip() for x in req.tx_ids if str(x).strip()]
    elif single_tid and single_tid != "all":
        candidate_ids = [single_tid]
    else:
        candidate_ids = [
            tid for tid, rec in tx_map.items()
            if isinstance(rec, dict)
            and (not req.bot_id or req.bot_id == "all" or str(rec.get("bot_id") or "") == req.bot_id)
            and (
                not req.group_id
                or req.group_id == "all"
                or str(rec.get("delivery_channel_id") or "") == req.group_id
                or str(rec.get("delivery_channel_name") or "") == req.group_id
            )
            and (
                not req.plan_name
                or req.plan_name == "all"
                or str(rec.get("offer_name") or "") == req.plan_name
            )
        ]

    sent_count = 0
    skipped_unpaid = 0
    for tid in candidate_ids:
        rec = tx_map.get(tid)
        if not isinstance(rec, dict):
            continue
        # Regra estrita: apenas transações efetivamente pagas podem receber link de acesso
        if not (rec.get("status") == "paid" or rec.get("delivered")):
            skipped_unpaid += 1
            continue
        uid = int(rec.get("user_id") or 0)
        if not uid:
            continue
        rec_bot_id = str(rec.get("bot_id") or "").strip()
        rec_token = str(rec.get("bot_token") or "").strip()
        if not rec_token or rec_token == REDACTED_SECRET_MASK or ":" not in rec_token:
            continue  # config ID alone cannot prove the purchase's original bot
        fb = None
        for candidate in (state.get("flow_bots") or []):
            if not isinstance(candidate, dict):
                continue
            config_token = str(candidate.get("token") or "").strip()
            if rec_bot_id:
                matches = str(candidate.get("id") or "") == rec_bot_id
            else:
                matches = bool(rec_token and rec_token != REDACTED_SECRET_MASK and config_token == rec_token)
            if matches:
                fb = candidate
                break
        if not fb:
            continue
        tok = str(fb.get("token") or "").strip()
        if not tok or tok == REDACTED_SECRET_MASK:
            continue
        if rec_token and rec_token != REDACTED_SECRET_MASK:
            if rec_token != tok and rec_token.split(":", 1)[0] != tok.split(":", 1)[0]:
                continue  # identity mismatch: paid purchase requires manual correction, not a blind send

        temp_bot = None
        try:
            temp_bot = _create_bot_instance(tok)
            eff_type = rec.get("delivery_dest_type") or "Canal ou grupo"
            ch_target = (rec.get("delivery_channel_id") or "").strip()
            ext_url = (rec.get("delivery_external_url") or "").strip()
            invite_url = ""

            if eff_type == "Link externo" and ext_url:
                invite_url = ext_url
            elif ch_target:
                try:
                    parsed_ch = int(ch_target) if ch_target.lstrip("-").isdigit() else ch_target
                    inv = await temp_bot.create_chat_invite_link(
                        chat_id=parsed_ch,
                        member_limit=1,
                        expire_date=datetime.now(timezone.utc) + timedelta(minutes=15)
                    )
                    invite_url = (inv.invite_link or "").strip()
                except Exception:
                    invite_url = ""

            if not invite_url or invite_url == "https://t.me/+link_exclusivo_vip":
                continue

            btn_txt = ((fb.get("payments") or {}).get("access_button_text") if isinstance(fb, dict) else None) or "Acessar conteúdo"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=btn_txt, url=invite_url)]
            ])
            plan_label = rec.get("offer_name") or "Plano VIP"
            safe_plan = html.escape(str(plan_label))
            resend_text = f"🔑 <b>Seu acesso ao plano {safe_plan} foi reenviado!</b>\n\nClique no botão abaixo para acessar:"
            try:
                await temp_bot.send_message(
                    chat_id=uid,
                    text=resend_text,
                    parse_mode="HTML",
                    reply_markup=kb
                )
            except Exception:
                await temp_bot.send_message(
                    chat_id=uid,
                    text=f"🔑 Seu acesso ao plano {plan_label} foi reenviado!\n\nClique no botão abaixo para acessar:",
                    reply_markup=kb
                )
            rec["invite_url"] = invite_url
            rec["delivered"] = True
            rec["access_status"] = "Ativo"
            sent_count += 1
        except Exception:
            pass
        finally:
            if temp_bot:
                try:
                    await temp_bot.session.close()
                except Exception:
                    pass

    if sent_count > 0:
        state = await _write_state_to_db(state, preserve_runtime=False)
    return {
        "status": "ok",
        "sent_count": sent_count,
        "skipped_unpaid": skipped_unpaid,
        "state": _panel_state(state)
    }


class AccessStatusUpdateRequest(BaseModel):
    tx_id: str
    access_status: Optional[str] = ""
    status: Optional[str] = ""


@app.post("/api/flow-bots/access/update-status", dependencies=[Depends(verify_admin_auth)])
async def update_vip_access_status(req: AccessStatusUpdateRequest):
    from telegram_service import _create_bot_instance
    state = await _read_state_from_db()
    tx_map = state.get("tx_records") or {}
    rec = tx_map.get(req.tx_id)
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="Registro de acesso não encontrado.")

    resolved_status = (req.access_status or req.status or "").strip()
    if not resolved_status:
        raise HTTPException(status_code=400, detail="Informe o novo status de acesso.")

    rec["access_status"] = resolved_status
    if resolved_status == "Removido":
        fb_match = next(
            (b for b in (state.get("flow_bots") or []) if isinstance(b, dict) and str(b.get("id")) == str(rec.get("bot_id") or "")),
            None
        )
        ch_target = (
            str(rec.get("delivery_channel_id") or "").strip()
            or (str((fb_match.get("fallback_delivery") or {}).get("channel_id") or "").strip() if isinstance(fb_match, dict) else "")
        )
        uid = int(rec.get("user_id") or 0)
        tok = (
            str(rec.get("bot_token") or "").strip()
            or (str(fb_match.get("token") or "").strip() if isinstance(fb_match, dict) else "")
            or str(state.get("publish", {}).get("bot_token") or "").strip()
        )
        if ch_target and uid and tok and tok != REDACTED_SECRET_MASK:
            temp_bot = None
            try:
                temp_bot = _create_bot_instance(tok)
                parsed_ch = int(ch_target) if ch_target.lstrip("-").isdigit() else ch_target
                await temp_bot.ban_chat_member(chat_id=parsed_ch, user_id=uid)
            except Exception:
                pass
            finally:
                if temp_bot:
                    try:
                        await temp_bot.session.close()
                    except Exception:
                        pass

    state = await _write_state_to_db(state, preserve_runtime=False)
    return {"status": "ok", "access_status": resolved_status, "state": _panel_state(state)}


def _crm_verified_bot_origin(lead: Dict[str, Any]) -> str:
    """Only an immutable identity captured on this lead can prove its origin."""
    recorded = str(lead.get("origin_bot_numeric_id") or "")
    return recorded if recorded.isdecimal() else ""


class LeadActionRequest(BaseModel):
    telegram_id: Optional[str] = ""
    user_id: Optional[str] = ""
    bot_id: Optional[str] = ""
    origin_bot_numeric_id: Optional[str] = ""
    action: str  # "send_message" | "toggle_block"
    message: Optional[str] = ""


@app.post("/api/flow-bots/leads/action", dependencies=[Depends(verify_admin_auth)])
async def handle_lead_action(req: LeadActionRequest):
    from telegram_service import _create_bot_instance
    state = await _read_state_from_db()
    leads = (state.get("memory") or {}).get("leads") or []
    tid = str(req.telegram_id or req.user_id or "").strip()
    requested_bot_id = str(req.bot_id or "").strip()
    requested_origin_id = str(req.origin_bot_numeric_id or "").strip()
    matches = [
        lead for lead in leads
        if str(lead.get("telegram_id") or lead.get("user_id") or "") == tid
        and (not requested_bot_id or str(lead.get("bot_id") or "") == requested_bot_id)
        and (not requested_origin_id or str(lead.get("origin_bot_numeric_id") or "") == requested_origin_id)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Lead não encontrado para este bot.")
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Lead ambíguo: selecione o bot de origem.")
    target_lead = matches[0]

    if req.action == "toggle_block":
        target_lead["blocked"] = not bool(target_lead.get("blocked", False))
        state = await _write_state_to_db(state, preserve_runtime=False)
        return {"status": "ok", "blocked": target_lead["blocked"], "state": _panel_state(state)}

    if req.action == "send_message":
        txt = (req.message or "").strip()
        if not txt:
            return {"status": "error", "message": "Mensagem vazia."}
        lead_bot_id = str(target_lead.get("bot_id") or "").strip()
        if not lead_bot_id:
            return {"status": "error", "message": "Bot de origem não identificado para este lead."}
        fb = next(
            (b for b in (state.get("flow_bots") or [])
             if isinstance(b, dict) and str(b.get("id") or "") == lead_bot_id),
            None
        )
        if not fb:
            return {"status": "error", "message": "Bot de origem não encontrado."}
        tok = str(fb.get("token") or "").strip()
        if not tok or tok == REDACTED_SECRET_MASK:
            return {"status": "error", "message": "Token do bot não configurado."}
        configured_bot_numeric_id = tok.split(":", 1)[0] if ":" in tok else ""
        recorded_bot_numeric_id = _crm_verified_bot_origin(target_lead)
        if (not configured_bot_numeric_id.isdecimal()
                or recorded_bot_numeric_id != configured_bot_numeric_id):
            return {"status": "error", "message": "Identidade do bot de origem não verificável para este lead."}
        temp_bot = None
        try:
            temp_bot = _create_bot_instance(tok)
            try:
                await temp_bot.send_message(chat_id=int(tid), text=txt, parse_mode="HTML")
            except Exception:
                await temp_bot.send_message(chat_id=int(tid), text=txt)
            target_lead.setdefault("history", []).append({"role": "assistant", "content": txt})
            state = await _write_state_to_db(state, preserve_runtime=False)
            return {"status": "ok", "state": _panel_state(state)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            if temp_bot:
                try:
                    await temp_bot.session.close()
                except Exception:
                    pass

    return {"status": "ok", "state": _panel_state(state)}


class RemarketingSendRequest(BaseModel):
    campaign_id: str
    bot_id: Optional[str] = "all"
    segment: Optional[str] = "Pendentes"
    message: Optional[str] = ""
    include_plans: Optional[bool] = True
    discount_pct: Optional[int] = 0


@app.post("/api/flow-bots/remarketing/send", dependencies=[Depends(verify_admin_auth)])
async def send_remarketing_campaign(req: RemarketingSendRequest):
    from aiogram import types
    from telegram_service import _create_bot_instance

    state = await _read_state_from_db()
    campaigns = state.setdefault("remarketing_campaigns", [])
    camp = next((c for c in campaigns if isinstance(c, dict) and str(c.get("id")) == str(req.campaign_id)), None)
    if not camp:
        raise HTTPException(status_code=404, detail="Campanha não encontrada.")

    target_bot_id = str(camp.get("bot_id") or req.bot_id or "all").strip()
    segment = str(camp.get("segment") or req.segment or "all").strip().lower()
    disc_val = int(camp.get("discount_pct") or req.discount_pct or 0)
    discount_str = f"{disc_val}%" if disc_val > 0 else str(camp.get("discount") or "Sem desconto").strip()
    include_buttons = bool(camp.get("include_plans", camp.get("include_plan_buttons", True)))
    msg_tmpl = (camp.get("message") or req.message or "").strip()

    flow_bots_list = [b for b in (state.get("flow_bots") or []) if isinstance(b, dict)]

    def verified_lead_bot(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        lead_bot_id = str(lead.get("bot_id") or "").strip()
        if not lead_bot_id or (target_bot_id != "all" and lead_bot_id != target_bot_id):
            return None
        matches = [b for b in flow_bots_list if str(b.get("id") or "") == lead_bot_id]
        if len(matches) != 1:
            return None
        config = matches[0]
        tok = str(config.get("token") or "").strip()
        numeric_id = tok.split(":", 1)[0] if ":" in tok else ""
        if not numeric_id.isdecimal() or numeric_id != _crm_verified_bot_origin(lead):
            return None
        return config

    leads = (state.get("memory") or {}).get("leads") or []
    paid_stages = ("Pagamento aprovado", "Entrega concluída", "Compra aprovada", "Comprou upsell")
    pending_stages = ("Gerou checkout", "Escolheu um plano", "Escolheu upsell")

    matched_leads: List[Dict[str, Any]] = []
    for l in leads:
        if not isinstance(l, dict) or l.get("blocked") or not verified_lead_bot(l):
            continue
        st = str(l.get("stage") or "Conversando")
        is_paid = st in paid_stages or bool(l.get("purchased"))
        is_pend = st in pending_stages or "pix" in st.lower() or "aguardando" in st.lower()
        if segment in ("paid", "pagos") and not is_paid:
            continue
        if segment in ("pending", "pendentes") and not is_pend:
            continue
        if segment in ("new", "novos") and (is_paid or is_pend):
            continue
        if segment == "unpaid" and is_paid:
            continue
        matched_leads.append(l)

    temp_bots: Dict[str, Any] = {}
    sent_ok = 0
    disc_pct = bot_manager._parse_discount_pct(discount_str)
    try:
        for l in matched_leads:
            uid_str = str(l.get("telegram_id") or l.get("user_id") or "").strip()
            if not uid_str.lstrip("-").isdigit():
                continue
            uid = int(uid_str)

            lead_fb = verified_lead_bot(l)
            if not lead_fb:
                continue
            lead_tok = str(lead_fb.get("token") or "").strip()
            if not lead_tok or lead_tok == REDACTED_SECRET_MASK:
                continue

            bot_inst = temp_bots.get(lead_tok)
            if not bot_inst:
                bot_inst = _create_bot_instance(lead_tok)
                temp_bots[lead_tok] = bot_inst

            kb = bot_manager._build_flow_plans_kb(lead_fb, discount_str=discount_str) if (include_buttons and isinstance(lead_fb, dict)) else None
            try:
                if disc_pct > 0:
                    auth_d = l.setdefault("authorized_flow_discounts", [])
                    if disc_pct not in auth_d:
                        auth_d.append(disc_pct)
                fake_user = types.User(id=uid, is_bot=False, first_name=l.get("first_name") or "Cliente")
                rendered = bot_manager._render_flow_vars(msg_tmpl or "🔥 Oferta especial liberada para você!", fake_user, lead_fb or {})
                try:
                    await bot_inst.send_message(chat_id=uid, text=rendered, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    await bot_inst.send_message(chat_id=uid, text=rendered, reply_markup=kb)
                l["from_remarketing"] = True
                l.setdefault("history", []).append({"role": "assistant", "content": f"[Remarketing: {camp.get('name', 'Campanha')}] {rendered}"})
                sent_ok += 1
                await _asyncio.sleep(0.05)
            except Exception as e:
                err_s = str(e).lower()
                if any(x in err_s for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")):
                    l["blocked"] = True
                    l["stage"] = "Bloqueou o bot"
                elif "retry after" in err_s or "too many requests" in err_s or "flood" in err_s:
                    raw_wait = getattr(e, "retry_after", None)
                    if isinstance(raw_wait, (int, float)):
                        retry_wait = float(raw_wait)
                    else:
                        m = re.search(r"retry\s+(?:after|in)?\s*(\d+)", err_s)
                        retry_wait = float(m.group(1)) if m else 3.0
                    retry_wait = min(max(1.0, retry_wait), 15.0)
                    await _asyncio.sleep(retry_wait)
                    try:
                        await bot_inst.send_message(chat_id=uid, text=rendered, reply_markup=kb)
                        sent_ok += 1
                    except Exception as e_after:
                        err_sa = str(e_after).lower()
                        if any(x in err_sa for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")):
                            l["blocked"] = True
                            l["stage"] = "Bloqueou o bot"
                else:
                    pass
    finally:
        for b_inst in temp_bots.values():
            try:
                await b_inst.session.close()
            except Exception:
                pass

    camp["sent_count"] = int(camp.get("sent_count") or 0) + sent_ok
    camp["total_targets"] = len(matched_leads)
    camp["status"] = "Finalizada"
    camp["last_sent_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    state = await _write_state_to_db(state, preserve_runtime=False)
    return {
        "status": "ok",
        "sent_count": sent_ok,
        "total_targets": len(matched_leads),
        "target_count": len(matched_leads),
        "state": _panel_state(state)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))



