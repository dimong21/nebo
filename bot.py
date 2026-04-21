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
WAITING_REVIEW_CATEGORY = 3
WAITING_REVIEW_ANONYMOUS = 4
WAITING_REVIEW_RATING = 5
WAITING_REVIEW_TEXT = 6
WAITING_ADMIN_PREFIX = 7

class Database:
    def __init__(self, db_path="bot.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.init_tables()

    def init_tables(self):
        # Пользователи
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
                mute_until TIMESTAMP
            )
        ''')

        # Администраторы
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT DEFAULT 'Администратор',
                prefix TEXT DEFAULT '👤',
                position TEXT DEFAULT 'Администратор',
                level INTEGER DEFAULT 1,
                permissions TEXT DEFAULT '[]',
                rating REAL DEFAULT 5.0,
                total_reviews INTEGER DEFAULT 0,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Обращения
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

        # Сообщения обращений
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS appeal_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appeal_id INTEGER,
                user_id INTEGER,
                message_text TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (appeal_id) REFERENCES appeals (appeal_id)
            )
        ''')

        # Отзывы
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

        # Системные баны
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

        # Статистика
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
                    self.cursor.execute('UPDATE users SET is_muted = 0, mute_until = NULL WHERE user_id = ?', (user_id,))
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

    def mute_user(self, user_id: int, until: Optional[datetime]):
        self.cursor.execute('UPDATE users SET is_muted = 1, mute_until = ? WHERE user_id = ?',
                          (until.isoformat() if until else None, user_id))
        self.conn.commit()

    def unmute_user(self, user_id: int):
        self.cursor.execute('UPDATE users SET is_muted = 0, mute_until = NULL WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def add_admin(self, user_id: int, username: str, display_name: str, permissions: list, added_by: int):
        self.cursor.execute('''
            INSERT OR REPLACE INTO admins (user_id, username, display_name, permissions, added_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, display_name, json.dumps(permissions), added_by))
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
            return json.loads(result[0])
        return []

    def has_permission(self, user_id: int, permission: str) -> bool:
        if user_id == OWNER_ID:
            return True
        perms = self.get_admin_permissions(user_id)
        return "all" in perms or permission in perms

    def get_all_admins(self):
        self.cursor.execute('SELECT user_id, username, display_name, prefix, position, level, rating FROM admins ORDER BY level DESC, rating DESC')
        return self.cursor.fetchall()

    def update_admin_prefix(self, user_id: int, prefix: str):
        self.cursor.execute('UPDATE admins SET prefix = ? WHERE user_id = ?', (prefix, user_id))
        self.conn.commit()

    def update_admin_position(self, user_id: int, position: str):
        self.cursor.execute('UPDATE admins SET position = ? WHERE user_id = ?', (position, user_id))
        self.conn.commit()

    def update_admin_level(self, user_id: int, level: int):
        self.cursor.execute('UPDATE admins SET level = ? WHERE user_id = ?', (level, user_id))
        self.conn.commit()

    def update_admin_permissions(self, user_id: int, permissions: list):
        self.cursor.execute('UPDATE admins SET permissions = ? WHERE user_id = ?', (json.dumps(permissions), user_id))
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

    def add_appeal_message(self, appeal_id: int, user_id: int, message_text: str, message_type: str = "text"):
        self.cursor.execute('''
            INSERT INTO appeal_messages (appeal_id, user_id, message_text, message_type)
            VALUES (?, ?, ?, ?)
        ''', (appeal_id, user_id, message_text, message_type))
        self.conn.commit()

    def get_appeal_messages(self, appeal_id: int):
        self.cursor.execute('''
            SELECT * FROM appeal_messages WHERE appeal_id = ? ORDER BY created_at
        ''', (appeal_id,))
        return self.cursor.fetchall()

    def get_today_appeals_by_admin(self, admin_id: int):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT COUNT(*) FROM appeals WHERE admin_id = ? AND DATE(started_at) = ?
        ''', (admin_id, today))
        return self.cursor.fetchone()[0]

    def get_today_user_appeals(self, user_id: int):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT appeal_id FROM appeals WHERE user_id = ? AND DATE(started_at) = ?
        ''', (user_id, today))
        return [row[0] for row in self.cursor.fetchall()]

    def add_review(self, user_id: int, username: str, display_name: str, is_anonymous: bool, 
                   category: str, admin_id: int, rating: int, text: str):
        self.cursor.execute('''
            INSERT INTO reviews (user_id, username, display_name, is_anonymous, category, admin_id, rating, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, display_name, is_anonymous, category, admin_id, rating, text))
        self.conn.commit()
        
        # Обновляем рейтинг админа
        if admin_id:
            self.cursor.execute('''
                SELECT AVG(rating), COUNT(*) FROM reviews WHERE admin_id = ?
            ''', (admin_id,))
            avg, count = self.cursor.fetchone()
            self.cursor.execute('''
                UPDATE admins SET rating = ?, total_reviews = ? WHERE user_id = ?
            ''', (avg or 5.0, count, admin_id))
            self.conn.commit()

    def increment_admin_stats(self, admin_id: int, stat_type: str):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            INSERT INTO daily_stats (admin_id, date, appeals_taken, messages_sent)
            VALUES (?, ?, 0, 0)
            ON CONFLICT DO NOTHING
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

db = Database()

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
    """Извлекает user_id из @username или упоминания"""
    if not text:
        return None
    # Убираем @
    username = text.replace("@", "").strip()
    admin = db.get_admin_by_username(username)
    if admin:
        return admin[0]
    return None

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
            [InlineKeyboardButton("ℹ️ Информация", callback_data=f"appeal_info_{appeal_id}")],
            [InlineKeyboardButton("✅ Завершить", callback_data=f"close_appeal_{appeal_id}")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Взять обращение", callback_data=f"take_appeal_{appeal_id}")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data=f"appeal_info_{appeal_id}")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username or "", user.first_name, user.last_name or "")

    if db.is_banned(user.id):
        await update.message.reply_text("🚫 Вы заблокированы в боте!")
        return

    welcome_text = f"""
✨ *Добро пожаловать в Сияние Неба!* ✨

🌸 *О боте:*
Сияние Неба — это бот поддержки, который поможет вам в любой ситуации. Мы всегда рядом, чтобы ответить на ваши вопросы и решить проблемы.

📋 *Возможности:*
• Связь с администрацией
• Техническая поддержка
• Наш Telegram канал

Выберите действие из меню ниже:
    """
    await update.message.reply_text(
        welcome_text,
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
        info_text = """
✨ *Сияние Неба — Бот Поддержки* ✨

🌸 *О нас:*
Сияние Неба — это команда профессионалов, готовая помочь вам в любой ситуации. Мы ценим каждого пользователя и стремимся сделать общение максимально комфортным.

🌟 *Что мы предлагаем:*
• Оперативную поддержку
• Решение технических вопросов
• Приятное общение
• Быструю обратную связь

Версия: 2.0.0
        """
        await query.edit_message_text(
            info_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")
            ]])
        )

    elif data == "call_admin":
        open_appeal = db.get_user_open_appeal(user_id)
        if open_appeal:
            await query.edit_message_text(
                f"🌸 У вас уже есть открытое обращение №{open_appeal[0]}. Ожидайте ответа.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data="back_to_main"),
                    InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_appeal_{open_appeal[0]}")
                ]])
            )
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Общение", callback_data="category_chat")],
            [InlineKeyboardButton("🛟 Поддержка", callback_data="category_support")],
            [InlineKeyboardButton("❓ Другой вопрос", callback_data="category_other")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ])
        await query.edit_message_text(
            "🌸 *Выберите цель обращения:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    elif data.startswith("category_"):
        category = data.replace("category_", "")
        category_names = {
            "chat": "Общение",
            "support": "Поддержка",
            "other": "Другой вопрос"
        }

        appeal_id = db.create_appeal(user_id, query.from_user.username or "", query.from_user.first_name, category)
        context.user_data['appeal_id'] = appeal_id
        context.user_data['appeal_category'] = category

        await query.edit_message_text(
            f"✅ *Обращение №{appeal_id} создано!*\n"
            f"🌸 Категория: {category_names.get(category, category)}\n\n"
            "Опишите ваш вопрос в одном сообщении.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_appeal_{appeal_id}")
            ]])
        )
        context.user_data['waiting_for_appeal'] = True

    elif data.startswith("cancel_appeal_"):
        appeal_id = int(data.replace("cancel_appeal_", ""))
        db.close_appeal(appeal_id)
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Обращение отменено.",
            reply_markup=get_main_menu_keyboard(user_id)
        )

    elif data == "tech_support":
        await query.edit_message_text(
            "🔧 *Техническая поддержка*\n\n"
            "Если вы обнаружили ошибку или у вас есть предложение, создайте обращение.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Создать обращение", callback_data="category_support")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
            ])
        )

    elif data == "back_to_main":
        context.user_data.clear()
        await query.edit_message_text(
            "✨ *Главное меню*\n\nВыберите действие:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )

    elif data.startswith("take_appeal_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return

        appeal_id = int(data.replace("take_appeal_", ""))
        appeal = db.get_appeal(appeal_id)

        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return

        if appeal[5] != 'open':
            await query.answer("❌ Обращение уже занято!", show_alert=True)
            return

        if db.take_appeal(appeal_id, user_id):
            db.increment_admin_stats(user_id, "appeal")
            admin = db.get_admin(user_id)
            prefix = admin[3] if admin else "👤"
            display_name = admin[2] if admin else "Администратор"

            await query.edit_message_text(
                f"✅ Вы взяли обращение №{appeal_id}\n"
                f"👤 Клиент: @{appeal[2]} ({appeal[3]})\n"
                f"🌸 Категория: {appeal[4]}",
                reply_markup=get_admin_appeal_keyboard(appeal_id, True)
            )

            try:
                await context.bot.send_message(
                    appeal[1],
                    f"{prefix} *{display_name}* принял(а) ваше обращение и скоро ответит.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass

            context.bot_data[f"appeal_{appeal_id}_admin"] = user_id
        else:
            await query.answer("❌ Не удалось взять обращение!", show_alert=True)

    elif data.startswith("close_appeal_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return

        appeal_id = int(data.replace("close_appeal_", ""))
        appeal = db.get_appeal(appeal_id)
        
        db.close_appeal(appeal_id)

        if f"appeal_{appeal_id}_admin" in context.bot_data:
            del context.bot_data[f"appeal_{appeal_id}_admin"]

        # Предлагаем оставить отзыв
        try:
            await context.bot.send_message(
                appeal[1],
                f"🌸 Ваше обращение №{appeal_id} завершено.\n\n"
                "Пожалуйста, оставьте отзыв о работе администратора!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⭐ Оставить отзыв", callback_data=f"review_appeal_{appeal_id}")
                ]])
            )
        except:
            pass

        await query.edit_message_text(f"✅ Обращение №{appeal_id} завершено.")

    elif data.startswith("review_appeal_"):
        appeal_id = int(data.replace("review_appeal_", ""))
        appeal = db.get_appeal(appeal_id)
        
        if not appeal:
            await query.answer("❌ Обращение не найдено!", show_alert=True)
            return

        context.user_data['review_appeal_id'] = appeal_id
        context.user_data['review_admin_id'] = appeal[6]
        context.user_data['review_category'] = appeal[4]

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Открыто", callback_data="review_anon_no")],
            [InlineKeyboardButton("🥷 Анонимно", callback_data="review_anon_yes")]
        ])
        await query.edit_message_text(
            "🌸 *Оставить отзыв*\n\n"
            "Как вы хотите оставить отзыв?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    elif data.startswith("review_anon_"):
        is_anonymous = data == "review_anon_yes"
        context.user_data['review_anonymous'] = is_anonymous

        await query.edit_message_text(
            "⭐ *Оцените работу администратора*\n\n"
            "Выберите оценку от 1 до 5:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1⭐", callback_data="review_rating_1"),
                 InlineKeyboardButton("2⭐", callback_data="review_rating_2"),
                 InlineKeyboardButton("3⭐", callback_data="review_rating_3"),
                 InlineKeyboardButton("4⭐", callback_data="review_rating_4"),
                 InlineKeyboardButton("5⭐", callback_data="review_rating_5")]
            ])
        )

    elif data.startswith("review_rating_"):
        rating = int(data.replace("review_rating_", ""))
        context.user_data['review_rating'] = rating

        await query.edit_message_text(
            f"📝 *Напишите ваш отзыв*\n\n"
            f"Оценка: {'⭐' * rating}\n\n"
            "Отправьте текст отзыва одним сообщением:",
            parse_mode=ParseMode.MARKDOWN
        )
        return WAITING_REVIEW_TEXT

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if db.is_banned(user.id):
        await message.reply_text("🚫 Вы заблокированы в боте!")
        return

    if db.is_muted(user.id):
        await message.reply_text("🔇 У вас ограничена возможность писать запросы.")
        return

    # Обработка отзыва
    if context.user_data.get('waiting_for_review'):
        review_text = message.text or "Без текста"
        appeal_id = context.user_data.get('review_appeal_id')
        admin_id = context.user_data.get('review_admin_id')
        category = context.user_data.get('review_category', 'other')
        rating = context.user_data.get('review_rating', 5)
        is_anonymous = context.user_data.get('review_anonymous', False)

        display_name = user.first_name
        if not is_anonymous:
            display_name = f"@{user.username}" if user.username else user.first_name

        db.add_review(user.id, user.username or "", display_name, is_anonymous, 
                     category, admin_id, rating, review_text)
        
        context.user_data.clear()

        # Отправка в чат отзывов
        if REVIEWS_CHAT_ID:
            category_names = {"chat": "Общение", "support": "Поддержка", "other": "Другой вопрос"}
            admin_info = ""
            if admin_id:
                admin = db.get_admin(admin_id)
                if admin:
                    admin_info = f"\n👨‍💼 Администратор: {admin[3]} {admin[2]}"

            review_msg = f"""
⭐ *Новый отзыв!*
👤 От: {display_name if not is_anonymous else 'Анонимно'}
📂 Категория: {category_names.get(category, category)}{admin_info}
{'⭐' * rating} ({rating}/5)

📝 *Отзыв:*
{review_text}
            """
            try:
                await context.bot.send_message(REVIEWS_CHAT_ID, review_msg, parse_mode=ParseMode.MARKDOWN)
            except:
                pass

        await message.reply_text(
            "🌸 Спасибо за ваш отзыв! Он помогает нам становиться лучше.",
            reply_markup=get_main_menu_keyboard(user.id)
        )
        return

    # Обработка обращения
    if context.user_data.get('waiting_for_appeal'):
        appeal_id = context.user_data.get('appeal_id')

        if not appeal_id:
            await message.reply_text("❌ Ошибка! Начните заново.")
            context.user_data.clear()
            return

        appeal = db.get_appeal(appeal_id)
        if not appeal or appeal[5] == 'closed':
            await message.reply_text("❌ Обращение уже закрыто.")
            context.user_data.clear()
            return

        db.add_appeal_message(appeal_id, user.id, message.text or "[Медиа]")
        admins = db.get_all_admins()
        category_names = {"chat": "Общение", "support": "Поддержка", "other": "Другой вопрос"}

        if appeal[5] == 'open':
            for admin in admins:
                try:
                    admin_msg = f"""
🆕 *Новое обращение №{appeal_id}*
🌸 Категория: {category_names.get(appeal[4], appeal[4])}
👤 Клиент: @{appeal[2]} ({appeal[3]})
🆔 ID: `{appeal[1]}`

📝 *Сообщение:*
{message.text or "[Медиа]"}
                    """
                    await context.bot.send_message(
                        admin[0],
                        admin_msg,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=get_admin_appeal_keyboard(appeal_id, False)
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin[0]}: {e}")

            await message.reply_text(
                f"✅ Ваше обращение №{appeal_id} отправлено. Ожидайте ответа.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_appeal_{appeal_id}")
                ]])
            )
            context.user_data['waiting_for_appeal'] = False

        elif appeal[5] == 'in_progress' and appeal[6]:
            try:
                await context.bot.send_message(
                    appeal[6],
                    f"📨 *Новое сообщение в обращении №{appeal_id}*\n"
                    f"👤 От: @{user.username or user.id}\n\n"
                    f"📝 {message.text or '[Медиа]'}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_admin_appeal_keyboard(appeal_id, True)
                )
                db.increment_admin_stats(appeal[6], "message")
            except Exception as e:
                logger.error(f"Failed to forward to admin {appeal[6]}: {e}")

            await message.reply_text("✅ Сообщение отправлено.")

        return

    # Ответ админа на обращение
    for key, value in context.bot_data.items():
        if key.startswith("appeal_") and key.endswith(f"_{user.id}"):
            appeal_id = int(key.split("_")[1])

            if not message.reply_to_message:
                await message.reply_text("❌ Ответьте на сообщение пользователя!")
                return

            original_text = message.reply_to_message.text or "[Медиа]"
            appeal = db.get_appeal(appeal_id)

            if appeal and appeal[1]:
                db.add_appeal_message(appeal_id, user.id, message.text or "[Медиа]", "admin_reply")
                db.increment_admin_stats(user.id, "message")

                admin = db.get_admin(user.id)
                prefix = admin[3] if admin else "👤"
                display_name = admin[2] if admin else "Администратор"

                try:
                    await context.bot.send_message(
                        appeal[1],
                        f"{prefix} *{display_name}*\n\n"
                        f"📨 *В ответ:* {original_text[:100]}...\n\n"
                        f"📝 {message.text or '[Медиа]'}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    await message.reply_text("✅ Ответ отправлен.")
                except Exception as e:
                    await message.reply_text(f"❌ Ошибка: {e}")
            return

async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов!")
        return

    appeals = db.get_open_appeals()
    
    if not appeals:
        await update.message.reply_text("📭 Нет открытых обращений.")
        return

    text = "📋 *Открытые обращения:*\n\n"
    for appeal in appeals[:10]:
        category_names = {"chat": "Общение", "support": "Поддержка", "other": "Другой вопрос"}
        text += f"№{appeal[0]} | {category_names.get(appeal[4], appeal[4])} | @{appeal[2]}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Взять №{a[0]}", callback_data=f"take_appeal_{a[0]}")]
        for a in appeals[:5]
    ])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def staff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = db.get_all_admins()
    
    if not admins:
        await update.message.reply_text("📭 Список администраторов пуст.")
        return

    text = "✨ *Команда Сияния Неба* ✨\n\n"
    
    for admin in admins:
        rating_stars = "⭐" * int(admin[6])
        text += f"{admin[3]} *{admin[2]}* — {admin[4]}\n"
        text += f"├ Уровень: {admin[5]} | Рейтинг: {rating_stars} ({admin[6]:.1f})\n"
        text += f"└ @{admin[1]}\n\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    args = context.args
    target_user_id = None
    target_username = None

    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username
    elif args:
        target_user_id = extract_user_id(args[0])
        target_username = args[0].replace("@", "")

    if not target_user_id:
        await update.message.reply_text("❌ Укажите пользователя через @username или ответ на сообщение!")
        return

    admin = db.get_admin(target_user_id)
    if not admin:
        await update.message.reply_text("❌ Этот пользователь не администратор!")
        return

    perms = db.get_admin_permissions(target_user_id)
    all_perms = ["manage_admins", "sysban", "mute", "mailing", "tech_support", "all"]
    
    keyboard = []
    for perm in all_perms:
        status = "✅" if ("all" in perms or perm in perms) else "❌"
        perm_names = {
            "manage_admins": "Управление админами",
            "sysban": "Системные баны",
            "mute": "Муты",
            "mailing": "Рассылка",
            "tech_support": "Техподдержка",
            "all": "Все права"
        }
        keyboard.append([InlineKeyboardButton(
            f"{status} {perm_names.get(perm, perm)}", 
            callback_data=f"setperm_{target_user_id}_{perm}"
        )])
    
    keyboard.extend([
        [InlineKeyboardButton(f"✏️ Сменить префикс: {admin[3]}", callback_data=f"setprefix_{target_user_id}")],
        [InlineKeyboardButton("💾 Сохранить", callback_data=f"saveperms_{target_user_id}")]
    ])

    await update.message.reply_text(
        f"🔧 *Настройка администратора*\n\n"
        f"👤 @{admin[1]} ({admin[2]})\n"
        f"📊 Уровень: {admin[5]} | Должность: {admin[4]}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def setdj_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
        await update.message.reply_text("❌ У вас нет прав!")
        return

    args = context.args
    target_user_id = None
    
    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        position = " ".join(args) if args else "Администратор"
    elif len(args) >= 2:
        target_user_id = extract_user_id(args[0])
        position = " ".join(args[1:])
    else:
        await update.message.reply_text("❌ Использование: /setdj @username Должность")
        return

    if not target_user_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return

    db.update_admin_position(target_user_id, position)
    admin = db.get_admin(target_user_id)
    
    await update.message.reply_text(
        f"✅ Должность для @{admin[1]} изменена на: *{position}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def sysban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "sysban"):
        await update.message.reply_text("❌ У вас нет права sysban!")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Использование:*\n"
            "`/sysban @username 1h причина` — бан на 1 час\n"
            "`/sysban @username full причина` — полный бан",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_user_id = None
    target_username = args[0].replace("@", "")
    
    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
    else:
        target_user_id = extract_user_id(args[0])

    if not target_user_id:
        await update.message.reply_text("❌ Пользователь не найден!")
        return

    ban_type = "temp"
    until = None
    reason = "Не указана"

    if args[1].lower() == "full":
        ban_type = "full"
        reason = " ".join(args[2:]) if len(args) > 2 else "Не указана"
    else:
        until = parse_time(args[1])
        if not until:
            await update.message.reply_text("❌ Неверный формат времени! Пример: 1h, 2d, 30m")
            return
        reason = " ".join(args[2:]) if len(args) > 2 else "Не указана"

    db.ban_user(target_user_id, target_username, reason, ban_type, until, user_id)

    await update.message.reply_text(
        f"✅ *Пользователь заблокирован!*\n\n"
        f"👤 @{target_username}\n"
        f"🔒 Тип: {'Навсегда' if ban_type == 'full' else 'Временный'}\n"
        f"⏰ До: {until.strftime('%d.%m.%Y %H:%M') if until else 'Навсегда'}\n"
        f"📝 Причина: {reason}",
        parse_mode=ParseMode.MARKDOWN
    )

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
        
        admin = db.get_admin(target_id)
        all_perms = ["manage_admins", "sysban", "mute", "mailing", "tech_support", "all"]
        perm_names = {
            "manage_admins": "Управление админами",
            "sysban": "Системные баны",
            "mute": "Муты",
            "mailing": "Рассылка",
            "tech_support": "Техподдержка",
            "all": "Все права"
        }
        
        keyboard = []
        for p in all_perms:
            status = "✅" if ("all" in perms or p in perms) else "❌"
            keyboard.append([InlineKeyboardButton(
                f"{status} {perm_names.get(p, p)}", 
                callback_data=f"setperm_{target_id}_{p}"
            )])
        
        keyboard.extend([
            [InlineKeyboardButton(f"✏️ Сменить префикс: {admin[3]}", callback_data=f"setprefix_{target_id}")],
            [InlineKeyboardButton("💾 Сохранить", callback_data=f"saveperms_{target_id}")]
        ])
        
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("setprefix_"):
        target_id = int(data.replace("setprefix_", ""))
        
        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return

        context.user_data['setting_prefix_for'] = target_id
        await query.edit_message_text(
            "✏️ Отправьте новый префикс (эмодзи или текст):\n"
            "Например: 🌟, 👑, 💎",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancelprefix_{target_id}")
            ]])
        )
        return WAITING_ADMIN_PREFIX

    elif data.startswith("cancelprefix_"):
        target_id = int(data.replace("cancelprefix_", ""))
        await query.edit_message_text("❌ Изменение префикса отменено.")
        return ConversationHandler.END

    elif data.startswith("saveperms_"):
        target_id = int(data.replace("saveperms_", ""))
        await query.edit_message_text("✅ Настройки сохранены!")

async def handle_prefix_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'setting_prefix_for' not in context.user_data:
        return

    target_id = context.user_data['setting_prefix_for']
    new_prefix = update.message.text.strip()[:10]  # Ограничиваем длину

    db.update_admin_prefix(target_id, new_prefix)
    del context.user_data['setting_prefix_for']

    await update.message.reply_text(
        f"✅ Префикс изменён на: {new_prefix}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К настройкам", callback_data="back_to_settings")
        ]])
    )
    return ConversationHandler.END

def main():
    application = Application.builder().token(TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reports", reports_command))
    application.add_handler(CommandHandler("staffa", staff_command))
    application.add_handler(CommandHandler("admin_set", admin_set_command))
    application.add_handler(CommandHandler("setdj", setdj_command))
    application.add_handler(CommandHandler("sysban", sysban_command))

    # Callback обработчики
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(?!setperm_|setprefix_|saveperms_|cancelprefix_).*"))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(setperm_|setprefix_|saveperms_|cancelprefix_).*"))

    # Conversation для префикса
    prefix_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback_handler, pattern="^setprefix_")],
        states={
            WAITING_ADMIN_PREFIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prefix_input)]
        },
        fallbacks=[]
    )
    application.add_handler(prefix_conv)

    # Обработчик сообщений
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO & ~filters.COMMAND,
        handle_message
    ))

    print("✨ Бот 'Сияние Неба' запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
