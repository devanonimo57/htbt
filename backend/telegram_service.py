import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from models import BotConfig, Offer, Lead, Order
from pushin_pay import PushinPayClient

logging.basicConfig(level=logging.INFO)

class TelegramService:
    def __init__(self, bot_token: str, pushin_token: str, vip_channel_id: str):
        self.bot = Bot(token=bot_token)
        self.dp = Dispatcher()
        self.pushin = PushinPayClient(token=pushin_token) if pushin_token else None
        self.vip_channel_id = vip_channel_id
        self.setup_handlers()

    def setup_handlers(self):
        @self.dp.message(CommandStart())
        async def cmd_start(message: types.Message):
            user_name = message.from_user.first_name or "amor"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Acesso Mensal VIP - R$ 29,90", callback_data="buy_offer_1")],
                [InlineKeyboardButton(text="👑 Acesso Vitalício VIP - R$ 69,90", callback_data="buy_offer_2")],
                [InlineKeyboardButton(text="💬 Suporte Humano", url="https://t.me/seusuporte")]
            ])
            await message.answer(
                f"oii {user_name} 💜 chegou agora por aqui?\n\n"
                "Aqui no meu canal exclusivo eu posto todos os dias fotos e vídeos sem censura que não posso postar em nenhum outro lugar... 😈\n\n"
                "Escolha um plano abaixo para liberar seu acesso imediato:",
                reply_markup=keyboard
            )

        @self.dp.callback_query(F.data.startswith("buy_offer_"))
        async def handle_buy_offer(callback: types.CallbackQuery):
            await callback.answer("Gerando Pix exclusivo...")
            offer_id = callback.data.replace("buy_offer_", "")
            
            value_cents = 2990 if offer_id == "1" else 6990
            plan_name = "Acesso Mensal VIP" if offer_id == "1" else "Acesso Vitalício VIP"

            # Gera Pix na Pushin Pay se o token estiver configurado
            pix_copia_cola = "00020126580014br.gov.bcb.pix0136fake-key-for-test-only520400005303986540529.905802BR5913HotBot VIP6009SAO PAULO62070503***6304ABCD"
            if self.pushin:
                try:
                    res = await self.pushin.create_pix_charge(value_in_cents=value_cents)
                    pix_copia_cola = res.get("qr_code", pix_copia_cola)
                except Exception as e:
                    logging.error(f"Erro ao gerar pix na Pushin Pay: {e}")

            await callback.message.answer(
                f"🔥 *{plan_name}* 🔥\n\n"
                f"💰 *Valor:* R$ {value_cents/100:.2f}\n\n"
                "Copie o código Pix abaixo e pague no app do seu banco:\n\n"
                f"`{pix_copia_cola}`\n\n"
                "⚡ *A aprovação é instantânea!* Assim que você pagar, o link de acesso exclusivo ao canal VIP será enviado aqui na hora!",
                parse_mode="Markdown"
            )

    async def generate_vip_invite_link(self) -> str:
        """
        Cria um link de convite único de 1 único uso no Canal VIP
        com validade de expiração controlada.
        """
        try:
            invite = await self.bot.create_chat_invite_link(
                chat_id=self.vip_channel_id,
                member_limit=1,
                expire_date=datetime.utcnow() + timedelta(days=1)
            )
            return invite.invite_link
        except Exception as e:
            logging.error(f"Erro ao gerar invite link no canal VIP: {e}")
            return "https://t.me/seucanalvip"

    async def deliver_vip_access(self, telegram_user_id: int):
        """
        Envia a mensagem comemorativa com o link exclusivo para o cliente
        """
        invite_link = await self.generate_vip_invite_link()
        await self.bot.send_message(
            chat_id=telegram_user_id,
            text=(
                "🎉 *PAGAMENTO CONFIRMADO COM SUCESSO!* 🎉\n\n"
                "Seu acesso ao meu canal VIP privativo já foi liberado!\n\n"
                f"👉 [CLIQUE AQUI PARA ENTRAR NO CANAL VIP]({invite_link})\n\n"
                "⚠️ *Importante:* este link é de uso único e exclusivo seu.",
            ),
            parse_mode="Markdown"
        )
