import os
import asyncio
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, unquote

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
DATABASE_URL = os.getenv("DATABASE_URL")

if not all([BOT_TOKEN, WEBHOOK_SECRET, RENDER_HOST, DATABASE_URL]):
    raise RuntimeError("Не заданы переменные окружения")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = None


def make_token():
    return secrets.token_urlsafe(8)


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👤 Моя ссылка",
                callback_data="personal"
            )
        ],
        [
            InlineKeyboardButton(
                text="👥 Создать общую ссылку",
                callback_data="create_group"
            )
        ],
        [
            InlineKeyboardButton(
                text="📨 Мои сообщения",
                callback_data="inbox"
            )
        ]
    ])


def connect_database():
    parts = urlsplit(DATABASE_URL)
    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": parts.path.lstrip("/"),
        "ssl": "require"
    }


async def setup_database():
    global db
    db = await asyncpg.create_pool(**connect_database())

    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT
            );

            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                kind TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS members (
                token TEXT NOT NULL REFERENCES links(token)
                    ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                PRIMARY KEY (token, user_id)
            );

            CREATE TABLE IF NOT EXISTS pending (
                user_id BIGINT PRIMARY KEY,
                token TEXT NOT NULL REFERENCES links(token)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                token TEXT NOT NULL,
                sender_id BIGINT,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def save_user(user):
    await db.execute("""
        INSERT INTO users(user_id, username)
        VALUES($1, $2)
        ON CONFLICT(user_id)
        DO UPDATE SET username = EXCLUDED.username
    """, user.id, user.username)


def link_url(token):
    return f"https://t.me/{BOT_USERNAME}?start={token}"


BOT_USERNAME = None


@dp.message(CommandStart())
async def start(message: types.Message):
    print("START COMMAND RECEIVED:", message.text)
    await save_user(message.from_user)

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        token = args[1].strip()

        # Ссылка для приглашения в команду
        if token.startswith("join_"):
            group_token = token[5:]
            link = await db.fetchrow(
                "SELECT * FROM links WHERE token=$1 AND kind='group'",
                group_token
            )

            if not link:
                await message.answer("Эта общая ссылка больше не существует.")
                return

            await db.execute("""
                INSERT INTO members(token, user_id)
                VALUES($1, $2) ON CONFLICT DO NOTHING
            """, group_token, message.from_user.id)

            await message.answer(
                "🎉 Ты присоединился к общей анонимной ссылке!\n"
                "Теперь сообщения по ней будут приходить и тебе.",
                reply_markup=main_menu()
            )
            return

        link = await db.fetchrow(
            "SELECT * FROM links WHERE token=$1", token
        )

        if link:
            if link["kind"] == "personal":
                recipients = await db.fetchval(
                    "SELECT COUNT(*) FROM members WHERE token=$1",
                    token
                )
                if link["owner_id"] == message.from_user.id:
                    await message.answer(
                        "Это твоя собственная ссылка. Отправь её друзьям!"
                    )
                    return
            else:
                recipients = await db.fetchval(
                    "SELECT COUNT(*) FROM members WHERE token=$1",
                    token
                )

            if recipients == 0:
                await message.answer("Получателей пока нет.")
                return

            await db.execute("""
                INSERT INTO pending(user_id, token)
                VALUES($1, $2)
                ON CONFLICT(user_id)
                DO UPDATE SET token=EXCLUDED.token
            """, message.from_user.id, token)

            await message.answer(
                "✉️ Напиши анонимное сообщение одним текстом.\n\n"
                "Его получат все участники этой ссылки."
            )
            return

    await message.answer(
        "👋 Добро пожаловать в AnonTogether!\n\n"
        "Создавай личные или общие анонимные ссылки "
        "и получай сообщения от друзей.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "personal")
async def personal(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    token = await db.fetchval("""
        SELECT token FROM links
        WHERE owner_id=$1 AND kind='personal'
    """, user_id)

    if not token:
        token = make_token()
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO links(token, owner_id, kind)
                    VALUES($1, $2, 'personal')
                """, token, user_id)
                await conn.execute("""
                    INSERT INTO members(token, user_id)
                    VALUES($1, $2)
                """, token, user_id)

    await callback.message.answer(
        "👤 Твоя личная анонимная ссылка:\n\n"
        f"{link_url(token)}\n\n"
        "Размести её в соцсетях и получай анонимные сообщения."
    )
    await callback.answer()


@dp.callback_query(F.data == "create_group")
async def create_group(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    token = make_token()

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO links(token, owner_id, kind)
                VALUES($1, $2, 'group')
            """, token, user_id)

            await conn.execute("""
                INSERT INTO members(token, user_id)
                VALUES($1, $2)
            """, token, user_id)

    public_link = link_url(token)
    invite_link = (
        f"https://t.me/{BOT_USERNAME}?start=join_{token}"
    )

    await callback.message.answer(
        "👥 Общая ссылка создана!\n\n"
        "📨 Ссылка для получения анонимок:\n"
        f"{public_link}\n\n"
        "🤝 Приглашение для друзей:\n"
        f"{invite_link}\n\n"
        "Каждое сообщение по общей ссылке получат все участники."
    )
    await callback.answer()


@dp.callback_query(F.data == "inbox")
async def inbox(callback: types.CallbackQuery):
    rows = await db.fetch("""
        SELECT body, created_at FROM messages m
        WHERE EXISTS (
            SELECT 1 FROM members mb
            WHERE mb.token=m.token AND mb.user_id=$1
        )
        ORDER BY id DESC LIMIT 10
    """, callback.from_user.id)

    if not rows:
        await callback.message.answer("📭 Пока сообщений нет.")
    else:
        text = "📨 Последние сообщения:\n\n"
        for row in rows:
            text += f"• {row['body']}\n\n"
        await callback.message.answer(text)

    await callback.answer()


@dp.message(F.text)
async def receive_message(message: types.Message):
    user_id = message.from_user.id
    await save_user(message.from_user)

    pending = await db.fetchrow(
        "SELECT token FROM pending WHERE user_id=$1",
        user_id
    )

    if not pending:
        await message.answer(
            "Выбери действие в меню:",
            reply_markup=main_menu()
        )
        return

    token = pending["token"]
    body = message.text.strip()

    if not body or len(body) > 3000:
        await message.answer("Сообщение должно быть от 1 до 3000 символов.")
        return

    link = await db.fetchrow(
        "SELECT * FROM links WHERE token=$1", token
    )

    if not link:
        await db.execute("DELETE FROM pending WHERE user_id=$1", user_id)
        await message.answer("Ссылка больше не существует.")
        return

    recipients = await db.fetch(
        "SELECT user_id FROM members WHERE token=$1",
        token
    )

    recipient_ids = [
        row["user_id"] for row in recipients
        if row["user_id"] != user_id
    ]

    if not recipient_ids:
        await message.answer("Пока нет других участников, которым можно отправить сообщение.")
        return

    # Сохраняем анонимное сообщение без раскрытия отправителя
    await db.execute("""
        INSERT INTO messages(token, sender_id, body)
        VALUES($1, $2, $3)
    """, token, user_id, body)

    await db.execute(
        "DELETE FROM pending WHERE user_id=$1",
        user_id
    )

    delivered = 0
    for recipient_id in recipient_ids:
        try:
            await bot.send_message(
                recipient_id,
                "📩 Новое анонимное сообщение!\n\n"
                f"{body}"
            )
            delivered += 1
        except Exception:
            pass

    await message.answer(
        f"✅ Сообщение отправлено! Получателей: {delivered}."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_USERNAME
    await setup_database()

    me = await bot.get_me()
    BOT_USERNAME = me.username

    await bot.set_webhook(
        url=f"https://{RENDER_HOST}/webhook",
        secret_token=WEBHOOK_SECRET
    )

    yield

    await bot.delete_webhook()
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return {"status": "AnonTogether is running"}


@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    data = await request.json()
    update = types.Update.model_validate(
        data, context={"bot": bot}
    )
    await dp.feed_update(bot, update)

    return JSONResponse({"ok": True})
