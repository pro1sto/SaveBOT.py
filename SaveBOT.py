import os
import asyncio
import logging
import sqlite3
import uuid
import subprocess
import yt_dlp
import imageio_ffmpeg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

# --- 1. НАСТРОЙКИ БОТА ---
TOKEN = "6570301210:AAFyu3vlgCrzKPW92l-NB75UkPccsxpFkhI"
ADMIN_ID = 994444870
MAX_FILE_SIZE_MB = 49
# -------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "temp_videos")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


# --- 2. БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def get_all_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def remove_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


# --- 3. СКАЧИВАНИЕ И ЖЁСТКАЯ НОРМАЛИЗАЦИЯ ЧЕРЕЗ FFMPEG ---
def download_and_fix_video(url: str) -> dict | None:
    file_id = uuid.uuid4().hex
    raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{file_id}.mp4")
    final_path = os.path.join(DOWNLOAD_DIR, f"ready_{file_id}.mp4")

    ydl_opts = {
        "outtmpl": raw_path,
        "ffmpeg_location": FFMPEG_PATH,
        # Скачиваем лучший доступный поток (видео + звук)
        "format": "bv*+ba/b",
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

    try:
        # Этап 1: Скачиваем исходник
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            width = info.get("width")
            height = info.get("height")
            if not width or not height:
                for fmt in info.get("requested_formats", []):
                    if fmt.get("width") and fmt.get("height"):
                        width, height = fmt["width"], fmt["height"]
                        break

        if not os.path.exists(raw_path):
            return None

        # Этап 2: Принудительно прогоняем через ffmpeg:
        # -c:v libx264 -pix_fmt yuv420p -> убирает чёрный экран в Instagram
        # -r 30 -vsync cfr -> фиксирует 30 FPS и убирает дёрганье/тормоза в TikTok
        # -vf scale=trunc(iw/2)*2:trunc(ih/2)*2 -> делает размеры чётными (обязательно для H.264)
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i", raw_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            final_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(final_path):
            return {
                "path": final_path,
                "width": width,
                "height": height,
            }

    except Exception as e:
        logging.error(f"Ошибка при обработке видео {url}: {e}")
    finally:
        # Всегда удаляем сырой промежуточный файл
        if os.path.exists(raw_path):
            os.remove(raw_path)

    return None


# --- 4. КОМАНДЫ АДМИНА ---
@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    users = get_all_users()
    await message.answer(f"📊 Живых пользователей в базе: **{len(users)}**", parse_mode="Markdown")


@dp.message(Command("send"))
async def send_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text_to_send = message.text.replace("/send", "", 1).strip()
    if not text_to_send:
        await message.answer("❗ Напишите текст после команды.\nПример: `/send Привет!`", parse_mode="Markdown")
        return

    users = get_all_users()
    status_msg = await message.answer(f"🚀 Запускаю рассылку по {len(users)} пользователям...")
    success, removed = 0, 0

    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=text_to_send)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=uid, text=text_to_send)
                success += 1
            except Exception:
                pass
        except TelegramForbiddenError:
            remove_user(uid)
            removed += 1
        except Exception:
            pass

    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n📨 Доставлено: **{success}**\n🗑 Очищено: **{removed}**",
        parse_mode="Markdown"
    )


# --- 5. ОБРАБОТКА ССЫЛОК ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)
    await message.answer(
        "📥 **Бот для скачивания видео из TikTok и Instagram**\n\n"
        "Отправь ссылку на Reels или TikTok — пришлю в плавном качестве без водяных знаков!",
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def handle_link(message: types.Message):
    add_user(message.from_user.id)
    url = message.text.strip()

    if not any(domain in url.lower() for domain in ["tiktok.com", "instagram.com"]):
        await message.answer("⚠️ Отправь корректную ссылку на видео из **TikTok** или **Instagram**.", parse_mode="Markdown")
        return

    status_msg = await message.answer("⏳ Скачиваю и оптимизирую видео...")
    video_data = await asyncio.to_thread(download_and_fix_video, url)

    if not video_data:
        await status_msg.edit_text("❌ Не удалось скачать видео. Проверь, что аккаунт открытый.")
        return

    file_path = video_data["path"]

    try:
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text("❌ Видео весит больше 50 МБ — лимит Telegram для ботов.")
            return

        await status_msg.edit_text("📤 Отправляю видео...")
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
            os.remove(file_path)


# --- 6. ЗАПУСК ---
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот-загрузчик с принудительной нормализацией запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
