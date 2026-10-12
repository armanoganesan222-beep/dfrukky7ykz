
import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config


# ============================================================
# CONFIG — config.py НЕ МЕНЯЕМ
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vpn_shop")

BOT_TOKEN = config.BOT_TOKEN
ADMIN_ID = int(config.ADMIN_ID)
DB_PATH = getattr(config, "DATABASE", "vpn_bot.db")
BOT_NAME = getattr(config, "BOT_NAME", "VPN SHOP")
LOCAL_TZ = timezone(timedelta(hours=4), name="Asia/Yerevan")

PAYMENT_DEFAULT = (
    "💳 <b>ОПЛАТА ПОДПИСКИ</b>\n\n"
    "🏦 Банк: Сбер\n"
    "💳 Номер карты: 2202209226426665\n"
    "👤 Получатель: Артем\n\n"
    "💰 К оплате: {price} ₽\n"
    "📦 Тариф: {tariff}\n"
    "📅 Срок: {days} дней\n\n"
    "После оплаты нажмите «✅ Я оплатил» и отправьте чек."
)

HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")


def db_execute(query: str, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def db_one(query: str, params=()):
    return db.execute(query, params).fetchone()


def db_all(query: str, params=()):
    return db.execute(query, params).fetchall()


def ensure_column(table: str, column: str, definition: str):
    columns = {
        row[1]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def local_date(value: Optional[str]) -> str:
    result = parse_dt(value)
    return (
        result.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        if result else "—"
    )


def setup_database():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            first_seen TEXT,
            last_seen TEXT,
            orders_count INTEGER NOT NULL DEFAULT 0,
            total_spent REAL NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            tariff TEXT NOT NULL DEFAULT 'VPN',
            price INTEGER NOT NULL DEFAULT 0,
            days INTEGER NOT NULL DEFAULT 30,
            status TEXT NOT NULL DEFAULT 'waiting_admin',
            receipt_file_id TEXT,
            vpn_key TEXT,
            created_at TEXT,
            issued_at TEXT,
            expires_at TEXT,
            receipt_type TEXT DEFAULT 'photo',
            promo_code TEXT,
            discount INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tariffs (
            tariff_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            days INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            note TEXT DEFAULT '',
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY COLLATE NOCASE,
            discount INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            per_user INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
    """)

    migrations = {
        "users": {
            "username": "TEXT DEFAULT ''",
            "first_name": "TEXT DEFAULT ''",
            "first_seen": "TEXT",
            "last_seen": "TEXT",
            "orders_count": "INTEGER NOT NULL DEFAULT 0",
            "total_spent": "REAL NOT NULL DEFAULT 0",
            "blocked": "INTEGER NOT NULL DEFAULT 0",
        },
        "orders": {
            "username": "TEXT DEFAULT ''",
            "tariff": "TEXT NOT NULL DEFAULT 'VPN'",
            "price": "INTEGER NOT NULL DEFAULT 0",
            "days": "INTEGER NOT NULL DEFAULT 30",
            "status": "TEXT NOT NULL DEFAULT 'waiting_admin'",
            "receipt_file_id": "TEXT",
            "vpn_key": "TEXT",
            "created_at": "TEXT",
            "issued_at": "TEXT",
            "expires_at": "TEXT",
            "receipt_type": "TEXT DEFAULT 'photo'",
            "promo_code": "TEXT",
            "discount": "INTEGER NOT NULL DEFAULT 0",
        },
        "whitelist": {
            "note": "TEXT DEFAULT ''",
            "added_at": "TEXT NOT NULL DEFAULT ''",
        },
    }

    for table, columns in migrations.items():
        for column, definition in columns.items():
            ensure_column(table, column, definition)

    defaults = {
        "welcome": getattr(
            config,
            "WELCOME_TEXT",
            f"👋 Добро пожаловать в {html.escape(BOT_NAME)}!\n\n"
            "Выберите действие ниже.",
        ),
        "payment": PAYMENT_DEFAULT,
        "support": (
            "💬 <b>Поддержка</b>\n\n"
            "По вопросам оплаты и подключения напишите администратору."
        ),
        "instruction": (
            "📖 <b>Как подключиться через HAPP</b>\n\n"
            "1️⃣ Установите HAPP по кнопке для своего устройства.\n"
            "2️⃣ Вернитесь в сообщение с VPN-ключом.\n"
            "3️⃣ Скопируйте ключ целиком.\n"
            "4️⃣ Откройте HAPP и найдите добавление профиля.\n"
            "5️⃣ Выберите импорт из буфера обмена.\n"
            "6️⃣ Выберите профиль и нажмите подключение.\n"
            "7️⃣ На iPhone подтвердите добавление VPN-конфигурации, "
            "если появится системный запрос.\n\n"
            "HAPP — приложение-клиент. Для подключения нужен "
            "действующий совместимый ключ."
        ),
        "delivery": (
            "🎉 <b>VPN готов!</b>\n\n"
            "📦 Тариф: <b>{tariff}</b>\n"
            "📅 Срок: <b>{days} дней</b>\n"
            "📆 Действует до: <b>{expires}</b>\n"
            "🧾 Заказ: <b>#{order_id}</b>\n\n"
            "🔐 <b>Ваш ключ:</b>\n<code>{key}</code>\n\n"
            "Скопируйте ключ целиком и добавьте его в HAPP.\n"
            "Кнопки ниже ведут прямо в магазины приложений."
        ),
        "whitelist_info": (
            "🤍 <b>Белый список</b>\n\n"
            "Пользователи из белого списка могут пользоваться ботом "
            "без проверки обязательной подписки.\n"
            "Белый список не выдаёт ключ и не заменяет оплату VPN."
        ),
        "required_channel": "",
        "required_channel_url": "",
    }

    for key, value in defaults.items():
        db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (key, str(value)),
        )

    if not db.execute(
        "SELECT 1 FROM settings WHERE key='payment_text_v3'"
    ).fetchone():
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('payment',?)",
            (PAYMENT_DEFAULT,),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) "
            "VALUES('payment_text_v3','1')"
        )

    source_tariffs = getattr(config, "TARIFFS", {}) or {
        "vpn_7": {"name": "VPN на 7 дней", "price": 99, "days": 7},
        "vpn_30": {"name": "VPN на 30 дней", "price": 199, "days": 30},
        "vpn_90": {"name": "VPN на 90 дней", "price": 499, "days": 90},
    }

    for index, (tariff_id, item) in enumerate(source_tariffs.items()):
        db.execute(
            """
            INSERT OR IGNORE INTO tariffs
                (tariff_id,name,price,days,description,enabled,sort_order)
            VALUES (?,?,?,?,?,1,?)
            """,
            (
                str(tariff_id),
                str(item.get("name", tariff_id)),
                max(0, int(item.get("price", 0))),
                max(1, int(item.get("days", 30))),
                str(item.get("description", "")),
                index,
            ),
        )

    if not db.execute("SELECT 1 FROM tariffs LIMIT 1").fetchone():
        fallback_tariffs = (
            ("vpn_7", "VPN на 7 дней", 99, 7),
            ("vpn_30", "VPN на 30 дней", 199, 30),
            ("vpn_90", "VPN на 90 дней", 499, 90),
        )
        for index, item in enumerate(fallback_tariffs):
            db.execute(
                """
                INSERT INTO tariffs
                    (tariff_id,name,price,days,description,enabled,sort_order)
                VALUES (?,?,?,?,?,1,?)
                """,
                (item[0], item[1], item[2], item[3], "", index),
            )

    db.commit()


setup_database()


def setting(key: str) -> str:
    row = db_one("SELECT value FROM settings WHERE key=?", (key,))
    return str(row["value"]) if row else ""


def set_setting(key: str, value: str):
    db_execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, value),
    )


def get_tariffs(enabled_only=True):
    if enabled_only:
        return db_all(
            "SELECT * FROM tariffs WHERE enabled=1 ORDER BY sort_order,tariff_id"
        )
    return db_all("SELECT * FROM tariffs ORDER BY sort_order,tariff_id")


def get_tariff(tariff_id: str):
    return db_one(
        "SELECT * FROM tariffs WHERE tariff_id=?",
        (tariff_id,),
    )


def touch_user(user):
    current = iso(now_utc())
    existing = db_one(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,),
    )
    username = user.username or ""
    first_name = user.first_name or ""

    if existing:
        db_execute(
            """
            UPDATE users SET username=?,first_name=?,last_seen=?,blocked=0
            WHERE user_id=?
            """,
            (username, first_name, current, user.id),
        )
    else:
        db_execute(
            """
            INSERT INTO users
                (user_id,username,first_name,first_seen,last_seen)
            VALUES (?,?,?,?,?)
            """,
            (user.id, username, first_name, current, current),
        )


def is_admin(user_id: int) -> bool:
    return int(user_id) == ADMIN_ID


def is_whitelisted(user_id: int) -> bool:
    return db_one(
        "SELECT 1 FROM whitelist WHERE user_id=?",
        (user_id,),
    ) is not None


def esc(value) -> str:
    return html.escape(str("" if value is None else value), quote=True)


def style_button(text: str, callback_data=None, url=None, style=None):
    # Если третий аргумент — цвет кнопки, это не URL.
    if style is None and url in {"primary", "success", "danger"}:
        style = url
        url = None

    kwargs = {"text": text}

    if callback_data is not None:
        kwargs["callback_data"] = callback_data

    if url is not None:
        kwargs["url"] = url

    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style

    return InlineKeyboardButton(**kwargs)

def make_kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button(*button) for button in row]
            for row in rows
        ]
    )


def back_kb(callback_data="menu"):
    return make_kb([[["⬅️ Назад", callback_data]]])


def main_kb(user_id: int):
    rows = [
        [["🛒 Купить VPN", "buy", "success"]],
        [["🔐 Моя подписка", "subscription", "primary"]],
        [["🏆 Рейтинг покупателей", "ranking"]],
        [["📖 Как подключиться", "instruction"]],
        [["💬 Поддержка", "support"]],
    ]
    if is_admin(user_id):
        rows.append([["⚙️ Админ-панель", "admin", "primary"]])
    return make_kb(rows)


def tariffs_kb():
    rows = []
    for tariff in get_tariffs(True):
        rows.append([[
            f"{tariff['name']} — {tariff['price']} ₽",
            f"tariff:{tariff['tariff_id']}",
            "success",
        ]])
    rows.append([["⬅️ Назад", "menu"]])
    return make_kb(rows)


def payment_kb():
    return make_kb([
        [["🎟 Ввести промокод", "promo:enter", "primary"]],
        [["✅ Я оплатил", "paid", "success"]],
        [["⬅️ К тарифам", "buy"]],
    ])


def happ_kb():
    return make_kb([
        [["🍎 HAPP для iPhone", None, HAPP_IOS, "primary"]],
        [["🤖 HAPP для Android", None, HAPP_ANDROID, "success"]],
        [["📖 Подробная инструкция", "instruction"]],
    ])


def admin_kb():
    return make_kb([
        [["📥 Новые заявки", "admin:orders", "success"]],
        [["📦 Все заказы", "admin:history"]],
        [["👥 Пользователи", "admin:users", "primary"]],
        [["📊 Статистика", "admin:stats", "primary"]],
        [["💰 Тарифы и цены", "admin:tariffs", "success"]],
        [["📝 Тексты", "admin:texts"]],
        [["🤍 Белый список", "admin:whitelist"]],
        [["🎟 Промокоды", "admin:promos", "success"]],
        [["📢 Обязательная подписка", "admin:channel"]],
        [["🏆 Рейтинг покупателей", "ranking"]],
        [["📣 Рассылка", "admin:broadcast", "danger"]],
        [["⚙️ Настройки", "admin:settings"]],
        [["⬅️ В магазин", "menu"]],
    ])


def order_admin_kb(order_id: int):
    return make_kb([
        [["🔑 Выдать ключ", f"give:{order_id}", "success"]],
        [["❌ Отклонить", f"reject:{order_id}", "danger"]],
    ])


def text_admin_kb():
    sections = [
        ("welcome", "🏠 Главное сообщение"),
        ("payment", "💳 Реквизиты / оплата"),
        ("instruction", "📖 Инструкция HAPP"),
        ("delivery", "🎁 Сообщение после выдачи"),
        ("support", "💬 Поддержка"),
        ("whitelist_info", "🤍 Описание белого списка"),
    ]
    rows = [[[title, f"edittext:{key}"]] for key, title in sections]
    rows.append([["⬅️ Назад", "admin"]])
    return make_kb(rows)


def format_template(template: str, replacements: dict) -> str:
    result = template or ""
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", esc(value))
    return result


def format_payment(template: str, tariff: str, price: int, days: int) -> str:
    return format_template(template, {
        "tariff": tariff,
        "price": price,
        "days": days,
    })


def format_delivery(
    template: str,
    tariff: str,
    days: int,
    key: str,
    expires: str,
    order_id: int,
) -> str:
    return format_template(template, {
        "tariff": tariff,
        "days": days,
        "key": key,
        "expires": expires,
        "order_id": order_id,
    })


# ============================================================
# FSM
# ============================================================

class UserStates(StatesGroup):
    waiting_receipt = State()
    waiting_promo = State()


class AdminStates(StatesGroup):
    waiting_key = State()
    waiting_edit_text = State()
    waiting_price = State()
    waiting_name = State()
    waiting_days = State()
    waiting_description = State()
    waiting_add_tariff = State()
    waiting_whitelist_user = State()
    waiting_broadcast = State()
    waiting_add_promo = State()
    waiting_channel = State()


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None):
    if callback.message is None:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.message.answer(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )


async def admin_only(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return False
    return True


async def admin_message_only(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return False
    return True


def status_value(status) -> str:
    return str(getattr(status, "value", status))


async def user_has_channel(user_id: int) -> bool:
    channel = setting("required_channel")
    if not channel or is_admin(user_id) or is_whitelisted(user_id):
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        status = status_value(member.status)
        return status in {"creator", "administrator", "member"} or (
            status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except Exception as exc:
        logger.warning("Не удалось проверить подписку на %s: %s", channel, exc)
        return False


def subscription_keyboard():
    rows = []
    url = setting("required_channel_url")
    if url:
        rows.append([
            style_button("📢 Подписаться на канал", url=url, style="primary")
        ])

    # Отдельный игровой бот: ссылка показывается всегда на экране ОП,
    # но не проверяется и не заменяет обязательную подписку.
    rows.append([
        style_button(
            "🎮 Запустить @GameBRxocsShop_Bot",
            url="https://t.me/GameBRxocsShop_Bot",
            style="success",
        )
    ])
    rows.append([
        style_button(
            "✅ Проверить подписку",
            callback_data="check_sub",
            style="success",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SubscribeMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)

        touch_user(user)

        if (
            is_admin(user.id)
            or is_whitelisted(user.id)
            or not setting("required_channel")
        ):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        if await user_has_channel(user.id):
            return await handler(event, data)

        notice = (
            "🔒 <b>Обязательная подписка</b>\n\n"
            "Чтобы пользоваться ботом, подпишитесь на наш канал.\n"
            "После подписки нажмите «✅ Проверить подписку».\n\n"
            "🎮 Дополнительно можно запустить @GameBRxocsShop_Bot. "
            "Это отдельный бот, его запуск не заменяет подписку на канал."
        )

        if isinstance(event, CallbackQuery):
            await event.answer()
            if event.message:
                await event.message.answer(
                    notice,
                    parse_mode=ParseMode.HTML,
                    reply_markup=subscription_keyboard(),
                )
        else:
            await event.answer(
                notice,
                parse_mode=ParseMode.HTML,
                reply_markup=subscription_keyboard(),
            )
        return None


dp.message.outer_middleware(SubscribeMiddleware())
dp.callback_query.outer_middleware(SubscribeMiddleware())


# ============================================================
# МЕНЮ И ПОКУПКА
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)
    await message.answer(
        setting("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(message.from_user.id),
        disable_web_page_preview=True,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Текущее действие отменено.",
        reply_markup=main_kb(message.from_user.id),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return

    await state.clear()
    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]
    await message.answer(
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"📥 Активных заявок: <b>{pending}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    touch_user(callback.from_user)
    await safe_edit(
        callback,
        setting("welcome"),
        main_kb(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "buy")
async def cb_buy(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        callback,
        "🛒 <b>Выберите тариф</b>\n\nНажмите на нужный вариант ниже.",
        tariffs_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("tariff:"))
async def cb_tariff(callback: CallbackQuery, state: FSMContext):
    tariff_id = callback.data.split(":", 1)[1]
    tariff = get_tariff(tariff_id)

    if not tariff or not tariff["enabled"]:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        tariff_id=tariff["tariff_id"],
        tariff_name=tariff["name"],
        base_price=int(tariff["price"]),
        days=int(tariff["days"]),
        promo_code="",
        discount=0,
    )

    description = tariff["description"] or "Без дополнительного описания."
    text = (
        f"🛒 <b>{esc(tariff['name'])}</b>\n\n"
        f"{description}\n\n"
        f"📅 Срок: <b>{int(tariff['days'])} дней</b>\n\n"
        f"{format_payment(setting('payment'), tariff['name'], int(tariff['price']), int(tariff['days']))}"
    )

    await safe_edit(callback, text, payment_kb())
    await callback.answer()


def check_promo(code: str, user_id: int, base_price: int):
    row = db_one(
        "SELECT * FROM promocodes WHERE code=?",
        (code.upper(),),
    )
    if not row or not row["enabled"]:
        return False, 0, "Промокод не найден или отключён"

    statuses = ("waiting_admin", "issuing", "completed")
    placeholders = ",".join("?" for _ in statuses)

    total = db_one(
        f"""
        SELECT COUNT(*) AS c FROM orders
        WHERE promo_code=? AND status IN ({placeholders})
        """,
        (row["code"], *statuses),
    )["c"]

    if int(total) >= int(row["max_uses"]):
        return False, 0, "Лимит использований промокода исчерпан"

    personal = db_one(
        f"""
        SELECT COUNT(*) AS c FROM orders
        WHERE promo_code=? AND user_id=? AND status IN ({placeholders})
        """,
        (row["code"], user_id, *statuses),
    )["c"]

    if int(personal) >= int(row["per_user"]):
        return False, 0, "Вы уже использовали этот промокод"

    final_price = (base_price * (100 - int(row["discount"])) + 50) // 100
    discount = max(0, base_price - final_price)
    return True, discount, ""


@dp.callback_query(F.data == "promo:enter")
async def promo_enter(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("tariff_id"):
        await callback.answer("Сначала выберите тариф.", show_alert=True)
        return

    await state.set_state(UserStates.waiting_promo)
    await callback.message.answer(
        "🎟 Отправьте промокод одним сообщением.\n"
        "Для отмены напишите /cancel."
    )
    await callback.answer()


@dp.message(UserStates.waiting_promo)
async def promo_apply(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()

    if (
        not code
        or len(code) > 32
        or not all(c.isascii() and (c.isalnum() or c in "_-") for c in code)
    ):
        await message.answer(
            "❌ Код должен содержать только латинские буквы, "
            "цифры, _ или - (до 32 символов)."
        )
        return

    data = await state.get_data()
    base = int(data.get("base_price", 0))

    if base <= 0:
        await state.clear()
        await message.answer("Покупка устарела. Начните заново: /start")
        return

    valid, discount, error = check_promo(
        code, message.from_user.id, base
    )
    if not valid:
        await message.answer(
            f"❌ {error}. Попробуйте другой промокод или /cancel."
        )
        return

    await state.update_data(promo_code=code, discount=discount)
    await state.set_state(None)

    final_price = max(0, base - discount)
    text = (
        f"✅ Промокод <code>{esc(code)}</code> применён!\n\n"
        f"📦 {esc(data['tariff_name'])}\n"
        f"💰 Цена без скидки: <s>{base} ₽</s>\n"
        f"🎁 Скидка: <b>{discount} ₽</b>\n"
        f"✅ <b>К оплате: {final_price} ₽</b>\n\n"
        f"{format_payment(setting('payment'), data['tariff_name'], final_price, int(data['days']))}"
    )

    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=payment_kb(),
    )


@dp.callback_query(F.data == "paid")
async def cb_paid(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if not data.get("tariff_id"):
        await callback.answer(
            "Сначала выберите тариф.",
            show_alert=True,
        )
        return

    if data.get("promo_code"):
        valid, discount, error = check_promo(
            data["promo_code"],
            callback.from_user.id,
            int(data["base_price"]),
        )
        if not valid:
            await callback.answer(
                f"Промокод больше недоступен: {error}.",
                show_alert=True,
            )
            return
        await state.update_data(discount=discount)

    await state.set_state(UserStates.waiting_receipt)
    await callback.message.answer(
        "🧾 <b>Отправьте чек об оплате.</b>\n\n"
        "Можно прислать фотографию или файл с чеком.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("buy"),
    )
    await callback.answer()


@dp.message(UserStates.waiting_receipt, F.photo)
async def receive_receipt_photo(message: Message, state: FSMContext):
    await create_order_from_receipt(
        message, state, "photo", message.photo[-1].file_id
    )


@dp.message(UserStates.waiting_receipt, F.document)
async def receive_receipt_document(message: Message, state: FSMContext):
    await create_order_from_receipt(
        message, state, "document", message.document.file_id
    )


async def create_order_from_receipt(
    message: Message,
    state: FSMContext,
    receipt_type: str,
    file_id: str,
):
    touch_user(message.from_user)
    data = await state.get_data()

    if not data.get("tariff_id") or "base_price" not in data:
        await state.clear()
        await message.answer(
            "❌ Сессия покупки истекла. Начните покупку заново: /start"
        )
        return

    code = (data.get("promo_code") or "").upper()
    base_price = int(data["base_price"])
    discount = int(data.get("discount", 0))

    if code:
        valid, discount, error = check_promo(
            code, message.from_user.id, base_price
        )
        if not valid:
            await message.answer(
                f"❌ Промокод больше недоступен: {error}. "
                "Начните покупку заново."
            )
            await state.clear()
            return

    final_price = max(0, base_price - discount)

    cursor = db.execute(
        """
        INSERT INTO orders(
            user_id,username,tariff,price,days,status,receipt_file_id,
            created_at,receipt_type,promo_code,discount
        ) VALUES(?,?,?,?,?,'waiting_admin',?,?,?,?,?)
        """,
        (
            message.from_user.id,
            message.from_user.username or "",
            data["tariff_name"],
            final_price,
            int(data["days"]),
            file_id,
            iso(now_utc()),
            receipt_type,
            code or None,
            discount,
        ),
    )
    db.commit()
    order_id = cursor.lastrowid
    await state.clear()

    await message.answer(
        f"✅ <b>Заявка #{order_id} создана!</b>\n\n"
        "Чек передан администратору. После проверки оплаты вам выдадут VPN-ключ.",
        parse_mode=ParseMode.HTML,
    )

    username = (
        f"@{esc(message.from_user.username)}"
        if message.from_user.username else "без username"
    )
    admin_caption = (
        f"🆕 <b>НОВАЯ ЗАЯВКА #{order_id}</b>\n\n"
        f"👤 {esc(message.from_user.full_name)}\n"
        f"🔗 {username}\n"
        f"🆔 <code>{message.from_user.id}</code>\n\n"
        f"📦 {esc(data['tariff_name'])}\n"
        f"💰 <b>{final_price} ₽</b>\n"
        f"🎁 Скидка: {discount} ₽\n"
        f"🎟 Промокод: {esc(code or 'не использовался')}\n"
        f"📅 {int(data['days'])} дней\n\n"
        "Проверьте поступление денег перед выдачей ключа."
    )

    try:
        if receipt_type == "photo":
            await bot.send_photo(
                ADMIN_ID,
                file_id,
                caption=admin_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_admin_kb(order_id),
            )
        else:
            await bot.send_document(
                ADMIN_ID,
                file_id,
                caption=admin_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_admin_kb(order_id),
            )
    except Exception:
        logger.exception("Не удалось переслать чек администратору")
        try:
            await bot.send_message(
                ADMIN_ID,
                admin_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_admin_kb(order_id),
            )
            if receipt_type == "photo":
                await bot.send_photo(ADMIN_ID, file_id)
            else:
                await bot.send_document(ADMIN_ID, file_id)
        except Exception:
            logger.exception(
                "Не удалось отправить уведомление о заявке %s", order_id
            )
            await message.answer(
                "⚠️ Заявка сохранена, но уведомление админу не доставлено. "
                "Напишите в поддержку."
            )


@dp.message(UserStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer(
        "❌ Отправьте фото или файл с чеком.",
        reply_markup=back_kb("buy"),
    )


# ============================================================
# МОЯ ПОДПИСКА / HAPP / ПОДДЕРЖКА
# ============================================================

@dp.callback_query(F.data == "subscription")
async def cb_subscription(callback: CallbackQuery):
    row = db_one(
        """
        SELECT * FROM orders
        WHERE user_id=? AND status='completed'
        ORDER BY COALESCE(expires_at,issued_at,created_at) DESC,id DESC
        LIMIT 1
        """,
        (callback.from_user.id,),
    )

    if not row:
        await safe_edit(
            callback,
            "🔐 <b>Моя подписка</b>\n\n"
            "Пока у вас нет выданных VPN-ключей.",
            make_kb([
                [["🛒 Купить VPN", "buy", "success"]],
                [["⬅️ Назад", "menu"]],
            ]),
        )
        await callback.answer()
        return

    expires = parse_dt(row["expires_at"])
    active = bool(expires and expires > now_utc())
    text = (
        "🔐 <b>Моя подписка</b>\n\n"
        f"Статус: <b>{'🟢 Активна' if active else '🔴 Срок истёк'}</b>\n"
        f"📦 Тариф: <b>{esc(row['tariff'])}</b>\n"
        f"📅 До: <b>{local_date(row['expires_at'])}</b>\n"
        f"🧾 Заказ: <b>#{row['id']}</b>\n\n"
        f"🔑 Ключ:\n<code>{esc(row['vpn_key'] or '—')}</code>"
    )

    await safe_edit(
        callback,
        text,
        make_kb([
            [["📱 HAPP для iPhone", None, HAPP_IOS, "primary"]],
            [["🤖 HAPP для Android", None, HAPP_ANDROID, "success"]],
            [["📖 Инструкция", "instruction"]],
            [["🛒 Купить ещё", "buy", "success"]],
            [["⬅️ Назад", "menu"]],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "instruction")
async def cb_instruction(callback: CallbackQuery):
    text = (
        f"{setting('instruction')}\n\n"
        "📌 <b>Если ключ не добавляется</b>\n"
        "• Скопируйте ключ полностью, без лишних символов.\n"
        "• Убедитесь, что поставщик выдал ключ, совместимый с HAPP.\n"
        "• Обновите HAPP до актуальной версии.\n"
        "• Если не помогло, отправьте скриншот администратору."
    )
    await safe_edit(callback, text, happ_kb())
    await callback.answer()


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await safe_edit(
        callback,
        setting("support"),
        back_kb("menu"),
    )
    await callback.answer()


# ============================================================
# АДМИНКА / СТАТИСТИКА / ПОЛЬЗОВАТЕЛИ / ЗАКАЗЫ
# ============================================================

@dp.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]

    await safe_edit(
        callback,
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"📥 Заявок в работе: <b>{pending}</b>",
        admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    users = db_one("SELECT COUNT(*) AS c FROM users")["c"]
    orders = db_one("SELECT COUNT(*) AS c FROM orders")["c"]
    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='waiting_admin'"
    )["c"]
    issuing = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='issuing'"
    )["c"]
    completed = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='completed'"
    )["c"]
    rejected = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='rejected'"
    )["c"]
    revenue = db_one(
        "SELECT COALESCE(SUM(price),0) AS s FROM orders WHERE status='completed'"
    )["s"]
    active = db_one(
        """
        SELECT COUNT(DISTINCT user_id) AS c FROM orders
        WHERE status='completed' AND expires_at IS NOT NULL AND expires_at>?
        """,
        (iso(now_utc()),),
    )["c"]
    white = db_one("SELECT COUNT(*) AS c FROM whitelist")["c"]
    today = (
        now_utc().astimezone(LOCAL_TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    today_orders = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE created_at>=?",
        (iso(today),),
    )["c"]

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📦 Заказов всего: <b>{orders}</b>\n"
        f"📥 Ожидают проверки: <b>{pending}</b>\n"
        f"🔑 Выдача/повторная отправка: <b>{issuing}</b>\n"
        f"✅ Выдано: <b>{completed}</b>\n"
        f"❌ Отклонено: <b>{rejected}</b>\n"
        f"💰 Выручка по выданным заказам: <b>{revenue} ₽</b>\n"
        f"🔐 Активных по сроку в боте: <b>{active}</b>\n"
        f"🤍 В белом списке: <b>{white}</b>\n"
        f"📅 Заявок сегодня: <b>{today_orders}</b>"
    )

    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all(
        "SELECT * FROM users ORDER BY last_seen DESC LIMIT 20"
    )
    parts = ["👥 <b>Последние пользователи</b>\n"]

    for row in rows:
        name = (
            f"@{esc(row['username'])}"
            if row["username"]
            else esc(row["first_name"] or row["user_id"])
        )
        flags = (
            (" 🤍" if is_whitelisted(row["user_id"]) else "")
            + (" 🚫" if row["blocked"] else "")
        )
        parts.append(
            f"\n{name}{flags}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"✅ покупок: {row['orders_count']} · "
            f"потрачено: {row['total_spent']:.0f} ₽"
        )

    if not rows:
        parts.append("Пока нет пользователей.")

    await safe_edit(callback, "\n".join(parts), back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:orders")
async def admin_orders(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all(
        """
        SELECT * FROM orders
        WHERE status IN ('waiting_admin','issuing')
        ORDER BY id DESC LIMIT 20
        """
    )

    if not rows:
        await safe_edit(
            callback,
            "📥 <b>Новые заявки</b>\n\n✅ Заявок в работе нет.",
            back_kb("admin"),
        )
        await callback.answer()
        return

    parts = ["📥 <b>Заявки в работе</b>\n"]
    buttons = []

    for row in rows:
        username = (
            f"@{esc(row['username'])}"
            if row["username"] else str(row["user_id"])
        )
        symbol = "🔑" if row["status"] == "issuing" else "⏳"
        parts.append(
            f"\n{symbol} <b>#{row['id']}</b> · {username} · "
            f"{esc(row['tariff'])} · {row['price']} ₽"
        )
        buttons.append([[
            f"Открыть #{row['id']}",
            f"order:view:{row['id']}",
            "primary",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "admin:history")
async def admin_history(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all("SELECT * FROM orders ORDER BY id DESC LIMIT 25")

    if not rows:
        await safe_edit(
            callback,
            "📦 <b>История заказов</b>\n\nЗаказов пока нет.",
            back_kb("admin"),
        )
        await callback.answer()
        return

    icons = {
        "completed": "✅",
        "waiting_admin": "⏳",
        "issuing": "🔑",
        "rejected": "❌",
    }
    parts = ["📦 <b>Последние заказы</b>\n"]
    buttons = []

    for row in rows:
        parts.append(
            f"\n{icons.get(row['status'], '•')} <b>#{row['id']}</b> · "
            f"{esc(row['tariff'])} · {row['price']} ₽"
        )
        buttons.append([[
            f"Открыть #{row['id']}",
            f"order:view:{row['id']}",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("order:view:"))
async def admin_order_view(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер заказа.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    username = (
        f"@{esc(row['username'])}"
        if row["username"] else "без username"
    )

    text = (
        f"📦 <b>Заказ #{row['id']}</b>\n\n"
        f"👤 {username}\n🆔 <code>{row['user_id']}</code>\n"
        f"📦 {esc(row['tariff'])}\n"
        f"💰 <b>{row['price']} ₽</b>\n"
        f"🎁 Скидка: {row['discount']} ₽\n"
        f"🎟 Промокод: {esc(row['promo_code'] or '—')}\n"
        f"📅 {row['days']} дней\n"
        f"📌 Статус: <b>{esc(row['status'])}</b>\n"
        f"🕒 Создан: {local_date(row['created_at'])}\n"
        f"📆 Истекает: {local_date(row['expires_at'])}"
    )

    buttons = []
    if row["status"] == "waiting_admin":
        buttons.extend([
            [["🔑 Выдать ключ", f"give:{order_id}", "success"]],
            [["❌ Отклонить", f"reject:{order_id}", "danger"]],
        ])
    elif row["status"] == "issuing" and row["vpn_key"]:
        buttons.append([[
            "🔁 Повторно отправить сохранённый ключ",
            f"resend:{order_id}",
            "primary",
        ]])
    elif row["status"] == "issuing":
        buttons.append([[
            "🔑 Продолжить выдачу ключа",
            f"give:{order_id}",
            "success",
        ]])
        buttons.append([[
            "❌ Отменить заявку",
            f"reject:{order_id}",
            "danger",
        ]])
    elif row["status"] == "completed" and row["vpn_key"]:
        buttons.append([[
            "🔁 Отправить ключ ещё раз",
            f"resend:{order_id}",
            "primary",
        ]])

    buttons.append([[
        "⬅️ Назад",
        "admin:orders"
        if row["status"] in {"waiting_admin", "issuing"}
        else "admin:history",
    ]])

    await safe_edit(callback, text, make_kb(buttons))
    await callback.answer()


# ============================================================
# ВЫДАЧА КЛЮЧЕЙ / ОТКЛОНЕНИЕ / ПОВТОРНАЯ ОТПРАВКА
# ============================================================

def subscription_expiry_for(user_id: int, days: int) -> datetime:
    current = now_utc()
    row = db_one(
        """
        SELECT expires_at FROM orders
        WHERE user_id=? AND status='completed'
          AND expires_at IS NOT NULL AND expires_at>?
        ORDER BY expires_at DESC LIMIT 1
        """,
        (user_id, iso(current)),
    )
    previous = parse_dt(row["expires_at"]) if row else None
    base = previous if previous and previous > current else current
    return base + timedelta(days=days)


def mark_order_completed(order_id: int):
    row = db_one(
        "SELECT user_id,price,status FROM orders WHERE id=?",
        (order_id,),
    )
    if not row:
        return
    if row["status"] != "completed":
        db_execute(
            "UPDATE orders SET status='completed' WHERE id=?",
            (order_id,),
        )
        db_execute(
            """
            UPDATE users SET
                orders_count=orders_count+1,
                total_spent=total_spent+?,
                blocked=0
            WHERE user_id=?
            """,
            (int(row["price"]), row["user_id"]),
        )


@dp.callback_query(F.data.startswith("give:"))
async def admin_give(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер заказа.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if row["status"] not in {"waiting_admin", "issuing"}:
        await callback.answer("Этот заказ уже обработан.", show_alert=True)
        return

    db_execute(
        "UPDATE orders SET status='issuing' WHERE id=?",
        (order_id,),
    )
    await state.set_state(AdminStates.waiting_key)
    await state.update_data(order_id=order_id)
    await callback.message.answer(
        f"🔑 <b>Выдача ключа для заказа #{order_id}</b>\n\n"
        "Купите ключ у поставщика и отправьте его сюда одним "
        "текстовым сообщением.\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def send_order_key(order, key: str) -> bool:
    expires = subscription_expiry_for(order["user_id"], int(order["days"]))

    db_execute(
        """
        UPDATE orders
        SET status='issuing',vpn_key=?,issued_at=?,expires_at=?
        WHERE id=?
        """,
        (key, iso(now_utc()), iso(expires), order["id"]),
    )

    delivery = format_delivery(
        setting("delivery"),
        order["tariff"],
        int(order["days"]),
        key,
        local_date(iso(expires)),
        int(order["id"]),
    )

    try:
        await bot.send_message(
            order["user_id"],
            delivery,
            parse_mode=ParseMode.HTML,
            reply_markup=happ_kb(),
            disable_web_page_preview=True,
        )
    except TelegramForbiddenError:
        db_execute(
            "UPDATE users SET blocked=1 WHERE user_id=?",
            (order["user_id"],),
        )
        logger.warning(
            "User %s blocked bot; key for order %s saved for retry.",
            order["user_id"],
            order["id"],
        )
        return False
    except Exception:
        logger.exception(
            "Не удалось доставить ключ для заказа %s",
            order["id"],
        )
        return False

    mark_order_completed(int(order["id"]))
    return True


@dp.message(AdminStates.waiting_key)
async def admin_receive_key(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    key = (message.text or "").strip()
    if not key:
        await message.answer("❌ Отправьте ключ текстом. Для отмены — /cancel.")
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    row = db_one(
        "SELECT * FROM orders WHERE id=?",
        (order_id,),
    ) if order_id else None

    if not row or row["status"] not in {"waiting_admin", "issuing"}:
        await state.clear()
        await message.answer("❌ Заявка не найдена или уже обработана.")
        return

    delivered = await send_order_key(row, key)
    await state.clear()

    if delivered:
        expiry = db_one(
            "SELECT expires_at FROM orders WHERE id=?",
            (order_id,),
        )["expires_at"]
        await message.answer(
            f"✅ <b>Ключ выдан!</b>\n\n"
            f"Заказ: <b>#{order_id}</b>\n"
            f"📆 Действует до: <b>{local_date(expiry)}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"⚠️ Ключ сохранён для заказа #{order_id}, "
            "но Telegram не подтвердил доставку.\n"
            "Откройте «Все заказы» или «Новые заявки» и нажмите "
            "«Повторно отправить сохранённый ключ»."
        )


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер заказа.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if (
        not row
        or row["status"] not in {"waiting_admin", "issuing"}
        or row["vpn_key"]
    ):
        await callback.answer(
            "Можно отклонить только заявку, по которой ещё не сохранён ключ.",
            show_alert=True,
        )
        return

    db_execute(
        "UPDATE orders SET status='rejected' WHERE id=?",
        (order_id,),
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"❌ <b>Заявка #{order_id} отклонена.</b>\n\n"
            "Если вы уже оплатили, напишите в поддержку.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramForbiddenError:
        db_execute(
            "UPDATE users SET blocked=1 WHERE user_id=?",
            (row["user_id"],),
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить пользователя о заказе %s",
            order_id,
        )

    await safe_edit(
        callback,
        f"❌ Заявка #{order_id} отклонена.",
        back_kb("admin:orders"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("resend:"))
async def admin_resend(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер заказа.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if (
        not row
        or row["status"] not in {"issuing", "completed"}
        or not row["vpn_key"]
    ):
        await callback.answer("Сохранённый ключ не найден.", show_alert=True)
        return

    delivery = format_delivery(
        setting("delivery"),
        row["tariff"],
        int(row["days"]),
        row["vpn_key"],
        local_date(row["expires_at"]),
        int(row["id"]),
    )

    try:
        await bot.send_message(
            row["user_id"],
            "🔁 <b>Повторная отправка ключа</b>\n\n" + delivery,
            parse_mode=ParseMode.HTML,
            reply_markup=happ_kb(),
            disable_web_page_preview=True,
        )
        if row["status"] == "issuing":
            mark_order_completed(order_id)
        await callback.answer("✅ Ключ отправлен.")
    except Exception as exc:
        logger.exception("Повторная отправка ключа не удалась: %s", exc)
        await callback.answer(
            "❌ Не получилось отправить ключ. Попробуйте ещё раз.",
            show_alert=True,
        )


# ============================================================
# ТАРИФЫ
# ============================================================

@dp.callback_query(F.data == "admin:tariffs")
async def admin_tariffs(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = get_tariffs(False)
    buttons = []
    for row in rows:
        symbol = "🟢" if row["enabled"] else "🔴"
        buttons.append([[
            f"{symbol} {row['name']} — {row['price']} ₽",
            f"edittariff:{row['tariff_id']}",
        ]])
    buttons.append([["➕ Добавить тариф", "addtariff", "success"]])
    buttons.append([["⬅️ Назад", "admin"]])

    await safe_edit(
        callback,
        "💰 <b>Тарифы и цены</b>\n\nНажмите на тариф для редактирования.",
        make_kb(buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edittariff:"))
async def admin_edit_tariff_menu(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    row = get_tariff(tariff_id)
    if not row:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    text = (
        f"💰 <b>{esc(row['name'])}</b>\n\n"
        f"ID: <code>{esc(row['tariff_id'])}</code>\n"
        f"Цена: <b>{row['price']} ₽</b>\n"
        f"Срок: <b>{row['days']} дней</b>\n"
        f"Статус: {'🟢 включён' if row['enabled'] else '🔴 выключен'}\n\n"
        f"Описание:\n{row['description'] or '—'}"
    )

    await safe_edit(callback, text, make_kb([
        [["💰 Изменить цену", f"price:{tariff_id}", "success"]],
        [["📝 Изменить название", f"name:{tariff_id}"]],
        [["📅 Изменить срок", f"days:{tariff_id}"]],
        [["📖 Изменить описание", f"desc:{tariff_id}"]],
        [["🔄 Включить / выключить", f"toggle:{tariff_id}"]],
        [["⬅️ Назад", "admin:tariffs"]],
    ]))
    await callback.answer()


@dp.callback_query(F.data.startswith("price:"))
async def admin_price_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    if not get_tariff(tariff_id):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_price)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer(
        "💰 Отправьте новую цену в рублях (целое число от 0 и выше). "
        "Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_price)
async def admin_price_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    try:
        price = int((message.text or "").strip())
        if price < 0 or price > 100000000:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Цена должна быть целым числом от 0 до 100000000."
        )
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET price=? WHERE tariff_id=?",
        (price, data["tariff_id"]),
    )
    await state.clear()
    await message.answer(
        f"✅ Цена изменена: <b>{price} ₽</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("name:"))
async def admin_name_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    if not get_tariff(tariff_id):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_name)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer(
        "📝 Отправьте новое название тарифа. Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_name)
async def admin_name_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer(
            "❌ Название должно содержать от 1 до 80 символов."
        )
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET name=? WHERE tariff_id=?",
        (name, data["tariff_id"]),
    )
    await state.clear()
    await message.answer("✅ Название тарифа изменено.")


@dp.callback_query(F.data.startswith("days:"))
async def admin_days_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    if not get_tariff(tariff_id):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_days)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer(
        "📅 Отправьте новый срок в днях (положительное целое число). "
        "Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_days)
async def admin_days_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    try:
        days = int((message.text or "").strip())
        if days <= 0 or days > 36500:
            raise ValueError
    except ValueError:
        await message.answer("❌ Срок должен быть от 1 до 36500 дней.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET days=? WHERE tariff_id=?",
        (days, data["tariff_id"]),
    )
    await state.clear()
    await message.answer(
        f"✅ Срок изменён: <b>{days} дней</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("desc:"))
async def admin_desc_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    if not get_tariff(tariff_id):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_description)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer(
        "📖 Отправьте новое описание тарифа. Жирный, курсив, ссылки "
        "и другое форматирование сохранятся.\nДля отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_description)
async def admin_desc_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    value = message.html_text if message.text else ""
    if not value or not value.strip():
        await message.answer("❌ Отправьте текстовое описание.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET description=? WHERE tariff_id=?",
        (value, data["tariff_id"]),
    )
    await state.clear()
    await message.answer("✅ Описание сохранено вместе с форматированием.")


@dp.callback_query(F.data.startswith("toggle:"))
async def admin_toggle_tariff(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    tariff_id = callback.data.split(":", 1)[1]
    row = get_tariff(tariff_id)
    if not row:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    db_execute(
        "UPDATE tariffs SET enabled=? WHERE tariff_id=?",
        (0 if row["enabled"] else 1, tariff_id),
    )
    await admin_edit_tariff_menu(callback)


@dp.callback_query(F.data == "addtariff")
async def admin_add_tariff_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_add_tariff)
    await callback.message.answer(
        "➕ <b>Новый тариф</b>\n\n"
        "Отправьте 4 строки:\n"
        "<code>id\nназвание\nцена\nдни</code>\n\n"
        "Пример:\n"
        "<code>vpn_180\nVPN на 180 дней\n799\n180</code>\n\n"
        "Для отмены — /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_tariff)
async def admin_add_tariff_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    lines = [
        line.strip()
        for line in (message.text or "").splitlines()
        if line.strip()
    ]
    if len(lines) != 4:
        await message.answer(
            "❌ Нужно ровно 4 строки: ID, название, цена, срок."
        )
        return

    tariff_id, name, raw_price, raw_days = lines

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", tariff_id):
        await message.answer(
            "❌ ID — латинские буквы, цифры, _ или -, до 24 символов."
        )
        return

    try:
        price, days = int(raw_price), int(raw_days)
        if price < 0 or price > 100000000 or days <= 0 or days > 36500:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Цена должна быть ≥ 0, срок — от 1 до 36500 дней."
        )
        return

    if get_tariff(tariff_id):
        await message.answer("❌ Такой ID уже существует.")
        return

    sort_order = db_one(
        "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM tariffs"
    )["n"]

    db_execute(
        """
        INSERT INTO tariffs
            (tariff_id,name,price,days,description,enabled,sort_order)
        VALUES (?,?,?,?,?,1,?)
        """,
        (tariff_id, name[:80], price, days, "", sort_order),
    )
    await state.clear()
    await message.answer(
        "✅ Тариф создан. Описание и остальные параметры можно изменить в админке."
    )


# ============================================================
# РЕДАКТОР ТЕКСТОВ / ФОРМАТИРОВАНИЕ
# ============================================================

@dp.callback_query(F.data == "admin:texts")
async def admin_texts(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    await safe_edit(
        callback,
        "📝 <b>Редактор текстов</b>\n\n"
        "Выберите сообщение и отправьте новую версию. "
        "Telegram-форматирование сохраняется.",
        text_admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edittext:"))
async def admin_edittext_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    key = callback.data.split(":", 1)[1]
    titles = {
        "welcome": "главное сообщение",
        "payment": "реквизиты и оплату",
        "instruction": "инструкцию HAPP",
        "delivery": "сообщение после выдачи ключа",
        "support": "поддержку",
        "whitelist_info": "описание белого списка",
    }

    if key not in titles:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return

    hint = ""
    if key == "payment":
        hint = (
            "\n\nПеременные: <code>{price}</code>, "
            "<code>{tariff}</code>, <code>{days}</code>."
        )
    elif key == "delivery":
        hint = (
            "\n\nПеременные: <code>{tariff}</code>, "
            "<code>{days}</code>, <code>{key}</code>, "
            "<code>{expires}</code>, <code>{order_id}</code>."
        )

    await state.set_state(AdminStates.waiting_edit_text)
    await state.update_data(edit_key=key)
    await callback.message.answer(
        f"📝 Отправьте новый текст для раздела «{titles[key]}».\n"
        "Жирный, курсив, ссылки и другое форматирование Telegram сохранятся.\n"
        "Для отмены — /cancel." + hint,
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_edit_text)
async def admin_edittext_finish(
    message: Message,
    state: FSMContext,
):
    if not await admin_message_only(message):
        return

    raw = message.html_text if message.text else ""
    if not raw or not raw.strip():
        await message.answer("❌ Отправьте непустой текст.")
        return

    data = await state.get_data()
    key = data.get("edit_key")

    if key not in {
        "welcome", "payment", "instruction",
        "delivery", "support", "whitelist_info",
    }:
        await state.clear()
        await message.answer("❌ Раздел не найден, начните заново.")
        return

    set_setting(key, raw)
    await state.clear()
    await message.answer(
        "✅ Текст сохранён вместе с Telegram-форматированием."
    )


# ============================================================
# БЕЛЫЙ СПИСОК
# ============================================================

@dp.callback_query(F.data == "admin:whitelist")
async def admin_whitelist(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all(
        "SELECT * FROM whitelist ORDER BY added_at DESC LIMIT 30"
    )
    parts = [
        setting("whitelist_info"),
        f"\n\n👥 Пользователей в списке: <b>{len(rows)}</b>",
    ]
    buttons = [[["➕ Добавить пользователя", "white:add", "success"]]]

    for row in rows:
        note = f" — {esc(row['note'])}" if row["note"] else ""
        parts.append(
            f"\n🤍 <code>{row['user_id']}</code>{note}"
        )
        buttons.append([[
            f"❌ Удалить {row['user_id']}",
            f"white:del:{row['user_id']}",
            "danger",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "white:add")
async def admin_whitelist_add_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_whitelist_user)
    await callback.message.answer(
        "🤍 Отправьте числовой Telegram ID. Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_whitelist_user)
async def admin_whitelist_add_finish(
    message: Message,
    state: FSMContext,
):
    if not await admin_message_only(message):
        return

    try:
        user_id = int((message.text or "").strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужен положительный числовой Telegram ID.")
        return

    db_execute(
        """
        INSERT OR REPLACE INTO whitelist(user_id,note,added_at)
        VALUES(?,?,?)
        """,
        (user_id, "", iso(now_utc())),
    )
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> добавлен в белый список. "
        "Он сможет пропускать обязательную подписку.",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("white:del:"))
async def admin_whitelist_delete(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    db_execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))
    await admin_whitelist(callback)


# ============================================================
# ПРОМОКОДЫ
# ============================================================

@dp.callback_query(F.data == "admin:promos")
async def admin_promos(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all(
        "SELECT * FROM promocodes ORDER BY created_at DESC LIMIT 30"
    )
    parts = [
        "🎟 <b>Управление промокодами</b>",
        "Скидка указывается в процентах.\n",
    ]
    buttons = [[["➕ Создать промокод", "promo:add", "success"]]]

    for row in rows:
        used = db_one(
            """
            SELECT COUNT(*) AS c FROM orders
            WHERE promo_code=?
              AND status IN ('waiting_admin','issuing','completed')
            """,
            (row["code"],),
        )["c"]

        parts.append(
            f"\n<code>{esc(row['code'])}</code> — "
            f"скидка <b>{row['discount']}%</b>\n"
            f"Использований: {used}/{row['max_uses']} · "
            f"на человека: {row['per_user']} · "
            f"{'🟢 включён' if row['enabled'] else '🔴 выключен'}"
        )
        buttons.append([[
            f"{'⛔ Отключить' if row['enabled'] else '✅ Включить'} "
            f"{row['code']}",
            f"promo:toggle:{row['code']}",
        ]])

    if not rows:
        parts.append("Промокодов пока нет.")

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "promo:add")
async def admin_promo_add(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_add_promo)
    await callback.message.answer(
        "🎟 <b>Создание промокода</b>\n\n"
        "Отправьте четыре значения через пробел:\n"
        "<code>SALE20 20 100 1</code>\n\n"
        "1. Код\n"
        "2. Скидка в процентах (1–99)\n"
        "3. Максимум использований\n"
        "4. Максимум на одного пользователя\n\n"
        "Для отмены — /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_promo)
async def admin_promo_save(
    message: Message,
    state: FSMContext,
):
    if not await admin_message_only(message):
        return

    values = (message.text or "").strip().upper().split()

    try:
        if len(values) != 4:
            raise ValueError

        code, raw_discount, raw_limit, raw_each = values
        discount = int(raw_discount)
        limit = int(raw_limit)
        per_user = int(raw_each)

        if not re.fullmatch(r"[A-Z0-9_-]{1,32}", code):
            raise ValueError
        if not (1 <= discount <= 99 and 1 <= per_user <= limit <= 1000000):
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Пример: "
            "<code>SALE20 20 100 1</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if db_one("SELECT 1 FROM promocodes WHERE code=?", (code,)):
        await message.answer(
            "❌ Такой промокод уже существует. "
            "Отключите старый или выберите новое имя."
        )
        return

    db_execute(
        """
        INSERT INTO promocodes
            (code,discount,max_uses,per_user,enabled,created_at)
        VALUES (?,?,?,?,1,?)
        """,
        (code, discount, limit, per_user, iso(now_utc())),
    )

    await state.clear()
    await message.answer(
        f"✅ Промокод <code>{esc(code)}</code> создан.\n"
        f"Скидка: {discount}% · лимит: {limit} · "
        f"на пользователя: {per_user}.",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("promo:toggle:"))
async def admin_promo_toggle(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    code = callback.data.split(":", 2)[2]
    row = db_one(
        "SELECT enabled FROM promocodes WHERE code=?",
        (code,),
    )
    if not row:
        await callback.answer("Промокод не найден.", show_alert=True)
        return

    db_execute(
        "UPDATE promocodes SET enabled=? WHERE code=?",
        (0 if row["enabled"] else 1, code),
    )
    await admin_promos(callback)


# ============================================================
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ============================================================

@dp.callback_query(F.data == "admin:channel")
async def admin_channel(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    channel = setting("required_channel")
    url = setting("required_channel_url")
    enabled = bool(channel)

    text = (
        "📢 <b>Обязательная подписка</b>\n\n"
        f"Состояние: <b>{'🟢 включена' if enabled else '🔴 выключена'}</b>\n"
        f"Канал: <code>{esc(channel or 'не задан')}</code>\n"
        f"Ссылка: {esc(url or 'не задана')}\n\n"
        "Добавьте бота администратором канала, чтобы он мог проверять подписчиков.\n"
        "Пользователи из 🤍 белого списка проходят без проверки подписки."
    )

    await safe_edit(callback, text, make_kb([
        [["✏️ Установить канал", "channel:set", "primary"]],
        [["🔴 Отключить подписку", "channel:off", "danger"]],
        [["⬅️ Назад", "admin"]],
    ]))
    await callback.answer()


@dp.callback_query(F.data == "channel:set")
async def channel_set(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_channel)
    await callback.message.answer(
        "📢 Отправьте <b>2 строки</b>:\n"
        "<code>@channelname</code>\n"
        "<code>https://t.me/channelname</code>\n\n"
        "Для приватного канала первая строка — ID вида -100..., "
        "вторая — ссылка-приглашение.\n"
        "Перед настройкой добавьте бота администратором канала. "
        "Для отмены — /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_channel)
async def channel_save(
    message: Message,
    state: FSMContext,
):
    if not await admin_message_only(message):
        return

    parts = [
        part.strip()
        for part in (message.text or "").splitlines()
        if part.strip()
    ]
    if len(parts) != 2:
        await message.answer(
            "❌ Нужно ровно 2 строки: канал/ID и ссылка."
        )
        return

    channel, url = parts
    valid_channel = (
        (channel.startswith("@") and len(channel) > 1)
        or bool(re.fullmatch(r"-100\d+", channel))
    )
    valid_url = (
        url.startswith("https://t.me/")
        or url.startswith("https://telegram.me/")
    )

    if not valid_channel or not valid_url:
        await message.answer("❌ Канал или ссылка указаны неверно.")
        return

    try:
        chat = await bot.get_chat(channel)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if status_value(member.status) not in {"administrator", "creator"}:
            await message.answer(
                "❌ Сделайте бота администратором канала и попробуйте ещё раз."
            )
            return
    except Exception as exc:
        logger.warning("Проверка канала не прошла: %s", exc)
        await message.answer(
            "❌ Не удалось проверить канал. Проверьте ID/username и права бота."
        )
        return

    saved_channel = str(chat.id) if channel.startswith("-100") else channel
    set_setting("required_channel", saved_channel)
    set_setting("required_channel_url", url)
    await state.clear()
    await message.answer("✅ Обязательная подписка включена.")


@dp.callback_query(F.data == "channel:off")
async def channel_off(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    set_setting("required_channel", "")
    set_setting("required_channel_url", "")
    await admin_channel(callback)


@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: CallbackQuery):
    if await user_has_channel(callback.from_user.id):
        await safe_edit(
            callback,
            "✅ Подписка подтверждена!",
            main_kb(callback.from_user.id),
        )
        await callback.answer("Подписка подтверждена.")
    else:
        await callback.answer(
            "❌ Подписка пока не найдена. Подпишитесь и проверьте снова.",
            show_alert=True,
        )


# ============================================================
# РАССЫЛКА С ФОРМАТИРОВАНИЕМ И МЕДИА
# ============================================================

@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.answer(
        "📣 <b>Рассылка</b>\n\n"
        "Отправьте одним сообщением текст, фото, видео или файл для рассылки.\n"
        "Форматирование и подпись сохранятся. Сначала бот покажет предпросмотр.\n"
        "Для отмены — /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast)
async def admin_broadcast_receive(
    message: Message,
    state: FSMContext,
):
    if not await admin_message_only(message):
        return

    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
        return

    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id,
    )

    preview_kb = make_kb([
        [["🚀 Отправить всем", "broadcast:send", "danger"]],
        [["❌ Отмена", "broadcast:cancel"]],
    ])

    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=preview_kb,
        )
    except Exception as exc:
        logger.exception(
            "Не получилось показать предпросмотр рассылки: %s",
            exc,
        )
        await message.answer(
            "❌ Не удалось показать предпросмотр. "
            "Отправьте другое сообщение или /cancel."
        )
        return

    await message.answer(
        "👆 Проверьте сообщение выше. Для отправки нажмите кнопку на его копии."
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def admin_broadcast_cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.clear()
    await safe_edit(
        callback,
        "❌ Рассылка отменена.",
        back_kb("admin"),
    )
    await callback.answer()


@dp.callback_query(F.data == "broadcast:send")
async def admin_broadcast_send(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    data = await state.get_data()
    source_chat = data.get("broadcast_chat_id")
    source_message = data.get("broadcast_message_id")

    if not source_chat or not source_message:
        await state.clear()
        await callback.answer(
            "Рассылка не найдена. Начните заново.",
            show_alert=True,
        )
        return

    users = db_all("SELECT user_id FROM users WHERE blocked=0")
    await callback.message.answer(
        f"📣 Начинаю рассылку по {len(users)} пользователям…"
    )

    sent = failed = 0
    for row in users:
        try:
            await bot.copy_message(
                chat_id=row["user_id"],
                from_chat_id=source_chat,
                message_id=source_message,
            )
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.3)
            try:
                await bot.copy_message(
                    chat_id=row["user_id"],
                    from_chat_id=source_chat,
                    message_id=source_message,
                )
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            db_execute(
                "UPDATE users SET blocked=1 WHERE user_id=?",
                (row["user_id"],),
            )
            failed += 1
        except Exception:
            failed += 1

    await state.clear()
    await callback.message.answer(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"⚠️ Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ============================================================
# НАСТРОЙКИ / РЕЙТИНГ ПОКУПАТЕЛЕЙ
# ============================================================

@dp.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🤖 Название: <b>{esc(BOT_NAME)}</b>\n"
        f"🆔 Admin ID: <code>{ADMIN_ID}</code>\n"
        f"📥 Заявок в работе: <b>{pending}</b>\n"
        f"📢 Обязательная подписка: "
        f"<b>{'включена' if setting('required_channel') else 'выключена'}</b>\n\n"
        "Реквизиты, сообщения, тарифы, промокоды и белый список редактируются отдельно."
    )

    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "ranking")
async def buyer_ranking(callback: CallbackQuery):
    winners = db_all(
        """
        SELECT
            o.user_id,
            COALESCE(
                NULLIF(u.first_name,''),
                NULLIF(u.username,''),
                'Покупатель'
            ) AS name,
            COALESCE(u.username,'') AS username,
            COUNT(*) AS purchases,
            SUM(o.price) AS spent
        FROM orders o
        LEFT JOIN users u ON u.user_id=o.user_id
        WHERE o.status='completed'
        GROUP BY o.user_id
        ORDER BY purchases DESC,spent DESC
        LIMIT 10
        """
    )

    lines = [
        "🏆 <b>Топ покупателей</b>",
        "Рейтинг по количеству успешно выданных заказов.\n",
    ]

    for index, row in enumerate(winners, 1):
        name = (
            ("@" + esc(row["username"]))
            if row["username"] else esc(row["name"])
        )
        name = name[:40]
        lines.append(
            f"{index}. {name} — <b>{row['purchases']}</b> покупок"
        )

    if not winners:
        lines.append("Пока подтверждённых покупок нет.")

    await safe_edit(
        callback,
        "\n".join(lines),
        back_kb("admin" if is_admin(callback.from_user.id) else "menu"),
    )
    await callback.answer()


# ============================================================
# FALLBACK / ЗАПУСК БЕЗ main()
# ============================================================

@dp.message()
async def fallback(message: Message):
    touch_user(message.from_user)
    await message.answer(
        "Выберите действие в меню 👇",
        reply_markup=main_kb(message.from_user.id),
    )


asyncio.run(dp.start_polling(bot))