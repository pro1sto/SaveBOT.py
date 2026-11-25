
import os
import json
import asyncio
import logging
import sqlite3
import tempfile
import shutil
import urllib.request
import urllib.parse

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InputMediaPhoto, FSInputFile
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest
)

# ==============================
# 1. НАСТРОЙКИ
# ==============================

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 994444870

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")

is_sending = False


# ==============================
# 2. БАЗА ДАННЫХ
# ==============================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )


def get_users_count():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]


def get_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT user_id FROM users")
        ]


def remove_users_bulk(user_ids):
    if not user_ids:
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "DELETE FROM users WHERE user_id = ?",
            [(uid,) for uid in user_ids]
        )


# ==============================
# 3. ПОЛУЧЕНИЕ TIKTOK МЕДИА
# ==============================

def get_tiktok_media_sync(url: str):
    api_url = "https://www.tikwm.com/api/"

    post_data = urllib.parse.urlencode({
        "url": url,
        "hd": 1
    }).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tikwm.com/",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("code") != 0:
            logging.error("TikWM error: %s", result)
            return None

        data = result.get("data") or {}

        # Фото-слайдшоу
        if data.get("images"):
            return {
                "type": "images",
                "urls": data["images"]
            }

        # Видео
        play_url = data.get("play")

        if play_url:
            play_url = urllib.parse.urljoin(
                "https://www.tikwm.com/",
                play_url
            )

            return {
                "type": "video",
                "url": play_url
            }

        logging.error("Медиа не найдено: %s", data)
        return None

    except Exception:
        logging.exception("Ошибка TikTok API")
        return None


# ==============================
# 4. СКАЧИВАНИЕ ВИДЕО
# ==============================

def download_video_sync(url: str):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.tiktok.com/"
            }
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if (
                "video" not in content_type
                and "octet-stream" not in content_type
            ):
                logging.warning(
                    "Неожиданный тип файла: %s",
                    content_type
                )

            temp = tempfile.NamedTemporaryFile(
                suffix=".mp4",
                delete=False
            )

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
# 5. КОМАНДА /START
# ==============================

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)

    await message.answer(
        "📥 <b>Бот для скачивания видео из TikTok</b>\n\n"
        "Отправь ссылку на видео или фото-пост TikTok.\n\n"
        "Я постараюсь скачать его и отправить тебе!",
        parse_mode="HTML"
    )


# ==============================
# 6. СТАТИСТИКА
# ==============================

@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        f"📊 Живых пользователей в базе: <b>{get_users_count()}</b>",
        parse_mode="HTML"
    )


# ==============================
# 7. ОСТАНОВКА РАССЫЛКИ
# ==============================

@dp.message(Command("stop_send"))
async def stop_send_cmd(message: types.Message):
    global is_sending

    if message.from_user.id != ADMIN_ID:
        return

    is_sending = False

    await message.answer("🛑 Останавливаю рассылку...")


# ==============================
# 8. РАССЫЛКА
# ==============================

@dp.message(Command("send"))
async def send_cmd(message: types.Message):
    global is_sending

    if message.from_user.id != ADMIN_ID:
        return

    if is_sending:
        await message.answer("⚠️ Рассылка уже запущена!")
        return

    text_to_send = message.text.replace(
        "/send", "", 1
    ).strip()

    if not text_to_send:
        await message.answer(
            "❗ Напиши текст после команды.\n"
            "Пример: /send Бот снова работает!"
        )
        return

    users = get_all_users()
    total = len(users)

    status_msg = await message.answer(
        f"🚀 Запускаю рассылку по {total} пользователям..."
    )

    is_sending = True

    success = 0
    removed = 0
    dead_batch = []

    try:
        for i, uid in enumerate(users, start=1):

            if not is_sending:
                break

            try:
                await bot.send_message(
                    chat_id=uid,
                    text=text_to_send
                )
                success += 1

            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)

                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=text_to_send
                    )
                    success += 1
                except Exception:
                    dead_batch.append(uid)
                    removed += 1

            except (
                TelegramForbiddenError,
                TelegramBadRequest
            ):
                dead_batch.append(uid)
                removed += 1

            except Exception:
                logging.exception(
                    "Ошибка рассылки пользователю %s", uid
                )

            await asyncio.sleep(0.05)

            if i % 100 == 0:
                remove_users_bulk(dead_batch)
                dead_batch.clear()

                try:
                    await status_msg.edit_text(
                        f"⏳ Рассылка: {i}/{total}\n"
                        f"📨 Доставлено: {success}\n"
                        f"🗑 Удалено: {removed}"
                    )
                except TelegramBadRequest:
                    pass

    finally:
        remove_users_bulk(dead_batch)
        is_sending = False

    await message.answer(
        "✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 Доставлено: {success}\n"
        f"🗑 Удалено: {removed}\n"
        f"📊 Пользователей в базе: {get_users_count()}",
        parse_mode="HTML"
    )


# ==============================
# 9. ОБРАБОТКА ССЫЛОК TIKTOK
# ==============================

@dp.message(F.text)
async def handle_link(message: types.Message):
    add_user(message.from_user.id)

    url = message.text.strip()

    parsed = urllib.parse.urlparse(url)

    allowed_domains = {
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com"
    }

    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in allowed_domains
    ):
        await message.answer(
            "⚠️ Пожалуйста, отправь ссылку на TikTok."
        )
        return

    status_msg = await message.answer(
        "⚡ Получаю данные TikTok..."
    )

    tt_data = await asyncio.to_thread(
        get_tiktok_media_sync,
        url
    )

    if not tt_data:
        await status_msg.edit_text(
            "❌ Не удалось получить медиа.\n\n"
            "Возможно, ролик удалён, закрыт "
            "или сервис загрузки временно недоступен."
        )
        return

    temp_path = None

    try:
        if tt_data["type"] == "images":

            photos = tt_data["urls"]

            if not photos:
                raise ValueError("Список фотографий пуст")

            for idx in range(0, len(photos), 10):
                media_group = [
                    InputMediaPhoto(media=p)
                    for p in photos[idx:idx + 10]
                ]

                await message.answer_media_group(
                    media=media_group
                )

        elif tt_data["type"] == "video":

            await status_msg.edit_text(
                "📥 Скачиваю видео..."
            )

            temp_path = await asyncio.to_thread(
                download_video_sync,
                tt_data["url"]
            )

            if not temp_path:
                await status_msg.edit_text(
                    "❌ Не удалось скачать видео."
                )
                return

            await status_msg.edit_text(
                "📤 Отправляю видео в Telegram..."
            )

            video = FSInputFile(
                temp_path,
                filename="tiktok_video.mp4"
            )

            await message.answer_video(
                video=video,
                supports_streaming=True,
                caption="✅ Видео скачано!"
            )

        await status_msg.delete()

    except Exception:
        logging.exception("Ошибка отправки медиа")

        try:
            await status_msg.edit_text(
                "❌ Ошибка отправки файла.\n"
                "Возможно, видео слишком большое "
                "или Telegram не поддерживает его формат."
            )
        except Exception:
            pass

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# ==============================
# 10. ЗАПУСК БОТА
# ==============================

async def main():
    init_db()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logging.info("TikTok Downloader запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
