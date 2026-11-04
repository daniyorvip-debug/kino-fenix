# bot.py
# Premium kino bot — pyTelegramBotAPI + SQLite
# Python 3.10+
#
# O‘rnatish:
#   pip install pyTelegramBotAPI>=4.14.0   (xabar reaksiyalari uchun shu versiyadan yuqori kerak)
#
# Ishga tushirishdan oldin quyidagilarni almashtiring:
#   BOT_TOKEN
#   ADMIN_IDS
#
# Majburiy obuna tekshiruvi ishlashi uchun bot kanallarda admin bo‘lishi kerak.

import html
import logging
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import telebot
from telebot import apihelper, types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, jsonify


# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Masalan: {123456789, 987654321}
ADMIN_IDS = {7560678590, 6153474241} | {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

DB_NAME = "kino_bot.db"

DEFAULT_CARD_NUMBER = "8600000000000000"
DEFAULT_CARD_OWNER = "KARTA EGASI"
DEFAULT_PAYMENT_NOTE = "To‘lovni amalga oshirgach, chek rasmini yoki PDF faylini yuboring."

DEFAULT_TARIFFS = [
    ("7 kunlik", 7, 15000),
    ("30 kunlik", 30, 39000),
    ("90 kunlik", 90, 99000),
    ("365 kunlik", 365, 249000),
]

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN noto‘g‘ri. Render Environment bo‘limiga haqiqiy BotFather tokenini kiriting."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# Har bir foydalanuvchi xabariga tasodifiy emoji bilan reaksiya bildirish
REACTION_EMOJIS = ["🔥", "✅", "🚀", "🎯", "⚡️", "😱"]


@bot.middleware_handler(update_types=["message"])
def react_to_every_message(bot_instance, message):
    try:
        bot_instance.set_message_reaction(
            message.chat.id,
            message.message_id,
            reaction=[types.ReactionTypeEmoji(random.choice(REACTION_EMOJIS))],
        )
    except Exception:
        pass


# =========================================================
# MA’LUMOTLAR BAZASI
# =========================================================

db_lock = threading.RLock()


def db_connect():
    connection = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def execute(query: str, params: tuple = (), fetchone=False, fetchall=False):
    with db_lock:
        conn = db_connect()
        try:
            cursor = conn.execute(query, params)
            conn.commit()

            if fetchone:
                return cursor.fetchone()
            if fetchall:
                return cursor.fetchall()
            return cursor.lastrowid
        finally:
            conn.close()


def init_db():
    execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            is_premium INTEGER NOT NULL DEFAULT 0,
            premium_until TEXT,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            caption TEXT NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL DEFAULT 'video',
            is_premium INTEGER NOT NULL DEFAULT 0,
            views INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            added_by INTEGER NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            invite_link TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            days INTEGER NOT NULL,
            price INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER NOT NULL,
            tariff_name TEXT NOT NULL,
            days INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            receipt_file_id TEXT NOT NULL,
            receipt_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            answered INTEGER NOT NULL DEFAULT 0
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT ''
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS watch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            movie_id INTEGER NOT NULL,
            watched_at TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            found INTEGER NOT NULL,
            searched_at TEXT NOT NULL
        )
    """)

    for key, value in [
        ("card_number", DEFAULT_CARD_NUMBER),
        ("card_owner", DEFAULT_CARD_OWNER),
        ("payment_note", DEFAULT_PAYMENT_NOTE),
    ]:
        execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (key, value),
        )

    tariff_count = execute(
        "SELECT COUNT(*) AS count FROM tariffs",
        fetchone=True,
    )["count"]

    if tariff_count == 0:
        for name, days, price in DEFAULT_TARIFFS:
            execute(
                "INSERT INTO tariffs(name, days, price) VALUES(?, ?, ?)",
                (name, days, price),
            )


# =========================================================
# YORDAMCHI FUNKSIYALAR
# =========================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def date_text(value: datetime):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def money(value: int):
    return f"{value:,}".replace(",", " ") + " so‘m"


def safe(value):
    return html.escape(str(value or ""))


def normalize_channel_username(value: str):
    value = value.strip()

    if value.startswith("https://t.me/"):
        value = value.replace("https://t.me/", "", 1)

    if value.startswith("t.me/"):
        value = value.replace("t.me/", "", 1)

    value = value.strip("/ ")

    if not value.startswith("@"):
        value = "@" + value

    return value


def normalize_card(value: str):
    digits = re.sub(r"\D", "", value)
    if len(digits) != 16:
        return None
    return " ".join(digits[i:i + 4] for i in range(0, 16, 4))


def is_admin(user_id: int):
    return user_id in ADMIN_IDS


def get_setting(key: str):
    row = execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
        fetchone=True,
    )
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    execute("""
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))


def set_state(user_id: int, state: str, data: str = ""):
    execute("""
        INSERT INTO states(user_id, state, data)
        VALUES(?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET state=excluded.state, data=excluded.data
    """, (user_id, state, data))


def get_state(user_id: int):
    row = execute(
        "SELECT state, data FROM states WHERE user_id=?",
        (user_id,),
        fetchone=True,
    )
    if not row:
        return None, ""
    return row["state"], row["data"]


def clear_state(user_id: int):
    execute("DELETE FROM states WHERE user_id=?", (user_id,))


# Admin uchun raqamli kiritish talab qiladigan holatlar — bu holatlarda
# yuborilgan raqam kino kodi sifatida emas, balki shu amal uchun
# kiritilgan qiymat sifatida qabul qilinadi.
ADMIN_ONLY_STATES = {
    "add_movie_caption",
    "add_movie_code",
    "delete_movie_code",
    "payment_reject_reason",
    "manage_user_id",
    "user_add_days_value",
    "reply_to_user",
    "channel_add_username",
    "channel_add_link",
    "channel_delete_id",
    "change_card_number",
    "change_card_owner",
    "change_payment_note",
    "broadcast_content",
}


def find_and_send_movie_by_code(message, user_id: int, code: str):
    """Kino kodini istalgan holatda (panel tanlanган yoki tanlanmagan)
    qidiradi va topilsa yuboradi, topilmasa foydalanuvchini xabardor qiladi."""
    movie = execute(
        "SELECT * FROM movies WHERE code=?",
        (code,),
        fetchone=True,
    )

    execute(
        "INSERT INTO search_log(user_id, code, found, searched_at) VALUES(?, ?, ?, ?)",
        (user_id, code, 1 if movie else 0, now_text()),
    )

    if not movie:
        bot.send_message(
            message.chat.id,
            "❌ Bunday kodli kino topilmadi.\n\n"
            "Kodni tekshirib, qayta urinib ko‘ring.",
            reply_markup=main_keyboard(user_id),
        )
        return

    send_movie_to_user(
        message.chat.id,
        user_id,
        movie,
        first_name=message.from_user.first_name or "",
    )


def register_user(user):
    username = f"@{user.username}" if user.username else ""
    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()

    execute("""
        INSERT INTO users(
            user_id, full_name, username, joined_at, last_active
        )
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            full_name=excluded.full_name,
            username=excluded.username,
            last_active=excluded.last_active
    """, (
        user.id,
        full_name,
        username,
        now_text(),
        now_text(),
    ))


def get_user(user_id: int):
    return execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
        fetchone=True,
    )


def premium_active(user_id: int):
    user = get_user(user_id)
    if not user:
        return False

    if not user["is_premium"]:
        return False

    premium_until = user["premium_until"]
    if not premium_until:
        return False

    try:
        expires = datetime.strptime(premium_until, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False

    if expires <= datetime.now():
        execute(
            "UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?",
            (user_id,),
        )
        return False

    return True


def add_premium_days(user_id: int, days: int):
    user = get_user(user_id)
    if not user:
        return None

    start = datetime.now()

    if user["premium_until"]:
        try:
            current_expiry = datetime.strptime(
                user["premium_until"],
                "%Y-%m-%d %H:%M:%S",
            )
            if current_expiry > start:
                start = current_expiry
        except ValueError:
            pass

    new_expiry = start + timedelta(days=days)

    execute("""
        UPDATE users
        SET is_premium=1, premium_until=?
        WHERE user_id=?
    """, (date_text(new_expiry), user_id))

    return new_expiry


def remove_premium(user_id: int):
    execute("""
        UPDATE users
        SET is_premium=0, premium_until=NULL
        WHERE user_id=?
    """, (user_id,))


def get_channels():
    return execute(
        "SELECT * FROM channels ORDER BY id",
        fetchall=True,
    )


def check_subscription(user_id: int):
    # Premium foydalanuvchilarga majburiy obuna qo‘llanmaydi.
    if premium_active(user_id):
        return True

    channels = get_channels()
    if not channels:
        return True

    for channel in channels:
        try:
            member = bot.get_chat_member(channel["username"], user_id)
            if member.status in ("left", "kicked"):
                return False
        except ApiTelegramException:
            return False
        except Exception:
            logging.exception("Obuna tekshirishda xato")
            return False

    return True


def subscription_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for channel in get_channels():
        keyboard.add(
            types.InlineKeyboardButton(
                f"📢 {channel['title']}",
                url=channel["invite_link"],
            )
        )

    keyboard.add(
        types.InlineKeyboardButton(
            "✅ Obunani tekshirish",
            callback_data="check_subscription",
        )
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "👑 Premium obuna",
            callback_data="premium_menu",
        )
    )
    return keyboard


def main_keyboard(user_id: int):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    keyboard.add(
        types.KeyboardButton("🔎 Kino kodi"),
        types.KeyboardButton("👑 Premium obuna"),
    )
    keyboard.add(
        types.KeyboardButton("💬 Adminga xabar"),
        types.KeyboardButton("👤 Profil"),
    )

    if is_admin(user_id):
        keyboard.add(types.KeyboardButton("🛠 Admin panel"))

    return keyboard


def admin_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    keyboard.add(
        types.KeyboardButton("➕ Oddiy kino"),
        types.KeyboardButton("👑 Premium kino"),
    )
    keyboard.add(
        types.KeyboardButton("🗑 Kino o‘chirish"),
        types.KeyboardButton("📋 Barcha kinolar"),
    )
    keyboard.add(
        types.KeyboardButton("💳 To‘lovlar"),
        types.KeyboardButton("👥 User boshqarish"),
    )
    keyboard.add(
        types.KeyboardButton("📢 Reklama yuborish"),
        types.KeyboardButton("📡 Kanallar"),
    )
    keyboard.add(
        types.KeyboardButton("💳 Karta sozlamasi"),
        types.KeyboardButton("📊 Admin statistika"),
    )
    keyboard.add(types.KeyboardButton("🏠 Asosiy menyu"))
    return keyboard


def send_subscription_required(chat_id: int):
    bot.send_message(
        chat_id,
        "🔐 <b>Botdan foydalanish uchun kanallarga obuna bo‘ling.</b>\n\n"
        "Barcha kanallarga kirgach, <b>✅ Obunani tekshirish</b> tugmasini bosing.\n\n"
        "👑 Premium foydalanuvchilarga majburiy obuna ko‘rsatilmaydi.",
        reply_markup=subscription_keyboard(),
    )


def open_main_menu(chat_id: int, user_id: int):
    bot.send_message(
        chat_id,
        "🎬 <b>Prime kino botga xush kelibsiz!</b>\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_keyboard(user_id),
    )


def ensure_access(message):
    register_user(message.from_user)

    user = get_user(message.from_user.id)
    if user and user["is_blocked"]:
        bot.send_message(message.chat.id, "🚫 Siz botdan bloklangansiz.")
        return False

    if is_admin(message.from_user.id):
        return True

    if not check_subscription(message.from_user.id):
        send_subscription_required(message.chat.id)
        return False

    return True


# =========================================================
# START VA ASOSIY MENYU
# =========================================================

@bot.message_handler(commands=["start"])
def start_handler(message):
    register_user(message.from_user)
    clear_state(message.from_user.id)

    user = get_user(message.from_user.id)
    if user and user["is_blocked"]:
        bot.send_message(message.chat.id, "🚫 Siz botdan bloklangansiz.")
        return

    if is_admin(message.from_user.id) or check_subscription(message.from_user.id):
        open_main_menu(message.chat.id, message.from_user.id)
    else:
        send_subscription_required(message.chat.id)


@bot.message_handler(commands=["admin"])
def admin_command(message):
    register_user(message.from_user)

    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Siz admin emassiz.")
        return

    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == "🛠 Admin panel")
def admin_button_handler(message):
    register_user(message.from_user)

    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Siz admin emassiz.")
        return

    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_subscription_callback(call):
    register_user(call.from_user)

    if check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi ✅")
        try:
            bot.edit_message_text(
                "✅ <b>Obunangiz tasdiqlandi.</b>",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass

        open_main_menu(call.message.chat.id, call.from_user.id)
    else:
        bot.answer_callback_query(
            call.id,
            "Hali barcha kanallarga obuna bo‘lmagansiz.",
            show_alert=True,
        )


@bot.message_handler(func=lambda message: message.text == "🏠 Asosiy menyu")
def home_handler(message):
    if not ensure_access(message):
        return
    clear_state(message.from_user.id)
    open_main_menu(message.chat.id, message.from_user.id)


# =========================================================
# PREMIUM TARIFLAR VA TO‘LOV
# =========================================================

def tariff_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    tariffs = execute(
        "SELECT * FROM tariffs WHERE is_active=1 ORDER BY days",
        fetchall=True,
    )

    for tariff in tariffs:
        keyboard.add(
            types.InlineKeyboardButton(
                f"👑 {tariff['name']} — {money(tariff['price'])}",
                callback_data=f"tariff:{tariff['id']}",
            )
        )

    return keyboard


def send_premium_menu(chat_id: int):
    bot.send_message(
        chat_id,
        "👑 <b>Premium obuna</b>\n\n"
        "Premium imkoniyatlar:\n"
        "• barcha premium kinolar;\n"
        "• majburiy obunasiz foydalanish;\n"
        "• maxsus kontentlar;\n"
        "• premium muddati davomida cheklovsiz kirish.\n\n"
        "Tarifni tanlang:",
        reply_markup=tariff_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == "👑 Premium obuna")
def premium_button(message):
    register_user(message.from_user)
    send_premium_menu(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "premium_menu")
def premium_menu_callback(call):
    register_user(call.from_user)
    bot.answer_callback_query(call.id)
    send_premium_menu(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("tariff:"))
def tariff_callback(call):
    register_user(call.from_user)

    try:
        tariff_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Tarif xatosi.", show_alert=True)
        return

    tariff = execute(
        "SELECT * FROM tariffs WHERE id=? AND is_active=1",
        (tariff_id,),
        fetchone=True,
    )

    if not tariff:
        bot.answer_callback_query(call.id, "Tarif topilmadi.", show_alert=True)
        return

    card = normalize_card(get_setting("card_number"))
    card = card or safe(get_setting("card_number"))
    owner = safe(get_setting("card_owner"))
    note = safe(get_setting("payment_note"))

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            "📤 Chek yuborish",
            callback_data=f"send_receipt:{tariff_id}",
        )
    )

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"💳 <b>To‘lov ma’lumotlari</b>\n\n"
        f"📦 Tarif: <b>{safe(tariff['name'])}</b>\n"
        f"⏳ Muddat: <b>{tariff['days']} kun</b>\n"
        f"💰 Summa: <b>{money(tariff['price'])}</b>\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: <b>{owner}</b>\n\n"
        f"📝 {note}",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("send_receipt:"))
def send_receipt_callback(call):
    try:
        tariff_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Tarif xatosi.", show_alert=True)
        return

    tariff = execute(
        "SELECT * FROM tariffs WHERE id=? AND is_active=1",
        (tariff_id,),
        fetchone=True,
    )

    if not tariff:
        bot.answer_callback_query(call.id, "Tarif topilmadi.", show_alert=True)
        return

    set_state(call.from_user.id, "waiting_receipt", str(tariff_id))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📤 <b>Chek rasmini yoki PDF faylini yuboring.</b>\n\n"
        "Bekor qilish uchun /cancel yuboring.",
    )


@bot.message_handler(
    content_types=["photo", "document"],
    func=lambda message: get_state(message.from_user.id)[0] == "waiting_receipt",
)
def receipt_handler(message):
    register_user(message.from_user)
    state, data = get_state(message.from_user.id)

    try:
        tariff_id = int(data)
    except ValueError:
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "❌ Tarif ma’lumoti buzilgan.")
        return

    tariff = execute(
        "SELECT * FROM tariffs WHERE id=? AND is_active=1",
        (tariff_id,),
        fetchone=True,
    )

    if not tariff:
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "❌ Tarif topilmadi.")
        return

    if message.content_type == "photo":
        receipt_file_id = message.photo[-1].file_id
        receipt_type = "photo"
    else:
        receipt_file_id = message.document.file_id
        receipt_type = "document"

    payment_id = execute("""
        INSERT INTO payments(
            user_id, tariff_id, tariff_name, days, amount,
            receipt_file_id, receipt_type, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.from_user.id,
        tariff["id"],
        tariff["name"],
        tariff["days"],
        tariff["price"],
        receipt_file_id,
        receipt_type,
        now_text(),
    ))

    clear_state(message.from_user.id)

    bot.send_message(
        message.chat.id,
        "✅ <b>Chekingiz adminga yuborildi.</b>\n\n"
        "Admin tekshirganidan keyin natija sizga yuboriladi.",
        reply_markup=main_keyboard(message.from_user.id),
    )

    user = get_user(message.from_user.id)
    caption = (
        f"💳 <b>Yangi to‘lov #{payment_id}</b>\n\n"
        f"👤 {safe(user['full_name'])}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"🔗 {safe(user['username'] or 'Username yo‘q')}\n"
        f"📦 Tarif: <b>{safe(tariff['name'])}</b>\n"
        f"⏳ Muddat: <b>{tariff['days']} kun</b>\n"
        f"💰 Summa: <b>{money(tariff['price'])}</b>"
    )

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "✅ Tasdiqlash",
            callback_data=f"payment_accept:{payment_id}",
        ),
        types.InlineKeyboardButton(
            "❌ Rad etish",
            callback_data=f"payment_reject:{payment_id}",
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "💬 Xabar yuborish",
            callback_data=f"reply_user:{message.from_user.id}",
        )
    )

    for admin_id in ADMIN_IDS:
        try:
            if receipt_type == "photo":
                bot.send_photo(
                    admin_id,
                    receipt_file_id,
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                bot.send_document(
                    admin_id,
                    receipt_file_id,
                    caption=caption,
                    reply_markup=keyboard,
                )
        except Exception:
            logging.exception("Chekni adminga yuborishda xato")


@bot.callback_query_handler(func=lambda call: call.data.startswith("payment_accept:"))
def payment_accept_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = int(call.data.split(":", 1)[1])
    payment = execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,),
        fetchone=True,
    )

    if not payment:
        bot.answer_callback_query(call.id, "To‘lov topilmadi.", show_alert=True)
        return

    if payment["status"] != "pending":
        bot.answer_callback_query(
            call.id,
            f"Bu to‘lov allaqachon {payment['status']}.",
            show_alert=True,
        )
        return

    expiry = add_premium_days(payment["user_id"], payment["days"])

    execute("""
        UPDATE payments
        SET status='accepted', reviewed_at=?, reviewed_by=?
        WHERE id=?
    """, (now_text(), call.from_user.id, payment_id))

    bot.answer_callback_query(call.id, "To‘lov tasdiqlandi ✅")

    try:
        bot.edit_message_caption(
            caption=(
                (call.message.caption or "")
                + f"\n\n✅ <b>Tasdiqlandi</b>\n"
                f"Admin: <code>{call.from_user.id}</code>"
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception:
        pass

    try:
        bot.send_message(
            payment["user_id"],
            f"✅ <b>To‘lovingiz tasdiqlandi!</b>\n\n"
            f"👑 Tarif: <b>{safe(payment['tariff_name'])}</b>\n"
            f"📅 Premium tugashi: <b>{expiry.strftime('%d.%m.%Y %H:%M')}</b>",
            reply_markup=main_keyboard(payment["user_id"]),
        )
    except Exception:
        logging.exception("Userga tasdiq xabarini yuborishda xato")


@bot.callback_query_handler(func=lambda call: call.data.startswith("payment_reject:"))
def payment_reject_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = int(call.data.split(":", 1)[1])
    payment = execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,),
        fetchone=True,
    )

    if not payment or payment["status"] != "pending":
        bot.answer_callback_query(call.id, "To‘lov mavjud emas yoki ko‘rilgan.")
        return

    set_state(call.from_user.id, "payment_reject_reason", str(payment_id))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "❌ Rad etish sababini yozing:",
    )


# =========================================================
# KINO QIDIRISH VA REYTING
# =========================================================

@bot.message_handler(func=lambda message: message.text == "🔎 Kino kodi")
def ask_movie_code(message):
    if not ensure_access(message):
        return

    set_state(message.from_user.id, "waiting_movie_code")
    bot.send_message(
        message.chat.id,
        "🔢 <b>Kino kodini yuboring:</b>\n\nMasalan: <code>145</code>",
    )


def send_movie_to_user(chat_id: int, user_id: int, movie, first_name: str = ""):
    if movie["is_premium"] and not premium_active(user_id) and not is_admin(user_id):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "👑 Premiumga obuna bo‘lish",
                callback_data="premium_menu",
            )
        )

        bot.send_message(
            chat_id,
            "🔒 <b>Bu kino faqat premium foydalanuvchilar uchun.</b>\n\n"
            "Kinoni ko‘rish uchun premium tariflardan birini tanlang.",
            reply_markup=keyboard,
        )
        return

    new_views = movie["views"] + 1
    execute(
        "UPDATE movies SET views=? WHERE id=?",
        (new_views, movie["id"]),
    )
    execute(
        "INSERT INTO watch_log(user_id, movie_id, watched_at) VALUES(?, ?, ?)",
        (user_id, movie["id"], now_text()),
    )

    type_text = "👑 Premium kino" if movie["is_premium"] else "🆓 Oddiy kino"
    greeting = f"🍿 <b>Yoqimli tomosha, {safe(first_name)}!</b>" if first_name else "🍿 <b>Yoqimli tomosha!</b>"
    caption = (
        f"{greeting}\n\n"
        f"<b>{safe(movie['caption'])}</b>\n\n"
        f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
        f"{type_text}\n\n"
        f"🎥 Ko‘proq kinolar: @PrimeKinolarbot"
    )

    if movie["file_type"] == "video":
        bot.send_video(
            chat_id,
            movie["file_id"],
            caption=caption,
            supports_streaming=True,
        )
    else:
        bot.send_document(
            chat_id,
            movie["file_id"],
            caption=caption,
        )


# =========================================================
# PROFIL
# =========================================================

@bot.message_handler(func=lambda message: message.text == "👤 Profil")
def profile_handler(message):
    register_user(message.from_user)
    user = get_user(message.from_user.id)

    if premium_active(message.from_user.id):
        status = "👑 Premium"
        expiry = datetime.strptime(
            get_user(message.from_user.id)["premium_until"],
            "%Y-%m-%d %H:%M:%S",
        ).strftime("%d.%m.%Y %H:%M")
    else:
        status = "🆓 Oddiy foydalanuvchi"
        expiry = "—"

    watched = execute(
        "SELECT COUNT(*) AS c FROM watch_log WHERE user_id=?",
        (message.from_user.id,),
        fetchone=True,
    )["c"]

    bot.send_message(
        message.chat.id,
        "👤 <b>PROFIL</b>\n\n"
        f"Ism: <b>{safe(user['full_name'])}</b>\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"🔗 Username: {safe(user['username'] or 'Yo‘q')}\n"
        f"Holat: <b>{status}</b>\n"
        f"📅 Premium tugashi: <b>{expiry}</b>\n"
        f"🎬 Ko‘rilgan kinolar: <b>{watched}</b>\n"
        f"📆 Qo‘shilgan sana: <b>{safe(user['joined_at'])}</b>",
    )


# =========================================================
# USERDAN ADMINGA XABAR
# =========================================================

@bot.message_handler(func=lambda message: message.text == "💬 Adminga xabar")
def ask_admin_message(message):
    if not ensure_access(message):
        return

    set_state(message.from_user.id, "waiting_admin_message")
    bot.send_message(
        message.chat.id,
        "💬 <b>Adminga yuboriladigan xabaringizni yozing:</b>",
    )


# =========================================================
# ADMIN: KINO QO‘SHISH
# =========================================================

@bot.message_handler(func=lambda message: message.text in ("➕ Oddiy kino", "👑 Premium kino"))
def add_movie_start(message):
    if not is_admin(message.from_user.id):
        return

    movie_type = "1" if message.text == "👑 Premium kino" else "0"
    set_state(message.from_user.id, "add_movie_video", movie_type)

    bot.send_message(
        message.chat.id,
        "🎥 <b>Kino videosini yuboring yoki boshqa kanaldan uzating.</b>\n\n"
        "Video fayl yoki document ko‘rinishida qabul qilinadi.",
    )


@bot.message_handler(
    content_types=["video", "document"],
    func=lambda message: get_state(message.from_user.id)[0] == "add_movie_video",
)
def add_movie_video(message):
    if not is_admin(message.from_user.id):
        return

    state, movie_type = get_state(message.from_user.id)

    if message.content_type == "video":
        file_id = message.video.file_id
        file_type = "video"
    else:
        file_id = message.document.file_id
        file_type = "document"

    # data: premium|file_type|file_id
    set_state(
        message.from_user.id,
        "add_movie_caption",
        f"{movie_type}|{file_type}|{file_id}",
    )

    bot.send_message(
        message.chat.id,
        "📝 <b>Kino captionini yuboring.</b>\n\n"
        "Birinchi qatorda kino nomi bo‘lsin.\n\n"
        "Masalan:\n"
        "🎬 Avatar\n"
        "🎭 Fantastika\n"
        "📅 2009\n"
        "🌐 O‘zbek tilida\n"
        "🎞 Full HD",
    )


# =========================================================
# ADMIN: KINO O‘CHIRISH VA RO‘YXAT
# =========================================================

@bot.message_handler(func=lambda message: message.text == "🗑 Kino o‘chirish")
def delete_movie_start(message):
    if not is_admin(message.from_user.id):
        return

    set_state(message.from_user.id, "delete_movie_code")
    bot.send_message(
        message.chat.id,
        "🗑 O‘chiriladigan kino kodini yuboring:",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_movie_confirm:"))
def delete_movie_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.")
        return

    movie_id = int(call.data.split(":", 1)[1])
    movie = execute(
        "SELECT * FROM movies WHERE id=?",
        (movie_id,),
        fetchone=True,
    )

    if not movie:
        bot.answer_callback_query(call.id, "Kino topilmadi.")
        return

    execute("DELETE FROM movies WHERE id=?", (movie_id,))
    bot.answer_callback_query(call.id, "Kino o‘chirildi ✅")
    bot.edit_message_text(
        f"✅ <b>{safe(movie['title'])}</b> bazadan o‘chirildi.",
        call.message.chat.id,
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data == "delete_movie_cancel")
def delete_movie_cancel(call):
    if not is_admin(call.from_user.id):
        return
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    bot.edit_message_text(
        "❌ Kino o‘chirish bekor qilindi.",
        call.message.chat.id,
        call.message.message_id,
    )


@bot.message_handler(func=lambda message: message.text == "📋 Barcha kinolar")
def all_movies_handler(message):
    if not is_admin(message.from_user.id):
        return

    movies = execute("""
        SELECT code, title, is_premium, views
        FROM movies
        ORDER BY id DESC
        LIMIT 100
    """, fetchall=True)

    if not movies:
        bot.send_message(message.chat.id, "Kinolar mavjud emas.")
        return

    text = ["📋 <b>BARCHA KINOLAR</b>\n"]
    for index, movie in enumerate(movies, start=1):
        movie_type = "👑 Premium" if movie["is_premium"] else "🆓 Oddiy"
        text.append(
            f"{index}. <b>{safe(movie['title'])}</b>\n"
            f"   Kod: <code>{safe(movie['code'])}</code>\n"
            f"   Turi: {movie_type}\n"
            f"   Ko‘rishlar: {movie['views']}"
        )

    full_text = "\n\n".join(text)

    for start in range(0, len(full_text), 3900):
        bot.send_message(message.chat.id, full_text[start:start + 3900])


# =========================================================
# ADMIN: TO‘LOVLAR
# =========================================================

@bot.message_handler(func=lambda message: message.text == "💳 To‘lovlar")
def admin_payments(message):
    if not is_admin(message.from_user.id):
        return

    payments = execute("""
        SELECT p.*, u.full_name, u.username
        FROM payments p
        LEFT JOIN users u ON u.user_id=p.user_id
        WHERE p.status='pending'
        ORDER BY p.id DESC
        LIMIT 30
    """, fetchall=True)

    if not payments:
        bot.send_message(message.chat.id, "✅ Kutilayotgan to‘lovlar yo‘q.")
        return

    bot.send_message(
        message.chat.id,
        f"⏳ Kutilayotgan to‘lovlar: <b>{len(payments)}</b> ta",
    )

    for payment in payments:
        caption = (
            f"💳 <b>To‘lov #{payment['id']}</b>\n\n"
            f"👤 {safe(payment['full_name'])}\n"
            f"🆔 <code>{payment['user_id']}</code>\n"
            f"📦 {safe(payment['tariff_name'])}\n"
            f"💰 {money(payment['amount'])}"
        )

        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton(
                "✅ Tasdiqlash",
                callback_data=f"payment_accept:{payment['id']}",
            ),
            types.InlineKeyboardButton(
                "❌ Rad etish",
                callback_data=f"payment_reject:{payment['id']}",
            ),
        )

        try:
            if payment["receipt_type"] == "photo":
                bot.send_photo(
                    message.chat.id,
                    payment["receipt_file_id"],
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                bot.send_document(
                    message.chat.id,
                    payment["receipt_file_id"],
                    caption=caption,
                    reply_markup=keyboard,
                )
        except Exception:
            logging.exception("To‘lov chekini chiqarishda xato")


# =========================================================
# ADMIN: FOYDALANUVCHI BOSHQARUVI
# =========================================================

@bot.message_handler(func=lambda message: message.text == "👥 User boshqarish")
def user_manage_start(message):
    if not is_admin(message.from_user.id):
        return

    set_state(message.from_user.id, "manage_user_id")
    bot.send_message(
        message.chat.id,
        "👥 Boshqariladigan foydalanuvchi Telegram ID sini yuboring:",
    )


def user_manage_keyboard(user_id: int):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "➕ Premium kun",
            callback_data=f"user_add_days:{user_id}",
        ),
        types.InlineKeyboardButton(
            "🗑 Premiumni olish",
            callback_data=f"user_remove_premium:{user_id}",
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "🚫 Bloklash",
            callback_data=f"user_block:{user_id}",
        ),
        types.InlineKeyboardButton(
            "✅ Blokdan chiqarish",
            callback_data=f"user_unblock:{user_id}",
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "💬 Xabar yuborish",
            callback_data=f"reply_user:{user_id}",
        )
    )
    return keyboard


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_add_days:"))
def user_add_days_callback(call):
    if not is_admin(call.from_user.id):
        return

    user_id = int(call.data.split(":", 1)[1])
    set_state(call.from_user.id, "user_add_days_value", str(user_id))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"➕ <code>{user_id}</code> foydalanuvchiga necha kun premium qo‘shilsin?",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_remove_premium:"))
def user_remove_premium_callback(call):
    if not is_admin(call.from_user.id):
        return

    user_id = int(call.data.split(":", 1)[1])
    remove_premium(user_id)
    bot.answer_callback_query(call.id, "Premium olib tashlandi.")
    bot.send_message(
        call.message.chat.id,
        f"🗑 <code>{user_id}</code> foydalanuvchining premiumi olib tashlandi.",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_block:"))
def user_block_callback(call):
    if not is_admin(call.from_user.id):
        return

    user_id = int(call.data.split(":", 1)[1])
    execute(
        "UPDATE users SET is_blocked=1 WHERE user_id=?",
        (user_id,),
    )
    bot.answer_callback_query(call.id, "Foydalanuvchi bloklandi.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_unblock:"))
def user_unblock_callback(call):
    if not is_admin(call.from_user.id):
        return

    user_id = int(call.data.split(":", 1)[1])
    execute(
        "UPDATE users SET is_blocked=0 WHERE user_id=?",
        (user_id,),
    )
    bot.answer_callback_query(call.id, "Blokdan chiqarildi.")


# =========================================================
# ADMIN: USERGA JAVOB
# =========================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_user:"))
def reply_user_callback(call):
    if not is_admin(call.from_user.id):
        return

    user_id = int(call.data.split(":", 1)[1])
    set_state(call.from_user.id, "reply_to_user", str(user_id))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"💬 <code>{user_id}</code> foydalanuvchiga yuboriladigan xabarni yozing:",
    )


# =========================================================
# ADMIN: REKLAMA
# =========================================================

@bot.message_handler(func=lambda message: message.text == "📢 Reklama yuborish")
def broadcast_start(message):
    if not is_admin(message.from_user.id):
        return

    set_state(message.from_user.id, "broadcast_content")
    bot.send_message(
        message.chat.id,
        "📢 Barcha foydalanuvchilarga yuboriladigan reklama xabarini yuboring.\n\n"
        "Matn, rasm, video yoki hujjat yuborishingiz mumkin.",
    )


def broadcast_copy(admin_chat_id: int, source_message_id: int):
    users = execute(
        "SELECT user_id FROM users WHERE is_blocked=0",
        fetchall=True,
    )

    success = 0
    failed = 0

    status_message = bot.send_message(
        admin_chat_id,
        f"📤 Reklama yuborish boshlandi.\nJami: {len(users)} ta user",
    )

    for index, row in enumerate(users, start=1):
        try:
            bot.copy_message(
                row["user_id"],
                admin_chat_id,
                source_message_id,
            )
            success += 1
        except Exception:
            failed += 1

        # Telegram limitlariga yaqinlashmaslik uchun.
        time.sleep(0.05)

        if index % 100 == 0:
            try:
                bot.edit_message_text(
                    f"📤 Jarayon: {index}/{len(users)}\n"
                    f"✅ {success} | ❌ {failed}",
                    admin_chat_id,
                    status_message.message_id,
                )
            except Exception:
                pass

    bot.edit_message_text(
        "📊 <b>REKLAMA NATIJASI</b>\n\n"
        f"✅ Yuborildi: <b>{success}</b>\n"
        f"❌ Yuborilmadi: <b>{failed}</b>",
        admin_chat_id,
        status_message.message_id,
    )


# =========================================================
# ADMIN: MAJBURIY OBUNA KANALLARI
# =========================================================

@bot.message_handler(func=lambda message: message.text == "📡 Kanallar")
def channels_admin(message):
    if not is_admin(message.from_user.id):
        return

    channels = get_channels()

    text = ["📡 <b>MAJBURIY OBUNA KANALLARI</b>\n"]
    if channels:
        for channel in channels:
            text.append(
                f"ID: <code>{channel['id']}</code>\n"
                f"📢 {safe(channel['title'])}\n"
                f"🔗 {safe(channel['username'])}"
            )
    else:
        text.append("Hozircha kanal qo‘shilmagan.")

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "➕ Kanal qo‘shish",
            callback_data="channel_add",
        ),
        types.InlineKeyboardButton(
            "🗑 Kanal o‘chirish",
            callback_data="channel_delete",
        ),
    )

    bot.send_message(
        message.chat.id,
        "\n\n".join(text),
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data == "channel_add")
def channel_add_callback(call):
    if not is_admin(call.from_user.id):
        return

    set_state(call.from_user.id, "channel_add_username")
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "➕ Kanal username yuboring.\n\nMasalan: <code>@kino_kanal</code>",
    )


@bot.callback_query_handler(func=lambda call: call.data == "channel_delete")
def channel_delete_callback(call):
    if not is_admin(call.from_user.id):
        return

    set_state(call.from_user.id, "channel_delete_id")
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "🗑 O‘chiriladigan kanal ID sini yuboring:",
    )


# =========================================================
# ADMIN: KARTA SOZLAMASI
# =========================================================

@bot.message_handler(func=lambda message: message.text == "💳 Karta sozlamasi")
def card_settings(message):
    if not is_admin(message.from_user.id):
        return

    card = normalize_card(get_setting("card_number")) or get_setting("card_number")

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(
            "🔢 Karta raqamini o‘zgartirish",
            callback_data="card_number_change",
        ),
        types.InlineKeyboardButton(
            "👤 Karta egasini o‘zgartirish",
            callback_data="card_owner_change",
        ),
        types.InlineKeyboardButton(
            "📝 To‘lov matnini o‘zgartirish",
            callback_data="payment_note_change",
        ),
    )

    bot.send_message(
        message.chat.id,
        "💳 <b>KARTA SOZLAMASI</b>\n\n"
        f"Karta: <code>{safe(card)}</code>\n"
        f"Egasi: <b>{safe(get_setting('card_owner'))}</b>\n"
        f"Izoh: {safe(get_setting('payment_note'))}",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data == "card_number_change")
def card_number_change(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "change_card_number")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "16 xonali karta raqamini yuboring:")


@bot.callback_query_handler(func=lambda call: call.data == "card_owner_change")
def card_owner_change(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "change_card_owner")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Karta egasi nomini yuboring:")


@bot.callback_query_handler(func=lambda call: call.data == "payment_note_change")
def payment_note_change(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "change_payment_note")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Yangi to‘lov izohini yuboring:")


# =========================================================
# ADMIN STATISTIKA
# =========================================================

@bot.message_handler(func=lambda message: message.text == "📊 Admin statistika")
def admin_statistics(message):
    if not is_admin(message.from_user.id):
        return

    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    premium = execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_premium=1",
        fetchone=True,
    )["c"]
    blocked = execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_blocked=1",
        fetchone=True,
    )["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    premium_movies = execute(
        "SELECT COUNT(*) AS c FROM movies WHERE is_premium=1",
        fetchone=True,
    )["c"]
    pending_payments = execute(
        "SELECT COUNT(*) AS c FROM payments WHERE status='pending'",
        fetchone=True,
    )["c"]
    accepted_sum = execute("""
        SELECT COALESCE(SUM(amount), 0) AS c
        FROM payments
        WHERE status='accepted'
    """, fetchone=True)["c"]

    bot.send_message(
        message.chat.id,
        "📊 <b>ADMIN STATISTIKA</b>\n\n"
        f"👥 Userlar: <b>{users}</b>\n"
        f"👑 Premium userlar: <b>{premium}</b>\n"
        f"🚫 Bloklanganlar: <b>{blocked}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👑 Premium kinolar: <b>{premium_movies}</b>\n"
        f"⏳ Kutilayotgan to‘lovlar: <b>{pending_payments}</b>\n"
        f"💰 Tasdiqlangan tushum: <b>{money(accepted_sum)}</b>",
    )


# =========================================================
# UNIVERSAL MATN HANDLER — HOLATLAR
# =========================================================

@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    clear_state(message.from_user.id)

    keyboard = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "❌ Amal bekor qilindi.",
        reply_markup=keyboard,
    )


@bot.message_handler(content_types=["text"])
def text_state_handler(message):
    register_user(message.from_user)
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state, data = get_state(user_id)

    # Foydalanuvchi qaysi panelda bo‘lishidan qat’i nazar (yoki hech qanday
    # panelda bo‘lmasa ham), raqamli kod yuborsa — kino kodi qidiruvi
    # ustuvor ishlaydi. Admin o‘ziga xos raqamli amal (kod kiritish,
    # foydalanuvchi ID kiritish va h.k.) ustida bo‘lsa, bu ustuvorlik
    # ishlamaydi — o‘sha amal odatdagidek davom etadi.
    if text.isdigit() and state not in ADMIN_ONLY_STATES:
        if not is_admin(user_id) and not check_subscription(user_id):
            send_subscription_required(message.chat.id)
            return

        clear_state(user_id)
        find_and_send_movie_by_code(message, user_id, text)
        return

    if not state:
        if not is_admin(user_id) and not check_subscription(user_id):
            send_subscription_required(message.chat.id)
            return

        bot.send_message(
            message.chat.id,
            "Kerakli bo‘limni menyudan tanlang.",
            reply_markup=main_keyboard(user_id),
        )
        return

    # USER: kino kodi
    if state == "waiting_movie_code":
        clear_state(user_id)
        find_and_send_movie_by_code(message, user_id, text)
        return

    # USER: adminga xabar
    if state == "waiting_admin_message":
        message_id = execute("""
            INSERT INTO messages(user_id, message_text, created_at)
            VALUES(?, ?, ?)
        """, (user_id, text, now_text()))

        clear_state(user_id)
        bot.send_message(
            message.chat.id,
            "✅ Xabaringiz adminga yuborildi.",
            reply_markup=main_keyboard(user_id),
        )

        user = get_user(user_id)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "✍️ Javob yozish",
                callback_data=f"reply_user:{user_id}",
            )
        )

        admin_text = (
            f"📩 <b>Yangi xabar #{message_id}</b>\n\n"
            f"👤 {safe(user['full_name'])}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🔗 {safe(user['username'] or 'Username yo‘q')}\n\n"
            f"💬 {safe(text)}"
        )

        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    admin_text,
                    reply_markup=keyboard,
                )
            except Exception:
                logging.exception("Adminga xabar yuborishda xato")
        return

    # ADMIN: kino captioni
    if state == "add_movie_caption" and is_admin(user_id):
        parts = data.split("|", 2)
        if len(parts) != 3:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Kino ma’lumoti buzilgan.")
            return

        movie_type, file_type, file_id = parts
        title = text.splitlines()[0].strip()
        title = re.sub(r"^[🎬\s]+", "", title).strip() or "Nomsiz kino"

        set_state(
            user_id,
            "add_movie_code",
            f"{movie_type}|{file_type}|{file_id}|{title}|{text}",
        )
        bot.send_message(message.chat.id, "🔢 Kino uchun unikal kod yuboring:")
        return

    # ADMIN: kino kodi
    if state == "add_movie_code" and is_admin(user_id):
        code = text.replace(" ", "")

        if len(code) > 30:
            bot.send_message(message.chat.id, "❌ Kod juda uzun.")
            return

        existing = execute(
            "SELECT title FROM movies WHERE code=?",
            (code,),
            fetchone=True,
        )

        if existing:
            bot.send_message(
                message.chat.id,
                f"❌ Bu kod band.\n\n"
                f"Kino: <b>{safe(existing['title'])}</b>\n"
                "Boshqa kod yuboring.",
            )
            return

        parts = data.split("|", 4)
        if len(parts) != 5:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Kino ma’lumoti buzilgan.")
            return

        movie_type, file_type, file_id, title, caption = parts

        execute("""
            INSERT INTO movies(
                code, title, caption, file_id, file_type,
                is_premium, created_at, added_by
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code,
            title,
            caption,
            file_id,
            file_type,
            int(movie_type),
            now_text(),
            user_id,
        ))

        clear_state(user_id)
        type_name = "👑 Premium kino" if int(movie_type) else "🆓 Oddiy kino"

        bot.send_message(
            message.chat.id,
            f"✅ <b>Kino muvaffaqiyatli qo‘shildi.</b>\n\n"
            f"🎬 {safe(title)}\n"
            f"🔢 Kod: <code>{safe(code)}</code>\n"
            f"Turi: {type_name}",
            reply_markup=admin_keyboard(),
        )
        return

    # ADMIN: kino o‘chirish kodi
    if state == "delete_movie_code" and is_admin(user_id):
        movie = execute(
            "SELECT * FROM movies WHERE code=?",
            (text,),
            fetchone=True,
        )
        clear_state(user_id)

        if not movie:
            bot.send_message(
                message.chat.id,
                "❌ Bunday kodli kino topilmadi.",
                reply_markup=admin_keyboard(),
            )
            return

        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton(
                "✅ Ha, o‘chirilsin",
                callback_data=f"delete_movie_confirm:{movie['id']}",
            ),
            types.InlineKeyboardButton(
                "❌ Bekor qilish",
                callback_data="delete_movie_cancel",
            ),
        )

        bot.send_message(
            message.chat.id,
            "🗑 <b>Kino o‘chirilsinmi?</b>\n\n"
            f"🎬 {safe(movie['title'])}\n"
            f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
            f"👁 Ko‘rishlar: {movie['views']}",
            reply_markup=keyboard,
        )
        return

    # ADMIN: to‘lovni rad etish
    if state == "payment_reject_reason" and is_admin(user_id):
        try:
            payment_id = int(data)
        except ValueError:
            clear_state(user_id)
            return

        payment = execute(
            "SELECT * FROM payments WHERE id=? AND status='pending'",
            (payment_id,),
            fetchone=True,
        )

        if not payment:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ To‘lov topilmadi.")
            return

        execute("""
            UPDATE payments
            SET status='rejected', admin_note=?, reviewed_at=?, reviewed_by=?
            WHERE id=?
        """, (text, now_text(), user_id, payment_id))

        clear_state(user_id)
        bot.send_message(message.chat.id, "❌ To‘lov rad etildi.")

        try:
            bot.send_message(
                payment["user_id"],
                "❌ <b>To‘lovingiz tasdiqlanmadi.</b>\n\n"
                f"Sabab: {safe(text)}\n\n"
                "Kerak bo‘lsa, qayta chek yuboring.",
                reply_markup=main_keyboard(payment["user_id"]),
            )
        except Exception:
            logging.exception("Rad etish xabarida xato")
        return

    # ADMIN: user ID boshqarish
    if state == "manage_user_id" and is_admin(user_id):
        try:
            target_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Faqat raqamli Telegram ID yuboring.")
            return

        target = get_user(target_id)
        clear_state(user_id)

        if not target:
            bot.send_message(message.chat.id, "❌ Foydalanuvchi bazada topilmadi.")
            return

        premium_status = "Ha" if premium_active(target_id) else "Yo‘q"
        target = get_user(target_id)

        bot.send_message(
            message.chat.id,
            "👤 <b>FOYDALANUVCHI</b>\n\n"
            f"Ism: {safe(target['full_name'])}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Username: {safe(target['username'] or 'Yo‘q')}\n"
            f"Premium: <b>{premium_status}</b>\n"
            f"Tugash sanasi: {safe(target['premium_until'] or '—')}\n"
            f"Bloklangan: {'Ha' if target['is_blocked'] else 'Yo‘q'}",
            reply_markup=user_manage_keyboard(target_id),
        )
        return

    # ADMIN: premium kun qo‘shish
    if state == "user_add_days_value" and is_admin(user_id):
        try:
            target_id = int(data)
            days = int(text)
            if days <= 0 or days > 10000:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, "❌ 1 dan 10000 gacha kun kiriting.")
            return

        expiry = add_premium_days(target_id, days)
        clear_state(user_id)

        if not expiry:
            bot.send_message(message.chat.id, "❌ User topilmadi.")
            return

        bot.send_message(
            message.chat.id,
            f"✅ <code>{target_id}</code> foydalanuvchiga "
            f"<b>{days} kun</b> premium qo‘shildi.\n"
            f"Tugash sanasi: <b>{expiry.strftime('%d.%m.%Y %H:%M')}</b>",
        )

        try:
            bot.send_message(
                target_id,
                f"👑 Sizga admin tomonidan <b>{days} kun</b> premium qo‘shildi.",
            )
        except Exception:
            pass
        return

    # ADMIN: userga xabar
    if state == "reply_to_user" and is_admin(user_id):
        try:
            target_id = int(data)
        except ValueError:
            clear_state(user_id)
            return

        try:
            bot.send_message(
                target_id,
                f"💬 <b>Admin javobi:</b>\n\n{safe(text)}",
            )
            bot.send_message(message.chat.id, "✅ Xabar foydalanuvchiga yuborildi.")
        except Exception as error:
            bot.send_message(
                message.chat.id,
                f"❌ Xabar yuborilmadi: {safe(error)}",
            )

        clear_state(user_id)
        return

    # ADMIN: kanal username
    if state == "channel_add_username" and is_admin(user_id):
        username = normalize_channel_username(text)

        try:
            chat = bot.get_chat(username)
        except Exception:
            bot.send_message(
                message.chat.id,
                "❌ Kanal topilmadi.\n"
                "Botni kanalga admin qiling va username’ni qayta yuboring.",
            )
            return

        title = chat.title or username
        invite_link = f"https://t.me/{username.lstrip('@')}"

        set_state(
            user_id,
            "channel_add_link",
            f"{username}|{title}",
        )

        bot.send_message(
            message.chat.id,
            f"📢 Kanal topildi: <b>{safe(title)}</b>\n\n"
            "Kanalga kirish havolasini yuboring.\n"
            f"Ochiq kanal bo‘lsa: <code>{safe(invite_link)}</code>",
        )
        return

    # ADMIN: kanal linki
    if state == "channel_add_link" and is_admin(user_id):
        try:
            username, title = data.split("|", 1)
        except ValueError:
            clear_state(user_id)
            return

        invite_link = text.strip()

        if not (
            invite_link.startswith("https://t.me/")
            or invite_link.startswith("http://t.me/")
        ):
            bot.send_message(message.chat.id, "❌ Telegram kanal havolasini yuboring.")
            return

        try:
            execute("""
                INSERT INTO channels(title, username, invite_link)
                VALUES(?, ?, ?)
            """, (title, username, invite_link))
        except sqlite3.IntegrityError:
            bot.send_message(message.chat.id, "❌ Bu kanal avval qo‘shilgan.")
            clear_state(user_id)
            return

        clear_state(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ <b>{safe(title)}</b> majburiy obunaga qo‘shildi.",
            reply_markup=admin_keyboard(),
        )
        return

    # ADMIN: kanal o‘chirish
    if state == "channel_delete_id" and is_admin(user_id):
        try:
            channel_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Kanal ID raqam bo‘lishi kerak.")
            return

        channel = execute(
            "SELECT * FROM channels WHERE id=?",
            (channel_id,),
            fetchone=True,
        )

        if not channel:
            bot.send_message(message.chat.id, "❌ Kanal topilmadi.")
            return

        execute("DELETE FROM channels WHERE id=?", (channel_id,))
        clear_state(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ <b>{safe(channel['title'])}</b> o‘chirildi.",
            reply_markup=admin_keyboard(),
        )
        return

    # ADMIN: karta
    if state == "change_card_number" and is_admin(user_id):
        card = normalize_card(text)

        if not card:
            bot.send_message(message.chat.id, "❌ Karta raqami aynan 16 ta raqam bo‘lsin.")
            return

        set_setting("card_number", re.sub(r"\D", "", card))
        clear_state(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Karta raqami yangilandi:\n<code>{card}</code>",
        )
        return

    if state == "change_card_owner" and is_admin(user_id):
        set_setting("card_owner", text)
        clear_state(user_id)
        bot.send_message(message.chat.id, "✅ Karta egasi yangilandi.")
        return

    if state == "change_payment_note" and is_admin(user_id):
        set_setting("payment_note", text)
        clear_state(user_id)
        bot.send_message(message.chat.id, "✅ To‘lov izohi yangilandi.")
        return

    bot.send_message(
        message.chat.id,
        "❌ Ushbu amal uchun noto‘g‘ri ma’lumot yuborildi.\n"
        "Bekor qilish: /cancel",
    )


# =========================================================
# REKLAMA UCHUN BARCHA KONTENT TURLARI
# =========================================================

@bot.message_handler(
    content_types=["text", "photo", "video", "document", "animation"],
    func=lambda message: (
        is_admin(message.from_user.id)
        and get_state(message.from_user.id)[0] == "broadcast_content"
    ),
)
def broadcast_content_handler(message):
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "📢 Reklama qabul qilindi. Yuborish boshlanmoqda...",
    )
    broadcast_copy(message.chat.id, message.message_id)


# =========================================================
# PREMIUM MUDDATINI TEKSHIRISH
# =========================================================

def premium_expiry_worker():
    while True:
        try:
            rows = execute("""
                SELECT user_id, premium_until
                FROM users
                WHERE is_premium=1 AND premium_until IS NOT NULL
            """, fetchall=True)

            now = datetime.now()

            for row in rows:
                try:
                    expiry = datetime.strptime(
                        row["premium_until"],
                        "%Y-%m-%d %H:%M:%S",
                    )
                except ValueError:
                    continue

                if expiry <= now:
                    remove_premium(row["user_id"])
                    try:
                        bot.send_message(
                            row["user_id"],
                            "❌ <b>Premium obunangiz muddati tugadi.</b>\n\n"
                            "Davom ettirish uchun premium tarifni yangilang.",
                            reply_markup=main_keyboard(row["user_id"]),
                        )
                    except Exception:
                        pass

        except Exception:
            logging.exception("Premium tekshiruv worker xatosi")

        time.sleep(3600)


# =========================================================
# ISHGA TUSHIRISH
# =========================================================
# =========================================================
# FLASK WEBHOOK — RENDER
# =========================================================

app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN.split(':', 1)[0]}"
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")


@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "premium-kino-bot",
        "mode": "flask-webhook",
        "webhook_path": WEBHOOK_PATH,
    })


@app.get("/health")
def health_check():
    return jsonify({"ok": True}), 200


@app.post(WEBHOOK_PATH)
def telegram_webhook():
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return jsonify({"ok": False, "error": "invalid content-type"}), 415

    try:
        update_json = request.get_json(force=True)
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
        return jsonify({"ok": True}), 200
    except Exception:
        logging.exception("Webhook update qayta ishlashda xato")
        return jsonify({"ok": False}), 500


def configure_webhook():
    init_db()

    if not RENDER_EXTERNAL_URL:
        logging.warning(
            "RENDER_EXTERNAL_URL topilmadi. Render Web Service deploy bo‘lganda avtomatik beriladi."
        )
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

    try:
        bot.remove_webhook()
        time.sleep(0.5)
        result = bot.set_webhook(
            url=webhook_url,
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
                "chat_member",
            ],
            drop_pending_updates=True,
        )
        logging.info("Webhook o‘rnatildi: %s | result=%s", webhook_url, result)
    except Exception:
        logging.exception("Webhook o‘rnatishda xato")


configure_webhook()

threading.Thread(
    target=premium_expiry_worker,
    daemon=True,
).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
