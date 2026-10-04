import asyncio
import base64
import html
import logging
import os
import random
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Set

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, URLInputFile, BufferedInputFile

if not hasattr(Dispatcher, "callback_handlers"):
    setattr(Dispatcher, "callback_handlers", property(lambda self: [h.callback for h in self.callback_query.handlers]))
if not hasattr(Dispatcher, "message_handlers"):
    setattr(Dispatcher, "message_handlers", property(lambda self: [h.callback for h in self.message.handlers]))

try:
    from aiogram.types import InputMediaPhoto, InputMediaVideo
except ImportError:
    class InputMediaPhoto:
        def __init__(self, media=None, caption=None, parse_mode=None, has_spoiler=False, **kwargs):
            self.type = "photo"
            self.media = media
            self.caption = caption
            self.parse_mode = parse_mode
            self.has_spoiler = has_spoiler

    class InputMediaVideo:
        def __init__(self, media=None, caption=None, parse_mode=None, has_spoiler=False, **kwargs):
            self.type = "video"
            self.media = media
            self.caption = caption
            self.parse_mode = parse_mode
            self.has_spoiler = has_spoiler

try:
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
except ImportError:
    class TelegramForbiddenError(Exception):
        pass

    class TelegramRetryAfter(Exception):
        def __init__(self, message="", retry_after=5, **kwargs):
            super().__init__(message)
            self.retry_after = retry_after

    class TelegramBadRequest(Exception):
        pass

from pushin_pay import PushinPayClient
from ai_engine import generate_ai_reply, _compute_effective_min_price

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("SharkBotTelegram")

_CURRENT_BOT_CTX: ContextVar[Optional[Bot]] = ContextVar("_CURRENT_BOT_CTX", default=None)
_CURRENT_BOT_TOKEN_CTX: ContextVar[str] = ContextVar("_CURRENT_BOT_TOKEN_CTX", default="")


def _create_bot_instance(token: str) -> Bot:
    try:
        import socket
        from aiogram.client.session.aiohttp import AiohttpSession
        session = AiohttpSession(timeout=120)
        if hasattr(session, "_connector_init") and isinstance(session._connector_init, dict):
            session._connector_init["family"] = socket.AF_INET
        return Bot(token=token, session=session)
    except Exception:
        return Bot(token=token)


class _RefCountedAsyncLock:
    """Lock assíncrono com contagem de referências.
    Garante que um lock NUNCA seja descartado prematuramente enquanto houver
    tarefas executando ou aguardando na fila pela mesma chave.
    """
    def __init__(self, key: str, container: dict):
        self.key = key
        self.container = container
        self.lock = asyncio.Lock()
        self.refs = 0

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            self.lock.release()
        finally:
            self.refs = max(0, self.refs - 1)
            if self.refs <= 0 and not self.lock.locked():
                self.container.pop(self.key, None)

    async def acquire(self):
        return await self.lock.acquire()

    def release(self):
        try:
            self.lock.release()
        finally:
            self.refs = max(0, self.refs - 1)
            if self.refs <= 0 and not self.lock.locked():
                self.container.pop(self.key, None)

    def locked(self) -> bool:
        return self.lock.locked()


class ActiveBotManager:
    """
    Gerencia as instâncias ativas dos Bots do Telegram em background (suporte multi-bot simultâneo),
    incluindo:
    - /start com variações de abertura e simulação de digitação ("typing...") ou Modo Fluxo por bot
    - Conversa com IA contextual + envio de mídias com Spoiler e Cache de file_id
    - Geração de PIX via Pushin Pay (Webhook + Polling automático de confirmação)
    - Entrega automática de link único do Canal VIP
    - Sequência de Downsell (até 6 etapas por inatividade)
    - Disparo automático de Upsell pós-compra
    - Registro em tempo real na Memória das Conversas e Aprendizado de Vendas
    """

    def __init__(self):
        self._primary_bot: Optional[Bot] = None
        self.extra_bots: Dict[str, Bot] = {}
        self.extra_polling_tasks: Dict[str, asyncio.Task] = {}
        self.dp: Optional[Dispatcher] = None
        self.polling_task: Optional[asyncio.Task] = None
        self.current_token: str = ""
        self.downsell_tasks: Dict[str, asyncio.Task] = {}
        self.pix_poll_tasks: Dict[str, asyncio.Task] = {}
        self.upsell_tasks: Dict[str, asyncio.Task] = {}
        self.active_tx_map: Dict[str, int] = {}
        self.tx_records: Dict[str, Dict[str, Any]] = {}
        self.lead_locks: Dict[str, _RefCountedAsyncLock] = {}
        self.tx_locks: Dict[str, _RefCountedAsyncLock] = {}
        self._active_update_tasks: Set[asyncio.Task] = set()
        self._delivery_retry_task: Optional[asyncio.Task] = None
        self.state_getter = None
        self.state_saver = None

    @property
    def bot(self) -> Optional[Bot]:
        ctx_bot = _CURRENT_BOT_CTX.get()
        if ctx_bot is not None:
            return ctx_bot
        return self._primary_bot

    @bot.setter
    def bot(self, value: Optional[Bot]):
        self._primary_bot = value
        if value is None:
            _CURRENT_BOT_CTX.set(None)

    @property
    def active_token(self) -> str:
        ctx_tok = _CURRENT_BOT_TOKEN_CTX.get()
        if ctx_tok:
            return ctx_tok
        return self.current_token

    def _get_bot_by_token(self, token: str) -> Optional[Bot]:
        clean = (token or "").strip()
        if clean:
            primary_tok = str(getattr(self._primary_bot, "token", "") or "")
            if self._primary_bot and ":" in primary_tok and ":" in clean and (
                primary_tok.split(":", 1)[0] == clean.split(":", 1)[0]
            ):
                return self._primary_bot
            if clean in self.extra_bots:
                candidate = self.extra_bots[clean]
                instance_tok = str(getattr(candidate, "token", "") or "")
                if ":" in instance_tok and instance_tok.split(":", 1)[0] == clean.split(":", 1)[0]:
                    return candidate
            if ":" in clean:
                prefix = clean.split(":", 1)[0]
                for k, b_inst in self.extra_bots.items():
                    instance_tok = str(getattr(b_inst, "token", "") or "")
                    if k.startswith(prefix + ":") and instance_tok.startswith(prefix + ":"):
                        return b_inst
            # A missing origin bot must never become the current/primary bot.
            return None
        return self.bot

    def _get_lead_lock(self, uid: str, bot_id: Optional[str] = None) -> _RefCountedAsyncLock:
        lock_key = f"{bot_id}:{uid}" if (bot_id and str(bot_id).strip()) else str(uid)
        if not hasattr(self, "lead_locks") or not isinstance(self.lead_locks, dict):
            self.lead_locks = {}
        entry = self.lead_locks.get(lock_key)
        if not entry:
            entry = _RefCountedAsyncLock(lock_key, self.lead_locks)
            self.lead_locks[lock_key] = entry
        entry.refs += 1
        return entry

    def _get_tx_lock(self, tx_id: str) -> _RefCountedAsyncLock:
        clean = str(tx_id or "").strip()
        if not hasattr(self, "tx_locks") or not isinstance(self.tx_locks, dict):
            self.tx_locks = {}
        entry = self.tx_locks.get(clean)
        if not entry:
            entry = _RefCountedAsyncLock(clean, self.tx_locks)
            self.tx_locks[clean] = entry
        entry.refs += 1
        return entry

    async def _sync_extra_bots(self, state: Dict[str, Any]):
        flow_bots = state.get("flow_bots") if isinstance(state, dict) else []
        if not isinstance(flow_bots, list):
            flow_bots = []

        desired_extra: Dict[str, Dict[str, Any]] = {}
        for fb in flow_bots:
            if not isinstance(fb, dict) or not fb.get("active", True):
                continue
            fb_tok = (fb.get("token") or "").strip()
            if (
                not fb_tok
                or fb_tok == "***configured***"
                or ":" not in fb_tok
                or fb_tok == self.current_token
            ):
                continue
            desired_extra[fb_tok] = fb

        # Encerra tasks de bots extras que foram desativados ou removidos
        for old_tok in list(self.extra_polling_tasks.keys()):
            t = self.extra_polling_tasks.get(old_tok)
            if old_tok not in desired_extra or (t and t.done()):
                self.extra_polling_tasks.pop(old_tok, None)
                if t and not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                old_bot = self.extra_bots.pop(old_tok, None)
                if old_bot:
                    try:
                        await old_bot.session.close()
                    except Exception:
                        pass

        state_changed = False
        for ext_tok, fb in desired_extra.items():
            existing_task = self.extra_polling_tasks.get(ext_tok)
            if existing_task and not existing_task.done() and ext_tok in self.extra_bots:
                continue
            ext_bot = None
            try:
                ext_bot = _create_bot_instance(ext_tok)
                me = await asyncio.wait_for(ext_bot.get_me(), timeout=10.0)
                if me.username and fb.get("username") != f"@{me.username}":
                    fb["username"] = f"@{me.username}"
                    state_changed = True
                if me.first_name and fb.get("first_name") != me.first_name:
                    fb["first_name"] = me.first_name
                    state_changed = True
                if me.id and str(fb.get("bot_id")) != str(me.id):
                    fb["bot_id"] = str(me.id)
                    state_changed = True
                self.extra_bots[ext_tok] = ext_bot
                self.extra_polling_tasks[ext_tok] = asyncio.create_task(
                    self._run_polling(bot_inst=ext_bot, bot_token=ext_tok)
                )
                logger.info(f"Bot adicional iniciado em produção: @{me.username}")
            except Exception as e:
                if ext_bot:
                    try:
                        await ext_bot.session.close()
                    except Exception:
                        pass
                logger.error(f"Falha ao iniciar bot adicional ({fb.get('username')}): {e}")

        if state_changed and self.state_saver:
            try:
                await self.state_saver(state, runtime_only=True)
            except Exception:
                pass

    async def configure_and_start(self, state_getter, state_saver):
        self.state_getter = state_getter
        self.state_saver = state_saver
        state = await self.state_getter()
        publish = state.get("publish", {})
        token = publish.get("bot_token", "").strip()
        if not token or token == "***configured***":
            for fb in (state.get("flow_bots") or []):
                if isinstance(fb, dict) and fb.get("active", True):
                    cand = (fb.get("token") or "").strip()
                    if cand and cand != "***configured***" and ":" in cand:
                        token = cand
                        publish["bot_token"] = cand
                        publish["is_live"] = True
                        break

        # Se há token preenchido, ativa por padrão a menos que explicitamente desativado
        if token and "is_live" not in publish:
            publish["is_live"] = True
        is_live = bool(publish.get("is_live", True)) if token else False

        if not token or not is_live:
            await self.stop()
            return {"status": "stopped", "reason": "Bot desativado ou sem token"}

        if token == self.current_token and self.polling_task and not self.polling_task.done():
            await self._sync_extra_bots(state)
            return {
                "status": "running",
                "bot_username": publish.get("bot_username", ""),
                "active_bots": 1 + len(self.extra_polling_tasks)
            }

        await self.stop()

        test_bot = None
        try:
            test_bot = _create_bot_instance(token)
            me = await asyncio.wait_for(test_bot.get_me(), timeout=10.0)
            publish["bot_username"] = f"@{me.username}"
            publish["bot_first_name"] = me.first_name
            publish["is_live"] = True
            for fb in (state.get("flow_bots") or []):
                if isinstance(fb, dict) and (fb.get("token") == token or str(fb.get("bot_id")) == str(me.id)):
                    fb["bot_id"] = str(me.id)
                    if me.username:
                        fb["username"] = f"@{me.username}"
                    if me.first_name:
                        fb["first_name"] = me.first_name
            await self.state_saver(state, runtime_only=True)
        except Exception as e:
            if test_bot:
                try:
                    await test_bot.session.close()
                except Exception:
                    pass
            logger.error(f"Token do Telegram inválido ({token[:10]}...): {e}")
            self.dp = Dispatcher()
            self._register_handlers()
            await self._sync_extra_bots(state)
            return {"status": "error", "message": f"Token inválido: {e}"}

        self.current_token = token
        self.bot = test_bot
        self.dp = Dispatcher()
        self._register_handlers()
        self.polling_task = asyncio.create_task(self._run_polling(bot_inst=test_bot, bot_token=token))
        await self._sync_extra_bots(state)
        self._resume_pending_pix_polls(state)
        if not self._delivery_retry_task or self._delivery_retry_task.done():
            self._delivery_retry_task = asyncio.create_task(self._delivery_retry_loop())
        logger.info(f"Bot iniciado em produção: @{me.username}")
        return {
            "status": "running",
            "bot_username": f"@{me.username}",
            "active_bots": 1 + len(self.extra_polling_tasks)
        }

    def _resume_pending_pix_polls(self, state: Dict[str, Any]):
        # Polling em background desativado: transações são tratadas 100% via webhooks
        return

    async def _delivery_retry_loop(self):
        """Worker em background que varre periodicamente transações 'paid' com 'delivered=False'.
        Garante entrega at-least-once mesmo após reinício ou falhas temporárias na API do Telegram.
        """
        while True:
            try:
                await asyncio.sleep(45)
                if not self.state_getter:
                    continue
                state = await self.state_getter()
                tx_map = state.get("tx_records") or {}
                now = datetime.now(timezone.utc)
                for tid, rec in list(tx_map.items()):
                    if not isinstance(rec, dict) or rec.get("status") != "paid" or rec.get("delivered"):
                        continue
                    try:
                        attempts = int(rec.get("automatic_delivery_attempts", rec.get("delivery_attempts") or 0))
                        if attempts != 1 and not (attempts == 0 and rec.get("initial_delivery_pending") is True):
                            # A known zero-attempt payment can get its initial send
                            # after its origin bot recovers. Legacy zero-attempt
                            # records have no reliable history: leave for review.
                            continue
                        last_attempt = rec.get("last_delivery_attempt_at")
                        if last_attempt:
                            dt_att = datetime.fromisoformat(str(last_attempt).replace("Z", "+00:00"))
                            if dt_att.tzinfo is None:
                                dt_att = dt_att.replace(tzinfo=timezone.utc)
                            if (now - dt_att).total_seconds() < 45:
                                continue
                        uid = int(rec.get("user_id") or 0)
                        if uid:
                            logger.info(f"Background retry worker tentando entrega de tx_id={tid} para user={uid}")
                            await self.deliver_purchase_and_upsell(uid, tx_id=tid)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"Entrega pendente com dados inválidos tx_id={tid}: {e}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Falha na entrega de tx_id={tid}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Aviso no delivery retry worker: {e}")
                await asyncio.sleep(10)

    async def stop(self):
        if hasattr(self, "_delivery_retry_task") and self._delivery_retry_task and not self._delivery_retry_task.done():
            self._delivery_retry_task.cancel()
            try:
                await self._delivery_retry_task
            except asyncio.CancelledError:
                pass
        self._delivery_retry_task = None

        if hasattr(self, "_active_update_tasks") and self._active_update_tasks:
            pending = list(self._active_update_tasks)
            for t in pending:
                t.cancel()
            try:
                await asyncio.wait(pending, timeout=2.0)
            except Exception:
                pass
            self._active_update_tasks.clear()

        for t in self.downsell_tasks.values():
            t.cancel()
        self.downsell_tasks.clear()

        for t in self.pix_poll_tasks.values():
            t.cancel()
        self.pix_poll_tasks.clear()

        for t in self.upsell_tasks.values():
            t.cancel()
        self.upsell_tasks.clear()

        for ext_tok, ext_task in list(self.extra_polling_tasks.items()):
            if ext_task and not ext_task.done():
                ext_task.cancel()
                try:
                    await ext_task
                except asyncio.CancelledError:
                    pass
        self.extra_polling_tasks.clear()

        for ext_tok, ext_bot in list(self.extra_bots.items()):
            try:
                await ext_bot.session.close()
            except Exception:
                pass
        self.extra_bots.clear()

        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        if self._primary_bot:
            try:
                await self._primary_bot.session.close()
            except Exception:
                pass
        self.bot = None
        self.dp = None
        self.current_token = ""

    async def _safe_feed_update(
        self,
        update: types.Update,
        bot_inst: Optional[Bot] = None,
        bot_token: str = ""
    ):
        target_bot = bot_inst or self._primary_bot
        target_token = bot_token or self.current_token
        _CURRENT_BOT_TOKEN_CTX.set(target_token)
        _CURRENT_BOT_CTX.set(target_bot)
        try:
            if self.dp and target_bot:
                await self.dp.feed_update(target_bot, update)
        except Exception as e:
            logger.error(f"Erro processando update {update.update_id}: {e}")

    async def _run_polling(self, bot_inst: Optional[Bot] = None, bot_token: str = ""):
        target_bot = bot_inst or self._primary_bot
        target_token = bot_token or self.current_token
        if not target_bot:
            return
        offset: Optional[int] = None
        bot_identifier = ""
        try:
            me = await target_bot.get_me()
            bot_identifier = f"@{me.username}" if me and me.username else f"ID {me.id}"
        except Exception:
            bot_identifier = f"Token ...{target_token[-6:]}" if len(target_token) > 6 else "Bot"

        try:
            await target_bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            logger.warning(f"Aviso ao limpar webhook para {bot_identifier}: {e}")

        update_sem = asyncio.Semaphore(30)

        async def _bounded_feed(upd):
            async with update_sem:
                await self._safe_feed_update(upd, bot_inst=target_bot, bot_token=target_token)

        while True:
            try:
                updates = await target_bot.get_updates(
                    offset=offset,
                    timeout=15,
                    allowed_updates=["message", "callback_query"]
                )
                for u in updates:
                    offset = u.update_id + 1
                    t = asyncio.create_task(_bounded_feed(u))
                    self._active_update_tasks.add(t)
                    t.add_done_callback(self._active_update_tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err_text = str(e)
                if "Conflict" in err_text or "terminated by other getUpdates" in err_text:
                    logger.warning(f"Polling [{bot_identifier}]: Conflito de instância (o Telegram detectou outro processo rodando com este mesmo bot: {err_text}). Aguardando 5s...")
                    await asyncio.sleep(5)
                else:
                    logger.warning(f"Polling [{bot_identifier}] aguardando liberação de instância ({err_text})...")
                    await asyncio.sleep(3)

    def _get_cadence_delay(self, state: Dict[str, Any]) -> float:
        cadence = state.get("style", {}).get("cadence", "Natural")
        if cadence == "Ágil":
            return 0.6
        if cadence in ("Calmo", "Calma"):
            return 2.4
        return 1.3

    STAGE_ORDER = [
        "Conversando",
        "Viu os planos",
        "Escolheu um plano",
        "Gerou checkout",
        "Pagamento aprovado",
        "Entrega concluída",
        "Recebeu upsell",
        "Escolheu upsell",
        "Checkout de upsell",
        "Comprou upsell",
    ]

    def _advance_stage(self, lead: Dict[str, Any], target_stage: str, reset: bool = False):
        if reset:
            lead["stage"] = "Conversando"
            lead["stage_history"] = ["Conversando"]
            return
        curr = lead.get("stage") or "Conversando"
        if curr == "Em conversa":
            curr = "Conversando"
            lead["stage"] = "Conversando"
        hist: List[str] = lead.setdefault("stage_history", ["Conversando"])
        if "Conversando" not in hist:
            hist.insert(0, "Conversando")
        if curr in self.STAGE_ORDER and curr not in hist:
            hist.append(curr)

        curr_idx = self.STAGE_ORDER.index(curr) if curr in self.STAGE_ORDER else 0
        target_idx = self.STAGE_ORDER.index(target_stage) if target_stage in self.STAGE_ORDER else 0
        if target_idx >= curr_idx:
            lead["stage"] = target_stage
        if target_stage not in hist:
            hist.append(target_stage)

    def _update_lead_insights(
        self,
        state: Dict[str, Any],
        lead: Dict[str, Any],
        user_text: str = "",
        chosen_plan: str = ""
    ):
        t = (user_text or "").lower()
        prefs: List[str] = lead.setdefault("prefs_list", [])
        objs: List[str] = lead.setdefault("objs_list", [])

        if chosen_plan:
            tag = f"Plano: {chosen_plan}"
            if tag not in prefs:
                prefs.append(tag)

        pref_map = [
            (["vídeo", "video", "videos", "vídeos"], "Vídeos exclusivos"),
            (["foto", "fotos", "pack"], "Fotos / Packs"),
            (["chamada", "call", "ao vivo"], "Chamada de vídeo"),
            (["vip", "grupo", "canal", "acesso"], "Acesso ao Grupo VIP"),
            (["vitalício", "vitalicio", "permanente"], "Plano Vitalício"),
            (["mensal", "30 dias", "mês", "mes"], "Plano Mensal"),
            (["personalizado", "fetiche", "exclusivo"], "Conteúdo personalizado"),
        ]
        for keywords, label in pref_map:
            if any(k in t for k in keywords) and label not in prefs:
                prefs.append(label)

        if prefs:
            lead["preferences"] = " • ".join(prefs[:4])
        elif not lead.get("preferences"):
            lead["preferences"] = "Iniciou conversa no bot"

        obj_map = [
            (["caro", "desconto", "barato", "sem dinheiro", "grana", "menor valor", "abaixa", "menos"], "Preço / Pediu desconto"),
            (["prévia", "previa", "amostra", "ver antes", "confiar", "real", "fake", "golpe"], "Pediu prévia / Prova social"),
            (["cartão", "cartao", "boleto", "como paga", "sigilo", "discreto", "extrato"], "Dúvida sobre pagamento / Sigilo"),
            (["depois", "amanhã", "amanha", "pensar", "agora não", "agora nao", "mais tarde"], "Deixou para depois"),
        ]
        for keywords, label in obj_map:
            if any(k in t for k in keywords) and label not in objs:
                objs.append(label)

        if objs:
            lead["objections"] = " • ".join(objs[:3])
        elif not lead.get("objections"):
            lead["objections"] = "Nenhuma identificada"

        # Atualiza métricas globais de aprendizado
        leads = state.get("memory", {}).get("leads", [])
        learning = state.setdefault("learning", {})
        learning["evaluated_results"] = len(leads)
        paid_stages = {
            "Pagamento aprovado",
            "Entrega concluída",
            "Recebeu upsell",
            "Escolheu upsell",
            "Checkout de upsell",
            "Comprou upsell",
        }
        learning["conversations_without_purchase"] = sum(
            1 for l in leads if l.get("stage") not in paid_stages
        )

        # Consolida objeções mais comuns e planos mais escolhidos
        obj_counts: Dict[str, int] = {}
        plan_counts: Dict[str, int] = {}
        for l in leads:
            for o_item in l.get("objs_list", []):
                obj_counts[o_item] = obj_counts.get(o_item, 0) + 1
            if l.get("chosen_plan"):
                p_name = l["chosen_plan"]
                plan_counts[p_name] = plan_counts.get(p_name, 0) + 1

        learning["common_objections"] = [
            {"name": k, "count": v}
            for k, v in sorted(obj_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        ]
        learning["top_plans"] = [
            {"name": k, "count": v}
            for k, v in sorted(plan_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        ]

    async def _get_or_create_lead(self, state: Dict[str, Any], user: types.User, bot_token: str = "") -> Dict[str, Any]:
        memory = state.setdefault("memory", {"leads": []})
        leads: List[Dict[str, Any]] = memory.setdefault("leads", [])
        uid = str(user.id)
        effective_tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.active_token or "").strip()
        active_fb = self._get_active_flow_bot(state, bot_token=effective_tok)
        bot_id_str = str(active_fb.get("id") or "") if active_fb else ""
        bot_uname_str = str(active_fb.get("username") or state.get("publish", {}).get("bot_username") or "@bot") if active_fb else str(state.get("publish", {}).get("bot_username") or "@bot")
        bot_mode_str = "flow" if active_fb else "ai"
        actual_token = (effective_tok or getattr(self.bot, "token", "") or self.current_token or "")
        origin_bot_numeric_id = str(actual_token).split(":", 1)[0] if ":" in str(actual_token) else ""
        if not origin_bot_numeric_id.isdecimal():
            origin_bot_numeric_id = ""

        for lead in leads:
            lead_uid = str(lead.get("telegram_id") or "")
            lead_bid = str(lead.get("bot_id") or "")
            lead_origin = str(lead.get("origin_bot_numeric_id") or "")
            matches = (lead_uid == uid) and (
                (bool(bot_id_str) and lead_bid == bot_id_str)
                or (not bot_id_str and not lead_bid)
            ) and bool(origin_bot_numeric_id) and lead_origin == origin_bot_numeric_id
            if matches:
                lead["first_name"] = user.first_name or lead.get("first_name", "Visitante")
                lead["username"] = f"@{user.username}" if user.username else lead.get("username", "")
                lead["last_interaction"] = datetime.utcnow().strftime("%d/%m %H:%M")
                lead.setdefault("created_at", datetime.utcnow().isoformat())
                if bot_id_str:
                    lead["bot_id"] = bot_id_str
                if bot_uname_str:
                    lead["bot_username"] = bot_uname_str
                lead["bot_mode"] = bot_mode_str
                if lead.get("stage") == "Em conversa" or not lead.get("stage"):
                    lead["stage"] = "Conversando"
                lead.setdefault("stage_history", ["Conversando"])
                return lead

        new_lead = {
            "telegram_id": uid,
            "first_name": user.first_name or "Visitante",
            "username": f"@{user.username}" if user.username else "Sem @user",
            "stage": "Conversando",
            "stage_history": ["Conversando"],
            "created_at": datetime.utcnow().isoformat(),
            "last_interaction": datetime.utcnow().strftime("%d/%m %H:%M"),
            "bot_id": bot_id_str,
            "origin_bot_numeric_id": origin_bot_numeric_id,
            "bot_username": bot_uname_str,
            "bot_mode": bot_mode_str,
            "blocked": False,
            "preferences": "Iniciou conversa no bot",
            "objections": "Nenhuma identificada",
            "prefs_list": [],
            "objs_list": [],
            "sent_downsell_steps": [],
            "history": []
        }
        leads.insert(0, new_lead)
        self._update_lead_insights(state, new_lead)
        return new_lead

    def _build_offers_keyboard(
        self,
        state: Dict[str, Any],
        only_offer: Optional[Dict[str, Any]] = None,
        is_upsell: bool = False
    ) -> InlineKeyboardMarkup:
        offers = [o for o in state.get("business", {}).get("offers", []) if o.get("visible", True)]
        if only_offer and (only_offer.get("discounted") or only_offer.get("specific_match") or not offers):
            offers = [only_offer]
        buttons = []
        suffix = ":up" if is_upsell else ""
        for idx, o in enumerate(offers):
            emoji = o.get("emoji", "💎")
            name = o.get("name", "Acesso VIP")
            price = float(o.get("price", 19))
            oid = str(o.get("id", f"offer_{idx}"))[:20]
            buttons.append([
                InlineKeyboardButton(
                    text=f"{emoji} {name} — R$ {price:.2f}".replace(".", ","),
                    callback_data=f"buy:{oid}:{price:.2f}{suffix}"
                )
            ])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def _get_active_flow_bot(
        self,
        state: Dict[str, Any],
        bot_token: str = "",
        bot_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        flow_bots = state.get("flow_bots", [])
        if not isinstance(flow_bots, list) or not flow_bots:
            return None

        ctx_tok = _CURRENT_BOT_TOKEN_CTX.get() or ""
        bot_inst_tok = getattr(self.bot, "token", None)
        if not isinstance(bot_inst_tok, str):
            bot_inst_tok = ""
        eff_token = (bot_token or ctx_tok or bot_inst_tok or self.current_token or "").strip()
        eff_token_prefix = eff_token.split(":")[0].strip() if ":" in eff_token else ""
        eff_bot_id = (bot_id or "").strip()

        # 1. Se bot_id foi especificado, casa estritamente pelo identificador configurado
        if eff_bot_id:
            for fb in flow_bots:
                if not isinstance(fb, dict):
                    continue
                fb_id = str(fb.get("id") or "").strip()
                if fb_id == eff_bot_id:
                    # Both identifiers present in a purchase must identify the same bot.
                    explicit_token = str(bot_token or "").strip()
                    if explicit_token and explicit_token != "***configured***":
                        fb_tok = str(fb.get("token") or "").strip()
                        fb_tg_id = str(fb.get("bot_id") or "").strip()
                        prefix = explicit_token.split(":", 1)[0] if ":" in explicit_token else ""
                        # The configured token is the actual Telegram identity;
                        # bot_id is only metadata and can be stale after a swap.
                        if not (prefix and fb_tok.startswith(prefix + ":")
                                and (not fb_tg_id or fb_tg_id == prefix)):
                            return None
                    if fb.get("active", True) and fb.get("mode", "flow") == "flow":
                        return fb
                    return None
            return None

        # 2. Se token foi especificado, casa estritamente pelo token ou prefixo numérico do bot
        if eff_token:
            for fb in flow_bots:
                if not isinstance(fb, dict):
                    continue
                fb_tok = str(fb.get("token") or "").strip()
                fb_tg_id = str(fb.get("bot_id") or "").strip()
                if eff_token_prefix and fb_tok.startswith(eff_token_prefix + ":"):
                    if fb_tg_id and fb_tg_id != eff_token_prefix:
                        return None
                    if fb.get("active", True) and fb.get("mode", "flow") == "flow":
                        return fb
                    return None
            return None

        # 3. Se nenhum identificador foi fornecido, apenas retorna se houver um único bot ativo sem ambiguidade
        active_flows = [
            fb for fb in flow_bots
            if isinstance(fb, dict) and fb.get("active", True) and fb.get("mode", "flow") == "flow"
        ]
        if len(active_flows) == 1:
            return active_flows[0]
        return None

    def _render_flow_vars(
        self,
        text: str,
        user: types.User,
        bot_cfg: Dict[str, Any],
        plan_name: str = "",
        valor_str: str = "",
        pix_code_fmt: str = ""
    ) -> str:
        if not text:
            return ""
        brt_hour = (datetime.utcnow() - timedelta(hours=3)).hour
        if 5 <= brt_hour < 12:
            saudacao = "Bom dia"
        elif 12 <= brt_hour < 18:
            saudacao = "Boa tarde"
        else:
            saudacao = "Boa noite"
        nome = html.escape((user.first_name or "Cliente").strip())
        rep = {
            "{saudacao}": saudacao,
            "{nome}": nome,
            "{primeiro_nome}": nome,
            "{estado}": "São Paulo",
            "{uf}": "SP",
            "{cidade}": "São Paulo",
            "{localizacao}": "São Paulo - SP",
            "{bot.nome}": html.escape(str(bot_cfg.get("first_name") or "VIP Bot")),
            "{bot.username}": html.escape(str(bot_cfg.get("username") or "@bot")),
            "{plano}": html.escape(str(plan_name)),
            "{valor}": valor_str,
            "{pix_code}": pix_code_fmt,
        }
        out = text
        for k, v in rep.items():
            out = out.replace(k, str(v))
        return out

    async def _mark_lead_blocked(self, chat_id: Any, bot_token: Optional[str] = None, bot_id: Optional[str] = None):
        try:
            uid_str = str(chat_id).strip()
            eff_tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or "").strip()
            bot_prefix = eff_tok.split(":", 1)[0] if (eff_tok and ":" in eff_tok) else (str(bot_id).strip() if bot_id else "")
            self._cancel_downsell(uid_str, bot_id=bot_prefix or None)
            self._cancel_upsell(uid_str, bot_id=bot_prefix or None)
            if self.state_getter and self.state_saver:
                state = await self.state_getter()
                memory = state.get("memory") or {}
                changed = False
                for ld in memory.get("leads", []):
                    if str(ld.get("telegram_id") or "") != uid_str:
                        continue
                    if bot_prefix:
                        lead_origin = str(ld.get("origin_bot_numeric_id") or "")
                        lead_bid = str(ld.get("bot_id") or "")
                        if lead_origin and lead_origin != bot_prefix and lead_bid != bot_prefix:
                            continue
                    if not ld.get("blocked"):
                        ld["blocked"] = True
                        ld["stage"] = "Bloqueou o bot"
                        changed = True
                    if bot_prefix:
                        break
                if changed:
                    await self.state_saver(state, runtime_only=True)
        except Exception as err:
            logger.warning(f"Aviso ao registrar bloqueio de lead {chat_id}: {err}")

    async def _safe_send_html(
        self,
        chat_id: Any,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        bot_instance: Optional[Bot] = None
    ):
        target_bot = bot_instance or _CURRENT_BOT_CTX.get() or self.bot
        if not target_bot:
            return None
        target_bot_token = getattr(target_bot, "token", "") or _CURRENT_BOT_TOKEN_CTX.get() or ""
        extra_kw = {"reply_markup": reply_markup} if reply_markup is not None else {}
        try:
            return await target_bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", **extra_kw)
        except Exception as e:
            err_s = str(e).lower()
            if isinstance(e, TelegramForbiddenError) or any(
                x in err_s for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")
            ):
                await self._mark_lead_blocked(chat_id, bot_token=target_bot_token)
                return None

            if isinstance(e, TelegramRetryAfter) or "retry after" in err_s or "flood control" in err_s:
                wait_sec = getattr(e, "retry_after", 3.0)
                if not isinstance(wait_sec, (int, float)):
                    digits = "".join(ch for ch in err_s if ch.isdigit())
                    wait_sec = float(digits) if digits else 2.0
                await asyncio.sleep(min(float(wait_sec), 10.0))
                try:
                    return await target_bot.send_message(chat_id=chat_id, text=text, **extra_kw)
                except Exception as e_after:
                    err_sa = str(e_after).lower()
                    if isinstance(e_after, TelegramForbiddenError) or any(
                        x in err_sa for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")
                    ):
                        await self._mark_lead_blocked(chat_id, bot_token=target_bot_token)
                    return None

            try:
                return await target_bot.send_message(chat_id=chat_id, text=text, **extra_kw)
            except Exception as e2:
                err_s2 = str(e2).lower()
                if isinstance(e2, TelegramForbiddenError) or any(
                    x in err_s2 for x in ("blocked by the user", "user is deactivated", "forbidden", "chat not found")
                ):
                    await self._mark_lead_blocked(chat_id, bot_token=target_bot_token)
                return None

    def _parse_discount_pct(self, disc_str: str) -> int:
        if not disc_str or "sem" in disc_str.lower():
            return 0
        digits = "".join(ch for ch in str(disc_str) if ch.isdigit())
        return int(digits) if digits else 0

    def _build_flow_plans_kb(
        self,
        bot_cfg: Dict[str, Any],
        filter_plan_ids: Optional[List[str]] = None,
        discount_str: str = "Sem desconto"
    ) -> Optional[InlineKeyboardMarkup]:
        plans = bot_cfg.get("plans", [])
        if not plans:
            return None
        disc_pct = self._parse_discount_pct(discount_str)
        color_emoji = {
            "Azul": "🔵 ",
            "Verde": "🟢 ",
            "Vermelho": "🔴 ",
            "Padrão": ""
        }
        buttons = []
        for idx, p in enumerate(plans):
            pid = str(p.get("id", f"plan_{idx+1}"))[:20]
            if filter_plan_ids and pid not in filter_plan_ids:
                continue
            name = p.get("name") or f"Plano {idx+1}"
            raw_price = float(p.get("price") or 0)
            final_price = round(raw_price * (1.0 - disc_pct / 100.0), 2) if disc_pct > 0 else raw_price
            price_fmt = f"{final_price:.2f}".replace(".", ",")
            prefix = color_emoji.get(p.get("button_color", "Padrão"), "")
            buttons.append([
                InlineKeyboardButton(
                    text=f"{prefix}{name} — R$ {price_fmt}",
                    callback_data=f"fplan:{pid}:{disc_pct}"
                )
            ])
        return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    def _get_candidate_cache_channels(
        self,
        state: Optional[Dict[str, Any]],
        fb: Optional[Dict[str, Any]] = None
    ) -> List[Any]:
        if not isinstance(state, dict):
            return []
        if fb is None:
            fb = self._get_active_flow_bot(state)

        raw_candidates: List[str] = []
        if isinstance(fb, dict):
            if fb.get("cache_channel_id"):
                raw_candidates.append(str(fb["cache_channel_id"]).strip())
            for p in (fb.get("plans") or []):
                if isinstance(p, dict) and p.get("delivery_channel_id"):
                    raw_candidates.append(str(p["delivery_channel_id"]).strip())
            fb_deliv = fb.get("fallback_delivery")
            if isinstance(fb_deliv, dict) and fb_deliv.get("channel_id"):
                raw_candidates.append(str(fb_deliv["channel_id"]).strip())

        pub = state.get("publish") if isinstance(state.get("publish"), dict) else {}
        if pub.get("cache_channel_id"):
            raw_candidates.append(str(pub["cache_channel_id"]).strip())
        dest = str(pub.get("delivery_destination") or "").strip()
        if dest.startswith("-100") or dest.startswith("@"):
            raw_candidates.append(dest)

        primary: List[Any] = []
        fallback_placeholder: List[Any] = []
        seen = set()
        for ch_str in raw_candidates:
            if not ch_str or ch_str in seen:
                continue
            seen.add(ch_str)
            parsed = int(ch_str) if ch_str.lstrip("-").isdigit() else ch_str
            if ch_str == "-1003939135982":
                fallback_placeholder.append(parsed)
            else:
                primary.append(parsed)

        return primary if primary else fallback_placeholder

    def _extract_sent_file_id(self, sent_msg: Any, default_kind: str = "photo"):
        if not sent_msg:
            return None, default_kind
        vid = getattr(sent_msg, "video", None)
        if vid and getattr(vid, "file_id", None):
            return str(vid.file_id), "video"
        anim = getattr(sent_msg, "animation", None)
        if anim and getattr(anim, "file_id", None):
            return str(anim.file_id), "animation"
        photos = getattr(sent_msg, "photo", None)
        if photos and len(photos) > 0 and getattr(photos[-1], "file_id", None):
            return str(photos[-1].file_id), "photo"
        doc = getattr(sent_msg, "document", None)
        if doc and getattr(doc, "file_id", None):
            return str(doc.file_id), "document"
        return None, default_kind

    async def _send_by_cached_fid(
        self,
        bot_instance: Bot,
        chat_id: Any,
        fid: str,
        send_as: str,
        caption: Optional[str] = None,
        has_spoiler: bool = False,
        reply_markup: Optional[InlineKeyboardMarkup] = None
    ):
        extra_kw = {"reply_markup": reply_markup} if reply_markup is not None else {}
        if send_as == "document":
            return await bot_instance.send_document(chat_id=chat_id, document=fid, caption=caption, **extra_kw)
        if send_as == "animation":
            try:
                return await bot_instance.send_animation(chat_id=chat_id, animation=fid, caption=caption, has_spoiler=has_spoiler, **extra_kw)
            except Exception:
                return await bot_instance.send_video(chat_id=chat_id, video=fid, caption=caption, has_spoiler=has_spoiler, **extra_kw)
        if send_as == "video":
            try:
                return await bot_instance.send_video(chat_id=chat_id, video=fid, caption=caption, has_spoiler=has_spoiler, **extra_kw)
            except Exception:
                return await bot_instance.send_document(chat_id=chat_id, document=fid, caption=caption, **extra_kw)
        try:
            return await bot_instance.send_photo(chat_id=chat_id, photo=fid, caption=caption, has_spoiler=has_spoiler, **extra_kw)
        except Exception:
            return await bot_instance.send_document(chat_id=chat_id, document=fid, caption=caption, **extra_kw)

    async def warm_media_item_in_channel(
        self,
        m: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        fb: Optional[Dict[str, Any]] = None,
        bot_instance: Optional[Bot] = None,
        exclude_chat_id: Optional[int] = None
    ) -> Optional[str]:
        if not isinstance(m, dict):
            return None
        existing_fid = (m.get("telegram_file_id") or "").strip()
        if existing_fid:
            return existing_fid

        url = m.get("url")
        if not url or str(url).startswith("tg://"):
            return None

        target_bot = bot_instance or self.bot
        if not target_bot:
            return None

        mtype = (m.get("type") or "").lower()
        fname = str(m.get("name") or "")
        is_video = (
            "video" in mtype
            or "vídeo" in mtype
            or str(url).startswith("data:video")
            or fname.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
        )
        channels = self._get_candidate_cache_channels(state, fb)
        last_error = ""

        for cache_ch in channels:
            if exclude_chat_id is not None and str(cache_ch) == str(exclude_chat_id):
                continue
            try:
                default_fn = fname or ("media.mp4" if is_video else "media.jpg")
                inp_cache = self._build_input_file(url, default_fn)
                if not inp_cache:
                    return None
                sent_cache = None
                try:
                    if is_video:
                        sent_cache = await target_bot.send_video(
                            chat_id=cache_ch,
                            video=inp_cache,
                            caption=f"📦 Cache Mídia: {default_fn}",
                            disable_notification=True,
                        )
                    else:
                        sent_cache = await target_bot.send_photo(
                            chat_id=cache_ch,
                            photo=inp_cache,
                            caption=f"📦 Cache Mídia: {default_fn}",
                            disable_notification=True,
                        )
                except Exception as primary_err:
                    # Fallback para send_document caso a imagem tenha dimensão > 10000px ou formato atípico
                    if hasattr(target_bot, "send_document"):
                        inp_doc = self._build_input_file(url, default_fn)
                        sent_cache = await target_bot.send_document(
                            chat_id=cache_ch,
                            document=inp_doc,
                            caption=f"📦 Cache Mídia: {default_fn}",
                            disable_notification=True,
                        )
                    else:
                        raise primary_err

                fid, send_as = self._extract_sent_file_id(sent_cache, "video" if is_video else "photo")
                if fid:
                    m["telegram_file_id"] = str(fid)
                    m["telegram_send_as"] = send_as
                    m["cached_channel_id"] = str(cache_ch)
                    m.pop("_cache_error", None)
                    if str(url).startswith("data:video") or len(str(url)) > 350000:
                        m["url"] = f"tg://file_id/{fid}"
                    logger.info(f"Mídia '{default_fn}' salva em cache no canal {cache_ch} ({send_as}) -> file_id={fid[:18]}...")
                    return str(fid)
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Upload de cache no canal ({cache_ch}) falhou para '{m.get('name')}': {e}")

        if last_error:
            m["_cache_error"] = last_error
        return None

    def _is_video_media(self, m: Dict[str, Any]) -> bool:
        mtype = (m.get("type") or "").lower()
        fname = str(m.get("name") or "")
        url = str(m.get("url") or "")
        send_as = (m.get("telegram_send_as") or "").lower()
        return (
            send_as in ("video", "animation")
            or "video" in mtype
            or "vídeo" in mtype
            or url.startswith("data:video")
            or fname.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
        )

    async def _download_file_by_id(self, bot_instance: Bot, file_id: str) -> Optional[bytes]:
        if not bot_instance or not file_id:
            return None
        try:
            import io
            buf = io.BytesIO()
            if hasattr(bot_instance, "download"):
                await bot_instance.download(file_id, destination=buf)
                data = buf.getvalue()
                if data:
                    return data
            if hasattr(bot_instance, "get_file") and hasattr(bot_instance, "download_file"):
                f_info = await bot_instance.get_file(file_id)
                await bot_instance.download_file(f_info.file_path, destination=buf)
                data = buf.getvalue()
                if data:
                    return data
        except Exception as e:
            logger.warning(f"Falha ao baixar arquivo {file_id[:16]} do Telegram: {e}")
        return None

    async def _build_group_media_input(
        self,
        m: Dict[str, Any],
        bot_instance: Bot,
        force_upload: bool = False,
        caption: Optional[str] = None
    ):
        is_video = self._is_video_media(m)
        fid = (m.get("telegram_file_id") or "").strip()
        send_as = (m.get("telegram_send_as") or "").lower()
        fname = str(m.get("name") or ("media.mp4" if is_video else "media.jpg"))
        url = str(m.get("url") or "")

        # No Telegram, file_ids de Animation (CgAC...) ou Document (BQAC...) não são aceitos em sendMediaGroup
        # sem reenviar os bytes dentro de InputMediaVideo / InputMediaPhoto para gerar um file_id nativo de álbum.
        needs_upload = (
            force_upload
            or not fid
            or send_as in ("animation", "document")
            or fid.startswith(("CgAC", "BQAC"))
        )

        media_ref = None
        if not needs_upload:
            media_ref = fid
        else:
            if url and not url.startswith("tg://"):
                media_ref = self._build_input_file(url, fname)
            elif fid:
                raw_bytes = await self._download_file_by_id(bot_instance, fid)
                if raw_bytes:
                    media_ref = BufferedInputFile(raw_bytes, filename=fname)
                else:
                    media_ref = fid
        if not media_ref:
            return None

        has_spoiler = bool(m.get("spoiler", False))
        if is_video:
            return InputMediaVideo(media=media_ref, caption=caption, has_spoiler=has_spoiler)
        return InputMediaPhoto(media=media_ref, caption=caption, has_spoiler=has_spoiler)

    async def warm_media_group_in_channel(
        self,
        mlist: List[Dict[str, Any]],
        state: Optional[Dict[str, Any]],
        fb: Optional[Dict[str, Any]] = None,
        bot_instance: Optional[Bot] = None,
        exclude_chat_id: Optional[int] = None,
        force_upload: bool = False
    ) -> int:
        valid_items = [
            m for m in (mlist or [])[:3]
            if isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
        ]
        if len(valid_items) < 2:
            return 0

        target_bot = bot_instance or self.bot
        if not target_bot or not hasattr(target_bot, "send_media_group"):
            return 0

        needs_group_warm = force_upload or any(
            not (m.get("telegram_file_id") or "").strip()
            or (m.get("telegram_send_as") or "").lower() in ("animation", "document")
            or str(m.get("telegram_file_id") or "").startswith(("CgAC", "BQAC"))
            or not m.get("album_cached")
            for m in valid_items
        )
        if not needs_group_warm:
            return 0

        channels = self._get_candidate_cache_channels(state, fb)
        for cache_ch in channels:
            if exclude_chat_id is not None and str(cache_ch) == str(exclude_chat_id):
                continue
            try:
                group_inputs = []
                for idx, m in enumerate(valid_items):
                    cap = "📦 Cache Álbum (Lado a Lado)" if idx == 0 else None
                    inp_item = await self._build_group_media_input(
                        m,
                        target_bot,
                        force_upload=force_upload,
                        caption=cap
                    )
                    if not inp_item:
                        group_inputs = []
                        break
                    group_inputs.append(inp_item)

                if len(group_inputs) < 2:
                    continue

                sent_msgs = await target_bot.send_media_group(
                    chat_id=cache_ch,
                    media=group_inputs,
                    disable_notification=True
                )
                if isinstance(sent_msgs, list) and len(sent_msgs) == len(valid_items):
                    warmed_count = 0
                    for m, sent_msg in zip(valid_items, sent_msgs):
                        is_vid = self._is_video_media(m)
                        fid, detected_kind = self._extract_sent_file_id(sent_msg, "video" if is_vid else "photo")
                        if fid:
                            m["telegram_file_id"] = str(fid)
                            m["telegram_send_as"] = detected_kind
                            m["cached_channel_id"] = str(cache_ch)
                            m["album_cached"] = True
                            m.pop("_cache_error", None)
                            url = str(m.get("url") or "")
                            if url.startswith("data:video") or len(url) > 350000:
                                m["url"] = f"tg://file_id/{fid}"
                            warmed_count += 1
                    logger.info(f"Álbum com {warmed_count} mídias salvo lado a lado no canal {cache_ch}")
                    return warmed_count
            except Exception as e:
                logger.warning(f"Falha ao aquecer álbum de mídias no canal {cache_ch}: {e}")
                if not force_upload:
                    # Tenta forçando reupload dos bytes caso um file_id antigo fosse incompatível com álbum
                    try:
                        group_inputs_retry = []
                        for idx, m in enumerate(valid_items):
                            cap = "📦 Cache Álbum (Lado a Lado)" if idx == 0 else None
                            inp_item = await self._build_group_media_input(
                                m,
                                target_bot,
                                force_upload=True,
                                caption=cap
                            )
                            if inp_item:
                                group_inputs_retry.append(inp_item)
                        if len(group_inputs_retry) == len(valid_items):
                            sent_msgs = await target_bot.send_media_group(
                                chat_id=cache_ch,
                                media=group_inputs_retry,
                                disable_notification=True
                            )
                            if isinstance(sent_msgs, list) and len(sent_msgs) == len(valid_items):
                                warmed_count = 0
                                for m, sent_msg in zip(valid_items, sent_msgs):
                                    is_vid = self._is_video_media(m)
                                    fid, detected_kind = self._extract_sent_file_id(sent_msg, "video" if is_vid else "photo")
                                    if fid:
                                        m["telegram_file_id"] = str(fid)
                                        m["telegram_send_as"] = detected_kind
                                        m["cached_channel_id"] = str(cache_ch)
                                        m["album_cached"] = True
                                        m.pop("_cache_error", None)
                                        url = str(m.get("url") or "")
                                        if url.startswith("data:video") or len(url) > 350000:
                                            m["url"] = f"tg://file_id/{fid}"
                                        warmed_count += 1
                                return warmed_count
                    except Exception as retry_err:
                        logger.warning(f"Retry de álbum no canal {cache_ch} falhou: {retry_err}")
        return 0

    async def warm_all_state_medias(self, state: Dict[str, Any]) -> int:
        if not isinstance(state, dict):
            return 0
        warmed = 0
        for fb in (state.get("flow_bots") or []):
            if not isinstance(fb, dict):
                continue
            tok = (fb.get("token") or state.get("publish", {}).get("bot_token") or "").strip()
            if not tok or tok == "***configured***":
                tok = self.current_token

            if not tok:
                continue

            temp_bot = None
            bot_to_use = self._get_bot_by_token(tok) if (self._primary_bot or self.extra_bots) else None
            if bot_to_use and tok not in (self.current_token, *self.extra_bots.keys()):
                bot_to_use = None
            if not bot_to_use:
                try:
                    temp_bot = _create_bot_instance(tok)
                    bot_to_use = temp_bot
                except Exception:
                    continue

            try:
                media_lists: List[List[Dict[str, Any]]] = []
                if isinstance(fb.get("welcome"), dict) and isinstance(fb["welcome"].get("medias"), list):
                    media_lists.append(fb["welcome"]["medias"])
                for ds_key in ("downsell_general", "downsell_pix"):
                    ds_obj = fb.get(ds_key)
                    if isinstance(ds_obj, dict):
                        for seq in (ds_obj.get("sequences") or []):
                            if isinstance(seq, dict) and isinstance(seq.get("medias"), list):
                                media_lists.append(seq["medias"])
                for ob in (fb.get("order_bumps") or []):
                    if isinstance(ob, dict) and isinstance(ob.get("medias"), list):
                        media_lists.append(ob["medias"])
                if isinstance(fb.get("payments"), dict):
                    if isinstance(fb["payments"].get("generated_medias"), list):
                        media_lists.append(fb["payments"]["generated_medias"])
                    if isinstance(fb["payments"].get("approved_medias"), list):
                        media_lists.append(fb["payments"]["approved_medias"])

                for mlist in media_lists:
                    valid_in_list = [
                        m for m in mlist[:3]
                        if isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
                    ]
                    if len(valid_in_list) >= 2 and hasattr(bot_to_use, "send_media_group"):
                        gw = await self.warm_media_group_in_channel(valid_in_list, state, fb=fb, bot_instance=bot_to_use)
                        if gw > 0:
                            warmed += gw
                            continue
                    for m in mlist:
                        if isinstance(m, dict) and not (m.get("telegram_file_id") or "").strip() and m.get("url"):
                            fid = await self.warm_media_item_in_channel(m, state, fb=fb, bot_instance=bot_to_use)
                            if fid:
                                warmed += 1
            finally:
                if temp_bot:
                    try:
                        await temp_bot.session.close()
                    except Exception:
                        pass

        # Mídias do modo IA (apenas uploads locais data: ou mídias ativas)
        active_fb = self._get_active_flow_bot(state)
        bot_for_ai = self.bot
        if bot_for_ai:
            for m in (state.get("medias") or []):
                if isinstance(m, dict) and not (m.get("telegram_file_id") or "").strip():
                    u = str(m.get("url") or "")
                    if u.startswith("data:") or m.get("active"):
                        fid = await self.warm_media_item_in_channel(m, state, fb=active_fb, bot_instance=bot_for_ai)
                        if fid:
                            warmed += 1
            upsell = state.get("upsell")
            if isinstance(upsell, dict):
                up_medias = upsell.get("medias") or []
                if len(up_medias) >= 2 and hasattr(bot_for_ai, "send_media_group"):
                    gw = await self.warm_media_group_in_channel(up_medias, state, fb=active_fb, bot_instance=bot_for_ai)
                    warmed += gw
                else:
                    for m in up_medias:
                        if isinstance(m, dict) and not (m.get("telegram_file_id") or "").strip():
                            u = str(m.get("url") or "")
                            if u.startswith("data:") or upsell.get("enabled"):
                                fid = await self.warm_media_item_in_channel(m, state, fb=active_fb, bot_instance=bot_for_ai)
                                if fid:
                                    warmed += 1

        return warmed

    async def _send_single_media_cached(
        self,
        chat_id: int,
        m: Dict[str, Any],
        caption: Optional[str] = None,
        has_spoiler: bool = False,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        bot_instance: Optional[Bot] = None
    ) -> Optional[str]:
        if not isinstance(m, dict):
            return None
        target_bot = bot_instance or _CURRENT_BOT_CTX.get() or self.bot
        url = m.get("url")
        cached_fid = (m.get("telegram_file_id") or "").strip()
        is_video = self._is_video_media(m)
        fname = str(m.get("name") or "")
        send_as = m.get("telegram_send_as") or ("video" if is_video else "photo")
        extra_kw = {"reply_markup": reply_markup} if reply_markup is not None else {}

        if cached_fid:
            try:
                await self._send_by_cached_fid(
                    target_bot,
                    chat_id=chat_id,
                    fid=cached_fid,
                    send_as=send_as,
                    caption=caption,
                    has_spoiler=has_spoiler,
                    reply_markup=reply_markup
                )
                return cached_fid
            except Exception:
                m.pop("telegram_file_id", None)

        if not url or str(url).startswith("tg://"):
            return None

        state = None
        fb = None
        try:
            if self.state_getter:
                state = await self.state_getter()
                ctx_tok = getattr(target_bot, "token", "") or ""
                fb = self._get_active_flow_bot(state, bot_token=ctx_tok) if isinstance(state, dict) else None
        except Exception:
            state = None

        fid = await self.warm_media_item_in_channel(
            m,
            state,
            fb=fb,
            bot_instance=target_bot,
            exclude_chat_id=chat_id
        )
        if fid:
            if state and self.state_saver:
                try:
                    await self.state_saver(state, runtime_only=True)
                except Exception:
                    pass
            eff_send_as = m.get("telegram_send_as") or ("video" if is_video else "photo")
            await self._send_by_cached_fid(
                target_bot,
                chat_id=chat_id,
                fid=str(fid),
                send_as=eff_send_as,
                caption=caption,
                has_spoiler=has_spoiler,
                reply_markup=reply_markup
            )
            return str(fid)

        default_fn = fname or ("media.mp4" if is_video else "media.jpg")
        inp = self._build_input_file(url, default_fn)
        if not inp:
            return None

        sent = None
        try:
            if is_video:
                sent = await target_bot.send_video(chat_id=chat_id, video=inp, caption=caption, has_spoiler=has_spoiler, **extra_kw)
            else:
                sent = await target_bot.send_photo(chat_id=chat_id, photo=inp, caption=caption, has_spoiler=has_spoiler, **extra_kw)
        except Exception as primary_err:
            if hasattr(target_bot, "send_document"):
                inp_doc = self._build_input_file(url, default_fn)
                sent = await target_bot.send_document(chat_id=chat_id, document=inp_doc, caption=caption, **extra_kw)
            else:
                raise primary_err

        fid, detected_kind = self._extract_sent_file_id(sent, "video" if is_video else "photo")
        if fid:
            m["telegram_file_id"] = str(fid)
            m["telegram_send_as"] = detected_kind
            if str(url).startswith("data:video") or len(str(url)) > 350000:
                m["url"] = f"tg://file_id/{fid}"
            if state and self.state_saver:
                try:
                    await self.state_saver(state, runtime_only=True)
                except Exception:
                    pass
            return str(fid)
        return None

    async def _send_flow_medias(
        self,
        chat_id: int,
        medias: List[Dict[str, Any]],
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        bot_instance: Optional[Bot] = None
    ) -> bool:
        target_bot = bot_instance or _CURRENT_BOT_CTX.get() or self.bot
        valid_items = [
            m for m in (medias or [])[:3]
            if isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
        ]
        if not valid_items:
            return False

        # Se houver apenas 1 mídia, envia individualmente via cache (podendo anexar reply_markup diretamente na mídia)
        if len(valid_items) == 1 or not hasattr(target_bot, "send_media_group"):
            attached = False
            for idx, m in enumerate(valid_items):
                try:
                    rm = reply_markup if (idx == len(valid_items) - 1 and reply_markup is not None) else None
                    await self._send_single_media_cached(chat_id, m, reply_markup=rm, bot_instance=target_bot)
                    if rm is not None:
                        attached = True
                except Exception as e:
                    logger.warning(f"Falha ao enviar mídia do fluxo: {e}")
            return attached

        # Quando há 2 ou 3 mídias, envia LADO A LADO (Álbum / send_media_group)
        state = None
        fb = None
        try:
            if self.state_getter:
                state = await self.state_getter()
                ctx_tok = getattr(target_bot, "token", "") or ""
                fb = self._get_active_flow_bot(state, bot_token=ctx_tok) if isinstance(state, dict) else None
        except Exception:
            state = None

        # Garante que todas as mídias do grupo já estejam aquecidas em formato compatível com Álbum no canal de cache
        warmed_cnt = await self.warm_media_group_in_channel(
            valid_items,
            state,
            fb=fb,
            bot_instance=target_bot,
            exclude_chat_id=chat_id
        )
        if warmed_cnt > 0 and state and self.state_saver:
            try:
                await self.state_saver(state, runtime_only=True)
            except Exception:
                pass

        try:
            group_inputs = []
            for m in valid_items:
                inp_item = await self._build_group_media_input(m, target_bot, force_upload=False)
                if inp_item:
                    group_inputs.append(inp_item)

            if len(group_inputs) >= 2:
                sent_msgs = await target_bot.send_media_group(chat_id=chat_id, media=group_inputs)
                if isinstance(sent_msgs, list) and len(sent_msgs) == len(valid_items):
                    updated = False
                    for m, sent_msg in zip(valid_items, sent_msgs):
                        if not (m.get("telegram_file_id") or "").strip():
                            is_vid = self._is_video_media(m)
                            fid, detected_kind = self._extract_sent_file_id(sent_msg, "video" if is_vid else "photo")
                            if fid:
                                m["telegram_file_id"] = str(fid)
                                m["telegram_send_as"] = detected_kind
                                m["album_cached"] = True
                                updated = True
                    if updated and state and self.state_saver:
                        try:
                            await self.state_saver(state, runtime_only=True)
                        except Exception:
                            pass
                return False
        except Exception as group_err:
            logger.warning(f"Tentando reconversão de álbum lado a lado após erro ({group_err})...")
            try:
                group_inputs_retry = []
                for m in valid_items:
                    inp_item = await self._build_group_media_input(m, target_bot, force_upload=True)
                    if inp_item:
                        group_inputs_retry.append(inp_item)
                if len(group_inputs_retry) >= 2:
                    sent_msgs = await target_bot.send_media_group(chat_id=chat_id, media=group_inputs_retry)
                    if isinstance(sent_msgs, list) and len(sent_msgs) == len(valid_items):
                        for m, sent_msg in zip(valid_items, sent_msgs):
                            is_vid = self._is_video_media(m)
                            fid, detected_kind = self._extract_sent_file_id(sent_msg, "video" if is_vid else "photo")
                            if fid:
                                m["telegram_file_id"] = str(fid)
                                m["telegram_send_as"] = detected_kind
                                m["album_cached"] = True
                        if state and self.state_saver:
                            try:
                                await self.state_saver(state, runtime_only=True)
                            except Exception:
                                pass
                    return False
            except Exception as retry_err:
                logger.warning(f"Fallback para envio individual após falha no álbum: {retry_err}")

        attached = False
        for idx, m in enumerate(valid_items):
            try:
                rm = reply_markup if (idx == len(valid_items) - 1 and reply_markup is not None) else None
                await self._send_single_media_cached(chat_id, m, reply_markup=rm, bot_instance=target_bot)
                if rm is not None:
                    attached = True
            except Exception as e:
                logger.warning(f"Falha ao enviar mídia do fluxo: {e}")
        return attached

    def _schedule_flow_downsell(self, user: types.User, track: str = "general", bot_token: str = ""):
        uid = str(user.id)
        tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.active_token or "").strip()
        bot_prefix = tok.split(":", 1)[0] if (tok and ":" in tok) else ""
        self._cancel_downsell(uid, bot_id=bot_prefix)
        task = asyncio.create_task(self._flow_downsell_worker(user, track, bot_token=tok))
        self.downsell_tasks[uid] = task
        if bot_prefix:
            self.downsell_tasks[f"{bot_prefix}:{uid}"] = task

    async def _flow_downsell_worker(self, user: types.User, track: str, bot_token: str = ""):
        paid_stages = ("Pagamento aprovado", "Entrega concluída", "Compra aprovada", "Comprou upsell")
        try:
            state = await self.state_getter()
            fb = self._get_active_flow_bot(state, bot_token=bot_token)
            if not fb:
                return
            bot_id = str(fb.get("id") or "")
            ds_cfg = fb.get("downsell_pix" if track == "pix" else "downsell_general", {})
            if not ds_cfg.get("enabled", False):
                return
            sequences = sorted(
                ds_cfg.get("sequences", []),
                key=lambda s: max(1, int(s.get("delay_minutes") or 1))
            )
            elapsed_mins = 0
            for seq in sequences:
                msg_tmpl = (seq.get("message") or "").strip()
                seq_medias = seq.get("medias") or []
                has_seq_medias = any(
                    isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
                    for m in seq_medias[:3]
                )
                if not msg_tmpl and not has_seq_medias:
                    continue
                target_mins = max(1, int(seq.get("delay_minutes") or 1))
                wait_mins = max(1, target_mins - elapsed_mins)
                await asyncio.sleep(wait_mins * 60)
                elapsed_mins += wait_mins

                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, user, bot_token=bot_token)
                if lead.get("stage") in paid_stages or lead.get("blocked"):
                    return
                if track == "general" and lead.get("stage") == "Gerou checkout":
                    return

                fb = self._get_active_flow_bot(state, bot_token=bot_token, bot_id=bot_id)
                if not fb:
                    return
                eff_tok = (str(fb.get("token") or "").strip() or bot_token or self.current_token).strip()
                live_bot = None
                if eff_tok:
                    _CURRENT_BOT_TOKEN_CTX.set(eff_tok)
                    live_bot = self._get_bot_by_token(eff_tok)
                    if live_bot:
                        _CURRENT_BOT_CTX.set(live_bot)

                disc_str = seq.get("discount", "Sem desconto")
                disc_pct = self._parse_discount_pct(disc_str)
                if disc_pct > 0:
                    auth_discounts = lead.setdefault("authorized_flow_discounts", [])
                    if disc_pct not in auth_discounts:
                        auth_discounts.append(disc_pct)
                kb = self._build_flow_plans_kb(
                    fb,
                    filter_plan_ids=seq.get("plan_ids") or None,
                    discount_str=disc_str
                )
                lead["from_downsell"] = True
                if not msg_tmpl and has_seq_medias:
                    attached_kb = await self._send_flow_medias(user.id, seq_medias, reply_markup=kb, bot_instance=live_bot)
                    if not attached_kb and kb:
                        await self._safe_send_html(user.id, "👇 <b>Escolha seu plano abaixo:</b>", reply_markup=kb, bot_instance=live_bot)
                    lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell {track} — Mídia]"})
                else:
                    await self._send_flow_medias(user.id, seq_medias, bot_instance=live_bot)
                    txt = self._render_flow_vars(msg_tmpl, user, fb)
                    await self._safe_send_html(user.id, txt, reply_markup=kb, bot_instance=live_bot)
                    lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell {track}] {txt}"})
                await self.state_saver(state, runtime_only=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Flow Downsell ({track}): {e}")

    def _cancel_downsell(self, user_id: Any, bot_id: Optional[str] = None):
        uid = str(user_id).strip()
        keys_to_pop = []
        if bot_id:
            bid = str(bot_id).strip()
            keys_to_pop.append(f"{bid}:{uid}")
        else:
            keys_to_pop.append(uid)
            for k in list(self.downsell_tasks.keys()):
                if k == uid or k.endswith(f":{uid}"):
                    keys_to_pop.append(k)
        for k in set(keys_to_pop):
            task = self.downsell_tasks.pop(k, None)
            if task and not task.done():
                task.cancel()

    def _verify_secret_test_token(self, candidate: str) -> bool:
        cand = (candidate or "").strip()
        if not cand:
            return False
        env_tok = (os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_PASSWORD") or "").strip()
        if env_tok and secrets.compare_digest(cand, env_tok):
            return True
        token_file = os.path.join(os.path.dirname(__file__), ".admin_token")
        try:
            if os.path.exists(token_file):
                with open(token_file, "r", encoding="utf-8") as f:
                    saved_tok = f.read().strip()
                    if saved_tok and secrets.compare_digest(cand, saved_tok):
                        return True
        except Exception:
            pass
        return False

    async def _handle_secret_test_command(self, message: types.Message, user_text: str) -> bool:
        parts = (user_text or "").strip().split(None, 1)
        if len(parts) < 2:
            return False
        cmd = parts[0].lower().lstrip("/")
        secret_arg = parts[1].strip()
        if cmd not in ("aprovar", "testepago", "downsell"):
            return False
        if not self._verify_secret_test_token(secret_arg):
            return False

        msg_bot = getattr(message, "bot", None)
        msg_bot_tok = getattr(msg_bot, "token", "") or ""
        if msg_bot_tok:
            _CURRENT_BOT_TOKEN_CTX.set(msg_bot_tok)
            if msg_bot:
                _CURRENT_BOT_CTX.set(msg_bot)

        state = await self.state_getter()
        lead = await self._get_or_create_lead(state, message.from_user)
        fb = self._get_active_flow_bot(state, bot_token=msg_bot_tok)

        if cmd == "downsell":
            if not fb:
                await message.answer("⚠️ Este bot está no modo IA. O comando /downsell rápido é para o modo Botões.")
                return True
            use_pix_track = (
                lead.get("stage") == "Gerou checkout"
                and bool((fb.get("downsell_pix") or {}).get("sequences"))
            )
            track = "pix" if use_pix_track else "general"
            ds_cfg = fb.get("downsell_pix" if track == "pix" else "downsell_general", {})
            seqs = ds_cfg.get("sequences") or []
            if not seqs:
                await message.answer("⚠️ Nenhuma sequência de Downsell cadastrada neste bot.")
                return True
            seq = seqs[0]
            msg_tmpl = (seq.get("message") or "").strip()
            seq_medias = seq.get("medias") or []
            has_seq_medias = any(
                isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
                for m in seq_medias[:3]
            )
            disc_str = seq.get("discount", "Sem desconto")
            disc_pct = self._parse_discount_pct(disc_str)
            if disc_pct > 0:
                auth_discounts = lead.setdefault("authorized_flow_discounts", [])
                if disc_pct not in auth_discounts:
                    auth_discounts.append(disc_pct)
            kb = self._build_flow_plans_kb(
                fb,
                filter_plan_ids=seq.get("plan_ids") or None,
                discount_str=disc_str
            )
            lead["from_downsell"] = True
            if not msg_tmpl and has_seq_medias:
                attached_kb = await self._send_flow_medias(message.chat.id, seq_medias, reply_markup=kb)
                if not attached_kb and kb:
                    await self._safe_send_html(message.chat.id, "👇 <b>Escolha seu plano abaixo:</b>", reply_markup=kb)
            else:
                await self._send_flow_medias(message.chat.id, seq_medias)
                txt = self._render_flow_vars(msg_tmpl or "🔥 Oferta especial liberada para você:", message.from_user, fb)
                await self._safe_send_html(message.chat.id, txt, reply_markup=kb)
            await self.state_saver(state, runtime_only=True)
            return True

        # Comando /aprovar <senha> ou /testepago <senha>
        tx_map = state.setdefault("tx_records", {})
        target_tx_id = str(lead.get("last_tx_id") or "").strip()
        tx_rec = tx_map.get(target_tx_id) or self.tx_records.get(target_tx_id)

        if not tx_rec or tx_rec.get("status") == "paid" or tx_rec.get("delivered"):
            # Procura qualquer transação pendente deste usuário
            target_tx_id = ""
            tx_rec = None
            for tid, rec in reversed(list(tx_map.items())):
                if isinstance(rec, dict) and int(rec.get("user_id") or 0) == int(message.from_user.id):
                    if rec.get("status") != "paid" and not rec.get("delivered"):
                        target_tx_id = tid
                        tx_rec = rec
                        break

        if not tx_rec:
            # Cria uma transação de teste vinculada ao primeiro plano para testar a entrega na hora
            target_tx_id = f"test_{int(datetime.utcnow().timestamp())}_{message.from_user.id}"
            if fb:
                plans = fb.get("plans") or []
                plan = plans[0] if plans else {}
                plan_name = (plan.get("name") or "Plano VIP").strip()
                price = float(plan.get("price") or 9.90)
                fb_deliv = fb.get("fallback_delivery", {})
                dest_type = (plan.get("delivery_dest_type") or "Canal ou grupo").strip()
                if dest_type == "Herdar do fluxo":
                    eff_type = fb_deliv.get("dest_type", "Canal ou grupo")
                    ch_target = (fb_deliv.get("channel_id") or "").strip()
                    ext_url = (fb_deliv.get("external_url") or "").strip()
                else:
                    eff_type = dest_type
                    raw_ch = (plan.get("delivery_channel_id") or "").strip()
                    fb_ch = (fb_deliv.get("channel_id") or "").strip()
                    ch_target = fb_ch if (not raw_ch or raw_ch == "-1003939135982") and fb_ch else (raw_ch or fb_ch)
                    ext_url = (plan.get("delivery_external_url") or fb_deliv.get("external_url") or "").strip()
                tx_rec = {
                    "tx_id": target_tx_id,
                    "user_id": int(message.from_user.id),
                    "mode": "flow",
                    "bot_id": str(fb.get("id") or ""),
                    "bot_token": str(_CURRENT_BOT_TOKEN_CTX.get() or fb.get("token") or self.current_token or ""),
                    "plan_id": str(plan.get("id") or "plan_1"),
                    "offer_name": plan_name,
                    "amount": round(price, 2),
                    "cents": max(50, int(round(price * 100))),
                    "is_upsell": False,
                    "is_test": True,
                    "deliverable": str(lead.get("accepted_bump_deliverable") or ""),
                    "delivery_dest_type": eff_type,
                    "delivery_channel_id": ch_target,
                    "delivery_external_url": ext_url,
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat(),
                }
            else:
                offers = state.get("business", {}).get("offers") or []
                first_offer = offers[0] if offers else {}
                price = float(first_offer.get("price") or 19.90)
                tx_rec = {
                    "tx_id": target_tx_id,
                    "user_id": int(message.from_user.id),
                    "mode": "ai",
                    "offer_name": first_offer.get("name") or "Acesso VIP",
                    "amount": round(price, 2),
                    "cents": max(50, int(round(price * 100))),
                    "is_upsell": False,
                    "is_test": True,
                    "delivery_type": state.get("publish", {}).get("delivery_type", "Link de acesso"),
                    "delivery_destination": state.get("publish", {}).get("delivery_destination", ""),
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat(),
                }

        tx_rec["is_test"] = True
        self.tx_records[target_tx_id] = tx_rec
        tx_map[target_tx_id] = tx_rec
        lead["last_tx_id"] = target_tx_id
        await self.state_saver(state, runtime_only=True)
        await self.deliver_purchase_and_upsell(message.from_user.id, tx_id=target_tx_id)
        return True

    def _schedule_downsell(self, user_id: int, user_first_name: str, bot_token: str = ""):
        uid = str(user_id)
        tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.active_token or "").strip()
        bot_prefix = tok.split(":", 1)[0] if (tok and ":" in tok) else ""
        self._cancel_downsell(uid, bot_id=bot_prefix)
        task = asyncio.create_task(
            self._downsell_worker(user_id, user_first_name, bot_token=tok)
        )
        self.downsell_tasks[uid] = task
        if bot_prefix:
            self.downsell_tasks[f"{bot_prefix}:{uid}"] = task

    async def _downsell_worker(self, user_id: int, user_first_name: str, bot_token: str = ""):
        paid_stages = (
            "Compra aprovada",
            "Pagamento aprovado",
            "Entrega concluída",
            "Recebeu upsell",
            "Escolheu upsell",
            "Checkout de upsell",
            "Comprou upsell",
        )
        try:
            while True:
                state = await self.state_getter()
                effective_tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.current_token or "").strip()
                if self._get_active_flow_bot(state, bot_token=effective_tok):
                    return
                ds = state.get("downsell", {})
                if not ds.get("enabled", False):
                    return

                lead = await self._get_or_create_lead(
                    state,
                    types.User(id=user_id, is_bot=False, first_name=user_first_name),
                    bot_token=effective_tok
                )
                if lead.get("stage") in paid_stages or lead.get("blocked"):
                    return

                sent_steps = lead.setdefault("sent_downsell_steps", [])
                steps = ds.get("steps", [])
                next_step = None
                for s in steps:
                    if s.get("id") not in sent_steps:
                        next_step = s
                        break

                if not next_step:
                    return

                delay_mins = max(1, int(next_step.get("delay_minutes", 1)))
                await asyncio.sleep(delay_mins * 60)

                # Recarrega estado após espera
                state = await self.state_getter()
                if self._get_active_flow_bot(state, bot_token=effective_tok):
                    return
                lead = await self._get_or_create_lead(
                    state,
                    types.User(id=user_id, is_bot=False, first_name=user_first_name),
                    bot_token=effective_tok
                )
                if lead.get("stage") in paid_stages or lead.get("blocked"):
                    return

                target_bot = None
                if effective_tok:
                    target_bot = self._get_bot_by_token(effective_tok)
                if not target_bot and lead.get("origin_bot_numeric_id"):
                    target_bot = self._get_bot_by_token(f"{lead['origin_bot_numeric_id']}:")
                if not target_bot:
                    target_bot = self.bot

                msg_text = next_step.get("message", "").replace("{nome}", user_first_name)
                accompany = next_step.get("accompany", "Botão com os planos atuais")
                has_plans = "planos" in accompany.lower() or "oferta" in accompany.lower()
                markup = self._build_offers_keyboard(state) if has_plans else None

                sent = await self._safe_send_html(user_id, msg_text, reply_markup=markup, bot_instance=target_bot)
                if not sent and lead.get("blocked"):
                    return

                if has_plans:
                    self._advance_stage(lead, "Viu os planos")
                lead["from_downsell"] = True
                lead.setdefault("sent_downsell_steps", []).append(next_step.get("id"))
                lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell] {msg_text}"})
                await self.state_saver(state, runtime_only=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Downsell worker: {e}")

    def _cancel_upsell(self, user_id: Any, bot_id: Optional[str] = None):
        uid = str(user_id).strip()
        keys_to_pop = []
        if bot_id:
            bid = str(bot_id).strip()
            keys_to_pop.append(f"{bid}:{uid}")
        else:
            keys_to_pop.append(uid)
            for k in list(self.upsell_tasks.keys()):
                if k == uid or k.endswith(f":{uid}"):
                    keys_to_pop.append(k)
        for k in set(keys_to_pop):
            task = self.upsell_tasks.pop(k, None)
            if task and not task.done():
                task.cancel()

    def cancel_lead_tasks(self, user_id: str, *, origin_bot_ids=None):
        uid = str(user_id)
        origins = None if origin_bot_ids is None else {str(x) for x in origin_bot_ids if str(x).isdecimal()}
        if origins is None:
            self._cancel_downsell(uid)
            self._cancel_upsell(uid)
        else:
            for origin in origins:
                self._cancel_downsell(uid, bot_id=origin)
                self._cancel_upsell(uid, bot_id=origin)
        for tx_id, owner_id in list(self.active_tx_map.items()):
            rec = self.tx_records.get(tx_id) or {}
            token = str(rec.get('bot_token') or '')
            origin = token.split(':', 1)[0] if ':' in token else ''
            if str(owner_id) == uid and (origins is None or origin in origins):
                pt = self.pix_poll_tasks.pop(tx_id, None)
                if pt and not pt.done():
                    pt.cancel()

    def _register_handlers(self):
        @self.dp.message(CommandStart())
        async def handle_start(message: types.Message):
            msg_bot = getattr(message, "bot", None)
            msg_bot_tok = getattr(msg_bot, "token", "") or ""
            if msg_bot_tok:
                _CURRENT_BOT_TOKEN_CTX.set(msg_bot_tok)
                if msg_bot:
                    _CURRENT_BOT_CTX.set(msg_bot)
            target_bot = msg_bot or _CURRENT_BOT_CTX.get() or self.bot

            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))
            self._cancel_upsell(str(message.from_user.id))

            # Ao enviar /start, reinicia o histórico, etapa e downsells do lead para permitir novo ciclo limpo
            lead["history"] = []
            self._advance_stage(lead, "Conversando", reset=True)
            lead["sent_downsell_steps"] = []
            lead["prefs_list"] = []
            lead["objs_list"] = []
            lead["preferences"] = "Iniciou conversa no bot"
            lead["objections"] = "Nenhuma identificada"
            lead.pop("accepted_bump_deliverable", None)
            lead.pop("accepted_bump_indices", None)
            lead.pop("has_order_bump", None)
            lead.pop("from_downsell", None)
            lead.pop("flow_plan_id", None)
            lead.pop("authorized_flow_discounts", None)
            lead.pop("authorized_discount_offer_id", None)
            lead.pop("authorized_discount_price", None)
            lead.pop("authorized_discount_expires_at", None)
            self._update_lead_insights(state, lead)

            flow_bot = self._get_active_flow_bot(state, bot_token=msg_bot_tok)
            if flow_bot:
                welcome = flow_bot.get("welcome", {})
                welcome_medias = welcome.get("medias", [])
                has_welcome_medias = any(
                    isinstance(m, dict) and ((m.get("telegram_file_id") or "").strip() or (m.get("url") or "").strip())
                    for m in (welcome_medias or [])[:3]
                )
                kb = self._build_flow_plans_kb(flow_bot)
                raw_main_msg = (welcome.get("message") or "").strip()
                sec_enabled = bool(welcome.get("secondary_enabled", False))
                sec_msg = self._render_flow_vars(welcome.get("secondary_message") or "", message.from_user, flow_bot).strip()

                if not raw_main_msg and has_welcome_medias:
                    if sec_enabled and sec_msg:
                        await self._send_flow_medias(message.chat.id, welcome_medias, bot_instance=target_bot)
                        try:
                            await message.answer(sec_msg, parse_mode="HTML", reply_markup=kb)
                        except Exception:
                            await message.answer(sec_msg, reply_markup=kb)
                        lead.setdefault("history", []).append({"role": "assistant", "content": f"[Mídia de boas-vindas]\n\n{sec_msg}"})
                    else:
                        attached_kb = await self._send_flow_medias(message.chat.id, welcome_medias, reply_markup=kb, bot_instance=target_bot)
                        if not attached_kb and kb:
                            await message.answer("👇 <b>Escolha seu plano abaixo:</b>", parse_mode="HTML", reply_markup=kb)
                        lead.setdefault("history", []).append({"role": "assistant", "content": "[Mídia de boas-vindas]"})
                else:
                    await self._send_flow_medias(message.chat.id, welcome_medias, bot_instance=target_bot)
                    main_msg = self._render_flow_vars(
                        raw_main_msg or "Olá {saudacao}, {nome}! Escolha seu plano abaixo:",
                        message.from_user,
                        flow_bot
                    )
                    if sec_enabled and sec_msg:
                        try:
                            await message.answer(main_msg, parse_mode="HTML")
                        except Exception:
                            await message.answer(main_msg)
                        try:
                            await message.answer(sec_msg, parse_mode="HTML", reply_markup=kb)
                        except Exception:
                            await message.answer(sec_msg, reply_markup=kb)
                        lead.setdefault("history", []).append({"role": "assistant", "content": f"{main_msg}\n\n{sec_msg}"})
                    else:
                        try:
                            await message.answer(main_msg, parse_mode="HTML", reply_markup=kb)
                        except Exception:
                            await message.answer(main_msg, reply_markup=kb)
                        lead.setdefault("history", []).append({"role": "assistant", "content": main_msg})

                self._advance_stage(lead, "Viu os planos")
                await self.state_saver(state, runtime_only=True)
                self._schedule_flow_downsell(message.from_user, track="general", bot_token=msg_bot_tok)
                return

            style = state.get("style", {})
            variations = [v.strip() for v in style.get("opening_variations", []) if v and v.strip()]
            main_opening = style.get("opening_messages", ["oii 💜", "chegou agora por aqui?"])
            if isinstance(main_opening, str):
                main_opening = [l.strip() for l in main_opening.split("\n") if l.strip()]
            if not main_opening:
                main_opening = ["oii 💜", "chegou agora por aqui?"]

            if variations and random.random() < 0.4:
                chosen_lines = [l.strip() for l in random.choice(variations).split("\n") if l.strip()]
            else:
                chosen_lines = main_opening

            delay = self._get_cadence_delay(state)
            for line in chosen_lines:
                if target_bot:
                    await target_bot.send_chat_action(chat_id=message.chat.id, action="typing")
                await asyncio.sleep(delay)
                await message.answer(line)
                lead.setdefault("history", []).append({"role": "assistant", "content": line})

            await self.state_saver(state, runtime_only=True)
            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor", bot_token=msg_bot_tok)

        @self.dp.message(F.text)
        async def handle_text(message: types.Message):
            msg_bot = getattr(message, "bot", None)
            msg_bot_tok = getattr(msg_bot, "token", "") or ""
            if msg_bot_tok:
                _CURRENT_BOT_TOKEN_CTX.set(msg_bot_tok)
                if msg_bot:
                    _CURRENT_BOT_CTX.set(msg_bot)
            target_bot = msg_bot or _CURRENT_BOT_CTX.get() or self.bot

            user_text = message.text.strip()
            if user_text.lower() in ("/reset", "/reiniciar", "/limpar"):
                await handle_start(message)
                return

            if await self._handle_secret_test_command(message, user_text):
                return

            state_check = await self.state_getter()
            fb_check = self._get_active_flow_bot(state_check, bot_token=msg_bot_tok)
            if fb_check:
                kb = self._build_flow_plans_kb(fb_check)
                try:
                    await message.answer(
                        "👇 <b>Escolha uma das opções abaixo para continuar seu acesso:</b>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                except Exception:
                    await message.answer("👇 Escolha uma das opções abaixo para continuar seu acesso:", reply_markup=kb)
                return

            uid = str(message.from_user.id)
            async with self._get_lead_lock(uid):
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user)
                self._cancel_downsell(uid)

                lead.setdefault("history", []).append({"role": "user", "content": user_text})
                self._update_lead_insights(state, lead, user_text=user_text)

                # Verifica se o usuário já tinha visto os planos e declarou escolha por texto
                lower_txt = user_text.lower()
                if "Viu os planos" in lead.get("stage_history", []):
                    if any(w in lower_txt for w in ("quero esse", "vou querer", "fechado", "manda o pix", "quero assinar", "quero entrar")):
                        if lead.get("stage") in ("Recebeu upsell", "Escolheu upsell"):
                            self._advance_stage(lead, "Escolheu upsell")
                        else:
                            self._advance_stage(lead, "Escolheu um plano")

                # Verifica palavras de encaminhamento para suporte humano
                handoff_words = [
                    w.strip().lower()
                    for w in state.get("style", {}).get("handoff_words", "").split(",")
                    if w.strip()
                ]
                if any(hw in lower_txt for hw in handoff_words):
                    support = state.get("publish", {}).get("support_username", "@suporte")
                    reply = f"Vou te passar o contato do suporte oficial para resolver isso rapidinho: {support} 💜"
                    await message.answer(reply)
                    lead["history"].append({"role": "assistant", "content": reply})
                    await self.state_saver(state, runtime_only=True)
                    return

                delay = self._get_cadence_delay(state)
                if target_bot:
                    await target_bot.send_chat_action(chat_id=message.chat.id, action="typing")
                await asyncio.sleep(delay)

                ai_res = await generate_ai_reply(
                    state,
                    lead["history"],
                    user_text,
                    lead_name=lead.get("first_name", "")
                )
                reply_text = ai_res.get("reply", "")

                # Reobtém a referência atualizada em RAM caso outro lead ou o painel tenha salvo durante os segundos da IA
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user)

                # Se houver mídia acionada pelo contexto (suporta URL http, upload Base64 e cache de telegram_file_id)
                media = ai_res.get("media")
                if media and (media.get("url") or media.get("telegram_file_id")):
                    try:
                        raw_cap = (media.get("caption") or "").replace("{nome}", lead.get("first_name", "")).strip()
                        fid = await self._send_single_media_cached(
                            message.chat.id,
                            media,
                            caption=raw_cap or None,
                            has_spoiler=bool(media.get("spoiler", False)),
                            bot_instance=target_bot
                        )
                        if fid and media.get("id"):
                            for sm in state.get("medias", []):
                                if isinstance(sm, dict) and sm.get("id") == media.get("id"):
                                    sm["telegram_file_id"] = fid
                    except Exception as e:
                        logger.warning(f"Falha ao enviar mídia de prévia: {e}")

                offer_card = ai_res.get("offer_card")
                is_upsell_ctx = lead.get("stage") in ("Entrega concluída", "Recebeu upsell", "Escolheu upsell", "Checkout de upsell")
                markup = self._build_offers_keyboard(state, offer_card, is_upsell=is_upsell_ctx) if offer_card else None

                if offer_card:
                    if is_upsell_ctx:
                        self._advance_stage(lead, "Recebeu upsell")
                    else:
                        self._advance_stage(lead, "Viu os planos")
                        if offer_card.get("discounted"):
                            self._advance_stage(lead, "Escolheu um plano")
                            bargain_cfg = state.get("business", {}).get("bargain") or state.get("bargain") or {}
                            val_mins = max(1, int(bargain_cfg.get("validity_minutes") or 30))
                            lead["authorized_discount_offer_id"] = str(offer_card.get("id", ""))[:20]
                            lead["authorized_discount_price"] = float(offer_card.get("price", 0))
                            lead["authorized_discount_expires_at"] = (datetime.utcnow() + timedelta(minutes=val_mins)).isoformat()

                try:
                    await message.answer(reply_text, reply_markup=markup)
                except Exception as e:
                    logger.warning(f"Fallback envio de mensagem sem markup: {e}")
                    await message.answer(reply_text)
                lead["history"].append({"role": "assistant", "content": reply_text})
                await self.state_saver(state, runtime_only=True)
            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor", bot_token=msg_bot_tok)

        @self.dp.message(F.voice | F.audio | F.photo | F.sticker | F.video)
        async def handle_non_text(message: types.Message):
            msg_bot = getattr(message, "bot", None)
            msg_bot_tok = getattr(msg_bot, "token", "") or ""
            if msg_bot_tok:
                _CURRENT_BOT_TOKEN_CTX.set(msg_bot_tok)
                if msg_bot:
                    _CURRENT_BOT_CTX.set(msg_bot)
            target_bot = msg_bot or _CURRENT_BOT_CTX.get() or self.bot

            state_check = await self.state_getter()
            if self._get_active_flow_bot(state_check, bot_token=msg_bot_tok):
                return

            uid = str(message.from_user.id)
            async with self._get_lead_lock(uid):
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user, bot_token=msg_bot_tok)
                self._cancel_downsell(uid)

                if message.voice or message.audio:
                    synthetic_msg = "[Visitante enviou um áudio — diga de forma simpática que não consegue ouvir áudio agora e peça para ele escrever por texto]"
                elif message.sticker:
                    synthetic_msg = f"[Visitante enviou uma figurinha {message.sticker.emoji or '😈'}]"
                else:
                    synthetic_msg = "[Visitante enviou uma foto/vídeo]"

                lead.setdefault("history", []).append({"role": "user", "content": synthetic_msg})
                delay = self._get_cadence_delay(state)
                if target_bot:
                    await target_bot.send_chat_action(chat_id=message.chat.id, action="typing")
                await asyncio.sleep(delay)

                ai_res = await generate_ai_reply(
                    state,
                    lead["history"],
                    synthetic_msg,
                    lead_name=lead.get("first_name", "")
                )
                reply_text = ai_res.get("reply", "")
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user, bot_token=msg_bot_tok)
                offer_card = ai_res.get("offer_card")
                is_upsell_ctx = lead.get("stage") in ("Entrega concluída", "Recebeu upsell", "Escolheu upsell", "Checkout de upsell")
                markup = self._build_offers_keyboard(state, offer_card, is_upsell=is_upsell_ctx) if offer_card else None
                if offer_card and not is_upsell_ctx:
                    self._advance_stage(lead, "Viu os planos")

                await message.answer(reply_text, reply_markup=markup)
                lead["history"].append({"role": "assistant", "content": reply_text})
                await self.state_saver(state, runtime_only=True)
            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor", bot_token=msg_bot_tok)

        @self.dp.callback_query(F.data.startswith("buy:"))
        async def handle_buy_callback(callback: types.CallbackQuery):
            try:
                await callback.answer("Gerando seu PIX seguro...")
            except Exception:
                pass
            parts = callback.data.split(":")
            oid = parts[1] if len(parts) > 1 else ""
            has_up_flag = len(parts) > 3 and parts[3] == "up"

            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, callback.from_user)
            self._cancel_downsell(str(callback.from_user.id))

            # Preço SEMPRE calculado no servidor a partir do cadastro de ofertas (ignora parts[2] do callback)
            matched_offer = None
            for idx, o in enumerate(state.get("business", {}).get("offers", [])):
                cand_id = str(o.get("id", f"offer_{idx}"))[:20]
                if cand_id == oid:
                    matched_offer = o
                    break
            if not matched_offer:
                offers_list = state.get("business", {}).get("offers", [])
                if not offers_list:
                    await callback.answer("Nenhuma oferta disponível no momento.", show_alert=True)
                    return
                matched_offer = offers_list[0]
                oid = str(matched_offer.get("id", "offer_0"))[:20]

            base_price = float(matched_offer.get("price", 19.0))
            price = base_price
            bargain = state.get("business", {}).get("bargain") or state.get("bargain") or {}
            exp_str = lead.get("authorized_discount_expires_at")
            discount_not_expired = True
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    discount_not_expired = datetime.now(timezone.utc) <= exp_dt
                except Exception:
                    discount_not_expired = False

            if (
                bargain.get("enabled")
                and discount_not_expired
                and str(lead.get("authorized_discount_offer_id", "")) == oid
                and lead.get("authorized_discount_price") is not None
            ):
                eff_min = _compute_effective_min_price([matched_offer], bargain)
                auth_p = float(lead.get("authorized_discount_price"))
                if eff_min <= auth_p <= base_price:
                    price = round(auth_p, 2)

            chosen_offer_name = matched_offer.get("name", f"Oferta R$ {price:.2f}".replace(".", ","))

            is_upsell = has_up_flag or lead.get("stage") in (
                "Entrega concluída",
                "Recebeu upsell",
                "Escolheu upsell",
                "Checkout de upsell",
                "Comprou upsell",
            ) or "Entrega concluída" in lead.get("stage_history", [])

            self._advance_stage(lead, "Viu os planos")
            if is_upsell:
                self._advance_stage(lead, "Recebeu upsell")
                self._advance_stage(lead, "Escolheu upsell")
                lead["chosen_upsell"] = chosen_offer_name
            else:
                self._advance_stage(lead, "Escolheu um plano")
                lead["chosen_plan"] = chosen_offer_name

            self._update_lead_insights(state, lead, chosen_plan=chosen_offer_name)

            pub = state.get("publish", {})
            pushin_token = pub.get("pushin_pay_token", "").strip()
            gw_provider = pub.get("gateway_provider", "syncpay").strip()
            gw_secret = pub.get("gateway_secret", "").strip()
            cents = max(50, int(round(price * 100)))
            target_chat_id = callback.message.chat.id if callback.message else callback.from_user.id
            cb_bot = getattr(callback, "bot", None) or _CURRENT_BOT_CTX.get() or self.bot
            cb_tok = str(getattr(cb_bot, "token", "") or _CURRENT_BOT_TOKEN_CTX.get() or self.current_token or "").strip()

            # Fail-closed: nunca gera transação sim_ nem PIX falso se o gateway falhar ou não estiver configurado
            if not pushin_token:
                await self.state_saver(state, runtime_only=True)
                await (cb_bot or self.bot).send_message(
                    chat_id=target_chat_id,
                    text="⚠️ O pagamento via PIX está temporariamente indisponível. Tente novamente em instantes."
                )
                return

            try:
                client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
                res = await client.create_pix_charge(
                    value_in_cents=cents,
                    client_name=callback.from_user.first_name or "Cliente VIP"
                )
                pix_code = (res or {}).get("qr_code") or ""
                tx_id = str((res or {}).get("id") or "").strip()
                if not pix_code or not tx_id or tx_id.startswith("sim_"):
                    raise ValueError("Gateway não retornou id ou qr_code válidos")
            except Exception as e:
                logger.error(f"Erro Gateway PIX ({gw_provider}): {e}")
                await self.state_saver(state, runtime_only=True)
                await (cb_bot or self.bot).send_message(
                    chat_id=target_chat_id,
                    text="⚠️ Não foi possível gerar a cobrança PIX no momento. Por favor, tente novamente em instantes."
                )
                return

            learning = state.setdefault("learning", {})
            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) + 1

            if is_upsell:
                self._advance_stage(lead, "Checkout de upsell")
            else:
                self._advance_stage(lead, "Gerou checkout")

            tx_record = {
                "tx_id": tx_id,
                "user_id": int(callback.from_user.id),
                "mode": "ai",
                "bot_id": str(cb_tok.split(":")[0] if ":" in cb_tok else ""),
                "bot_token": cb_tok,
                "offer_id": oid,
                "offer_name": chosen_offer_name,
                "amount": round(price, 2),
                "cents": cents,
                "is_upsell": bool(is_upsell),
                "delivery_type": pub.get("delivery_type", "Link de acesso"),
                "delivery_destination": pub.get("delivery_destination", "").strip(),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
            }
            self.tx_records[tx_id] = tx_record
            state.setdefault("tx_records", {})[tx_id] = tx_record

            lead["last_tx_id"] = tx_id
            lead["last_tx_is_upsell"] = is_upsell
            self.active_tx_map[tx_id] = callback.from_user.id
            price_fmt = f"{price:.2f}".replace(".", ",")
            label_prefix = "PIX Upsell Gerado" if is_upsell else "PIX Gerado"
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": f"[{label_prefix} — {chosen_offer_name} (R$ {price_fmt})] {pix_code}"
            })
            await self.state_saver(state, runtime_only=True)

            # IMPORTANTE: callback_data no Telegram tem limite rígido de 64 bytes!
            # checkpix:<uuid 36 chars> = 45 bytes (sempre < 64 bytes).
            safe_cb = f"checkpix:{tx_id[:50]}"
            confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Já paguei / Liberar acesso", callback_data=safe_cb)]
            ])

            safe_pix_html = html.escape(pix_code)
            target_bot = cb_bot or self.bot
            try:
                await target_bot.send_message(
                    chat_id=target_chat_id,
                    text=(
                        f"🔒 <b>Pagamento Seguro via PIX</b>\n\n"
                        f"Plano: <b>{html.escape(chosen_offer_name)}</b>\n"
                        f"Valor: <b>R$ {price_fmt}</b>\n\n"
                        f"Toque no código PIX Copia e Cola abaixo para copiar e pague no app do seu banco:\n\n"
                        f"<code>{safe_pix_html}</code>\n\n"
                        f"Assim que concluir, seu acesso é liberado automaticamente aqui na conversa!"
                    ),
                    parse_mode="HTML",
                    reply_markup=confirm_kb
                )
            except Exception as e:
                logger.error(f"Fallback texto simples no envio do PIX: {e}")
                try:
                    await target_bot.send_message(
                        chat_id=target_chat_id,
                        text=(
                            f"🔒 Pagamento Seguro via PIX\n\n"
                            f"Plano: {chosen_offer_name}\n"
                            f"Valor: R$ {price_fmt}\n\n"
                            f"Copie o código PIX Copia e Cola abaixo:\n\n"
                            f"{pix_code}\n\n"
                            f"Assim que concluir, clique abaixo para liberar seu acesso!"
                        ),
                        reply_markup=confirm_kb
                    )
                except Exception as e_fb:
                    logger.error(f"Falha definitiva no envio do PIX: {e_fb}")

            self._start_pix_poll_task(callback.from_user.id, tx_id, pushin_token, gw_provider, gw_secret)

        @self.dp.callback_query(F.data.startswith("checkpix:"))
        async def handle_check_pix(callback: types.CallbackQuery):
            await callback.answer(
                "⏳ Pagamento em processamento! Assim que o seu banco confirmar a transferência, o seu acesso VIP será enviado aqui na conversa automaticamente.",
                show_alert=True
            )

        @self.dp.callback_query(F.data.startswith("fplan:"))
        async def handle_flow_plan_click(callback: types.CallbackQuery):
            try:
                await callback.answer("Carregando oferta...")
            except Exception:
                pass
            parts = callback.data.split(":")
            plan_id = parts[1] if len(parts) > 1 else ""
            disc_pct = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

            state = await self.state_getter()
            cb_bot_tok = getattr(getattr(callback, "bot", None), "token", "") or ""
            if cb_bot_tok:
                _CURRENT_BOT_TOKEN_CTX.set(cb_bot_tok)
                if getattr(callback, "bot", None):
                    _CURRENT_BOT_CTX.set(callback.bot)
            fb = self._get_active_flow_bot(state, bot_token=cb_bot_tok)
            if not fb:
                return

            lead = await self._get_or_create_lead(state, callback.from_user)
            self._advance_stage(lead, "Escolheu um plano")
            lead["accepted_bump_indices"] = []
            auth_discounts = lead.get("authorized_flow_discounts") or []
            if disc_pct not in auth_discounts:
                disc_pct = 0

            active_bumps = self._get_active_bumps_for_plan(fb, plan_id)
            if active_bumps:
                await self.state_saver(state, runtime_only=True)
                await self._present_flow_order_bump(callback, fb, plan_id, disc_pct, bump_index=0)
            else:
                await self._generate_flow_checkout(callback, fb, state, plan_id, disc_pct, accepted_bumps=[])

        @self.dp.callback_query(F.data.startswith("fbump:"))
        async def handle_flow_bump_click(callback: types.CallbackQuery):
            try:
                await callback.answer("Processando...")
            except Exception:
                pass
            parts = callback.data.split(":")
            decision = parts[1] if len(parts) > 1 else "no"
            plan_id = parts[2] if len(parts) > 2 else ""
            disc_pct = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            bump_idx = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

            state = await self.state_getter()
            cb_bot_tok = getattr(getattr(callback, "bot", None), "token", "") or ""
            if cb_bot_tok:
                _CURRENT_BOT_TOKEN_CTX.set(cb_bot_tok)
                if getattr(callback, "bot", None):
                    _CURRENT_BOT_CTX.set(callback.bot)
            fb = self._get_active_flow_bot(state, bot_token=cb_bot_tok)
            if not fb:
                return
            lead = await self._get_or_create_lead(state, callback.from_user)
            auth_discounts = lead.get("authorized_flow_discounts") or []
            if disc_pct not in auth_discounts:
                disc_pct = 0

            accepted_list = lead.setdefault("accepted_bump_indices", [])
            if bump_idx == 0:
                accepted_list.clear()
            if decision == "yes" and bump_idx not in accepted_list:
                accepted_list.append(bump_idx)
            await self.state_saver(state, runtime_only=True)

            active_bumps = self._get_active_bumps_for_plan(fb, plan_id)
            next_idx = bump_idx + 1
            if next_idx < len(active_bumps):
                await self._present_flow_order_bump(callback, fb, plan_id, disc_pct, bump_index=next_idx)
            else:
                chosen_bumps = [active_bumps[i] for i in accepted_list if 0 <= i < len(active_bumps)]
                lead["accepted_bump_indices"] = []
                await self._generate_flow_checkout(callback, fb, state, plan_id, disc_pct, accepted_bumps=chosen_bumps)

    def _get_active_bumps_for_plan(self, fb: Dict[str, Any], plan_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        target_pid = str(plan_id or "")[:20]
        for b in (fb.get("order_bumps") or []):
            if not isinstance(b, dict) or not b.get("active", True):
                continue
            pids = b.get("plan_ids")
            if isinstance(pids, list) and len(pids) > 0:
                allowed = [str(x)[:20] for x in pids if x]
                if allowed and target_pid and target_pid not in allowed:
                    continue
            out.append(b)
        return out

    async def _present_flow_order_bump(
        self,
        callback: types.CallbackQuery,
        fb: Dict[str, Any],
        plan_id: str,
        disc_pct: int,
        bump_index: int
    ):
        active_bumps = self._get_active_bumps_for_plan(fb, plan_id)
        if bump_index >= len(active_bumps):
            return
        bump = active_bumps[bump_index]
        target_bot = getattr(callback, "bot", None) or _CURRENT_BOT_CTX.get() or self.bot
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        await self._send_flow_medias(chat_id, bump.get("medias", []), bot_instance=target_bot)

        color_emoji = {"Azul": "🔵 ", "Verde": "🟢 ", "Vermelho": "🔴 ", "Padrão": ""}
        acc_text = (bump.get("accept_text") or "Adicionar ao pedido").strip()
        rej_text = (bump.get("reject_text") or "Continuar sem o adicional").strip()
        acc_pref = color_emoji.get(bump.get("accept_color", "Padrão"), "")
        rej_pref = color_emoji.get(bump.get("reject_color", "Padrão"), "")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{acc_pref}{acc_text}", callback_data=f"fbump:yes:{plan_id}:{disc_pct}:{bump_index}")],
            [InlineKeyboardButton(text=f"{rej_pref}{rej_text}", callback_data=f"fbump:no:{plan_id}:{disc_pct}:{bump_index}")]
        ])

        val_fmt = f"R$ {float(bump.get('price') or 0):.2f}".replace(".", ",")
        offer_msg = (bump.get("offer_message") or "").strip()
        if not offer_msg:
            title = bump.get("title") or "Oferta Especial"
            desc = bump.get("description") or ""
            offer_msg = f"🎁 <b>{html.escape(title)}</b> (+{val_fmt})\n\n{html.escape(desc)}"
        else:
            offer_msg = self._render_flow_vars(
                offer_msg,
                callback.from_user,
                fb,
                plan_name=str(bump.get("title") or "Oferta Especial"),
                valor_str=val_fmt
            )

        await self._safe_send_html(chat_id, offer_msg, reply_markup=kb, bot_instance=target_bot)

    async def _generate_flow_checkout(
        self,
        callback: types.CallbackQuery,
        fb: Dict[str, Any],
        state: Dict[str, Any],
        plan_id: str,
        disc_pct: int,
        accepted_bumps: List[Dict[str, Any]]
    ):
        # The caller may hold the shared cache: publish only after commit.
        import copy
        state = copy.deepcopy(state)
        lead = await self._get_or_create_lead(state, callback.from_user)
        self._cancel_downsell(str(callback.from_user.id))

        # Valida desconto contra descontos efetivamente autorizados pelo downsell no servidor
        auth_discounts = lead.get("authorized_flow_discounts") or []
        if disc_pct not in auth_discounts:
            disc_pct = 0

        plan = None
        for idx, p in enumerate(fb.get("plans", [])):
            if str(p.get("id", f"plan_{idx+1}"))[:20] == plan_id:
                plan = p
                break
        if not plan and fb.get("plans"):
            plan = fb["plans"][0]

        plan_name = (plan.get("name") if plan else "Plano VIP") or "Plano VIP"
        base_price = float(plan.get("price", 19.90)) if plan else 19.90
        discounted_price = round(base_price * (1.0 - disc_pct / 100.0), 2) if disc_pct > 0 else base_price
        bump_total = sum(float(b.get("price") or 0) for b in accepted_bumps)
        total_price = round(discounted_price + bump_total, 2)

        if fb.get("price_variation", False):
            total_price = round(total_price + random.randint(1, 9) / 100.0, 2)

        bump_deliverable_str = ""
        if accepted_bumps:
            bump_titles = " + ".join(b.get("title") or "Adicional" for b in accepted_bumps)
            full_plan_label = f"{plan_name} (+ {bump_titles})"
            deliverables = [b.get("deliverable", "").strip() for b in accepted_bumps if (b.get("deliverable") or "").strip()]
            bump_deliverable_str = "\n\n".join(deliverables)
            lead["accepted_bump_deliverable"] = bump_deliverable_str
            lead["has_order_bump"] = True
        else:
            full_plan_label = plan_name
            lead.pop("accepted_bump_deliverable", None)
            lead["has_order_bump"] = False

        lead["flow_plan_id"] = plan.get("id") if plan else ""
        lead["flow_plan_price"] = total_price
        lead["chosen_plan"] = full_plan_label
        self._advance_stage(lead, "Escolheu um plano")
        self._update_lead_insights(state, lead, chosen_plan=full_plan_label)

        pub = state.get("publish", {})
        pushin_token = pub.get("pushin_pay_token", "").strip()
        gw_provider = pub.get("gateway_provider", "syncpay").strip()
        gw_secret = pub.get("gateway_secret", "").strip()
        cents = max(50, int(round(total_price * 100)))
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        target_bot = getattr(callback, "bot", None) or _CURRENT_BOT_CTX.get() or self.bot

        # Fail-closed: nunca cria PIX sim_ caso o gateway falhe ou não esteja configurado
        if not pushin_token:
            await self.state_saver(state, runtime_only=True)
            await (target_bot or self.bot).send_message(
                chat_id=chat_id,
                text="⚠️ O pagamento via PIX está temporariamente indisponível. Tente novamente em instantes."
            )
            return

        try:
            client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
            res = await client.create_pix_charge(
                value_in_cents=cents,
                client_name=callback.from_user.first_name or "Cliente VIP"
            )
            pix_code = (res or {}).get("qr_code") or ""
            tx_id = str((res or {}).get("id") or "").strip()
            if not pix_code or not tx_id or tx_id.startswith("sim_"):
                raise ValueError("Gateway não retornou id ou qr_code válidos")
        except Exception as e:
            logger.error(f"Erro Gateway PIX Flow ({gw_provider}): {e}")
            await self.state_saver(state, runtime_only=True)
            await (target_bot or self.bot).send_message(
                chat_id=chat_id,
                text="⚠️ Não foi possível gerar a cobrança PIX no momento. Por favor, tente novamente em instantes."
            )
            return

        self._advance_stage(lead, "Gerou checkout")
        learning = state.setdefault("learning", {})
        learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) + 1

        fb_deliv = fb.get("fallback_delivery", {})
        dest_type = (plan.get("delivery_dest_type") if plan else "Herdar do fluxo") or "Herdar do fluxo"
        if dest_type == "Herdar do fluxo":
            eff_type = fb_deliv.get("dest_type", "Canal ou grupo")
            ch_target = (fb_deliv.get("channel_id") or "").strip()
            ext_url = (fb_deliv.get("external_url") or "").strip()
        else:
            eff_type = dest_type
            raw_plan_ch = ((plan.get("delivery_channel_id") if plan else "") or "").strip()
            fb_ch = (fb_deliv.get("channel_id") or "").strip()
            if (not raw_plan_ch or raw_plan_ch == "-1003939135982") and fb_ch and fb_ch != "-1003939135982":
                ch_target = fb_ch
            else:
                ch_target = raw_plan_ch or fb_ch
            ext_url = ((plan.get("delivery_external_url") if plan else "") or fb_deliv.get("external_url") or "").strip()

        ch_name_val = ((plan.get("delivery_channel_name") if plan else "") or fb_deliv.get("channel_name") or "Grupo VIP").strip()
        dur_val = ((plan.get("duration") if plan else "") or "Vitalício").strip()
        tx_record = {
            "tx_id": tx_id,
            "user_id": int(callback.from_user.id),
            "mode": "flow",
            "bot_id": str(fb.get("id") or ""),
            "bot_token": str(_CURRENT_BOT_TOKEN_CTX.get() or fb.get("token") or self.current_token or ""),
            "plan_id": str(plan.get("id") if plan else plan_id),
            "offer_name": full_plan_label,
            "plan_duration": dur_val,
            "amount": round(total_price, 2),
            "cents": cents,
            "is_upsell": False,
            "deliverable": bump_deliverable_str,
            "delivery_dest_type": eff_type,
            "delivery_channel_id": ch_target,
            "delivery_channel_name": ch_name_val,
            "delivery_external_url": ext_url,
            "access_status": "Pendente",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }
        state.setdefault("tx_records", {})[tx_id] = tx_record

        lead["last_tx_id"] = tx_id
        lead["last_tx_is_upsell"] = False
        valor_str = f"R$ {total_price:.2f}".replace(".", ",")
        lead.setdefault("history", []).append({
            "role": "assistant",
            "content": f"[PIX Flow — {full_plan_label} ({valor_str})] {pix_code}"
        })
        await self.state_saver(state, runtime_only=True)
        self.tx_records[tx_id] = tx_record
        self.active_tx_map[tx_id] = callback.from_user.id

        pay_cfg = fb.get("payments", {})
        pix_fmt_mode = pay_cfg.get("pix_format", "Código (monoespaçado)")
        safe_pix = html.escape(pix_code)
        if "bloco" in pix_fmt_mode.lower():
            pix_code_formatted = f"<pre>{safe_pix}</pre>"
        elif "simples" in pix_fmt_mode.lower():
            pix_code_formatted = safe_pix
        else:
            pix_code_formatted = f"<code>{safe_pix}</code>"

        gen_tmpl = pay_cfg.get("generated_message") or (
            "⭐ <b>Você selecionou o seguinte plano:</b>\n\n"
            "🎁 Plano: {plano}\n💲 Valor: {valor}\n\n"
            "💎 Pague via Pix Copia e Cola:\n\n{pix_code}"
        )
        rendered_gen = self._render_flow_vars(
            gen_tmpl,
            callback.from_user,
            fb,
            plan_name=full_plan_label,
            valor_str=valor_str,
            pix_code_fmt=pix_code_formatted
        )

        target_bot = getattr(callback, "bot", None) or _CURRENT_BOT_CTX.get() or self.bot
        bot_tok_val = str(getattr(target_bot, "token", "") or _CURRENT_BOT_TOKEN_CTX.get() or fb.get("token") or self.current_token or "").strip()

        await self._send_flow_medias(chat_id, pay_cfg.get("generated_medias", []), bot_instance=target_bot)

        safe_cb = f"checkpix:{tx_id[:50]}"
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Verificar pagamento", callback_data=safe_cb)]
        ])

        await self._safe_send_html(chat_id, rendered_gen, reply_markup=confirm_kb, bot_instance=target_bot)

        if pay_cfg.get("secondary_enabled") and (pay_cfg.get("secondary_message") or "").strip():
            sec_txt = self._render_flow_vars(
                pay_cfg["secondary_message"],
                callback.from_user,
                fb,
                plan_name=full_plan_label,
                valor_str=valor_str,
                pix_code_fmt=pix_code_formatted
            )
            await self._safe_send_html(chat_id, sec_txt, bot_instance=target_bot)

        self._schedule_flow_downsell(callback.from_user, track="pix", bot_token=bot_tok_val)
        self._start_pix_poll_task(callback.from_user.id, tx_id, pushin_token, gw_provider, gw_secret)

    def _start_pix_poll_task(
        self,
        user_id: int,
        tx_id: str,
        pushin_token: str,
        gw_provider: str = "syncpay",
        gw_secret: str = ""
    ):
        if not tx_id or str(tx_id).startswith("sim_"):
            return
        for old_tx, old_uid in list(self.active_tx_map.items()):
            if int(old_uid) == int(user_id) and old_tx != str(tx_id):
                prev_t = self.pix_poll_tasks.pop(old_tx, None)
                if prev_t and not prev_t.done():
                    prev_t.cancel()
        old_t = self.pix_poll_tasks.pop(str(tx_id), None)
        if old_t and not old_t.done():
            old_t.cancel()
        self.active_tx_map[str(tx_id)] = user_id

    def _build_input_file(self, url_or_b64: str, filename: str = "media.jpg"):
        if not url_or_b64:
            return None
        if url_or_b64.startswith("data:"):
            try:
                _, b64_data = url_or_b64.split(",", 1)
                raw_bytes = base64.b64decode(b64_data)
                return BufferedInputFile(raw_bytes, filename=filename)
            except Exception as e:
                logger.error(f"Erro decodificando mídia Base64: {e}")
                return None
        return URLInputFile(url_or_b64)

    async def notify_payment_by_tx_id(self, tx_id: str) -> bool:
        if not tx_id or str(tx_id).startswith("sim_"):
            return False
        state = await self.state_getter() if self.state_getter else {}
        tx_record = (state.get("tx_records") or {}).get(tx_id) or self.tx_records.get(tx_id)
        if not tx_record:
            return False
        if tx_record.get("status") in ("cancelled", "canceled") or tx_record.get("delivered"):
            return False
        uid = int(tx_record.get("user_id") or 0)
        if not uid:
            return False
        return await self.deliver_purchase_and_upsell(uid, tx_id=tx_id)

    async def _poll_pix_payment(self, user_id: int, tx_id: str, pushin_token: str, gw_provider: str = "syncpay", gw_secret: str = ""):
        if not tx_id or str(tx_id).startswith("sim_"):
            return
        client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
        # Backoff progressivo: 8s nos primeiros 2 min (15x), 15s de 2 a 5 min (12x), 30s de 5 a 15 min (20x)
        intervals = ([8.0] * 15) + ([15.0] * 12) + ([30.0] * 20)
        try:
            for base_wait in intervals:
                await asyncio.sleep(base_wait + random.uniform(0.0, 1.2))
                existing_tx = self.tx_records.get(tx_id)
                if existing_tx and (existing_tx.get("status") in ("cancelled", "canceled") or existing_tx.get("delivered")):
                    return
                try:
                    data = await client.check_transaction_status(tx_id)
                    if str((data or {}).get("status", "")).lower() == "paid":
                        # The retry worker handles the single delayed retry.
                        await self.deliver_purchase_and_upsell(user_id, tx_id=tx_id)
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            self.pix_poll_tasks.pop(tx_id, None)

    def _schedule_upsell(self, user_id: int, bot_token: str = ""):
        uid = str(user_id)
        tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.active_token or "").strip()
        bot_prefix = tok.split(":", 1)[0] if (tok and ":" in tok) else ""
        self._cancel_upsell(uid, bot_id=bot_prefix)
        task = asyncio.create_task(self._upsell_worker(user_id, bot_token=tok))
        self.upsell_tasks[uid] = task
        if bot_prefix:
            self.upsell_tasks[f"{bot_prefix}:{uid}"] = task

    async def _upsell_worker(self, user_id: int, bot_token: str = ""):
        try:
            state = await self.state_getter()
            effective_tok = (bot_token or _CURRENT_BOT_TOKEN_CTX.get() or self.current_token or "").strip()
            if self._get_active_flow_bot(state, bot_token=effective_tok):
                return
            upsell = state.get("upsell", {})
            if not upsell.get("enabled", False):
                return
            delay_mins = max(1, int(upsell.get("delay_minutes", 1)))
            await asyncio.sleep(max(5, delay_mins * 60))

            state = await self.state_getter()
            if self._get_active_flow_bot(state, bot_token=effective_tok):
                return
            upsell = state.get("upsell", {})
            if not upsell.get("enabled", False):
                return
            lead = await self._get_or_create_lead(
                state,
                types.User(id=user_id, is_bot=False, first_name="Assinante"),
                bot_token=effective_tok
            )
            if lead.get("blocked"):
                return

            target_bot = None
            if effective_tok:
                target_bot = self._get_bot_by_token(effective_tok)
            if not target_bot and lead.get("origin_bot_numeric_id"):
                target_bot = self._get_bot_by_token(f"{lead['origin_bot_numeric_id']}:")
            if not target_bot:
                target_bot = self.bot

            up_medias = [m for m in upsell.get("medias", []) if isinstance(m, dict) and (m.get("url") or m.get("telegram_file_id"))]
            if len(up_medias) >= 2:
                await self._send_flow_medias(user_id, up_medias, bot_instance=target_bot)
            else:
                for m in up_medias:
                    try:
                        await self._send_single_media_cached(
                            user_id,
                            m,
                            caption=m.get("caption") or None,
                            has_spoiler=bool(m.get("spoiler", False)),
                            bot_instance=target_bot
                        )
                    except Exception:
                        pass
            upsell_msg = upsell.get("message", "agora que seu acesso foi liberado, tenho uma opção extra que combina com você 👀")
            target_up_id = str(upsell.get("offer_id", ""))
            matched_up_offer = None
            for o in state.get("business", {}).get("offers", []):
                if str(o.get("id")) == target_up_id:
                    matched_up_offer = {**o, "specific_match": True}
                    break
            markup = self._build_offers_keyboard(state, only_offer=matched_up_offer, is_upsell=True)
            sent = await self._safe_send_html(user_id, upsell_msg, reply_markup=markup, bot_instance=target_bot)
            if not sent and lead.get("blocked"):
                return

            self._advance_stage(lead, "Recebeu upsell")
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": f"[Upsell] {upsell_msg}"
            })
            await self.state_saver(state, runtime_only=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Upsell worker: {e}")
        finally:
            self._cancel_upsell(user_id)

    async def deliver_purchase_and_upsell(self, user_id: int, tx_id: Optional[str] = None) -> bool:
        if not tx_id or str(tx_id).startswith("sim_"):
            return False

        async with self._get_tx_lock(tx_id):
            state = await self.state_getter() if self.state_getter else {}
            tx_record = (state.setdefault("tx_records", {})).get(tx_id) or self.tx_records.get(tx_id)
            if not tx_record or int(tx_record.get("user_id", 0)) != int(user_id):
                return False

            if tx_record.get("status") in ("cancelled", "canceled"):
                return False

            # Idempotência total de entrega
            if tx_record.get("delivered"):
                return True

            tx_bot_token = str(tx_record.get("bot_token") or "").strip()
            tx_bot_id = str(tx_record.get("bot_id") or "").strip()
            # A flow configuration ID is not an immutable Telegram identity:
            # never fill a missing purchase token from a mutable configuration.
            resolved_bot = None
            if tx_bot_token and tx_bot_token != "***configured***":
                _CURRENT_BOT_TOKEN_CTX.set(tx_bot_token)
                resolved_bot = self._get_bot_by_token(tx_bot_token)
                if resolved_bot:
                    _CURRENT_BOT_CTX.set(resolved_bot)
            is_flow_tx = (tx_record.get("mode") == "flow")
            flow_bot = self._get_active_flow_bot(state, bot_token=tx_bot_token, bot_id=tx_bot_id) if is_flow_tx else None
            target_bot = resolved_bot
            effective_bot_id = tx_bot_id or (str(flow_bot.get("id") or "") if flow_bot else "")
            missing_origin = not tx_bot_token or tx_bot_token == "***configured***"
            ai_identity_conflict = (
                not is_flow_tx and tx_bot_id.isdigit()
                and not missing_origin and tx_bot_id != tx_bot_token.split(":", 1)[0]
            )

            if missing_origin or ai_identity_conflict or (is_flow_tx and not flow_bot):
                # Payment remains confirmed; ownership is ambiguous, so do not
                # attribute revenue or dispatch using another bot's configuration.
                if tx_record.get("status") != "paid":
                    tx_record["status"] = "paid"
                    tx_record.setdefault("sales_counted", False)
                    tx_record.setdefault("paid_at", datetime.utcnow().isoformat())
                inactive_origin = any(
                    isinstance(fb, dict) and str(fb.get("id") or "") == tx_bot_id
                    and not fb.get("active", True)
                    and bool(tx_bot_token and tx_bot_token != "***configured***")
                    and (
                        str(fb.get("token") or "") == tx_bot_token
                        or str(fb.get("token") or "").startswith(tx_bot_token.split(":", 1)[0] + ":")
                    )
                    for fb in (state.get("flow_bots") or [])
                )
                if inactive_origin:
                    tx_record["initial_delivery_pending"] = True
                else:
                    tx_record["attribution_uncertain"] = True
                tx_record["access_status"] = (
                    "Pendente (Bot de Origem Indisponível)" if inactive_origin
                    else "Pendente (Bot de origem não confere)"
                )
                tx_record["delivery_error"] = "Identidade do bot de origem indisponível ou divergente."
                self.tx_records[tx_id] = tx_record
                state["tx_records"][tx_id] = tx_record
                await self.state_saver(state, runtime_only=True)
                return False

            if not is_flow_tx and not target_bot:
                if tx_record.get("status") != "paid":
                    tx_record["status"] = "paid"
                    tx_record.setdefault("sales_counted", False)
                    tx_record.setdefault("paid_at", datetime.utcnow().isoformat())
                tx_record["initial_delivery_pending"] = True
                tx_record["access_status"] = "Pendente (Bot de origem indisponível)"
                tx_record["delivery_error"] = "Bot de origem não está em execução."
                self.tx_records[tx_id] = tx_record
                state["tx_records"][tx_id] = tx_record
                await self.state_saver(state, runtime_only=True)
                return False

            # Matching purchase identity is now verified; keep the payment
            # marker unchanged because legacy records may have been counted.
            tx_record.pop("attribution_uncertain", None)

            # Lead retrieval sob lock do par (bot_id, user_id)
            async with self._get_lead_lock(str(user_id), bot_id=effective_bot_id):
                memory = state.setdefault("memory", {})
                tx_origin_numeric_id = tx_bot_token.split(":", 1)[0] if ":" in tx_bot_token else ""

                def matches_purchase_lead(item: Dict[str, Any]) -> bool:
                    if (str(item.get("telegram_id") or "") != str(user_id)
                            or not tx_origin_numeric_id.isdecimal()
                            or str(item.get("origin_bot_numeric_id") or "") != tx_origin_numeric_id):
                        return False
                    lead_config_id = str(item.get("bot_id") or "")
                    if is_flow_tx:
                        return lead_config_id == effective_bot_id
                    return not lead_config_id or lead_config_id == effective_bot_id

                active_lead = next((item for item in memory.get("leads", []) if matches_purchase_lead(item)), None)
                archived_lead = next((item for item in memory.get("deleted_leads", []) if matches_purchase_lead(item)), None)
                lead = active_lead or archived_lead
                if lead is None:
                    lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name="Assinante"), bot_token=tx_bot_token)
                self._cancel_downsell(str(user_id), bot_id=effective_bot_id)

                is_upsell_payment = bool(tx_record.get("is_upsell"))
                paid_val = float(tx_record.get("amount") or (lead.get("flow_plan_price") or 9.90))

                # ========================================================
                # FASE 1: LIQUIDAÇÃO FINANCEIRA ATÔMICA (EXATAMENTE UMA VEZ)
                # ========================================================
                already_counted = bool(tx_record.get("sales_counted")) or (
                    tx_record.get("status") == "paid" and "sales_counted" not in tx_record
                )
                if not already_counted:
                    tx_record["status"] = "paid"
                    tx_record["sales_counted"] = True
                    tx_record.setdefault("paid_at", datetime.utcnow().isoformat())

                    if is_upsell_payment:
                        learning = state.setdefault("learning", {})
                        learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
                        if int(learning.get("unpaid_checkouts", 0)) > 0:
                            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1
                        self._advance_stage(lead, "Comprou upsell")
                    elif is_flow_tx and flow_bot:
                        flow_bot["sales_today"] = int(flow_bot.get("sales_today") or 0) + 1
                        flow_bot["revenue_today"] = round(float(flow_bot.get("revenue_today") or 0.0) + paid_val, 2)
                        self._advance_stage(lead, "Pagamento aprovado")
                        learning = state.setdefault("learning", {})
                        learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
                        if int(learning.get("unpaid_checkouts", 0)) > 0:
                            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1
                    elif not is_flow_tx:
                        self._advance_stage(lead, "Pagamento aprovado")
                        learning = state.setdefault("learning", {})
                        learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
                        if int(learning.get("unpaid_checkouts", 0)) > 0:
                            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1

                    self._update_lead_insights(state, lead)

                    # Cancela polling de PIX se ainda ativo
                    poll_t = self.pix_poll_tasks.pop(tx_id, None)
                    if poll_t and not poll_t.done() and poll_t is not asyncio.current_task():
                        poll_t.cancel()

                    # Persiste estado da Fase 1 imediatamente
                    self.tx_records[tx_id] = tx_record
                    state["tx_records"][tx_id] = tx_record
                    await self.state_saver(state, runtime_only=True)

                if is_flow_tx and not target_bot:
                    tx_record["initial_delivery_pending"] = True
                    tx_record["access_status"] = "Pendente (Bot de origem indisponível)"
                    tx_record["delivery_error"] = "Bot de origem não está em execução."
                    self.tx_records[tx_id] = tx_record
                    state["tx_records"][tx_id] = tx_record
                    await self.state_saver(state, runtime_only=True)
                    return False

                # The payment is counted independently of fulfillment. Only the
                # initial automatic send and one delayed retry are permitted per Pix.
                try:
                    auto_attempts = max(0, int(tx_record.get("automatic_delivery_attempts", tx_record.get("delivery_attempts") or 0)))
                except (TypeError, ValueError):
                    auto_attempts = 2  # ambiguous legacy data: require manual review
                if auto_attempts >= 2:
                    if tx_record.get("access_status") != "Pendente (Reenvio manual)":
                        tx_record["access_status"] = "Pendente (Reenvio manual)"
                        await self.state_saver(state, runtime_only=True)
                    return False
                if auto_attempts and tx_record.get("last_delivery_attempt_at"):
                    try:
                        last_attempt = datetime.fromisoformat(str(tx_record["last_delivery_attempt_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
                        if (datetime.utcnow() - last_attempt).total_seconds() < 45:
                            return False
                    except (TypeError, ValueError):
                        pass

                # Reserve the attempt durably BEFORE contacting Telegram, so a
                # crash after sending cannot spend the same automatic retry again.
                tx_record["automatic_delivery_attempts"] = auto_attempts + 1
                tx_record.pop("initial_delivery_pending", None)
                tx_record["delivery_attempts"] = int(tx_record.get("delivery_attempts") or 0) + 1
                tx_record["last_delivery_attempt_at"] = datetime.utcnow().isoformat()
                if auto_attempts + 1 >= 2:
                    tx_record["access_status"] = "Pendente (Reenvio manual)"
                self.tx_records[tx_id] = tx_record
                state["tx_records"][tx_id] = tx_record
                await self.state_saver(state, runtime_only=True)

                # ========================================================
                # FASE 2: DESPACHO E ENTREGA (AT-LEAST-ONCE DELIVERY)
                # ========================================================

                if is_upsell_payment:
                    try:
                        await (target_bot or self.bot).send_message(
                            chat_id=user_id,
                            text="💜 <b>Upsell confirmado!</b>\n\nSeu conteúdo extra exclusivo já está liberado!",
                            parse_mode="HTML"
                        )
                        tx_record["delivered"] = True
                        tx_record["access_status"] = "Entregue"
                        lead.setdefault("history", []).append({
                            "role": "assistant",
                            "content": "[Upsell pago e entregue com sucesso]"
                        })
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record
                        await self.state_saver(state, runtime_only=True)
                        return True
                    except Exception as e:
                        logger.error(f"Erro no despacho de upsell para tx_id={tx_id}: {e}")
                        tx_record["delivery_error"] = str(e)
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record
                        await self.state_saver(state, runtime_only=True)
                        return False

                if is_flow_tx:
                    if not flow_bot:
                        logger.error(f"Entrega abortada para tx_id={tx_id}: Bot de fluxo associado está inativo ou indisponível.")
                        tx_record["access_status"] = "Pendente (Bot de Origem Indisponível)"
                        tx_record["delivered"] = False
                        tx_record["delivery_error"] = "Bot de fluxo associado está inativo ou desativado."
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record
                        await self.state_saver(state, runtime_only=True)
                        return False

                    if tx_record.get("delivery_dest_type"):
                        eff_type = tx_record.get("delivery_dest_type", "Canal ou grupo")
                        ch_target = (tx_record.get("delivery_channel_id") or "").strip()
                        ext_url = (tx_record.get("delivery_external_url") or "").strip()
                    else:
                        target_plan_id = str(tx_record.get("plan_id") or lead.get("flow_plan_id"))
                        plan_obj = None
                        for p in flow_bot.get("plans", []):
                            if str(p.get("id")) == target_plan_id:
                                plan_obj = p
                                break
                        if not plan_obj and flow_bot.get("plans"):
                            plan_obj = flow_bot["plans"][0]

                        fb_deliv = flow_bot.get("fallback_delivery", {})
                        dest_type = (plan_obj.get("delivery_dest_type") if plan_obj else "Herdar do fluxo") or "Herdar do fluxo"
                        if dest_type == "Herdar do fluxo":
                            eff_type = fb_deliv.get("dest_type", "Canal ou grupo")
                            ch_target = (fb_deliv.get("channel_id") or "").strip()
                            ext_url = (fb_deliv.get("external_url") or "").strip()
                        else:
                            eff_type = dest_type
                            ch_target = (plan_obj.get("delivery_channel_id") or fb_deliv.get("channel_id") or "").strip()
                            ext_url = (plan_obj.get("delivery_external_url") or fb_deliv.get("external_url") or "").strip()

                    invite_url = ""
                    if tx_record.get('primary_access_delivered'):
                        # This retry sends only the purchased additional; the
                        # primary message already carries its original invite.
                        invite_url = str(tx_record.get('invite_url') or '')
                    elif eff_type == "Link externo" and ext_url:
                        invite_url = ext_url
                    elif ch_target and (target_bot or self.bot):
                        try:
                            active_bot_deliv = target_bot or self.bot
                            invite = await active_bot_deliv.create_chat_invite_link(
                                chat_id=ch_target,
                                member_limit=1,
                                expire_date=datetime.now(timezone.utc) + timedelta(minutes=15)
                            )
                            invite_url = (invite.invite_link or "").strip()
                        except Exception as e:
                            logger.warning(f"Erro gerando link de convite no canal {ch_target}: {e}")
                            invite_url = ""

                    if not invite_url or invite_url == "https://t.me/+link_exclusivo_vip":
                        logger.error(f"Entrega automática pendente para tx_id={tx_id}: link de convite não pôde ser gerado no canal {ch_target}.")
                        tx_record["access_status"] = "Pendente (Admin no Canal)"
                        tx_record["delivered"] = False
                        tx_record["delivery_error"] = f"Não foi possível gerar link de convite no canal {ch_target}."
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record

                        sup_contact = state.get("publish", {}).get("support_username", "@suporte")
                        plan_label = tx_record.get("offer_name") or lead.get("chosen_plan") or "Plano VIP"
                        try:
                            await self._safe_send_html(
                                user_id,
                                f"✅ <b>Pagamento Aprovado!</b>\n\n"
                                f"Parabéns {html.escape(lead.get('first_name') or 'Cliente')}! Seu pagamento do plano <b>{html.escape(plan_label)}</b> no valor de <b>R$ {paid_val:.2f}</b> foi confirmado com sucesso!\n\n"
                                f"⚠️ O link automático de acesso está sendo gerado pelo suporte. Se demorar, contate {sup_contact} para liberação imediata.",
                                bot_instance=target_bot
                            )
                        except Exception:
                            pass
                        await self.state_saver(state, runtime_only=True)
                        return False

                    pay_cfg = flow_bot.get("payments", {})
                    await self._send_flow_medias(user_id, pay_cfg.get("approved_medias", []), bot_instance=target_bot)

                    btn_txt = (pay_cfg.get("access_button_text") or "Acessar conteúdo").strip()
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=btn_txt, url=invite_url)]
                    ])
                    fake_user = types.User(id=user_id, is_bot=False, first_name=lead.get("first_name") or "Assinante")
                    app_tmpl = pay_cfg.get("approved_message") or (
                        "✅ <b>Pagamento Aprovado!</b>\n\n"
                        "Parabéns {nome}! Seu pagamento do plano <b>{plano}</b> no valor de <b>{valor}</b> foi confirmado.\n\n"
                        "🎉 Você já tem acesso ao conteúdo!"
                    )
                    plan_label = tx_record.get("offer_name") or lead.get("chosen_plan") or "Plano VIP"
                    rendered_app = self._render_flow_vars(
                        app_tmpl,
                        fake_user,
                        flow_bot,
                        plan_name=plan_label,
                        valor_str=f"R$ {paid_val:.2f}".replace(".", ",")
                    )
                    access_message = True if tx_record.get('primary_access_delivered') else await self._safe_send_html(user_id, rendered_app, reply_markup=kb, bot_instance=target_bot)
                    if access_message is not None:
                        tx_record['primary_access_delivered'] = True
                        tx_record['invite_url'] = invite_url
                        await self.state_saver(state, runtime_only=True)

                    bump_deliv = (tx_record.get("deliverable") or "").strip()
                    if access_message is not None and bump_deliv and not tx_record.get('additional_delivered'):
                        access_message = await self._safe_send_html(
                            user_id,
                            f"🎁 <b>Seu adicional adquirido:</b>\n\n{bump_deliv}",
                            bot_instance=target_bot
                        )
                        if access_message is not None:
                            tx_record['additional_delivered'] = True
                            await self.state_saver(state, runtime_only=True)
                    # The HTML helper returns None after both transport attempts
                    # fail. A confirmed payment is not evidence of access delivery.
                    if access_message is None:
                        tx_record["delivered"] = False
                        tx_record["access_status"] = (
                            "Pendente (Reenvio manual)" if auto_attempts + 1 >= 2 else "Pendente"
                        )
                        tx_record["delivery_error"] = "Telegram não confirmou o envio do acesso."
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record
                        await self.state_saver(state, runtime_only=True)
                        return False

                    dur_str = str(tx_record.get("plan_duration") or "Vitalício")
                    exp_days = 0
                    if "7" in dur_str or "semanal" in dur_str.lower():
                        exp_days = 7
                    elif "30" in dur_str or "mensal" in dur_str.lower():
                        exp_days = 30
                    elif "90" in dur_str or "trimestral" in dur_str.lower():
                        exp_days = 90
                    elif "180" in dur_str or "semestral" in dur_str.lower():
                        exp_days = 180
                    elif "365" in dur_str or "anual" in dur_str.lower():
                        exp_days = 365

                    tx_record["delivered"] = True
                    tx_record["access_status"] = "Ativo"
                    tx_record["invite_url"] = invite_url
                    tx_record["expires_at"] = (datetime.utcnow() + timedelta(days=exp_days)).isoformat() if exp_days > 0 else "Vitalício"
                    tx_record.pop("delivery_error", None)
                    if not tx_record.get("delivery_channel_name"):
                        tx_record["delivery_channel_name"] = (
                            (flow_bot.get("fallback_delivery") or {}).get("channel_name")
                            or ch_target
                            or "Link Externo"
                        )
                    self.tx_records[tx_id] = tx_record
                    state["tx_records"][tx_id] = tx_record

                    self._advance_stage(lead, "Entrega concluída")
                    lead.setdefault("history", []).append({
                        "role": "assistant",
                        "content": f"[Pagamento aprovado Flow ({plan_label}) — Entrega concluída]"
                    })
                    await self.state_saver(state, runtime_only=True)
                    return True

                # Caso IA (não-flow)
                pub = state.get("publish", {})
                dest = (tx_record.get("delivery_destination") or pub.get("delivery_destination", "")).strip()
                delivery_type = tx_record.get("delivery_type") or pub.get("delivery_type", "Link de acesso")
                invite_url = ""

                if delivery_type == "Mensagem no bot" and dest and not dest.startswith("http"):
                    await (target_bot or self.bot).send_message(
                        chat_id=user_id,
                        text=f"💜 <b>Pagamento confirmado!</b>\n\n{dest}",
                        parse_mode="HTML"
                    )
                else:
                    if dest.startswith("http"):
                        invite_url = dest
                    elif dest and (target_bot or self.bot):
                        try:
                            active_bot_deliv = target_bot or self.bot
                            invite = await active_bot_deliv.create_chat_invite_link(
                                chat_id=dest,
                                member_limit=1,
                                expire_date=datetime.now(timezone.utc) + timedelta(days=1)
                            )
                            invite_url = (invite.invite_link or "").strip()
                        except Exception as e:
                            logger.warning(f"Não foi possível criar link único no canal {dest}: {e}")
                            invite_url = ""

                    if not invite_url or invite_url == "https://t.me/+link_exclusivo_vip":
                        logger.error(f"Entrega abortada para tx_id={tx_id}: link de convite válido não pôde ser gerado.")
                        tx_record["access_status"] = "Pendente"
                        tx_record["delivered"] = False
                        tx_record["delivery_error"] = f"Não foi possível gerar link no canal {dest}."
                        self.tx_records[tx_id] = tx_record
                        state["tx_records"][tx_id] = tx_record
                        await self.state_saver(state, runtime_only=True)
                        return False

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔥 ENTRAR NO MEU VIP AGORA", url=invite_url)]
                    ])

                    await target_bot.send_message(
                        chat_id=user_id,
                        text="💜 <b>Pagamento confirmado!</b>\n\nAqui está o seu acesso exclusivo ao meu VIP privado:",
                        parse_mode="HTML",
                        reply_markup=kb
                    )

                tx_record["delivered"] = True
                tx_record["access_status"] = "Ativo"
                tx_record["invite_url"] = invite_url
                tx_record.pop("delivery_error", None)
                self.tx_records[tx_id] = tx_record
                state["tx_records"][tx_id] = tx_record

                self._advance_stage(lead, "Entrega concluída")
                lead.setdefault("history", []).append({
                    "role": "assistant",
                    "content": "[Pagamento aprovado — Entrega concluída]"
                })
                await self.state_saver(state, runtime_only=True)

                upsell = state.get("upsell", {})
                if upsell.get("enabled", False):
                    self._schedule_upsell(user_id, bot_token=tx_bot_token)
                return True


bot_manager = ActiveBotManager()

