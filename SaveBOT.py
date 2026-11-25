import os
import re
import json
import urllib.request
import urllib.parse
import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InputMediaPhoto, BufferedInputFile
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

# --- 1. НАСТРОЙКИ БОТА ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 994444870
# -------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")
is_sending = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


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
        for (count,) in conn.execute("SELECT COUNT(*) FROM users"):
            return count
    return 0


def get_all_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        return [uid for (uid,) in conn.execute("SELECT user_id FROM users")]


def remove_users_bulk(user_ids: list[int]):
    if not user_ids:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("DELETE FROM users WHERE user_id = ?", [(uid,) for uid in user_ids])


# --- 3. ПОЛУЧЕНИЕ МЕДИА ИЗ TIKTOK БЕЗ ВОДЯНЫХ ЗНАКОВ ---
def get_tiktok_media_sync(url: str) -> dict | None:
    api_url = "https://tikwm.com"
    post_data = urllib.parse.urlencode({"url": url, "hd": 1}).encode("utf-8")
    req = urllib.request.Request(api_url, data=post_data, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if result.get("code") == 0 and result.get("data"):
            data = result["data"]

            # Если пост состоит из фотографий (слайд-шоу)
            if data.get("images"):
                return {"type": "images", "urls": data["images"]}

            # Если это обычный видеоролик (берём HD, если доступно, иначе обычное)
            play_url = data.get("hdplay") or data.get("play")
            if play_url:
                if play_url.startswith("/"):
                    play_url = "https://www.tikwm.com" + play_url
                return {"type": "video", "url": play_url}
        else:
            logging.warning(f"API TikWM вернул ответ с ошибкой: {result}")
    except Exception as e:
        logging.error(f"Ошибка при получении данных TikTok: {e}")

    return None


def download_bytes_sync(file_url: str) -> bytes | None:
    req = urllib.request.Request(file_url, headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read()
    except Exception as e:
        logging.error(f"Ошибка при скачивании файла по прямой ссылке: {e}")
        return None


# --- 4. КОМАНДЫ АДМИНА ---
@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(f"📊 Живых пользователей в базе: **{get_users_count()}**", parse_mode="Markdown")


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
    await message.answer(
        f"✅ **Рассылка завершена!**\n\n"
        f"📨 Доставлено живым: **{success}**\n"
        f"🗑 Очищено мёртвых/заблокировавших: **{removed}**\n"
        f"📊 Осталось живых в базе: **{get_users_count()}**",
        parse_mode="Markdown"
    )


# --- 5. ОБРАБОТКА ССЫЛОК TIKTOK ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)
    await message.answer(
        "📥 **Бот для скачивания видео из TikTok**\n\n"
        "Отправь ссылку на любой ролик или фото-пост из TikTok — пришлю моментально и без водяных знаков!",
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def handle_link(message: types.Message):
    add_user(message.from_user.id)
    match = re.search(r"https?://[^\s]+tiktok\.com[^\s]*", message.text)
    if not match:
        await message.answer("⚠️ Пожалуйста, отправь корректную ссылку на **TikTok**.", parse_mode="Markdown")
        return

    url = match.group(0)
    status_msg = await message.answer("⚡ Загружаю...")
    tt_data = await asyncio.to_thread(get_tiktok_media_sync, url)

    if not tt_data:
        await status_msg.edit_text("❌ Не удалось скачать медиа. Проверь, что ролик не удалён и аккаунт открытый.")
        return

    try:
        if tt_data["type"] == "images":
            photos = tt_data["urls"]
            for idx in range(0, len(photos), 10):
                media_group = [InputMediaPhoto(media=p_url) for p_url in photos[idx:idx + 10]]
                await message.answer_media_group(media=media_group)
            await status_msg.delete()

        elif tt_data["type"] == "video":
            video_bytes = await asyncio.to_thread(download_bytes_sync, tt_data["url"])
            if not video_bytes:
                await status_msg.edit_text("❌ Не удалось загрузить видеофайл с сервера.")
                return

            video_file = BufferedInputFile(video_bytes, filename="tiktok_video.mp4")
            await message.answer_video(
                video=video_file,
                supports_streaming=True,
                caption="✅ Скачано без водяных знаков!"
            )
            await status_msg.delete()

    except Exception as e:
        logging.error(f"Ошибка при отправке медиа: {e}")
        await status_msg.edit_text("❌ Ошибка при отправке файла в Telegram (возможно, файл слишком большой).")


# --- 6. ЗАПУСК ---
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("TikTok-загрузчик успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")

