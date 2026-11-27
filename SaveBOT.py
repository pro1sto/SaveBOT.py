import os
import json
import time
import asyncio
import logging
import sqlite3
import tempfile
import shutil
import urllib.request
import urllib.parse

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto, FSInputFile, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
dp = Dispatcher(storage=MemoryStorage())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")

is_sending = False


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()


# ==============================
# 2. БАЗА ДАННЫХ
# ==============================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at INTEGER DEFAULT 0
            )
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN joined_at INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('downloads', 0)")


def add_user(user_id: int):
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)",
            (user_id, now)
        )


def increment_downloads():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE stats SET value = value + 1 WHERE key = 'downloads'")


def get_users_count():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_bot_stats():
    day_ago = int(time.time()) - 86400
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new_24h = conn.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at >= ?",
            (day_ago,)
        ).fetchone()[0]
        downloads = conn.execute(
            "SELECT value FROM stats WHERE key = 'downloads'"
        ).fetchone()[0]
    return total_users, new_24h, downloads


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
# 3. КЛАВИАТУРЫ И ТЕКСТ АДМИНКИ
# ==============================

def get_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="✉️ Сделать рассылку", callback_data="admin_broadcast")
    builder.adjust(1)
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    return builder.as_markup()


def get_stop_sending_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛑 Остановить рассылку", callback_data="admin_stop_send")
    return builder.as_markup()


def build_stats_text() -> str:
    total, new_24h, downloads = get_bot_stats()
    return (
        "📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🆕 Новых за 24 часа: <b>{new_24h}</b>\n"
        f"📥 Скачано видео: <b>{downloads}</b>"
    )


# ==============================
# 4. ПОЛУЧЕНИЕ TIKTOK МЕДИА
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
# 5. СКАЧИВАНИЕ ВИДЕО
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
# 6. КОМАНДА /START
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
# 7. АДМИН-МЕНЮ И СТАТИСТИКА
# ==============================

@dp.message(Command("admin", "stats"))
async def admin_menu_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()
    await message.answer(
        build_stats_text(),
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    try:
        await callback.message.edit_text(
            build_stats_text(),
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass

    await callback.answer("Статистика обновлена")


# ==============================
# 8. РАССЫЛКА ЧЕРЕЗ КНОПКИ И FSM
# ==============================

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    if is_sending:
        await callback.answer("⚠️ Рассылка уже запущена!", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.edit_text(
        "✉️ <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение, которое нужно разослать всем пользователям.\n"
        "<i>Поддерживается текст, фото, видео, GIF и голосовые.</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.clear()
    await callback.message.edit_text(
        build_stats_text(),
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Отменено")


@dp.callback_query(F.data == "admin_stop_send")
async def cb_admin_stop_send(callback: types.CallbackQuery):
    global is_sending

    if callback.from_user.id != ADMIN_ID:
        return

    is_sending = False
    await callback.answer("🛑 Останавливаю рассылку...", show_alert=True)


@dp.message(Command("stop_send"))
async def stop_send_cmd(message: types.Message):
    global is_sending

    if message.from_user.id != ADMIN_ID:
        return

    is_sending = False
    await message.answer("🛑 Останавливаю рассылку...")


@dp.message(AdminStates.waiting_for_broadcast, F.from_user.id == ADMIN_ID)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    global is_sending

    await state.clear()

    if is_sending:
        await message.answer("⚠️ Рассылка уже запущена!")
        return

    users = get_all_users()
    total = len(users)

    status_msg = await message.answer(
        f"🚀 Запускаю рассылку по <b>{total}</b> пользователям...",
        reply_markup=get_stop_sending_keyboard(),
        parse_mode="HTML"
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
                await message.copy_to(chat_id=uid)
                success += 1

            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await message.copy_to(chat_id=uid)
                    success += 1
                except Exception:
                    dead_batch.append(uid)
                    removed += 1

            except (TelegramForbiddenError, TelegramBadRequest):
                dead_batch.append(uid)
                removed += 1

            except Exception:
                logging.exception("Ошибка рассылки пользователю %s", uid)

            await asyncio.sleep(0.05)

            if i % 100 == 0:
                remove_users_bulk(dead_batch)
                dead_batch.clear()

                try:
                    await status_msg.edit_text(
                        f"⏳ Рассылка: <b>{i}/{total}</b>\n"
                        f"📨 Доставлено: <b>{success}</b>\n"
                        f"🗑 Удалено: <b>{removed}</b>",
                        reply_markup=get_stop_sending_keyboard(),
                        parse_mode="HTML"
                    )
                except TelegramBadRequest:
                    pass

    finally:
        remove_users_bulk(dead_batch)
        is_sending = False

    await status_msg.edit_text(
        "✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 Доставлено: <b>{success}</b>\n"
        f"🗑 Удалено: <b>{removed}</b>\n"
        f"📊 Пользователей в базе: <b>{get_users_count()}</b>",
        reply_markup=get_admin_keyboard(),
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
        await message.answer("⚠️ Пожалуйста, отправь ссылку на TikTok.")
        return

    status_msg = await message.answer("⚡ Получаю данные TikTok...")

    tt_data = await asyncio.to_thread(get_tiktok_media_sync, url)

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
                await message.answer_media_group(media=media_group)

            increment_downloads()

        elif tt_data["type"] == "video":
            await status_msg.edit_text("📥 Скачиваю видео...")

            temp_path = await asyncio.to_thread(
                download_video_sync,
                tt_data["url"]
            )

            if not temp_path:
                await status_msg.edit_text("❌ Не удалось скачать видео.")
                return

            await status_msg.edit_text("📤 Отправляю видео в Telegram...")

            video = FSInputFile(temp_path, filename="tiktok_video.mp4")

            await message.answer_video(
                video=video,
                supports_streaming=True,
                caption="✅ Видео скачано!"
            )

            increment_downloads()

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

    await bot.delete_webhook(drop_pending_updates=True)

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
