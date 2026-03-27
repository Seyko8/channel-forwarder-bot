#!/usr/bin/env python3
"""
Telegram Forward Bot – leitet Videos & Fotos aus Gruppen in einen Kanal weiter.
Konfiguration über Bot-Menü. Duplikat-Erkennung via SQLite.
"""

import json
import os
import logging
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Timezone ─────────────────────────────────────────────
DE_TZ = timezone(timedelta(hours=2))  # CEST (Sommerzeit)

# ── Paths ────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duplicates.db")

# ── SQLite Setup ─────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS forwarded_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_unique_id TEXT UNIQUE NOT NULL,
            file_type TEXT NOT NULL,
            source_chat_id INTEGER,
            source_user TEXT,
            forwarded_at TEXT NOT NULL,
            original_message_id INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_unique_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            source_chat_id INTEGER,
            blocked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def is_duplicate(file_unique_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM forwarded_media WHERE file_unique_id = ?", (file_unique_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def record_media(file_unique_id: str, file_type: str, source_chat_id: int, source_user: str, message_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now(DE_TZ).isoformat()
    c.execute(
        "INSERT OR IGNORE INTO forwarded_media (file_unique_id, file_type, source_chat_id, source_user, forwarded_at, original_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (file_unique_id, file_type, source_chat_id, source_user, now, message_id),
    )
    conn.commit()
    conn.close()

def record_duplicate(file_unique_id: str, file_type: str, source_chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now(DE_TZ).isoformat()
    c.execute(
        "INSERT INTO duplicate_log (file_unique_id, file_type, source_chat_id, blocked_at) VALUES (?, ?, ?, ?)",
        (file_unique_id, file_type, source_chat_id, now),
    )
    conn.commit()
    conn.close()

def get_stats() -> dict:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_media")
    total_forwarded = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM duplicate_log")
    total_duplicates = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_media WHERE file_type = 'video'")
    videos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_media WHERE file_type = 'photo'")
    photos = c.fetchone()[0]
    conn.close()
    return {
        "total_forwarded": total_forwarded,
        "total_duplicates": total_duplicates,
        "videos": videos,
        "photos": photos,
    }


# ── Config ───────────────────────────────────────────────
def load_config() -> dict:
    default = {"admin_ids": [], "source_chats": [], "target_channels": [], "caption_template": "📹 Von: {user} | 📅 {date} | 💬 Quelle: {source}"}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            # Migration: alte target_channel -> target_channels
            if "target_channel" in data:
                old = data.pop("target_channel")
                if old and old not in data["target_channels"]:
                    data["target_channels"].append(old)
            return data
    return default

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


config = load_config()


def is_admin(user_id: int) -> bool:
    return user_id in config["admin_ids"]


# ── /start ───────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Nur in Privatchats antworten
    if update.message.chat.type != "private":
        return
    user = update.effective_user
    if not config["admin_ids"]:
        config["admin_ids"].append(user.id)
        save_config(config)
        await update.message.reply_text(
            f"✅ Du ({user.first_name}) bist jetzt Admin!\nNutze /menu für Einstellungen."
        )
    elif is_admin(user.id):
        await update.message.reply_text("👋 Willkommen zurück! Nutze /menu für Einstellungen.")
    else:
        await update.message.reply_text("⛔ Kein Zugriff.")


# ── /menu ────────────────────────────────────────────────
async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Nur in Privatchats antworten
    if update.message.chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Kein Zugriff.")

    keyboard = [
        [InlineKeyboardButton("📡 Quell-Gruppen anzeigen", callback_data="show_sources")],
        [InlineKeyboardButton("➕ Quell-Gruppen hinzufügen", callback_data="add_source")],
        [InlineKeyboardButton("➖ Quell-Gruppe entfernen", callback_data="remove_source")],
        [InlineKeyboardButton("🎯 Ziel-Kanäle anzeigen", callback_data="show_target")],
        [InlineKeyboardButton("🎯 Ziel-Kanäle hinzufügen", callback_data="set_target")],
        [InlineKeyboardButton("➖ Ziel-Kanal entfernen", callback_data="remove_target")],
        [InlineKeyboardButton("📊 Statistiken", callback_data="show_stats")],
        [InlineKeyboardButton("✏️ Caption-Template", callback_data="show_caption")],
    ]
    await update.message.reply_text("⚙️ *Bot-Menü*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ── Callback-Handler ────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return await query.edit_message_text("⛔ Kein Zugriff.")

    data = query.data

    if data == "show_sources":
        sources = config.get("source_chats", [])
        if sources:
            lines = []
            for s in sources:
                try:
                    chat = await ctx.bot.get_chat(s)
                    name = chat.title or "Unbekannt"
                except Exception:
                    name = "⚠️ Kein Zugriff"
                lines.append(f"• `{s}` — {name}")
            text = "📡 *Quell-Gruppen:*\n" + "\n".join(lines)
        else:
            text = "Keine Quell-Gruppen konfiguriert."
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "add_source":
        ctx.user_data["awaiting"] = "add_source"
        await query.edit_message_text("Sende mir die Chat-IDs der Gruppen.\nMehrere IDs mit Komma oder Zeilenumbruch trennen.\n\nBeispiel: `-1001234567890, -1009876543210`", parse_mode="Markdown")

    elif data == "remove_source":
        sources = config.get("source_chats", [])
        if not sources:
            return await query.edit_message_text("Keine Quell-Gruppen vorhanden.")
        keyboard = []
        for s in sources:
            try:
                chat = await ctx.bot.get_chat(s)
                name = chat.title or str(s)
            except Exception:
                name = str(s)
            keyboard.append([InlineKeyboardButton(f"❌ {s} — {name}", callback_data=f"del_source_{s}")])
        await query.edit_message_text("Welche Gruppe entfernen?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_source_"):
        chat_id = int(data.replace("del_source_", ""))
        if chat_id in config["source_chats"]:
            config["source_chats"].remove(chat_id)
            save_config(config)
        await query.edit_message_text(f"✅ Gruppe `{chat_id}` entfernt.", parse_mode="Markdown")

    elif data == "show_target":
        targets = config.get("target_channels", [])
        if targets:
            lines = []
            for t in targets:
                try:
                    chat = await ctx.bot.get_chat(t)
                    name = chat.title or "Unbekannt"
                except Exception:
                    name = "⚠️ Kein Zugriff"
                lines.append(f"• `{t}` — {name}")
            text = "🎯 *Ziel-Kanäle:*\n" + "\n".join(lines)
        else:
            text = "Keine Ziel-Kanäle gesetzt."
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "set_target":
        ctx.user_data["awaiting"] = "set_target"
        await query.edit_message_text("Sende mir die Chat-IDs der Ziel-Kanäle.\nMehrere IDs mit Komma oder Zeilenumbruch trennen.\n\nBeispiel: `-1001234567890, -1009876543210`", parse_mode="Markdown")

    elif data == "remove_target":
        targets = config.get("target_channels", [])
        if not targets:
            return await query.edit_message_text("Keine Ziel-Kanäle vorhanden.")
        keyboard = []
        for t in targets:
            try:
                chat = await ctx.bot.get_chat(t)
                name = chat.title or str(t)
            except Exception:
                name = str(t)
            keyboard.append([InlineKeyboardButton(f"❌ {t} — {name}", callback_data=f"del_target_{t}")])
        await query.edit_message_text("Welchen Ziel-Kanal entfernen?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_target_"):
        chat_id = int(data.replace("del_target_", ""))
        if chat_id in config.get("target_channels", []):
            config["target_channels"].remove(chat_id)
            save_config(config)
        await query.edit_message_text(f"✅ Ziel-Kanal `{chat_id}` entfernt.", parse_mode="Markdown")

    elif data == "show_stats":
        stats = get_stats()
        text = (
            "📊 *Statistiken:*\n\n"
            f"✅ Weitergeleitet: {stats['total_forwarded']}\n"
            f"  📹 Videos: {stats['videos']}\n"
            f"  📷 Fotos: {stats['photos']}\n\n"
            f"🚫 Duplikate blockiert: {stats['total_duplicates']}"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "show_caption":
        tpl = config.get("caption_template", "")
        await query.edit_message_text(
            f"✏️ *Aktuelles Caption-Template:*\n`{tpl}`\n\nVariablen: {{user}}, {{date}}, {{source}}",
            parse_mode="Markdown",
        )


# ── Text-Input-Handler ──────────────────────────────────
async def text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Nur in Privatchats antworten
    if update.message.chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return

    awaiting = ctx.user_data.get("awaiting")
    if not awaiting:
        return

    text = update.message.text.strip()
    ctx.user_data["awaiting"] = None

    if awaiting == "add_source":
        ids_raw = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]
        added = []
        existing = []
        invalid = []
        for raw in ids_raw:
            try:
                cid = int(raw)
                if cid not in config["source_chats"]:
                    config["source_chats"].append(cid)
                    added.append(str(cid))
                else:
                    existing.append(str(cid))
            except ValueError:
                invalid.append(raw)
        save_config(config)
        parts = []
        if added:
            parts.append(f"✅ Hinzugefügt: {', '.join(added)}")
        if existing:
            parts.append(f"ℹ️ Bereits vorhanden: {', '.join(existing)}")
        if invalid:
            parts.append(f"❌ Ungültig: {', '.join(invalid)}")
        await update.message.reply_text("\n".join(parts) or "Keine Änderungen.")

    elif awaiting == "set_target":
        ids_raw = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]
        added = []
        invalid = []
        if "target_channels" not in config:
            config["target_channels"] = []
        for raw in ids_raw:
            try:
                cid = int(raw)
                if cid not in config["target_channels"]:
                    config["target_channels"].append(cid)
                    added.append(str(cid))
            except ValueError:
                invalid.append(raw)
        save_config(config)
        parts = []
        if added:
            parts.append(f"✅ Ziel-Kanäle hinzugefügt: {', '.join(added)}")
        if invalid:
            parts.append(f"❌ Ungültig: {', '.join(invalid)}")
        await update.message.reply_text("\n".join(parts) or "Keine Änderungen.")


# ── Media-Handler ────────────────────────────────────────
async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id
    if chat_id not in config.get("source_chats", []):
        return

    targets = config.get("target_channels", [])
    if not targets:
        return

    # Determine media type and file_unique_id
    file_unique_id = None
    file_type = None

    if msg.video:
        file_unique_id = msg.video.file_unique_id
        file_type = "video"
    elif msg.photo:
        file_unique_id = msg.photo[-1].file_unique_id
        file_type = "photo"
    elif msg.animation:
        file_unique_id = msg.animation.file_unique_id
        file_type = "animation"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
        file_unique_id = msg.document.file_unique_id
        file_type = "video"
    else:
        return

    # Duplikat-Check
    if is_duplicate(file_unique_id):
        record_duplicate(file_unique_id, file_type, chat_id)
        logger.info(f"Duplikat blockiert: {file_unique_id} aus {chat_id}")
        return

    # Build caption
    user = msg.from_user
    user_name = f"@{user.username}" if user and user.username else (user.first_name if user else "Unbekannt")
    now = datetime.now(DE_TZ).strftime("%d.%m.%Y %H:%M")
    source_title = msg.chat.title or str(chat_id)

    caption_template = config.get("caption_template", "📹 Von: {user} | 📅 {date} | 💬 Quelle: {source}")
    caption = caption_template.format(user=user_name, date=now, source=source_title)

    try:
        for target in targets:
            try:
                if msg.video:
                    await ctx.bot.send_video(chat_id=target, video=msg.video.file_id, caption=caption)
                elif msg.photo:
                    await ctx.bot.send_photo(chat_id=target, photo=msg.photo[-1].file_id, caption=caption)
                elif msg.animation:
                    await ctx.bot.send_animation(chat_id=target, animation=msg.animation.file_id, caption=caption)
                elif msg.document:
                    await ctx.bot.send_document(chat_id=target, document=msg.document.file_id, caption=caption)
            except Exception as e:
                logger.error(f"Fehler beim Weiterleiten an {target}: {e}")

        record_media(file_unique_id, file_type, chat_id, user_name, msg.message_id)
        logger.info(f"Weitergeleitet: {file_type} von {user_name} aus {source_title}")
    except Exception as e:
        logger.error(f"Fehler beim Weiterleiten: {e}")


# ── Main ─────────────────────────────────────────────────
def main():
    init_db()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN nicht gesetzt!")
        return

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    app.add_handler(MessageHandler(filters.VIDEO | filters.PHOTO | filters.ANIMATION | filters.Document.VIDEO, handle_media))

    # Bot-Commands setzen
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "Bot starten"),
            BotCommand("menu", "Einstellungen öffnen"),
        ])

    app.post_init = post_init

    logger.info("Bot gestartet...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
