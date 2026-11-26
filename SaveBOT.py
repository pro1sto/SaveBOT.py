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

# 1. НАСТРОЙКИ
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 994444870

REQUIRED_CHANNELS = [
    {
        "id": -1006003972835,
        "url": "https://t.me/+afPTm145m0JhNjcy",
        "name": "ПОДПИСАТЬСЯ",
    },
]

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "downloader_users.db")
is_sending = False

# 2. БАЗА ДАННЫХ
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "joined_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN joined_at TEXT")
        if "downloads" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN downloads INTEGER DEFAULT 0")

def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, joined_at, downloads) VALUES (?, ?, 0)",
            (user_id, datetime.now().isoformat()),
        )

def increment_downloads(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET downloads = COALESCE(downloads, 0) + 1 WHERE user_id = ?", (user_id,))

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        new_24 = conn.execute("SELECT COUNT(*) FROM users WHERE joined_at >= ?", (yesterday,)).fetchone()[0]
        dl = conn.execute("SELECT SUM(downloads) FROM users").fetchone()[0] or 0
        return total, new_24, dl

def get_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]

def remove_users_bulk(user_ids):
    if not user_ids:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("DELETE FROM users WHERE user_id = ?", [(uid,) for uid in user_ids])

# 3. ПРОВЕРКА ПОДПИСКИ
async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logging.error(f"Ошибка канала {ch['id']}: {e}")
    return True

def get_sub_keyboard() -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(text=f"📢 {c['name']}", url=c["url"])] for c in REQUIRED_CHANNELS]
    btns.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

# 4. TIKTOK API + РЕЗЕРВНЫЙ YT-DLP
def resolve_url(url: str) -> str:
    """Разворачивает короткие ссылки ://tiktok.com / ://tiktok.com в полные."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl()
    except Exception:
        return url

def get_tiktok_media_sync(url: str):
    full_url = resolve_url(url)
    post_data = urllib.parse.urlencode({"url": full_url, "hd": 1}).encode("utf-8")
    req = urllib.request.Request(
        "https://tikwm.com",
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://tikwm.com",
            "Referer": "https://tikwm.com/",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") != 0:
            logging.error(f"TikWM вернул ошибку: {res}")
            return None
        data = res.get("data") or {}
        if data.get("images"):
            return {"type": "images", "urls": data["images"]}
        if data.get("play"):
            return {"type": "video", "url": urllib.parse.urljoin("https://tikwm.com/", data["play"])}
        return None
    except Exception as e:
        logging.error(f"Сбой запроса к TikWM: {e}")
        return None

def download_video_sync(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tiktok.com"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            with temp:
                shutil.copyfileobj(resp, temp)
            if os.path.getsize(temp.name) == 0:
                os.remove(temp.name)
                return None
            return temp.name
    except Exception as e:
        logging.error(f"Ошибка загрузки файла: {e}")
        return None

def download_with_ytdlp_sync(url: str):
    """Резервное скачивание через yt-dlp, если tikwm не сработал."""
    try:
        import yt_dlp
        temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        temp_path = temp.name
        temp.close()
        os.remove(temp_path)

        ydl_opts = {
            "outtmpl": temp_path,
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            return temp_path
        return None
    except Exception as e:
        logging.error(f"Ошибка резервного yt-dlp: {e}")
        return None

def download_video_sync(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tiktok.com"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            with temp:
                shutil.copyfileobj(resp, temp)
            if os.path.getsize(temp.name) == 0:
                os.remove(temp.name)
                return None
            return temp.name
    except Exception:
        return None

# 5. ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЕЙ
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    add_user(message.from_user.id)
    if not await check_subscription(message.from_user.id):
        await message.answer("👋 Подпишись на канал, чтобы пользоваться ботом:", reply_markup=get_sub_keyboard())
        return
    await message.answer("📥 Отправь ссылку на видео или фото-пост TikTok!")

@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text("✅ Спасибо за подписку! Отправь ссылку на TikTok.")
    else:
        await callback.answer("❌ Вы ещё не подписались!", show_alert=True)

@dp.message(F.text.regexp(r"https?://(www\.|vm\.|vt\.)?tiktok\.com/"))
async def handle_tiktok(message: types.Message):
    add_user(message.from_user.id)
    if not await check_subscription(message.from_user.id):
        await message.answer("🔒 Для скачивания подпишись на канал:", reply_markup=get_sub_keyboard())
        return

    status = await message.answer("⏳ Получаю данные...")
    media = await asyncio.to_thread(get_tiktok_media_sync, message.text.strip())
    if not media:
        await status.edit_text("❌ Не удалось получить медиа.")
        return

    if media["type"] == "images":
        try:
            await status.edit_text("📤 Отправляю фото...")
            urls = media["urls"]
            for i in range(0, len(urls), 10):
                await message.answer_media_group(media=[InputMediaPhoto(media=u) for u in urls[i:i+10]])
            increment_downloads(message.from_user.id)
            await status.delete()
        except Exception:
            await status.edit_text("❌ Ошибка отправки фото.")
        return

    await status.edit_text("⏳ Скачиваю видео...")
    path = await asyncio.to_thread(download_video_sync, media["url"])
    if not path:
        await status.edit_text("❌ Ошибка загрузки файла.")
        return

    try:
        if os.path.getsize(path) / (1024 * 1024) > 49.5:
            await status.edit_text("❌ Видео больше 50 МБ (лимит Telegram).")
            return
        await status.edit_text("📤 Отправляю видео...")
        await message.answer_video(video=FSInputFile(path), caption="Готово! 📥")
        increment_downloads(message.from_user.id)
        await status.delete()
    except Exception:
        await status.edit_text("❌ Не удалось отправить видео.")
    finally:
        if os.path.exists(path):
            os.remove(path)

# 6. АДМИН-ПАНЕЛЬ
class BroadcastState(StatesGroup):
    waiting_for_post = State()

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="✉️ Рассылка", callback_data="admin_bc")],
        [InlineKeyboardButton(text="🛑 Стоп рассылка", callback_data="admin_stop")],
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    total, new_24, dl = get_stats()
    await callback.message.edit_text(
        f"📊 <b>Статистика:</b>\n\n👥 Живых в базе: <b>{total}</b>\n🆕 За 24 часа: <b>{new_24}</b>\n📥 Скачиваний: <b>{dl}</b>",
        reply_markup=admin_kb(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_bc")
async def cb_bc(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID or is_sending:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]])
    await callback.message.edit_text("✉️ Отправьте пост для рассылки (текст, фото, видео):", reply_markup=kb)
    await state.set_state(BroadcastState.waiting_for_post)

@dp.callback_query(F.data == "admin_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_stop")
async def cb_stop(callback: CallbackQuery):
    global is_sending
    if callback.from_user.id == ADMIN_ID:
        is_sending = False
        await callback.answer("🛑 Останавливаю...", show_alert=True)

@dp.message(BroadcastState.waiting_for_post)
async def run_bc(message: types.Message, state: FSMContext):
    global is_sending
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    users = get_all_users()
    msg = await message.answer(f"🚀 Рассылка по {len(users)} чел...")
    is_sending = True
    ok, dead, batch = 0, 0, []
    try:
        for i, uid in enumerate(users, 1):
            if not is_sending:
                break
            try:
                await message.copy_to(chat_id=uid)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await message.copy_to(chat_id=uid)
                ok += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                batch.append(uid)
                dead += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
            if i % 100 == 0:
                remove_users_bulk(batch)
                batch.clear()
    finally:
        remove_users_bulk(batch)
        is_sending = False
    await msg.edit_text(f"✅ Готово!\n📨 Доставлено: <b>{ok}</b>\n🗑 Удалено мёртвых: <b>{dead}</b>", parse_mode="HTML")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
