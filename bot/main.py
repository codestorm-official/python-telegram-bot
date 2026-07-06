import asyncio
import logging
import re
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = "8946681835:AAF9DyUNGmBZzMtgPRVBcJr_1sbppsWTGIQ"  # <-- ЗАМЕНИ НА СВОЙ ТОКЕН

# Префиксы для RP-команд
RP_PREFIXES = (".", "!", ";")

# База данных
DB_PATH = "rp_bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Инициализация SQLite базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Таблица RP-команд: chat_id -> команды чата
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rp_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            action_template TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, command_name)
        )
    """)

    # Таблица для анонимных сообщений (для ответов)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anon_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            anon_text TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована.")


def get_db():
    """Получить соединение с БД."""
    return sqlite3.connect(DB_PATH)


# ═══════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

def escape_markdown(text: str) -> str:
    """Экранирование спецсимволов MarkdownV2."""
    chars = r"\_*[]()~`>#+-=|{}.!"
    for ch in chars:
        text = text.replace(ch, f"\\{ch}")
    return text


def get_user_mention(user) -> str:
    """Создать кликабельное упоминание пользователя."""
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"
    # Экранируем имя для MarkdownV2
    safe_name = escape_markdown(name)
    return f"[{safe_name}](tg://user?id={user.id})"


def parse_rp_command(text: str, chat_id: int) -> Optional[dict]:
    """
    Парсит текст на предмет RP-команды.
    Возвращает словарь с данными или None.
    """
    text = text.strip()

    # Проверяем префикс
    if not any(text.startswith(prefix) for prefix in RP_PREFIXES):
        return None

    # Убираем префикс
    prefix = text[0]
    rest = text[1:].strip()

    if not rest:
        return None

    # Разделяем на части: команда, цель, реплика
    # Формат: .обнять @username say: привет
    # или: .обнять @username
    # или: .обнять (reply)

    parts = rest.split(None, 1)  # Разделить по первому пробелу
    command_name = parts[0].lower()

    remainder = parts[1] if len(parts) > 1 else ""

    # Ищем say: для реплики
    say_text = None
    if "say:" in remainder:
        idx = remainder.index("say:")
        say_text = remainder[idx + 4:].strip()
        remainder = remainder[:idx].strip()

    # Цель — упоминание или reply
    target_username = None
    target_mention = None

    # Ищем @username в remainder
    mention_match = re.search(r'@(\w+)', remainder)
    if mention_match:
        target_username = mention_match.group(1)

    return {
        "prefix": prefix,
        "command_name": command_name,
        "target_username": target_username,
        "remainder": remainder,
        "say_text": say_text,
        "full_text": text,
    }


def get_rp_template(chat_id: int, command_name: str) -> Optional[str]:
    """Получить шаблон RP-команды из БД."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT action_template FROM rp_commands WHERE chat_id = ? AND command_name = ?",
        (chat_id, command_name),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_all_rp_commands(chat_id: int) -> list:
    """Получить все RP-команды чата."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT command_name, action_template FROM rp_commands WHERE chat_id = ?",
        (chat_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start."""
    await update.message.reply_text(
        "🎭 *RP Chat Bot*\n\n"
        "Я бот для ролевого чата!\n\n"
        "*Основные команды:*\n"
        "• `/rpnew <название> <шаблон>` — создать RP-команду\n"
        "• `/rpdel <название>` — удалить RP-команду\n"
        "• `/rplist` — список RP-команд\n"
        "• `/sendanon <текст>` — анонимное сообщение\n"
        "• `/rpts @username` — заткнуть на 5 минут\n\n"
        "*Использование RP:*\n"
        "`.обнять @username` или `!обнять @username`\n"
        "`.обнять @username say: Привет!`\n"
        "Также работает через ответ на сообщение (reply).",
        parse_mode=ParseMode.MARKDOWN,
    )


async def rpnew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Создать новую RP-команду.
    Формат: /rpnew укусить [пользователь] укусил [пользователя]
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ *Неверный формат!*\n\n"
            "Используй:\n"
            "`/rpnew <название> <шаблон>`\n\n"
            "Пример:\n"
            "`/rpnew укусить [пользователь] укусил [пользователя]`\n\n"
            "Плейсхолдеры:\n"
            "`[пользователь]` — автор команды\n"
            "`[пользователя]` — цель команды",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = update.effective_chat.id
    command_name = context.args[0].lower()
    template = " ".join(context.args[1:])

    # Валидация шаблона
    if "[пользователь]" not in template or "[пользователя]" not in template:
        await update.message.reply_text(
            "❌ Шаблон должен содержать `[пользователь]` и `[пользователя]`!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO rp_commands (chat_id, command_name, action_template, created_by) VALUES (?, ?, ?, ?)",
            (chat_id, command_name, template, update.effective_user.id),
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ RP-команда `.{command_name}` создана!\n\n"
            f"Шаблон: {escape_markdown(template)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"❌ Команда `.{command_name}` уже существует в этом чате!",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        conn.close()


async def rpdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить RP-команду."""
    if not context.args:
        await update.message.reply_text(
            "❌ Укажи название команды:\n`/rpdel <название>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = update.effective_chat.id
    command_name = context.args[0].lower()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM rp_commands WHERE chat_id = ? AND command_name = ?",
        (chat_id, command_name),
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()

    if deleted:
        await update.message.reply_text(
            f"✅ Команда `.{command_name}` удалена!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Команда `.{command_name}` не найдена!",
            parse_mode=ParseMode.MARKDOWN,
        )


async def rplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех RP-команд чата."""
    chat_id = update.effective_chat.id
    commands = get_all_rp_commands(chat_id)

    if not commands:
        await update.message.reply_text(
            "📭 В этом чате пока нет RP-команд.\n"
            "Создай первую: `/rpnew укусить [пользователь] укусил [пользователя]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = "📜 *RP-команды этого чата:*\n\n"
    for name, template in commands:
        text += f"• `.{name}` — {escape_markdown(template)}\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def sendanon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправить анонимное сообщение.
    Формат: /sendanon текст
    или: /sendanon текст + https://t.me/...
    """
    if not context.args:
        await update.message.reply_text(
            "❌ Укажи текст:\n`/sendanon <текст>`\n\n"
            "Для ответа на сообщение добавь URL:\n"
            "`/sendanon текст + https://t.me/c/.../123`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    full_text = " ".join(context.args)
    reply_url = None
    reply_to_message_id = None

    # Ищем URL сообщения для ответа
    if "+" in full_text:
        parts = full_text.rsplit("+", 1)
        message_text = parts[0].strip()
        reply_url = parts[1].strip()
    else:
        message_text = full_text

    # Если указан URL, пытаемся извлечь message_id
    if reply_url:
        # Формат: https://t.me/c/1234567890/123 или https://t.me/username/123
        match = re.search(r'/(\d+)$', reply_url)
        if match:
            reply_to_message_id = int(match.group(1))

    # Удаляем команду пользователя
    try:
        await update.message.delete()
    except Exception:
        pass

    # Отправляем анонимное сообщение
    try:
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🕵️ *Аноним:*\n{escape_markdown(message_text)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_to_message_id=reply_to_message_id,
        )

        # Сохраняем в БД для возможности ответа
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO anon_messages (chat_id, message_id, anon_text) VALUES (?, ?, ?)",
            (update.effective_chat.id, sent.message_id, message_text),
        )
        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"Ошибка отправки анонима: {e}")
        await update.message.reply_text("❌ Не удалось отправить анонимное сообщение.")


async def rpts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    RP-тушение (мут на 5 минут).
    Формат: /rpts @username
    """
    if not context.args:
        await update.message.reply_text(
            "❌ Укажи пользователя:\n`/rpts @username`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = update.effective_chat.id
    bot = context.bot

    # Получаем целевого пользователя
    target_username = context.args[0].replace("@", "")
    
    # Пытаемся найти пользователя в чате
    # (Telegram API не позволяет напрямую резолвить username без контакта,
    # поэтому используем reply или упоминание)
    
    # Проверяем, является ли reply
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        # Пытаемся найти по username в недавних сообщениях
        # Это ограничение API — лучше использовать reply
        await update.message.reply_text(
            "❌ Используй reply на сообщение пользователя или убедись, что бот видел его сообщения.\n"
            "Лучший способ: ответь `/rpts` на сообщение цели.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if target_user.is_bot:
        await update.message.reply_text("❌ Нельзя тушить ботов!")
        return

    # Выдаём мут на 5 минут
    until_date = datetime.now() + timedelta(minutes=5)
    
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=until_date,
        )

        # Удаляем команду
        await update.message.delete()

        # Отправляем RP-сообщение
        author = update.effective_user
        author_mention = get_user_mention(author)
        target_mention = get_user_mention(target_user)

        text = (
            f"🤐 {author_mention} *заткнул\\(а\\) ротик* {target_mention}\n"
            f"⏱ Мут на 5 минут\\."
        )

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as e:
        logger.error(f"Ошибка мута: {e}")
        await update.message.reply_text(
            "❌ Не удалось выдать мут. Проверь права бота (должен быть админом с правами на ограничение)."
        )


# ═══════════════════════════════════════════════════════════════
# ОБРАБОТЧИК RP-СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════

async def handle_rp_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает RP-команды в сообщениях.
    Форматы:
        .обнять @username
        !обнять @username say: Привет
        ;поцеловать (reply)
    """
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    chat_id = message.chat.id

    # Парсим команду
    parsed = parse_rp_command(text, chat_id)
    if not parsed:
        return

    command_name = parsed["command_name"]

    # Получаем шаблон
    template = get_rp_template(chat_id, command_name)
    if not template:
        # Проверяем встроенные команды (опционально)
        return  # Неизвестная команда — игнорируем

    # Определяем автора и цель
    author = message.from_user
    author_mention = get_user_mention(author)

    target_user = None
    target_mention = None

    # 1. Проверяем reply
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        target_mention = get_user_mention(target_user)
    # 2. Проверяем @username в тексте
    elif parsed["target_username"]:
        # Пытаемся найти пользователя по username
        # Ограничение: бот должен видеть сообщения от этого пользователя
        # В реальности лучше использовать reply
        target_mention = f"@{escape_markdown(parsed['target_username'])}"
    # 3. Если нет цели — RP на себя или в пустоту
    else:
        target_mention = "себя"

    # Формируем сообщение
    # Заменяем плейсхолдеры
    rp_text = template.replace("[пользователь]", author_mention)
    rp_text = rp_text.replace("[пользователя]", target_mention)

    # Добавляем реплику, если есть
    if parsed["say_text"]:
        safe_say = escape_markdown(parsed["say_text"])
        rp_text += f"\n\n💬 _\\«{safe_say}\\»_"

    # Удаляем оригинальное сообщение
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение: {e}")

    # Отправляем RP-сообщение
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=rp_text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки RP: {e}")


# ═══════════════════════════════════════════════════════════════
# INLINE-МЕНЮ ДЛЯ УПРАВЛЕНИЯ RP
# ═══════════════════════════════════════════════════════════════

async def rpinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Информация о боте и командах."""
    await update.message.reply_text(
        "🎭 *RP Chat Bot — Справка*\n\n"
        "*Создание команд:*\n"
        "`/rpnew обнять [пользователь] обнял [пользователя]`\n\n"
        "*Использование:*\n"
        "`.обнять @username` — обнять пользователя\n"
        "`!обнять @username say: Как дела?` — с репликой\n"
        "`;обнять` (reply) — обнять через ответ\n\n"
        "*Префиксы:* `.` `!` `;`\n\n"
        "*Другие команды:*\n"
        "`/rpdel <название>` — удалить\n"
        "`/rplist` — список команд\n"
        "`/sendanon <текст>` — аноним\n"
        "`/rpts` (reply) — мут 5 минут\n\n"
        "*Права бота:*\n"
        "• Администратор\n"
        "• Удаление сообщений\n"
        "• Ограничение пользователей",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ
# ═══════════════════════════════════════════════════════════════

def main():
    """Запуск бота."""
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rpnew", rpnew))
    application.add_handler(CommandHandler("rpdel", rpdel))
    application.add_handler(CommandHandler("rplist", rplist))
    application.add_handler(CommandHandler("sendanon", sendanon))
    application.add_handler(CommandHandler("rpts", rpts))
    application.add_handler(CommandHandler("rpinfo", rpinfo))

    # Обработка RP-сообщений (начинаются с . ! ;)
    rp_filter = filters.TEXT & filters.Regex(rf"^[{re.escape(''.join(RP_PREFIXES))}]")
    application.add_handler(MessageHandler(rp_filter, handle_rp_message))

    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
