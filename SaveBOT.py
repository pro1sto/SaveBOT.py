import os
import json
import asyncio
import logging
import sqlite3
import tempfile
import shutil
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InputMediaPhoto,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)

# ==============================
# 1. НАСТРОЙКИ
# ==============================

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 994444870

# Список каналов для обязательной подписки (бот должен быть в них АДМИНИСТРАТОРОМ!)
REQUIRED_CHANNELS = [
    {"id": "@ваш_канал", "url": "https://t.me", "name": "Наш основной канал"},
    # Можно добавить ещё каналы по аналогии:
    # {"id": -1001234567890, "url": "https://t.me", "name": "Второй канал"},
]

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")

is_sending = False


# ==============================
# 2. БАЗА ДАННЫХ (с сохранением старых пользователей)
# ==============================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        # Мягко добавляем новые колонки в вашу существующую таблицу, если их ещё нет
        columns = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "joined_at" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN joined_at TEXT")
        if "downloads" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN downloads INTEGER DEFAULT 0")


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, joined_at, downloads) VALUES (?, ?, 0)",
            (user_id, datetime.now().isoformat()),
        )


def increment_downloads(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET downloads = COALESCE(downloads, 0) + 1 WHERE user_id = ?",
            (user_id,),
        )


def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        new_today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at >= ?", (yesterday,)
        ).fetchone()[0]
        total_downloads = (
            conn.execute("SELECT SUM(downloads) FROM users").fetchone()[0] or 0
        )
        return total_users, new_today, total_downloads


def get_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]


def remove_users_bulk(user_ids):
    if not user_ids:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "DELETE FROM users WHERE user_id = ?",
            [(uid,) for uid in user_ids],
        )


# ==============================
# 3. ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ==============================

async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logging.error(f"Ошибка проверки канала {channel['id']}: {e}")
            continue
    return True


def get_sub_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📢 {ch['name']}", url=ch["url"])]
        for ch in REQUIRED_CHANNELS
    ]
    buttons.append(
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==============================
# 4. ПОЛУЧЕНИЕ И СКАЧИВАНИЕ TIKTOK МЕДИА
# ==============================

def get_tiktok_media_sync(url: str):
    api_url = "https://www.tikwm.com/api/"
    post_data = urllib.parse.urlencode({"url": url, "hd": 1}).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tikwm.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("code") != 0:
            logging.error("TikWM error: %s", result)
            return None

        data = result.get("data") or {}

        if data.get("images"):
            return {"type": "images", "urls": data["images"]}

        play_url = data.get("play")
        if play_url:
            play_url = urllib.parse.urljoin("https://www.tikwm.com/", play_url)
            return {"type": "video", "url": play_url}

        logging.error("Медиа не найдено: %s", data)
        return None

    except Exception:
        logging.exception("Ошибка TikTok API")
        return None


def download_video_sync(url: str):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.tiktok.com/",
            },
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "video" not in content_type and "octet-stream" not in content_type:
                logging.warning("Неожиданный тип файла: %s", content_type)

            temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            with temp:
                shutil.copyfileobj(response, temp)

            if os.path.getsize(temp.name) == 0:
                os.remove(temp.name)
                return None

            return temp.name

    except Exception:
        logging.exception("Ошибка скачивания видео")
        return None


# ==============================
# 5. ПОЛЬЗОВАТЕЛЬСКИЕ ОБРАБОТЧИКИ
# ==============================

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)

    if not await check_subscription(message.from_user.id):
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом, подпишись на наши каналы:",
            reply_markup=get_sub_keyboard(),
        )
        return

    await message.answer(
        "📥 <b>Бот для скачивания видео из TikTok</b>\n\n"
        "Отправь ссылку на видео или фото-пост TikTok.\n"
        "Я постараюсь скачать его и отправить тебе!",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку! Теперь отправь ссылку на видео или фото-пост TikTok."
        )
    else:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)


@dp.message(F.text.regexp(r"https?://(www\.|vm\.|vt\.)?tiktok\.com/"))
async def handle_tiktok_link(message: types.Message):
    add_user(message.from_user.id)

    if not await check_subscription(message.from_user.id):
        await message.answer(
            "🔒 Для скачивания необходимо подписаться на каналы:",
            reply_markup=get_sub_keyboard(),
        )
        return

    url = message.text.strip()
    status_msg = await message.answer("⏳ Получаю данные из TikTok...")

    media = await asyncio.to_thread(get_tiktok_media_sync, url)
    if not media:
        await status_msg.edit_text("❌ Не удалось получить медиа. Проверьте ссылку или попробуйте позже.")
        return

    # Если это фото-слайдшоу
    if media["type"] == "images":
        try:
            await status_msg.edit_text("📤 Отправляю фото...")
            urls = media["urls"]
            # Telegram позволяет отправлять до 10 фото в одном альбоме
            for i in range(0, len(urls), 10):
                chunk = [InputMediaPhoto(media=u) for u in urls[i : i + 10]]
                await message.answer_media_group(media=chunk)
            increment_downloads(message.from_user.id)
            await status_msg.delete()
        except Exception as e:
            logging.error(f"Ошибка отправки фото: {e}")
            await status_msg.edit_text("❌ Не удалось отправить фотографии.")
        return

    # Если это видео
    await status_msg.edit_text("⏳ Скачиваю видео...")
    video_path = await asyncio.to_thread(download_video_sync, media["url"])

    if not video_path:
        await status_msg.edit_text("❌ Ошибка при загрузке видеофайла.")
        return

    try:
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        if file_size_mb > 49.5:
            await status_msg.edit_text(
                f"❌ Видео весит {file_size_mb:.1f} МБ. Лимит Telegram для ботов — 50 МБ."
            )
            return

        await status_msg.edit_text("📤 Отправляю видео...")
        await message.answer_video(video=FSInputFile(video_path), caption="Готово! 📥")
        increment_downloads(message.from_user.id)
        await status_msg.delete()
