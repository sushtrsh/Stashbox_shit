import asyncio
import json
import re

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

with open("config.json", "r", encoding="utf-8") as _f:
    CONFIG = json.load(_f)

BOT_TOKEN = CONFIG["bot_token"]
START_MESSAGE = CONFIG.get("start_message", "Hello! Send a file.")
DB_FILE = CONFIG.get("db_file", "files.json")
MAX_FILE_COMMANDS = CONFIG.get("max_file_commands", 95)
CONFIRM_MESSAGE = CONFIG.get("confirm_message", "✅ Done")  # empty = no message
CONFIRM_SECONDS = CONFIG.get("confirm_seconds", 0)  # 0 = keep the message
DELETE_UPLOAD = CONFIG.get("delete_upload", True)
TEMP_SECONDS = CONFIG.get("temp_seconds", 3)  # how long temporary messages stay visible
START_DELETE_SECONDS = CONFIG.get("start_delete_seconds", 3)  # delay before removing the user's /start


def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_media(msg):
    """Return (type, file_id, name)."""
    mid = msg.message_id
    if msg.document:
        d = msg.document
        return "document", d.file_id, d.file_name or f"document_{mid}"
    if msg.video:
        v = msg.video
        return "video", v.file_id, v.file_name or f"video_{mid}.mp4"
    if msg.audio:
        a = msg.audio
        return "audio", a.file_id, a.file_name or a.title or f"audio_{mid}"
    if msg.voice:
        return "voice", msg.voice.file_id, f"voice_{mid}.ogg"
    if msg.photo:
        return "photo", msg.photo[-1].file_id, f"photo_{mid}.jpg"
    return None, None, None


async def update_menu(bot, chat_id):
    items = load_db().get(str(chat_id), [])
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("list", "Show your files"),
        BotCommand("delete", "Delete a file: /delete f1"),
    ]
    for it in items[-MAX_FILE_COMMANDS:]:
        desc = it["name"][:256].ljust(3, ".")
        commands.append(BotCommand(f"f{it['id']}", desc))
    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id))


async def delete_later(msg, seconds):
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except Exception:
        pass


async def reply_temp(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Delete the user's command, send a reply, then remove the reply after a few seconds."""
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass
    note = await context.bot.send_message(chat_id, text)
    asyncio.create_task(delete_later(note, TEMP_SECONDS))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update_menu(context.bot, chat_id)
    greets = context.bot_data.setdefault("greet", {})
    old = greets.pop(chat_id, None)
    if old:
        try:
            await context.bot.delete_message(chat_id, old)
        except Exception:
            pass
    sent = await context.bot.send_message(chat_id, START_MESSAGE)
    greets[chat_id] = sent.message_id
    asyncio.create_task(delete_later(update.message, START_DELETE_SECONDS))


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items = load_db().get(str(chat_id), [])
    if not items:
        await reply_temp(update, context, "No files yet.")
        return
    lines = [f"/f{it['id']}  {it['name']}" for it in items]
    try:
        await update.message.delete()
    except Exception:
        pass
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 Delete", callback_data="close")]]
    )
    await context.bot.send_message(
        chat_id, "Your files:\n\n" + "\n".join(lines), reply_markup=markup
    )


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    mtype, file_id, name = get_media(msg)
    if not file_id:
        return

    db = load_db()
    items = db.setdefault(str(chat_id), [])
    new_id = max((it["id"] for it in items), default=0) + 1
    items.append({"id": new_id, "name": name, "type": mtype, "file_id": file_id})
    save_db(db)
    await update_menu(context.bot, chat_id)

    greet_id = context.bot_data.setdefault("greet", {}).pop(chat_id, None)
    if greet_id:
        try:
            await context.bot.delete_message(chat_id, greet_id)
        except Exception:
            pass

    if CONFIRM_MESSAGE:
        note = await context.bot.send_message(chat_id, CONFIRM_MESSAGE)
        if CONFIRM_SECONDS > 0:
            asyncio.create_task(delete_later(note, CONFIRM_SECONDS))

    if DELETE_UPLOAD:
        try:
            await msg.delete()
        except Exception:
            pass


async def get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"^/f(\d+)", update.message.text)
    if not m:
        return
    chat_id = update.effective_chat.id
    items = load_db().get(str(chat_id), [])
    it = next((x for x in items if x["id"] == int(m.group(1))), None)
    if not it:
        await reply_temp(update, context, "File not found.")
        return
    senders = {
        "document": context.bot.send_document,
        "video": context.bot.send_video,
        "audio": context.bot.send_audio,
        "voice": context.bot.send_voice,
        "photo": context.bot.send_photo,
    }
    try:
        await update.message.delete()
    except Exception:
        pass
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 Delete", callback_data="close")]]
    )
    try:
        await senders[it["type"]](chat_id, it["file_id"], reply_markup=markup)
    except Exception:
        await context.bot.send_message(chat_id, "Failed to send file.")


async def on_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass


async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0].lower().lstrip("/f") if context.args else ""
    if not arg.isdigit():
        await reply_temp(update, context, "Usage: /delete f1")
        return
    chat_id = update.effective_chat.id
    db = load_db()
    items = db.get(str(chat_id), [])
    it = next((x for x in items if x["id"] == int(arg)), None)
    if not it:
        await reply_temp(update, context, "File not found. Check /list.")
        return
    items.remove(it)
    save_db(db)
    await update_menu(context.bot, chat_id)
    await reply_temp(update, context, f"🗑 Deleted: {it['name']}")


async def refresh_all_menus(app: Application):
    for chat_id in load_db().keys():
        try:
            await update_menu(app.bot, int(chat_id))
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(refresh_all_menus).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("delete", delete_file))
    app.add_handler(MessageHandler(filters.Regex(r"^/f\d+(@\w+)?$"), get_file))
    app.add_handler(CallbackQueryHandler(on_close, pattern="^close$"))
    app.add_handler(MessageHandler(filters.ATTACHMENT & filters.ChatType.PRIVATE, on_file))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
