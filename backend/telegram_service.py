import asyncio
import base64
import html
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, URLInputFile, BufferedInputFile

from pushin_pay import PushinPayClient
from ai_engine import generate_ai_reply

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SharkBotTelegram")


class ActiveBotManager:
    """
    Gerencia a instância ativa do Bot do Telegram em background,
    incluindo:
    - /start com variações de abertura e simulação de digitação ("typing...")
    - Conversa com IA contextual + envio de mídias com Spoiler e Cache de file_id
    - Geração de PIX via Pushin Pay (Webhook + Polling automático de confirmação)
    - Entrega automática de link único do Canal VIP
    - Sequência de Downsell (até 6 etapas por inatividade)
    - Disparo automático de Upsell pós-compra
    - Registro em tempo real na Memória das Conversas e Aprendizado de Vendas
    """

    def __init__(self):
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.polling_task: Optional[asyncio.Task] = None
        self.current_token: str = ""
        self.downsell_tasks: Dict[str, asyncio.Task] = {}
        self.active_tx_map: Dict[str, int] = {}
        self.lead_locks: Dict[str, asyncio.Lock] = {}
        self.state_getter = None
        self.state_saver = None

    def _get_lead_lock(self, uid: str) -> asyncio.Lock:
        if len(self.lead_locks) > 1000:
            # Limpa locks ociosos para não acumular na RAM
            idle_keys = [k for k, lk in self.lead_locks.items() if not lk.locked()][:500]
            for k in idle_keys:
                self.lead_locks.pop(k, None)
        lock = self.lead_locks.get(uid)
        if not lock:
            lock = asyncio.Lock()
            self.lead_locks[uid] = lock
        return lock

    async def configure_and_start(self, state_getter, state_saver):
        self.state_getter = state_getter
        self.state_saver = state_saver
        state = await self.state_getter()
        publish = state.get("publish", {})
        token = publish.get("bot_token", "").strip()
        # Se há token preenchido, ativa por padrão a menos que explicitamente desativado
        if token and "is_live" not in publish:
            publish["is_live"] = True
        is_live = bool(publish.get("is_live", True)) if token else False

        if not token or not is_live:
            await self.stop()
            return {"status": "stopped", "reason": "Bot desativado ou sem token"}

        if token == self.current_token and self.polling_task and not self.polling_task.done():
            return {"status": "running", "bot_username": publish.get("bot_username", "")}

        await self.stop()

        try:
            test_bot = Bot(token=token)
            me = await test_bot.get_me()
            publish["bot_username"] = f"@{me.username}"
            publish["bot_first_name"] = me.first_name
            publish["is_live"] = True
            await self.state_saver(state)
        except Exception as e:
            logger.error(f"Token do Telegram inválido: {e}")
            return {"status": "error", "message": f"Token inválido: {e}"}

        self.current_token = token
        self.bot = test_bot
        self.dp = Dispatcher()
        self._register_handlers()
        self.polling_task = asyncio.create_task(self._run_polling())
        logger.info(f"Bot iniciado em produção: @{me.username}")
        return {"status": "running", "bot_username": f"@{me.username}"}

    async def stop(self):
        for t in self.downsell_tasks.values():
            t.cancel()
        self.downsell_tasks.clear()

        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        if self.bot:
            try:
                await self.bot.session.close()
            except Exception:
                pass
        self.bot = None
        self.dp = None
        self.current_token = ""

    async def _safe_feed_update(self, update: types.Update):
        try:
            if self.dp and self.bot:
                await self.dp.feed_update(self.bot, update)
        except Exception as e:
            logger.error(f"Erro processando update {update.update_id}: {e}")

    async def _run_polling(self):
        offset: Optional[int] = None
        try:
            await self.bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            logger.warning(f"Aviso ao limpar webhook: {e}")

        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=offset,
                    timeout=15,
                    allowed_updates=["message", "callback_query"]
                )
                for u in updates:
                    offset = u.update_id + 1
                    asyncio.create_task(self._safe_feed_update(u))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Polling aguardando liberação de instância ({e})...")
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

    async def _get_or_create_lead(self, state: Dict[str, Any], user: types.User) -> Dict[str, Any]:
        memory = state.setdefault("memory", {"leads": []})
        leads: List[Dict[str, Any]] = memory.setdefault("leads", [])
        uid = str(user.id)
        for lead in leads:
            if str(lead.get("telegram_id")) == uid:
                lead["first_name"] = user.first_name or lead.get("first_name", "Visitante")
                lead["username"] = f"@{user.username}" if user.username else lead.get("username", "")
                lead["last_interaction"] = datetime.utcnow().strftime("%d/%m %H:%M")
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
            "last_interaction": datetime.utcnow().strftime("%d/%m %H:%M"),
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

    def _get_active_flow_bot(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        flow_bots = state.get("flow_bots", [])
        if not isinstance(flow_bots, list):
            return None
        for fb in flow_bots:
            if fb.get("active") and fb.get("mode", "flow") == "flow":
                # Usa modo fluxo se houver mensagem de boas-vindas ou planos cadastrados
                if (fb.get("welcome", {}).get("message") or "").strip() or fb.get("plans"):
                    return fb
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
        nome = (user.first_name or "Cliente").strip()
        rep = {
            "{saudacao}": saudacao,
            "{nome}": nome,
            "{estado}": "São Paulo",
            "{uf}": "SP",
            "{cidade}": "São Paulo",
            "{localizacao}": "São Paulo - SP",
            "{bot.nome}": bot_cfg.get("first_name") or "VIP Bot",
            "{bot.username}": bot_cfg.get("username") or "@bot",
            "{plano}": plan_name,
            "{valor}": valor_str,
            "{pix_code}": pix_code_fmt,
        }
        out = text
        for k, v in rep.items():
            out = out.replace(k, str(v))
        return out

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

    async def _send_flow_medias(self, chat_id: int, medias: List[Dict[str, Any]]):
        for m in (medias or [])[:3]:
            url = m.get("url")
            if not url:
                continue
            try:
                inp = self._build_input_file(url, m.get("name") or "media.jpg")
                if inp:
                    mtype = (m.get("type") or "").lower()
                    if "video" in mtype or str(url).startswith("data:video"):
                        await self.bot.send_video(chat_id=chat_id, video=inp)
                    else:
                        await self.bot.send_photo(chat_id=chat_id, photo=inp)
            except Exception as e:
                logger.warning(f"Falha ao enviar mídia do fluxo: {e}")

    def _schedule_flow_downsell(self, user: types.User, track: str = "general"):
        uid = str(user.id)
        self._cancel_downsell(uid)
        self.downsell_tasks[uid] = asyncio.create_task(self._flow_downsell_worker(user, track))

    async def _flow_downsell_worker(self, user: types.User, track: str):
        paid_stages = ("Pagamento aprovado", "Entrega concluída", "Compra aprovada", "Comprou upsell")
        try:
            state = await self.state_getter()
            fb = self._get_active_flow_bot(state)
            if not fb:
                return
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
                if not msg_tmpl:
                    continue
                target_mins = max(1, int(seq.get("delay_minutes") or 1))
                wait_mins = max(1, target_mins - elapsed_mins)
                await asyncio.sleep(wait_mins * 60)
                elapsed_mins += wait_mins

                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, user)
                if lead.get("stage") in paid_stages:
                    return
                if track == "general" and lead.get("stage") == "Gerou checkout":
                    return

                fb = self._get_active_flow_bot(state)
                if not fb:
                    return
                await self._send_flow_medias(user.id, seq.get("medias", []))
                txt = self._render_flow_vars(msg_tmpl, user, fb)
                kb = self._build_flow_plans_kb(
                    fb,
                    filter_plan_ids=seq.get("plan_ids") or None,
                    discount_str=seq.get("discount", "Sem desconto")
                )
                await self.bot.send_message(chat_id=user.id, text=txt, parse_mode="HTML", reply_markup=kb)
                lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell {track}] {txt}"})
                await self.state_saver(state)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Flow Downsell ({track}): {e}")

    def _cancel_downsell(self, user_id: str):
        task = self.downsell_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    def _schedule_downsell(self, user_id: int, user_first_name: str):
        uid = str(user_id)
        self._cancel_downsell(uid)
        self.downsell_tasks[uid] = asyncio.create_task(self._downsell_worker(user_id, user_first_name))

    async def _downsell_worker(self, user_id: int, user_first_name: str):
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
                ds = state.get("downsell", {})
                if not ds.get("enabled", False):
                    return

                lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name=user_first_name))
                if lead.get("stage") in paid_stages:
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
                lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name=user_first_name))
                if lead.get("stage") in paid_stages:
                    return

                msg_text = next_step.get("message", "").replace("{nome}", user_first_name)
                accompany = next_step.get("accompany", "Botão com os planos atuais")
                has_plans = "planos" in accompany.lower() or "oferta" in accompany.lower()
                markup = self._build_offers_keyboard(state) if has_plans else None

                await self.bot.send_message(chat_id=user_id, text=msg_text, reply_markup=markup)
                if has_plans:
                    self._advance_stage(lead, "Viu os planos")
                lead.setdefault("sent_downsell_steps", []).append(next_step.get("id"))
                lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell] {msg_text}"})
                await self.state_saver(state)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Downsell worker: {e}")

    def cancel_lead_tasks(self, user_id: str):
        self._cancel_downsell(str(user_id))

    def _register_handlers(self):
        @self.dp.message(CommandStart())
        async def handle_start(message: types.Message):
            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))

            # Ao enviar /start, reinicia o histórico, etapa e downsells do lead para permitir novo ciclo limpo
            lead["history"] = []
            self._advance_stage(lead, "Conversando", reset=True)
            lead["sent_downsell_steps"] = []
            lead["prefs_list"] = []
            lead["objs_list"] = []
            lead["preferences"] = "Iniciou conversa no bot"
            lead["objections"] = "Nenhuma identificada"
            lead.pop("accepted_bump_deliverable", None)
            lead.pop("flow_plan_id", None)
            self._update_lead_insights(state, lead)

            flow_bot = self._get_active_flow_bot(state)
            if flow_bot:
                welcome = flow_bot.get("welcome", {})
                await self._send_flow_medias(message.chat.id, welcome.get("medias", []))
                kb = self._build_flow_plans_kb(flow_bot)
                main_msg = self._render_flow_vars(
                    welcome.get("message") or "Olá {saudacao}, {nome}! Escolha seu plano abaixo:",
                    message.from_user,
                    flow_bot
                )
                sec_enabled = bool(welcome.get("secondary_enabled", False))
                sec_msg = self._render_flow_vars(welcome.get("secondary_message") or "", message.from_user, flow_bot).strip()

                if sec_enabled and sec_msg:
                    await message.answer(main_msg, parse_mode="HTML")
                    await message.answer(sec_msg, parse_mode="HTML", reply_markup=kb)
                    lead.setdefault("history", []).append({"role": "assistant", "content": f"{main_msg}\n\n{sec_msg}"})
                else:
                    await message.answer(main_msg, parse_mode="HTML", reply_markup=kb)
                    lead.setdefault("history", []).append({"role": "assistant", "content": main_msg})

                self._advance_stage(lead, "Viu os planos")
                await self.state_saver(state)
                self._schedule_flow_downsell(message.from_user, track="general")
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
                await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
                await asyncio.sleep(delay)
                await message.answer(line)
                lead.setdefault("history", []).append({"role": "assistant", "content": line})

            await self.state_saver(state)
            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor")

        @self.dp.message(F.text)
        async def handle_text(message: types.Message):
            user_text = message.text.strip()
            if user_text.lower() in ("/reset", "/reiniciar", "/limpar"):
                await handle_start(message)
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
                    await self.state_saver(state)
                    return

                delay = self._get_cadence_delay(state)
                await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
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

                # Se houver mídia acionada pelo contexto (suporta URL http e upload Base64)
                media = ai_res.get("media")
                if media and media.get("url"):
                    try:
                        photo_input = self._build_input_file(media["url"], f"{media.get('name', 'media')}.jpg")
                        if photo_input:
                            raw_cap = (media.get("caption") or "").replace("{nome}", lead.get("first_name", "")).strip()
                            await self.bot.send_photo(
                                chat_id=message.chat.id,
                                photo=photo_input,
                                caption=raw_cap or None,
                                has_spoiler=bool(media.get("spoiler", False))
                            )
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

                try:
                    await message.answer(reply_text, reply_markup=markup)
                except Exception as e:
                    logger.warning(f"Fallback envio de mensagem sem markup: {e}")
                    await message.answer(reply_text)
                lead["history"].append({"role": "assistant", "content": reply_text})
                await self.state_saver(state)

            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor")

        @self.dp.message(F.voice | F.audio | F.photo | F.sticker | F.video)
        async def handle_non_text(message: types.Message):
            uid = str(message.from_user.id)
            async with self._get_lead_lock(uid):
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user)
                self._cancel_downsell(uid)

                if message.voice or message.audio:
                    synthetic_msg = "[Visitante enviou um áudio — diga de forma simpática que não consegue ouvir áudio agora e peça para ele escrever por texto]"
                elif message.sticker:
                    synthetic_msg = f"[Visitante enviou uma figurinha {message.sticker.emoji or '😈'}]"
                else:
                    synthetic_msg = "[Visitante enviou uma foto/vídeo]"

                lead.setdefault("history", []).append({"role": "user", "content": synthetic_msg})
                delay = self._get_cadence_delay(state)
                await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
                await asyncio.sleep(delay)

                ai_res = await generate_ai_reply(
                    state,
                    lead["history"],
                    synthetic_msg,
                    lead_name=lead.get("first_name", "")
                )
                reply_text = ai_res.get("reply", "")
                state = await self.state_getter()
                lead = await self._get_or_create_lead(state, message.from_user)
                offer_card = ai_res.get("offer_card")
                is_upsell_ctx = lead.get("stage") in ("Entrega concluída", "Recebeu upsell", "Escolheu upsell", "Checkout de upsell")
                markup = self._build_offers_keyboard(state, offer_card, is_upsell=is_upsell_ctx) if offer_card else None
                if offer_card and not is_upsell_ctx:
                    self._advance_stage(lead, "Viu os planos")

                await message.answer(reply_text, reply_markup=markup)
                lead["history"].append({"role": "assistant", "content": reply_text})
                await self.state_saver(state)
            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor")

        @self.dp.callback_query(F.data.startswith("buy:"))
        async def handle_buy_callback(callback: types.CallbackQuery):
            try:
                await callback.answer("Gerando seu PIX seguro...")
            except Exception:
                pass
            parts = callback.data.split(":")
            oid = parts[1] if len(parts) > 1 else ""
            price = float(parts[2]) if len(parts) > 2 else 19.0
            has_up_flag = len(parts) > 3 and parts[3] == "up"

            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, callback.from_user)
            self._cancel_downsell(str(callback.from_user.id))

            # Identifica o nome da oferta escolhida
            chosen_offer_name = f"Oferta R$ {price:.2f}".replace(".", ",")
            for idx, o in enumerate(state.get("business", {}).get("offers", [])):
                cand_id = str(o.get("id", f"offer_{idx}"))[:20]
                if cand_id == oid:
                    chosen_offer_name = o.get("name", chosen_offer_name)
                    break

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

            learning = state.setdefault("learning", {})
            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) + 1

            pub = state.get("publish", {})
            pushin_token = pub.get("pushin_pay_token", "").strip()
            gw_provider = pub.get("gateway_provider", "syncpay").strip()
            gw_secret = pub.get("gateway_secret", "").strip()
            cents = int(round(price * 100))
            pix_code = f"00020126580014br.gov.bcb.pix0136sharkbot-{cents}5204000053039865405{price:.2f}5802BR5913VIP PRIVADO6009SAO PAULO62070503***6304ABCD"
            tx_id = f"sim_{int(datetime.utcnow().timestamp())}"

            if pushin_token:
                try:
                    client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
                    res = await client.create_pix_charge(
                        value_in_cents=cents,
                        client_name=callback.from_user.first_name or "Cliente VIP"
                    )
                    pix_code = res.get("qr_code") or pix_code
                    tx_id = str(res.get("id") or tx_id)
                except Exception as e:
                    logger.error(f"Erro Gateway PIX ({gw_provider}): {e}")

            if is_upsell:
                self._advance_stage(lead, "Checkout de upsell")
            else:
                self._advance_stage(lead, "Gerou checkout")

            lead["last_tx_id"] = tx_id
            lead["last_tx_is_upsell"] = is_upsell
            self.active_tx_map[tx_id] = callback.from_user.id
            price_fmt = f"{price:.2f}".replace(".", ",")
            label_prefix = "PIX Upsell Gerado" if is_upsell else "PIX Gerado"
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": f"[{label_prefix} — {chosen_offer_name} (R$ {price_fmt})] {pix_code}"
            })
            await self.state_saver(state)

            # IMPORTANTE: callback_data no Telegram tem limite rígido de 64 bytes!
            # checkpix:<uuid 36 chars> = 45 bytes (sempre < 64 bytes).
            safe_cb = f"checkpix:{tx_id[:50]}"
            confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Já paguei / Liberar acesso", callback_data=safe_cb)]
            ])

            target_chat_id = callback.message.chat.id if callback.message else callback.from_user.id
            safe_pix_html = html.escape(pix_code)
            try:
                await self.bot.send_message(
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
                await self.bot.send_message(
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

            if pushin_token and not tx_id.startswith("sim_"):
                asyncio.create_task(self._poll_pix_payment(callback.from_user.id, tx_id, pushin_token, gw_provider, gw_secret))

        @self.dp.callback_query(F.data.startswith("checkpix:"))
        async def handle_check_pix(callback: types.CallbackQuery):
            parts = callback.data.split(":")
            tx_id = parts[1] if len(parts) > 1 else ""
            state = await self.state_getter()
            pub = state.get("publish", {})
            pushin_token = pub.get("pushin_pay_token", "").strip()
            gw_provider = pub.get("gateway_provider", "syncpay").strip()
            gw_secret = pub.get("gateway_secret", "").strip()

            if pushin_token and not tx_id.startswith("sim_"):
                try:
                    client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
                    status_data = await client.check_transaction_status(tx_id)
                    if str(status_data.get("status", "")).lower() == "paid":
                        await callback.answer("Pagamento confirmado! 💜")
                        await self.deliver_purchase_and_upsell(callback.from_user.id)
                        return
                    else:
                        await callback.answer("Ainda aguardando confirmação do banco. Assim que pagar, tente novamente!", show_alert=True)
                        return
                except Exception:
                    pass

            # Modo de demonstração caso não tenha token real configurado
            await callback.answer("Pagamento de teste aprovado!")
            await self.deliver_purchase_and_upsell(callback.from_user.id)

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
            fb = self._get_active_flow_bot(state)
            if not fb:
                return

            active_bumps = [b for b in fb.get("order_bumps", []) if b.get("active", True)]
            if active_bumps:
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
            fb = self._get_active_flow_bot(state)
            if not fb:
                return
            lead = await self._get_or_create_lead(state, callback.from_user)
            accepted_list = lead.setdefault("accepted_bump_indices", [])
            if bump_idx == 0 and decision == "no" and not accepted_list:
                pass
            if decision == "yes" and bump_idx not in accepted_list:
                accepted_list.append(bump_idx)
            await self.state_saver(state)

            active_bumps = [b for b in fb.get("order_bumps", []) if b.get("active", True)]
            next_idx = bump_idx + 1
            if next_idx < len(active_bumps):
                await self._present_flow_order_bump(callback, fb, plan_id, disc_pct, bump_index=next_idx)
            else:
                chosen_bumps = [active_bumps[i] for i in accepted_list if 0 <= i < len(active_bumps)]
                lead["accepted_bump_indices"] = []
                await self._generate_flow_checkout(callback, fb, state, plan_id, disc_pct, accepted_bumps=chosen_bumps)

    async def _present_flow_order_bump(
        self,
        callback: types.CallbackQuery,
        fb: Dict[str, Any],
        plan_id: str,
        disc_pct: int,
        bump_index: int
    ):
        active_bumps = [b for b in fb.get("order_bumps", []) if b.get("active", True)]
        if bump_index >= len(active_bumps):
            return
        bump = active_bumps[bump_index]
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        await self._send_flow_medias(chat_id, bump.get("medias", []))

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

        await self.bot.send_message(chat_id=chat_id, text=offer_msg, parse_mode="HTML", reply_markup=kb)

    async def _generate_flow_checkout(
        self,
        callback: types.CallbackQuery,
        fb: Dict[str, Any],
        state: Dict[str, Any],
        plan_id: str,
        disc_pct: int,
        accepted_bumps: List[Dict[str, Any]]
    ):
        lead = await self._get_or_create_lead(state, callback.from_user)
        self._cancel_downsell(str(callback.from_user.id))

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

        if accepted_bumps:
            bump_titles = " + ".join(b.get("title") or "Adicional" for b in accepted_bumps)
            full_plan_label = f"{plan_name} (+ {bump_titles})"
            deliverables = [b.get("deliverable", "").strip() for b in accepted_bumps if (b.get("deliverable") or "").strip()]
            lead["accepted_bump_deliverable"] = "\n\n".join(deliverables)
        else:
            full_plan_label = plan_name
            lead.pop("accepted_bump_deliverable", None)

        lead["flow_plan_id"] = plan.get("id") if plan else ""
        lead["flow_plan_price"] = total_price
        lead["chosen_plan"] = full_plan_label
        self._advance_stage(lead, "Escolheu um plano")
        self._advance_stage(lead, "Gerou checkout")
        self._update_lead_insights(state, lead, chosen_plan=full_plan_label)

        learning = state.setdefault("learning", {})
        learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) + 1

        pub = state.get("publish", {})
        pushin_token = pub.get("pushin_pay_token", "").strip()
        gw_provider = pub.get("gateway_provider", "syncpay").strip()
        gw_secret = pub.get("gateway_secret", "").strip()
        cents = max(50, int(round(total_price * 100)))
        pix_code = f"00020126580014br.gov.bcb.pix0136neurotbot-{cents}5204000053039865405{total_price:.2f}5802BR5913VIP PRIVADO6009SAO PAULO62070503***6304ABCD"
        tx_id = f"sim_{int(datetime.utcnow().timestamp())}"

        if pushin_token:
            try:
                client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
                res = await client.create_pix_charge(
                    value_in_cents=cents,
                    client_name=callback.from_user.first_name or "Cliente VIP"
                )
                pix_code = res.get("qr_code") or pix_code
                tx_id = str(res.get("id") or tx_id)
            except Exception as e:
                logger.error(f"Erro Gateway PIX Flow ({gw_provider}): {e}")

        lead["last_tx_id"] = tx_id
        lead["last_tx_is_upsell"] = False
        self.active_tx_map[tx_id] = callback.from_user.id

        valor_str = f"R$ {total_price:.2f}".replace(".", ",")
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

        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        await self._send_flow_medias(chat_id, pay_cfg.get("generated_medias", []))

        safe_cb = f"checkpix:{tx_id[:50]}"
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Verificar pagamento", callback_data=safe_cb)]
        ])

        try:
            await self.bot.send_message(chat_id=chat_id, text=rendered_gen, parse_mode="HTML", reply_markup=confirm_kb)
        except Exception:
            await self.bot.send_message(chat_id=chat_id, text=rendered_gen, reply_markup=confirm_kb)

        if pay_cfg.get("secondary_enabled") and (pay_cfg.get("secondary_message") or "").strip():
            sec_txt = self._render_flow_vars(
                pay_cfg["secondary_message"],
                callback.from_user,
                fb,
                plan_name=full_plan_label,
                valor_str=valor_str,
                pix_code_fmt=pix_code_formatted
            )
            await self.bot.send_message(chat_id=chat_id, text=sec_txt, parse_mode="HTML")

        lead.setdefault("history", []).append({
            "role": "assistant",
            "content": f"[PIX Flow — {full_plan_label} ({valor_str})] {pix_code}"
        })
        await self.state_saver(state)

        self._schedule_flow_downsell(callback.from_user, track="pix")
        if pushin_token and not tx_id.startswith("sim_"):
            asyncio.create_task(self._poll_pix_payment(callback.from_user.id, tx_id, pushin_token, gw_provider, gw_secret))

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
        if not tx_id:
            return False
        uid = self.active_tx_map.get(tx_id)
        if not uid and self.state_getter:
            state = await self.state_getter()
            for lead in state.get("memory", {}).get("leads", []):
                if str(lead.get("last_tx_id")) == str(tx_id):
                    uid = int(lead.get("telegram_id"))
                    break
        if uid:
            await self.deliver_purchase_and_upsell(uid)
            return True
        return False

    async def _poll_pix_payment(self, user_id: int, tx_id: str, pushin_token: str, gw_provider: str = "syncpay", gw_secret: str = ""):
        client = PushinPayClient(token=pushin_token, provider=gw_provider, secret=gw_secret)
        for _ in range(90):
            await asyncio.sleep(8)
            try:
                data = await client.check_transaction_status(tx_id)
                if str(data.get("status", "")).lower() == "paid":
                    await self.deliver_purchase_and_upsell(user_id)
                    return
            except Exception:
                pass

    async def deliver_purchase_and_upsell(self, user_id: int):
        state = await self.state_getter()
        lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name="Assinante"))
        self._cancel_downsell(str(user_id))

        is_upsell_payment = bool(lead.get("last_tx_is_upsell")) or lead.get("stage") in (
            "Escolheu upsell",
            "Checkout de upsell",
        )

        if is_upsell_payment:
            if lead.get("stage") == "Comprou upsell":
                return
            self._advance_stage(lead, "Comprou upsell")
            learning = state.setdefault("learning", {})
            learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
            if int(learning.get("unpaid_checkouts", 0)) > 0:
                learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1
            self._update_lead_insights(state, lead)
            await self.bot.send_message(
                chat_id=user_id,
                text="💜 <b>Upsell confirmado!</b>\n\nSeu conteúdo extra exclusivo já está liberado!",
                parse_mode="HTML"
            )
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": "[Upsell pago e entregue com sucesso]"
            })
            await self.state_saver(state)
            return

        if lead.get("stage") in ("Pagamento aprovado", "Entrega concluída", "Compra aprovada", "Recebeu upsell", "Comprou upsell"):
            return

        self._advance_stage(lead, "Pagamento aprovado")
        learning = state.setdefault("learning", {})
        learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
        if int(learning.get("unpaid_checkouts", 0)) > 0:
            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1
        self._update_lead_insights(state, lead)

        flow_bot = self._get_active_flow_bot(state)
        if flow_bot:
            paid_val = float(lead.get("flow_plan_price") or 9.90)
            flow_bot["sales_today"] = int(flow_bot.get("sales_today") or 0) + 1
            flow_bot["revenue_today"] = round(float(flow_bot.get("revenue_today") or 0.0) + paid_val, 2)

            plan_obj = None
            for p in flow_bot.get("plans", []):
                if str(p.get("id")) == str(lead.get("flow_plan_id")):
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

            invite_url = "https://t.me/+link_exclusivo_vip"
            if eff_type == "Link externo" and ext_url:
                invite_url = ext_url
            elif ch_target and self.bot:
                try:
                    invite = await self.bot.create_chat_invite_link(
                        chat_id=ch_target,
                        member_limit=1,
                        expire_date=datetime.utcnow() + timedelta(minutes=15)
                    )
                    invite_url = invite.invite_link
                except Exception as e:
                    logger.warning(f"Erro gerando link de convite no canal {ch_target}: {e}")

            pay_cfg = flow_bot.get("payments", {})
            await self._send_flow_medias(user_id, pay_cfg.get("approved_medias", []))

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
            rendered_app = self._render_flow_vars(
                app_tmpl,
                fake_user,
                flow_bot,
                plan_name=lead.get("chosen_plan") or "Plano VIP",
                valor_str=f"R$ {paid_val:.2f}".replace(".", ",")
            )
            await self.bot.send_message(chat_id=user_id, text=rendered_app, parse_mode="HTML", reply_markup=kb)

            bump_deliv = (lead.get("accepted_bump_deliverable") or "").strip()
            if bump_deliv:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=f"🎁 <b>Seu adicional adquirido:</b>\n\n{bump_deliv}",
                    parse_mode="HTML"
                )

            self._advance_stage(lead, "Entrega concluída")
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": "[Pagamento aprovado Flow — Entrega concluída]"
            })
            await self.state_saver(state)
            return

        await self.state_saver(state)

        pub = state.get("publish", {})
        dest = pub.get("delivery_destination", "").strip()
        delivery_type = pub.get("delivery_type", "Link de acesso")

        if delivery_type == "Mensagem no bot" and dest and not dest.startswith("http"):
            await self.bot.send_message(
                chat_id=user_id,
                text=f"💜 <b>Pagamento confirmado!</b>\n\n{dest}",
                parse_mode="HTML"
            )
        else:
            invite_url = "https://t.me/+link_exclusivo_vip"
            if dest.startswith("http"):
                invite_url = dest
            elif dest and self.bot:
                try:
                    invite = await self.bot.create_chat_invite_link(
                        chat_id=dest,
                        member_limit=1,
                        expire_date=datetime.utcnow() + timedelta(days=1)
                    )
                    invite_url = invite.invite_link
                except Exception as e:
                    logger.warning(f"Não foi possível criar link único no canal {dest}: {e}")

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔥 ENTRAR NO MEU VIP AGORA", url=invite_url)]
            ])

            await self.bot.send_message(
                chat_id=user_id,
                text="💜 <b>Pagamento confirmado!</b>\n\nAqui está o seu acesso exclusivo ao meu VIP privado:",
                parse_mode="HTML",
                reply_markup=kb
            )

        self._advance_stage(lead, "Entrega concluída")
        lead.setdefault("history", []).append({
            "role": "assistant",
            "content": "[Pagamento aprovado — Entrega concluída]"
        })
        await self.state_saver(state)

        # Dispara Upsell se estiver ativo
        upsell = state.get("upsell", {})
        if upsell.get("enabled", False):
            delay_mins = int(upsell.get("delay_minutes", 1))
            await asyncio.sleep(max(5, delay_mins * 60))
            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name="Assinante"))
            for m in upsell.get("medias", []):
                if m.get("url"):
                    try:
                        p_in = self._build_input_file(m["url"], f"{m.get('name', 'upsell')}.jpg")
                        if p_in:
                            await self.bot.send_photo(
                                chat_id=user_id,
                                photo=p_in,
                                caption=m.get("caption") or None,
                                has_spoiler=bool(m.get("spoiler", False))
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
            await self.bot.send_message(
                chat_id=user_id,
                text=upsell_msg,
                reply_markup=self._build_offers_keyboard(state, only_offer=matched_up_offer, is_upsell=True)
            )
            self._advance_stage(lead, "Recebeu upsell")
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": f"[Upsell] {upsell_msg}"
            })
            await self.state_saver(state)


bot_manager = ActiveBotManager()
