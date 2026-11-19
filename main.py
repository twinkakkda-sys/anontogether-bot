
import os
import asyncio
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")

if not BOT_TOKEN or not WEBHOOK_SECRET or not RENDER_HOST:
    raise RuntimeError("Missing environment variables")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        "👋 Добро пожаловать в AnonTogether!\n\n"
        "Здесь можно создавать личные и общие "
        "анонимные ссылки.\n\n"
        "🚀 Скоро всё заработает!"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(
        url=f"https://{RENDER_HOST}/webhook",
        secret_token=WEBHOOK_SECRET
    )
    yield
    await bot.delete_webhook()
    await bot.session.close()


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
