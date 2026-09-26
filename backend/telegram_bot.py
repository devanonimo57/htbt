import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO)

# Exemplo de fluxo básico de atendimento integrado
def get_plans_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Acesso Mensal VIP - R$ 29,90", callback_data="buy_plan_1")],
        [InlineKeyboardButton(text="👑 Acesso Vitalício VIP - R$ 69,90", callback_data="buy_plan_2")],
        [InlineKeyboardButton(text="💬 Falar com Suporte", url="https://t.me/seusuporte")]
    ])
    return keyboard

async def start_telegram_bot(token: str):
    """Inicializa o bot do Telegram com escuta ativa"""
    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        user_name = message.from_user.first_name or "meu bem"
        await message.answer(
            f"oii {user_name} 💜 chegou agora por aqui?",
            reply_markup=get_plans_keyboard()
        )

    @dp.callback_query(F.data.startswith("buy_plan_"))
    async def handle_buy(callback: types.CallbackQuery):
        await callback.answer("Gerando seu Pix...")
        # Aqui é chamado o PushinPayClient.create_pix_charge()
        # Retorna o QR Code e o Copia e Cola instantâneo
        await callback.message.answer(
            "✨ *Aqui está o seu Pix Copia e Cola:* ✨\n\n"
            "`00020126580014br.gov.bcb.pix0136123e4567-e89b-12d3-a456-426614174000520400005303986540529.905802BR5913HotBot VIP6009SAO PAULO62070503***6304ABCD`\n\n"
            "⏳ *Assim que você pagar, o link de acesso ao canal VIP será enviado aqui na hora!*",
            parse_mode="Markdown"
        )

    logging.info("Bot do Telegram iniciado com sucesso!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Para testar: substitua pelo seu token do BotFather
    # asyncio.run(start_telegram_bot("SEU_TOKEN_AQUI"))
    pass
