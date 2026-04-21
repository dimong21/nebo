import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ChatMember
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

import sqlite3
import json

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

# Состояния для рассылки
WAITING_MAILING_MESSAGE = 1
WAITING_MAILING_CONFIRM = 2

# Состояния для тикетов
WAITING_TICKET_MESSAGE = 3

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
                permissions TEXT DEFAULT '[]',
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Тикеты (обращения)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
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

        # Сообщения тикетов
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_id INTEGER,
                message_text TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id)
            )
        ''')

        # Отзывы
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
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

        # Статистика ответов
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                date DATE,
                tickets_taken INTEGER DEFAULT 0,
                messages_sent INTEGER DEFAULT 0
            )
        ''')

        self.conn.commit()

    # Пользователи
    def add_user(self, user_id: int, username: str, first_name: str, last_name: str = ""):
        self.cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        self.conn.commit()

    def get_user(self, user_id: int):
        self.cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()

    def is_banned(self, user_id: int) -> bool:
        self.cursor.execute('''
            SELECT is_banned, ban_until FROM users WHERE user_id = ?
        ''', (user_id,))
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
        self.cursor.execute('''
            SELECT is_muted, mute_until FROM users WHERE user_id = ?
        ''', (user_id,))
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

    # Администраторы
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
        self.cursor.execute('SELECT user_id, username, display_name FROM admins')
        return self.cursor.fetchall()

    def update_admin_display_name(self, user_id: int, display_name: str):
        self.cursor.execute('UPDATE admins SET display_name = ? WHERE user_id = ?', (display_name, user_id))
        self.conn.commit()

    def update_admin_permissions(self, user_id: int, permissions: list):
        self.cursor.execute('UPDATE admins SET permissions = ? WHERE user_id = ?', (json.dumps(permissions), user_id))
        self.conn.commit()

    # Тикеты
    def create_ticket(self, user_id: int, username: str, first_name: str, category: str) -> int:
        self.cursor.execute('''
            INSERT INTO tickets (user_id, username, first_name, category)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, category))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_ticket(self, ticket_id: int):
        self.cursor.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,))
        return self.cursor.fetchone()

    def get_user_open_ticket(self, user_id: int):
        self.cursor.execute('''
            SELECT * FROM tickets WHERE user_id = ? AND status = 'open'
        ''', (user_id,))
        return self.cursor.fetchone()

    def take_ticket(self, ticket_id: int, admin_id: int):
        self.cursor.execute('''
            UPDATE tickets SET status = 'in_progress', admin_id = ? WHERE ticket_id = ? AND status = 'open'
        ''', (admin_id, ticket_id))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def close_ticket(self, ticket_id: int):
        self.cursor.execute('''
            UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE ticket_id = ?
        ''', (ticket_id,))
        self.conn.commit()

    def add_ticket_message(self, ticket_id: int, user_id: int, message_text: str, message_type: str = "text"):
        self.cursor.execute('''
            INSERT INTO ticket_messages (ticket_id, user_id, message_text, message_type)
            VALUES (?, ?, ?, ?)
        ''', (ticket_id, user_id, message_text, message_type))
        self.conn.commit()

    def get_ticket_messages(self, ticket_id: int):
        self.cursor.execute('''
            SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at
        ''', (ticket_id,))
        return self.cursor.fetchall()

    def get_today_tickets_by_admin(self, admin_id: int):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT COUNT(*) FROM tickets WHERE admin_id = ? AND DATE(started_at) = ?
        ''', (admin_id, today))
        return self.cursor.fetchone()[0]

    def get_today_user_tickets(self, user_id: int):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT ticket_id FROM tickets WHERE user_id = ? AND DATE(started_at) = ?
        ''', (user_id, today))
        return [row[0] for row in self.cursor.fetchall()]

    # Отзывы
    def add_review(self, user_id: int, username: str, rating: int, text: str):
        self.cursor.execute('''
            INSERT INTO reviews (user_id, username, rating, text)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, rating, text))
        self.conn.commit()

    # Статистика
    def increment_admin_stats(self, admin_id: int, stat_type: str):
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            INSERT INTO daily_stats (admin_id, date, tickets_taken, messages_sent)
            VALUES (?, ?, 0, 0)
            ON CONFLICT DO NOTHING
        ''', (admin_id, today))

        if stat_type == "ticket":
            self.cursor.execute('''
                UPDATE daily_stats SET tickets_taken = tickets_taken + 1
                WHERE admin_id = ? AND date = ?
            ''', (admin_id, today))
        elif stat_type == "message":
            self.cursor.execute('''
                UPDATE daily_stats SET messages_sent = messages_sent + 1
                WHERE admin_id = ? AND date = ?
            ''', (admin_id, today))
        self.conn.commit()

db = Database()

# Декораторы для проверки прав
def admin_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not db.is_admin(user_id):
            await update.message.reply_text("❌ У вас нет прав администратора!")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def permission_required(permission: str):
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            if not db.has_permission(user_id, permission):
                await update.message.reply_text(f"❌ У вас нет права: {permission}")
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

def owner_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != OWNER_ID:
            await update.message.reply_text("❌ Эта команда только для владельца бота!")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# Клавиатуры
def get_main_menu_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("ℹ️ Информация", callback_data="info")],
        [InlineKeyboardButton("👤 Вызвать администратора", callback_data="call_admin")],
        [InlineKeyboardButton("🛠 Техподдержка бота", callback_data="tech_support")],
        [InlineKeyboardButton("📢 Telegram канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("⭐ Оставить отзыв", callback_data="leave_review")]
    ]
    if db.is_admin(user_id):
        keyboard.append([InlineKeyboardButton("📨 Рассылка", callback_data="mailing_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_ticket_keyboard(ticket_id: int, is_taken: bool = False):
    if is_taken:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("ℹ️ Информация о клиенте", callback_data=f"ticket_info_{ticket_id}")],
            [InlineKeyboardButton("❌ Закрыть тикет", callback_data=f"close_ticket_{ticket_id}")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Взять клиента", callback_data=f"take_ticket_{ticket_id}")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data=f"ticket_info_{ticket_id}")]
    ])

def get_review_rating_keyboard():
    keyboard = []
    row = []
    for i in range(1, 6):
        row.append(InlineKeyboardButton(str(i) + "⭐", callback_data=f"rating_{i}"))
        if len(row) == 5:
            keyboard.append(row)
    return InlineKeyboardMarkup([row])

def get_admin_permissions_keyboard(user_id: int):
    perms = db.get_admin_permissions(user_id)
    all_perms = ["manage_admins", "sysban", "mute", "mailing", "get_stats", "ticket_info", "all"]
    keyboard = []
    for perm in all_perms:
        status = "✅" if ("all" in perms or perm in perms) else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {perm}", callback_data=f"toggle_perm_{user_id}_{perm}")])
    keyboard.append([InlineKeyboardButton("💾 Сохранить и выйти", callback_data=f"save_perms_{user_id}")])
    return InlineKeyboardMarkup(keyboard)

def get_mailing_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Рассылка всем", callback_data="mailing_all")],
        [InlineKeyboardButton("👑 Администраторам", callback_data="mailing_admins")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ])

# Команды пользователя
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username or "", user.first_name, user.last_name or "")

    if db.is_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы в боте!")
        return

    welcome_text = f"""
👋 *Добро пожаловать, {user.first_name}!*

Я бот поддержки. Здесь вы можете:
• Получить информацию о боте
• Связаться с администратором
• Сообщить о технических проблемах
• Перейти в наш Telegram канал
• Оставить отзыв

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
        await query.edit_message_text("❌ Вы заблокированы в боте!")
        return

    if data == "info":
        info_text = """
ℹ️ *О боте поддержки*

Этот бот создан для:
• Помощи пользователям
• Решения технических вопросов
• Связи с администрацией

Версия: 1.0.0
Разработчик: @your_username
        """
        await query.edit_message_text(
            info_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
            ]])
        )

    elif data == "call_admin":
        open_ticket = db.get_user_open_ticket(user_id)
        if open_ticket:
            await query.edit_message_text(
                f"ℹ️ У вас уже есть открытое обращение #{open_ticket[0]}. Ожидайте ответа администратора.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
                ]])
            )
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Общий вопрос", callback_data="category_general")],
            [InlineKeyboardButton("🛠 Техническая проблема", callback_data="category_tech")],
            [InlineKeyboardButton("💰 Финансовый вопрос", callback_data="category_finance")],
            [InlineKeyboardButton("🤝 Сотрудничество", callback_data="category_collab")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
        ])
        await query.edit_message_text(
            "📋 *Выберите категорию обращения:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    elif data.startswith("category_"):
        category = data.replace("category_", "")
        category_names = {
            "general": "Общий вопрос",
            "tech": "Техническая проблема",
            "finance": "Финансовый вопрос",
            "collab": "Сотрудничество"
        }

        ticket_id = db.create_ticket(user_id, query.from_user.username or "", query.from_user.first_name, category)

        context.user_data['ticket_id'] = ticket_id
        context.user_data['ticket_category'] = category

        await query.edit_message_text(
            f"✅ *Обращение #{ticket_id} создано!*\n"
            f"Категория: {category_names.get(category, category)}\n\n"
            "Опишите ваш вопрос в одном сообщении. Администратор ответит вам в ближайшее время.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отменить", callback_data="cancel_ticket")
            ]])
        )
        context.user_data['waiting_for_ticket'] = True

    elif data == "cancel_ticket":
        if 'ticket_id' in context.user_data:
            db.close_ticket(context.user_data['ticket_id'])
            del context.user_data['ticket_id']
            del context.user_data['ticket_category']
        context.user_data['waiting_for_ticket'] = False
        await query.edit_message_text(
            "❌ Обращение отменено.",
            reply_markup=get_main_menu_keyboard(user_id)
        )

    elif data == "tech_support":
        await query.edit_message_text(
            "🛠 *Техподдержка бота*\n\n"
            "Если вы обнаружили ошибку или у вас есть предложение по улучшению бота, "
            "пожалуйста, создайте обращение в категории 'Техническая проблема'.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Создать обращение", callback_data="category_tech")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
            ])
        )

    elif data == "leave_review":
        await query.edit_message_text(
            "⭐ *Оставить отзыв*\n\n"
            "Пожалуйста, оцените качество поддержки от 1 до 5:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_review_rating_keyboard()
        )

    elif data.startswith("rating_"):
        rating = int(data.replace("rating_", ""))
        context.user_data['review_rating'] = rating
        await query.edit_message_text(
            f"Вы выбрали оценку: {'⭐' * rating}\n\n"
            "Напишите ваш отзыв в одном сообщении:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="back_to_main")
            ]])
        )
        context.user_data['waiting_for_review'] = True

    elif data == "back_to_main":
        context.user_data.clear()
        await query.edit_message_text(
            f"👋 *Главное меню*\n\nВыберите действие:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )

    # Админские кнопки
    elif data.startswith("take_ticket_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return

        ticket_id = int(data.replace("take_ticket_", ""))
        ticket = db.get_ticket(ticket_id)

        if not ticket:
            await query.answer("❌ Тикет не найден!", show_alert=True)
            return

        if ticket[5] != 'open':
            await query.answer("❌ Клиент уже занят!", show_alert=True)
            return

        if db.take_ticket(ticket_id, user_id):
            db.increment_admin_stats(user_id, "ticket")
            admin = db.get_admin(user_id)
            display_name = admin[2] if admin else "Администратор"

            await query.edit_message_text(
                f"✅ Вы взяли обращение #{ticket_id}\n"
                f"Клиент: @{ticket[2]} ({ticket[3]})\n"
                f"Категория: {ticket[4]}",
                reply_markup=get_admin_ticket_keyboard(ticket_id, True)
            )

            # Уведомление клиенту
            try:
                await context.bot.send_message(
                    ticket[1],
                    f"👤 *{display_name}* принял ваше обращение и скоро ответит вам.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass

            # Сохраняем контекст для пересылки сообщений
            context.bot_data[f"ticket_{ticket_id}_admin"] = user_id
        else:
            await query.answer("❌ Не удалось взять тикет!", show_alert=True)

    elif data.startswith("ticket_info_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return

        ticket_id = int(data.replace("ticket_info_", ""))
        ticket = db.get_ticket(ticket_id)

        if not ticket:
            await query.answer("❌ Тикет не найден!", show_alert=True)
            return

        admin_info = ""
        if ticket[6]:
            admin = db.get_admin(ticket[6])
            if admin:
                admin_info = f"\nВзят: @{admin[1]} ({admin[2]})"
            else:
                admin_info = f"\nВзят админом ID: {ticket[6]}"

        info_text = f"""
📋 *Информация о тикете #{ticket_id}*

👤 Клиент: @{ticket[2]} ({ticket[3]})
🆔 ID: `{ticket[1]}`
📂 Категория: {ticket[4]}
📊 Статус: {ticket[5]}
🕐 Создан: {ticket[7]}{admin_info}
        """
        await query.edit_message_text(
            info_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data=f"admin_tickets")
            ]])
        )

    elif data.startswith("close_ticket_"):
        if not db.is_admin(user_id):
            await query.answer("❌ Только для администраторов!", show_alert=True)
            return

        ticket_id = int(data.replace("close_ticket_", ""))
        db.close_ticket(ticket_id)

        if f"ticket_{ticket_id}_admin" in context.bot_data:
            del context.bot_data[f"ticket_{ticket_id}_admin"]

        await query.edit_message_text(f"✅ Тикет #{ticket_id} закрыт.")

    # Рассылка
    elif data == "mailing_menu":
        if not db.has_permission(user_id, "mailing"):
            await query.answer("❌ Нет доступа к рассылке!", show_alert=True)
            return
        await query.edit_message_text(
            "📨 *Меню рассылки*\nВыберите тип рассылки:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_mailing_menu_keyboard()
        )

    elif data in ["mailing_all", "mailing_admins"]:
        if not db.has_permission(user_id, "mailing"):
            await query.answer("❌ Нет доступа к рассылке!", show_alert=True)
            return

        context.user_data['mailing_type'] = data
        await query.edit_message_text(
            "📝 Отправьте сообщение для рассылки (текст, фото, видео):\n"
            "Для отмены нажмите кнопку ниже.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")
            ]])
        )
        return WAITING_MAILING_MESSAGE

    elif data == "cancel_mailing":
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Рассылка отменена.",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return ConversationHandler.END

    elif data == "confirm_mailing":
        if 'mailing_message' not in context.user_data:
            await query.edit_message_text("❌ Ошибка! Сообщение не найдено.")
            return ConversationHandler.END

        mailing_type = context.user_data.get('mailing_type', 'mailing_all')
        message_data = context.user_data['mailing_message']

        success = 0
        failed = 0

        if mailing_type == "mailing_all":
            db.cursor.execute('SELECT user_id FROM users WHERE is_banned = 0')
            users = db.cursor.fetchall()
        else:
            admins = db.get_all_admins()
            users = [(admin[0],) for admin in admins]

        for user in users:
            try:
                if message_data.get('type') == 'text':
                    await context.bot.send_message(
                        user[0],
                        message_data['text'],
                        parse_mode=ParseMode.MARKDOWN if message_data.get('parse_mode') else None,
                        reply_markup=message_data.get('reply_markup')
                    )
                elif message_data.get('type') == 'photo':
                    await context.bot.send_photo(
                        user[0],
                        message_data['file_id'],
                        caption=message_data.get('caption'),
                        parse_mode=ParseMode.MARKDOWN if message_data.get('parse_mode') else None
                    )
                elif message_data.get('type') == 'video':
                    await context.bot.send_video(
                        user[0],
                        message_data['file_id'],
                        caption=message_data.get('caption'),
                        parse_mode=ParseMode.MARKDOWN if message_data.get('parse_mode') else None
                    )
                success += 1
            except Exception as e:
                logger.error(f"Failed to send to {user[0]}: {e}")
                failed += 1

        await query.edit_message_text(
            f"✅ Рассылка завершена!\n\n"
            f"Успешно: {success}\n"
            f"Не удалось: {failed}"
        )
        context.user_data.clear()
        return ConversationHandler.END

    elif data == "edit_mailing":
        await query.edit_message_text(
            "📝 Отправьте новое сообщение для рассылки:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")
            ]])
        )
        return WAITING_MAILING_MESSAGE

# Обработка сообщений пользователя
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if db.is_banned(user.id):
        await message.reply_text("❌ Вы заблокированы в боте!")
        return

    if db.is_muted(user.id):
        await message.reply_text("🔇 У вас ограничена возможность писать запросы. Попробуйте позже.")
        return

    # Обработка ожидания отзыва
    if context.user_data.get('waiting_for_review'):
        rating = context.user_data.get('review_rating', 5)
        review_text = message.text or "Без текста"

        db.add_review(user.id, user.username or "", rating, review_text)
        context.user_data.clear()

        # Отправка в чат отзывов
        if REVIEWS_CHAT_ID:
            review_msg = f"""
⭐ *Новый отзыв!*
👤 От: @{user.username or user.id} ({user.first_name})
{'⭐' * rating} ({rating}/5)

📝 *Отзыв:*
{review_text}
            """
            try:
                await context.bot.send_message(REVIEWS_CHAT_ID, review_msg, parse_mode=ParseMode.MARKDOWN)
            except:
                pass

        await message.reply_text(
            "✅ Спасибо за ваш отзыв! Он помогает нам становиться лучше.",
            reply_markup=get_main_menu_keyboard(user.id)
        )
        return

    # Обработка ожидания сообщения для тикета
    if context.user_data.get('waiting_for_ticket'):
        ticket_id = context.user_data.get('ticket_id')

        if not ticket_id:
            await message.reply_text("❌ Ошибка! Начните заново.", reply_markup=get_main_menu_keyboard(user.id))
            context.user_data.clear()
            return

        ticket = db.get_ticket(ticket_id)
        if not ticket or ticket[5] == 'closed':
            await message.reply_text("❌ Тикет уже закрыт.", reply_markup=get_main_menu_keyboard(user.id))
            context.user_data.clear()
            return

        # Сохраняем сообщение
        db.add_ticket_message(ticket_id, user.id, message.text or "[Медиа]")

        # Отправляем админам
        admins = db.get_all_admins()
        category_names = {
            "general": "Общий вопрос",
            "tech": "Техническая проблема",
            "finance": "Финансовый вопрос",
            "collab": "Сотрудничество"
        }

        if ticket[5] == 'open':
            # Новый тикет - уведомляем всех админов
            for admin in admins:
                try:
                    admin_msg = f"""
🆕 *Новое обращение #{ticket_id}*
📂 Категория: {category_names.get(ticket[4], ticket[4])}
👤 Клиент: @{ticket[2]} ({ticket[3]})
🆔 ID: `{ticket[1]}`

📝 *Сообщение:*
{message.text or "[Медиа]"}
                    """
                    await context.bot.send_message(
                        admin[0],
                        admin_msg,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=get_admin_ticket_keyboard(ticket_id, False)
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin[0]}: {e}")

            await message.reply_text(
                f"✅ Ваше обращение #{ticket_id} отправлено. Ожидайте ответа администратора.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Закрыть обращение", callback_data="cancel_ticket")
                ]])
            )
            context.user_data['waiting_for_ticket'] = False

        elif ticket[5] == 'in_progress':
            # Тикет в работе - отправляем админу, который взял
            if ticket[6]:
                try:
                    await context.bot.send_message(
                        ticket[6],
                        f"📨 *Новое сообщение в тикете #{ticket_id}*\n"
                        f"От: @{user.username or user.id}\n\n"
                        f"📝 {message.text or '[Медиа]'}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=get_admin_ticket_keyboard(ticket_id, True)
                    )
                    db.increment_admin_stats(ticket[6], "message")
                except Exception as e:
                    logger.error(f"Failed to forward to admin {ticket[6]}: {e}")

            await message.reply_text("✅ Сообщение отправлено администратору.")

        return

    # Проверка - отвечает ли админ на тикет
    for key, value in context.bot_data.items():
        if key.startswith("ticket_") and key.endswith(f"_{user.id}"):
            # Админ отвечает на тикет
            ticket_id = int(key.split("_")[1])

            if not message.reply_to_message:
                await message.reply_text("❌ Ответьте на сообщение пользователя!")
                return

            # Находим оригинальное сообщение пользователя
            original_text = message.reply_to_message.text or "[Медиа]"

            ticket = db.get_ticket(ticket_id)
            if ticket and ticket[1]:
                db.add_ticket_message(ticket_id, user.id, message.text or "[Медиа]", "admin_reply")
                db.increment_admin_stats(user.id, "message")

                admin = db.get_admin(user.id)
                display_name = admin[2] if admin else "Администратор"

                try:
                    await context.bot.send_message(
                        ticket[1],
                        f"👤 *{display_name}*\n\n"
                        f"📨 *В ответ на:* {original_text[:100]}...\n\n"
                        f"📝 {message.text or '[Медиа]'}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    await message.reply_text("✅ Ответ отправлен клиенту.")
                except Exception as e:
                    await message.reply_text(f"❌ Ошибка отправки: {e}")
            return

    # Обычное сообщение
    await message.reply_text(
        "Используйте кнопки меню для взаимодействия с ботом.",
        reply_markup=get_main_menu_keyboard(user.id)
    )

# Админские команды
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != OWNER_ID and not db.is_admin(user_id):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return

    args = context.args
    if not args:
        await show_admin_help(update)
        return

    subcommand = args[0].lower()

    if subcommand == "add":
        await admin_add(update, context)
    elif subcommand == "del":
        await admin_del(update, context)
    else:
        # /admin @username или ответ на сообщение
        await admin_settings(update, context)

async def show_admin_help(update: Update):
    help_text = """
📋 *Команды администратора:*

/admin add @username - Добавить админа
/admin del @username - Удалить админа
/admin @username - Настроить права админа
/sysban - Системный бан
/mute - Мут пользователя
/sysunban - Разбан
/unmute - Размут
/getadmin @username - Статистика ответов
/infoticket ID - Информация о тикете
    """
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

@owner_required
async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /admin add @username")
        return

    username = context.args[1].replace("@", "")

    # Пытаемся получить user_id
    try:
        # Это упрощенный вариант, в реальности нужно получить ID через API
        await update.message.reply_text(f"✅ Администратор @{username} добавлен!")
        # Здесь должен быть код для получения user_id
    except:
        await update.message.reply_text("❌ Пользователь не найден!")

@owner_required
async def admin_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /admin del @username")
        return

    username = context.args[1].replace("@", "")
    db.cursor.execute('DELETE FROM admins WHERE username = ?', (username,))
    db.conn.commit()

    await update.message.reply_text(f"✅ Администратор @{username} удален!")

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_user_id = None
    target_username = None

    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username
    elif context.args and len(context.args) >= 2:
        target_username = context.args[1].replace("@", "")
        # Здесь должен быть поиск user_id по username

    if not target_user_id:
        await update.message.reply_text("❌ Укажите пользователя через @username или ответ на сообщение")
        return

    # Показываем меню настроек
    admin = db.get_admin(target_user_id)
    display_name = admin[2] if admin else "Администратор"

    keyboard = [
        [InlineKeyboardButton(f"✏️ Тэг: {display_name}", callback_data=f"edit_tag_{target_user_id}")],
        [InlineKeyboardButton("🔐 Права доступа", callback_data=f"edit_perms_{target_user_id}")],
        [InlineKeyboardButton("📊 Статистика", callback_data=f"admin_stats_{target_user_id}")]
    ]

    await update.message.reply_text(
        f"👤 *Настройка администратора*\n"
        f"Пользователь: @{target_username} (ID: `{target_user_id}`)\n"
        f"Текущий тэг: {display_name}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def sysban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "sysban"):
        await update.message.reply_text("❌ У вас нет права sysban!")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование:\n"
            "/sysban @username [время] [причина] - Бан на время\n"
            "/sysban @username full [причина] - Полный бан\n"
            "Время в формате: 1h, 2d, 30m"
        )
        return

    target_username = args[0].replace("@", "")
    ban_type = "temp"
    until = None
    reason = " ".join(args[2:]) if len(args) > 2 else "Не указана"

    if len(args) > 1 and args[1].lower() == "full":
        ban_type = "full"
        reason = " ".join(args[2:]) if len(args) > 2 else "Не указана"
    elif len(args) > 1:
        # Парсим время
        time_str = args[1]
        until = parse_time(time_str)
        if not until:
            await update.message.reply_text("❌ Неверный формат времени! Пример: 1h, 2d, 30m")
            return

    # Здесь должен быть поиск user_id
    target_user_id = 123456  # Заглушка

    db.ban_user(target_user_id, target_username, reason, ban_type, until, user_id)

    await update.message.reply_text(
        f"✅ Пользователь @{target_username} заблокирован!\n"
        f"Тип: {ban_type}\n"
        f"До: {until.strftime('%d.%m.%Y %H:%M') if until else 'Навсегда'}\n"
        f"Причина: {reason}"
    )

def parse_time(time_str: str) -> Optional[datetime]:
    import re
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

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "mute"):
        await update.message.reply_text("❌ У вас нет права mute!")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ Использование: /mute @username время [причина]")
        return

    target_username = args[0].replace("@", "")
    time_str = args[1]
    reason = " ".join(args[2:]) if len(args) > 2 else "Не указана"

    until = parse_time(time_str)
    if not until:
        await update.message.reply_text("❌ Неверный формат времени! Пример: 1h, 2d, 30m")
        return

    # Здесь должен быть поиск user_id
    target_user_id = 123456  # Заглушка

    db.mute_user(target_user_id, until)

    await update.message.reply_text(
        f"🔇 Пользователь @{target_username} замучен!\n"
        f"До: {until.strftime('%d.%m.%Y %H:%M')}\n"
        f"Причина: {reason}"
    )

async def sysunban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "sysban"):
        await update.message.reply_text("❌ У вас нет права sysban!")
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ Использование: /sysunban @username")
        return

    target_username = args[0].replace("@", "")
    # Здесь должен быть поиск user_id
    target_user_id = 123456  # Заглушка

    db.unban_user(target_user_id)

    await update.message.reply_text(f"✅ Пользователь @{target_username} разблокирован!")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "mute"):
        await update.message.reply_text("❌ У вас нет права mute!")
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ Использование: /unmute @username")
        return

    target_username = args[0].replace("@", "")
    # Здесь должен быть поиск user_id
    target_user_id = 123456  # Заглушка

    db.unmute_user(target_user_id)

    await update.message.reply_text(f"🔊 Пользователь @{target_username} размучен!")

async def getadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "get_stats"):
        await update.message.reply_text("❌ У вас нет права get_stats!")
        return

    args = context.args
    target_username = None
    target_user_id = None

    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username
    elif args:
        target_username = args[0].replace("@", "")
        # Поиск user_id

    if not target_user_id:
        await update.message.reply_text("❌ Укажите пользователя!")
        return

    ticket_ids = db.get_today_user_tickets(target_user_id)
    tickets_count = len(ticket_ids)

    await update.message.reply_text(
        f"📊 *Статистика за сегодня для @{target_username}*\n\n"
        f"📨 Обращений: {tickets_count}\n"
        f"🆔 ID обращений: {', '.join(map(str, ticket_ids)) if ticket_ids else 'Нет'}",
        parse_mode=ParseMode.MARKDOWN
    )

async def infoticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "ticket_info"):
        await update.message.reply_text("❌ У вас нет права ticket_info!")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: /infoticket ID")
        return

    try:
        ticket_id = int(args[0])
    except:
        await update.message.reply_text("❌ Неверный ID тикета!")
        return

    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("❌ Тикет не найден!")
        return

    messages = db.get_ticket_messages(ticket_id)

    admin_info = "Не взят"
    if ticket[6]:
        admin = db.get_admin(ticket[6])
        if admin:
            admin_info = f"@{admin[1]} ({admin[2]})"
        else:
            admin_info = f"ID: {ticket[6]}"

    info = f"""
📋 *Тикет #{ticket_id}*

👤 Клиент: @{ticket[2]} ({ticket[3]})
🆔 ID: `{ticket[1]}`
📂 Категория: {ticket[4]}
📊 Статус: {ticket[5]}
👨‍💼 Взял: {admin_info}
🕐 Создан: {ticket[7]}
🕐 Закрыт: {ticket[8] or 'Не закрыт'}

📝 *Сообщения ({len(messages)}):*
    """

    for msg in messages[:20]:  # Ограничим вывод
        msg_time = msg[4]
        msg_from = "Клиент" if msg[2] == ticket[1] else "Админ"
        msg_text = msg[3][:100] + "..." if len(msg[3]) > 100 else msg[3]
        info += f"\n[{msg_time}] *{msg_from}*: {msg_text}"

    if len(messages) > 20:
        info += f"\n\n... и еще {len(messages) - 20} сообщений"

    await update.message.reply_text(info, parse_mode=ParseMode.MARKDOWN)

# Команды для ролевой системы
async def level_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов!")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: /level_up @username")
        return

    target_username = args[0].replace("@", "")

    # Отправка в админский чат
    if ADMIN_CHAT_ID:
        message = f"""
🎉 *Повышение!*

Пользователь @{target_username} получил повышение!
Поздравляем с новым уровнем! 🌟

*Новые возможности открыты!*
        """
        try:
            await context.bot.send_message(ADMIN_CHAT_ID, message, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text(f"✅ Сообщение о повышении @{target_username} отправлено!")
        except:
            await update.message.reply_text("❌ Не удалось отправить сообщение в чат!")

async def level_down_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов!")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: /level_down @username")
        return

    target_username = args[0].replace("@", "")

    if ADMIN_CHAT_ID:
        message = f"""
📉 *Понижение*

К сожалению, @{target_username} понижен в уровне.
Надеемся на улучшение в будущем! 💪
        """
        try:
            await context.bot.send_message(ADMIN_CHAT_ID, message, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text(f"✅ Сообщение о понижении @{target_username} отправлено!")
        except:
            await update.message.reply_text("❌ Не удалось отправить сообщение в чат!")

# Обработка callback_query для админ-панели
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("edit_tag_"):
        target_id = int(data.replace("edit_tag_", ""))
        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return

        context.user_data['editing_tag_for'] = target_id
        await query.edit_message_text(
            "✏️ Введите новый тэг (отображаемое имя) для администратора:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"admin_settings_{target_id}")
            ]])
        )
        return "WAITING_TAG"

    elif data.startswith("edit_perms_"):
        target_id = int(data.replace("edit_perms_", ""))
        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return

        await query.edit_message_text(
            f"🔐 *Настройка прав для ID: {target_id}*\n"
            "Нажмите на право, чтобы переключить:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_permissions_keyboard(target_id)
        )

    elif data.startswith("toggle_perm_"):
        parts = data.split("_")
        target_id = int(parts[2])
        perm = "_".join(parts[3:])

        if user_id != OWNER_ID and not db.has_permission(user_id, "manage_admins"):
            await query.answer("❌ Нет прав!", show_alert=True)
            return

        perms = db.get_admin_permissions(target_id)

        if perm == "all":
            if "all" in perms:
                perms = []
            else:
                perms = ["all"]
        else:
            if "all" in perms:
                perms.remove("all")
            if perm in perms:
                perms.remove(perm)
            else:
                perms.append(perm)

        db.update_admin_permissions(target_id, perms)
        await query.edit_message_reply_markup(reply_markup=get_admin_permissions_keyboard(target_id))

    elif data.startswith("save_perms_"):
        target_id = int(data.replace("save_perms_", ""))
        await query.edit_message_text(
            f"✅ Права для ID {target_id} сохранены!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data=f"admin_settings_{target_id}")
            ]])
        )

    elif data.startswith("admin_settings_"):
        target_id = int(data.replace("admin_settings_", ""))
        admin = db.get_admin(target_id)
        display_name = admin[2] if admin else "Администратор"

        keyboard = [
            [InlineKeyboardButton(f"✏️ Тэг: {display_name}", callback_data=f"edit_tag_{target_id}")],
            [InlineKeyboardButton("🔐 Права доступа", callback_data=f"edit_perms_{target_id}")],
            [InlineKeyboardButton("📊 Статистика", callback_data=f"admin_stats_{target_id}")]
        ]

        await query.edit_message_text(
            f"👤 *Настройка администратора*\nID: `{target_id}`\nТэг: {display_name}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("admin_stats_"):
        target_id = int(data.replace("admin_stats_", ""))
        today_tickets = db.get_today_tickets_by_admin(target_id)

        await query.edit_message_text(
            f"📊 *Статистика администратора*\n\n"
            f"🆔 ID: `{target_id}`\n"
            f"📨 Обращений за сегодня: {today_tickets}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data=f"admin_settings_{target_id}")
            ]])
        )

# Обработка сообщений для админ-тэга
async def handle_tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'editing_tag_for' not in context.user_data:
        return

    target_id = context.user_data['editing_tag_for']
    new_tag = update.message.text

    db.update_admin_display_name(target_id, new_tag)
    del context.user_data['editing_tag_for']

    await update.message.reply_text(
        f"✅ Тэг администратора изменен на: {new_tag}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К настройкам", callback_data=f"admin_settings_{target_id}")
        ]])
    )

# Обработка рассылки
async def handle_mailing_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.has_permission(user_id, "mailing"):
        await update.message.reply_text("❌ Нет доступа к рассылке!")
        return ConversationHandler.END

    message = update.message
    message_data = {
        'type': 'text',
        'text': message.text,
        'parse_mode': True,
        'reply_markup': None
    }

    if message.photo:
        message_data = {
            'type': 'photo',
            'file_id': message.photo[-1].file_id,
            'caption': message.caption,
            'parse_mode': True
        }
    elif message.video:
        message_data = {
            'type': 'video',
            'file_id': message.video.file_id,
            'caption': message.caption,
            'parse_mode': True
        }

    context.user_data['mailing_message'] = message_data

    preview_text = "📨 *Предпросмотр рассылки*\n\n"
    if message_data['type'] == 'text':
        preview_text += message_data['text'][:500]
    else:
        preview_text += f"[{message_data['type'].upper()}] {message_data.get('caption', 'Без подписи')[:500]}"

    if len(preview_text) > 1000:
        preview_text = preview_text[:1000] + "..."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_mailing")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="edit_mailing")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")]
    ])

    await message.reply_text(
        preview_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )

    return WAITING_MAILING_CONFIRM

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    application = Application.builder().token(TOKEN).build()

    # Базовые команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("sysban", sysban_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("sysunban", sysunban_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("getadmin", getadmin_command))
    application.add_handler(CommandHandler("infoticket", infoticket_command))
    application.add_handler(CommandHandler("level_up", level_up_command))
    application.add_handler(CommandHandler("level_down", level_down_command))

    # Callback обработчики
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(?!admin_|toggle_|save_|edit_).*"))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(admin_|toggle_|save_|edit_).*"))

    # Обработчик рассылки
    mailing_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^mailing_")],
        states={
            WAITING_MAILING_MESSAGE: [
                MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, handle_mailing_message),
                CallbackQueryHandler(button_handler, pattern="^cancel_mailing$")
            ],
            WAITING_MAILING_CONFIRM: [
                CallbackQueryHandler(button_handler, pattern="^(confirm_mailing|edit_mailing|cancel_mailing)$")
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    application.add_handler(mailing_conv)

    # Обработчик ввода тэга
    tag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback_handler, pattern="^edit_tag_")],
        states={
            "WAITING_TAG": [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tag_input)]
        },
        fallbacks=[CommandHandler("start", start)],
    )
    application.add_handler(tag_conv)

    # Обработчик сообщений
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO & ~filters.COMMAND,
        handle_message
    ))

    application.add_error_handler(error_handler)

    print("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()