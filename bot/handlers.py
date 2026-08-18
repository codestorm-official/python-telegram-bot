"""Telegram update handlers."""

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot import cache, db


logger = logging.getLogger(__name__)

# Keys used to read shared connections from Application.bot_data.
DB_KEY = "db"
REDIS_KEY = "redis"
REVIEW_CHAT_ID_KEY = "review_chat_id"
PUBLISH_CHANNEL_ID_KEY = "publish_channel_id"
ANON_GROUP_BUFFER_KEY = "anon_group_buffer"
ANON_GROUP_TASKS_KEY = "anon_group_tasks"

# In-memory fallback for the per-user message counter when Redis is unavailable.
# Process-local and non-persistent, but keeps the feature working for local testing.
_LOCAL_MESSAGE_COUNTS: dict[int, int] = {}

BOT_COMMANDS = (
    ("start", "Exibir o menu principal"),
    ("help", "Exibir a ajuda"),
    ("about", "Exibir informações do bot"),
    ("ping", "Verificar o status do bot"),
)

MENU_HELP = "Ajuda"
MENU_ABOUT = "Sobre"
MENU_PING = "Ping"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Escolha uma opção do menu",
)

HELP_TEXT = """Comandos disponíveis:
/start - Iniciar o bot
/help - Exibir a ajuda
/about - Exibir informações do bot
/ping - Verificar o status do bot

Envie textos, fotos, vídeos, arquivos ou álbuns de mídia e o bot vai encaminhá-los para revisão anônima."""

ANONYMOUS_INPUT_FILTER = (
    (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.VIDEO | filters.Document.ALL
)
ANON_GROUP_GRACE_SECONDS = 1.5


def _submission_keyboard(submission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Aprovar", callback_data=f"anon:approve:{submission_id}"),
                InlineKeyboardButton("Rejeitar", callback_data=f"anon:reject:{submission_id}"),
            ]
        ]
    )


def _submission_label(message: Update) -> str:
    effective_message = message.effective_message
    if effective_message is None:
        return "mensagem"
    if effective_message.media_group_id:
        return "álbum de mídia"
    if effective_message.photo:
        return "foto"
    if effective_message.video:
        return "vídeo"
    if effective_message.document:
        return "arquivo"
    if effective_message.text:
        return "texto"
    return "mensagem"


def _review_text(submission_id: str, label: str, item_count: int) -> str:
    count_label = "item" if item_count == 1 else "itens"
    return (
        f"Envio anônimo #{submission_id[:8]}\n"
        f"Tipo: {label}\n"
        f"Conteúdo: {item_count} {count_label}\n\n"
        "Aprove ou rejeite com os botões abaixo."
    )


async def _copy_submission_content(
    bot,
    source_chat_id: int,
    source_message_ids: list[int],
    destination_chat_id: int | str,
) -> list[int]:
    if len(source_message_ids) == 1:
        copied = await bot.copy_message(
            chat_id=destination_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_ids[0],
            protect_content=True,
        )
        return [copied.message_id]

    copied_messages = await bot.copy_messages(
        chat_id=destination_chat_id,
        from_chat_id=source_chat_id,
        message_ids=source_message_ids,
        protect_content=True,
    )
    return [item.message_id for item in copied_messages]


async def _notify_source(bot, source_chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=source_chat_id, text=text)


async def _send_review_submission(
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    source_message_ids: list[int],
    review_chat_id: int,
    label: str,
    media_group_id: str | None = None,
) -> None:
    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await _notify_source(
            context.bot,
            source_chat_id,
            "As submissões anônimas estão indisponíveis no momento.",
        )
        return

    submission_id = uuid4().hex
    await db.create_anonymous_submission(
        pool,
        submission_id,
        source_chat_id,
        source_message_ids,
        media_group_id,
        review_chat_id,
    )

    try:
        control_message = await context.bot.send_message(
            chat_id=review_chat_id,
            text=_review_text(submission_id, label, len(source_message_ids)),
            reply_markup=_submission_keyboard(submission_id),
        )
    except TelegramError as exc:
        logger.exception("Failed to create review message for submission %s", submission_id)
        await db.mark_anonymous_submission_failed(pool, submission_id, str(exc))
        await _notify_source(
            context.bot,
            source_chat_id,
            "Sua submissão anônima não pôde ser enviada para revisão.",
        )
        return

    await db.attach_review_message(pool, submission_id, control_message.message_id)

    try:
        await _copy_submission_content(context.bot, source_chat_id, source_message_ids, review_chat_id)
    except TelegramError as exc:
        logger.exception("Failed to copy anonymous submission %s", submission_id)
        await db.mark_anonymous_submission_failed(pool, submission_id, str(exc))
        await context.bot.edit_message_text(
            chat_id=review_chat_id,
            message_id=control_message.message_id,
            text=f"Envio anônimo #{submission_id[:8]}\n\nFalha ao copiar o conteúdo para revisão.",
            reply_markup=None,
        )
        await _notify_source(
            context.bot,
            source_chat_id,
            "Sua submissão anônima não pôde ser copiada para revisão.",
        )
        return

    await _notify_source(
        context.bot,
        source_chat_id,
        "Sua submissão anônima foi enviada para revisão.",
    )


async def _finalize_media_group(
    application: Application,
    media_group_id: str,
    generation: int,
) -> None:
    try:
        await asyncio.sleep(ANON_GROUP_GRACE_SECONDS)
    except asyncio.CancelledError:
        return

    buffers = application.bot_data.get(ANON_GROUP_BUFFER_KEY, {})
    tasks = application.bot_data.get(ANON_GROUP_TASKS_KEY, {})
    current_task = tasks.get(media_group_id)
    if current_task is not asyncio.current_task():
        return

    entry = buffers.get(media_group_id)
    if entry is None or entry.get("generation") != generation:
        return

    buffers.pop(media_group_id, None)
    tasks.pop(media_group_id, None)
    if entry is None:
        return

    review_chat_id = application.bot_data.get(REVIEW_CHAT_ID_KEY)
    if review_chat_id is None:
        await _notify_source(
            application.bot,
            entry["source_chat_id"],
            "As submissões anônimas ainda não estão configuradas.",
        )
        return

    await _send_review_submission(
        SimpleNamespace(bot=application.bot, bot_data=application.bot_data),
        entry["source_chat_id"],
        entry["message_ids"],
        review_chat_id,
        "media group",
        media_group_id=media_group_id,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    # Persist the user in PostgreSQL when available (insert on first contact,
    # refresh otherwise). Without a database the bot still greets the user.
    pool = context.bot_data.get(DB_KEY)
    is_new = True
    if pool is not None:
        is_new = await db.upsert_user(pool, user.id, user.username, user.first_name)

    name = user.first_name if user.first_name else "amigo"
    greeting = "Olá" if is_new else "Olá de novo"
    await message.reply_text(
        f"{greeting}, {name}! O bot está em execução.\n\n"
        "Envie um texto, foto, vídeo, arquivo ou álbum de mídia e eu vou encaminhar para revisão anônima.\n"
        "Escolha um botão do menu abaixo ou digite /help para ver os comandos disponíveis.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(HELP_TEXT)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "Este bot recebe envios anônimos e publica o conteúdo aprovado em um canal privado."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    # Demonstrate a short-lived Redis cache: warm within the TTL, cold otherwise.
    # Without Redis we simply reply with a plain pong.
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        await message.reply_text("resposta")
        return

    cached = await cache.get_or_set_ping(client)
    source = "em cache" if cached else "novo"
    await message.reply_text(f"resposta ({source})")


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text == MENU_HELP:
        await help_command(update, context)
    elif text == MENU_ABOUT:
        await about(update, context)
    elif text == MENU_PING:
        await ping(update, context)


async def anonymous_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if message.text is None and not (message.photo or message.video or message.document):
        return

    review_chat_id = context.bot_data.get(REVIEW_CHAT_ID_KEY)
    publish_channel_id = context.bot_data.get(PUBLISH_CHANNEL_ID_KEY)
    if review_chat_id is None or publish_channel_id is None:
        await message.reply_text("As submissões anônimas ainda não estão configuradas.")
        return

    if message.media_group_id:
        buffers = context.bot_data.setdefault(ANON_GROUP_BUFFER_KEY, {})
        tasks = context.bot_data.setdefault(ANON_GROUP_TASKS_KEY, {})
        entry = buffers.get(message.media_group_id)
        if entry is None:
            entry = {
                "source_chat_id": message.chat_id,
                "message_ids": [],
                "generation": 0,
            }
            buffers[message.media_group_id] = entry

        if message.message_id not in entry["message_ids"]:
            entry["message_ids"].append(message.message_id)
        entry["generation"] += 1

        task = tasks.get(message.media_group_id)
        if task is not None:
            task.cancel()
        tasks[message.media_group_id] = asyncio.create_task(
            _finalize_media_group(context.application, message.media_group_id, entry["generation"])
        )
        return

    label = _submission_label(update)
    await _send_review_submission(
        context,
        message.chat_id,
        [message.message_id],
        review_chat_id,
        label,
    )


async def review_submission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return

    review_chat_id = context.bot_data.get(REVIEW_CHAT_ID_KEY)
    publish_channel_id = context.bot_data.get(PUBLISH_CHANNEL_ID_KEY)
    if review_chat_id is None or publish_channel_id is None:
        await query.answer("As submissões anônimas ainda não estão configuradas.", show_alert=True)
        return

    if query.message.chat_id != review_chat_id:
        await query.answer("Esta ação só está disponível no chat de revisão.", show_alert=True)
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Ação inválida.", show_alert=True)
        return

    _, action, submission_id = parts
    if action not in {"approve", "reject"}:
        await query.answer("Ação inválida.", show_alert=True)
        return

    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await query.answer("O banco de dados está indisponível.", show_alert=True)
        return

    submission = await db.get_anonymous_submission(pool, submission_id)
    if submission is None:
        await query.answer("Envio não encontrado.", show_alert=True)
        return

    if submission["status"] != "pending":
        await query.answer("Este envio já foi processado.", show_alert=True)
        return

    source_message_ids = list(submission["source_message_ids"])
    source_chat_id = submission["source_chat_id"]
    review_message_id = submission["review_message_id"]

    if action == "reject":
        await db.mark_anonymous_submission_rejected(pool, submission_id, query.from_user.id)
        if review_message_id is not None:
            await context.bot.edit_message_text(
                chat_id=review_chat_id,
                message_id=review_message_id,
                text=f"Envio anônimo #{submission_id[:8]}\n\nRejeitado pelo moderador.",
                reply_markup=None,
            )
        await _notify_source(
            context.bot,
            source_chat_id,
            "Sua submissão anônima foi rejeitada.",
        )
        await query.answer("Rejeitado.")
        return

    try:
        published_message_ids = await _copy_submission_content(
            context.bot,
            source_chat_id,
            source_message_ids,
            publish_channel_id,
        )
    except TelegramError as exc:
        logger.exception("Failed to publish anonymous submission %s", submission_id)
        await db.mark_anonymous_submission_failed(pool, submission_id, str(exc))
        if review_message_id is not None:
            await context.bot.edit_message_text(
                chat_id=review_chat_id,
                message_id=review_message_id,
                text=f"Envio anônimo #{submission_id[:8]}\n\nFalha ao publicar.",
                reply_markup=None,
            )
        await query.answer("Falha ao publicar.", show_alert=True)
        return

    await db.mark_anonymous_submission_approved(
        pool,
        submission_id,
        query.from_user.id,
        published_message_ids,
    )
    if review_message_id is not None:
        await context.bot.edit_message_text(
            chat_id=review_chat_id,
            message_id=review_message_id,
            text=f"Envio anônimo #{submission_id[:8]}\n\nAprovado e publicado.",
            reply_markup=None,
        )
    await _notify_source(
        context.bot,
        source_chat_id,
        "Sua submissão anônima foi aprovada e publicada.",
    )
    await query.answer("Aprovado.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text("Comando desconhecido. Digite /help para obter ajuda.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Transient polling/network errors (e.g. a brief 409 Conflict during a
    # Railway redeploy when two instances overlap) are self-healing, so log them
    # as warnings without a traceback instead of alarming-looking errors.
    if isinstance(error, (Conflict, NetworkError, TimedOut)):
        logger.warning("Transient Telegram error: %s", error)
        return

    logger.exception("Error while processing update: %s", update, exc_info=error)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Desculpe, ocorreu um erro ao processar sua mensagem."
        )


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(filters.Regex(f"^({MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"), menu_button)
    )
    application.add_handler(MessageHandler(ANONYMOUS_INPUT_FILTER, anonymous_submission))
    application.add_handler(CallbackQueryHandler(review_submission_callback, pattern=r"^anon:(approve|reject):"))
