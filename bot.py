#!/usr/bin/env python3
"""
Telegram-бот для учёта рабочих дней бригады (солнечные панели)
Библиотека: python-telegram-bot + aiosqlite
"""

import asyncio
import logging
import os
from datetime import datetime, date
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
BOT_TOKEN = "8714625237:AAGOKvrckFK9cvHlAP0XF8rSfDA8xXDAEM4"  # <-- Вставьте токен сюда

# Главный (вы) — видит все бригады и может переключаться
SUPER_ADMIN_IDS = [
    121318132,   # <-- ВАШ Telegram ID
]

# Кто к какой бригаде привязан (полный доступ только к своей)
# brigade_id: 1 = Основная, 2 = Команда Дастана
USER_BRIGADE = {
    # 111111111: 1,   # помощник основной бригады
    722258878: 2,    # Команда Дастана
}

# На Railway с Volume: переменная DB_PATH=/data/brigade.db
DB_PATH = os.environ.get("DB_PATH", "brigade.db")
DEFAULT_OT_RATE = 20000  # ₩ за 1 час сверхурочных
HOURS_PER_DAY = 8

# Состояния ConversationHandler
(
    MARK_DATE, MARK_WORKERS, MARK_OVERTIME,
    ADV_WORKER, ADV_AMOUNT, ADV_NOTE,
    EXP_AMOUNT, EXP_DESC,
    OBJ_TYPE, OBJ_NAME, OBJ_SUM,
    ADD_W_NAME, ADD_W_RATE, ADD_W_PERCENT,
    EDIT_W_VALUE, OT_COEFF,
    OT_WORKER, OT_HOURS,
    RCPT_AMOUNT, RCPT_NOTE,
    CORR_WORKER, CORR_DELTA
) = range(22)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ================== БАЗА ДАННЫХ ==================
async def init_db():
    # создаём папку для базы (нужно для Volume /data)
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
        """)
        # Бригады
        await db.execute("INSERT OR IGNORE INTO brigades (id, name) VALUES (1, 'Основная')")
        await db.execute("INSERT OR IGNORE INTO brigades (id, name) VALUES (2, 'Команда Дастана')")

        # Миграции старых баз
        for col_sql in [
            "ALTER TABLE workers ADD COLUMN brigade_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE objects ADD COLUMN brigade_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE objects ADD COLUMN work_type TEXT DEFAULT 'profit'",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass

        # Работники основной бригады
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


async def get_brigade_name(brigade_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM brigades WHERE id = ?", (brigade_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else f"Бригада {brigade_id}"


async def get_active_object(brigade_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM objects WHERE status = 'open' AND brigade_id = ? ORDER BY id DESC LIMIT 1",
            (brigade_id,)
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
        "В конце объекта нажмите «Закрыть объект»."
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
        "<b>Формула:</b>\n"
        "Прибыль = Сумма объекта − Зарплаты − Расходы\n"
        "Прибыль делится: Хушнуд 40%, Бато/Мумин/Дастан по 20%"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keep = context.user_data.get("active_brigade_id")
    context.user_data.clear()
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
    await query.answer()
    if not is_super(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    bid = int(query.data.split("_")[1])
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
        text = f"<b>Текущий объект:</b> {obj['name']}\nТип: {type_label}\nСумма: {obj['total_sum']:,} ₩\nСтатус: открыт"
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
            f"<b>Объект:</b> {obj['name']}\n"
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
        report = await generate_report(obj["id"])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE objects SET status = 'closed', closed_at = ? WHERE id = ?",
                             (datetime.now().isoformat(), obj["id"]))
            await db.commit()
        await query.message.reply_text(f"🔒 Объект «{obj['name']}» закрыт.\n\n{report}", parse_mode="HTML",
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

    # проверка, что объект реально в базе
    obj = await get_active_object(bid)
    type_text = "за илтан" if work_type == "daily" else "с прибылью"
    bname = await get_brigade_name(bid)
    if obj and obj.get("name") == name:
        msg = (
            f"✅ Объект «{name}» создан ({type_text})\n"
            f"Бригада: {bname}\n"
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
    await update.message.reply_text(msg, reply_markup=main_keyboard(update.effective_user.id))
    keep = context.user_data.get("active_brigade_id")
    context.user_data.clear()
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
            await db.commit()
        names = ", ".join(w["name"] for w in selected_workers)
        day_text = "полный день" if days_val == 1 else "полдня"
        ot_rate = await get_setting("ot_rate", str(DEFAULT_OT_RATE))
        await query.message.reply_text(
            f"✅ Отмечено за {work_date} ({day_text}):\n{names}\n\n"
            f"Сверхурочные — кнопка «➕ Сверхурочные» ({int(float(ot_rate)):,} ₩/час).",
            reply_markup=main_keyboard(query.from_user.id)
        )
        context.user_data.clear()
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


async def mark_overtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        hours = float(update.message.text.replace(",", "."))
        if hours < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число ≥ 0")
        return MARK_OVERTIME

    selected_workers = context.user_data["selected_workers"]
    ot_index = context.user_data["ot_index"]
    overtime = context.user_data.get("overtime", {})
    worker = selected_workers[ot_index]
    overtime[worker["id"]] = hours
    ot_index += 1

    if ot_index >= len(selected_workers):
        object_id = context.user_data["object_id"]
        work_date = context.user_data["work_date"]
        async with aiosqlite.connect(DB_PATH) as db:
            for w in selected_workers:
                await db.execute(
                    """INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(object_id, worker_id, work_date)
                       DO UPDATE SET days=1, overtime_hours=excluded.overtime_hours""",
                    (object_id, w["id"], work_date, overtime.get(w["id"], 0))
                )
            await db.commit()
        names = ", ".join(w["name"] for w in selected_workers)
        await update.message.reply_text(f"✅ Отмечено за {work_date}:\n{names}",
                                        reply_markup=main_keyboard(update.effective_user.id))
        context.user_data.clear()
        return ConversationHandler.END
    else:
        context.user_data["ot_index"] = ot_index
        context.user_data["overtime"] = overtime
        next_w = selected_workers[ot_index]
        await update.message.reply_text(f"Сверхурочные часы для <b>{next_w['name']}</b> (0 если нет):", parse_mode="HTML")
        return MARK_OVERTIME


# ================== АВАНС ==================
async def advance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = [[InlineKeyboardButton(w["name"], callback_data=f"adv_{w['id']}")] for w in workers]
    await update.message.reply_text("Кому выдать аванс?", reply_markup=InlineKeyboardMarkup(buttons))
    return ADV_WORKER


async def advance_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid = int(query.data.split("_")[1])
    context.user_data["worker_id"] = wid
    await query.message.reply_text("Сумма аванса (только число):",
                                   reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True))
    return ADV_AMOUNT


async def advance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число.")
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO advances (object_id, worker_id, amount, note) VALUES (?, ?, ?, ?)",
            (context.user_data["object_id"], context.user_data["worker_id"], context.user_data["amount"], note)
        )
        await db.commit()
        async with db.execute("SELECT name FROM workers WHERE id = ?", (context.user_data["worker_id"],)) as cur:
            name = (await cur.fetchone())[0]
    await update.message.reply_text(f"✅ Аванс {context.user_data['amount']:,} ₩ выдан: {name}",
                                    reply_markup=main_keyboard(update.effective_user.id))
    context.user_data.clear()
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
    return EXP_AMOUNT  # после выбора категории сразу сумма


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
            "INSERT INTO expenses (object_id, category, amount, description) VALUES (?, ?, ?, ?)",
            (context.user_data["object_id"], context.user_data["category"], context.user_data["amount"], desc)
        )
        await db.commit()
    await update.message.reply_text(
        f"✅ Расход {context.user_data['amount']:,} ₩ ({cat_names.get(context.user_data['category'])}) добавлен.",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    context.user_data.clear()
    return ConversationHandler.END


# ================== РАБОТНИКИ ==================
async def workers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context), active_only=False)
    text = "<b>Список работников:</b>\n\n"
    for w in workers:
        status = "✅" if w["is_active"] else "❌"
        pct = f" + {w['percent']}%" if w["percent"] else ""
        text += f"{status} <b>{w['name']}</b> — {w['daily_rate']:,} ₩/день{pct}\n"
    kb = None
    if is_admin(update.effective_user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить", callback_data="w_add")],
            [InlineKeyboardButton("✏️ Изменить ставку/%", callback_data="w_edit")],
            [InlineKeyboardButton("🗑 Удалить работника", callback_data="w_delete")],
        ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def start_add_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход в диалог добавления работника (только через ConversationHandler)."""
    query = update.callback_query
    await query.answer()
    # сохраняем бригаду, чистим временные поля прошлого диалога
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
        workers = await get_workers(get_user_brigade_id(update.effective_user.id, context), active_only=False)
        buttons = [[InlineKeyboardButton(w["name"], callback_data=f"wedit_{w['id']}")] for w in workers]
        await query.message.reply_text("Кого изменить?", reply_markup=InlineKeyboardMarkup(buttons))
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
        # Деактивируем (история сохраняется)
        await db.execute("UPDATE workers SET is_active = 0 WHERE id = ?", (wid,))
        await db.commit()
    await query.message.reply_text(
        f"✅ Работник <b>{name}</b> удалён из списка.\n"
        "(История его дней и авансов сохранена)",
        parse_mode="HTML",
        reply_markup=main_keyboard(query.from_user.id)
    )


async def add_worker_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    context.user_data["new_name"] = update.message.text.strip()
    await update.message.reply_text("Дневная ставка (число):")
    return ADD_W_RATE


async def add_worker_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = int(update.message.text.replace(" ", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Введите число.")
        return ADD_W_RATE
    context.user_data["new_rate"] = rate
    await update.message.reply_text("Процент от прибыли (0 если нет):")
    return ADD_W_PERCENT


async def add_worker_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pct = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число.")
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
            return ConversationHandler.END
    name = context.user_data.get("new_name", "")
    rate = context.user_data.get("new_rate", 0)
    await update.message.reply_text(
        f"✅ {name} добавлен: {rate:,} ₩ + {pct}%",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    keep = context.user_data.get("active_brigade_id")
    context.user_data.clear()
    if keep:
        context.user_data["active_brigade_id"] = keep
    return ConversationHandler.END


# ================== КОРРЕКТИРОВКА ДНЕЙ ==================
async def corr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    workers = await get_workers(get_user_brigade_id(update.effective_user.id, context))
    buttons = [[InlineKeyboardButton(w["name"], callback_data=f"corrw_{w['id']}")] for w in workers]
    await update.message.reply_text(
        "Кому скорректировать дни?\n(можно добавить или убавить, в т.ч. 0.5)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CORR_WORKER


async def corr_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid = int(query.data.split("_")[1])
    context.user_data["worker_id"] = wid
    # Показать текущие дни
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(days),0) FROM work_logs WHERE object_id=? AND worker_id=?",
            (context.user_data["object_id"], wid)
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT name FROM workers WHERE id=?", (wid,)) as cur:
            name = (await cur.fetchone())[0]
    context.user_data["worker_name"] = name
    await query.message.reply_text(
        f"<b>{name}</b> — сейчас дней: <b>{total:g}</b>\n\n"
        "Введите сколько добавить или убавить:\n"
        "Примеры: <code>1</code>  <code>0.5</code>  <code>-1</code>  <code>-0.5</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return CORR_DELTA


async def corr_delta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        delta = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число, например 1 или -0.5")
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
            new_days = (row[1] or 0) + delta
            if new_days < 0:
                new_days = 0
            await db.execute("UPDATE work_logs SET days=? WHERE id=?", (new_days, row[0]))
        else:
            # Создаём запись на сегодня с корректировкой
            days_val = max(delta, 0)
            await db.execute(
                "INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours) VALUES (?, ?, ?, ?, 0)",
                (object_id, worker_id, today, days_val)
            )
        await db.commit()
        async with db.execute(
            "SELECT COALESCE(SUM(days),0) FROM work_logs WHERE object_id=? AND worker_id=?",
            (object_id, worker_id)
        ) as cur:
            total = (await cur.fetchone())[0]
    sign = "+" if delta > 0 else ""
    await update.message.reply_text(
        f"✅ {name}: {sign}{delta:g} дн.\nТеперь всего дней: <b>{total:g}</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    context.user_data.clear()
    return ConversationHandler.END


# ================== ПОСТУПЛЕНИЕ ОТ ЗАКАЗЧИКА ==================
async def receipt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Сначала создайте объект.")
        return ConversationHandler.END
    context.user_data["object_id"] = obj["id"]
    await update.message.reply_text(
        "Сумма поступления от заказчика (только число):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return RCPT_AMOUNT


async def receipt_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        amount = int(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число.")
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
        f"✅ Поступление {context.user_data['amount']:,} ₩ добавлено.\n\n"
        f"Всего получено: <b>{total_received:,} ₩</b>\n"
        f"Сумма объекта: {total_sum:,} ₩\n"
        f"Осталось получить: <b>{left:,} ₩</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    context.user_data.clear()
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
    await update.message.reply_text(
        "Кому добавить сверхурочные?\n(1 час = 20 000 ₩)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return OT_WORKER


async def overtime_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid = int(query.data.split("_")[1])
    context.user_data["worker_id"] = wid
    await query.message.reply_text(
        "Сколько часов добавить? (можно дробные, например 1.5):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )
    return OT_HOURS


async def overtime_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        hours = float(update.message.text.replace(",", "."))
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число.")
        return OT_HOURS
    object_id = context.user_data["object_id"]
    worker_id = context.user_data["worker_id"]
    # Добавляем к сегодняшней дате или к последней записи
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем есть ли запись на сегодня
        async with db.execute(
            "SELECT id, overtime_hours FROM work_logs WHERE object_id=? AND worker_id=? AND work_date=?",
            (object_id, worker_id, today)
        ) as cur:
            row = await cur.fetchone()
        if row:
            new_ot = (row[1] or 0) + hours
            await db.execute("UPDATE work_logs SET overtime_hours=? WHERE id=?", (new_ot, row[0]))
        else:
            # Создаём запись только со сверхурочными (days=0)
            await db.execute(
                "INSERT INTO work_logs (object_id, worker_id, work_date, days, overtime_hours) VALUES (?, ?, ?, 0, ?)",
                (object_id, worker_id, today, hours)
            )
        await db.commit()
        async with db.execute("SELECT name FROM workers WHERE id=?", (worker_id,)) as cur:
            name = (await cur.fetchone())[0]
    ot_rate = int(float(await get_setting("ot_rate", str(DEFAULT_OT_RATE))))
    pay = int(hours * ot_rate)
    await update.message.reply_text(
        f"✅ {name}: +{hours:g} ч сверхурочных (+{pay:,} ₩)",
        reply_markup=main_keyboard(update.effective_user.id)
    )
    context.user_data.clear()
    return ConversationHandler.END


# ================== ОТЧЁТ ==================
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = await get_active_object(get_user_brigade_id(update.effective_user.id, context))
    if not obj:
        await update.message.reply_text("Нет активного объекта.")
        return
    report = await generate_report(obj["id"])
    await update.message.reply_text(report, parse_mode="HTML")


async def generate_report(object_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM objects WHERE id = ?", (object_id,)) as cur:
            obj = dict(await cur.fetchone())
        workers = await get_workers(get_user_brigade_id(update.effective_user.id, context), active_only=False)
        worker_map = {w["id"]: w for w in workers}

        async with db.execute(
            "SELECT worker_id, SUM(days) as total_days, SUM(overtime_hours) as total_ot FROM work_logs WHERE object_id = ? GROUP BY worker_id",
            (object_id,)
        ) as cur:
            logs = [dict(r) for r in await cur.fetchall()]

        ot_rate = int(float(await get_setting("ot_rate", str(DEFAULT_OT_RATE))))
        wages = {}
        days_info = {}
        total_wages = 0
        for log in logs:
            wid = log["worker_id"]
            w = worker_map.get(wid)
            if not w:
                continue
            days = log["total_days"] or 0
            ot = log["total_ot"] or 0
            base = days * w["daily_rate"]
            ot_pay = ot * ot_rate
            amount = int(base + ot_pay)
            wages[wid] = amount
            days_info[wid] = (days, ot)
            total_wages += amount

        async with db.execute("SELECT category, SUM(amount) as s FROM expenses WHERE object_id = ? GROUP BY category", (object_id,)) as cur:
            exp_by_cat = {r["category"]: r["s"] for r in await cur.fetchall()}
        total_expenses = sum(exp_by_cat.values())

        async with db.execute("SELECT worker_id, SUM(amount) as s FROM advances WHERE object_id = ? GROUP BY worker_id", (object_id,)) as cur:
            advances = {r["worker_id"]: r["s"] for r in await cur.fetchall()}

        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM receipts WHERE object_id = ?", (object_id,)) as cur:
            total_received = (await cur.fetchone())[0] or 0

        work_type = obj.get("work_type") or "profit"
        is_daily = work_type == "daily"

        revenue = obj["total_sum"] or 0
        profit = 0 if is_daily else (revenue - total_wages - total_expenses)

        profit_share = {}
        for w in workers:
            if is_daily:
                profit_share[w["id"]] = 0
            else:
                profit_share[w["id"]] = int(profit * w["percent"] / 100) if w["percent"] and profit > 0 else 0

        type_label = "📅 Работа за илтан" if is_daily else "💰 С прибылью"
        lines = [
            f"<b>📋 ОТЧЁТ: {obj['name']}</b>",
            f"Тип: {type_label}",
            f"Статус: {obj['status']}",
            "",
        ]
        if revenue:
            lines.append(f"💰 <b>Сумма объекта:</b> {revenue:,} ₩")
        if total_received or revenue:
            lines.append(f"💵 <b>Получено от заказчика:</b> {total_received:,} ₩")
            if revenue:
                lines.append(f"📉 <b>Осталось получить:</b> {revenue - total_received:,} ₩")
        lines.append(f"👷 <b>Зарплаты (всего):</b> {total_wages:,} ₩")
        lines.append(f"🧾 <b>Расходы (всего):</b> {total_expenses:,} ₩")
        cat_names = {"food": "Питание", "transport": "Дорога", "housing": "Проживание", "technician": "Техника", "other": "Другое"}
        for c, s in exp_by_cat.items():
            lines.append(f"   • {cat_names.get(c, c)}: {s:,} ₩")
        if not is_daily:
            lines.append(f"📈 <b>Прибыль (остаток):</b> {profit:,} ₩")
        lines += [
            "",
            "<b>Выплаты работникам:</b>",
            "─" * 20,
        ]

        grand_total = 0
        for w in sorted(workers, key=lambda x: -(x["percent"] or 0)):
            wid = w["id"]
            wage = wages.get(wid, 0)
            share = profit_share.get(wid, 0)
            adv = advances.get(wid, 0)
            to_pay = wage + share - adv
            if days_info.get(wid, (0, 0))[0] == 0 and share == 0 and adv == 0:
                continue
            grand_total += max(to_pay, 0)
            days, ot = days_info.get(wid, (0, 0))
            lines.append(f"<b>{w['name']}</b>")
            if days:
                lines.append(f"  Дней: {days:g}" + (f" + {ot:g} ч сверхурочно" if ot else ""))
                lines.append(f"  Зарплата: {wage:,} ₩")
            if share:
                lines.append(f"  Доля прибыли ({w['percent']}%): {share:,} ₩")
            if adv:
                lines.append(f"  Авансы: −{adv:,} ₩")
            lines.append(f"  <b>К выплате: {to_pay:,} ₩</b>")
            lines.append("")

        lines.append("─" * 20)
        lines.append(f"<b>Итого к выплате: {grand_total:,} ₩</b>")
        if not is_daily and profit < 0:
            lines.append("\n⚠️ Прибыль отрицательная.")
        return "\n".join(lines)


# ================== НАСТРОЙКИ ==================
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    rate = await get_setting("ot_rate", str(DEFAULT_OT_RATE))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Сверхурочные: {int(float(rate)):,} ₩/час", callback_data="set_ot")]])
    await update.message.reply_text("Настройки:", reply_markup=kb)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "set_ot":
        await query.message.reply_text(
            "Введите оплату за 1 час сверхурочных в вонах\n(например 20000 или 30000):",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
        )
        return OT_COEFF


async def set_ot_coeff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        val = int(update.message.text.replace(" ", "").replace(",", ""))
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите целое положительное число.")
        return OT_COEFF
    await set_setting("ot_rate", str(val))
    await update.message.reply_text(f"✅ Сверхурочные: {val:,} ₩/час", reply_markup=main_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ================== ЗАПУСК ==================
async def post_init(app: Application):
    await init_db()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Защита: только люди из ADMIN_IDS
    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    # Простые команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📊 Отчёт$"), report_cmd))
    app.add_handler(MessageHandler(filters.Regex("^👷 Работники$"), workers_menu))
    app.add_handler(MessageHandler(filters.Regex("^🏗️ Объект$"), object_menu))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), settings_menu))
    app.add_handler(MessageHandler(filters.Regex("^🔄 Сменить бригаду$"), switch_brigade_menu))
    app.add_handler(CallbackQueryHandler(switch_brigade_callback, pattern="^swbrig_"))

    # Объект
    # Общий обработчик объектов (кроме создания и суммы — их ловят ConversationHandler)
    app.add_handler(CallbackQueryHandler(obj_callback, pattern="^obj_(info|close|close_yes|close_no)$"))
    app.add_handler(CallbackQueryHandler(workers_callback, pattern="^w_(edit|delete)$"))
    app.add_handler(CallbackQueryHandler(worker_delete_confirm, pattern="^wdel_"))

    # Корректировка дней
    conv_corr = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ Корректировка дней$"), corr_start)],
        states={
            CORR_WORKER: [CallbackQueryHandler(corr_worker, pattern="^corrw_")],
            CORR_DELTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, corr_delta)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_corr)

    # Отметить день
    conv_mark = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📅 Отметить день$"), mark_day_start)],
        states={
            MARK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mark_date)],
            MARK_WORKERS: [CallbackQueryHandler(mark_workers_callback, pattern="^wd_")],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_mark)

    # Сверхурочные
    conv_ot = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Сверхурочные$"), overtime_start)],
        states={
            OT_WORKER: [CallbackQueryHandler(overtime_worker, pattern="^otw_")],
            OT_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, overtime_hours)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_ot)

    # Поступление от заказчика
    conv_rcpt = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💵 Поступление$"), receipt_start)],
        states={
            RCPT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_amount)],
            RCPT_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_note)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_rcpt)

    # Аванс работнику
    conv_adv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Аванс работнику$"), advance_start)],
        states={
            ADV_WORKER: [CallbackQueryHandler(advance_worker, pattern="^adv_")],
            ADV_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, advance_amount)],
            ADV_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, advance_note)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_adv)

    # Расход
    conv_exp = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🧾 Расход$"), expense_start)],
        states={
            EXP_AMOUNT: [
                CallbackQueryHandler(expense_cat, pattern="^exp_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount),
            ],
            EXP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_desc)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_exp)

    # Добавление работника
    conv_add_w = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_worker, pattern="^w_add$")],
        states={
            ADD_W_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_name)],
            ADD_W_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_rate)],
            ADD_W_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_percent)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv_add_w)

    # Настройки
    conv_set = ConversationHandler(
        entry_points=[CallbackQueryHandler(settings_callback, pattern="^set_ot$")],
        states={OT_COEFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_ot_coeff)]},
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_set)

    # Создание объекта
    conv_obj = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_new_object, pattern="^obj_new$")],
        states={
            OBJ_TYPE: [CallbackQueryHandler(choose_work_type, pattern="^type_")],
            OBJ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, obj_name)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv_obj)

    # Установка суммы
    conv_sum = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_set_sum, pattern="^obj_set_sum$")],
        states={OBJ_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, obj_sum)]},
        fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), cancel)],
    )
    app.add_handler(conv_sum)

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
