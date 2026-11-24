import os
import re
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
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
is_sending = False


# --- 2. БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def add_users_bulk(user_ids: list[int]) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.executemany("INSERT OR IGNORE INTO users (user_id) VALUES (?)", [(uid,) for uid in user_ids])
        after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return after - before


def get_users_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_all_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def remove_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


# --- 3. СКАЧИВАНИЕ И НОРМАЛИЗАЦИЯ ЧЕРЕЗ FFMPEG ---
def download_and_fix_video(url: str) -> dict | None:
    file_id = uuid.uuid4().hex
    raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{file_id}.mp4")
    final_path = os.path.join(DOWNLOAD_DIR, f"ready_{file_id}.mp4")

    ydl_opts = {
        "outtmpl": raw_path,
        "ffmpeg_location": FFMPEG_PATH,
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
        if os.path.exists(raw_path):
            os.remove(raw_path)

    return None


# --- 4. ИМПОРТ БАЗЫ ИЗ .TXT ФАЙЛА (ТОЛЬКО ДЛЯ АДМИНА) ---
@dp.message(F.document)
async def import_txt_db(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.endswith(".txt"):
        await message.answer("⚠️ Отправьте текстовый файл с расширением `.txt`.", parse_mode="Markdown")
        return

    wait_msg = await message.answer("⏳ Скачиваю и загружаю базу в SQLite...")
    temp_txt = os.path.join(DOWNLOAD_DIR, f"import_{uuid.uuid4().hex}.txt")

    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, destination=temp_txt)

        with open(temp_txt, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        found_ids = [int(match) for match in re.findall(r"\b\d{5,15}\b", content)]

        if not found_ids:
            await wait_msg.edit_text("❌ В файле не найдено ни одного числового ID.")
            return

        added_count = add_users_bulk(found_ids)
        total_count = get_users_count()

        await wait_msg.edit_text(
            f"✅ **Импорт завершён!**\n\n"
            f"📄 Найдено ID в файле: **{len(found_ids)}**\n"
            f"➕ Добавлено новых в базу: **{added_count}**\n"
            f"📊 Всего в базе теперь: **{total_count}**",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка импорта базы: {e}")
        await wait_msg.edit_text(f"❌ Ошибка при загрузке файла: {e}")
    finally:
        if os.path.exists(temp_txt):
            os.remove(temp_txt)


# --- 5. КОМАНДЫ АДМИНА (СТАТИСТИКА, РАССЫЛКА, БЭКАП) ---
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
        await message.answer("⚠️ Рассылка уже идёт! Чтобы остановить её, напишите `/stop_send`.", parse_mode="Markdown")
        return

    text_to_send = message.text.replace("/send", "", 1).strip()
    if not text_to_send:
        await message.answer("❗ Напишите текст после команды.\nПример: `/send Бот снова работает!`", parse_mode="Markdown")
        return

    users = get_all_users()
    total = len(users)
    status_msg = await message.answer(
        f"🚀 Запускаю рассылку по **{total}** пользователям...\n"
        f"Чтобы остановить досрочно: `/stop_send`",
        parse_mode="Markdown"
    )

    is_sending = True
    success, removed = 0, 0

    for i, uid in enumerate(users, start=1):
        if not is_sending:
            break

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

        if i % 1000 == 0:
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

    is_sending = False
    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"📨 Доставлено живым: **{success}**\n"
        f"🗑 Очищено заблокировавших: **{removed}**\n"
        f"📊 Осталось живых в базе: **{get_users_count()}**",
        parse_mode="Markdown"
    )


# --- 6. ОБРАБОТКА ССЫЛОК ---
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

