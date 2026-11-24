import os
import re
import asyncio
import logging
import sqlite3
import uuid
import aiohttp
import yt_dlp
import imageio_ffmpeg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

# --- 1. НАСТРОЙКИ БОТА ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 994444870
MAX_FILE_SIZE_MB = 49
# -------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "temp_videos")
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
is_sending = False


# --- 2. БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def get_users_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0


def get_all_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def remove_users_bulk(user_ids: list[int]):
    if not user_ids:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("DELETE FROM users WHERE user_id = ?", [(uid,) for uid in user_ids])


# --- 3. МОМЕНТАЛЬНЫЙ СПОСОБ ДЛЯ TIKTOK (1-2 СЕКУНДЫ) ---
async def get_tiktok_fast(url: str) -> str | None:
    api_url = "https://tikwm.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, data={"url": url}, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0 and data.get("data"):
                        return data["data"].get("play")
    except Exception as e:
        logging.error(f"Ошибка быстрого API TikTok: {e}")
    return None


# --- 4. БЫСТРЫЙ СПОСОБ ДЛЯ INSTAGRAM (СКЛЕЙКА БЕЗ ПЕРЕКОДИРОВКИ) ---
def download_fast_ytdlp(url: str) -> dict | None:
    file_id = uuid.uuid4().hex
    output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

    ydl_opts = {
        "outtmpl": output_path,
        "ffmpeg_location": FFMPEG_PATH,
        "format": "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/b[vcodec^=avc1][ext=mp4]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    if os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            width = info.get("width")
            height = info.get("height")
            if not width or not height:
                for fmt in info.get("requested_formats", []):
                    if fmt.get("width") and fmt.get("height"):
                        width, height = fmt["width"], fmt["height"]
                        break

        if os.path.exists(output_path):
            return {
                "path": output_path,
                "width": width,
                "height": height,
            }
    except Exception as e:
        logging.error(f"Ошибка yt-dlp при скачивании {url}: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)

    return None


# --- 5. КОМАНДЫ АДМИНА ---
@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(f"📊 Живых пользователей в базе: **{get_users_count()}**", parse_mode="Markdown")


@dp.message(Command("backup"))
async def backup_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    if os.path.exists(DB_PATH):
        await message.answer_document(FSInputFile(DB_PATH), caption="💾 Резервная копия базы пользователей")
    else:
        await message.answer("❌ Файл базы данных пока не найден.")


@dp.message(Command("stop_send"))
async def stop_send_cmd(message: types.Message):
    global is_sending
    if message.from_user.id != ADMIN_ID:
        return
    is_sending = False
    await message.answer("🛑 Останавливаю рассылку...")


@dp.message(Command("send"))
async def send_cmd(message: types.Message):
    global is_sending
    if message.from_user.id != ADMIN_ID:
        return

    if is_sending:
        await message.answer("⚠️ Рассылка уже идёт! Остановить: `/stop_send`", parse_mode="Markdown")
        return

    text_to_send = message.text.replace("/send", "", 1).strip()
    if not text_to_send:
        await message.answer("❗ Напишите текст после команды.\nПример: `/send Бот снова работает!`", parse_mode="Markdown")
        return

    users = get_all_users()
    total = len(users)
    status_msg = await message.answer(
        f"🚀 Запускаю рассылку по **{total}** пользователям...\n"
        f"Остановить досрочно: `/stop_send`",
        parse_mode="Markdown"
    )

    is_sending = True
    success, removed = 0, 0
    dead_batch = []

    for i, uid in enumerate(users, start=1):
        if not is_sending:
            break

        try:
            await bot.send_message(chat_id=uid, text=text_to_send)
            success += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=uid, text=text_to_send)
                success += 1
            except Exception:
                dead_batch.append(uid)
                removed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            dead_batch.append(uid)
            removed += 1
        except Exception:
            pass
        finally:
            await asyncio.sleep(0.05)

        if i % 250 == 0:
            if dead_batch:
                remove_users_bulk(dead_batch)
                dead_batch.clear()
            try:
                await status_msg.edit_text(
                    f"⏳ **Идёт рассылка:** {i} из {total}\n"
                    f"📨 Доставлено живым: **{success}**\n"
                    f"🗑 Очищено мёртвых: **{removed}**\n\n"
                    f"Остановить: `/stop_send`",
                    parse_mode="Markdown"
                )
            except TelegramBadRequest:
                pass

    if dead_batch:
        remove_users_bulk(dead_batch)

    is_sending = False
    # Отправляем финальный отчёт новым сообщением, чтобы он гарантированно пришёл даже спустя 1.5 часа
    await message.answer(
        f"✅ **Рассылка завершена!**\n\n"
        f"📨 Доставлено живым: **{success}**\n"
        f"🗑 Очищено мёртвых/заблокировавших: **{removed}**\n"
        f"📊 Осталось живых в базе: **{get_users_count()}**",
        parse_mode="Markdown"
    )


# --- 6. ОБРАБОТКА ССЫЛОК ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)
    await message.answer(
        "📥 **Бот для скачивания видео из TikTok и Instagram**\n\n"
        "Отправь ссылку на Reels или TikTok — пришлю моментально и без водяных знаков!",
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def handle_link(message: types.Message):
    add_user(message.from_user.id)
    url = message.text.strip()
    lower_url = url.lower()

    if not any(domain in lower_url for domain in ["tiktok.com", "instagram.com"]):
        await message.answer("⚠️ Отправь корректную ссылку на видео из **TikTok** или **Instagram**.", parse_mode="Markdown")
        return

    status_msg = await message.answer("⚡ Загружаю...")

    if "tiktok.com" in lower_url:
        direct_video_url = await get_tiktok_fast(url)
        if direct_video_url:
            try:
                await message.answer_video(
                    video=direct_video_url,
                    supports_streaming=True,
                    caption="✅ Скачано без водяных знаков!"
                )
                await status_msg.delete()
                return
            except Exception as e:
                logging.warning(f"Прямая отправка TikTok не прошла, переключаюсь на yt-dlp: {e}")

    video_data = await asyncio.to_thread(download_fast_ytdlp, url)

    if not video_data:
        await status_msg.edit_text("❌ Не удалось скачать видео. Проверь, что аккаунт открытый.")
        return

    file_path = video_data["path"]

    try:
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text("❌ Видео весит больше 50 МБ — лимит Telegram для ботов.")
            return

        video_file = FSInputFile(file_path)
        await message.answer_video(
            video=video_file,
            width=video_data["width"],
            height=video_data["height"],
            supports_streaming=True,
            caption="✅ Скачано без водяных знаков!"
        )
        await status_msg.delete()

    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")
        await status_msg.edit_text("❌ Ошибка при отправке видео в Telegram.")
    finally:
        if os.path.exists(file_path):
