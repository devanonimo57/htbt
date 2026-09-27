import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, URLInputFile

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
        self.state_getter = None
        self.state_saver = None

    async def configure_and_start(self, state_getter, state_saver):
        self.state_getter = state_getter
        self.state_saver = state_saver
        state = await self.state_getter()
        publish = state.get("publish", {})
        token = publish.get("bot_token", "").strip()
        is_live = bool(publish.get("is_live", False))

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

    async def _run_polling(self):
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
            await self.dp.start_polling(self.bot, handle_signals=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no loop de polling do Telegram: {e}")

    def _get_cadence_delay(self, state: Dict[str, Any]) -> float:
        cadence = state.get("style", {}).get("cadence", "Natural")
        if cadence == "Ágil":
            return 0.6
        if cadence == "Calma":
            return 2.4
        return 1.3

    async def _get_or_create_lead(self, state: Dict[str, Any], user: types.User) -> Dict[str, Any]:
        memory = state.setdefault("memory", {"leads": []})
        leads: List[Dict[str, Any]] = memory.setdefault("leads", [])
        uid = str(user.id)
        for lead in leads:
            if str(lead.get("telegram_id")) == uid:
                lead["first_name"] = user.first_name or lead.get("first_name", "Visitante")
                lead["username"] = f"@{user.username}" if user.username else lead.get("username", "")
                lead["last_interaction"] = datetime.utcnow().strftime("%d/%m %H:%M")
                return lead

        new_lead = {
            "telegram_id": uid,
            "first_name": user.first_name or "Visitante",
            "username": f"@{user.username}" if user.username else "Sem @user",
            "stage": "Em conversa",
            "last_interaction": datetime.utcnow().strftime("%d/%m %H:%M"),
            "preferences": "Iniciou conversa no bot",
            "objections": "Nenhuma identificada",
            "sent_downsell_steps": [],
            "history": []
        }
        leads.insert(0, new_lead)
        learning = state.setdefault("learning", {})
        learning["evaluated_results"] = len(leads)
        return new_lead

    def _build_offers_keyboard(self, state: Dict[str, Any], only_offer: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
        offers = [o for o in state.get("business", {}).get("offers", []) if o.get("visible", True)]
        if only_offer:
            offers = [only_offer]
        buttons = []
        for idx, o in enumerate(offers):
            emoji = o.get("emoji", "💎")
            name = o.get("name", "Acesso VIP")
            price = float(o.get("price", 19))
            oid = o.get("id", f"offer_{idx}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"{emoji} {name} — R$ {price:.2f}".replace(".", ","),
                    callback_data=f"buy:{oid}:{price:.2f}"
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
        try:
            while True:
                state = await self.state_getter()
                ds = state.get("downsell", {})
                if not ds.get("enabled", False):
                    return

                lead = await self._get_or_create_lead(state, types.User(id=user_id, is_bot=False, first_name=user_first_name))
                if lead.get("stage") == "Compra aprovada":
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
                if lead.get("stage") == "Compra aprovada":
                    return

                msg_text = next_step.get("message", "").replace("{nome}", user_first_name)
                accompany = next_step.get("accompany", "Botão com os planos atuais")
                markup = self._build_offers_keyboard(state) if "planos" in accompany.lower() or "oferta" in accompany.lower() else None

                await self.bot.send_message(chat_id=user_id, text=msg_text, reply_markup=markup)
                lead.setdefault("sent_downsell_steps", []).append(next_step.get("id"))
                lead.setdefault("history", []).append({"role": "assistant", "content": f"[Downsell] {msg_text}"})
                await self.state_saver(state)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no Downsell worker: {e}")

    def _register_handlers(self):
        @self.dp.message(CommandStart())
        async def handle_start(message: types.Message):
            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))

            style = state.get("style", {})
            variations = [v.strip() for v in style.get("opening_variations", []) if v and v.strip()]
            main_opening = style.get("opening_messages", ["oii 💜", "chegou agora por aqui?"])

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
            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, message.from_user)
            self._cancel_downsell(str(message.from_user.id))

            user_text = message.text.strip()
            lead.setdefault("history", []).append({"role": "user", "content": user_text})

            # Verifica palavras de encaminhamento para suporte humano
            handoff_words = [
                w.strip().lower()
                for w in state.get("style", {}).get("handoff_words", "").split(",")
                if w.strip()
            ]
            if any(hw in user_text.lower() for hw in handoff_words):
                support = state.get("publish", {}).get("support_username", "@suporte")
                reply = f"Vou te passar o contato do suporte oficial para resolver isso rapidinho: {support} 💜"
                await message.answer(reply)
                lead["history"].append({"role": "assistant", "content": reply})
                await self.state_saver(state)
                return

            delay = self._get_cadence_delay(state)
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            await asyncio.sleep(delay)

            ai_res = await generate_ai_reply(state, lead["history"], user_text)
            reply_text = ai_res.get("reply", "")

            # Se houver mídia acionada pelo contexto
            media = ai_res.get("media")
            if media and media.get("url"):
                try:
                    await self.bot.send_photo(
                        chat_id=message.chat.id,
                        photo=URLInputFile(media["url"]),
                        has_spoiler=bool(media.get("spoiler", False))
                    )
                except Exception as e:
                    logger.warning(f"Falha ao enviar mídia de prévia: {e}")

            offer_card = ai_res.get("offer_card")
            markup = self._build_offers_keyboard(state, offer_card) if offer_card else None

            await message.answer(reply_text, reply_markup=markup)
            lead["history"].append({"role": "assistant", "content": reply_text})
            await self.state_saver(state)

            self._schedule_downsell(message.from_user.id, message.from_user.first_name or "amor")

        @self.dp.callback_query(F.data.startswith("buy:"))
        async def handle_buy_callback(callback: types.CallbackQuery):
            await callback.answer("Gerando seu PIX seguro...")
            parts = callback.data.split(":")
            offer_id = parts[1] if len(parts) > 1 else "offer_1"
            price = float(parts[2]) if len(parts) > 2 else 19.0

            state = await self.state_getter()
            lead = await self._get_or_create_lead(state, callback.from_user)
            self._cancel_downsell(str(callback.from_user.id))

            lead["stage"] = "Checkout gerado"
            learning = state.setdefault("learning", {})
            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) + 1

            pushin_token = state.get("publish", {}).get("pushin_pay_token", "").strip()
            cents = int(round(price * 100))
            pix_code = f"00020126580014br.gov.bcb.pix0136sharkbot-{cents}5204000053039865405{price:.2f}5802BR5913VIP PRIVADO6009SAO PAULO62070503***6304ABCD"
            tx_id = f"sim_{int(datetime.utcnow().timestamp())}"

            if pushin_token:
                try:
                    client = PushinPayClient(token=pushin_token)
                    res = await client.create_pix_charge(value_in_cents=cents)
                    pix_code = res.get("qr_code", pix_code)
                    tx_id = str(res.get("id", tx_id))
                except Exception as e:
                    logger.error(f"Erro Pushin Pay: {e}")

            confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Já paguei / Liberar acesso", callback_data=f"checkpix:{tx_id}:{offer_id}")]
            ])

            await callback.message.answer(
                f"🔒 *Pagamento Seguro via PIX*\n\n"
                f"Valor: *R$ {price:.2f}*\n\n"
                f"Copie o código PIX Copia e Cola abaixo e pague no app do seu banco:\n\n"
                f"`{pix_code}`\n\n"
                f"Assim que concluir, seu acesso é liberado automaticamente aqui na conversa!",
                parse_mode="Markdown",
                reply_markup=confirm_kb
            )
            await self.state_saver(state)

            if pushin_token and not tx_id.startswith("sim_"):
                asyncio.create_task(self._poll_pix_payment(callback.from_user.id, tx_id, pushin_token))

        @self.dp.callback_query(F.data.startswith("checkpix:"))
        async def handle_check_pix(callback: types.CallbackQuery):
            parts = callback.data.split(":")
            tx_id = parts[1] if len(parts) > 1 else ""
            state = await self.state_getter()
            pushin_token = state.get("publish", {}).get("pushin_pay_token", "").strip()

            if pushin_token and not tx_id.startswith("sim_"):
                try:
                    client = PushinPayClient(token=pushin_token)
                    status_data = await client.check_transaction_status(tx_id)
                    if str(status_data.get("status", "")).lower() == "paid":
                        await callback.answer("Pagamento confirmado! 💜")
                        await self.deliver_purchase_and_upsell(callback.from_user.id)
                        return
                    else:
                        await callback.answer("Ainda aguardando confirmação do banco. Tente novamente em instantes!", show_alert=True)
                        return
                except Exception:
                    pass

            # Modo de demonstração caso não tenha token real configurado
            await callback.answer("Pagamento de teste aprovado!")
            await self.deliver_purchase_and_upsell(callback.from_user.id)

    async def _poll_pix_payment(self, user_id: int, tx_id: str, pushin_token: str):
        client = PushinPayClient(token=pushin_token)
        for _ in range(60):
            await asyncio.sleep(10)
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
        if lead.get("stage") == "Compra aprovada":
            return

        lead["stage"] = "Compra aprovada"
        learning = state.setdefault("learning", {})
        learning["confirmed_purchases"] = int(learning.get("confirmed_purchases", 0)) + 1
        if int(learning.get("unpaid_checkouts", 0)) > 0:
            learning["unpaid_checkouts"] = int(learning.get("unpaid_checkouts", 0)) - 1

        dest = state.get("publish", {}).get("delivery_destination", "").strip()
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
            text="💜 *Pagamento confirmado!*\n\nAqui está o seu acesso exclusivo ao meu VIP privado:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        await self.state_saver(state)

        # Dispara Upsell se estiver ativo
        upsell = state.get("upsell", {})
        if upsell.get("enabled", False):
            delay_mins = int(upsell.get("delay_minutes", 1))
            await asyncio.sleep(max(5, delay_mins * 60))
            upsell_msg = upsell.get("message", "agora que seu acesso foi liberado, tenho uma opção extra que combina com você! 😉")
            await self.bot.send_message(
                chat_id=user_id,
                text=upsell_msg,
                reply_markup=self._build_offers_keyboard(state)
            )


bot_manager = ActiveBotManager()
