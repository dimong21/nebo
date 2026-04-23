import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

import sqlite3
import json
import re
import random

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
REVIEWS_CHAT_ID = int(os.getenv("REVIEWS_CHAT_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
BOT_REVIEWS_LINK = os.getenv("BOT_REVIEWS_LINK", "https://t.me/bot_reviews")

# Состояния
WAITING_MAILING_MESSAGE = 1
WAITING_MAILING_CONFIRM = 2
WAITING_REVIEW_TEXT = 3

# Мотивирующие фразы
LEVEL_UP_MESSAGES = [
    "🌟 Ты растёшь! Твоя работа замечена и оценена по достоинству!",
    "💪 Отличная работа! Ты становишься сильнее с каждым днём!",
    "🚀 Твои усилия приносят плоды! Продолжай в том же духе!",
    "⭐ Ты превзошёл ожидания! Сияние Неба гордится тобой!",
    "🔥 Твоя преданность делу вдохновляет! Так держать!",
    "💎 Ты — настоящая находка для команды! Блестящая работа!",
    "🎯 Ты точно идёшь к цели! Уверенный шаг вперёд!",
    "🏆 Ты достигаешь новых высот! Команда ценит тебя!",
    "🌈 Твой труд делает мир светлее! Продолжай сиять!",
    "🦅 Ты паришь над задачами! Впечатляющий прогресс!",
]

class Database:
    def __init__(self, db_path="bot.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.init_tables()

    def init_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT 0,
                ban_until TIMESTAMP,
                is_muted BOOLEAN DEFAULT 0,
                mute_until TIMESTAMP,
                mute_category TEXT
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT DEFAULT 'Администратор',
                position TEXT DEFAULT 'Администратор',
                level INTEGER DEFAULT 1,
                permissions TEXT DEFAULT '[]',
                departments TEXT DEFAULT '["chat", "support", "other"]',
                rating REAL DEFAULT 5.0,
                total_reviews INTEGER DEFAULT 0,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                category TEXT,
                status TEXT DEFAULT 'open',
                admin_id INTEGER,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS appeal_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appeal_id INTEGER,
                user_id INTEGER,
                message_text TEXT,
                message_type TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (appeal_id) REFERENCES appeals (appeal_id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                is_anonymous BOOLEAN DEFAULT 0,
                category TEXT,
                admin_id INTEGER,
                rating INTEGER,
                text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS sys_bans (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                reason TEXT,
                ban_type TEXT,
                banned_until TIMESTAMP,
                banned_by INTEGER,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                date DATE,
                appeals_taken INTEGER DEFAULT 0,
                messages_sent INTEGER DEFAULT 0
            )
        ''')

        self.conn.commit()

    def add_user(self, user_id: int, username: str, first_name: str, last_name: str = ""):
        self.cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        self.conn.commit()

    def get_user_by_username(self, username: str) -> Optional[int]:
        self.cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
        result = self.cursor.fetchone()
        return result[0] if result else None

    def is_banned(self, user_id: int) -> bool:
        self.cursor.execute('SELECT is_banned, ban_until FROM users WHERE user_id = ?', (user_id,))
        result = self.cursor.fetchone()
        if not result:
            return False
        if result[0]:
            if result[1]:
                ban_until = datetime.fromisoformat(result[1])
                if ban_until < datetime.now():
                    self.cursor.execute('UPDATE users SET is_banned = 0, ban_until = NULL WHERE user_id = ?', (user_id,))
                    self.conn.commit()
                    return False
            return True
        return False

    def is_muted(self, user_id: int) -> bool:
        self.cursor.execute('SELECT is_muted, mute_until FROM users WHERE user_id = ?', (user_id,))
        result = self.cursor.fetchone()
        if not result:
            return False
        if result[0]:
            if result[1]:
                mute_until = datetime.fromisoformat(result[1])
                if mute_until < datetime.now():
                    self.cursor.execute('UPDATE users SET is_muted = 0, mute_until = NULL, mute_category = NULL WHERE user_id = ?', (user_id,))
                    self.conn.commit()
                    return False
            return True
        return False

    def ban_user(self, user_id: int, username: str, reason: str, ban_type: str, until: Optional[datetime], banned_by: int):
        self.cursor.execute('''
            INSERT OR REPLACE INTO sys_bans (user_id, username, reason, ban_type, banned_until, banned_by)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, reason, ban_type, until.isoformat() if until else None, banned_by))
        if ban_type == "full":
            self.cursor.execute('UPDATE users SET is_banned = 1, ban_until = NULL WHERE user_id = ?', (user_id,))
        else:
            self.cursor.execute('UPDATE users SET is_banned = 1, ban_until = ? WHERE user_id = ?',
                              (until.isoformat() if until else None, user_id))
        self.conn.commit()

    def unban_user(self, user_id: int):
        self.cursor.execute('DELETE FROM sys_bans WHERE user_id = ?', (user_id,))
        self.cursor.execute('UPDATE users SET is_banned = 0, ban_until = NULL WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def mute_user(self, user_id: int, until: Optional[datetime], category: str = "", reason: str = ""):
        self.cursor.execute('UPDATE users SET is_muted = 1, mute_until = ?, mute_category = ? WHERE user_id = ?',
                          (until.isoformat() if until else None, category, user_id))
        self.conn.commit()

    def unmute_user(self, user_id: int):
        self.cursor.execute('UPDATE users SET is_muted = 0, mute_until = NULL, mute_category = NULL WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def add_admin(self, user_id: int, username: str, display_name: str, added_by: int):
        self.cursor.execute('''
            INSERT OR REPLACE INTO admins (user_id, username, display_name, added_by)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, display_name, added_by))
        self.conn.commit()

    def remove_admin(self, user_id: int):
        self.cursor.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def get_admin(self, user_id: int):
        self.cursor.execute('SELECT * FROM admins WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()

    def get_admin_by_username(self, username: str):
        self.cursor.execute('SELECT * FROM admins WHERE username = ?', (username,))
        return self.cursor.fetchone()

    def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        self.cursor.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone() is not None

    def get_admin_permissions(self, user_id: int) -> list:
        if user_id == OWNER_ID:
            return ["all"]
        self.cursor.execute('SELECT permissions FROM admins WHERE user_id = ?', (user_id,))
        result = self.cursor.fetchone()
        if result:
            perms = json.loads(result[0])
            return perms if perms else []
        return []

    def get_admin_departments(self, user_id: int) -> list:
        """Возвращает список отделов админа. Пустой список = нет доступа ни к чему."""
        if user_id == OWNER_ID:
            return ["chat", "support", "other"]
        self.cursor.execute('SELECT departments FROM admins WHERE user_id = ?', (user_id,))
        result = self.cursor.fetchone()
        if result:
            try:
                depts = json.loads(result[0])
                return depts if depts else []
            except:
                return []
        return []

    def has_permission(self, user_id: int, permission: str) -> bool:
        if user_id == OWNER_ID:
            return True
        perms = self.get_admin_permissions(user_id)
        return "all" in perms or permission in perms

    def can_handle_category(self, user_id: int, category: str) -> bool:
        """Проверяет, может ли админ работать с обращениями этой категории"""
        if user_id == OWNER_ID:
            return True
        depts = self.get_admin_departments(user_id)
        return "all" in depts or category in depts

    def get_all_admins(self):
        self.cursor.execute('SELECT user_id, username, display_name, position, level, rating, total_reviews FROM admins ORDER BY level DESC, rating DESC')
        return self.cursor.fetchall()

    def get_admins_for_category(self, category: str):
        """Возвращает админов, у которых есть доступ к категории"""
        admins = self.get_all_admins()
        result = []
        for admin in admins:
            if self.can_handle_category(admin[0], category):
                result.append(admin)
        return result

    def update_admin_position(self, user_id: int, position: str):
        self.cursor.execute('UPDATE admins SET position = ? WHERE user_id = ?', (position, user_id))
        self.conn.commit()

    def update_admin_level(self, user_id: int, level: int):
        self.cursor.execute('UPDATE admins SET level = ? WHERE user_id = ?', (level, user_id))
        self.conn.commit()

    def update_admin_permissions(self, user_id: int, permissions: list):
        self.cursor.execute('UPDATE admins SET permissions = ? WHERE user_id = ?', (json.dumps(permissions), user_id))
        self.conn.commit()

    def update_admin_departments(self, user_id: int, departments: list):
        self.cursor.execute('UPDATE admins SET departments = ? WHERE user_id = ?', (json.dumps(departments), user_id))
        self.conn.commit()

    def set_admin_full_perms(self, user_id: int):
        self.cursor.execute('UPDATE admins SET permissions = ?, departments = ?, level = 5 WHERE user_id = ?',
                          (json.dumps(["all"]), json.dumps(["chat", "support", "other"]), user_id))
        self.conn.commit()

    def create_appeal(self, user_id: int, username: str, first_name: str, category: str) -> int:
        self.cursor.execute('''
            INSERT INTO appeals (user_id, username, first_name, category)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, category))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_appeal(self, appeal_id: int):
        self.cursor.execute('SELECT * FROM appeals WHERE appeal_id = ?', (appeal_id,))
        return self.cursor.fetchone()

    def get_user_open_appeal(self, user_id: int):
        self.cursor.execute('''
            SELECT * FROM appeals WHERE user_id = ? AND status IN ('open', 'in_progress')
        ''', (user_id,))
        return self.cursor.fetchone()

    def get_open_appeals(self):
        self.cursor.execute('SELECT * FROM appeals WHERE status = "open" ORDER BY started_at')
        return self.cursor.fetchall()

    def take_appeal(self, appeal_id: int, admin_id: int):
        self.cursor.execute('''
            UPDATE appeals SET status = 'in_progress', admin_id = ? WHERE appeal_id = ? AND status = 'open'
        ''', (admin_id, appeal_id))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def close_appeal(self, appeal_id: int):
        self.cursor.execute('''
            UPDATE appeals SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE appeal_id = ?
        ''', (appeal_id,))
        self.conn.commit()

    def add_appeal_message(self, appeal_id: int, user_id: int, message_text: str, message_type: str = "text", file_id: str = None):
        self.cursor.execute('''
            INSERT INTO appeal_messages (appeal_id, user_id, message_text, message_type, file_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (appeal_id, user_id, message_text, message_type, file_id))
        self.conn.commit()

    def get_appeal_messages(self, appeal_id: int):
        self.cursor.execute('''
            SELECT * FROM appeal_messages WHERE appeal_id = ? ORDER BY created_at
        ''', (appeal_id,))
        return self.cursor.fetchall()

    def get_today_appeals_by_admin(self, admin_id: int):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT appeal_id, category FROM appeals WHERE admin_id = ? AND DATE(started_at) = ?
        ''', (admin_id, today))
        return self.cursor.fetchall()

    def add_review(self, user_id: int, username: str, display_name: str, is_anonymous: bool, 
                   category: str, admin_id: int, rating: int, text: str):
        self.cursor.execute('''
            INSERT INTO reviews (user_id, username, display_name, is_anonymous, category, admin_id, rating, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, display_name, is_anonymous, category, admin_id, rating, text))
        self.conn.commit()
        if admin_id:
            self.cursor.execute('''
                SELECT AVG(rating), COUNT(*) FROM reviews WHERE admin_id = ?
            ''', (admin_id,))
            avg, count = self.cursor.fetchone()
            self.cursor.execute('''
                UPDATE admins SET rating = ?, total_reviews = ? WHERE user_id = ?
            ''', (round(avg or 5.0, 1), count, admin_id))
            self.conn.commit()

    def increment_admin_stats(self, admin_id: int, stat_type: str):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            INSERT OR IGNORE INTO daily_stats (admin_id, date, appeals_taken, messages_sent)
            VALUES (?, ?, 0, 0)
        ''', (admin_id, today))
        if stat_type == "appeal":
            self.cursor.execute('''
                UPDATE daily_stats SET appeals_taken = appeals_taken + 1
                WHERE admin_id = ? AND date = ?
            ''', (admin_id, today))
        elif stat_type == "message":
            self.cursor.execute('''
                UPDATE daily_stats SET messages_sent = messages_sent + 1
                WHERE admin_id = ? AND date = ?
            ''', (admin_id, today))
        self.conn.commit()

    def get_all_users(self):
        self.cursor.execute('SELECT user_id FROM users WHERE is_banned = 0')
        return self.cursor.fetchall()

db = Database()

def owner_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != OWNER_ID:
            await update.message.reply_text("❌ Эта команда только для владельца!")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def parse_time(time_str: str) -> Optional[datetime]:
    match = re.match(r'(\d+)([mhd])', time_str.lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return datetime.now() + timedelta(minutes=value)
    elif unit == 'h':
        return datetime.now() + timedelta(hours=value)
    elif unit == 'd':
        return datetime.now() + timedelta(days=value)
    return None

def extract_user_id(text: str) -> Optional[int]:
    if not text:
        return None
    username = text.replace("@", "").strip()
    user_id = db.get_user_by_username(username)
    if user_id:
        return user_id
    admin = db.get_admin_by_username(username)
    if admin:
        return admin[0]
    return None

def notify_admins_by_category(context: ContextTypes.DEFAULT_TYPE, category: str, text: str, reply_markup=None):
    """Отправляет уведомление только админам с доступом к категории"""
    admins = db.get_admins_for_category(category)
    for admin in admins:
        try:
            context.bot.send_message(
                admin[0],
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin[0]}: {e}")

def get_main_menu_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("✨ Информация", callback_data="info")],
        [InlineKeyboardButton("🌸 Вызвать администратора", callback_data="call_admin")],
        [InlineKeyboardButton("🔧 Техподдержка", callback_data="tech_support")],
        [InlineKeyboardButton("📢 Telegram канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("⭐ Отзывы о боте", url=BOT_REVIEWS_LINK)]
    ]
    if db.is_admin(user_id):
        keyboard.append([InlineKeyboardButton("📨 Рассылка", callback_data="mailing_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_appeal_keyboard(appeal_id: int, is_taken: bool = False):
    if is_taken:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Войти в диалог", callback_data=f"enter_chat_{appeal_id}")],
            [InlineKeyboardButton("ℹ️ Информация", callback_data=f"appeal_info_{appeal_id}")],
            [InlineKeyboardButton("✅ Завершить", callback_data=f"close_appeal_{appeal_id}")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Взять обращение", callback_data=f"take_appeal_{appeal_id}")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data=f"appeal_info_{appeal_id}")]
    ])

def get_user_appeal_keyboard(appeal_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Войти в диалог", callback_data=f"enter_chat_{appeal_id}")],
        [InlineKeyboardButton("❌ Отменить обращение", callback_data=f"cancel_appeal_{appeal_id}")]
    ])

def get_exit_chat_keyboard(appeal_id: int, is_admin: bool = False):
    keyboard = [[InlineKeyboardButton("🚪 Выйти из диалога", callback_data=f"exit_chat_{appeal_id}")]]
    if is_admin:
        keyboard.append([InlineKeyboardButton("✅ Завершить обращение", callback_data=f"close_appeal_{appeal_id}")])
    return InlineKeyboardMarkup(keyboard)

def get_mailing_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Рассылка всем", callback_data="mailing_all")],
        [InlineKeyboardButton("👑 Администраторам", callback_data="mailing_admins")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username or "", user.first_name, user.last_name or "")
    if db.is_banned(user.id):
        await update.message.reply_text("🚫 Вы заблокированы в боте!")
        return
    await update.message.reply_text(
        f"✨ *Добро пожаловать в Сияние Неба!* ✨\n\n🌸 *О боте:*\nСияние Неба — это бот поддержки, который поможет вам в любой ситуации.\n\n📋 *Возможности:*\n• Связь с администрацией\n• Техническая поддержка\n• Наш Telegram канал\n\nВыберите действие из меню ниже:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user.id)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if db.is_banned(user_id):
        await query.edit_message_text("🚫 Вы заблокированы в боте!")
        return

    if data == "info":
        await query.edit_message_text(
            "✨ *Сияние Неба — Бот Поддержки* ✨\n\n🌸 *О нас:*\nСияние Неба — это команда профессионалов.\n\n🌟 *Что мы предлагаем:*\n• Оперативную поддержку\n• Решение технических вопросов\n\nВерсия: 7.0.0",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )

    elif data == "call_admin":
        open_appeal = db.get_user_open_appeal(user_id)
        if open_appeal:
            await query.edit_message_text(f"🌸 У вас уже есть открытое обращение №{open_appeal[0]}.", reply_markup=get_user_appeal_keyboard(open_appeal[0]))
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Общение", callback_data="category_chat")],
            [InlineKeyboardButton("🛟 Поддержка", callback_data="category_support")],
            [InlineKeyboardButton("❓ Другой вопрос", callback_data="category_other")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ])
        await query.edit_message_text("🌸 *Выберите цель обращения:*", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    elif data.startswith("category_"):
        category = data.replace("category_", "")
        category_names = {"chat": "Общение", "support": "Поддержка", "other": "Другой вопрос"}
        appeal_id = db.create_appeal(user_id, query.from_user.username or "", query.from_user.first_name, category)
        context.user_data['active_appeal'] = appeal_id
        await query.edit_message_text(
            f"✅ *Обращение №{appeal_id} создано!*\n🌸 Категория: {category_names.get(category, category)}\n\nНажмите кнопку ниже, чтобы войти в диалог.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=get_user_appeal_keyboard(appeal_id)
        )
        # Уведомляем ТОЛЬКО админов с доступом к этой категории
        category_admins = db.get_admins_for_category(category)
        for admin in category_admins:
            try:
                await context.bot.send_message(
                    admin[0],
                    f"🆕 *Новое обращение №{appeal_id}*\n🌸 Категория: {category_names.get(category, category)}\n👤 Клиент: @{query.from_user.username or query.from_user.first_name}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_admin_appeal_keyboard(appeal_id, False)
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin[0]}: {e}")

    elif data.startswith("enter_chat_"):
        appeal_id = int(data.replace("enter_chat_", ""))
        appeal = db.get_appeal(appeal_id)
        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return
        context.user_data['active_appeal'] = appeal_id
        is_admin = db.is_admin(user_id)
        await query.edit_message_text(f"💬 *Вы в диалоге обращения №{appeal_id}*\n\nОтправляйте сообщения. Они будут доставлены.\nКогда закончите, нажмите кнопку выхода.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_exit_chat_keyboard(appeal_id, is_admin))

    elif data.startswith("exit_chat_"):
        context.user_data.pop('active_appeal', None)
        await query.edit_message_text("🚪 Вы вышли из диалога.", reply_markup=get_main_menu_keyboard(user_id))

    elif data.startswith("cancel_appeal_"):
        appeal_id = int(data.replace("cancel_appeal_", ""))
        db.close_appeal(appeal_id)
        context.user_data.pop('active_appeal', None)
        await query.edit_message_text("❌ Обращение отменено.", reply_markup=get_main_menu_keyboard(user_id))

    elif data == "tech_support":
        open_appeal = db.get_user_open_appeal(user_id)
        if open_appeal:
            await query.edit_message_text(f"🔧 У вас уже есть открытое обращение №{open_appeal[0]}.", reply_markup=get_user_appeal_keyboard(open_appeal[0]))
            return
        appeal_id = db.create_appeal(user_id, query.from_user.username or "", query.from_user.first_name, "support")
        context.user_data['active_appeal'] = appeal_id
        await query.edit_message_text(f"✅ *Обращение №{appeal_id} создано!*\n🔧 Категория: Техподдержка\n\nНажмите кнопку ниже, чтобы войти в диалог.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_user_appeal_keyboard(appeal_id))
        # Уведомляем ТОЛЬКО админов с доступом к support
        category_admins = db.get_admins_for_category("support")
        for admin in category_admins:
            try:
                await context.bot.send_message(admin[0], f"🆕 *Новое обращение №{appeal_id}*\n🔧 Категория: Техподдержка\n👤 Клиент: @{query.from_user.username or query.from_user.first_name}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_appeal_keyboard(appeal_id, False))
            except Exception as e:
                logger.error(f"Failed to notify admin {admin[0]}: {e}")

    elif data == "back_to_main":
        context.user_data.pop('active_appeal', None)
        await query.edit_message_text("✨ *Главное меню*\n\nВыберите действие:", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

    elif data.startswith("take_appeal_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return
        appeal_id = int(data.replace("take_appeal_", ""))
        appeal = db.get_appeal(appeal_id)
        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return
        # Проверяем доступ к категории
        if not db.can_handle_category(user_id, appeal[4]):
            await query.answer(f"❌ У вас нет доступа к категории '{appeal[4]}'!", show_alert=True)
            return
        if appeal[5] != 'open':
            await query.answer("❌ Обращение уже занято!", show_alert=True)
            return
        if db.take_appeal(appeal_id, user_id):
            db.increment_admin_stats(user_id, "appeal")
            admin = db.get_admin(user_id)
            display_name = admin[2] if admin else "Администратор"
            await query.edit_message_text(f"✅ Вы взяли обращение №{appeal_id}\n👤 Клиент: @{appeal[2]} ({appeal[3]})\n🌸 Категория: {appeal[4]}\n\nНажмите кнопку ниже, чтобы войти в диалог.", reply_markup=get_admin_appeal_keyboard(appeal_id, True))
            try:
                await context.bot.send_message(appeal[1], f"👤 *{display_name}* принял(а) ваше обращение и скоро ответит.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_user_appeal_keyboard(appeal_id))
            except:
                pass
        else:
            await query.answer("❌ Не удалось взять обращение!", show_alert=True)

    elif data.startswith("appeal_info_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return
        appeal_id = int(data.replace("appeal_info_", ""))
        appeal = db.get_appeal(appeal_id)
        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return
        category_names = {"chat": "Общение", "support": "Поддержка", "other": "Другой вопрос"}
        admin_info = "Не взят"
        if appeal[6]:
            a = db.get_admin(appeal[6])
            if a:
                admin_info = f"@{a[1]} ({a[2]})"
        await query.edit_message_text(f"📋 *Информация об обращении №{appeal_id}*\n\n👤 Клиент: @{appeal[2]}\n📂 Категория: {category_names.get(appeal[4], appeal[4])}\n📊 Статус: {appeal[5]}\n👨‍💼 Взял: {admin_info}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]))

    elif data.startswith("close_appeal_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return
        appeal_id = int(data.replace("close_appeal_", ""))
        appeal = db.get_appeal(appeal_id)
        db.close_appeal(appeal_id)
        context.user_data.pop('active_appeal', None)
        try:
            await context.bot.send_message(appeal[1], f"🌸 Ваше обращение №{appeal_id} завершено.\n\nПожалуйста, оставьте отзыв о работе администратора!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Оставить отзыв", callback_data=f"review_appeal_{appeal_id}")]]))
        except:
            pass
        await query.edit_message_text(f"✅ Обращение №{appeal_id} завершено.", reply_markup=get_main_menu_keyboard(user_id))

    elif data.startswith("review_appeal_"):
        appeal_id = int(data.replace("review_appeal_", ""))
        appeal = db.get_appeal(appeal_id)
        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return
        context.user_data['review_appeal_id'] = appeal_id
        context.user_data['review_admin_id'] = appeal[6]
        context.user_data['review_category'] = appeal[4]
        await query.edit_message_text("🌸 *Оставить отзыв*\n\nКак вы хотите оставить отзыв?", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 Открыто", callback_data="review_anon_no")], [InlineKeyboardButton("🥷 Анонимно", callback_data="review_anon_yes")]]))

    elif data.startswith("review_anon_"):
        context.user_data['review_anonymous'] = data == "review_anon_yes"
        await query.edit_message_text("⭐ *Оцените работу администратора*\n\nВыберите оценку от 1 до 5:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("1⭐", callback_data="review_rating_1"), InlineKeyboardButton("2⭐", callback_data="review_rating_2"), InlineKeyboardButton("3⭐", callback_data="review_rating_3"), InlineKeyboardButton("4⭐", callback_data="review_rating_4"), InlineKeyboardButton("5⭐", callback_data="review_rating_5")]]))

    elif data.startswith("review_rating_"):
        rating = int(data.replace("review_rating_", ""))
        context.user_data['review_rating'] = rating
        await query.edit_message_text(f"📝 *Напишите ваш отзыв*\n\nОценка: {'⭐' * rating}\n\nОтправьте текст отзыва одним сообщением:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back_to_main")]]))
        return WAITING_REVIEW_TEXT

    elif data == "mailing_menu":
        if not db.has_permission(user_id, "mailing"):
            await query.answer("❌ Нет доступа к рассылке!", show_alert=True)
            return
        await query.edit_message_text("📨 *Меню рассылки*\nВыберите тип рассылки:", parse_mode=ParseMode.MARKDOWN, reply_markup=get_mailing_menu_keyboard())

    elif data in ["mailing_all", "mailing_admins"]:
        if not db.has_permission(user_id, "mailing"):
            await query.answer("❌ Нет доступа к рассылке!", show_alert=True)
            return
        context.user_data['mailing_type'] = data
        await query.edit_message_text("📝 Отправьте сообщение для рассылки (текст, фото, видео):\nДля отмены нажмите кнопку ниже.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")]]))
        return WAITING_MAILING_MESSAGE

    elif data == "cancel_mailing":
        context.user_data.clear()
        await query.edit_message_text("❌ Рассылка отменена.", reply_markup=get_main_menu_keyboard(user_id))
        return ConversationHandler.END

    elif data == "confirm_mailing":
        if 'mailing_message' not in context.user_data:
            await query.edit_message_text("❌ Ошибка! Сообщение не найдено.")
            return ConversationHandler.END
        mailing_type = context.user_data.get('mailing_type', 'mailing_all')
        message_data = context.user_data['mailing_message']
        success, failed = 0, 0
        users = db.get_all_users() if mailing_type == "mailing_all" else [(a[0],) for a in db.get_all_admins()]
        for user in users:
            try:
                if message_data.get('type') == 'text':
                    await context.bot.send_message(user[0], message_data['text'], parse_mode=ParseMode.MARKDOWN if message_data.get('parse_mode') else None)
                elif message_data.get('type') == 'photo':
                    await context.bot.send_photo(user[0], message_data['file_id'], caption=message_data.get('caption'))
                elif message_data.get('type') == 'video':
                    await context.bot.send_video(user[0], message_data['file_id'], caption=message_data.get('caption'))
                success += 1
            except:
                failed += 1
        await query.edit_message_text(f"✅ Рассылка завершена!\n\nУспешно: {success}\nНе удалось: {failed}")
        context.user_data.clear()
        return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if not message:
        return

    if db.is_banned(user.id):
        await message.reply_text("🚫 Вы заблокированы в боте!")
        return
    if db.is_muted(user.id):
        await message.reply_text("🔇 У вас ограничена возможность писать запросы.")
        return

    # Отзыв
    if context.user_data.get('waiting_for_review'):
        review_text = message.text or message.caption or "Без текста"
        appeal_id = context.user_data.get('review_appeal_id')
        admin_id = context.user_data.get('review_admin_id')
        category = context.user_data.get('review_category', 'other')
        rating = context.user_data.get('review_rating', 5)
        is_anonymous = context.user_data.get('review_anonymous', False)
        display_name = f"@{user.username}" if user.username and not is_anonymous else (user.first_name if not is_anonymous else "Аноним")
        db.add_review(user.id, user.username or "", display_name, is_anonymous, category, admin_id, rating, review_text)
        context.user_data.clear()
        if REVIEWS_CHAT_ID:
            try:
                await context.bot.send_message(REVIEWS_CHAT_ID, f"⭐ *Новый отзыв!*\n👤 От: {display_name}\n📂 Категория: {category}\n{'⭐' * rating} ({rating}/5)\n\n📝 *Отзыв:*\n{review_text}", parse_mode=ParseMode.MARKDOWN)
            except:
                pass
        await message.reply_text("🌸 Спасибо за ваш отзыв!", reply_markup=get_main_menu_keyboard(user.id))
        return

    # Диалог
    active_appeal_id = context.user_data.get('active_appeal')
    if active_appeal_id:
        appeal = db.get_appeal(active_appeal_id)
        if not appeal or appeal[5] == 'closed':
            await message.reply_text("❌ Обращение уже закрыто.")
            context.user_data.pop('active_appeal', None)
            return
        msg_text = message.text or message.caption or ""
        msg_type = "text"
        file_id = None
        if message.photo:
            msg_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            msg_type = "video"
            file_id = message.video.file_id
        db.add_appeal_message(active_appeal_id, user.id, msg_text or "[Медиа]", msg_type, file_id)
        is_admin = db.is_admin(user.id)
        target_id = None
        if is_admin and appeal[6]:
            target_id = appeal[1]
            db.increment_admin_stats(user.id, "message")
        elif user.id == appeal[1] and appeal[6]:
            target_id = appeal[6]
        elif user.id == appeal[1]:
            # Уведомляем ТОЛЬКО админов с доступом к категории
            category_admins = db.get_admins_for_category(appeal[4])
            for a in category_admins:
                try:
                    await context.bot.send_message(a[0], f"📨 *Новое сообщение в обращении №{active_appeal_id}*\n👤 От: @{user.username or user.first_name}\n\n📝 {msg_text or '[Медиа]'}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_appeal_keyboard(active_appeal_id, False))
                except:
                    pass
            await message.reply_text("✅ Сообщение отправлено.", reply_markup=get_exit_chat_keyboard(active_appeal_id, False))
            return
        if target_id:
            try:
                if message.photo:
                    await context.bot.send_photo(target_id, file_id, caption=f"📝 *Обращение №{active_appeal_id}*\n\n{msg_text}", parse_mode=ParseMode.MARKDOWN)
                elif message.video:
                    await context.bot.send_video(target_id, file_id, caption=f"📝 *Обращение №{active_appeal_id}*\n\n{msg_text}", parse_mode=ParseMode.MARKDOWN)
                else:
                    await context.bot.send_message(target_id, f"📝 *Обращение №{active_appeal_id}*\n\n{message.text}", parse_mode=ParseMode.MARKDOWN)
                await message.reply_text("✅ Сообщение отправлено.", reply_markup=get_exit_chat_keyboard(active_appeal_id, is_admin))
            except Exception as e:
                await message.reply_text(f"❌ Ошибка: {e}")
        return

async def handle_mailing_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.has_permission(user_id, "mailing"):
        await update.message.reply_text("❌ Нет доступа к рассылке!")
        return ConversationHandler.END
    message = update.message
    message_data = {'type': 'text', 'text': message.text, 'parse_mode': True}
    if message.photo:
        message_data = {'type': 'photo', 'file_id': message.photo[-1].file_id, 'caption': message.caption}
    elif message.video:
        message_data = {'type': 'video', 'file_id': message.video.file_id, 'caption': message.caption}
    context.user_data['mailing_message'] = message_data
    preview = message_data.get('text', '')[:500] if message_data['type'] == 'text' else f"[{message_data['type'].upper()}] {message_data.get('caption', '')[:500]}"
    await message.reply_text(f"📨 *Предпросмотр*\n\n{preview}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_mailing")], [InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")]]))
    return WAITING_MAILING_CONFIRM

# ==================== КОМАНДЫ ====================

async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для администраторов!")
        return
    user_id = update.effective_user.id
    appeals = db.get_open_appeals()
    # Фильтруем обращения по отделам админа
    filtered = []
    for a in appeals:
        if db.can_handle_category(user_id, a[4]):
            filtered.append(a)
    if not filtered:
        await update.message.reply_text("📭 Нет открытых обращений в ваших отделах.")
        return
    text = "📋 *Открытые обращения:*\n\n"
    cn = {"chat": "💬 Общение", "support": "🛟 Поддержка", "other": "❓ Другое"}
    for a in filtered[:10]:
        text += f"№{a[0]} | {cn.get(a[4], a[4])} | @{a[2]}\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Взять №{a[0]}", callback_data=f"take_appeal_{a[0]}")] for a in filtered[:5]])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def staff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для администраторов!")
        return
    admins = db.get_all_admins()
    if not admins:
        await update.message.reply_text("📭 Список пуст.")
        return
    text = "✨ *Команда Сияния Неба* ✨\n\n"
    for a in admins:
        r = a[5] or 5.0
        depts = db.get_admin_departments(a[0])
        dept_str = " ".join([{"chat": "💬", "support": "🛟", "other": "❓"}.get(d, d) for d in depts]) if depts else "❌ Нет отделов"
        text += f"*{a[2]}* — {a[3]}\n├ 🔰 Ур.{a[4]} | {'⭐' * int(r)} ({r:.1f}) | 📝 {a[6] or 0}\n├ 📂 {dept_str}\n└ @{a[1]}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@owner_required
async def sysadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    admin = db.get_admin(target_id)
    if not admin:
        await update.message.reply_text("❌ Не администратор!")
        return
    db.set_admin_full_perms(target_id)
    await update.message.reply_text(f"✅ @{admin[1]} получил полные права!")

@owner_required
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    target_id = None
    target_un = None
    display_name = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_un = update.message.reply_to_message.from_user.username or str(target_id)
        display_name = " ".join(args) if args else update.message.reply_to_message.from_user.first_name
    elif args:
        target_un = args[0].replace("@", "")
        display_name = " ".join(args[1:]) if len(args) > 1 else target_un
        target_id = extract_user_id(args[0])
        if not target_id:
            await update.message.reply_text("❌ Пользователь не найден! Попросите его запустить /start")
            return
    else:
        await update.message.reply_text("❌ /addadmin @username Имя")
        return
    db.add_admin(target_id, target_un, display_name, update.effective_user.id)
    await update.message.reply_text(f"✅ Администратор @{target_un} добавлен как «{display_name}»!\nИспользуйте /admin_set для настройки прав и отделов.")

@owner_required
async def deladmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    target_un = context.args[0].replace("@", "") if context.args else (update.message.reply_to_message.from_user.username if update.message.reply_to_message else None)
    if target_id == OWNER_ID:
        await update.message.reply_text("❌ Нельзя удалить владельца!")
        return
    if not target_id:
        await update.message.reply_text("❌ Администратор не найден!")
        return
    db.remove_admin(target_id)
    await update.message.reply_text(f"✅ @{target_un} удалён!")

async def admin_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ Нет прав!")
        return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    target_un = context.args[0].replace("@", "") if context.args else (update.message.reply_to_message.from_user.username if update.message.reply_to_message else None)
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    admin = db.get_admin(target_id)
    if not admin:
        await update.message.reply_text("❌ Не администратор!")
        return
    perms = db.get_admin_permissions(target_id)
    depts = db.get_admin_departments(target_id)
    all_perms = ["manage_admins", "sysban", "mute", "mailing", "all"]
    all_depts = ["chat", "support", "other"]
    keyboard = []
    pn = {"manage_admins": "Управление админами", "sysban": "Системные баны", "mute": "Муты", "mailing": "Рассылка", "all": "Все права"}
    for p in all_perms:
        status = "✅" if ("all" in perms or p in perms) else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {pn.get(p, p)}", callback_data=f"setperm_{target_id}_{p}")])
    dn = {"chat": "💬 Общение", "support": "🛟 Поддержка", "other": "❓ Другое"}
    for d in all_depts:
        status = "✅" if d in depts else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} Отдел: {dn.get(d, d)}", callback_data=f"setdept_{target_id}_{d}")])
    keyboard.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"saveperms_{target_id}")])
    await update.message.reply_text(f"🔧 *Настройка @{target_un}*\n📊 Уровень: {admin[4]} | Должность: {admin[3]}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def setdj_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ Нет прав!")
        return
    args = context.args
    target_id = None
    target_un = None
    position = "Администратор"
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_un = update.message.reply_to_message.from_user.username or str(target_id)
        position = " ".join(args) if args else "Администратор"
    elif len(args) >= 2:
        target_un = args[0].replace("@", "")
        position = " ".join(args[1:])
        target_id = extract_user_id(args[0])
    else:
        await update.message.reply_text("❌ /setdj @username Должность")
        return
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    db.update_admin_position(target_id, position)
    await update.message.reply_text(f"✅ @{target_un} → *{position}*", parse_mode=ParseMode.MARKDOWN)

async def sysban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.has_permission(update.effective_user.id, "sysban"):
        await update.message.reply_text("❌ Нет права sysban!")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ /sysban @username 1h причина\n/sysban @username full причина")
        return
    target_un = args[0].replace("@", "")
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else extract_user_id(args[0])
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("❌ Нельзя забанить владельца!")
        return
    ban_type = "full" if args[1].lower() == "full" else "temp"
    until = None if ban_type == "full" else parse_time(args[1])
    reason = " ".join(args[2:]) or "Не указана"
    if ban_type == "temp" and not until:
        await update.message.reply_text("❌ Неверный формат времени!")
        return
    db.ban_user(target_id, target_un, reason, ban_type, until, update.effective_user.id)
    await update.message.reply_text(f"✅ @{target_un} заблокирован!")

async def sysunban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.has_permission(update.effective_user.id, "sysban"):
        await update.message.reply_text("❌ Нет права sysban!")
        return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    db.unban_user(target_id)
    await update.message.reply_text("✅ Разблокирован!")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.has_permission(update.effective_user.id, "mute"):
        await update.message.reply_text("❌ Нет права mute!")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("❌ /mute @username категория 1h причина\nКатегории: chat, support, other")
        return
    target_un = args[0].replace("@", "")
    category = args[1]
    time_str = args[2]
    if category not in ["chat", "support", "other"]:
        await update.message.reply_text("❌ Категории: chat, support, other")
        return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else extract_user_id(args[0])
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    until = parse_time(time_str)
    if not until:
        await update.message.reply_text("❌ Неверный формат времени!")
        return
    reason = " ".join(args[3:]) or "Не указана"
    db.mute_user(target_id, until, category, reason)
    cn = {"chat": "Общение", "support": "Поддержка", "other": "Другое"}
    await update.message.reply_text(f"🔇 @{target_un} замучен в категории {cn.get(category, category)} до {until.strftime('%H:%M')}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.has_permission(update.effective_user.id, "mute"):
        await update.message.reply_text("❌ Нет права mute!")
        return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    db.unmute_user(target_id)
    await update.message.reply_text("🔊 Размучен!")

async def getadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ Нет прав!")
        return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else (extract_user_id(context.args[0]) if context.args else None)
    target_un = context.args[0].replace("@", "") if context.args else (update.message.reply_to_message.from_user.username if update.message.reply_to_message else None)
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    admin = db.get_admin(target_id)
    if not admin:
        await update.message.reply_text("❌ Не администратор!")
        return
    today = db.get_today_appeals_by_admin(target_id)
    depts = db.get_admin_departments(target_id)
    dept_str = ", ".join([{"chat": "Общение", "support": "Поддержка", "other": "Другое"}.get(d, d) for d in depts]) if depts else "Нет отделов"
    await update.message.reply_text(f"📊 *{admin[2]}* (@{target_un})\n📋 {admin[3]}\n📈 Уровень: {admin[4]}\n📂 Отделы: {dept_str}\n⭐ Рейтинг: {admin[5]:.1f}\n📨 Обращений сегодня: {len(today)}", parse_mode=ParseMode.MARKDOWN)

async def infoticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для администраторов!")
        return
    if not context.args:
        await update.message.reply_text("❌ /infoticket ID")
        return
    try:
        appeal_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Неверный ID!")
        return
    appeal = db.get_appeal(appeal_id)
    if not appeal:
        await update.message.reply_text("❌ Не найдено!")
        return
    cn = {"chat": "💬 Общение", "support": "🛟 Поддержка", "other": "❓ Другое"}
    ai = "Не взят"
    if appeal[6]:
        a = db.get_admin(appeal[6])
        if a:
            ai = f"@{a[1]} ({a[2]})"
    text = f"📋 *Обращение №{appeal_id}*\n👤 @{appeal[2]}\n📂 {cn.get(appeal[4], appeal[4])}\n📊 {appeal[5]}\n👨‍💼 {ai}\n🕐 {appeal[7]}\n🔒 {appeal[8] or 'Нет'}\n\n📝 *Сообщения:*"
    msgs = db.get_appeal_messages(appeal_id)
    for m in msgs[:10]:
        frm = "👤" if m[2] == appeal[1] else "👨‍💼"
        text += f"\n[{m[5]}] {frm}: {m[3][:100]}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def level_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ Нет прав!")
        return
    args = context.args
    target_id = None
    target_un = None
    inc = 1
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_un = update.message.reply_to_message.from_user.username or str(target_id)
        if args:
            try:
                inc = int(args[0])
            except:
                pass
    elif args:
        target_un = args[0].replace("@", "")
        target_id = extract_user_id(args[0])
        if len(args) > 1:
            try:
                inc = int(args[1])
            except:
                pass
    else:
        await update.message.reply_text("❌ /level_up @username [количество]")
        return
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    admin = db.get_admin(target_id)
    if not admin:
        await update.message.reply_text("❌ Не администратор!")
        return
    new_level = min(5, admin[4] + inc)
    db.update_admin_level(target_id, new_level)
    motivation = random.choice(LEVEL_UP_MESSAGES)
    await update.message.reply_text(f"🎉 *Повышение!*\n👤 @{target_un}\n📈 Уровень {new_level} (+{inc})\n💬 _{motivation}_", parse_mode=ParseMode.MARKDOWN)

async def level_down_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ Нет прав!")
        return
    args = context.args
    target_id = None
    target_un = None
    dec = 1
    reason = "Не указана"
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_un = update.message.reply_to_message.from_user.username or str(target_id)
        if args:
            try:
                dec = int(args[0])
                reason = " ".join(args[1:]) or "Не указана"
            except:
                reason = " ".join(args)
    elif args:
        target_un = args[0].replace("@", "")
        target_id = extract_user_id(args[0])
        if len(args) > 1:
            try:
                dec = int(args[1])
                reason = " ".join(args[2:]) or "Не указана"
            except:
                reason = " ".join(args[1:])
    else:
        await update.message.reply_text("❌ /level_down @username [количество] [причина]")
        return
    if not target_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return
    admin = db.get_admin(target_id)
    if not admin:
        await update.message.reply_text("❌ Не администратор!")
        return
    new_level = max(1, admin[4] - dec)
    db.update_admin_level(target_id, new_level)
    await update.message.reply_text(f"📉 *Понижение*\n👤 @{target_un}\n📉 Уровень {new_level} (-{dec})\n📝 Причина: _{reason}_", parse_mode=ParseMode.MARKDOWN)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("setperm_"):
        parts = data.split("_")
        target_id = int(parts[1])
        perm = "_".join(parts[2:])
        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return
        perms = db.get_admin_permissions(target_id)
        if perm == "all":
            perms = [] if "all" in perms else ["all"]
        else:
            if "all" in perms:
                perms.remove("all")
            if perm in perms:
                perms.remove(perm)
            else:
                perms.append(perm)
        db.update_admin_permissions(target_id, perms)
        depts = db.get_admin_departments(target_id)
        all_perms = ["manage_admins", "sysban", "mute", "mailing", "all"]
        all_depts = ["chat", "support", "other"]
        keyboard = []
        pn = {"manage_admins": "Управление админами", "sysban": "Системные баны", "mute": "Муты", "mailing": "Рассылка", "all": "Все права"}
        for p in all_perms:
            status = "✅" if ("all" in perms or p in perms) else "❌"
            keyboard.append([InlineKeyboardButton(f"{status} {pn.get(p, p)}", callback_data=f"setperm_{target_id}_{p}")])
        dn = {"chat": "💬 Общение", "support": "🛟 Поддержка", "other": "❓ Другое"}
        for d in all_depts:
            status = "✅" if d in depts else "❌"
            keyboard.append([InlineKeyboardButton(f"{status} Отдел: {dn.get(d, d)}", callback_data=f"setdept_{target_id}_{d}")])
        keyboard.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"saveperms_{target_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("setdept_"):
        parts = data.split("_")
        target_id = int(parts[1])
        dept = parts[2]
        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return
        depts = db.get_admin_departments(target_id)
        if dept in depts:
            depts.remove(dept)
        else:
            depts.append(dept)
        db.update_admin_departments(target_id, depts)
        perms = db.get_admin_permissions(target_id)
        all_perms = ["manage_admins", "sysban", "mute", "mailing", "all"]
        all_depts = ["chat", "support", "other"]
        keyboard = []
        pn = {"manage_admins": "Управление админами", "sysban": "Системные баны", "mute": "Муты", "mailing": "Рассылка", "all": "Все права"}
        for p in all_perms:
            status = "✅" if ("all" in perms or p in perms) else "❌"
            keyboard.append([InlineKeyboardButton(f"{status} {pn.get(p, p)}", callback_data=f"setperm_{target_id}_{p}")])
        dn = {"chat": "💬 Общение", "support": "🛟 Поддержка", "other": "❓ Другое"}
        for d in all_depts:
            status = "✅" if d in depts else "❌"
            keyboard.append([InlineKeyboardButton(f"{status} Отдел: {dn.get(d, d)}", callback_data=f"setdept_{target_id}_{d}")])
        keyboard.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"saveperms_{target_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("saveperms_"):
        await query.edit_message_text("✅ Настройки сохранены!")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    application = Application.builder().token(TOKEN).build()

    # 1. ConversationHandler для рассылки
    mailing_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^mailing_")],
        states={
            WAITING_MAILING_MESSAGE: [
                MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, handle_mailing_message),
                CallbackQueryHandler(button_handler, pattern="^cancel_mailing$")
            ],
            WAITING_MAILING_CONFIRM: [
                CallbackQueryHandler(button_handler, pattern="^(confirm_mailing|cancel_mailing)$")
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    application.add_handler(mailing_conv)

    # 2. Callback для админки
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(setperm_|setdept_|saveperms_).*"))

    # 3. Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reports", reports_command))
    application.add_handler(CommandHandler("staffa", staff_command))
    application.add_handler(CommandHandler("sysadmin", sysadmin_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("deladmin", deladmin_command))
    application.add_handler(CommandHandler("admin_set", admin_set_command))
    application.add_handler(CommandHandler("setdj", setdj_command))
    application.add_handler(CommandHandler("sysban", sysban_command))
    application.add_handler(CommandHandler("sysunban", sysunban_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("getadmin", getadmin_command))
    application.add_handler(CommandHandler("infoticket", infoticket_command))
    application.add_handler(CommandHandler("level_up", level_up_command))
    application.add_handler(CommandHandler("level_down", level_down_command))

    # 4. Основной callback
    application.add_handler(CallbackQueryHandler(button_handler))

    # 5. MessageHandler В КОНЦЕ
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    application.add_error_handler(error_handler)

    print("✨ Бот 'Сияние Неба' запущен...")
    print(f"👑 OWNER_ID: {OWNER_ID}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
