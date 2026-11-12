import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# KONFIGURATSIYA
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
FILE_IDS_PATH = os.path.join(CACHE_DIR, "file_ids.json")

# Diskdagi fayl keshi 6 soat saqlanadi
CACHE_TIME = 60 * 60 * 6

# Telegram file_id keshi 30 kun saqlanadi
FILE_ID_TIME = 60 * 60 * 24 * 30

# Bir foydalanuvchining ikki so'rovi orasidagi minimal vaqt (soniya)
USER_COOLDOWN = 5

# Telegram Bot API cheklovlari
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
MAX_PHOTO_SIZE = 10 * 1024 * 1024

# Oddiy rasm havolasi uchun cheklovlar
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_REDIRECTS = 3

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".avi")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
IMAGE_LINK_EXTENSIONS = PHOTO_EXTENSIONS + (".gif", ".bmp")

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("instasave")

ytdlp_log = logging.getLogger("yt_dlp")
ytdlp_log.setLevel(logging.CRITICAL)


# =========================================================
# XATO TURLARI
# =========================================================

ERROR_TEXTS = {
    "private": (
        "🔒 Bu post yopiq profilga tegishli yoki kirish talab qiladi.\n"
        "Bot faqat ochiq postlarni yuklay oladi."
    ),
    "not_found": (
        "🔍 Media topilmadi.\n"
        "Havola noto'g'ri yoki post o'chirilgan bo'lishi mumkin."
    ),
    "rate_limit": (
        "🚦 Instagram vaqtinchalik cheklov qo'ydi.\n"
        "Bir necha daqiqadan keyin qayta urinib ko'ring."
    ),
    "restricted": (
        "🚫 Instagram bu postni ko'rsatmadi.\n"
        "Post o'chirilgan, profil yopiq yoki Instagram vaqtinchalik "
        "cheklov qo'ygan bo'lishi mumkin. Havolani tekshirib, "
        "birozdan keyin qayta urinib ko'ring."
    ),
    "too_big": (
        "📦 Fayl 50 MB dan katta.\n"
        "Telegram botlarga bunday faylni yuborishga ruxsat bermaydi."
    ),
    "unknown": (
        "❌ Yuklab bo'lmadi.\n"
        "Iltimos, keyinroq qayta urinib ko'ring."
    ),
}


class DownloadFailed(Exception):
    """Yuklash amalga oshmadi; reason — ERROR_TEXTS kalitlaridan biri."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def classify_download_error(message: str) -> str:
    text = message.lower()

    # Instagram ko'pincha ikkala sababni bitta xabarda aytadi
    if "login" in text and "rate" in text:
        return "restricted"

    # O'chirilgan va yopiq postlar uchun Instagram bir xil javob beradi
    if "empty media response" in text:
        return "restricted"

    if any(word in text for word in ("login", "private", "cookies", "authentic")):
        return "private"

    if any(word in text for word in ("rate", "429", "try again later", "too many")):
        return "rate_limit"

    if any(word in text for word in ("404", "not found", "unsupported url", "unavailable", "no video")):
        return "not_found"

    return "unknown"


# =========================================================
# HAVOLALAR BILAN ISHLASH
# =========================================================

def extract_url(text: str):
    """Xabar ichidan birinchi havolani ajratib oladi."""
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,)!") if match else None


def is_instagram_link(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in INSTAGRAM_HOSTS)


def normalize_instagram_url(url: str) -> str:
    """?igsh=... kabi qo'shimchalarni olib tashlaydi.

    Shunda bitta post turli havolalar bilan kelsa ham
    keshda bitta kalit bilan saqlanadi.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()

    if host.startswith("www."):
        host = host[4:]

    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def is_image_link(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()

    return (
        path.endswith(IMAGE_LINK_EXTENSIONS)
        or host == "i.imgur.com"
        or host.endswith("ibb.co")
    )


def is_public_host(url: str) -> bool:
    """Havola ichki tarmoqqa (localhost, 192.168.x.x ...) ishora qilmasligini tekshiradi."""
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False

    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        address = ipaddress.ip_address(info[4][0])

        if not address.is_global:
            return False

    return True


def get_cache_key(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


# =========================================================
# TELEGRAM FILE_ID KESHI
# =========================================================
#
# Telegram bir marta yuklangan faylni o'z serverida saqlaydi.
# file_id ni eslab qolsak, keyingi safar faylni qayta yuklamasdan
# bir zumda yuboramiz.

def load_file_ids() -> dict:
    try:
        with open(FILE_IDS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except FileNotFoundError:
        return {}

    except Exception as error:
        log.warning("file_id keshini o'qib bo'lmadi: %s", error)
        return {}


def save_file_ids() -> None:
    try:
        temp_path = FILE_IDS_PATH + ".tmp"

        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(file_ids, file)

        os.replace(temp_path, FILE_IDS_PATH)

    except Exception as error:
        log.warning("file_id keshini saqlab bo'lmadi: %s", error)


def remember_file_id(key: str, kind: str, file_id: str) -> None:
    file_ids[key] = {"kind": kind, "file_id": file_id, "time": time.time()}
    save_file_ids()


def forget_file_id(key: str) -> None:
    if file_ids.pop(key, None) is not None:
        save_file_ids()


# =========================================================
# GLOBAL HOLAT
# =========================================================

file_ids = load_file_ids()

# Foydalanuvchining oxirgi so'rov vaqti
user_last_request = {}


class KeyedLocks:
    """Har bir kalit uchun alohida lock; ishlatib bo'lingach o'chiriladi."""

    def __init__(self):
        self._entries = {}

    @asynccontextmanager
    async def hold(self, key: str):
        entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1

        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1

            if entry[1] == 0:
                self._entries.pop(key, None)


download_locks = KeyedLocks()


# =========================================================
# DISK KESHI
# =========================================================

def find_cached_file(key: str):
    """Keshdan amal qilayotgan faylni qaytaradi, bo'lmasa None."""
    try:
        for filename in os.listdir(CACHE_DIR):

            if not filename.startswith(key + ".") or filename.endswith(".part"):
                continue

            filepath = os.path.join(CACHE_DIR, filename)

            if not os.path.isfile(filepath):
                continue

            if time.time() - os.path.getmtime(filepath) < CACHE_TIME:
                return filepath

            remove_file(filepath)

    except OSError as error:
        log.warning("Keshni tekshirib bo'lmadi: %s", error)

    return None


def remove_file(filepath: str) -> None:
    try:
        os.remove(filepath)
    except OSError as error:
        log.warning("Faylni o'chirib bo'lmadi (%s): %s", filepath, error)


def clean_old_cache() -> None:
    now = time.time()

    for filename in os.listdir(CACHE_DIR):
        filepath = os.path.join(CACHE_DIR, filename)

        if filepath == FILE_IDS_PATH or not os.path.isfile(filepath):
            continue

        if now - os.path.getmtime(filepath) > CACHE_TIME:
            remove_file(filepath)
            log.info("Eski kesh o'chirildi: %s", filename)

    expired = [
        key for key, entry in file_ids.items()
        if now - entry.get("time", 0) > FILE_ID_TIME
    ]

    for key in expired:
        file_ids.pop(key, None)

    if expired:
        save_file_ids()

    for user_id, last_time in list(user_last_request.items()):
        if now - last_time > USER_COOLDOWN:
            user_last_request.pop(user_id, None)


# =========================================================
# INSTAGRAM MEDIA YUKLASH
# =========================================================

def download_instagram_media_sync(url: str, key: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        # yt-dlp xatoni o'zi chop etmasin, uni biz log qilamiz
        "logger": ytdlp_log,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(CACHE_DIR, key + ".%(ext)s"),
        "restrictfilenames": True,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": MAX_UPLOAD_SIZE,
    }

    log.info("Instagram'dan yuklanmoqda: %s", url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

    except yt_dlp.utils.DownloadError as error:
        log.warning("Yuklash xatosi (%s): %s", url, error)
        raise DownloadFailed(classify_download_error(str(error))) from error

    if not os.path.isfile(filename):
        # Kengaytma o'zgargan yoki fayl hajm cheklovidan oshgan bo'lishi mumkin
        filename = find_cached_file(key)

    if not filename:
        size = (info or {}).get("filesize") or (info or {}).get("filesize_approx") or 0
        raise DownloadFailed("too_big" if size > MAX_UPLOAD_SIZE else "not_found")

    if os.path.getsize(filename) > MAX_UPLOAD_SIZE:
        remove_file(filename)
        raise DownloadFailed("too_big")

    log.info("Yuklandi: %s", filename)
    return filename


# =========================================================
# ODDIY RASM YUKLASH
# =========================================================

def download_image_sync(url: str) -> str:
    session = requests.Session()
    response = None

    try:
        # Har bir yo'naltirishni (redirect) alohida tekshiramiz
        for _ in range(MAX_REDIRECTS + 1):

            if not is_public_host(url):
                raise DownloadFailed("not_found")

            response = session.get(
                url,
                timeout=30,
                stream=True,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue

            break

        else:
            raise DownloadFailed("not_found")

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        if not content_type.startswith("image/"):
            raise DownloadFailed("not_found")

        extension = os.path.splitext(urlsplit(url).path)[1].lower()

        if extension not in IMAGE_LINK_EXTENSIONS:
            extension = ".jpg"

        # Noyob nom: ikki foydalanuvchining fayli to'qnashmaydi
        filepath = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex + extension)
        size = 0

        try:
            with open(filepath, "wb") as file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)

                    if size > MAX_IMAGE_SIZE:
                        raise DownloadFailed("too_big")

                    file.write(chunk)

        except BaseException:
            if os.path.exists(filepath):
                remove_file(filepath)
            raise

        return filepath

    except requests.RequestException as error:
        log.warning("Rasm yuklash xatosi (%s): %s", url, error)
        raise DownloadFailed("not_found") from error

    finally:
        if response is not None:
            response.close()

        session.close()


# =========================================================
# MEDIA YUBORISH
# =========================================================

def detect_kind(filepath: str) -> str:
    extension = os.path.splitext(filepath)[1].lower()

    if extension in VIDEO_EXTENSIONS:
        return "video"

    if extension in PHOTO_EXTENSIONS and os.path.getsize(filepath) <= MAX_PHOTO_SIZE:
        return "photo"

    return "document"


async def send_media(update: Update, kind: str, media, caption: str = "✅ Tayyor!"):
    """Media yuboradi va Telegram bergan file_id ni qaytaradi.

    media — ochiq fayl yoki avval saqlangan file_id.
    """
    message = update.effective_message
    timeouts = {"read_timeout": 120, "write_timeout": 120}

    if kind == "video":
        sent = await message.reply_video(
            video=media,
            caption=caption,
            supports_streaming=True,
            **timeouts,
        )
        result = sent.video or sent.animation or sent.document

    elif kind == "photo":
        sent = await message.reply_photo(photo=media, caption=caption, **timeouts)
        result = sent.photo[-1] if sent.photo else sent.document

    else:
        sent = await message.reply_document(document=media, caption=caption, **timeouts)
        result = sent.document or sent.video or sent.animation

    return result.file_id if result else None


async def send_file(update: Update, filepath: str, caption: str = "✅ Tayyor!"):
    kind = detect_kind(filepath)

    with open(filepath, "rb") as file:
        file_id = await send_media(update, kind, file, caption)

    return kind, file_id


# =========================================================
# HANDLERLAR
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Assalomu alaykum!\n\n"
        "📥 Instagram video va rasmlarini yuklab beruvchi bot.\n\n"
        "📌 Foydalanish:\n\n"
        "🎬 Instagram Reels havolasini yuboring\n"
        "🎥 Instagram video havolasini yuboring\n"
        "🖼 Instagram rasm havolasini yuboring\n\n"
        "⏳ Havolani yuboring, bot media'ni yuklab beradi."
    )


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception as error:
        log.warning("Xabarni tahrirlab bo'lmadi: %s", error)


async def safe_delete(message) -> None:
    try:
        await message.delete()
    except Exception as error:
        log.warning("Xabarni o'chirib bo'lmadi: %s", error)


async def handle_instagram(update: Update, url: str) -> None:
    url = normalize_instagram_url(url)
    key = get_cache_key(url)
    loading_message = await update.effective_message.reply_text("⏳")

    try:
        # Bir xil post bir vaqtda faqat bir marta yuklanadi
        async with download_locks.hold(key):

            # 1. Telegram file_id keshi — eng tez yo'l
            cached = file_ids.get(key)

            if cached:
                try:
                    await send_media(update, cached["kind"], cached["file_id"])
                    log.info("file_id keshidan yuborildi: %s", url)
                    await safe_delete(loading_message)
                    return

                except Exception as error:
                    log.warning("file_id ishlamadi, qayta yuklanadi: %s", error)
                    forget_file_id(key)

            # 2. Disk keshi yoki Instagram
            filepath = find_cached_file(key)

            if filepath:
                log.info("Disk keshidan olindi: %s", url)
            else:
                filepath = await asyncio.to_thread(
                    download_instagram_media_sync,
                    url,
                    key,
                )

            kind, file_id = await send_file(update, filepath)

            if file_id:
                remember_file_id(key, kind, file_id)

        await safe_delete(loading_message)

    except DownloadFailed as error:
        await safe_edit(loading_message, ERROR_TEXTS[error.reason])

    except Exception:
        log.exception("Instagram so'rovida xatolik: %s", url)
        await safe_edit(
            loading_message,
            "❌ Xatolik yuz berdi.\n\nIltimos, keyinroq qayta urinib ko'ring.",
        )


async def handle_image(update: Update, url: str) -> None:
    loading_message = await update.effective_message.reply_text("⏳")
    filepath = None

    try:
        filepath = await asyncio.to_thread(download_image_sync, url)
        await send_file(update, filepath, "✅ Rasm tayyor!")
        await safe_delete(loading_message)

    except DownloadFailed as error:
        text = (
            "📦 Rasm 20 MB dan katta."
            if error.reason == "too_big"
            else "❌ Rasmni yuklab bo'lmadi."
        )
        await safe_edit(loading_message, text)

    except Exception:
        log.exception("Rasm so'rovida xatolik: %s", url)
        await safe_edit(loading_message, "❌ Rasmni yuklashda xatolik.")

    finally:
        # Oddiy rasm keshlanmaydi, shuning uchun o'chiramiz
        if filepath and os.path.exists(filepath):
            remove_file(filepath)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message or not message.text:
        return

    url = extract_url(message.text)
    is_instagram = bool(url) and is_instagram_link(url)

    if not is_instagram and not (url and is_image_link(url)):
        await message.reply_text(
            "❌ Havola noto'g'ri.\n\n"
            "📥 Instagram Reels yoki video havolasini yuboring."
        )
        return

    # Cooldown faqat haqiqiy yuklash so'rovlariga qo'llanadi
    user_id = update.effective_user.id if update.effective_user else message.chat_id
    now = time.time()
    wait = USER_COOLDOWN - (now - user_last_request.get(user_id, 0))

    if wait > 0:
        await message.reply_text(
            f"⏳ Biroz kuting, {int(wait) + 1} soniyadan keyin yana yuboring."
        )
        return

    user_last_request[user_id] = now

    if is_instagram:
        await handle_instagram(update, url)
    else:
        await handle_image(update, url)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Bot xatosi", exc_info=context.error)


# =========================================================
# KESH TOZALOVCHI
# =========================================================

async def cache_cleaner():
    while True:
        try:
            clean_old_cache()
        except Exception:
            log.exception("Kesh tozalashda xatolik")

        # Har 30 daqiqada tekshiradi
        await asyncio.sleep(60 * 30)


async def post_init(application: Application):
    # Vazifaga havola saqlanadi, aks holda u xotiradan o'chib ketishi mumkin
    application.bot_data["cache_cleaner"] = asyncio.create_task(cache_cleaner())


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        sys.exit(
            "BOT_TOKEN topilmadi. .env.example faylidan nusxa olib, "
            ".env fayliga tokenni yozing."
        )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    log.info("Bot ishga tushdi")
    application.run_polling()


if __name__ == "__main__":
    main()
