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
        self.state_getter = None
        self.state_saver = None

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
        if cadence == "Calma":
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
        if only_offer:
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
        support = state.get("publish", {}).get("support_username", "").strip().lstrip("@")
        if support:
            buttons.append([InlineKeyboardButton(text="💬 Falar com Suporte", url=f"https://t.me/{support}")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

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
            self._update_lead_insights(state, lead)

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

            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))

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

            # Se houver mídia acionada pelo contexto (suporta URL http e upload Base64)
            media = ai_res.get("media")
            if media and media.get("url"):
                try:
                    photo_input = self._build_input_file(media["url"], f"{media.get('name', 'media')}.jpg")
                    if photo_input:
                        await self.bot.send_photo(
                            chat_id=message.chat.id,
                            photo=photo_input,
                            caption=media.get("caption") or None,
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
            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))

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
            await self.bot.send_message(
                chat_id=user_id,
                text=upsell_msg,
                reply_markup=self._build_offers_keyboard(state, is_upsell=True)
            )
            self._advance_stage(lead, "Recebeu upsell")
            lead.setdefault("history", []).append({
                "role": "assistant",
                "content": f"[Upsell] {upsell_msg}"
            })
            await self.state_saver(state)


bot_manager = ActiveBotManager()
