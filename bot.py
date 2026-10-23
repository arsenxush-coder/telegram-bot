#!/usr/bin/env python3
"""
Telegram-бот для учёта рабочих дней бригады (солнечные панели)
Библиотека: python-telegram-bot + aiosqlite

Версия 2: исправлены баги, добавлено редактирование записей и просмотр по дням.
"""

import asyncio
import logging
import os
import math
from html import escape
from datetime import datetime, date, timedelta
from typing import Optional

import aiosqlite
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters, TypeHandler, ApplicationHandlerStop
)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()


def _parse_ids(value: str) -> list[int]:
    result = []
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                result.append(int(item))
            except ValueError:
                print(f"Предупреждение: некорректный Telegram ID пропущен: {item!r}")
    return result


SUPER_ADMIN_IDS = _parse_ids(os.environ.get("121318132", ""))

# brigade_id: 1 = Основная, 2 = Команда Дастана
USER_BRIGADE = {
    722258878: 2,    # Команда Дастана
}

DB_PATH = os.environ.get("DB_PATH", "brigade.db")
DEFAULT_OT_RATE = 20000
HOURS_PER_DAY = 8

# Состояния ConversationHandler
(
    MARK_DATE, MARK_WORKERS, MARK_OVERTIME,
    ADV_WORKER, ADV_AMOUNT, ADV_NOTE,
    EXP_CATEGORY, EXP_AMOUNT, EXP_DESC,
    OBJ_TYPE, OBJ_NAME, OBJ_SUM,
    ADD_W_NAME, ADD_W_RATE, ADD_W_PERCENT,
    EDIT_W_VALUE, OT_COEFF,
    OT_WORKER, OT_HOURS,
    RCPT_AMOUNT, RCPT_NOTE,
    CORR_WORKER, CORR_DELTA,
    EDIT_MENU, EDIT_PICK_RECORD, EDIT_NEW_VALUE,
    DAILY_PICK_DATE,
) = range(27)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ================== БАЗА ДАННЫХ ==================
async def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS brigades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brigade_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                daily_rate INTEGER NOT NULL DEFAULT 0,
                percent REAL NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(brigade_id, name)
            );
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brigade_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                total_sum INTEGER DEFAULT 0,
                work_type TEXT DEFAULT 'profit',
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS work_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL,
                worker_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                days REAL NOT NULL DEFAULT 1,
                overtime_hours REAL NOT NULL DEFAULT 0,
                UNIQUE(object_id, worker_id, work_date)
            );
            CREATE TABLE IF NOT EXISTS advances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL,
                worker_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brigade_id INTEGER NOT NULL,
                object_id INTEGER,
                user_id INTEGER NOT NULL,
                username TEXT,
                user_name TEXT,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS edit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brigade_id INTEGER NOT NULL,
                object_id INTEGER,
                table_name TEXT NOT NULL,
                row_id INTEGER NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("INSERT OR IGNORE INTO brigades (id, name) VALUES (1, 'Основная')")
        await db.execute("INSERT OR IGNORE INTO brigades (id, name) VALUES (2, 'Команда Дастана')")

        for col_sql in [
            "ALTER TABLE workers ADD COLUMN brigade_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE objects ADD COLUMN brigade_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE objects ADD COLUMN work_type TEXT DEFAULT 'profit'",
            "ALTER TABLE workers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE work_logs ADD COLUMN overtime_hours REAL NOT NULL DEFAULT 0",
            "ALTER TABLE expenses ADD COLUMN user_id INTEGER",
            "ALTER TABLE expenses ADD COLUMN user_name TEXT",
            "ALTER TABLE work_logs ADD COLUMN updated_at TEXT",
            "ALTER TABLE work_logs ADD COLUMN updated_by INTEGER",
            "ALTER TABLE advances ADD COLUMN updated_at TEXT",
            "ALTER TABLE advances ADD COLUMN updated_by INTEGER",
            "ALTER TABLE expenses ADD COLUMN updated_at TEXT",
            "ALTER TABLE expenses ADD COLUMN updated_by INTEGER",
            "ALTER TABLE receipts ADD COLUMN updated_at TEXT",
            "ALTER TABLE receipts ADD COLUMN updated_by INTEGER",
        ]:
            try:
                await db.execute(col_sql)
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        workers = [
            ("Хушнуд", 220000, 40),
            ("Бато", 170000, 20),
            ("Мумин", 170000, 20),
            ("Дастан", 170000, 20),
            ("Буни", 160000, 0),
            ("Денис", 160000, 0),
        ]
        for name, rate, pct in workers:
            await db.execute(
                "INSERT OR IGNORE INTO workers (brigade_id, name, daily_rate, percent) VALUES (1, ?, ?, ?)",
                (name, rate, pct)
            )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ot_rate', ?)",
            (str(DEFAULT_OT_RATE),)
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


def is_super(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


def is_allowed(user_id: int) -> bool:
    return is_super(user_id) or user_id in USER_BRIGADE


def get_user_brigade_id(user_id: int, context: ContextTypes.DEFAULT_TYPE = None) -> int:
    if is_super(user_id) and context is not None:
        bid = context.user_data.get("active_brigade_id")
        if bid:
            return int(bid)
        return 1
    return int(USER_BRIGADE.get(user_id, 1))


def clear_temp_data(context: ContextTypes.DEFAULT_TYPE):
    active_brigade = context.user_data.get("active_brigade_id")
    context.user_data.clear()
    if active_brigade is not None:
        context.user_data["active_brigade_id"] = active_brigade


def _user_display_name(user) -> str:
    parts = [user.first_name, user.last_name]
    full = " ".join(p for p in parts if p)
    return full or user.username or str(user.id)


async def audit_log(db, update: Update, context: ContextTypes.DEFAULT_TYPE,
                    object_id: int | None, action: str, details: str):
    user = update.effective_user
    brigade_id = get_user_brigade_id(user.id, context)
    await db.execute(
        """INSERT INTO audit_logs
           (brigade_id, object_id, user_id, username, user_name, action, details)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (brigade_id, object_id, user.id, user.username, _user_display_name(user), action, details)
    )


async def log_edit(db, brigade_id: int, object_id: int | None, table_name: str, row_id: int,
                   field: str, old_value, new_value, user):
    await db.execute(
        """INSERT INTO edit_history
           (brigade_id, object_id, table_name, row_id, field, old_value, new_value, user_id, user_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (brigade_id, object_id, table_name, row_id, field,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         user.id, _user_display_name(user))
    )


async def get_brigade_name(brigade_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM brigades WHERE id = ?", (brigade_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else f"Бригада {brigade_id}"


async def get_active_object(brigade_id: int = None) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if brigade_id is None:
            return None
        async with db.execute(
            "SELECT * FROM objects WHERE status = 'open' AND brigade_id = ? ORDER BY id DESC LIMIT 1",
            (int(brigade_id),)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_workers(brigade_id: int, active_only: bool = True) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if active_only:
            q = "SELECT * FROM workers WHERE brigade_id = ? AND is_active = 1 ORDER BY id"
        else:
            q = "SELECT * FROM workers WHERE brigade_id = ? ORDER BY id"
        async with db.execute(q, (brigade_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


def is_admin(user_id: int) -> bool:
    return is_allowed(user_id)


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    if not is_allowed(user.id):
        if update.message:
            await update.message.reply_text("⛔ Доступ запрещён.\nОбратитесь к бригадиру.")
        elif update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        raise ApplicationHandlerStop


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("📅 Отметить день")],
        [KeyboardButton("✏️ Корректировка дней"), KeyboardButton("➕ Сверхурочные")],
        [KeyboardButton("💰 Аванс работнику"), KeyboardButton("💵 Поступление")],
        [KeyboardButton("🧾 Расход"), KeyboardButton("📊 Отчёт")],
        [KeyboardButton("📅 Расходы по дням"), KeyboardButton("📆 Всё по дням")],
        [KeyboardButton("✏️ Редактировать запись")],
        [KeyboardButton("🕘 История операций")],
        [KeyboardButton("👷 Работники")],
        [KeyboardButton("🏗️ Объект"), KeyboardButton("⚙️ Настройки")],
    ]
    if is_super(user_id):
        buttons.append([KeyboardButton("🔄 Сменить бригаду")])
    buttons.append([KeyboardButton("ℹ️ Помощь")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# ================== СТАРТ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bid = get_user_brigade_id(user_id, context)
    bname = await get_brigade_name(bid)
    text = (
        "👋 <b>Бот учёта бригады</b>\n\n"
        f"Бригада: <b>{bname}</b>\n\n"
        "Отмечайте рабочие дни, авансы и расходы.\n"
        "В конце объекта нажмите «Закрыть объект».\n\n"
        "<b>Новое:</b>\n"
        "• ✏️ Редактировать запись — исправить любую запись\n"
        "• 📆 Всё по дням — детальный просмотр по датам"
    )
    if is_super(user_id):
        text += "\n\nВы главный — можете переключать бригады («🔄 Сменить бригаду»)."
    await update.message.reply_text(text, reply_markup=main_keyboard(user_id), parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Как пользоваться:</b>\n\n"
        "1. 🏗️ Объект → Новый объект → название\n"
        "2. Укажите сумму объекта\n"
        "3. Каждый день: 📅 Отметить день\n"
        "4. Добавляйте авансы и расходы\n"
        "5. Когда закончили — Закрыть объект\n\n"
        "<b>Исправление ошибок:</b>\n"
        "✏️ Редактировать запись — выбрать тип, найти запись и ввести новое значение.\n\n"
        "<b>Просмотр по датам:</b>\n"
        "📆 Всё по дням — дни, расходы, авансы, поступления за день/неделю.\n\n"
        "<b>Формула:</b>\n"
        "Прибыль = Сумма объекта − Зарплаты − Расходы\n"
        "Прибыль делится между <b>активными</b> работниками по проценту."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keep = context.user_data.get("active_brigade_id")
    clear_temp_data(context)
    if keep:
        context.user_data["active_brigade_id"] = keep
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard(update.effective_user.id))
    return ConversationHandler.END


async def switch_brigade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        await update.message.reply_text("Только главный может менять бригаду.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name FROM brigades ORDER BY id") as cur:
            rows = await cur.fetchall()
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"swbrig_{r['id']}")] for r in rows]
    cur_id = get_user_brigade_id(update.effective_user.id, context)
    cur_name = await get_brigade_name(cur_id)
    await update.message.reply_text(
        f"Сейчас: <b>{cur_name}</b>\nВыберите бригаду:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def switch_brigade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_super(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    try:
        bid = int(query.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text("Не удалось определить бригаду.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM brigades WHERE id = ?", (bid,)) as cur:
            if not await cur.fetchone():
                await query.message.reply_text("Такая бригада не найдена.")
                return
    context.user_data["active_brigade_id"] = bid
    name = await get_brigade_name(bid)
    await query.message.reply_text(
        f"✅ Переключено на бригаду: <b>{name}</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(query.from_user.id)
    )


# ================== ОБЪЕКТ ==================
async def object_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if obj:
        kb_buttons = []
        if (obj.get("work_type") or "profit") != "daily":
            kb_buttons.append([InlineKeyboardButton("💰 Указать/изменить сумму", callback_data="obj_set_sum")])
        kb_buttons.append([InlineKeyboardButton("🔒 Закрыть объект + отчёт", callback_data="obj_close")])
        kb_buttons.append([InlineKeyboardButton("📋 Инфо об объекте", callback_data="obj_info")])
        kb = InlineKeyboardMarkup(kb_buttons)
        type_label = "илтан" if (obj.get("work_type") or "profit") == "daily" else "с прибылью"
        text = f"<b>Текущий объект:</b> {escape(obj['name'])}\nТип: {type_label}\nСумма: {obj['total_sum']:,} ₩\nСтатус: открыт"
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый объект", callback_data="obj_new")]])
        text = "Нет активного объекта. Создайте новый."
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def obj_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "obj_info":
        obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
        if not obj:
            await query.message.reply_text("Нет активного объекта.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(DISTINCT work_date) FROM work_logs WHERE object_id = ?", (obj["id"],)) as cur:
                days = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE object_id = ?", (obj["id"],)) as cur:
                exp = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(SUM(amount),0) FROM advances WHERE object_id = ?", (obj["id"],)) as cur:
                adv = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(SUM(amount),0) FROM receipts WHERE object_id = ?", (obj["id"],)) as cur:
                received = (await cur.fetchone())[0]
        total = obj["total_sum"] or 0
        text = (
            f"<b>Объект:</b> {escape(obj['name'])}\n"
            f"Сумма: <b>{total:,} ₩</b>\n"
            f"Получено: <b>{received:,} ₩</b>\n"
            f"Осталось получить: <b>{total - received:,} ₩</b>\n"
            f"Рабочих дней: {days}\n"
            f"Расходы: {exp:,} ₩\n"
            f"Авансы работникам: {adv:,} ₩"
        )
        await query.message.reply_text(text, parse_mode="HTML")
        return

    if data == "obj_close":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, закрыть", callback_data="obj_close_yes")],
            [InlineKeyboardButton("❌ Нет", callback_data="obj_close_no")],
        ])
        await query.message.reply_text("Закрыть текущий объект?", reply_markup=kb)
        return

    if data == "obj_close_no":
        await query.message.delete()
        return

    if data == "obj_close_yes":
        obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
        if not obj:
            await query.message.reply_text("Нет активного объекта.")
            return
        brigade_id = get_user_brigade_id(update.effective_user.id, context)
        report = await generate_report(obj["id"], brigade_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE objects SET status = 'closed', closed_at = ? WHERE id = ?",
                             (datetime.now().isoformat(), obj["id"]))
            await db.commit()
        await query.message.reply_text(f"🔒 Объект «{escape(obj['name'])}» закрыт.\n\n{report}", parse_mode="HTML",
                                       reply_markup=main_keyboard(update.effective_user.id))
        return


async def start_new_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 С прибылью (%, остаток)", callback_data="type_profit")],
        [InlineKeyboardButton("📅 Работа за илтан (только дневная)", callback_data="type_daily")],
    ])
    await query.message.reply_text("Выберите тип работы:", reply_markup=kb)
    return OBJ_TYPE


async def choose_work_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "type_profit":
        context.user_data["work_type"] = "profit"
        type_name = "с прибылью"
    else:
        context.user_data["work_type"] = "daily"
        type_name = "за илтан"
    await query.message.reply_text(
        f"Тип: <b>{type_name}</b>\n\nВведите название объекта:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return OBJ_NAME


async def start_set_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Введите сумму объекта (только число):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return OBJ_SUM


async def obj_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Введите название объекта:")
        return OBJ_NAME
    work_type = context.user_data.get("work_type", "profit")
    bid = get_user_brigade_id(update.effective_user.id, context)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE objects SET status = 'closed', closed_at = ? WHERE status = 'open' AND brigade_id = ?",
                (datetime.now().isoformat(), bid)
            )
            cur = await db.execute(
                "INSERT INTO objects (brigade_id, name, work_type, status) VALUES (?, ?, ?, 'open')",
                (bid, name, work_type)
            )
            await db.commit()
            new_id = cur.lastrowid
    except Exception as e:
        logger.exception("Ошибка создания объекта")
        await update.message.reply_text(
            f"❌ Ошибка сохранения объекта: {e}\n"
            f"Путь БД: {DB_PATH}\nБригада: {bid}"
        )
        return ConversationHandler.END

    obj = await get_active_object(bid)
    type_text = "за илтан" if work_type == "daily" else "с прибылью"
    bname = await get_brigade_name(bid)
    if obj and obj.get("name") == name:
        msg = (
            f"✅ Объект «{escape(name)}» создан ({type_text})\n"
            f"Бригада: {escape(bname)}\n"
            f"ID: {obj['id']}"
        )
        if work_type == "profit":
            msg += "\n\nТеперь: 🏗️ Объект → Указать сумму"
    else:
        msg = (
            f"⚠ Объект записан (id={new_id}), но не найден как активный.\n"
            f"Бригада: {bid}, БД: {DB_PATH}\n"
            f"Напишите /start и снова 🏗️ Объект"
        )
    await update.message.reply_text(msg, reply_markup=main_keyboard(update.effective_user.id), parse_mode="HTML")
    keep = context.user_data.get("active_brigade_id")
    clear_temp_data(context)
    if keep:
        context.user_data["active_brigade_id"] = keep
    return ConversationHandler.END


async def obj_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите целое положительное число.")
        return OBJ_SUM
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Нет активного объекта.")
        return ConversationHandler.END
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE objects SET total_sum = ? WHERE id = ?", (amount, obj["id"]))
        await db.commit()
    await update.message.reply_text(f"✅ Сумма объекта: {amount:,} ₩", reply_markup=main_keyboard(update.effective_user.id))
    return ConversationHandler.END

# ================== ОТМЕТИТЬ ДЕНЬ ==================
async def mark_day_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект (🏗️ Объект).")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    context.user_data["selected"] = set()
    today = date.today().isoformat()
    kb = ReplyKeyboardMarkup([[KeyboardButton("Сегодня")], [KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(f"Введите дату (ГГГГ-ММ-ДД) или нажмите «Сегодня» ({today}):", reply_markup=kb)
    return MARK_DATE


async def mark_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "Сегодня":
        work_date = date.today().isoformat()
    else:
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            work_date = text.strip()
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: 2026-10-12")
            return MARK_DATE
    context.user_data["work_date"] = work_date
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = []
    row = []
    for i, w in enumerate(workers):
        row.append(InlineKeyboardButton(w["name"], callback_data=f"wd_{w['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✔️ Готово", callback_data="wd_done")])
    await update.message.reply_text(f"Дата: <b>{work_date}</b>\nВыберите, кто вышел:",
                                    reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    return MARK_WORKERS


async def mark_workers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    selected = context.user_data.get("selected", set())

    if data == "wd_done":
        if not selected:
            await query.answer("Выберите хотя бы одного!", show_alert=True)
            return MARK_WORKERS
        context.user_data["selected"] = selected
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Полный день (1)", callback_data="wd_full")],
            [InlineKeyboardButton("½ Полдня (0.5)", callback_data="wd_half")],
        ])
        await query.message.reply_text("Какой день отметить?", reply_markup=kb)
        return MARK_WORKERS

    if data in ("wd_full", "wd_half"):
        days_val = 1.0 if data == "wd_full" else 0.5
        selected = context.user_data.get("selected", set())
        workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
        selected_workers = [w for w in workers if w["id"] in selected]
        object_id = context.user_data["object_id"]
        work_date = context.user_data["work_date"]
        async with aiosqlite.connect(DB_PATH) as db:
            for w in selected_workers:
                await db.execute(
                    """INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours)
                       VALUES (?, ?, ?, ?, 0)
                       ON CONFLICT(object_id, worker_id, work_date)
                       DO UPDATE SET days = excluded.days""",
                    (object_id, w["id"], work_date, days_val)
                )
            await audit_log(db, update, context, object_id, "Отметка дня",
                            f"Дата {work_date}; {'полдня' if days_val == 0.5 else 'полный день'}; работники: {', '.join(w['name'] for w in selected_workers)}")
            await db.commit()
        names = ", ".join(w["name"] for w in selected_workers)
        day_text = "полный день" if days_val == 1 else "полдня"
        ot_rate = await get_setting("ot_rate", str(DEFAULT_OT_RATE))
        await query.message.reply_text(
            f"✅ Отмечено за {work_date} ({day_text}):\n{names}\n\n"
            f"Сверхурочные — кнопка «➕ Сверхурочные» ({int(float(ot_rate)):,} ₩/час).",
            reply_markup=main_keyboard(query.from_user.id)
        )
        clear_temp_data(context)
        return ConversationHandler.END

    wid = int(data.split("_")[1])
    if wid in selected:
        selected.remove(wid)
    else:
        selected.add(wid)
    context.user_data["selected"] = selected

    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = []
    row = []
    for w in workers:
        mark = "✅ " if w["id"] in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{w['name']}", callback_data=f"wd_{w['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✔️ Готово", callback_data="wd_done")])
    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    return MARK_WORKERS


# ================== АВАНС ==================
async def advance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = [[InlineKeyboardButton(w["name"], callback_data=f"adv_{w['id']}")] for w in workers]
    buttons.append([InlineKeyboardButton("👥 Всем работникам", callback_data="adv_all")])
    await update.message.reply_text("Кому скорректировать аванс? Можно выбрать одного или сразу всех работников.", reply_markup=InlineKeyboardMarkup(buttons))
    return ADV_WORKER


async def advance_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target = query.data.split("_", 1)[1]
    if target == "all":
        workers = await get_workers(get_user_brigade_id(query.from_user.id, context))
        context.user_data["worker_ids"] = [w["id"] for w in workers]
        context.user_data["worker_names"] = [w["name"] for w in workers]
    else:
        wid = int(target)
        context.user_data["worker_ids"] = [wid]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT name FROM workers WHERE id=?", (wid,)) as cur:
                row = await cur.fetchone()
        context.user_data["worker_names"] = [row[0] if row else str(wid)]
    await query.message.reply_text(
        "Введите сумму корректировки для каждого выбранного работника:\n"
        "➕ Например, 50000 — добавить аванс\n"
        "➖ Например, -20000 — уменьшить аванс или отметить возврат денег\n"
        "Ноль вводить не нужно.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return ADV_AMOUNT


async def advance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount == 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите ненулевую сумму: положительную для добавления или отрицательную для уменьшения аванса.")
        return ADV_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text(
        "Комментарий (или нажмите «Нет»):",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Нет")], [KeyboardButton("❌ Отмена")]],
            resize_keyboard=True
        )
    )
    return ADV_NOTE


async def advance_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    note = "" if update.message.text.strip() in ("Нет", "-", "нет") else update.message.text.strip()
    worker_ids = context.user_data.get("worker_ids", [])
    if not worker_ids:
        await update.message.reply_text("Не выбраны работники. Начните заново.", reply_markup=main_keyboard(update.effective_user.id))
        clear_temp_data(context)
        return ConversationHandler.END
    async with aiosqlite.connect(DB_PATH) as db:
        for wid in worker_ids:
            if wid is None:
                continue
            await db.execute(
                "INSERT INTO advances (object_id, worker_id, amount, note) VALUES (?, ?, ?, ?)",
                (context.user_data["object_id"], wid, context.user_data["amount"], note)
            )
            await audit_log(db, update, context, context.user_data["object_id"], "Аванс",
                            f"Работник ID {wid}; сумма {context.user_data['amount']:+,} ₩; {note or 'без комментария'}")
        await db.commit()
    names = ", ".join(context.user_data.get("worker_names", [])) or "выбранным работникам"
    amount = context.user_data["amount"]
    action = "Аванс добавлен" if amount > 0 else "Аванс уменьшен / возврат учтён"
    await update.message.reply_text(f"✅ {action}: {amount:+,} ₩ на каждого. Работники: {names}",
                                    reply_markup=main_keyboard(update.effective_user.id))
    clear_temp_data(context)
    return ConversationHandler.END


# ================== РАСХОД ==================
async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍽 Питание", callback_data="exp_food"),
         InlineKeyboardButton("🚗 Дорога", callback_data="exp_transport")],
        [InlineKeyboardButton("🏠 Проживание", callback_data="exp_housing"),
         InlineKeyboardButton("🔧 Техника", callback_data="exp_technician")],
        [InlineKeyboardButton("📦 Другое", callback_data="exp_other")],
    ])
    await update.message.reply_text("Выберите категорию расхода:", reply_markup=kb)
    return EXP_CATEGORY


async def expense_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split("_")[1]
    context.user_data["category"] = cat
    await query.message.reply_text("Сумма расхода (только число):",
                                   reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True))
    return EXP_AMOUNT


async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    if "category" not in context.user_data:
        await update.message.reply_text("Сначала выберите категорию — нажмите «🧾 Расход» заново.",
                                        reply_markup=main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число.")
        return EXP_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text(
        "Описание расхода (или нажмите «Нет»):",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Нет")], [KeyboardButton("❌ Отмена")]],
            resize_keyboard=True
        )
    )
    return EXP_DESC


async def expense_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    desc = "" if update.message.text.strip() in ("Нет", "-", "нет") else update.message.text.strip()
    cat_names = {"food": "Питание", "transport": "Дорога", "housing": "Проживание",
                 "technician": "Техника", "other": "Другое"}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO expenses (object_id, category, amount, description, user_id, user_name) VALUES (?, ?, ?, ?, ?, ?)",
            (context.user_data["object_id"], context.user_data["category"], context.user_data["amount"], desc,
             update.effective_user.id, _user_display_name(update.effective_user))
        )
        await audit_log(db, update, context, context.user_data["object_id"], "Расход",
                        f"Категория: {cat_names.get(context.user_data['category'], context.user_data['category'])}; сумма {context.user_data['amount']:,} ₩; {desc or 'без описания'}")
        await db.commit()
    await update.message.reply_text(
        f"✅ Расход {context.user_data['amount']:,} ₩ ({cat_names.get(context.user_data['category'])}) добавлен.",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    clear_temp_data(context)
    return ConversationHandler.END


# ================== РАБОТНИКИ ==================
async def workers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context), active_only=False)
    text = "<b>Список работников:</b>\n\n"
    for w in workers:
        status = "✅" if w["is_active"] else "❌"
        pct = f" + {w['percent']}%" if w["percent"] else ""
        text += f"{status} <b>{escape(w['name'])}</b> — {w['daily_rate']:,} ₩/день{pct}\n"
    kb = None
    if is_admin(update.effective_user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить", callback_data="w_add")],
            [InlineKeyboardButton("✏️ Изменить ставку/%", callback_data="w_edit")],
            [InlineKeyboardButton("🗑 Удалить работника", callback_data="w_delete")],
        ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def start_add_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keep = context.user_data.get("active_brigade_id")
    for k in list(context.user_data.keys()):
        if k != "active_brigade_id":
            context.user_data.pop(k, None)
    if keep:
        context.user_data["active_brigade_id"] = keep
    await query.message.reply_text(
        "Имя нового работника:\n(или /start чтобы выйти)",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return ADD_W_NAME


async def workers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "w_edit":
        brigade_id = get_user_brigade_id(update.effective_user.id, context)
        workers = await get_workers(brigade_id, active_only=False)
        buttons = [[InlineKeyboardButton(w["name"], callback_data=f"wedit_{w['id']}")] for w in workers]
        buttons.append([InlineKeyboardButton("👥 Изменить всем работникам", callback_data="wedit_all")])
        await query.message.reply_text("Выберите работника или измените ставку и процент сразу всем:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    if query.data == "w_delete":
        workers = await get_workers(get_user_brigade_id(update.effective_user.id, context), active_only=True)
        if not workers:
            await query.message.reply_text("Нет активных работников.")
            return
        buttons = [[InlineKeyboardButton(f"🗑 {w['name']}", callback_data=f"wdel_{w['id']}")] for w in workers]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="wdel_cancel")])
        await query.message.reply_text(
            "Кого удалить?\n(работник исчезнет из списка, история сохранится)",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return


async def worker_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "wdel_cancel":
        await query.message.reply_text("Отменено.", reply_markup=main_keyboard(query.from_user.id))
        return
    wid = int(data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM workers WHERE id = ?", (wid,)) as cur:
            row = await cur.fetchone()
        if not row:
            await query.message.reply_text("Работник не найден.")
            return
        name = row[0]
        await db.execute("UPDATE workers SET is_active = 0 WHERE id = ?", (wid,))
        await db.commit()
    await query.message.reply_text(
        f"✅ Работник <b>{escape(name)}</b> удалён из списка.\n"
        "(История его дней и авансов сохранена)",
        parse_mode="HTML",
        reply_markup=main_keyboard(query.from_user.id)
    )


async def add_worker_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Имя не может быть пустым. Введите имя:")
        return ADD_W_NAME
    context.user_data["new_name"] = name
    await update.message.reply_text("Дневная ставка (целое число в вонах, не меньше 0):")
    return ADD_W_RATE


async def add_worker_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        rate = int(update.message.text.replace(" ", "").replace(",", ""))
        if rate < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите целое число 0 или больше.")
        return ADD_W_RATE
    context.user_data["new_rate"] = rate
    await update.message.reply_text("Процент от прибыли (0 если нет):")
    return ADD_W_PERCENT


async def add_worker_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        pct = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число от 0 до 100.")
        return ADD_W_PERCENT
    if not 0 <= pct <= 100:
        await update.message.reply_text("Процент должен быть от 0 до 100.")
        return ADD_W_PERCENT
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            bid = get_user_brigade_id(update.effective_user.id, context)
            await db.execute(
                "INSERT INTO workers (brigade_id, name, daily_rate, percent) VALUES (?, ?, ?, ?)",
                (bid, context.user_data["new_name"], context.user_data["new_rate"], pct)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await update.message.reply_text("Такое имя уже есть.")
            keep = context.user_data.get("active_brigade_id")
            clear_temp_data(context)
            if keep:
                context.user_data["active_brigade_id"] = keep
            return ConversationHandler.END
    name = context.user_data.get("new_name", "")
    rate = context.user_data.get("new_rate", 0)
    await update.message.reply_text(
        f"✅ {escape(name)} добавлен: {rate:,} ₩ + {pct}%",
        reply_markup=main_keyboard(update.effective_user.id),
        parse_mode="HTML"
    )
    keep = context.user_data.get("active_brigade_id")
    clear_temp_data(context)
    if keep:
        context.user_data["active_brigade_id"] = keep
    return ConversationHandler.END


async def worker_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target = query.data.split("_", 1)[1]
    brigade_id = get_user_brigade_id(query.from_user.id, context)
    if target == "all":
        context.user_data["edit_all_workers"] = True
        worker_label = "Все активные работники бригады"
        current_label = "Для всех будет установлено одинаковое значение."
    else:
        try:
            wid = int(target)
        except (ValueError, IndexError):
            await query.message.reply_text("Не удалось определить работника.")
            return ConversationHandler.END
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT name, daily_rate, percent FROM workers WHERE id = ? AND brigade_id = ?",
                (wid, brigade_id)
            ) as cur:
                worker = await cur.fetchone()
        if not worker:
            await query.message.reply_text("Работник не найден в текущей бригаде.")
            return ConversationHandler.END
        context.user_data["edit_worker_id"] = wid
        worker_label = worker[0]
        current_label = f"Текущая ставка: {worker[1]:,} ₩/день\nТекущий процент: {worker[2]}%"
    await query.message.reply_text(
        f"Работник: {escape(worker_label)}\n{escape(current_label)}\n\n"
        "Введите новую дневную ставку и процент через пробел.\n"
        "Например: 170000 20\n"
        "Если процент не нужен, укажите 0.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True),
        parse_mode="HTML"
    )
    return EDIT_W_VALUE


async def worker_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("Введите два значения через пробел, например: 170000 20")
        return EDIT_W_VALUE
    try:
        rate_text = parts[0].replace(",", "")
        rate = int(rate_text)
        percent = float(parts[1].replace(",", "."))
        if rate < 0 or not 0 <= percent <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ставка должна быть целым числом ≥ 0, процент — от 0 до 100.")
        return EDIT_W_VALUE

    wid = context.user_data.get("edit_worker_id")
    brigade_id = get_user_brigade_id(update.effective_user.id, context)
    async with aiosqlite.connect(DB_PATH) as db:
        if context.user_data.get("edit_all_workers"):
            cur = await db.execute(
                "UPDATE workers SET daily_rate = ?, percent = ? WHERE brigade_id = ? AND is_active = 1",
                (rate, percent, brigade_id)
            )
        else:
            cur = await db.execute(
                "UPDATE workers SET daily_rate = ?, percent = ? WHERE id = ? AND brigade_id = ?",
                (rate, percent, wid, brigade_id)
            )
        await db.commit()
        if cur.rowcount == 0:
            await update.message.reply_text("Не удалось изменить: работники не найдены в текущей бригаде.")
            keep = context.user_data.get("active_brigade_id")
            clear_temp_data(context)
            if keep:
                context.user_data["active_brigade_id"] = keep
            return ConversationHandler.END

    target_label = "у всех активных работников" if context.user_data.get("edit_all_workers") else "у работника"
    await update.message.reply_text(
        f"✅ Ставка обновлена {target_label}: {rate:,} ₩/день, доля прибыли: {percent:g}%",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    keep = context.user_data.get("active_brigade_id")
    clear_temp_data(context)
    if keep:
        context.user_data["active_brigade_id"] = keep
    return ConversationHandler.END


# ================== КОРРЕКТИРОВКА ДНЕЙ ==================
async def corr_show_workers(message, context, user_id):
    workers = await get_workers(get_user_brigade_id(user_id, context))
    selected = context.user_data.setdefault("corr_selected", set())
    buttons, row = [], []
    for w in workers:
        mark = "✅ " if w["id"] in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{w['name']}", callback_data=f"corrw_{w['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("➡️ Продолжить", callback_data="corr_done")])
    await message.reply_text(
        "Выберите всех работников, которых нужно скорректировать. "
        "Можно выбрать несколько, затем указать изменение для каждого.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def corr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    context.user_data["corr_selected"] = set()
    await corr_show_workers(update.message, context, update.effective_user.id)
    return CORR_WORKER


async def corr_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        wid = int(query.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Не удалось определить работника.", show_alert=True)
        return CORR_WORKER
    selected = context.user_data.setdefault("corr_selected", set())
    if wid in selected:
        selected.remove(wid)
    else:
        selected.add(wid)
    workers = await get_workers(get_user_brigade_id(query.from_user.id, context))
    buttons, row = [], []
    for w in workers:
        mark = "✅ " if w["id"] in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{w['name']}", callback_data=f"corrw_{w['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("➡️ Продолжить", callback_data="corr_done")])
    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    return CORR_WORKER


async def corr_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected = context.user_data.get("corr_selected", set())
    if not selected:
        await query.answer("Сначала выберите хотя бы одного работника.", show_alert=True)
        return CORR_WORKER
    workers = await get_workers(get_user_brigade_id(query.from_user.id, context))
    context.user_data["corr_queue"] = [w for w in workers if w["id"] in selected]
    context.user_data["corr_index"] = 0
    await corr_prompt_current(query.message, context)
    return CORR_DELTA


async def corr_prompt_current(message, context):
    queue = context.user_data.get("corr_queue", [])
    idx = context.user_data.get("corr_index", 0)
    worker = queue[idx]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(days),0) FROM work_logs WHERE object_id=? AND worker_id=?",
            (context.user_data["object_id"], worker["id"])
        ) as cur:
            total = (await cur.fetchone())[0]
    context.user_data["worker_id"] = worker["id"]
    context.user_data["worker_name"] = worker["name"]
    await message.reply_text(
        f"<b>{escape(worker['name'])}</b> — сейчас дней: <b>{total:g}</b>\n"
        f"Работник {idx + 1} из {len(queue)}.\n\n"
        "Введите изменение дней: <code>1</code>, <code>0.5</code>, "
        "<code>-1</code> или <code>-0.5</code>. Чтобы не менять, введите <code>0</code>.\n\n"
        "Корректировка применяется к сегодняшнему дню.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )


async def corr_delta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        delta = float(update.message.text.replace(",", "."))
        if not math.isfinite(delta):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число, например 1, -0.5 или 0.")
        return CORR_DELTA

    object_id = context.user_data["object_id"]
    worker_id = context.user_data["worker_id"]
    name = context.user_data["worker_name"]
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, days FROM work_logs WHERE object_id=? AND worker_id=? AND work_date=?",
            (object_id, worker_id, today)
        ) as cur:
            row = await cur.fetchone()
        if row:
            new_days = max(0, (row[1] or 0) + delta)
            await db.execute("UPDATE work_logs SET days=? WHERE id=?", (new_days, row[0]))
        elif delta > 0:
            await db.execute(
                "INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours) VALUES (?, ?, ?, ?, 0)",
                (object_id, worker_id, today, delta)
            )
        await audit_log(db, update, context, object_id, "Корректировка дней",
                        f"Работник: {name}; изменение {delta:+g} дн.; дата {today}")
        await db.commit()
        async with db.execute(
            "SELECT COALESCE(SUM(days),0) FROM work_logs WHERE object_id=? AND worker_id=?",
            (object_id, worker_id)
        ) as cur:
            total = (await cur.fetchone())[0]

    sign = "+" if delta > 0 else ""
    await update.message.reply_text(
        f"✅ {escape(name)}: {sign}{delta:g} дн.\nТеперь всего дней: <b>{total:g}</b>",
        parse_mode="HTML"
    )
    context.user_data["corr_index"] += 1
    if context.user_data["corr_index"] < len(context.user_data["corr_queue"]):
        await corr_prompt_current(update.message, context)
        return CORR_DELTA

    await update.message.reply_text(
        "✅ Корректировка выбранных работников завершена.",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    clear_temp_data(context)
    return ConversationHandler.END


# ================== ПОСТУПЛЕНИЕ ОТ ЗАКАЗЧИКА ==================
async def receipt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    await update.message.reply_text(
        "Сумма поступления/корректировки (например 50000 или -20000):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return RCPT_AMOUNT


async def receipt_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount == 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text("Введите целое число, отличное от нуля. Плюс добавляет поступление, минус уменьшает.")
        return RCPT_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text(
        "Комментарий (или «Нет»):",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Нет")], [KeyboardButton("❌ Отмена")]],
            resize_keyboard=True
        )
    )
    return RCPT_NOTE


async def receipt_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    note = "" if update.message.text.strip() in ("Нет", "-", "нет") else update.message.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO receipts (object_id, amount, note) VALUES (?, ?, ?)",
            (context.user_data["object_id"], context.user_data["amount"], note)
        )
        await audit_log(db, update, context, context.user_data["object_id"], "Поступление",
                        f"Сумма {context.user_data['amount']:+,} ₩; {note or 'без комментария'}")
        await db.commit()
        async with db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM receipts WHERE object_id = ?",
            (context.user_data["object_id"],)
        ) as cur:
            total_received = (await cur.fetchone())[0]
        async with db.execute("SELECT total_sum FROM objects WHERE id = ?", (context.user_data["object_id"],)) as cur:
            total_sum = (await cur.fetchone())[0] or 0
    left = total_sum - total_received
    await update.message.reply_text(
        f"✅ Корректировка поступления: {context.user_data['amount']:+,} ₩.\n\n"
        f"Всего получено: <b>{total_received:,} ₩</b>\n"
        f"Сумма объекта: {total_sum:,} ₩\n"
        f"Осталось получить: <b>{left:,} ₩</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    clear_temp_data(context)
    return ConversationHandler.END


# ================== СВЕРХУРОЧНЫЕ ==================
async def overtime_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = [[InlineKeyboardButton(w["name"], callback_data=f"otw_{w['id']}")] for w in workers]
    buttons.append([InlineKeyboardButton("👥 Всем работникам", callback_data="otw_all")])
    ot_rate = await get_setting("ot_rate", str(DEFAULT_OT_RATE))
    await update.message.reply_text(
        f"Кому добавить сверхурочные? Можно выбрать одного или сразу всех.\n(1 час = {int(float(ot_rate)):,} ₩)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return OT_WORKER


async def overtime_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target = query.data.split("_", 1)[1]
    if target == "all":
        workers = await get_workers(get_user_brigade_id(query.from_user.id, context))
        context.user_data["worker_ids"] = [w["id"] for w in workers]
        context.user_data["worker_names"] = [w["name"] for w in workers]
    else:
        wid = int(target)
        context.user_data["worker_ids"] = [wid]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT name FROM workers WHERE id=?", (wid,)) as cur:
                row = await cur.fetchone()
        context.user_data["worker_names"] = [row[0] if row else str(wid)]
    await query.message.reply_text(
        "Сколько часов добавить каждому выбранному работнику? (можно дробные, например 1.5):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return OT_HOURS


async def overtime_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        hours = float(update.message.text.replace(",", "."))
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число.")
        return OT_HOURS
    object_id = context.user_data["object_id"]
    worker_ids = context.user_data.get("worker_ids", [])
    if not worker_ids:
        await update.message.reply_text("Не выбраны работники.", reply_markup=main_keyboard(update.effective_user.id))
        clear_temp_data(context)
        return ConversationHandler.END
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for worker_id in worker_ids:
            if worker_id is None:
                continue
            async with db.execute(
                "SELECT id, overtime_hours FROM work_logs WHERE object_id=? AND worker_id=? AND work_date=?",
                (object_id, worker_id, today)
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute("UPDATE work_logs SET overtime_hours=? WHERE id=?", ((row[1] or 0) + hours, row[0]))
            else:
                await db.execute(
                    "INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours) VALUES (?, ?, ?, 0, ?)",
                    (object_id, worker_id, today, hours)
                )
        await audit_log(db, update, context, object_id, "Сверхурочные",
                        f"Часы {hours:g}; работники: {', '.join(context.user_data.get('worker_names', []))}")
        await db.commit()
    ot_rate = int(float(await get_setting("ot_rate", str(DEFAULT_OT_RATE))))
    pay = int(hours * ot_rate)
    await update.message.reply_text(
        f"✅ Каждому из выбранных ({', '.join(context.user_data.get('worker_names', []))}): +{hours:g} ч (+{pay:,} ₩ на человека)",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    clear_temp_data(context)
    return ConversationHandler.END


# ================== ИСТОРИЯ ОПЕРАЦИЙ ==================
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brigade_id = get_user_brigade_id(update.effective_user.id, context)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT a.created_at, a.user_name, a.username, a.action, a.details,
                      o.name AS object_name
               FROM audit_logs a LEFT JOIN objects o ON o.id = a.object_id
               WHERE a.brigade_id = ? ORDER BY a.id DESC LIMIT 40""",
            (brigade_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("История пока пустая.")
        return
    lines = ["<b>🕘 История операций (последние 40)</b>", ""]
    for r in rows:
        who = r["user_name"] or ("@" + r["username"] if r["username"] else "пользователь")
        obj = r["object_name"] or "объект не указан"
        lines.append(f"<b>{escape(str(r['action']))}</b> · {escape(str(r['created_at']))}")
        lines.append(f"Кто: {escape(str(who))} · Объект: {escape(str(obj))}")
        lines.append(escape(str(r["details"])))
        lines.append("")
    text = "\n".join(lines)
    if len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        text = text[:cut] if cut > 0 else text[:3900]
    await update.message.reply_text(text, parse_mode="HTML")


async def edit_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brigade_id = get_user_brigade_id(update.effective_user.id, context)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT h.created_at, h.user_name, h.table_name, h.field,
                      h.old_value, h.new_value, o.name AS object_name
               FROM edit_history h LEFT JOIN objects o ON o.id = h.object_id
               WHERE h.brigade_id = ? ORDER BY h.id DESC LIMIT 30""",
            (brigade_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("Правок ещё не было.")
        return
    table_ru = {"expenses": "Расход", "advances": "Аванс", "receipts": "Поступление",
                "work_logs": "Дни/OT", "objects": "Объект"}
    field_ru = {"amount": "сумма", "description": "описание", "note": "комментарий",
                "days": "дни", "overtime_hours": "часы OT", "total_sum": "сумма объекта"}
    lines = ["<b>✏️ История правок (последние 30)</b>", ""]
    for r in rows:
        tname = table_ru.get(r["table_name"], r["table_name"])
        fname = field_ru.get(r["field"], r["field"])
        obj = r["object_name"] or "—"
        lines.append(f"<b>{escape(str(r['created_at']))}</b> · {escape(r['user_name'] or '—')}")
        lines.append(f"{escape(tname)} · {escape(fname)} · объект: {escape(obj)}")
        lines.append(f"  Было: {escape(str(r['old_value'] or '—'))}")
        lines.append(f"  Стало: {escape(str(r['new_value'] or '—'))}")
        lines.append("")
    text = "\n".join(lines)
    if len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        text = text[:cut] if cut > 0 else text[:3900]
    await update.message.reply_text(text, parse_mode="HTML")


# ================== РАСХОДЫ ПО ДНЯМ ==================
async def expense_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала открой или выбери рабочий объект.", reply_markup=main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["expense_history_object_id"] = obj["id"]
    await update.message.reply_text(
        "📅 За какой период показать расходы?\n\n"
        "Напиши дату в формате ГГГГ-ММ-ДД, например 2026-10-22.\n"
        "Или напиши «Сегодня» либо «7 дней».\n\n"
        "❌ Отмена — вернуться в меню.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Сегодня"), KeyboardButton("7 дней")], [KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return DAILY_PICK_DATE


async def expense_history_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_keyboard(update.effective_user.id))
        clear_temp_data(context)
        return ConversationHandler.END
    today = date.today()
    if raw.lower() == "сегодня":
        start_day = end_day = today
    elif raw.lower() in ("7 дней", "7дней"):
        start_day, end_day = today - timedelta(days=6), today
    else:
        try:
            start_day = end_day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text("Не понял дату. Напиши, например, 2026-10-22, или выбери «Сегодня» / «7 дней».")
            return DAILY_PICK_DATE

    brigade_id = get_user_brigade_id(update.effective_user.id, context)
    object_id = context.user_data.get("expense_history_object_id")
    cat_names = {"food": "Питание", "transport": "Дорога", "housing": "Проживание", "technician": "Техника", "other": "Другое"}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        params = [brigade_id, start_day.isoformat(), end_day.isoformat(), object_id]
        async with db.execute(
            """SELECT e.created_at, e.category, e.amount, e.description, e.user_name, o.name AS object_name
               FROM expenses e JOIN objects o ON o.id=e.object_id
               WHERE o.brigade_id=? AND date(e.created_at) BETWEEN date(?) AND date(?) AND e.object_id = ?
               ORDER BY e.created_at, e.id""",
            params
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text(
            f"За период {start_day.isoformat()} — {end_day.isoformat()} расходов по выбранному объекту не найдено.",
            reply_markup=main_keyboard(update.effective_user.id)
        )
        clear_temp_data(context)
        return ConversationHandler.END

    from collections import defaultdict
    by_day = defaultdict(list)
    for row in rows:
        by_day[str(row["created_at"])[:10]].append(row)
    lines = [f"🧾 <b>Расходы по дням</b>", f"Объект: <b>{escape(str(rows[0]['object_name']))}</b>",
             f"Период: {start_day.isoformat()} — {end_day.isoformat()}", ""]
    grand_total = 0
    grand_categories = {}
    for day_key in sorted(by_day):
        day_total = 0
        day_categories = {}
        lines.append(f"<b>📅 {day_key}</b>")
        for row in by_day[day_key]:
            amount = int(row["amount"] or 0)
            day_total += amount
            grand_total += amount
            day_categories[row["category"]] = day_categories.get(row["category"], 0) + amount
            grand_categories[row["category"]] = grand_categories.get(row["category"], 0) + amount
            time_text = str(row["created_at"])[11:16]
            who = row["user_name"] or "Автор старой записи не сохранён"
            category = cat_names.get(row["category"], row["category"])
            desc = row["description"] or "без описания"
            lines.append(f"• {escape(category)} — <b>{amount:,} ₩</b> · {escape(time_text)}")
            lines.append(f"  Кто: {escape(str(who))}; {escape(str(desc))}")
        lines.append(f"Итого за день: <b>{day_total:,} ₩</b>")
        for cat, cat_total in sorted(day_categories.items()):
            lines.append(f"  · {escape(cat_names.get(cat, cat))}: {cat_total:,} ₩")
        lines.append("")
    lines.append("<b>ИТОГО ПО КАТЕГОРИЯМ ЗА ПЕРИОД:</b>")
    for cat, cat_total in sorted(grand_categories.items()):
        lines.append(f"• {escape(cat_names.get(cat, cat))}: {cat_total:,} ₩")
    lines.append(f"<b>ИТОГО ЗА ПЕРИОД: {grand_total:,} ₩</b>")
    output = "\n".join(lines)
    if len(output) <= 3900:
        await update.message.reply_text(output, parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id))
    else:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                await update.message.reply_text(chunk, parse_mode="HTML")
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            await update.message.reply_text(chunk, parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id))
    clear_temp_data(context)
    return ConversationHandler.END


# ================== ВСЁ ПО ДНЯМ ==================
async def daily_view_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.", reply_markup=main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["daily_obj_id"] = obj["id"]
    await update.message.reply_text(
        "📆 За какой день показать всё?\n\n"
        "Напиши дату ГГГГ-ММ-ДД, «Сегодня», «Вчера» или «7 дней».",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Сегодня"), KeyboardButton("Вчера")],
             [KeyboardButton("7 дней"), KeyboardButton("❌ Отмена")]],
            resize_keyboard=True
        )
    )
    return DAILY_PICK_DATE


async def daily_view_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    raw = update.message.text.strip().lower()
    today = date.today()
    if raw == "сегодня":
        start = end = today
    elif raw == "вчера":
        start = end = today - timedelta(days=1)
    elif raw in ("7 дней", "7дней"):
        start, end = today - timedelta(days=6), today
    else:
        try:
            start = end = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text("Не понял дату. Пример: 2026-10-22")
            return DAILY_PICK_DATE

    object_id = context.user_data["daily_obj_id"]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT l.work_date, w.name AS wname, l.days, l.overtime_hours, l.updated_at "
            "FROM work_logs l JOIN workers w ON w.id=l.worker_id "
            "WHERE l.object_id=? AND l.work_date BETWEEN ? AND ? ORDER BY l.work_date, w.name",
            (object_id, start.isoformat(), end.isoformat())
        ) as cur:
            logs = await cur.fetchall()
        async with db.execute(
            "SELECT a.created_at, w.name AS wname, a.amount, a.note, a.updated_at "
            "FROM advances a JOIN workers w ON w.id=a.worker_id "
            "WHERE a.object_id=? AND date(a.created_at) BETWEEN ? AND ? ORDER BY a.created_at",
            (object_id, start.isoformat(), end.isoformat())
        ) as cur:
            advances = await cur.fetchall()
        async with db.execute(
            "SELECT e.created_at, e.category, e.amount, e.description, e.user_name, e.updated_at "
            "FROM expenses e WHERE e.object_id=? AND date(e.created_at) BETWEEN ? AND ? ORDER BY e.created_at",
            (object_id, start.isoformat(), end.isoformat())
        ) as cur:
            expenses = await cur.fetchall()
        async with db.execute(
            "SELECT r.created_at, r.amount, r.note, r.updated_at "
            "FROM receipts r WHERE r.object_id=? AND date(r.created_at) BETWEEN ? AND ? ORDER BY r.created_at",
            (object_id, start.isoformat(), end.isoformat())
        ) as cur:
            receipts = await cur.fetchall()

    cat_names = {"food": "Питание", "transport": "Дорога", "housing": "Проживание", "technician": "Техника", "other": "Другое"}
    lines = [f"📆 <b>Всё по дням: {start.isoformat()} — {end.isoformat()}</b>", ""]
    has_any = False
    if logs:
        has_any = True
        lines.append("<b>📅 Рабочие дни:</b>")
        for r in logs:
            ot = f" + {r['overtime_hours']:g} ч OT" if r["overtime_hours"] else ""
            edited = " (правка)" if r["updated_at"] else ""
            lines.append(f"  • {r['work_date']} · {escape(r['wname'])}: {r['days']:g} дн.{ot}{edited}")
        lines.append("")
    if expenses:
        has_any = True
        lines.append("<b>🧾 Расходы:</b>")
        for r in expenses:
            edited = " (правка)" if r["updated_at"] else ""
            cat = cat_names.get(r["category"], r["category"])
            desc = r["description"] or "—"
            lines.append(f"  • {str(r['created_at'])[:16]} · {escape(cat)} — {r['amount']:,} ₩ · {escape(r['user_name'] or '—')} · {escape(desc)}{edited}")
        lines.append("")
    if advances:
        has_any = True
        lines.append("<b>💰 Авансы:</b>")
        for r in advances:
            edited = " (правка)" if r["updated_at"] else ""
            lines.append(f"  • {str(r['created_at'])[:16]} · {escape(r['wname'])}: {r['amount']:+,} ₩ · {escape(r['note'] or '—')}{edited}")
        lines.append("")
    if receipts:
        has_any = True
        lines.append("<b>💵 Поступления:</b>")
        for r in receipts:
            edited = " (правка)" if r["updated_at"] else ""
            lines.append(f"  • {str(r['created_at'])[:16]} · {r['amount']:+,} ₩ · {escape(r['note'] or '—')}{edited}")
        lines.append("")
    if not has_any:
        await update.message.reply_text("За этот период записей нет.", reply_markup=main_keyboard(update.effective_user.id))
        clear_temp_data(context)
        return ConversationHandler.END
    output = "\n".join(lines)
    if len(output) > 3900:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                await update.message.reply_text(chunk, parse_mode="HTML")
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            await update.message.reply_text(chunk, parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id))
    else:
        await update.message.reply_text(output, parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id))
    clear_temp_data(context)
    return ConversationHandler.END


# ================== РЕДАКТИРОВАНИЕ ЗАПИСЕЙ ==================
async def edit_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.", reply_markup=main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["edit_object_id"] = obj["id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Расходы", callback_data="editcat_exp")],
        [InlineKeyboardButton("💰 Авансы", callback_data="editcat_adv")],
        [InlineKeyboardButton("💵 Поступления", callback_data="editcat_rcpt")],
        [InlineKeyboardButton("📅 Рабочие дни", callback_data="editcat_log")],
        [InlineKeyboardButton("➕ Сверхурочные", callback_data="editcat_ot")],
    ])
    await update.message.reply_text(
        f"Объект: <b>{escape(obj['name'])}</b>\nЧто редактируем?",
        reply_markup=kb, parse_mode="HTML"
    )
    return EDIT_MENU


async def edit_pick_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    object_id = context.user_data["edit_object_id"]

    if data.startswith("editcat_"):
        cat = data.split("_", 1)[1]
        context.user_data["edit_table"] = cat
        rows = []
        labels = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if cat == "exp":
                async with db.execute(
                    "SELECT id, category, amount, description, created_at FROM expenses "
                    "WHERE object_id=? ORDER BY id DESC LIMIT 30", (object_id,)
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    labels.append(f"🧾 {r['category']} — {r['amount']:,} ₩ ({str(r['created_at'])[:16]})")
            elif cat == "adv":
                async with db.execute(
                    "SELECT a.id, w.name AS wname, a.amount, a.created_at FROM advances a "
                    "JOIN workers w ON w.id=a.worker_id "
                    "WHERE a.object_id=? ORDER BY a.id DESC LIMIT 30", (object_id,)
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    labels.append(f"💰 {r['wname']} — {r['amount']:+,} ₩ ({str(r['created_at'])[:16]})")
            elif cat == "rcpt":
                async with db.execute(
                    "SELECT id, amount, created_at FROM receipts "
                    "WHERE object_id=? ORDER BY id DESC LIMIT 30", (object_id,)
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    labels.append(f"💵 {r['amount']:+,} ₩ ({str(r['created_at'])[:16]})")
            elif cat == "log":
                async with db.execute(
                    "SELECT l.id, w.name AS wname, l.work_date, l.days FROM work_logs l "
                    "JOIN workers w ON w.id=l.worker_id "
                    "WHERE l.object_id=? ORDER BY l.work_date DESC LIMIT 30", (object_id,)
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    labels.append(f"📅 {r['work_date']} · {r['wname']} — {r['days']:g} дн.")
            elif cat == "ot":
                async with db.execute(
                    "SELECT l.id, w.name AS wname, l.work_date, l.overtime_hours FROM work_logs l "
                    "JOIN workers w ON w.id=l.worker_id "
                    "WHERE l.object_id=? AND l.overtime_hours>0 ORDER BY l.work_date DESC LIMIT 30", (object_id,)
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    labels.append(f"➕ {r['work_date']} · {r['wname']} — {r['overtime_hours']:g} ч")
        if not rows:
            await query.message.reply_text("Нет записей для редактирования.", reply_markup=main_keyboard(query.from_user.id))
            clear_temp_data(context)
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(lbl, callback_data=f"editid_{cat}_{r['id']}")] for lbl, r in zip(labels, rows)]
        await query.message.reply_text("Выберите запись:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_PICK_RECORD

    if data.startswith("editid_"):
        parts = data.split("_")
        cat = parts[1]
        rid = int(parts[2])
        context.user_data["edit_row_id"] = rid
        context.user_data["edit_cat"] = cat
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            old_label = ""
            if cat == "exp":
                async with db.execute("SELECT * FROM expenses WHERE id=?", (rid,)) as cur:
                    r = await cur.fetchone()
                old_label = f"Категория: {r['category']}\nСумма: {r['amount']:,} ₩\nОписание: {r['description'] or '—'}"
            elif cat == "adv":
                async with db.execute("SELECT * FROM advances WHERE id=?", (rid,)) as cur:
                    r = await cur.fetchone()
                old_label = f"Сумма: {r['amount']:+,} ₩\nКомментарий: {r['note'] or '—'}"
            elif cat == "rcpt":
                async with db.execute("SELECT * FROM receipts WHERE id=?", (rid,)) as cur:
                    r = await cur.fetchone()
                old_label = f"Сумма: {r['amount']:+,} ₩\nКомментарий: {r['note'] or '—'}"
            elif cat == "log":
                async with db.execute("SELECT * FROM work_logs WHERE id=?", (rid,)) as cur:
                    r = await cur.fetchone()
                old_label = f"Дата: {r['work_date']}\nДни: {r['days']:g}"
            elif cat == "ot":
                async with db.execute("SELECT * FROM work_logs WHERE id=?", (rid,)) as cur:
                    r = await cur.fetchone()
                old_label = f"Дата: {r['work_date']}\nСверхурочные: {r['overtime_hours']:g} ч"
        prompt = {
            "exp": "Введите новую сумму и описание через |, например: 50000 | обед",
            "adv": "Введите новую сумму (можно с минусом) и комментарий через |, например: 30000 | аванс",
            "rcpt": "Введите новую сумму и комментарий через |, например: 100000 | оплата",
            "log": "Введите новое количество дней (например 1, 0.5, 0):",
            "ot": "Введите новое количество часов (например 2, 1.5, 0):",
        }[cat]
        await query.message.reply_text(
            f"<b>Текущее значение:</b>\n{escape(old_label)}\n\n{prompt}",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
        )
        return EDIT_NEW_VALUE
    return EDIT_PICK_RECORD


async def edit_apply_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    cat = context.user_data.get("edit_cat")
    rid = context.user_data.get("edit_row_id")
    if cat is None or rid is None:
        await update.message.reply_text("Что-то пошло не так, начните заново.", reply_markup=main_keyboard(update.effective_user.id))
        clear_temp_data(context)
        return ConversationHandler.END
    text = update.message.text.strip()
    user = update.effective_user
    brigade_id = get_user_brigade_id(user.id, context)
    now = datetime.now().isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            if cat == "exp":
                parts = [p.strip() for p in text.split("|", 1)]
                amount = int(parts[0].replace(" ", "").replace(",", ""))
                desc = parts[1] if len(parts) > 1 else ""
                if amount <= 0:
                    raise ValueError("Сумма должна быть > 0")
                async with db.execute("SELECT * FROM expenses WHERE id=?", (rid,)) as cur:
                    old = await cur.fetchone()
                await db.execute(
                    "UPDATE expenses SET amount=?, description=?, updated_at=?, updated_by=? WHERE id=?",
                    (amount, desc, now, user.id, rid)
                )
                await log_edit(db, brigade_id, old["object_id"], "expenses", rid, "amount", old["amount"], amount, user)
                await log_edit(db, brigade_id, old["object_id"], "expenses", rid, "description", old["description"] or "", desc, user)
            elif cat == "adv":
                parts = [p.strip() for p in text.split("|", 1)]
                amount = int(parts[0].replace(" ", "").replace(",", ""))
                note = parts[1] if len(parts) > 1 else ""
                if amount == 0:
                    raise ValueError("Сумма не может быть 0")
                async with db.execute("SELECT * FROM advances WHERE id=?", (rid,)) as cur:
                    old = await cur.fetchone()
                await db.execute(
                    "UPDATE advances SET amount=?, note=
