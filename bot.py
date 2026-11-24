import asyncio
import html
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
START_MESSAGE = CONFIG.get("start_message", "Hello! 👋")
SEND_FILE_MESSAGE = CONFIG.get("send_file_message", "Send me a file 📎")
NOTES_MESSAGE = CONFIG.get("notes_message", "Type your note and send it ✍️")
DB_FILE = CONFIG.get("db_file", "files.json")
FILE_TITLE_MESSAGE = CONFIG.get("file_title_message", "Send a title for this file ✍️")
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


ICONS = {
    "note": "📝",
    "document": "📄",
    "video": "🎬",
    "audio": "🎵",
    "voice": "🎤",
    "photo": "🖼",
}


def make_title(text, limit=60):
    """First non-empty line of a text, shortened. Used as the note/file name."""
    first = next((l.strip() for l in text.splitlines() if l.strip()), "Untitled")
    return first if len(first) <= limit else first[: limit - 1] + "…"


def render_note(text):
    """Split a note on blank lines; each block becomes a tap-to-copy block.

    Lines written together ("one\ntwo") stay in one block and are copied
    together. Lines separated by a blank line are separate blocks.
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for b in blocks:
        b = b.strip("\n")
        if not b.strip():
            continue
        e = html.escape(b, quote=False)
        out.append(f"<pre>{e}</pre>" if "\n" in b else f"<code>{e}</code>")
    return "\n\n".join(out)


def main_menu_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Send file", callback_data="sendfile")],
            [InlineKeyboardButton("📝 Notes", callback_data="notes")],
        ]
    )


def back_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
    )


def is_note(it):
    return it["type"] == "note"


def prefix(it):
    """Notes are /n<id>, files are /f<id>; each kind has its own numbering."""
    return "n" if is_note(it) else "f"


def next_id(items, note):
    return max((it["id"] for it in items if is_note(it) == note), default=0) + 1


def find_item(items, ref):
    note, item_id = ref
    return next(
        (x for x in items if is_note(x) == note and x["id"] == item_id), None
    )


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
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("addfile", "Add a file"),
        BotCommand("addnotes", "Add a note"),
        BotCommand("list", "Show your files"),
        BotCommand("notes", "Show your notes"),
        BotCommand("search", "Search: /search keyword"),
    ]
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
    await finalize_pending(context, chat_id, confirm=False)
    greets = context.bot_data.setdefault("greet", {})
    old = greets.pop(chat_id, None)
    if old:
        try:
            await context.bot.delete_message(chat_id, old)
        except Exception:
            pass
    sent = await context.bot.send_message(
        chat_id, START_MESSAGE, reply_markup=main_menu_markup()
    )
    greets[chat_id] = sent.message_id
    asyncio.create_task(delete_later(update.message, START_DELETE_SECONDS))


async def clear_lists(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Delete the last list/search message(s) sent to this chat."""
    ids = context.bot_data.setdefault("lists", {}).pop(chat_id, [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def send_list(update: Update, context: ContextTypes.DEFAULT_TYPE, header: str, items):
    chat_id = update.effective_chat.id
    lines = [f"{ICONS.get(it['type'], '📄')} /{prefix(it)}{it['id']}  {it['name']}" for it in items]
    chunks, cur = [], header + "\n\n"
    for line in lines:
        if len(cur) + len(line) + 1 > 4000:  # Telegram limit is 4096 chars
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    try:
        await update.message.delete()
    except Exception:
        pass
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Close", callback_data="close")]]
    )
    await clear_lists(context, chat_id)  # replace any older list
    sent_ids = []
    for c in chunks:
        sent = await context.bot.send_message(chat_id, c.rstrip(), reply_markup=markup)
        sent_ids.append(sent.message_id)
    context.bot_data.setdefault("lists", {})[chat_id] = sent_ids


async def show_add_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Open the send-file / add-note screen directly, without the hello menu."""
    chat_id = update.effective_chat.id
    await finalize_pending(context, chat_id, confirm=False)
    greets = context.bot_data.setdefault("greet", {})
    old = greets.pop(chat_id, None)
    if old:
        try:
            await context.bot.delete_message(chat_id, old)
        except Exception:
            pass
    try:
        await update.message.delete()
    except Exception:
        pass
    sent = await context.bot.send_message(chat_id, text, reply_markup=back_markup())
    greets[chat_id] = sent.message_id  # removed after saving; Back returns to the hello menu


async def add_file_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_add_screen(update, context, SEND_FILE_MESSAGE)


async def add_notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_add_screen(update, context, NOTES_MESSAGE)


PAGE_SIZE = 8


def get_items(chat_id, note):
    """Notes or files of a chat in saved order: number 1 on top, newest at the bottom."""
    items = load_db().get(str(chat_id), [])
    return [it for it in items if is_note(it) == note]


def items_keyboard(items, page, note):
    kind, page_key = ("note", "notes_p") if note else ("file", "files_p")
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for it in chunk:
        label = it["name"][:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"{kind}:{it['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{page_key}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{page_key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"del:list:{'n' if note else 'f'}:0"),
            InlineKeyboardButton("✖️ Close", callback_data="close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def send_item(context: ContextTypes.DEFAULT_TYPE, chat_id: int, it):
    """Send a note (tap-to-copy blocks) or a stored file, with a Close button."""
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Close", callback_data="close")]]
    )
    senders = {
        "document": context.bot.send_document,
        "video": context.bot.send_video,
        "audio": context.bot.send_audio,
        "voice": context.bot.send_voice,
        "photo": context.bot.send_photo,
    }
    try:
        if is_note(it):
            await context.bot.send_message(
                chat_id, render_note(it["text"]), parse_mode="HTML", reply_markup=markup
            )
        else:
            await senders[it["type"]](chat_id, it["file_id"], reply_markup=markup)
    except Exception:
        await context.bot.send_message(chat_id, "Failed to send file.")


async def show_item_list(update: Update, context: ContextTypes.DEFAULT_TYPE, note: bool):
    """Files or notes as tappable buttons."""
    chat_id = update.effective_chat.id
    items = get_items(chat_id, note)
    if not items:
        await reply_temp(update, context, "No notes yet." if note else "No files yet.")
        return
    try:
        await update.message.delete()
    except Exception:
        pass
    await clear_lists(context, chat_id)
    sent = await context.bot.send_message(
        chat_id,
        "Your notes:" if note else "Your files:",
        reply_markup=items_keyboard(items, 0, note),
    )
    context.bot_data.setdefault("lists", {})[chat_id] = [sent.message_id]


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_item_list(update, context, False)


async def list_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_item_list(update, context, True)


async def on_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key, page = q.data.split(":")
    note = key == "notes_p"
    items = get_items(q.message.chat_id, note)
    if not items:
        await q.message.delete()
        return
    try:
        await q.message.edit_reply_markup(
            reply_markup=items_keyboard(items, int(page), note)
        )
    except Exception:
        pass


async def on_item_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    kind, item_id = q.data.split(":")
    it = find_item(load_db().get(str(chat_id), []), (kind == "note", int(item_id)))
    if not it:
        await q.answer("Not found.")
        return
    await q.answer()
    context.bot_data.setdefault("lists", {}).pop(chat_id, None)
    try:
        await q.message.delete()  # the list disappears once an item is opened
    except Exception:
        pass
    await send_item(context, chat_id, it)


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args).strip().lower()
    if not q:
        await reply_temp(update, context, "Usage: /search keyword")
        return
    items = load_db().get(str(update.effective_chat.id), [])
    hits = [
        it for it in items
        if q in it["name"].lower() or q in it.get("text", "").lower()
    ]
    if not hits:
        await reply_temp(update, context, "No match.")
        return
    await send_list(update, context, f"Results for “{q}”:", hits)


async def finish_save(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_msg_ids=()):
    """Shared steps after saving a file or a note."""
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
        for mid in user_msg_ids:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass


def pending_store(context: ContextTypes.DEFAULT_TYPE):
    """Files that were uploaded and are waiting for a title, per chat."""
    return context.bot_data.setdefault("pending", {})


async def finalize_pending(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    title=None,
    user_msg_ids=(),
    confirm=True,
):
    """Save the waiting file with the given title (or its original name)."""
    p = pending_store(context).pop(chat_id, None)
    if not p:
        return False
    db = load_db()
    items = db.setdefault(str(chat_id), [])
    items.append(
        {
            "id": next_id(items, False),
            "name": title or p["name"],
            "type": p["type"],
            "file_id": p["file_id"],
        }
    )
    save_db(db)
    if p.get("prompt_id"):
        try:
            await context.bot.delete_message(chat_id, p["prompt_id"])
        except Exception:
            pass
    ids = [p["file_msg_id"], *user_msg_ids]
    if confirm:
        await finish_save(context, chat_id, ids)
    elif DELETE_UPLOAD:
        for mid in ids:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass
    return True


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    mtype, file_id, name = get_media(msg)
    if not file_id:
        return
    # an earlier file still waiting for a title keeps its original name
    await finalize_pending(context, chat_id, confirm=False)
    pending_store(context)[chat_id] = {
        "type": mtype,
        "file_id": file_id,
        "name": name,
        "file_msg_id": msg.message_id,
    }
    if msg.caption:  # a caption is used as the title, no question asked
        await finalize_pending(context, chat_id, title=make_title(msg.caption))
        return

    greet_id = context.bot_data.setdefault("greet", {}).pop(chat_id, None)
    if greet_id:
        try:
            await context.bot.delete_message(chat_id, greet_id)
        except Exception:
            pass
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Use original name", callback_data="keepname")]]
    )
    prompt = await context.bot.send_message(chat_id, FILE_TITLE_MESSAGE, reply_markup=markup)
    pending_store(context)[chat_id]["prompt_id"] = prompt.message_id


async def on_keepname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await finalize_pending(context, q.message.chat_id):
        try:
            await q.message.delete()  # nothing waiting anymore (e.g. bot restarted)
        except Exception:
            pass


async def on_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text: the title of a file that is waiting, otherwise a new note."""
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    mid = update.message.message_id
    if chat_id in pending_store(context):
        await finalize_pending(
            context, chat_id, title=make_title(text), user_msg_ids=[mid]
        )
        return
    db = load_db()
    items = db.setdefault(str(chat_id), [])
    new_id = next_id(items, True)
    items.append({"id": new_id, "name": make_title(text), "type": "note", "text": text})
    save_db(db)
    await finish_save(context, chat_id, [mid])


async def get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"^/([fn])(\d+)", update.message.text)
    if not m:
        return
    chat_id = update.effective_chat.id
    items = load_db().get(str(chat_id), [])
    it = find_item(items, (m.group(1) == "n", int(m.group(2))))
    if not it:
        await reply_temp(
            update, context, "Note not found." if m.group(1) == "n" else "File not found."
        )
        return
    await clear_lists(context, chat_id)  # the list disappears once an item is opened
    try:
        await update.message.delete()
    except Exception:
        pass
    await send_item(context, chat_id, it)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Swap the /start message in place: main menu <-> send file / notes screens."""
    q = update.callback_query
    await q.answer()
    if q.data == "sendfile":
        text, markup = SEND_FILE_MESSAGE, back_markup()
    elif q.data == "notes":
        text, markup = NOTES_MESSAGE, back_markup()
    else:
        text, markup = START_MESSAGE, main_menu_markup()
    try:
        await q.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


async def on_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass


def delete_keyboard(items, page, note):
    k = "n" if note else "f"
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for it in chunk:
        label = f"🗑 {it['name']}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"del:ask:{k}:{it['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"del:list:{k}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"del:list:{k}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"del:back:{k}:{page}"),
            InlineKeyboardButton("✖️ Close", callback_data="close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_delete_list(q, chat_id, note, page):
    """Edit the message in place to show the delete list."""
    items = get_items(chat_id, note)
    k = "n" if note else "f"
    if not items:
        text = "No notes left." if note else "No files left."
        markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("⬅️ Back", callback_data=f"del:back:{k}:0"),
                InlineKeyboardButton("✖️ Close", callback_data="close"),
            ]]
        )
    else:
        text = "Delete which note?" if note else "Delete which file?"
        markup = delete_keyboard(items, page, note)
    try:
        await q.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


async def on_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete flow, opened from the 🗑 Delete button of /list and /notes:
    list -> confirm -> back to the list, all in one message edited in place."""
    q = update.callback_query
    chat_id = q.message.chat_id
    parts = q.data.split(":")
    action = parts[1]
    note = parts[2] == "n"

    if action == "list":
        await q.answer()
        await show_delete_list(q, chat_id, note, int(parts[3]))
        return

    if action == "back":
        await q.answer()
        items = get_items(chat_id, note)
        if items:
            text = "Your notes:" if note else "Your files:"
            markup = items_keyboard(items, int(parts[3]), note)
        else:
            text = "No notes yet." if note else "No files yet."
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✖️ Close", callback_data="close")]]
            )
        try:
            await q.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        return

    db = load_db()
    items = db.get(str(chat_id), [])
    it = find_item(items, (note, int(parts[3])))
    if not it:
        await q.answer("Not found.")
        await show_delete_list(q, chat_id, note, 0)
        return
    page = get_items(chat_id, note).index(it) // PAGE_SIZE

    if action == "ask":
        await q.answer()
        markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("🗑 Yes, delete", callback_data=f"del:do:{parts[2]}:{it['id']}"),
                InlineKeyboardButton("Cancel", callback_data=f"del:list:{parts[2]}:{page}"),
            ]]
        )
        try:
            await q.message.edit_text(f"Delete “{it['name']}”?", reply_markup=markup)
        except Exception:
            pass
    elif action == "do":
        items.remove(it)
        save_db(db)
        await q.answer("🗑 Deleted")
        await show_delete_list(q, chat_id, note, page)


async def refresh_all_menus(app: Application):
    for chat_id in load_db().keys():
        try:
            await update_menu(app.bot, int(chat_id))
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(refresh_all_menus).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addfile", add_file_cmd))
    app.add_handler(CommandHandler("addnotes", add_notes_cmd))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("notes", list_notes))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(MessageHandler(filters.Regex(r"^/[fn]\d+(@\w+)?$"), get_file))
    app.add_handler(CallbackQueryHandler(on_close, pattern="^close$"))
    app.add_handler(CallbackQueryHandler(on_item_open, pattern=r"^(note|file):\d+$"))
    app.add_handler(CallbackQueryHandler(on_page, pattern=r"^(notes|files)_p:\d+$"))
    app.add_handler(CallbackQueryHandler(on_keepname, pattern="^keepname$"))
    app.add_handler(CallbackQueryHandler(on_delete_cb, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^(menu|sendfile|notes)$"))
    app.add_handler(MessageHandler(filters.ATTACHMENT & filters.ChatType.PRIVATE, on_file))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_note
        )
    )
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
