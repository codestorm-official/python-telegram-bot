import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.getenv("TOKEN")
CHANNEL = "@shrimpege"
YOUR_TG = "https://t.me/Pablos777n"


def keyboard_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Получить шпоры", url="https://t.me/shrimpege")],
        [InlineKeyboardButton("Я подписался", callback_data="check")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добрый день, это shrimpEGE 👋\n\n"
        "Чтобы получить шпоры на 80+ баллов, подпишитесь на канал 👇",
        reply_markup=keyboard_main()
    )


async def check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    try:
        member = await context.bot.get_chat_member(CHANNEL, user_id)
        is_sub = member.status in ["member", "administrator", "creator"]
    except:
        is_sub = False

    if not is_sub:
        await query.edit_message_text(
            "ой похоже кто-то не подписался 😏",
            reply_markup=keyboard_main()
        )
    else:
        await query.edit_message_text(
            "Хорош 😎\nНапиши мне, и я дам тебе шпоры",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Написать мне", url=YOUR_TG)]
            ])
        )


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_sub, pattern="check"))

    app.run_polling()


if __name__ == "__main__":
    main()
