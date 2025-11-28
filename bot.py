#!/usr/bin/env python3
"""
Telegram a’zolik tekshiruvi va reklama filtrlovchi bot (python-telegram-bot v20+)

Xususiyatlar:
 - Guruh uchun majburiy kanal/guruhlar ro‘yxati.
 - Foydalanuvchi majburiy kanallarga a’zo bo‘lmasa — xabari o‘chiriladi.
 - Reklama, havolalar, t.me linklar va taqiqlangan so‘zlarni avtomatik aniqlab o‘chiradi.
 - Administratorlar bundan mustasno.
 - Barcha sozlamalar SQLite bazasida saqlanadi.
 - Administrator buyruqlari orqali sozlanadi.
"""

import asyncio
import logging
import re
import sqlite3
from typing import List, Optional, Tuple

from telegram import __version__ as TG_VER

try:
    from telegram import (
        BotCommand,
        ChatMember,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
        constants,
    )
    from telegram.error import TelegramError
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        ChatMemberHandler,
        CallbackQueryHandler,
        filters,
    )
except Exception as e:
    raise RuntimeError(
        "Ushbu skript python-telegram-bot v20+ talab qiladi. O‘rnatish: pip install python-telegram-bot --upgrade"
    ) from e


# ---------------------------
# Asosiy sozlamalar
# ---------------------------

import os
BOT_TOKEN = os.environ.get("BOT_TOKEN")


GLOBAL_ADMINS = []  # global adminlar ro‘yxati (ixtiyoriy)

DEFAULT_BANNED_KEYWORDS = [
    "promo", "promotion", "discount", "bet", "casino", "followers",
    "free followers", "giveaway", "click here", "subscribe",
    "earn", "work from home"
]

DEFAULT_ENFORCE_MEMBERSHIP = True
DEFAULT_ENFORCE_ADBLOCK = True

DB_PATH = "bot_settings.db"
LOG_LEVEL = logging.INFO


# ---------------------------
# Logging
# ---------------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=LOG_LEVEL
)
logger = logging.getLogger(__name__)


# ---------------------------
# Ma’lumotlar bazasi (SQLite)
# ---------------------------
# Jadval: groups
#   - group_id: integer (PRIMARY KEY)
#   - required_channels: majburiy kanallar ("," bilan ajratilgan)
#   - banned_keywords: taqiqlangan so‘zlar
#   - enforce_membership: a’zolik tekshiruvi (0/1)
#   - enforce_adblock: reklamani bloklash (0/1)
#   - join_button_text: tugma matni
#   - override_message: maxsus matn (ixtiyoriy)
#
# Jadval: pending_join_msgs
#   - user_id: foydalanuvchi ID
#   - group_id: guruh ID
#   - chat_id: xabar qaysi chatga yuborilgan
#   - message_id: yuborilgan xabar ID
#
# Ushbu jadval join-subscribtion xabarlari keyin o‘chirilishi uchun kerak.

class DB:
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        c = self.conn.cursor()

        # Guruh sozlamalari jadvali
        c.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                required_channels TEXT,
                banned_keywords TEXT,
                enforce_membership INTEGER DEFAULT 1,
                enforce_adblock INTEGER DEFAULT 1,
                join_button_text TEXT DEFAULT 'Kanalga a’zo bo‘ling',
                override_message TEXT DEFAULT 'Iltimos, majburiy kanalga a’zo bo‘ling.'
            )
        """)

        # Pending join xabarlari (keyin o‘chiriladigan)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_join_msgs (
                user_id INTEGER,
                group_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER
            )
        """)

        self.conn.commit()

    # --- Guruh sozlamalari funksiyalari ---

    def get_group(self, group_id: int) -> Optional[dict]:
        c = self.conn.cursor()
        c.execute("""
            SELECT required_channels, banned_keywords, enforce_membership,
                   enforce_adblock, join_button_text, override_message
            FROM groups
            WHERE group_id = ?
        """, (group_id,))
        row = c.fetchone()

        if not row:
            return None

        return {
            "required_channels": row[0] or "",
            "banned_keywords": row[1] or "",
            "enforce_membership": bool(row[2]),
            "enforce_adblock": bool(row[3]),
            "join_button_text": row[4] or "Kanalga a’zo bo‘ling",
            "override_message": row[5] or "",
        }

    def ensure_group(self, group_id: int):
        if self.get_group(group_id) is None:
            c = self.conn.cursor()
            c.execute("""
                INSERT INTO groups (group_id, required_channels, banned_keywords,
                    enforce_membership, enforce_adblock)
                VALUES (?, ?, ?, ?, ?)
            """, (
                group_id,
                "",
                ",".join(DEFAULT_BANNED_KEYWORDS),
                1 if DEFAULT_ENFORCE_MEMBERSHIP else 0,
                1 if DEFAULT_ENFORCE_ADBLOCK else 0,
            ))
            self.conn.commit()

    def set_required_channels(self, group_id: int, channels: List[str]):
        self.ensure_group(group_id)
        c = self.conn.cursor()
        c.execute(
            "UPDATE groups SET required_channels = ? WHERE group_id = ?",
            (",".join(channels), group_id)
        )
        self.conn.commit()

    def get_required_channels(self, group_id: int) -> List[str]:
        g = self.get_group(group_id)
        if not g or not g["required_channels"]:
            return []
        return [s.strip() for s in g["required_channels"].split(",") if s.strip()]

    def set_banned_keywords(self, group_id: int, keywords: List[str]):
        self.ensure_group(group_id)
        c = self.conn.cursor()
        c.execute("""
            UPDATE groups SET banned_keywords = ?
            WHERE group_id = ?
        """, (",".join(keywords), group_id))
        self.conn.commit()

    def get_banned_keywords(self, group_id: int) -> List[str]:
        g = self.get_group(group_id)
        if not g or not g["banned_keywords"]:
            return DEFAULT_BANNED_KEYWORDS.copy()
        return [s.strip() for s in g["banned_keywords"].split(",") if s.strip()]

    def set_enforce_membership(self, group_id: int, value: bool):
        self.ensure_group(group_id)
        c = self.conn.cursor()
        c.execute("""
            UPDATE groups SET enforce_membership = ?
            WHERE group_id = ?
        """, (1 if value else 0, group_id))
        self.conn.commit()

    def set_enforce_adblock(self, group_id: int, value: bool):
        self.ensure_group(group_id)
        c = self.conn.cursor()
        c.execute("""
            UPDATE groups SET enforce_adblock = ?
            WHERE group_id = ?
        """, (1 if value else 0, group_id))
        self.conn.commit()

    # --- Pending join xabarlarini boshqarish ---

    def save_join_message(self, user_id: int, group_id: int, chat_id: int, message_id: int):
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO pending_join_msgs (user_id, group_id, chat_id, message_id)
            VALUES (?, ?, ?, ?)
        """, (user_id, group_id, chat_id, message_id))
        self.conn.commit()

    def get_join_messages(self, user_id: int, group_id: int) -> List[Tuple[int, int]]:
        c = self.conn.cursor()
        c.execute("""
            SELECT chat_id, message_id FROM pending_join_msgs
            WHERE user_id = ? AND group_id = ?
        """, (user_id, group_id))
        return [(r[0], r[1]) for r in c.fetchall()]

    def delete_join_messages(self, user_id: int, group_id: int):
        c = self.conn.cursor()
        c.execute("""
            DELETE FROM pending_join_msgs
            WHERE user_id = ? AND group_id = ?
        """, (user_id, group_id))
        self.conn.commit()

    def get_pending_groups_for_user(self, user_id: int) -> List[int]:
        c = self.conn.cursor()
        c.execute("""
            SELECT DISTINCT group_id FROM pending_join_msgs
            WHERE user_id = ?
        """, (user_id,))
        return [r[0] for r in c.fetchall()]


db = DB(DB_PATH)


# ---------------------------
# Foydali funksiyalar
# ---------------------------

URL_REGEX = re.compile(
    r"(https?://[^\s]+)|"
    r"(www\.[^\s]+)|"
    r"(t\.me/[^\s]+)|"
    r"([^\s]+\.[a-z]{2,})",
    re.IGNORECASE,
)

def contains_url(text: str) -> bool:
    return bool(URL_REGEX.search(text))

def contains_tme_link(text: str) -> bool:
    return "t.me/" in text.lower()

def contains_banned_keyword(text: str, banned_keywords: List[str]) -> Optional[str]:
    text_l = text.lower()
    for kw in banned_keywords:
        if kw.lower() in text_l:
            return kw
    return None

async def is_user_admin_or_owner(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except TelegramError:
        return False

async def user_is_member_of_channel(bot, user_id: int, channel_ident: str) -> Optional[bool]:
    try:
        member = await bot.get_chat_member(chat_id=channel_ident, user_id=user_id)
        return member.status in (
            ChatMember.OWNER,
            ChatMember.ADMINISTRATOR,
            ChatMember.MEMBER,
            ChatMember.RESTRICTED,
        )
    except TelegramError:
        return None

def mention_html(user):
    if user.username:
        return f"@{user.username}"
    return f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
# ---------------------------
# /start — asosiy xabar
# ---------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Assalomu alaykum.\n\n"
        "Ushbu bot guruhingizda quyidagi vazifalarni bajaradi:\n"
        "• Majburiy kanallarga a’zolikni tekshiradi.\n"
        "• A’zo bo‘lmagan foydalanuvchilarning xabarlarini o‘chiradi.\n"
        "• Reklama, havolalar va taqiqlangan so‘zlarni avtomatik aniqlab bloklaydi.\n\n"
        "Botni guruhga administrator sifatida qo‘shing va buyruqlar orqali sozlashingiz mumkin."
    )
    await update.message.reply_text(text)


# ---------------------------
# /help — yordam
# ---------------------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Bot bo‘yicha yordam:\n\n"
        "/setchannel — Guruh uchun majburiy kanallarni belgilash.\n"
        "   Format: /setchannel @kanal1, @kanal2\n\n"
        "/setkeywords — Taqiqlangan so‘zlar ro‘yxatini o‘rnatish.\n"
        "   Format: /setkeywords so‘z1, so‘z2\n\n"
        "/enable_membership — A’zolik tekshiruvini yoqish.\n"
        "/disable_membership — A’zolik tekshiruvini o‘chirish.\n\n"
        "/enable_adblock — Reklama filtrini yoqish.\n"
        "/disable_adblock — Reklama filtrini o‘chirish.\n\n"
        "/listsettings — Ushbu guruhdagi barcha joriy sozlamalarni ko‘rsatish.\n\n"
        "Barcha buyruqlarni faqat guruh administratorlari bajarishi mumkin."
    )
    await update.message.reply_text(text)


# ---------------------------
# Faqat adminlar uchun tekshiruv
# ---------------------------
async def admin_required(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False

    if user.id in GLOBAL_ADMINS:
        return True

    try:
        member = await update.get_bot().get_chat_member(chat.id, user.id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except TelegramError:
        return False


# ---------------------------
# /setchannel — majburiy kanallarni o‘rnatish
# ---------------------------
async def setchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text(
            "❌ Ushbu buyruqni faqat guruh administratorlari bajarishi mumkin."
        )
        return

    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "❌ Ushbu buyruq faqat guruh ichida ishlaydi."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Iltimos, majburiy kanallar ro‘yxatini kiriting.\n"
            "Masalan: /setchannel @kanal1, @kanal2"
        )
        return

    raw = " ".join(context.args)
    channels = [c.strip() for c in raw.split(",") if c.strip()]

    db.set_required_channels(chat.id, channels)

    await update.message.reply_text(
        "✅ Majburiy kanallar muvaffaqiyatli o‘rnatildi:\n" +
        "\n".join(channels)
    )


# ---------------------------
# /setkeywords — taqiqlangan so‘zlarni o‘rnatish
# ---------------------------
async def setkeywords_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text(
            "❌ Ushbu buyruqni faqat guruh administratorlari bajarishi mumkin."
        )
        return

    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "❌ Ushbu buyruq faqat guruh ichida ishlaydi."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Iltimos, taqiqlangan so‘zlar ro‘yxatini kiriting.\n"
            "Masalan: /setkeywords so‘z1, so‘z2, so‘z3"
        )
        return

    raw = " ".join(context.args)
    kws = [w.strip() for w in raw.split(",") if w.strip()]

    db.set_banned_keywords(chat.id, kws)

    await update.message.reply_text(
        "✅ Taqiqlangan so‘zlar muvaffaqiyatli o‘rnatildi:\n" +
        ", ".join(kws)
    )


# ---------------------------
# /enable_membership /disable_membership
# ---------------------------
async def enable_membership_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text("❌ Faqat administratorlar uchun.")
        return

    chat = update.effective_chat
    db.set_enforce_membership(chat.id, True)

    await update.message.reply_text(
        "✅ A’zolik tekshiruvi yoqildi.\n"
        "Endi majburiy kanallarga a’zo bo‘lmagan foydalanuvchilar xabarlari o‘chiriladi."
    )


async def disable_membership_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text("❌ Faqat administratorlar uchun.")
        return

    chat = update.effective_chat
    db.set_enforce_membership(chat.id, False)

    await update.message.reply_text(
        "🚫 A’zolik tekshiruvi o‘chirildi."
    )


# ---------------------------
# /enable_adblock /disable_adblock
# ---------------------------
async def enable_adblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text("❌ Faqat administratorlar uchun.")
        return

    chat = update.effective_chat
    db.set_enforce_adblock(chat.id, True)

    await update.message.reply_text(
        "✅ Reklama filtri yoqildi."
    )


async def disable_adblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text("❌ Faqat administratorlar uchun.")
        return

    chat = update.effective_chat
    db.set_enforce_adblock(chat.id, False)

    await update.message.reply_text(
        "🚫 Reklama filtri o‘chirildi."
    )


# ---------------------------
# /listsettings — Guruh sozlamalarini ko‘rsatish
# ---------------------------
async def listsettings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        await update.message.reply_text("❌ Faqat administratorlar uchun.")
        return

    chat = update.effective_chat
    g = db.get_group(chat.id)

    if not g:
        await update.message.reply_text(
            "Sozlamalar mavjud emas. Bot ushbu guruh uchun hali sozlanmagan."
        )
        return

    text = (
        "📌 *Guruh sozlamalari:*\n\n"
        f"*Majburiy kanallar:* {g['required_channels'] or '—'}\n"
        f"*Taqiqlangan so‘zlar:* {g['banned_keywords']}\n"
        f"*A’zolik tekshiruvi:* {'Yoqilgan' if g['enforce_membership'] else 'O‘chirilgan'}\n"
        f"*Reklama filtri:* {'Yoqilgan' if g['enforce_adblock'] else 'O‘chirilgan'}\n"
    )

    await update.message.reply_text(text, parse_mode="Markdown")
# -----------------------------------------
# A’zolik tekshiruvi va reklama filtri
# -----------------------------------------

async def membership_and_adblock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg = update.message
    chat = msg.chat
    user = msg.from_user

    # Guruhda ishlaydi
    if chat.type not in ("group", "supergroup"):
        return

    # Guruh sozlamalarini olish
    db.ensure_group(chat.id)
    g = db.get_group(chat.id)

    required_channels = db.get_required_channels(chat.id)
    banned_keywords = db.get_banned_keywords(chat.id)

    enforce_membership = g["enforce_membership"]
    enforce_adblock = g["enforce_adblock"]

    # Adminlar mustasno
    if await is_user_admin_or_owner(context.bot, chat.id, user.id):
        return

    # Reklama filtri — URL, t.me, so‘zlar
    if enforce_adblock:
        text = msg.text or msg.caption or ""

        if contains_url(text) or contains_tme_link(text):
            try:
                await msg.delete()
            except:
                pass

            await chat.send_message(
                f"❗ Hurmatli foydalanuvchi {mention_html(user)}, guruhda reklama yoki havola yuborish taqiqlangan.",
                parse_mode=constants.ParseMode.HTML
            )
            return

        bad_kw = contains_banned_keyword(text, banned_keywords)
        if bad_kw:
            try:
                await msg.delete()
            except:
                pass

            await chat.send_message(
                f"❗ Hurmatli foydalanuvchi {mention_html(user)}, xabaringizda taqiqlangan so‘z aniqlandi: <b>{bad_kw}</b>.",
                parse_mode=constants.ParseMode.HTML
            )
            return

    # A’zolik tekshiruvini o‘chirgan bo‘lsa — qaytamiz
    if not enforce_membership or not required_channels:
        return

    # Foydalanuvchi majburiy kanallarga a’zo bo‘lganligini tekshirish
    not_member_channels = []

    for ch in required_channels:
        res = await user_is_member_of_channel(context.bot, user.id, ch)
        if not res:
            not_member_channels.append(ch)

    if not not_member_channels:
        return  # hammasiga a’zo bo‘lgan

    # ❗ Foydalanuvchi a’zo emas — xabarni o‘chirish
    try:
        await msg.delete()
    except:
        pass

    # JOIN TUGMA
    buttons = [
        [InlineKeyboardButton(g["join_button_text"], url=f"https://t.me/{c.replace('@', '')}")]
        for c in not_member_channels
    ]
    kb = InlineKeyboardMarkup(buttons)

    notify_text = (
        f"❗ Hurmatli {mention_html(user)},\n\n"
        f"Guruhda xabar yuborishdan oldin iltimos quyidagi kanallarga a’zo bo‘ling.\n"
        f"A’zo bo‘lgach, bu ogohlantirish xabari avtomatik o‘chiriladi."
    )

    sent = await context.bot.send_message(
        chat_id=chat.id,
        text=notify_text,
        reply_markup=kb,
        parse_mode=constants.ParseMode.HTML,
    )

    db.save_join_message(user.id, chat.id, chat.id, sent.message_id)

    # DM orqali ogohlantirish
    dm_text = (
        "Hurmatli foydalanuvchi,\n\n"
        "Siz guruhda xabar yuborishdan oldin majburiy kanallarga a’zo bo‘lishingiz kerak.\n"
        "Iltimos, guruhdagi havolalar orqali kanalga a’zo bo‘ling."
    )

    try:
        dm_sent = await context.bot.send_message(
            chat_id=user.id,
            text=dm_text
        )
        db.save_join_message(user.id, chat.id, user.id, dm_sent.message_id)
    except:
        pass
# -----------------------------------------
# A’zolikni fon rejimida tekshiruvchi funksiya
# (Har 5 soniyada bir marta tekshiradi)
# -----------------------------------------

async def background_membership_checker(application):
    bot = application.bot

    while True:
        await asyncio.sleep(5)  # 5 soniya kutish

        try:
            # Foydalanuvchining barcha kutayotgan (pending) guruhlarini olish
            c = db.conn.cursor()
            c.execute("SELECT DISTINCT user_id FROM pending_join_msgs")
            users = [r[0] for r in c.fetchall()]

            for user_id in users:
                # Ushbu foydalanuvchi uchun barcha guruhlar
                group_ids = db.get_pending_groups_for_user(user_id)

                for group_id in group_ids:
                    required_channels = db.get_required_channels(group_id)
                    if not required_channels:
                        continue

                    # Foydalanuvchi hamma kanallarga a'zo bo‘lganmi?
                    fully_joined = True
                    for ch in required_channels:
                        res = await user_is_member_of_channel(bot, user_id, ch)
                        if not res:
                            fully_joined = False
                            break

                    if not fully_joined:
                        continue  # hali ham a’zo emas

                    # ❗ A’zo bo‘lgan — endi xabarlarni o‘chiramiz
                    join_msgs = db.get_join_messages(user_id, group_id)

                    for chat_id, message_id in join_msgs:
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=message_id)
                        except:
                            pass

                    # Ma’lumotlar bazasidan tozalash
                    db.delete_join_messages(user_id, group_id)

        except Exception as e:
            logger.error(f"Xatolik (background_membership_checker): {e}")
            continue


# -----------------------------------------
# Botni ishga tushirish — MAIN()
# -----------------------------------------

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Buyruqlar
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    application.add_handler(CommandHandler("setchannel", setchannel_cmd))
    application.add_handler(CommandHandler("setkeywords", setkeywords_cmd))
    application.add_handler(CommandHandler("enable_membership", enable_membership_cmd))
    application.add_handler(CommandHandler("disable_membership", disable_membership_cmd))
    application.add_handler(CommandHandler("enable_adblock", enable_adblock_cmd))
    application.add_handler(CommandHandler("disable_adblock", disable_adblock_cmd))
    application.add_handler(CommandHandler("listsettings", listsettings_cmd))

    # Xabarlar uchun asosiy handler
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            membership_and_adblock_handler
        )
    )

    # Fon ishchi vazifa — har 5 soniyada tekshiradi
    application.job_queue.run_repeating(
        lambda context: asyncio.create_task(background_membership_checker(application)),
        interval=5,
        first=5
    )

    print("Bot ishga tushirildi...")

    application.run_polling()


if __name__ == "__main__":
    main()
