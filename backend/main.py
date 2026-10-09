import os
from dotenv import load_dotenv

load_dotenv()

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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlite_policy import create_application_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func, case, update, delete, and_

from models import Base, BotConfig, Offer, MediaAsset, DownsellStep, UpsellConfig, Lead, Order, ClientRecord, PixRecord, RemarketingDelivery
from pushin_pay import PushinPayClient, parse_gateway_cents
from ai_engine import generate_ai_reply
from telegram_service import bot_manager

_DEFAULT_DB_PATH = "/var/data/hotbot.db" if os.path.isdir("/var/data") and os.access("/var/data", os.W_OK) else "./hotbot.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}")

engine = create_application_engine(DATABASE_URL, echo=False)
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
    _mask_archived_secrets(copy_state.get("saved_profiles", []))
    return copy_state


def _preserve_existing_secrets(incoming: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """Preserva segredos já salvos no banco quando o frontend envia a máscara REDACTED_SECRET_MASK."""
    profile_id = str(incoming.pop("restore_profile_id", "") or "")
    save_profile_id = str(incoming.pop("save_profile_id", "") or "")
    if not existing:
        return incoming
    inc_pub = incoming.setdefault("publish", {})
    ex_pub = existing.get("publish", {})
    selected_profile = next((p.get("snapshot") for p in existing.get("saved_profiles", [])
                             if isinstance(p, dict) and str(p.get("id")) == profile_id), None)
    profile_pub = selected_profile.get("publish", {}) if isinstance(selected_profile, dict) else {}
    profile_acc = selected_profile.get("account", {}) if isinstance(selected_profile, dict) else {}
    for key in ("bot_token", "pushin_pay_token", "gateway_secret"):
        val = (inc_pub.get(key) or "").strip()
        if val == REDACTED_SECRET_MASK:
            inc_pub[key] = profile_pub.get(key) or ex_pub.get(key, "")

    inc_acc = incoming.setdefault("account", {})
    ex_acc = existing.get("account", {})
    if (inc_acc.get("ai_api_key") or "").strip() == REDACTED_SECRET_MASK:
        inc_acc["ai_api_key"] = profile_acc.get("ai_api_key") or ex_acc.get("ai_api_key", "")

    # Profile snapshots returned to browsers contain masks. Match only stable
    # profile IDs, so a renamed/overwritten snapshot does not lose credentials.
    old_profiles = {str(p.get("id")): p.get("snapshot") for p in existing.get("saved_profiles", [])
                    if isinstance(p, dict) and p.get("id") and isinstance(p.get("snapshot"), dict)}
    def restore_masks(value, old):
        if isinstance(value, dict) and isinstance(old, dict):
            for key, item in value.items():
                if item == REDACTED_SECRET_MASK and key in old:
                    value[key] = old[key]
                else:
                    restore_masks(item, old.get(key))
        elif isinstance(value, list) and isinstance(old, list):
            old_by_id = {str(item["id"]): item for item in old
                         if isinstance(item, dict) and item.get("id") is not None}
            for index, item in enumerate(value):
                if isinstance(item, dict) and item.get("id") is not None:
                    original = old_by_id.get(str(item["id"]))
                else:
                    original = old[index] if index < len(old) else None
                restore_masks(item, original)
    for profile in incoming.get("saved_profiles", []):
        if isinstance(profile, dict) and isinstance(profile.get("snapshot"), dict):
            restore_masks(profile["snapshot"], old_profiles.get(str(profile.get("id"))))
            if str(profile.get("id")) == save_profile_id:
                snapshot = profile["snapshot"]
                for section, keys, live in (("publish", ("bot_token", "pushin_pay_token", "gateway_secret"), inc_pub),
                                             ("account", ("ai_api_key",), inc_acc)):
                    fields = snapshot.get(section)
                    if isinstance(fields, dict):
                        for key in keys:
                            if key in fields:
                                fields[key] = live.get(key, "")

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
            matched = ex_by_key.get(key)
            if matched and any(m.get(field) != matched.get(field) for field in ('type', 'name')):
                matched = None
            if matched and matched.get("telegram_file_id"):
                m["telegram_file_id"] = str(matched["telegram_file_id"])
                if matched.get("cached_channel_id"):
                    m["cached_channel_id"] = str(matched["cached_channel_id"])
                if str(matched.get("url") or "").startswith("tg://"):
                    m["url"] = matched["url"]


def _preserve_campaign_progress(incoming: Dict[str, Any], existing: Dict[str, Any]) -> None:
    """Never replace live delivery metadata with an older config snapshot."""
    live_campaigns = {str(c.get('id')): c for c in existing.get('remarketing_campaigns', [])
                      if isinstance(c, dict) and c.get('id')}
    for campaign in incoming.get('remarketing_campaigns', []):
        if not isinstance(campaign, dict):
            continue
        live = live_campaigns.get(str(campaign.get('id')))
        if live and (live.get('deliveries') or live.get('run_id')):
            if campaign.get('run_id') != live.get('run_id'):
                campaign['deliveries'] = {}
            campaign['deliveries'] = {**campaign.get('deliveries', {}), **copy.deepcopy(live.get('deliveries', {}))}
            for field in ('sent_count', 'total_targets', 'status', 'last_sent_at', 'run_id', 'run_ids', 'run_message', 'retry_not_before'):
                if field in live:
                    campaign[field] = copy.deepcopy(live[field])


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

    _preserve_campaign_progress(incoming, existing)

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
                if fb.get("token") != old_bot.get("token"):
                    continue  # Telegram file IDs do not belong to a replacement bot.
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
_STATE_COMMITTED: Dict[str, Any] = {}
_STATE_COMMITTED_OWNER = None
_DB_LOCK = _asyncio.Lock()


class _FrozenDict(dict):
    """Private, recursively immutable SQL ancestry; never a live cache row."""
    def _readonly(self, *args, **kwargs):
        raise TypeError('Committed state is immutable')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, memo):
        # Explicit copies remain ordinary editable data. Snapshot ancestry itself
        # is shared directly, never thawed by _StateSnapshot.__deepcopy__.
        return copy.deepcopy(dict(self), memo)


class _FrozenList(list):
    def _readonly(self, *args, **kwargs):
        raise TypeError('Committed state is immutable')
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly

    def __deepcopy__(self, memo):
        return copy.deepcopy(list(self), memo)


def _freeze(value):
    if isinstance(value, (_FrozenDict, _FrozenList)):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(child) for child in value)
    return value


def _config_view(state):
    """Project before copying/serializing: row payloads are not configuration."""
    result = {key: value for key, value in state.items() if key != 'tx_records'}
    result['memory'] = {key: value for key, value in state.get('memory', {}).items()
                        if key not in ('leads', 'deleted_leads')}
    return result


def _capture_publication(state):
    """Freeze only dirty rows; unchanged immutable committed rows are reusable."""
    baseline = getattr(state, '_row_base', None) or {}
    old_clients = _client_map(baseline)
    old_pix = baseline.get('tx_records', {})
    result = _StateSnapshot(_freeze(_config_view(state)))
    result['memory'] = _FrozenDict({**result.get('memory', {}), **{
        key: _FrozenList(old_clients[identity] if isinstance(old_clients.get(identity), _FrozenDict)
                         and lead == old_clients[identity] else _freeze(lead)
                         for lead in state.get('memory', {}).get(key, [])
                         for identity in [(_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived)])
        for archived, key in ((False, 'leads'), (True, 'deleted_leads'))}})
    result['tx_records'] = _FrozenDict({tid: old_pix[tid]
        if isinstance(old_pix.get(tid), _FrozenDict) and rec == old_pix[tid] else _freeze(rec)
        for tid, rec in state.get('tx_records', {}).items()})
    result._row_base = baseline
    result._pending_client_deletes = getattr(state, '_pending_client_deletes', frozenset())
    result._pending_pix_deletes = getattr(state, '_pending_pix_deletes', frozenset())
    result._seen_client_keys = getattr(state, '_seen_client_keys', frozenset())
    return result


class _StateSnapshot(dict):
    """Keep a copy's row ancestry for three-way merging of stale writers."""

    _row_base: Optional[Dict[str, Any]] = None
    _pending_client_deletes = frozenset()
    _pending_pix_deletes = frozenset()
    _seen_client_keys = frozenset()

    def _capture_client_presence(self):
        # Published cache-only admissions are real observed rows, even before
        # their first SQL commit. Detached, newly staged admissions are not.
        # Keep this evidence on the snapshot, never in global tombstones.
        if self is _STATE_CACHE:
            self._seen_client_keys |= frozenset(_client_map(self))

    def __deepcopy__(self, memo):
        self._capture_client_presence()
        result = type(self)(copy.deepcopy(dict(self), memo))
        memo[id(self)] = result
        # Immutable committed ancestry may be shared; mutable cache rows may not.
        result._row_base = _freeze(self._row_base if self._row_base is not None else dict(self))
        result._pending_client_deletes = self._pending_client_deletes
        result._pending_pix_deletes = self._pending_pix_deletes
        result._seen_client_keys = self._seen_client_keys
        return result
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
    numeric = str(rec.get("origin_bot_id") or rec.get("origin_bot_numeric_id") or "").strip()
    token = str(rec.get("bot_token") or "").split(":", 1)[0]
    if not numeric.isdecimal() and token.isdecimal():
        numeric = token
    if numeric:
        return numeric
    return f"legacy:{rec.get('bot_id') or ''}"


async def _hydrate_records(session: AsyncSession, state: Dict[str, Any]) -> Dict[str, Any]:
    clients = (await session.execute(select(ClientRecord))).scalars().all()
    pix = (await session.execute(select(PixRecord))).scalars().all()
    memory = state.setdefault("memory", {})
    memory["leads"] = [json.loads(row.data_json) for row in clients if not row.archived]
    memory["deleted_leads"] = [json.loads(row.data_json) for row in clients if row.archived]
    state["tx_records"] = {row.tx_id: json.loads(row.data_json) for row in pix}
    return state


def _client_map(state):
    return {
        (_origin_for_lead(lead, state), str(lead['telegram_id']), archived): lead
        for archived, key in ((False, 'leads'), (True, 'deleted_leads'))
        for lead in state.get('memory', {}).get(key, [])
        if lead.get('telegram_id')
    }


async def _persist_records(session: AsyncSession, state: Dict[str, Any], delete_tx_ids: Optional[List[str]], delete_client_keys=None, *, dirty_clients=None, dirty_pix=None):
    persisted_clients, persisted_pix = {}, {}
    client_baseline = _client_map(getattr(state, '_row_base', None) or _STATE_COMMITTED or {})
    for archived, key in ((False, "leads"), (True, "deleted_leads")):
        for lead in state.get("memory", {}).get(key, []):
            uid = str(lead.get("telegram_id") or "").strip()
            if not uid:
                continue
            origin = _origin_for_lead(lead, state)
            if dirty_clients is not None and (origin, uid, archived) not in dirty_clients:
                continue
            row = await session.get(ClientRecord, (origin, uid, archived))
            if row is not None:
                # Merge offer authorization against the durable row, not a worker cache.
                persisted = json.loads(row.data_json)
                previous = client_baseline.get((origin, uid, archived), {})
                removed = set(previous.get('authorized_flow_discounts', [])) - set(lead.get('authorized_flow_discounts', []))
                discounts = [d for d in persisted.get('authorized_flow_discounts', []) if d not in removed]
                if discounts:
                    lead['authorized_flow_discounts'] = list(dict.fromkeys(
                        lead.get('authorized_flow_discounts', []) + discounts))
            data = json.dumps(lead, ensure_ascii=False)
            persisted_clients[(origin, uid, archived)] = json.loads(data)
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
        if dirty_pix is not None and str(tid) not in dirty_pix:
            continue
        row = await session.get(PixRecord, str(tid))
        data = json.dumps(rec, ensure_ascii=False)
        persisted_pix[str(tid)] = json.loads(data)
        if row is None:
            row = PixRecord(tx_id=str(tid), origin_bot_id=_origin_for_tx(rec))
            session.add(row)
        if row.data_json != data:
            row.data_json = data
            # The payment's original Telegram bot identity never changes with
            # mutable bot configuration or a stale payload.
            row.bot_id = str(rec.get("bot_id") or "")
            row.telegram_id = str(rec.get("user_id") or rec.get("telegram_id") or "")
            row.status = str(rec.get("status") or "pending")
            row.amount = float(rec.get("amount") or 0)
            row.is_test = bool(rec.get("is_test") or str(tid).startswith("test_"))
            row.is_upsell = bool(rec.get("is_upsell"))
            row.paid_at = str(rec.get("paid_at") or "")
            row.created_at = str(rec.get("created_at") or "")
    return persisted_clients, persisted_pix


def _remarketing_claim_key(camp):
    return str(camp.get('id')) + ('::' + str(camp['run_id']) if camp.get('run_id') else '')


def _parse_iso_ts(ts_val: str) -> Optional[datetime]:
    if not ts_val or not isinstance(ts_val, str):
        return None
    s = ts_val.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _compute_remarketing_metrics(session, state: Dict[str, Any]):
    campaigns = state.get("remarketing_campaigns") or []
    if not campaigns:
        return

    # Reset all campaigns first: prevents stale totals when there are no valid conversions
    for camp in campaigns:
        if not isinstance(camp, dict):
            continue
        camp["conversions"] = 0
        camp["revenue"] = 0.0

    # Build recipient -> list of (camp, sent_at_str, sent_dt)
    recipient_campaigns: Dict[str, List[Tuple[Dict[str, Any], str, Optional[datetime]]]] = {}
    for camp in campaigns:
        if not isinstance(camp, dict):
            continue
        delivs = camp.get("deliveries") or {}
        ts_map = camp.get("delivery_timestamps") or {}
        run_map = camp.get("run_delivery_timestamps") or {}
        run_id = str(camp.get("run_id") or "default")

        for recipient, status in delivs.items():
            if status != "sent":
                continue
            sent_at = ts_map.get(recipient)
            if not sent_at:
                sent_at = run_map.get(run_id, {}).get(recipient)
            if not sent_at:
                # Fallback to current time if reconciled without explicit timestamp
                continue
            sent_dt = _parse_iso_ts(sent_at)
            recipient_campaigns.setdefault(recipient, []).append((camp, sent_at, sent_dt))

    if not recipient_campaigns:
        return

    tx_list = []
    if session is not None:
        try:
            stmt = select(
                PixRecord.origin_bot_id,
                PixRecord.telegram_id,
                PixRecord.amount,
                PixRecord.paid_at,
                PixRecord.created_at,
                PixRecord.is_test,
                PixRecord.tx_id,
            ).where(PixRecord.status == "paid")
            rows = (await session.execute(stmt)).all()
            for r in rows:
                if getattr(r, "is_test", False):
                    continue
                tx_list.append({
                    "origin_bot_id": str(r.origin_bot_id or ""),
                    "telegram_id": str(r.telegram_id or ""),
                    "amount": float(r.amount or 0.0),
                    "paid_at": str(r.paid_at or r.created_at or ""),
                    "tx_id": str(r.tx_id or ""),
                })
        except Exception:
            tx_list = []

    if not tx_list and state.get("tx_records"):
        for tid, rec in state.get("tx_records", {}).items():
            if not isinstance(rec, dict) or rec.get("status") != "paid" or rec.get("is_test"):
                continue
            tx_list.append({
                "origin_bot_id": _origin_for_tx(rec),
                "telegram_id": str(rec.get("user_id") or rec.get("telegram_id") or ""),
                "amount": float(rec.get("amount") or 0.0),
                "paid_at": str(rec.get("paid_at") or rec.get("created_at") or ""),
                "tx_id": str(tid),
            })

    attributed_tx_ids = set()
    for tx in tx_list:
        origin = tx["origin_bot_id"]
        uid = tx["telegram_id"]
        if not origin or not uid:
            continue
        rec_key = f"{origin}:{uid}"

        candidate_camps = recipient_campaigns.get(rec_key)
        if not candidate_camps:
            continue

        # Decisão pendente: quando o cliente recebeu múltiplas campanhas, não decidir
        # arbitrariamente nem atribuir duplicado até haver definição do usuário.
        if len(candidate_camps) > 1:
            continue

        target_camp, sent_at_str, sent_dt = candidate_camps[0]
        paid_at_str = tx["paid_at"]
        if not paid_at_str:
            continue

        paid_dt = _parse_iso_ts(paid_at_str)
        # Compra anterior ao envio não conta; deve ser estritamente posterior
        if paid_dt and sent_dt:
            is_after = paid_dt > sent_dt
        else:
            is_after = paid_at_str > sent_at_str

        if not is_after:
            continue

        tx_id = tx["tx_id"]
        if tx_id in attributed_tx_ids:
            continue
        attributed_tx_ids.add(tx_id)

        target_camp["conversions"] += 1
        target_camp["revenue"] = round(target_camp["revenue"] + tx["amount"], 2)


async def _project_remarketing_deliveries(session, state):
    rows = (await session.execute(select(RemarketingDelivery))).scalars().all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.campaign_id, {})[row.recipient] = row.status
    for camp in state.get('remarketing_campaigns', []):
        if not isinstance(camp, dict):
            continue
        claims = grouped.get(_remarketing_claim_key(camp), {})
        if claims:
            camp['deliveries'] = {**camp.get('deliveries', {}), **claims}
            camp['sent_count'] = sum(status == 'sent' for status in camp['deliveries'].values())
            if any(status in ('sending', 'uncertain') for status in camp['deliveries'].values()):
                if camp.get('status') != 'Interrompida':
                    camp['status'] = 'Revisão necessária'
            elif camp.get('status') == 'Revisão necessária':
                camp['status'] = 'Parcial' if len(camp['deliveries']) < int(camp.get('total_targets') or 0) else 'Finalizada'
    await _compute_remarketing_metrics(session, state)


async def _claim_remarketing_recipient(campaign_id, recipient):
    """Unique DB insert arbitrates claims even across application processes."""
    async with async_session() as session:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        dialect = session.bind.dialect.name
        insert = sqlite_insert if dialect == 'sqlite' else pg_insert
        result = await session.execute(insert(RemarketingDelivery).values(
            campaign_id=str(campaign_id), recipient=recipient, status='sending'
        ).on_conflict_do_nothing(index_elements=['campaign_id', 'recipient']))
        await session.commit()
        return result.rowcount == 1


async def _mark_remarketing_recipient(campaign_id, recipient, status):
    async with async_session() as session:
        condition = (RemarketingDelivery.campaign_id == str(campaign_id),
                     RemarketingDelivery.recipient == recipient)
        statement = delete(RemarketingDelivery).where(*condition) if status == 'retryable' else update(RemarketingDelivery).where(
            *condition).values(status=status)
        await session.execute(statement)
        await session.commit()


async def _discount_needed_elsewhere(state, campaign_id, recipient, discount):
    async with async_session() as session:
        rows = (await session.execute(select(RemarketingDelivery).where(
            RemarketingDelivery.recipient == recipient))).scalars().all()
    by_campaign = {row.campaign_id: row.status for row in rows}
    for other in state.get('remarketing_campaigns', []):
        if not isinstance(other, dict) or str(other.get('id')) == str(campaign_id):
            continue
        other_discount = bot_manager._parse_discount_pct(
            f"{other.get('discount_pct')}%" if other.get('discount_pct') else str(other.get('discount') or ''))
        if other_discount != discount:
            continue
        status = by_campaign.get(str(other.get('id')), other.get('deliveries', {}).get(recipient))
        if status in ('sent', 'sending', 'uncertain'):
            return True
    return False


async def _read_state_from_db() -> Dict[str, Any]:
    global _STATE_CACHE, _STATE_COMMITTED, _STATE_COMMITTED_OWNER
    if _STATE_CACHE is not None:
        if isinstance(_STATE_CACHE, _StateSnapshot):
            _STATE_CACHE._capture_client_presence()
        return _STATE_CACHE
    async with _DB_LOCK:
        if _STATE_CACHE is not None:
            if isinstance(_STATE_CACHE, _StateSnapshot):
                _STATE_CACHE._capture_client_presence()
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
                hydrated = await _hydrate_records(session, merged)
                await _project_remarketing_deliveries(session, hydrated)
                confirmed = copy.deepcopy(hydrated)
                _STATE_CACHE = _StateSnapshot(_normalize_providers(hydrated))
                _STATE_COMMITTED = _freeze(confirmed)
                _STATE_CACHE._row_base = _STATE_COMMITTED
                _STATE_COMMITTED_OWNER = _STATE_CACHE
                return _STATE_CACHE
            except Exception:
                _STATE_CACHE = _StateSnapshot(_normalize_providers(json.loads(json.dumps(DEFAULT_SHARKBOT_STATE))))
                _STATE_COMMITTED = _freeze(dict(_STATE_CACHE))
                _STATE_CACHE._row_base = _STATE_COMMITTED
                _STATE_COMMITTED_OWNER = _STATE_CACHE
                return _STATE_CACHE

def _flow_cache_slots(bot):
    slots = {}
    def walk(value, path):
        if isinstance(value, dict):
            if value.get('id') and 'url' in value:
                slots[path] = value
            else:
                for key, child in value.items():
                    walk(child, path + (key,))
        elif isinstance(value, list):
            ids = [str(child.get('id') or '') for child in value if isinstance(child, dict)]
            for child in value:
                if isinstance(child, dict) and child.get('id') and ids.count(str(child['id'])) == 1:
                    walk(child, path + (str(child['id']),))
    walk(bot, ())
    return slots


def _flow_cache_event(before, after, state, channel_id=None):
    old, new = _flow_cache_slots(before), _flow_cache_slots(after)
    return {'bot_id': str(before['id']), 'token': before['token'],
            'previous_channel': before.get('cache_channel_id'), 'channel_id': channel_id,
            'previous_publish_channel': state.get('publish', {}).get('cache_channel_id'),
            'updates': [(slot, copy.deepcopy(media), copy.deepcopy(new[slot]))
                        for slot, media in old.items() if slot in new and media != new[slot]]}


def _apply_flow_cache_event(existing_state, event):
    # Keep immutable SQL ancestry, including when current cache rows are dirty.
    state = copy.deepcopy(existing_state)
    bot = next((b for b in state.get('flow_bots', []) if str(b.get('id')) == event['bot_id']), None)
    if not bot or bot.get('token') != event['token'] or bot.get('cache_channel_id') != event['previous_channel']:
        raise HTTPException(status_code=409, detail='O bot ou o canal foi alterado durante a verificação.')
    slots = _flow_cache_slots(bot)
    cache_fields = ('telegram_file_id', 'telegram_send_as', 'cached_channel_id', 'album_cached')
    for slot, before, after in event['updates']:
        media = slots.get(tuple(slot))
        if media is None or any(media.get(k) != before.get(k) for k in ('id', 'url', 'type', 'name') + cache_fields):
            continue
        for field in cache_fields:
            if field in after:
                media[field] = after[field]
            else:
                media.pop(field, None)
        if str(after.get('url') or '').startswith('tg://file_id/'):
            media['url'] = after['url']
    if event.get('channel_id') is not None:
        bot['cache_channel_id'] = event['channel_id']
        pub = state.setdefault('publish', {})
        if pub.get('bot_token') == event['token'] and pub.get('cache_channel_id') == event['previous_publish_channel']:
            pub['cache_channel_id'] = event['channel_id']
    return state


def _apply_checkout_event(existing_state, event):
    """Reconcile one generated Pix onto current rows inside the writer lock."""
    state = copy.deepcopy(existing_state)
    record, before, after = event['record'], event['before'], event['lead']
    tx_id = str(record['tx_id'])
    is_new = tx_id not in state.get('tx_records', {})
    if is_new:
        state.setdefault('tx_records', {})[tx_id] = copy.deepcopy(record)
        learning = state.setdefault('learning', {})
        learning['unpaid_checkouts'] = int(learning.get('unpaid_checkouts') or 0) + 1
    identity = (_origin_for_lead(after, state), str(after['telegram_id']))
    leads = state.setdefault('memory', {}).setdefault('leads', [])
    lead = next((l for l in leads if (_origin_for_lead(l, state), str(l.get('telegram_id'))) == identity), None)
    archived = False
    if lead is None:
        lead = next((l for l in state['memory'].get('deleted_leads', [])
                     if (_origin_for_lead(l, state), str(l.get('telegram_id'))) == identity), None)
        archived = True
    if lead is None:
        # The charge remains payable even if the customer was permanently deleted.
        return state
    # Checkout-owned fields only; never copy an old profile or insight snapshot.
    for field in ('accepted_bump_deliverable', 'has_order_bump', 'flow_plan_id',
                  'flow_plan_price', 'chosen_plan', 'last_tx_id', 'last_tx_is_upsell'):
        if not archived and lead.get(field) == before.get(field):
            if field in after:
                lead[field] = copy.deepcopy(after[field])
            else:
                lead.pop(field, None)
    if not archived:
        bot_manager._advance_stage(lead, 'Gerou checkout')
    history = lead.setdefault('history', [])
    if not any(str(h.get('tx_id') or '') == tx_id for h in history):
        history.append(copy.deepcopy(event['history']))
    if not archived:
        # Merge only this checkout's insight delta; leave newer custom insights intact.
        for field in ('prefs_list', 'objs_list', 'preferences', 'objections'):
            if lead.get(field) == before.get(field) and field in after:
                lead[field] = copy.deepcopy(after[field])
    return state


def _merge_late_cache_delta(committed, before, live, path=(), *, owned=False):
    """Apply only edits since capture, without certifying them as SQL writes."""
    if before == live:
        return committed if owned else copy.deepcopy(committed)
    if isinstance(before, dict) and isinstance(live, dict) and isinstance(committed, dict):
        # A stable panel ID alone cannot carry cache across a bot or source
        # replacement. Compare the captured origin/source, not event output.
        if (('token' in before and before.get('token') != live.get('token'))
                or ('url' in before and any(before.get(k) != live.get(k)
                                           for k in ('id', 'url', 'type', 'name')))):
            return copy.deepcopy(live)
        merged = dict(committed) if owned else copy.deepcopy(committed)
        for key in before.keys() - live.keys():
            merged.pop(key, None)
        for key, value in live.items():
            if key not in before:
                if key in merged and isinstance(value, (dict, list)):
                    merged[key] = _merge_late_cache_delta(merged[key], type(value)(), value, path + (key,), owned=owned)
                else:
                    merged[key] = copy.deepcopy(value)
            elif before[key] != value:
                merged[key] = _merge_late_cache_delta(merged.get(key), before[key], value, path + (key,), owned=owned)
        if path == ('memory',):
            # Coexisting active/archived rows are independent customers.
            old = _client_map({'memory': before})
            saved = _client_map({'memory': committed})
            current = _client_map({'memory': live})
            for archived, key in ((False, 'leads'), (True, 'deleted_leads')):
                rows = []
                for lead in live.get(key, []):
                    identity = (_origin_for_lead(lead, {}), str(lead.get('telegram_id')), archived)
                    source = identity
                    if identity not in old or lead.get('created_at') != old[identity].get('created_at'):
                        # Only a captured incarnation that vacated the opposite
                        # position may carry its checkout across archive/restore.
                        # A fresh reentry at the same SQL key is not that move.
                        opposite = (identity[0], identity[1], not archived)
                        created = lead.get('created_at')
                        source = (opposite if created
                                  and old.get(opposite, {}).get('created_at') == created
                                  and saved.get(opposite, {}).get('created_at') == created
                                  and current.get(opposite, {}).get('created_at') != created else None)
                    rows.append(_merge_late_cache_delta(saved[source], old[source], lead, owned=owned)
                                if source in old and source in saved
                                and saved[source].get('created_at') == old[source].get('created_at')
                                else copy.deepcopy(lead))
                # Retain admission-owned new customers unless live explicitly
                # supplied another row with their same immutable identity.
                known = {(_origin_for_lead(l, {}), str(l.get('telegram_id')))
                         for k in ('leads', 'deleted_leads') for l in live.get(k, [])}
                rows.extend(l if owned else copy.deepcopy(l) for l in committed.get(key, [])
                            if (_origin_for_lead(l, {}), str(l.get('telegram_id')), archived) not in old
                            and (_origin_for_lead(l, {}), str(l.get('telegram_id'))) not in known)
                merged[key] = rows
        return merged
    if isinstance(before, list) and isinstance(live, list) and isinstance(committed, list):
        def item_identity(value):
            if isinstance(value, dict):
                if value.get('telegram_id'):
                    return ('lead', _origin_for_lead(value, {}), str(value['telegram_id']))
                if value.get('id'):
                    return ('item', str(value['id']))
            return None
        before_ids = [item_identity(value) for value in before]
        live_ids = [item_identity(value) for value in live]
        committed_ids = [item_identity(value) for value in committed]
        if (all(key is not None for key in before_ids + live_ids + committed_ids)
                and all(len(keys) == len(set(keys)) for keys in (before_ids, live_ids, committed_ids))):
            old = dict(zip(before_ids, before))
            current = dict(zip(live_ids, live))
            saved = dict(zip(committed_ids, committed))
            merged = []
            for key in live_ids:
                if key not in old:
                    merged.append(copy.deepcopy(current[key]))
                elif key in saved:
                    merged.append(_merge_late_cache_delta(saved[key], old[key], current[key], owned=owned))
            merged.extend(saved[key] if owned else copy.deepcopy(saved[key]) for key in committed_ids if key not in old and key not in current)
            return merged
        # Subtract removed occurrences and add only new occurrences, not the
        # entire live list (which would duplicate checkout-owned history).
        merged = list(committed) if owned else copy.deepcopy(committed)
        unchanged = copy.deepcopy(before)
        added = []
        for value in live:
            if value in unchanged:
                unchanged.remove(value)
            else:
                added.append(copy.deepcopy(value))
        for value in unchanged:
            if value in merged:
                merged.remove(value)
        concurrent = copy.deepcopy(committed)
        for value in before:
            if value in concurrent:
                concurrent.remove(value)
        for value in added:
            # A queued writer can already contain the same pending append
            # captured by the event; merge occurrences, do not replay them.
            if value in concurrent:
                concurrent.remove(value)
            else:
                merged.append(value)
        return merged
    return copy.deepcopy(live)


async def _write_state_to_db(state: Dict[str, Any], preserve_runtime: bool = False, delete_tx_ids: Optional[List[str]] = None, delete_client_keys=None, *, remarketing_new_run: bool = False, runtime_only: bool = False, checkout_media_cache: Optional[Dict[str, Any]] = None, checkout_event: Optional[Dict[str, Any]] = None, checkout_start: Optional[Dict[str, Any]] = None, flow_cache_event: Optional[Dict[str, Any]] = None, lead_block_event: Optional[Dict[str, Any]] = None, access_status_event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _STATE_CACHE, _STATE_COMMITTED, _STATE_COMMITTED_OWNER
    async with _DB_LOCK:
        if isinstance(_STATE_CACHE, _StateSnapshot):
            _STATE_CACHE._capture_client_presence()
        # Only the currently published, SQL-confirmed snapshot can prove a
        # genuine no-op. Detached old writers and pending intents must reconcile.
        if (runtime_only and state is _STATE_CACHE and _STATE_COMMITTED_OWNER is state
                and isinstance(state, _StateSnapshot) and state._row_base is not None
                and not (preserve_runtime or remarketing_new_run or delete_tx_ids or delete_client_keys
                         or checkout_media_cache or checkout_event or checkout_start or flow_cache_event
                         or lead_block_event or access_status_event
                         or state._pending_client_deletes or state._pending_pix_deletes
                         or state.get('remarketing_campaigns'))
                and state == state._row_base):
            return state
        async with async_session() as session:
            result = await session.execute(select(BotConfig))
            config = result.scalars().first()
            existing_state: Dict[str, Any] = {}
            if _STATE_CACHE is not None:
                existing_state = _STATE_CACHE
            elif config and config.full_state_json:
                try:
                    existing_state = await _hydrate_records(session, json.loads(config.full_state_json))
                    await _project_remarketing_deliveries(session, existing_state)
                except Exception:
                    existing_state = {}

            # Pending removals created only by post-serialization live deltas
            # stay explicit intents until a later transaction commits them.
            pending_clients = (getattr(state, '_pending_client_deletes', frozenset())
                               | getattr(existing_state, '_pending_client_deletes', frozenset()))
            pending_pix = (getattr(state, '_pending_pix_deletes', frozenset())
                           | getattr(existing_state, '_pending_pix_deletes', frozenset()))
            delete_client_keys = set(delete_client_keys or []) | (pending_clients - set(_client_map(existing_state)))
            delete_tx_ids = sorted(set(str(t) for t in (delete_tx_ids or [])) | (pending_pix - set(existing_state.get('tx_records', {}))))
            publication_live = existing_state
            publication_before = None
            if checkout_start or checkout_event or flow_cache_event or lead_block_event or access_status_event or runtime_only:
                # Freeze event/queued-runtime reconciliation before row/config
                # awaits; the still-published live reference remains the delta source.
                publication_before = _capture_publication(existing_state)
                existing_state = publication_before
            if not (checkout_start or checkout_event or flow_cache_event or lead_block_event or access_status_event):
                if runtime_only and state is publication_live:
                    state = copy.deepcopy(existing_state)
                elif runtime_only:
                    # Even a detached snapshot can be retained by its caller or
                    # share rows with an earlier publication. Own a fresh copy
                    # before preparation, preserving the snapshot's ancestry.
                    state = copy.deepcopy(state)
            if access_status_event:
                state = copy.deepcopy(existing_state)
                rec = state.get('tx_records', {}).get(access_status_event['tx_id'])
                if not isinstance(rec, dict) or any(rec.get(k) != v for k, v in access_status_event['identity'].items()):
                    raise HTTPException(status_code=409, detail='Compra alterada durante a atualização do acesso.')
                rec['access_status'] = access_status_event['status']
            if lead_block_event:
                state = copy.deepcopy(existing_state)
                identity = (lead_block_event['origin'], lead_block_event['uid'], False)
                target = _client_map(state).get(identity)
                if (not target or not lead_block_event['created_at']
                        or target.get('created_at') != lead_block_event['created_at']
                        or str(target.get('bot_id') or '') != lead_block_event['bot_id']):
                    raise HTTPException(status_code=409, detail='Lead alterado durante a ação.')
                target['blocked'] = not bool(target.get('blocked', False))
            if flow_cache_event:
                state = _apply_flow_cache_event(existing_state, flow_cache_event)
            if checkout_start:
                state = copy.deepcopy(existing_state)
                candidate = checkout_start['lead']
                identity = (_origin_for_lead(candidate, state), str(candidate['telegram_id']))
                clients = _client_map(state)
                if (identity[0], identity[1], False) not in clients:
                    if checkout_start['allow_create'] and (identity[0], identity[1], True) not in clients:
                        state.setdefault('memory', {}).setdefault('leads', []).append(copy.deepcopy(candidate))
                elif checkout_start.get('before') is not None:
                    lead = clients[(identity[0], identity[1], False)]
                    before = checkout_start['before']
                    # Only admission-owned deltas, never an old full lead snapshot.
                    if candidate.get('stage') != before.get('stage') and lead.get('stage') == before.get('stage'):
                        bot_manager._advance_stage(lead, candidate['stage'])
                    if lead.get('accepted_bump_indices') == before.get('accepted_bump_indices'):
                        if 'accepted_bump_indices' in candidate:
                            lead['accepted_bump_indices'] = copy.deepcopy(candidate['accepted_bump_indices'])
            if checkout_event:
                state = _apply_checkout_event(existing_state, checkout_event)

            baseline = (getattr(state, '_row_base', None) or
                        (_STATE_COMMITTED if _STATE_COMMITTED_OWNER is _STATE_CACHE else existing_state))
            old_clients = _client_map(baseline)
            dirty_clients = {key for key, lead in _client_map(state).items() if lead != old_clients.get(key)}
            old_pix = baseline.get('tx_records', {})
            live_pix = existing_state.get('tx_records', {})
            # A Pix seen by this snapshot but explicitly deleted meanwhile is
            # not a newly generated payment; never recreate its row.
            discarded_pix = set(old_pix) - set(live_pix)
            for tid in discarded_pix:
                state.get('tx_records', {}).pop(tid, None)
            dirty_pix = {str(tid) for tid, rec in state.get('tx_records', {}).items() if rec != old_pix.get(tid)}
            # Count only paid transitions that are not already committed. Two
            # independent stale webhooks must both contribute; a replay must not.
            newly_counted = [rec for tid, rec in state.get('tx_records', {}).items()
                             if str(tid) in dirty_pix and str(tid) not in (delete_tx_ids or [])
                             and isinstance(rec, dict) and rec.get('status') == 'paid'
                             and rec.get('sales_counted') is True
                             and not (isinstance(live_pix.get(tid), dict)
                                      and live_pix[tid].get('sales_counted') is True)]

            # Do not turn a stale copy's untouched rows into writes (or into
            # stale values in the returned cache). Only explicit deletion drops rows.
            live_clients = _client_map(existing_state)
            first_creation_collision = False
            seen_clients = getattr(state, '_seen_client_keys', frozenset())
            observed_clients = old_clients.keys() | seen_clients
            for archived, key in ((False, 'leads'), (True, 'deleted_leads')):
                incoming = state.get('memory', {}).get(key, [])
                # A vacated observed position is not a first insertion. Its SQL
                # ancestry can be empty when /start was held on its first reply.
                first_creation_collision |= any(
                    (identity := (_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived))
                    in seen_clients and identity not in old_clients and identity not in live_clients
                    for lead in incoming)
                incoming[:] = [lead for lead in incoming
                    if ((_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived) not in observed_clients
                        or (_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived) in live_clients)
                    and (_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived) not in delete_client_keys]
                for idx, lead in enumerate(incoming):
                    identity = (_origin_for_lead(lead, state), str(lead.get('telegram_id')), archived)
                    if identity not in dirty_clients and identity in live_clients:
                        if lead is live_clients[identity] or lead != live_clients[identity]:
                            incoming[idx] = copy.deepcopy(live_clients[identity])
                    elif identity in live_clients and state is not _STATE_CACHE:
                        # Transfer this stale writer's field/history delta, not
                        # its old whole lead over the just-published event.
                        before = old_clients.get(identity)
                        first_creation_collision |= before is None
                        current = live_clients[identity]
                        created = lead.get('created_at')
                        if (not created or current.get('created_at') != created
                                or str(current.get('bot_id') or '') != str(lead.get('bot_id') or '')
                                or (before is not None and (
                                    before.get('created_at') != created
                                    or str(before.get('bot_id') or '') != str(lead.get('bot_id') or '')))):
                            # Baseline absence is not replacement authority: a
                            # first /start can outlive archive/delete and reentry.
                            incoming[idx] = copy.deepcopy(current)
                            continue
                        if before is not None:
                            lead = _merge_late_cache_delta(current, before, lead)
                        else:
                            # Same first incarnation, but no committed ancestry
                            # to prove field edits/removals. Retain current fields;
                            # admit occurrence-aware first histories and forward
                            # funnel progress only, never an old whole-row reset.
                            merged = copy.deepcopy(current)
                            if 'history' in lead:
                                merged['history'] = _merge_late_cache_delta(
                                    current.get('history', []), [], lead['history'])
                            order = bot_manager.STAGE_ORDER
                            target, stage = lead.get('stage'), current.get('stage')
                            if target in order and (stage not in order or order.index(target) >= order.index(stage)):
                                bot_manager._advance_stage(merged, target)
                            lead = merged
                        incoming[idx] = lead
                        # Detached snapshots may edit history but cannot revoke
                        # an already exposed offer; revocation uses the live state.
                        live_discounts = live_clients[identity].get('authorized_flow_discounts', [])
                        if live_discounts:
                            lead['authorized_flow_discounts'] = list(dict.fromkeys(
                                lead.get('authorized_flow_discounts', []) + live_discounts))
            tx_map = state.setdefault('tx_records', {})
            for tid, record in existing_state.get('tx_records', {}).items():
                if str(tid) not in dirty_pix and str(tid) not in (delete_tx_ids or []):
                    if tx_map.get(tid) is record or tx_map.get(tid) != record:
                        tx_map[tid] = copy.deepcopy(record)

            if runtime_only:
                # Runtime send completion must never restore the configuration
                # captured before an await. Transfer runtime onto fresh config.
                if first_creation_collision:
                    # The first handler's learning summary describes its old
                    # lead snapshot, not the reconciled rows (possibly paid).
                    # Keep the current summary; new settlements are replayed
                    # against current accounting below as usual.
                    state['learning'] = copy.deepcopy(existing_state.get('learning', {}))
                runtime_state = state
                if checkout_media_cache:
                    # This checkout's generated-media cache transfers only via
                    # the source/origin-checked payload below, never by bare ID.
                    runtime_state = copy.deepcopy(state)
                    for bot in runtime_state.get("flow_bots", []):
                        if str(bot.get("id") or "") == checkout_media_cache["bot_id"]:
                            bot.get("payments", {}).pop("generated_medias", None)
                state = _merge_config_preserving_runtime(
                    copy.deepcopy(_config_view(existing_state)), _config_view(runtime_state))
                # These rows already belong to the detached writer. Do not copy
                # and serialize them again just to merge small configuration.
                state['memory'] = runtime_state['memory']
                state['tx_records'] = runtime_state['tx_records']
                # getMe metadata is runtime-owned, but only for the same token;
                # never resurrect a removed/replaced bot or overwrite panel fields.
                runtime_bots = {str(b.get('id')): b for b in runtime_state.get('flow_bots', []) if isinstance(b, dict)}
                base_bots = {str(b.get('id')): b for b in baseline.get('flow_bots', []) if isinstance(b, dict)}
                for bot in state.get('flow_bots', []):
                    runtime_bot = runtime_bots.get(str(bot.get('id')), {})
                    if bot.get('token') and bot.get('token') == runtime_bot.get('token'):
                        for field in ('bot_id', 'username', 'first_name'):
                            if field in runtime_bot and runtime_bot[field] != base_bots.get(str(bot.get('id')), {}).get(field):
                                bot[field] = runtime_bot[field]
                live_pub = state.get('publish', {})
                runtime_pub = runtime_state.get('publish', {})
                if live_pub.get('bot_token') and live_pub.get('bot_token') == runtime_pub.get('bot_token'):
                    for field in ('bot_username', 'bot_first_name'):
                        if field in runtime_pub and runtime_pub[field] != baseline.get('publish', {}).get(field):
                            live_pub[field] = runtime_pub[field]
            if preserve_runtime:
                state = _merge_config_preserving_runtime(state, existing_state)
            else:
                state = _preserve_existing_secrets(state, existing_state)
                if state is not _STATE_CACHE and not remarketing_new_run:
                    _preserve_campaign_progress(state, existing_state)
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
                            for marker in ('primary_access_delivered', 'additional_delivered'):
                                if ex_rec.get(marker):
                                    st_rec[marker] = True
                            for counter in ("delivery_attempts", "automatic_delivery_attempts", "manual_delivery_attempts"):
                                if counter in ex_rec or counter in st_rec:
                                    st_rec[counter] = max(
                                        int(st_rec.get(counter) or 0), int(ex_rec.get(counter) or 0)
                                    )
                            for field in ("access_status", "invite_url", "delivered_at", "expires_at"):
                                if ex_rec.get(field) and (not st_rec.get(field) or
                                        st_rec.get(field) == old_pix.get(ex_id, {}).get(field)):
                                    st_rec[field] = ex_rec[field]
                            # The reservation timestamp belongs to the latest
                            # attempt, never to an older paid snapshot.
                            if ex_rec.get("last_delivery_attempt_at") and (
                                    not st_rec.get("last_delivery_attempt_at") or
                                    str(ex_rec["last_delivery_attempt_at"]) > str(st_rec["last_delivery_attempt_at"])):
                                st_rec["last_delivery_attempt_at"] = ex_rec["last_delivery_attempt_at"]

                ex_learning = existing_state.get("learning") if isinstance(existing_state.get("learning"), dict) else {}
                st_learning = state.setdefault("learning", {})
                if not delete_tx_ids:
                    # Config totals are aggregates, not monotonic max values:
                    # deletions/reset may legitimately decrease them. Replay
                    # only genuinely new settlements against the live totals.
                    for bot in state.get("flow_bots", []):
                        if not isinstance(bot, dict):
                            continue
                        bid = str(bot.get("id") or "")
                        live_bot = next((b for b in existing_state.get("flow_bots", [])
                                         if isinstance(b, dict) and str(b.get("id") or "") == bid), {})
                        base_bot = next((b for b in baseline.get("flow_bots", [])
                                         if isinstance(b, dict) and str(b.get("id") or "") == bid), {})
                        payments = [r for r in newly_counted if r.get("mode") == "flow"
                                    and not r.get("is_upsell") and str(r.get("bot_id") or "") == bid]
                        if live_bot:
                            for key, increment in (("sales_today", len(payments)),
                                                   ("revenue_today", sum(float(r.get("amount") or 0) for r in payments))):
                                if payments or discarded_pix or bot.get(key) == base_bot.get(key):
                                    value = float(live_bot.get(key) or 0) + increment
                                    bot[key] = round(value, 2) if key == "revenue_today" else int(value)
                    baseline_learning = baseline.get("learning")
                    baseline_confirmed = (baseline_learning.get("confirmed_purchases")
                                          if isinstance(baseline_learning, dict) else None)
                    if (discarded_pix or newly_counted or
                            st_learning.get("confirmed_purchases") == baseline_confirmed):
                        st_learning["confirmed_purchases"] = (
                            int(ex_learning.get("confirmed_purchases") or 0) + len(newly_counted)
                        )

            if runtime_only and checkout_media_cache:
                # Match under the writer lock, not before awaiting it: a panel
                # save can remove/replace the bot or source while upload runs.
                cached_bot = next((bot for bot in state.get("flow_bots", [])
                                   if str(bot.get("id") or "") == checkout_media_cache["bot_id"]
                                   and bot.get("token") == checkout_media_cache["bot_token"]), None)
                if cached_bot:
                    for before, after in checkout_media_cache["updates"]:
                        matches = [media for media in cached_bot.get("payments", {}).get("generated_medias", [])
                                   if isinstance(media, dict) and all(media.get(key) == before.get(key)
                                       for key in ("id", "url", "type", "name"))]
                        if len(matches) != 1:
                            continue
                        media = matches[0]
                        cache_fields = ("telegram_file_id", "telegram_send_as", "cached_channel_id", "album_cached")
                        # Accept our repair/conversion only if nobody changed the
                        # original cache meanwhile, including its format/channel.
                        if ({key: media[key] for key in cache_fields if key in media}
                                != {key: before[key] for key in cache_fields if key in before}):
                            continue
                        for field in cache_fields:
                            if field in after:
                                media[field] = after[field]
                            else:
                                media.pop(field, None)
                        if str(after.get("url") or "").startswith("tg://file_id/"):
                            media["url"] = after["url"]

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
            await _project_remarketing_deliveries(session, state)
            state = _normalize_providers(state)

            if not config:
                config = BotConfig()
                session.add(config)

            persisted_clients, persisted_pix = await _persist_records(
                session, state, delete_tx_ids, delete_client_keys,
                dirty_clients=dirty_clients, dirty_pix=dirty_pix)
            config_state = copy.deepcopy(_config_view(state))
            confirmed_config = getattr(existing_state, '_row_base', None)
            if not confirmed_config or config_state != _config_view(confirmed_config):
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
                for tx_id, tx_rec in tx_map.items():
                    if str(tx_id) not in dirty_pix or str(tx_id) in (delete_tx_ids or []):
                        continue
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
            if publication_before is not None:
                state = _merge_late_cache_delta(state, publication_before, publication_live, owned=True)
            _STATE_CACHE = _StateSnapshot(state)
            _STATE_CACHE._seen_client_keys = frozenset(_client_map(_STATE_CACHE))
            if publication_before is not None:
                _STATE_CACHE._pending_client_deletes = frozenset(
                    set(_client_map(publication_before)) - set(_client_map(publication_live)))
                _STATE_CACHE._pending_pix_deletes = frozenset(
                    set(publication_before.get('tx_records', {})) - set(publication_live.get('tx_records', {})))
            # Cache rows copied during reconciliation are not necessarily writes.
            # Advance ancestry only with the exact JSON payloads committed to SQL.
            committed_config = dict(config_state)
            confirmed = getattr(existing_state, '_row_base', None)
            if confirmed is None:
                confirmed = existing_state
            confirmed_clients = {**_client_map(confirmed), **persisted_clients}
            for archived, key in ((False, 'leads'), (True, 'deleted_leads')):
                committed_config['memory'][key] = [_freeze(lead)
                    for identity, lead in confirmed_clients.items()
                    if identity[2] == archived and identity not in deleted_keys]
            committed_config['tx_records'] = {
                tid: _freeze(rec) for tid, rec in
                {**confirmed.get('tx_records', {}), **persisted_pix}.items()
                if str(tid) not in (delete_tx_ids or [])}
            _STATE_COMMITTED = _freeze(committed_config)
            _STATE_CACHE._row_base = _STATE_COMMITTED
            _STATE_COMMITTED_OWNER = _STATE_CACHE
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


async def _create_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Existing installations already have pix_records; create_all does not add columns.
        from sqlalchemy import inspect, text
        columns = await conn.run_sync(lambda sync: {c['name'] for c in inspect(sync).get_columns('pix_records')})
        if 'created_at' not in columns:
            await conn.execute(text("ALTER TABLE pix_records ADD COLUMN created_at VARCHAR NOT NULL DEFAULT ''"))


async def init_db():
    await _create_schema()

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

async def _pix_filters(period: str, bot_id: str, date: str = ''):
    filters = []
    if bot_id != 'all':
        state = await _read_state_from_db()
        bot = next((b for b in state.get('flow_bots', []) if str(b.get('id')) == bot_id), None)
        if not bot:
            raise HTTPException(status_code=404, detail='Bot não encontrado')
        filters.append(PixRecord.origin_bot_id == str(bot.get('bot_id') or ''))
    if date:
        try:
            parsed = datetime.strptime(date, '%Y-%m-%d').date()
            if parsed.isoformat() != date:
                raise ValueError('date format')
        except ValueError:
            raise HTTPException(status_code=422, detail='Data inválida')
        effective = func.coalesce(func.nullif(PixRecord.paid_at, ''), PixRecord.created_at)
        local_day = func.date(effective, '-3 hours')
        filters.extend((local_day >= date, local_day < (parsed + timedelta(days=1)).isoformat()))
    elif period not in ('Total', 'Tudo'):
        today = datetime.now(timezone(timedelta(hours=-3))).date()
        if period == 'Hoje':
            start = today.isoformat()
        elif period == 'Ontem':
            start = (today - timedelta(days=1)).isoformat()
            local_day = func.date(func.coalesce(func.nullif(PixRecord.paid_at, ''), PixRecord.created_at), '-3 hours')
            filters.append(local_day < today.isoformat())
        elif period in ('Semana', '7 dias'):
            start = (today - timedelta(days=6)).isoformat()
        elif period in ('Mês', '30d'):
            start = (today - timedelta(days=29)).isoformat()
        else:
            raise HTTPException(status_code=422, detail='Período inválido')
        effective = func.coalesce(func.nullif(PixRecord.paid_at, ''), PixRecord.created_at)
        local_day = func.date(effective, '-3 hours')
        filters.extend((local_day >= start, local_day < (today + timedelta(days=1)).isoformat()))
    return filters


def _metrics_date_filters(period, date, timestamp):
    """Bound a single event timestamp; generated and paid events have different dates."""
    local_day = func.date(timestamp, '-3 hours')
    if date:
        try:
            parsed = datetime.strptime(date, '%Y-%m-%d').date()
            if parsed.isoformat() != date:
                raise ValueError('date format')
        except ValueError:
            raise HTTPException(status_code=422, detail='Data inválida')
        return [local_day >= date, local_day < (parsed + timedelta(days=1)).isoformat()]
    if period in ('Total', 'Tudo'):
        return []
    today = datetime.now(timezone(timedelta(hours=-3))).date()
    spans = {'Hoje': 0, 'Ontem': 1, 'Semana': 6, '7 dias': 6, 'Mês': 29, '30d': 29}
    if period not in spans:
        raise HTTPException(status_code=422, detail='Período inválido')
    filters = [local_day >= (today - timedelta(days=spans[period])).isoformat()]
    if period == 'Ontem':
        filters.append(local_day < today.isoformat())
    else:
        filters.append(local_day < (today + timedelta(days=1)).isoformat())
    return filters


@app.get('/api/metrics', dependencies=[Depends(verify_admin_auth)])
async def get_panel_metrics(period: str = 'Total', bot_id: str = 'all', date: str = '', flow: str = 'all'):
    """SQL-only payment aggregates; no Pix payloads or per-transaction response."""
    if flow not in ('all', 'flow', 'ai'):
        raise HTTPException(status_code=422, detail='Fluxo inválido')
    scope = []
    matching_origins = []
    if bot_id != 'all' or flow != 'all':
        # Read only configuration; hydrating _read_state_from_db() would SELECT
        # every Pix payload on a cold cache just to resolve bot identities.
        async with async_session() as session:
            raw = (await session.execute(select(BotConfig.full_state_json).order_by(BotConfig.id).limit(1))).scalar_one_or_none()
        bots = json.loads(raw or '{}').get('flow_bots', [])
        if bot_id != 'all' and not any(str(b.get('id')) == bot_id for b in bots):
            raise HTTPException(status_code=404, detail='Bot não encontrado')
        matching = [b for b in bots if (bot_id == 'all' or str(b.get('id')) == bot_id)
                    and (flow == 'all' or ('ai' if b.get('mode') == 'ai' else 'flow') == flow)]
        matching_origins = [str(b.get('bot_id') or '') for b in matching]
        scope.append(PixRecord.origin_bot_id.in_(matching_origins))
    paid = PixRecord.status == 'paid'
    pending = PixRecord.status == 'pending'
    paid_ts = func.coalesce(func.nullif(PixRecord.paid_at, ''), PixRecord.created_at)
    generated_filters = scope + _metrics_date_filters(period, date, PixRecord.created_at)
    paid_filters = scope + [paid] + _metrics_date_filters(period, date, paid_ts)
    pending_filters = generated_filters + [pending]
    count = func.count(PixRecord.tx_id)
    amount = func.coalesce(func.sum(PixRecord.amount), 0)
    async with async_session() as session:
        async def totals(filters):
            return (await session.execute(select(count, amount).where(*filters))).one()
        generated = await totals(generated_filters)
        paid_total = await totals(paid_filters)
        pending_total = await totals(pending_filters)
        total_paid = (await session.execute(select(amount).where(paid))).scalar_one()

        # Agregados da Coorte de Aprovação de Pix (Pix gerados no período e quantos deles estão pagos)
        pix_cohort_denom = generated[0]
        pix_cohort_paid_filters = generated_filters + [paid]
        pix_cohort_num = (await session.execute(select(count).where(*pix_cohort_paid_filters))).scalar_one() or 0
        pix_approval_rate = round(float(pix_cohort_num) / float(pix_cohort_denom) * 100, 1) if pix_cohort_denom > 0 else None

        # Agregados da Coorte de Conversão de Leads (Leads criados no período e quantos compraram ao menos uma vez)
        lead_created_ts = func.json_extract(ClientRecord.data_json, '$.created_at')
        lead_filters = [ClientRecord.archived == False]
        if matching_origins:
            lead_filters.append(ClientRecord.origin_bot_id.in_(matching_origins))
        lead_filters += _metrics_date_filters(period, date, lead_created_ts)

        lead_cohort_denom = (await session.execute(
            select(func.count(ClientRecord.telegram_id)).where(*lead_filters)
        )).scalar_one() or 0

        lead_cohort_num = (await session.execute(
            select(func.count(func.distinct(ClientRecord.telegram_id)))
            .join(
                PixRecord,
                and_(
                    PixRecord.origin_bot_id == ClientRecord.origin_bot_id,
                    PixRecord.telegram_id == ClientRecord.telegram_id,
                    paid
                )
            )
            .where(*lead_filters)
        )).scalar_one() or 0
        lead_conversion_rate = round(float(lead_cohort_num) / float(lead_cohort_denom) * 100, 1) if lead_cohort_denom > 0 else None

        day = func.date(paid_ts, '-3 hours')
        hour_paid = func.strftime('%H', paid_ts, '-3 hours')
        hour_created = func.strftime('%H', PixRecord.created_at, '-3 hours')
        day_rows = (await session.execute(select(day, count, amount).where(*paid_filters, day != '')
                    .group_by(day).order_by(amount.desc(), day.desc()).limit(5))).all()
        bot_rows = (await session.execute(select(PixRecord.origin_bot_id, count, amount).where(*paid_filters)
                    .group_by(PixRecord.origin_bot_id).order_by(amount.desc()).limit(5))).all()
        ticket_rows = (await session.execute(select(PixRecord.amount, count).where(*paid_filters)
                       .group_by(PixRecord.amount).order_by(count.desc(), PixRecord.amount.desc()).limit(5))).all()
        paid_hours = (await session.execute(select(hour_paid, count).where(*paid_filters, hour_paid != '')
                      .group_by(hour_paid).limit(24))).all()
        generated_hours = (await session.execute(select(hour_created, count).where(*generated_filters, hour_created != '')
                           .group_by(hour_created).limit(24))).all()

    hours = {}
    for key, value in generated_hours:
        if str(key).isdigit() and 0 <= int(key) < 24:
            hours.setdefault(int(key), {'hour': int(key), 'generated_count': 0, 'paid_count': 0})['generated_count'] = value
    for key, value in paid_hours:
        if str(key).isdigit() and 0 <= int(key) < 24:
            hours.setdefault(int(key), {'hour': int(key), 'generated_count': 0, 'paid_count': 0})['paid_count'] = value
    return dict(generated_count=generated[0], generated_amount=round(float(generated[1]), 2),
                paid_count=paid_total[0], paid_amount=round(float(paid_total[1]), 2),
                pending_count=pending_total[0], pending_amount=round(float(pending_total[1]), 2),
                all_time_paid_amount=round(float(total_paid), 2),
                pix_approval_cohort=dict(
                    denominator=pix_cohort_denom,
                    numerator=pix_cohort_num,
                    rate=pix_approval_rate
                ),
                lead_conversion_cohort=dict(
                    denominator=lead_cohort_denom,
                    numerator=lead_cohort_num,
                    rate=lead_conversion_rate
                ),
                by_day=[dict(day=d, paid_count=n, paid_amount=round(float(a), 2)) for d, n, a in day_rows],
                by_bot=[dict(origin_bot_id=origin, paid_count=n, paid_amount=round(float(a), 2))
                        for origin, n, a in bot_rows],
                by_ticket=[dict(amount=round(float(a), 2), paid_count=n) for a, n in ticket_rows],
                by_hour=[hours[h] for h in sorted(hours)])


@app.get('/api/transactions', dependencies=[Depends(verify_admin_auth)])
async def list_panel_transactions(period: str = 'Total', bot_id: str = 'all', status: str = 'all',
                                  page: int = 1, page_size: int = 25, date: str = ''):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail='Paginação inválida')
    filters = await _pix_filters(period, bot_id, date)
    if status != 'all':
        filters.append(PixRecord.status == status)
    async with async_session() as session:
        total = (await session.execute(select(func.count(PixRecord.tx_id)).where(*filters))).scalar_one()
        event_ts = case(
            (PixRecord.status == 'paid', func.coalesce(func.nullif(PixRecord.paid_at, ''), PixRecord.created_at)),
            else_=PixRecord.created_at,
        )
        rows = (await session.execute(select(PixRecord.tx_id, PixRecord.bot_id, PixRecord.telegram_id,
                    PixRecord.status, PixRecord.amount, PixRecord.is_test, PixRecord.paid_at,
                    PixRecord.created_at).where(*filters)
                    .order_by(func.julianday(event_ts).desc(), PixRecord.tx_id.desc())
                    .offset((page - 1) * page_size).limit(page_size))).all()
    return {'total': total, 'page': page, 'page_size': page_size, 'items': [
        dict(tx_id=r[0], bot_id=r[1], user_id=r[2], status=r[3], amount=r[4],
             is_test=r[5], paid_at=r[6], created_at=r[7]) for r in rows]}


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
    lead_bot = str(lead.get("origin_bot_numeric_id") or lead.get("bot_id") or "")
    def _matches_archived(item):
        if str(item.get("telegram_id")) != telegram_id:
            return False
        item_bot = str(item.get("origin_bot_numeric_id") or item.get("bot_id") or "")
        return (item_bot == lead_bot) or not item_bot or not lead_bot

    old = next((item for item in archived if _matches_archived(item)), None)
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
            if not bot_id or bot_id == 'all':
                bot_manager.cancel_lead_tasks(clean_uid)
            else:
                bot_manager.cancel_lead_tasks(clean_uid, origin_bot_ids={
                    _crm_verified_bot_origin(lead) for lead in matched
                })
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
                    transaction_bot_id = str(record.get("bot_id") or "").strip()
                    bot_token = str(record.get("bot_token") or "").strip()
                    origin_id = bot_token.split(":", 1)[0] if ":" in bot_token else ""
                    matches = [b for b in bots if isinstance(b, dict) and origin_id.isdecimal()
                               and (not transaction_bot_id or str(b.get("id") or "") == transaction_bot_id)
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
async def reset_lead_conversation(telegram_id: str, bot_id: str = "all"):
    clean_uid = str(telegram_id or "").strip()
    if not clean_uid:
        raise HTTPException(status_code=400, detail="ID de lead inválido.")

    async with bot_manager._get_lead_lock(clean_uid):
        current = await _read_state_from_db()
        staged = copy.deepcopy(current)
        memory = staged.setdefault("memory", {"leads": []})
        for lead in memory.get("leads", []):
            if _matches_lead_scope(lead, clean_uid, bot_id, staged):
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
                if bot_id != "all":
                    break
        saved = await _write_state_to_db(staged)
        if bot_id == "all":
            bot_manager.cancel_lead_tasks(clean_uid)
        else:
            bot = next((b for b in staged.get("flow_bots", []) if str(b.get("id")) == bot_id), None)
            if bot:
                bot_manager._cancel_downsell(clean_uid, str(bot.get("bot_id")))
                bot_manager._cancel_upsell(clean_uid, str(bot.get("bot_id")))
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
            headers.get("x-pushinpay-token")
            or headers.get("x-webhook-secret")
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
        or data_obj.get("transaction_id")
        or data_obj.get("id")
        or payload.get("identifier")
        or payload.get("reference_id")
        or payload.get("transaction_id")
        or payload.get("id")
        or ""
    ).strip()

    if not transaction_id or transaction_id.startswith("sim_"):
        return {"status": "rejected", "action": "invalid_transaction_id", "transaction_id": transaction_id}

    status = str(data_obj.get("status") or payload.get("status") or "").lower()
    if status not in ("paid", "completed", "approved", "settled", "pago", "aprovado", "concluido", "concluído", "liquidado", "received", "confirmed"):
        return {"status": "received", "action": "ignored_non_paid_status", "transaction_id": transaction_id}

    # Cobranças locais canceladas ou inexistentes não devem consultar o gateway
    # novamente nem liberar acesso. O QR externo pode continuar pagável.
    state = await _read_state_from_db()
    tx_record = (state.get("tx_records") or {}).get(transaction_id) or bot_manager.tx_records.get(transaction_id)
    if not tx_record:
        return {"status": "rejected", "action": "unknown_transaction_id", "transaction_id": transaction_id}
    if isinstance(tx_record, dict) and tx_record.get("status") in ("cancelled", "canceled"):
        return {"status": "received", "action": "archived_or_cancelled_tx", "transaction_id": transaction_id}

    # Fail-closed: NUNCA confia apenas no JSON do webhook; consulta o status oficial no gateway
    # Prioridade: credenciais capturadas na emissão da cobrança; fallback para o painel atual
    rec_prov = str(tx_record.get("gateway_provider") or "").lower().strip() if isinstance(tx_record, dict) else ""
    rec_tok = str(tx_record.get("gateway_token") or "").strip() if isinstance(tx_record, dict) else ""
    rec_sec = str(tx_record.get("gateway_secret") or "").strip() if isinstance(tx_record, dict) else ""

    pub = state.get("publish", {})
    gw_provider = rec_prov or (pub.get("gateway_provider") or "syncpay").strip()
    token = rec_tok or (pub.get("pushin_pay_token") or "").strip()
    gw_secret = rec_sec or (pub.get("gateway_secret") or "").strip()

    req_path = str(request.url.path) if request and hasattr(request, "url") and hasattr(request.url, "path") else ""
    route_prov = "pushinpay" if "pushinpay" in req_path else ("syncpay" if "syncpay" in req_path else "")
    if rec_prov and route_prov and rec_prov != route_prov:
        return {"status": "rejected", "action": "provider_mismatch", "transaction_id": transaction_id}
    if not rec_prov and route_prov:
        gw_provider = route_prov

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
        expected_cents = int(tx_record.get("cents") or round(expected_amount * 100))
        paid_cents = parse_gateway_cents(raw_gw, provider=gw_provider)
        if paid_cents == -1:
            logger.error(f"Erro de interpretação de valor do gateway para tx_id={transaction_id}")
            return {
                "status": "error",
                "action": "invalid_gateway_amount_format",
                "transaction_id": transaction_id,
                "detail": "Valor informado pelo gateway não pôde ser interpretado."
            }
        elif paid_cents is not None:
            if paid_cents < expected_cents:
                logger.warning(
                    f"Tentativa de pagamento com valor menor: tx_id={transaction_id}, esperado={expected_cents}, recebido={paid_cents}"
                )
                return {
                    "status": "rejected",
                    "action": "amount_mismatch",
                    "transaction_id": transaction_id,
                    "detail": f"Valor pago ({paid_cents} centavos) menor que o esperado ({expected_cents} centavos)."
                }

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
    origin_bot_numeric_id: Optional[str] = ""
    channel_id: str

@app.post("/api/flow-bots/verify-channel", dependencies=[Depends(verify_admin_auth)])
async def verify_flow_channel(req: VerifyChannelRequest):
    from telegram_service import _create_bot_instance
    state = copy.deepcopy(await _read_state_from_db())
    target_fb = None
    if req.bot_id:
        for b in (state.get("flow_bots") or []):
            if isinstance(b, dict) and str(b.get("id")) == str(req.bot_id):
                target_fb = b
                break
    if req.bot_id and not target_fb:
        raise HTTPException(status_code=404, detail="Bot solicitado não encontrado.")
    if not target_fb:
        target_fb = bot_manager._get_active_flow_bot(state)
    if not target_fb:
        raise HTTPException(status_code=404, detail="Bot não encontrado.")
    tok = str(target_fb.get("token") or "").strip()
    supplied_token = (req.token or "").strip()
    if supplied_token and supplied_token != REDACTED_SECRET_MASK and supplied_token != tok:
        raise HTTPException(status_code=409, detail="Token do bot foi alterado.")
    origin = tok.split(":", 1)[0] if ":" in tok else ""
    if req.origin_bot_numeric_id and (not req.origin_bot_numeric_id.isdecimal() or req.origin_bot_numeric_id != origin):
        raise HTTPException(status_code=409, detail="Identidade do bot de origem não confere.")
    before_bot = copy.deepcopy(target_fb)
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
        live_state = await _read_state_from_db()
        live_bot = next((b for b in live_state.get("flow_bots", []) if str(b.get("id")) == str(target_fb["id"])), None)
        if not live_bot or live_bot.get("token") != tok or live_bot.get("cache_channel_id") != before_bot.get("cache_channel_id"):
            raise HTTPException(status_code=409, detail="O bot ou o canal foi alterado durante a verificação.")
        target_fb["cache_channel_id"] = resolved_id
        # Warm only this bot on detached configuration; apply source-checked
        # cache deltas to current configuration under the writer lock.
        warm_state = {"flow_bots": [target_fb], "publish": {"bot_token": tok, "cache_channel_id": resolved_id}}
        warmed = await bot_manager.warm_all_state_medias(warm_state)
        state = await _write_state_to_db(state, flow_cache_event=_flow_cache_event(before_bot, target_fb, state, resolved_id))
        return {
            "status": "ok",
            "channel_id": resolved_id,
            "title": chat.title or chat.username or resolved_id,
            "verified_at": datetime.now().strftime("%d/%m, %H:%M"),
            "warmed_medias": warmed,
            "state": _panel_state(state)
        }
    except HTTPException:
        raise
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
    origin_bot_numeric_id: Optional[str] = ""
    channel_id: Optional[str] = ""
    media: Dict[str, Any]


@app.post("/api/flow-bots/cache-media", dependencies=[Depends(verify_admin_auth)])
async def cache_flow_media(req: CacheMediaRequest):
    from telegram_service import _create_bot_instance
    state = copy.deepcopy(await _read_state_from_db())
    fb = None
    for b in (state.get("flow_bots") or []):
        if isinstance(b, dict) and (not req.bot_id or str(b.get("id")) == str(req.bot_id)):
            fb = b
            break
    if req.bot_id and not fb:
        raise HTTPException(status_code=404, detail="Bot solicitado não encontrado.")
    if not fb:
        fb = bot_manager._get_active_flow_bot(state)

    tok = str((fb.get("token") if isinstance(fb, dict) else "") or "").strip()
    # Only an omitted identity keeps the legacy active/publish fallback.
    if not req.bot_id and (not tok or tok == REDACTED_SECRET_MASK):
        tok = (state.get("publish", {}).get("bot_token") or bot_manager.current_token or "").strip()
    if not tok or tok == REDACTED_SECRET_MASK:
        return {"status": "error", "message": "Token do bot não configurado"}
    origin = tok.split(":", 1)[0] if ":" in tok else ""
    if req.origin_bot_numeric_id and (not req.origin_bot_numeric_id.isdecimal()
                                      or req.origin_bot_numeric_id != origin):
        raise HTTPException(status_code=409, detail="Identidade do bot de origem não confere.")

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
    origin_bot_numeric_id: Optional[str] = ""
    track: str = "general"
    seq_idx: int = -1
    seq_id: Optional[str] = ""
    seq_ids: Optional[List[str]] = None
    channel_id: Optional[str] = ""


@app.post("/api/flow-bots/downsell/test-send", dependencies=[Depends(verify_admin_auth)])
async def test_send_downsell(req: DownsellTestSendRequest):
    from aiogram import types
    from telegram_service import _create_bot_instance

    state = copy.deepcopy(await _read_state_from_db())
    flow_bots = [b for b in (state.get("flow_bots") or []) if isinstance(b, dict)]
    target_fb = None
    if req.bot_id:
        target_fb = next((b for b in flow_bots if str(b.get("id")) == str(req.bot_id)), None)
    if req.bot_id and not target_fb:
        raise HTTPException(status_code=404, detail="Bot solicitado não encontrado.")
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
    origin = tok.split(":", 1)[0]
    if req.origin_bot_numeric_id and (not req.origin_bot_numeric_id.isdecimal() or req.origin_bot_numeric_id != origin):
        raise HTTPException(status_code=409, detail="Identidade do bot de origem não confere.")
    if req.track not in ("general", "pix"):
        raise HTTPException(status_code=400, detail="Trilha de downsell inválida.")
    track_key = "downsell_general" if req.track == "general" else "downsell_pix"
    initial_seqs = copy.deepcopy(target_fb.get(track_key, {}).get("sequences") or [])
    if req.seq_ids is not None and req.seq_ids != [str(s.get("id") or "") for s in initial_seqs]:
        raise HTTPException(status_code=409, detail="As sequências foram alteradas. Salve e teste novamente.")
    if req.seq_id and (req.seq_idx < 0 or req.seq_idx >= len(initial_seqs) or str(initial_seqs[req.seq_idx].get("id")) != req.seq_id):
        raise HTTPException(status_code=409, detail="A sequência selecionada foi alterada.")
    before_bot = copy.deepcopy(target_fb)
    def same_preview(live_state):
        live = next((b for b in live_state.get("flow_bots", []) if str(b.get("id")) == str(target_fb["id"])), None)
        return live and live.get("token") == tok and live.get("cache_channel_id") == before_bot.get("cache_channel_id") and live.get(track_key, {}).get("sequences", []) == initial_seqs

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
            if not same_preview(await _read_state_from_db()):
                raise HTTPException(status_code=409, detail="A configuração foi alterada durante o teste.")
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
                    bot_instance=bot_inst,
                    persist_cache=False
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
                        bot_instance=bot_inst,
                        persist_cache=False
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

        # Cache-only, source-checked transfer; never restore this send's configuration snapshot.
        await _write_state_to_db(state, flow_cache_event=_flow_cache_event(before_bot, target_fb, state))

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
    requested_bot_id = (req.bot_id or "").strip()
    numeric_scope = requested_bot_id.isdecimal()
    if isinstance(req.tx_ids, list):
        candidate_ids = [str(x).strip() for x in req.tx_ids if str(x).strip()]
    elif single_tid and single_tid != "all":
        candidate_ids = [single_tid]
    else:
        candidate_ids = [
            tid for tid, rec in tx_map.items()
            if isinstance(rec, dict)
            and (not req.bot_id or req.bot_id == "all"
                 or (not numeric_scope and str(rec.get("bot_id") or "") == req.bot_id)
                 or (numeric_scope and str(rec.get("bot_token") or "").startswith(requested_bot_id + ":")
                     and str(rec.get("origin_bot_numeric_id") or req.bot_id) == req.bot_id))
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
    for tid in dict.fromkeys(candidate_ids):
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
        if numeric_scope and not (isinstance(req.tx_ids, list) or (single_tid and single_tid != "all")):
            # Ignore mutable config ID for scoped numeric requests.
            # A numeric API scope must resolve to exactly one configured origin.
            # Legacy config IDs can be reused; only purchase token proves identity.
            origin_configs = [candidate for candidate in (state.get("flow_bots") or [])
                              if isinstance(candidate, dict)
                              and str(candidate.get("token") or "").startswith(requested_bot_id + ":")]
            if len(origin_configs) != 1:
                continue
            fb = origin_configs[0]
        elif not fb:
            continue
        tok = str(fb.get("token") or "").strip()
        if not tok or tok == REDACTED_SECRET_MASK:
            continue
        if rec_token and rec_token != REDACTED_SECRET_MASK:
            if rec_token != tok and rec_token.split(":", 1)[0] != tok.split(":", 1)[0]:
                continue  # identity mismatch: paid purchase requires manual correction, not a blind send

        temp_bot = None
        lock = bot_manager._get_tx_lock(tid)
        await lock.acquire()
        try:
            state = await _read_state_from_db()
            rec = state.get('tx_records', {}).get(tid)
            if not isinstance(rec, dict) or rec.get('status') != 'paid' or rec.get('bot_token') != rec_token:
                continue
            temp_bot = _create_bot_instance(rec_token)
            eff_type = rec.get("delivery_dest_type") or "Canal ou grupo"
            ch_target = (rec.get("delivery_channel_id") or "").strip()
            ext_url = (rec.get("delivery_external_url") or "").strip()
            resend_primary = not rec.get('primary_access_delivered') or bool(rec.get('delivered'))
            invite_url = ""

            if not resend_primary:
                invite_url = str(rec.get('invite_url') or '')
            elif eff_type == "Link externo" and ext_url:
                invite_url = ext_url
            elif ch_target:
                from telegram_service import _confirm_channel_reentry
                await _confirm_channel_reentry(temp_bot, tid, rec, _read_state_from_db)
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

            if resend_primary and (not invite_url or invite_url == "https://t.me/+link_exclusivo_vip"):
                continue

            rec['manual_delivery_attempts'] = int(rec.get('manual_delivery_attempts') or 0) + 1
            rec['last_manual_delivery_attempt_at'] = datetime.utcnow().isoformat()
            await _write_state_to_db(state, runtime_only=True)

            btn_txt = ((fb.get("payments") or {}).get("access_button_text") if isinstance(fb, dict) else None) or "Acessar conteúdo"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=btn_txt, url=invite_url)]
            ])
            plan_label = rec.get("offer_name") or "Plano VIP"
            safe_plan = html.escape(str(plan_label))
            resend_text = f"🔑 <b>Seu acesso ao plano {safe_plan} foi reenviado!</b>\n\nClique no botão abaixo para acessar:"
            async def send_access(text, markup=None):
                try:
                    result = await _asyncio.wait_for(temp_bot.send_message(
                        chat_id=uid, text=text, parse_mode='HTML', reply_markup=markup), 5)
                except Exception as error:
                    if "can't parse entities" not in str(error).lower():
                        raise
                    result = await _asyncio.wait_for(temp_bot.send_message(
                        chat_id=uid, text=text, reply_markup=markup), 5)
                if result is None:
                    raise RuntimeError('Telegram não confirmou o envio.')

            if resend_primary:
                await send_access(resend_text, kb)
                rec['primary_access_delivered'] = True
                rec['invite_url'] = invite_url
                await _write_state_to_db(state, runtime_only=True)
            bump_deliv = str(rec.get('deliverable') or '').strip()
            if bump_deliv and not rec.get('additional_delivered'):
                await send_access(f"🎁 <b>Seu adicional adquirido:</b>\n\n{bump_deliv}")
                rec['additional_delivered'] = True
                await _write_state_to_db(state, runtime_only=True)
            rec["delivered"] = True
            rec["access_status"] = "Ativo"
            rec.pop('delivery_error', None)
            sent_count += 1
        except Exception as error:
            if isinstance(rec, dict):
                rec['delivery_error'] = str(error)
                if not rec.get('delivered'):
                    rec['access_status'] = 'Pendente (Reenvio manual)'
        finally:
            try:
                await _write_state_to_db(state, runtime_only=True)
            finally:
                lock.release()
                if temp_bot:
                    try:
                        await _asyncio.wait_for(temp_bot.session.close(), 1)
                    except Exception:
                        pass

    state = await _read_state_from_db()
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
    resolved_status = (req.access_status or req.status or "").strip()
    if not resolved_status:
        raise HTTPException(status_code=400, detail="Informe o novo status de acesso.")
    # A per-Pix lock is allowed across transport; the global writer is not.
    async with bot_manager._get_tx_lock(req.tx_id):
        state = await _read_state_from_db()
        rec = (state.get('tx_records') or {}).get(req.tx_id)
        if not isinstance(rec, dict):
            raise HTTPException(status_code=404, detail="Registro de acesso não encontrado.")
        identity = {k: copy.deepcopy(rec.get(k)) for k in
                    ('bot_id', 'bot_token', 'origin_bot_numeric_id', 'user_id', 'delivery_channel_id', 'created_at')}
        if resolved_status == "Removido":
            tok = str(rec.get('bot_token') or '').strip()
            origin = tok.split(':', 1)[0] if ':' in tok else ''
            configs = [b for b in state.get('flow_bots', []) if isinstance(b, dict)
                       and str(b.get('id') or '') == str(rec.get('bot_id') or '')]
            if (not origin.isdecimal() or tok == REDACTED_SECRET_MASK or len(configs) != 1
                    or str(configs[0].get('token') or '').split(':', 1)[0] != origin
                    or str(rec.get('origin_bot_numeric_id') or origin) != origin):
                raise HTTPException(status_code=409, detail='Bot de origem da compra não comprovado; acesso não removido.')
            ch_target = str(rec.get('delivery_channel_id') or '').strip()
            uid = str(rec.get('user_id') or '')
            if not ch_target or not uid.isdecimal() or int(uid) <= 0:
                raise HTTPException(status_code=409, detail='Canal ou cliente da compra não comprovado; acesso não removido.')
            temp_bot = None
            try:
                # Use only the token captured by the purchase, never a mutable
                # bot configuration's fallback credential or delivery channel.
                temp_bot = _create_bot_instance(tok)
                if str(getattr(temp_bot, 'id', origin)) != origin:
                    raise RuntimeError('Identidade Telegram divergente')
                parsed_ch = int(ch_target) if ch_target.lstrip('-').isdigit() else ch_target
                confirmed = await temp_bot.ban_chat_member(chat_id=parsed_ch, user_id=int(uid))
                if confirmed is not True:
                    raise RuntimeError('Telegram não confirmou a remoção')
            except Exception as exc:
                raise HTTPException(status_code=502, detail='Telegram não confirmou a remoção; acesso não foi marcado como Removido.') from exc
            finally:
                if temp_bot:
                    try:
                        await temp_bot.session.close()
                    except Exception:
                        pass
        event = {'tx_id': req.tx_id, 'identity': identity, 'status': resolved_status}
        try:
            state = await _write_state_to_db(copy.deepcopy(state), runtime_only=True, access_status_event=event)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail='Não foi possível salvar o status de acesso; atualização não confirmada.') from exc
        return {"status": "ok", "access_status": resolved_status, "state": _panel_state(state)}


def _crm_verified_bot_origin(lead: Dict[str, Any]) -> str:
    """Only an immutable identity captured on this lead can prove its origin."""
    recorded = str(lead.get("origin_bot_numeric_id") or "")
    return recorded if recorded.isdecimal() else ""


async def _record_crm_message(origin: str, uid: str, bot_id: str, created_at: str, text: str) -> bool:
    """Append only to the still-existing original row, never insert a lead.

    Serialize fresh read/update/commit with deletions and all state writers.
    Configuration, archived rows and replacement origins are not touched.
    """
    global _STATE_CACHE
    async with _DB_LOCK:
        async with async_session() as session:
            row = await session.get(ClientRecord, (origin, uid, False))
            if row is None:
                return False
            lead = json.loads(row.data_json)
            if (str(lead.get('bot_id') or '') != bot_id
                    or str(lead.get('origin_bot_numeric_id') or '') != origin
                    or str(lead.get('created_at') or '') != created_at):
                return False
            lead.setdefault('history', []).append({'role': 'assistant', 'content': text})
            lead['history'] = lead['history'][-MAX_HISTORY_PER_LEAD:]
            row.data_json = json.dumps(lead, ensure_ascii=False)
            await session.commit()
            _STATE_CACHE = None
            return True


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
        origin = _crm_verified_bot_origin(target_lead)
        if not origin:
            raise HTTPException(status_code=409, detail='Origem do lead não comprovada.')
        event = {'origin': origin, 'uid': tid, 'bot_id': str(target_lead.get('bot_id') or ''),
                 'created_at': target_lead.get('created_at')}
        state = await _write_state_to_db(copy.deepcopy(state), runtime_only=True, lead_block_event=event)
        blocked = _client_map(state)[(origin, tid, False)]['blocked']
        return {"status": "ok", "blocked": blocked, "state": _panel_state(state)}

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
        lead_created_at = str(target_lead.get("created_at") or "")
        temp_bot = None
        try:
            temp_bot = _create_bot_instance(tok)
            try:
                await temp_bot.send_message(chat_id=int(tid), text=txt, parse_mode="HTML")
            except Exception:
                await temp_bot.send_message(chat_id=int(tid), text=txt)
            recorded = await _record_crm_message(recorded_bot_numeric_id, tid, lead_bot_id, lead_created_at, txt)
            state = await _read_state_from_db()
            return {"status": "ok", "history_recorded": recorded, "state": _panel_state(state)}
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
    run_id: Optional[str] = None
    start_new_run: bool = False
    bot_id: Optional[str] = "all"
    segment: Optional[str] = "Pendentes"
    message: Optional[str] = ""
    include_plans: Optional[bool] = True
    discount_pct: Optional[int] = 0


class RemarketingReconcileRequest(BaseModel):
    campaign_id: str
    run_id: Optional[str] = None
    recipient: str
    resolution: str


@app.post('/api/flow-bots/remarketing/reconcile', dependencies=[Depends(verify_admin_auth)])
async def reconcile_remarketing(req: RemarketingReconcileRequest):
    if req.resolution not in ('sent', 'not_delivered'):
        raise HTTPException(400, 'Resolução inválida')
    async with async_session() as session:
        state = await _read_state_from_db()
        camp = next((c for c in state.get('remarketing_campaigns', []) if str(c.get('id')) == req.campaign_id), None)
        if not camp or req.run_id != camp.get('run_id'):
            raise HTTPException(409, 'Execução mudou; atualize os dados')
        claim = await session.get(RemarketingDelivery, (_remarketing_claim_key(camp), req.recipient))
        if claim is None or claim.status not in ('sending', 'uncertain'):
            raise HTTPException(409, 'Destinatário sem entrega ambígua')
        claim.status = req.resolution
        await session.commit()
    global _STATE_CACHE
    _STATE_CACHE = None
    state = await _read_state_from_db()
    return {'status': 'ok', 'state': _panel_state(state)}


# Serializes overlapping sends in one worker; persisted recipient claims survive retries.
_REMARKETING_SEND_LOCK = _asyncio.Lock()
REMARKETING_SEND_TIMEOUT_SECONDS = 5.0
REMARKETING_BATCH_TIMEOUT_SECONDS = 15.0

@app.post("/api/flow-bots/remarketing/send", dependencies=[Depends(verify_admin_auth)])
async def send_remarketing_campaign(req: RemarketingSendRequest):
    async with _REMARKETING_SEND_LOCK:
        return await _send_remarketing_campaign_locked(req)

async def _send_remarketing_campaign_locked(req: RemarketingSendRequest):
    from aiogram import types
    from telegram_service import _create_bot_instance

    state = await _read_state_from_db()
    campaigns = state.setdefault("remarketing_campaigns", [])
    camp = next((c for c in campaigns if isinstance(c, dict) and str(c.get("id")) == str(req.campaign_id)), None)
    if not camp:
        raise HTTPException(status_code=404, detail="Campanha não encontrada.")

    # Explicit new runs get distinct durable claims; retries keep the same ID.
    changed_completed = (not req.run_id and camp.get('status') == 'Finalizada'
                         and camp.get('run_message') != camp.get('message'))
    if req.start_new_run and not req.run_id:
        raise HTTPException(400, 'Uma nova execução requer run_id para permitir retomada segura')
    new_run = (req.start_new_run and req.run_id != camp.get('run_id')) or changed_completed
    if new_run:
        import uuid
        if req.run_id in camp.get('run_ids', []):
            raise HTTPException(409, 'Esta execução já foi encerrada; atualize os dados')
        if any(v in ('sending', 'uncertain') for v in camp.get('deliveries', {}).values()):
            raise HTTPException(409, 'Reconcilie as entregas ambíguas antes de iniciar outra execução')
        # Publish the new identity only after its durable commit succeeds.
        state = copy.deepcopy(state)
        camp = next(c for c in state['remarketing_campaigns'] if str(c.get('id')) == req.campaign_id)
        camp.update(run_id=req.run_id or uuid.uuid4().hex, deliveries={}, delivery_timestamps={}, sent_count=0,
                    status='Em andamento', retry_not_before=0)
        camp.setdefault('run_ids', []).append(camp['run_id'])
    elif req.run_id and req.run_id != camp.get('run_id'):
        raise HTTPException(409, 'Execução não encontrada; atualize os dados')
    camp.setdefault('run_message', camp.get('message'))
    if new_run:
        camp['run_message'] = camp.get('message')
        state = await _write_state_to_db(state, preserve_runtime=False, remarketing_new_run=True)
        camp = next(c for c in state['remarketing_campaigns'] if str(c.get('id')) == req.campaign_id)
    claim_key = _remarketing_claim_key(camp)
    retry_after = max(0, float(camp.get('retry_not_before') or 0) - datetime.now().timestamp())
    if retry_after:
        return {'status':'deferred', 'sent_count':0, 'total_targets':int(camp.get('total_targets') or 0),
                'has_more':False, 'retry_after':retry_after, 'run_id':camp.get('run_id'), 'state':_panel_state(state)}
    deadline = _asyncio.get_running_loop().time() + REMARKETING_BATCH_TIMEOUT_SECONDS

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
    target_count = len(matched_leads)
    # Campaign-scoped identities prevent a retry from re-sending confirmed or
    # ambiguous deliveries. A Telegram acceptance and our DB commit cannot be atomic.
    targets = [(str(verified_lead_bot(l).get("token")).split(":", 1)[0],
                str(l.get("telegram_id") or l.get("user_id") or "").strip())
               for l in matched_leads]
    def current_campaign():
        return next(c for c in state["remarketing_campaigns"] if str(c.get("id")) == str(req.campaign_id))

    attempted = 0
    batch_rate_limited = False
    attempted_recipients = set()
    try:
        for origin, uid_str in targets:
            if not uid_str.lstrip("-").isdigit():
                continue
            uid = int(uid_str)
            camp = current_campaign()
            recipient = f"{origin}:{uid_str}"
            deliveries = camp.setdefault("deliveries", {})
            if recipient in deliveries:
                continue  # 'sending' is ambiguous after a crash; never auto-resend it.
            l = next((lead for lead in state.get("memory", {}).get("leads", [])
                      if str(lead.get("telegram_id") or lead.get("user_id") or "") == uid_str
                      and _crm_verified_bot_origin(lead) == origin), None)
            if l is None:
                continue
            lead_fb = verified_lead_bot(l)
            if not lead_fb:
                continue
            lead_tok = str(lead_fb.get("token") or "").strip()
            if not lead_tok or lead_tok == REDACTED_SECRET_MASK:
                continue
            if attempted >= 10 or _asyncio.get_running_loop().time() >= deadline:
                break
            if not await _claim_remarketing_recipient(claim_key, recipient):
                continue
            attempted += 1
            attempted_recipients.add(recipient)
            bot_inst = temp_bots.get(lead_tok)
            if not bot_inst:
                bot_inst = _create_bot_instance(lead_tok)
                temp_bots[lead_tok] = bot_inst
            kb = bot_manager._build_flow_plans_kb(lead_fb, discount_str=discount_str) if include_buttons else None
            fake_user = types.User(id=uid, is_bot=False, first_name=l.get("first_name") or "Cliente")
            rendered = bot_manager._render_flow_vars(msg_tmpl or "🔥 Oferta especial liberada para você!", fake_user, lead_fb)
            # Persist the claim BEFORE calling the external API. An interrupted
            # send remains 'sending' for manual reconciliation, not blind retry.
            deliveries[recipient] = "sending"
            camp["status"] = "Em andamento"
            camp["total_targets"] = target_count
            # The unique claim is already durable; avoid a redundant full-state write.
            # The state writer may replace untouched lead objects while merging rows.
            l = next(lead for lead in state["memory"]["leads"]
                     if str(lead.get("telegram_id") or lead.get("user_id") or "") == uid_str
                     and _crm_verified_bot_origin(lead) == origin)
            authorized_here = False
            if disc_pct > 0:
                auth_d = l.setdefault("authorized_flow_discounts", [])
                if disc_pct not in auth_d:
                    auth_d.append(disc_pct)
                    authorized_here = True
                state = await _write_state_to_db(state, preserve_runtime=False)
            try:
                try:
                    await _asyncio.wait_for(bot_inst.send_message(chat_id=uid, text=rendered, parse_mode="HTML", reply_markup=kb),
                        timeout=min(REMARKETING_SEND_TIMEOUT_SECONDS, max(0.001, deadline - _asyncio.get_running_loop().time())))
                except Exception as e:
                    if "can't parse entities" not in str(e).lower():
                        raise
                    await _asyncio.wait_for(bot_inst.send_message(chat_id=uid, text=rendered, reply_markup=kb),
                        timeout=min(REMARKETING_SEND_TIMEOUT_SECONDS, max(0.001, deadline - _asyncio.get_running_loop().time())))
            except Exception as e:
                camp = current_campaign()
                err_s = str(e).lower()
                rate_limited = False
                if any(x in err_s for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")):
                    camp["deliveries"][recipient] = "blocked"
                    l["blocked"] = True
                    l["stage"] = "Bloqueou o bot"
                    if authorized_here and not await _discount_needed_elsewhere(state, req.campaign_id, recipient, disc_pct):
                        l['authorized_flow_discounts'].remove(disc_pct)
                elif "retry after" in err_s or "too many requests" in err_s or "flood" in err_s:
                    rate_limited = batch_rate_limited = True
                    import re
                    delay = getattr(e, 'retry_after', None)
                    match = re.search(r'retry after\s+(\d+)', err_s)
                    retry_after = max(float(delay or (match.group(1) if match else 60)), 1)
                    camp['retry_not_before'] = datetime.now().timestamp() + retry_after
                    # Telegram rejected the request; this recipient is safe to retry.
                    camp["deliveries"].pop(recipient, None)
                    if authorized_here and not await _discount_needed_elsewhere(state, req.campaign_id, recipient, disc_pct):
                        l['authorized_flow_discounts'].remove(disc_pct)
                else:
                    # A transport error may arrive after Telegram accepted the send.
                    camp["deliveries"][recipient] = "uncertain"
                await _mark_remarketing_recipient(claim_key, recipient,
                                                  camp['deliveries'].get(recipient, 'retryable'))
                if isinstance(e, _asyncio.TimeoutError):
                    # The durable claim holds uncertainty; no redundant full-state
                    # commit may consume the remaining HTTP budget. Cold reads
                    # project this claim back into campaign progress.
                    camp['status'] = 'Revisão necessária'
                    return {'status':'ok', 'sent_count':sent_ok, 'total_targets':target_count,
                            'target_count':target_count, 'has_more':False, 'retry_after':0,
                            'run_id':camp.get('run_id'), 'state':_panel_state(state)}
                state = await _write_state_to_db(state, preserve_runtime=False)
                if rate_limited:
                    break  # Telegram rejected the batch: no later recipient may be attempted.
                continue
            camp = current_campaign()
            camp["deliveries"][recipient] = "sent"
            sent_now_iso = datetime.utcnow().isoformat()
            camp.setdefault("delivery_timestamps", {})[recipient] = sent_now_iso
            camp.setdefault("run_delivery_timestamps", {}).setdefault(str(camp.get("run_id") or "default"), {})[recipient] = sent_now_iso
            await _mark_remarketing_recipient(claim_key, recipient, 'sent')
            camp["sent_count"] = int(camp.get("sent_count") or 0) + 1
            l["from_remarketing"] = True
            l.setdefault("history", []).append({"role": "assistant", "content": f"[Remarketing: {camp.get('name', 'Campanha')}] {rendered}"})
            sent_ok += 1
            state = await _write_state_to_db(state, preserve_runtime=False)
            await _asyncio.sleep(min(0.05, max(0, deadline - _asyncio.get_running_loop().time())))
    except _asyncio.CancelledError:
        current_campaign()["status"] = "Interrompida"
        await _write_state_to_db(state, preserve_runtime=False)
        raise
    finally:
        for b_inst in temp_bots.values():
            try:
                await _asyncio.wait_for(b_inst.session.close(), timeout=1)
            except Exception:
                pass

    camp = current_campaign()
    # Other workers may have claimed/sent recipients while this snapshot was
    # running. Always project DB claims before deciding whether to continue.
    async with async_session() as claim_session:
        await _project_remarketing_deliveries(claim_session, state)
    deliveries = camp.get("deliveries", {})
    has_more = not batch_rate_limited and not retry_after and any(uid.lstrip("-").isdigit() and f"{origin}:{uid}" not in deliveries
                   and f"{origin}:{uid}" not in attempted_recipients
                   for origin, uid in targets)
    camp["status"] = ("Revisão necessária" if any(v in ("sending", "uncertain")
                       for v in deliveries.values()) else
                      "Parcial" if any(uid.lstrip("-").isdigit() and f"{origin}:{uid}" not in deliveries
                                       for origin, uid in targets) else "Finalizada")
    camp["last_sent_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    state = await _write_state_to_db(state, preserve_runtime=False)
    camp = current_campaign()
    retry_after = max(0, float(camp.get('retry_not_before') or 0) - datetime.now().timestamp())
    return {
        "status": "ok",
        "sent_count": sent_ok,
        "total_targets": target_count,
        "target_count": target_count,
        "has_more": has_more,
        "retry_after": retry_after,
        "run_id": camp.get('run_id'),
        "state": _panel_state(state)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))



