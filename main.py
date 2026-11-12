"""Instagram, YouTube va Facebook'dan video hamda musiqa yuklovchi Telegram bot."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yt_dlp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
MEDIA_DB_PATH = os.path.join(CACHE_DIR, "file_ids.json")

# Diskdagi fayl keshi 6 soat saqlanadi
CACHE_TIME = 60 * 60 * 6

# Telegram file_id keshi 30 kun saqlanadi
FILE_ID_TIME = 60 * 60 * 24 * 30

# Bir foydalanuvchi bir vaqtda nechta so'rov bajara oladi
MAX_USER_JOBS = 2

# Telegram Bot API cheklovlari
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
SAFE_UPLOAD_SIZE = 48 * 1024 * 1024
MAX_PHOTO_SIZE = 10 * 1024 * 1024

# Video sifati: qisqa tomoni shu qiymatdan oshmaydi (720p)
MAX_VIDEO_SIDE = 720

# Musiqa
MAX_AUDIO_DURATION = 30 * 60
AUDIO_BITRATE = "192"

# Qidiruv
SEARCH_PAGE_SIZE = 10
SEARCH_MAX_RESULTS = 30
SEARCH_CACHE_TIME = 30 * 60
SEARCH_CACHE_LIMIT = 500
MAX_QUERY_LENGTH = 100
SEARCH_PREFIX = "🔎 "

# Oddiy rasm havolasi uchun cheklovlar
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_REDIRECTS = 3

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".avi")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
IMAGE_LINK_EXTENSIONS = PHOTO_EXTENSIONS + (".gif", ".bmp")
TEMP_EXTENSIONS = (".part", ".ytdl", ".tmp")

PLATFORM_HOSTS = {
    "instagram": ("instagram.com", "instagr.am"),
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
}

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
YOUTUBE_ID_PATTERN = re.compile(r"^[\w-]{11}$")
YOUTUBE_PATH_PATTERN = re.compile(r"^/(?:shorts|live|embed|v)/([\w-]{11})(?:/|$)")
CACHE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# Windows konsoli emoji yoki boshqa alifbodagi harflarni chiqara olmasa,
# log yozish xato bermasin
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="backslashreplace")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("instasave")

ytdlp_log = logging.getLogger("yt_dlp")
ytdlp_log.setLevel(logging.CRITICAL)


# =========================================================
# TASHQI DASTURLAR (ffmpeg, deno)
# =========================================================

def find_tool(name: str):
    """Dasturni PATH'dan yoki joriy Python muhitidan qidiradi."""
    path = shutil.which(name)

    if path:
        return path

    scripts_dir = sysconfig.get_path("scripts")

    for filename in (name + ".exe", name):
        candidate = os.path.join(scripts_dir, filename)

        if os.path.isfile(candidate):
            return candidate

    return None


def find_ffmpeg():
    path = find_tool("ffmpeg")

    if path:
        return path

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()

    except Exception:
        return None


# ffmpeg: video va ovozni birlashtirish hamda MP3 ga o'tkazish uchun
FFMPEG_PATH = find_ffmpeg()

# deno: YouTube himoyasini yechish uchun yt-dlp talab qiladi
DENO_PATH = find_tool("deno")


# =========================================================
# XATO TURLARI
# =========================================================

ERROR_TEXTS = {
    "private": (
        "🔒 Bu video yopiq yoki kirish talab qiladi.\n"
        "Bot faqat ochiq videolarni yuklay oladi."
    ),
    "restricted": (
        "🚫 Instagram bu postni ko'rsatmadi.\n"
        "Post o'chirilgan, profil yopiq yoki Instagram vaqtinchalik "
        "cheklov qo'ygan bo'lishi mumkin. Havolani tekshirib, "
        "birozdan keyin qayta urinib ko'ring."
    ),
    "not_found": (
        "🔍 Media topilmadi.\n"
        "Havola noto'g'ri yoki video o'chirilgan bo'lishi mumkin."
    ),
    "rate_limit": (
        "🚦 Sayt vaqtinchalik cheklov qo'ydi.\n"
        "Bir necha daqiqadan keyin qayta urinib ko'ring."
    ),
    "too_big": (
        "📦 Video eng past sifatda ham 50 MB dan katta.\n"
        "Telegram botlarga bunday faylni yuborishga ruxsat bermaydi."
    ),
    "too_long": (
        "⏱ Bu audio 30 daqiqadan uzun.\n"
        "Bot faqat 30 daqiqagacha bo'lgan musiqani yuboradi."
    ),
    "no_audio": "🔇 Bu videoda ovoz yo'q.",
    "live": "🔴 Jonli efirni yuklab bo'lmaydi.",
    "unknown": (
        "❌ Yuklab bo'lmadi.\n"
        "Iltimos, keyinroq qayta urinib ko'ring."
    ),
}

GENERIC_ERROR_TEXT = "❌ Xatolik yuz berdi.\n\nIltimos, keyinroq qayta urinib ko'ring."

BUSY_TEXT = "⏳ Oldingi so'rovlaringiz hali bajarilmoqda. Biroz kuting."

EXPIRED_TEXT = "Bu xabar eskirgan. So'rovni qaytadan yuboring."

UNSUPPORTED_SITE_TEXT = (
    "❌ Bu sayt qo'llab-quvvatlanmaydi.\n\n"
    "Instagram, YouTube yoki Facebook havolasini yuboring "
    "yoki qo'shiq nomini yozing."
)

NOT_A_VIDEO_TEXT = (
    "🔗 Bu havola aynan videoga olib bormaydi.\n\n"
    "Kanal yoki pleylist emas, videoning o'z havolasini yuboring."
)

START_TEXT = (
    "👋 Assalomu alaykum!\n\n"
    "📥 Men video va musiqa yuklab beraman.\n\n"
    "🎬 Video: Instagram, YouTube yoki Facebook havolasini yuboring.\n"
    "🎵 Musiqa: video tagidagi «🎵 Musiqa» tugmasini bosing.\n"
    "🔎 Qidiruv: ijrochi ismi, qo'shiq nomi yoki matnidan parcha yozing."
)


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

    # YouTube server manzilini shubhali deb topganda shunday javob beradi
    if "not a bot" in text:
        return "rate_limit"

    if any(word in text for word in (
        "login", "sign in", "private", "cookies", "authentic", "registered users",
    )):
        return "private"

    if any(word in text for word in ("rate", "429", "try again later", "too many")):
        return "rate_limit"

    if any(word in text for word in (
        "404", "not found", "unsupported url", "unavailable", "no video",
    )):
        return "not_found"

    return "unknown"


# =========================================================
# HAVOLALAR BILAN ISHLASH
# =========================================================

def extract_url(text: str):
    """Xabar ichidan birinchi havolani ajratib oladi."""
    match = URL_PATTERN.search(text)

    if not match:
        return None

    url = match.group(0).rstrip(".,)!")

    try:
        urlsplit(url)
    except ValueError:
        return None

    return url


def bare_host(url_parts) -> str:
    """www., m. kabi old qo'shimchalarsiz domen nomi."""
    host = (url_parts.hostname or "").lower()

    for prefix in ("www.", "m.", "mobile.", "web.", "music."):
        if host.startswith(prefix):
            return host[len(prefix):]

    return host


def detect_platform(url: str):
    host = (urlsplit(url).hostname or "").lower()

    for platform, hosts in PLATFORM_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return platform

    return None


def normalize_instagram_url(url: str) -> str:
    """?igsh=... kabi qo'shimchalarni olib tashlaydi."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()

    if host.startswith("www."):
        host = host[4:]

    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


def youtube_video_id(url: str):
    parts = urlsplit(url.strip())
    host = bare_host(parts)

    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]

    elif parts.path.rstrip("/") == "/watch":
        candidate = parse_qs(parts.query).get("v", [""])[0]

    else:
        match = YOUTUBE_PATH_PATTERN.match(parts.path)
        candidate = match.group(1) if match else ""

    return candidate if YOUTUBE_ID_PATTERN.match(candidate) else None


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_facebook_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = bare_host(parts)

    if host == "facebook.com":
        host = "www.facebook.com"

    # Faqat videoni aniqlaydigan parametrlar qoldiriladi
    query = parse_qs(parts.query)
    kept = {
        name: query[name][0]
        for name in ("v", "story_fbid", "id", "fbid")
        if name in query
    }

    return urlunsplit(("https", host, parts.path, urlencode(kept), ""))


def normalize_url(url: str, platform: str):
    """Bitta video turli havolalar bilan kelsa ham bir xil ko'rinishga keltiradi.

    Havola videoga olib bormasa (masalan, YouTube kanali) None qaytaradi.
    """
    if platform == "instagram":
        return normalize_instagram_url(url)

    if platform == "youtube":
        video_id = youtube_video_id(url)
        return youtube_watch_url(video_id) if video_id else None

    if platform == "facebook":
        return normalize_facebook_url(url)

    return None


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
# MATN YORDAMCHILARI
# =========================================================

def clean_query(text: str) -> str:
    """Qidiruv so'rovini bir qatorga keltiradi va uzunligini cheklaydi."""
    return " ".join(text.split())[:MAX_QUERY_LENGTH].strip()


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())

    if len(text) <= limit:
        return text

    return text[: limit - 1].rstrip() + "…"


def format_duration(seconds) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"


def describe_media(info) -> dict:
    """yt-dlp ma'lumotidan audio uchun nom, ijrochi va davomiylikni oladi."""
    info = info or {}

    title = str(info.get("track") or info.get("title") or "").strip()
    performer = str(
        info.get("artist") or ", ".join(info.get("artists") or [])
    ).strip()

    # "Ijrochi - Qo'shiq" ko'rinishidagi nomni ikkiga ajratamiz
    if not performer and " - " in title:
        performer, title = (part.strip() for part in title.split(" - ", 1))

    if not performer:
        performer = str(info.get("uploader") or info.get("channel") or "").strip()

    return {
        "title": shorten(title, 64) or "Audio",
        "performer": shorten(performer, 64),
        "duration": int(info.get("duration") or 0),
    }


def audio_filename(meta: dict, filepath: str) -> str:
    name = meta["title"]

    if meta.get("performer"):
        name = f"{meta['performer']} - {name}"

    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()[:80] or "audio"
    return name + os.path.splitext(filepath)[1]


# =========================================================
# MEDIA BAZASI (TELEGRAM FILE_ID KESHI)
# =========================================================
#
# Telegram bir marta yuklangan faylni o'z serverida saqlaydi.
# file_id ni eslab qolsak, keyingi safar faylni qayta yuklamasdan
# bir zumda yuboramiz.
#
# Har bir yozuv: url, kind, file_id (video), audio_id (musiqa),
# title, performer, duration, time.

def load_media_db() -> dict:
    try:
        with open(MEDIA_DB_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except FileNotFoundError:
        return {}

    except Exception as error:
        log.warning("Media bazasini o'qib bo'lmadi: %s", error)
        return {}


def save_media_db() -> None:
    try:
        temp_path = MEDIA_DB_PATH + ".tmp"

        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(media_db, file, ensure_ascii=False)

        os.replace(temp_path, MEDIA_DB_PATH)

    except Exception as error:
        log.warning("Media bazasini saqlab bo'lmadi: %s", error)


def update_media(key: str, **fields) -> None:
    """Yozuvni yangilaydi; None berilgan maydon o'chiriladi."""
    entry = media_db.setdefault(key, {})

    for name, value in fields.items():
        if value is None:
            entry.pop(name, None)
        else:
            entry[name] = value

    entry["time"] = time.time()
    save_media_db()


# =========================================================
# GLOBAL HOLAT
# =========================================================

media_db = load_media_db()

# So'rov matni -> (vaqt, natijalar)
search_cache = {}


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


class UserJobs:
    """Bir foydalanuvchining bir vaqtdagi so'rovlari sonini cheklaydi."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = {}

    def start(self, user_id) -> bool:
        count = self.active.get(user_id, 0)

        if count >= self.limit:
            return False

        self.active[user_id] = count + 1
        return True

    def finish(self, user_id) -> None:
        count = self.active.get(user_id, 0) - 1

        if count > 0:
            self.active[user_id] = count
        else:
            self.active.pop(user_id, None)


locks = KeyedLocks()
user_jobs = UserJobs(MAX_USER_JOBS)


# =========================================================
# DISK KESHI
# =========================================================

def find_cached_file(name: str):
    """Keshdan `name.<kengaytma>` faylini qaytaradi, bo'lmasa None.

    Video uchun name — kesh kaliti, audio uchun — kalit + "_audio".
    """
    try:
        for filename in os.listdir(CACHE_DIR):
            stem, extension = os.path.splitext(filename)

            # yt-dlp vaqtinchalik fayllari (kalit.f137.mp4, kalit.mp4.part) o'tkazib yuboriladi
            if stem != name or extension.lower() in TEMP_EXTENSIONS:
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

        if filepath == MEDIA_DB_PATH or not os.path.isfile(filepath):
            continue

        if now - os.path.getmtime(filepath) > CACHE_TIME:
            remove_file(filepath)
            log.info("Eski kesh o'chirildi: %s", filename)

    expired = [
        key for key, entry in media_db.items()
        if now - entry.get("time", 0) > FILE_ID_TIME
    ]

    for key in expired:
        media_db.pop(key, None)

    if expired:
        save_media_db()

    for query, (saved_at, _) in list(search_cache.items()):
        if now - saved_at > SEARCH_CACHE_TIME:
            search_cache.pop(query, None)


# =========================================================
# YT-DLP SOZLAMALARI
# =========================================================

def base_ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        # yt-dlp xatoni o'zi chop etmasin, uni biz log qilamiz
        "logger": ytdlp_log,
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        # Fayl vaqti yuklangan payt bo'lsin, aks holda kesh uni eski deb o'chiradi
        "updatetime": False,
    }

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    if DENO_PATH:
        opts["js_runtimes"] = {"deno": {"path": DENO_PATH}}

    return opts


def skip_live(info, *, incomplete=False):
    """Jonli efir cheksiz davom etadi, shuning uchun yuklanmaydi."""
    return "live" if info.get("is_live") else None


def skip_live_and_long(info, *, incomplete=False):
    if info.get("is_live"):
        return "live"

    if (info.get("duration") or 0) > MAX_AUDIO_DURATION:
        return "too long"

    return None


def make_youtube_selector(state: dict):
    """50 MB ga sig'adigan eng yaxshi YouTube sifatini tanlaydi.

    YouTube yuqori sifatda video va ovozni alohida beradi, shuning uchun
    ularning umumiy hajmini hisoblab, ffmpeg bilan birlashtiramiz.
    """

    def has_video(fmt) -> bool:
        return fmt.get("vcodec") not in (None, "none")

    def has_audio(fmt) -> bool:
        return fmt.get("acodec") not in (None, "none")

    def size_of(fmt):
        return fmt.get("filesize") or fmt.get("filesize_approx")

    def short_side(fmt) -> int:
        width, height = fmt.get("width"), fmt.get("height")

        if width and height:
            return min(width, height)

        return height or 0

    def quality(candidate):
        video = candidate[0]
        return (short_side(video), video.get("tbr") or 0)

    def selector(ctx):
        # Formatlar yomondan yaxshiga qarab tartiblangan
        direct = [
            fmt for fmt in ctx.get("formats") or []
            if fmt.get("protocol") in ("https", "http")
        ]

        audios = [
            fmt for fmt in direct
            if has_audio(fmt) and not has_video(fmt) and fmt.get("ext") == "m4a"
        ]
        best_audio = audios[-1] if audios else None

        # Har bir nomzod: (video, yuklanadigan qismlar, umumiy hajm)
        candidates = []

        for fmt in direct:
            if (
                not has_video(fmt)
                or fmt.get("ext") != "mp4"
                or short_side(fmt) > MAX_VIDEO_SIDE
            ):
                continue

            if has_audio(fmt):
                candidates.append((fmt, [fmt], size_of(fmt)))

            elif best_audio and FFMPEG_PATH:
                video_size, audio_size = size_of(fmt), size_of(best_audio)
                total = video_size + audio_size if video_size and audio_size else None
                candidates.append((fmt, [fmt, best_audio], total))

        fitting = [c for c in candidates if c[2] and c[2] <= SAFE_UPLOAD_SIZE]
        unknown = [c for c in candidates if not c[2]]

        # H.264 (avc1) hamma qurilmada ochiladi, shuning uchun u afzal
        compatible = [
            c for c in fitting
            if str(c[0].get("vcodec", "")).startswith("avc1")
        ]

        if compatible or fitting:
            choice = max(compatible or fitting, key=quality)

        elif unknown:
            choice = max(unknown, key=quality)

        else:
            state["too_big"] = bool(candidates)
            return

        parts = choice[1]

        if len(parts) == 1:
            yield parts[0]
            return

        video, audio = parts

        yield {
            "format_id": f"{video['format_id']}+{audio['format_id']}",
            "ext": "mp4",
            "requested_formats": [video, audio],
            "protocol": f"{video['protocol']}+{audio['protocol']}",
        }

    return selector


# =========================================================
# VIDEO YUKLASH
# =========================================================

def download_video_sync(url: str, key: str, platform: str):
    """Videoni keshga yuklaydi; (fayl yo'li, audio ma'lumoti) qaytaradi."""
    state = {}

    opts = base_ydl_opts()
    opts.update({
        "format": (
            make_youtube_selector(state)
            if platform == "youtube"
            else "b[ext=mp4]/b"
        ),
        "outtmpl": os.path.join(CACHE_DIR, key + ".%(ext)s"),
        "merge_output_format": "mp4",
        "max_filesize": MAX_UPLOAD_SIZE,
        "match_filter": skip_live,
    })

    log.info("Video yuklanmoqda (%s): %s", platform, url)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}

    except yt_dlp.utils.DownloadError as error:
        if state.get("too_big"):
            raise DownloadFailed("too_big") from error

        log.warning("Yuklash xatosi (%s): %s", url, error)
        raise DownloadFailed(classify_download_error(str(error))) from error

    if info.get("is_live"):
        raise DownloadFailed("live")

    filepath = find_cached_file(key)

    if not filepath:
        # Fayl hajm cheklovidan oshgani uchun yuklanmagan bo'lishi mumkin
        size = info.get("filesize") or info.get("filesize_approx") or 0
        raise DownloadFailed("too_big" if size > MAX_UPLOAD_SIZE else "not_found")

    if os.path.getsize(filepath) > MAX_UPLOAD_SIZE:
        remove_file(filepath)
        raise DownloadFailed("too_big")

    log.info("Yuklandi: %s", filepath)
    return filepath, describe_media(info)


# =========================================================
# MUSIQA (AUDIO)
# =========================================================

def extract_audio_sync(video_path: str, key: str) -> str:
    """Keshdagi videoning ovozini MP3 qilib ajratadi (tarmoqsiz)."""
    target = os.path.join(CACHE_DIR, key + "_audio.mp3")
    temp_path = target + ".part"

    command = [
        FFMPEG_PATH, "-y", "-loglevel", "error",
        "-i", video_path,
        "-vn",
        "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE + "k",
        "-f", "mp3", temp_path,
    ]

    try:
        result = subprocess.run(command, capture_output=True, timeout=300)

    except (OSError, subprocess.TimeoutExpired) as error:
        log.warning("ffmpeg ishga tushmadi: %s", error)
        raise DownloadFailed("unknown") from error

    if (
        result.returncode != 0
        or not os.path.isfile(temp_path)
        or os.path.getsize(temp_path) == 0
    ):
        if os.path.exists(temp_path):
            remove_file(temp_path)

        details = result.stderr.decode("utf-8", "replace").strip()[-300:]
        log.warning("Ovozni ajratib bo'lmadi (%s): %s", video_path, details)
        raise DownloadFailed("no_audio")

    os.replace(temp_path, target)
    return target


def download_audio_sync(url: str, key: str):
    """Ovozni manbadan yuklaydi; (fayl yo'li, audio ma'lumoti) qaytaradi."""
    name = key + "_audio"

    opts = base_ydl_opts()
    opts.update({
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(CACHE_DIR, name + ".%(ext)s"),
        "max_filesize": MAX_UPLOAD_SIZE,
        "match_filter": skip_live_and_long,
    })

    if FFMPEG_PATH:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": AUDIO_BITRATE,
        }]

    log.info("Musiqa yuklanmoqda: %s", url)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}

    except yt_dlp.utils.DownloadError as error:
        log.warning("Musiqa yuklash xatosi (%s): %s", url, error)
        raise DownloadFailed(classify_download_error(str(error))) from error

    if info.get("is_live"):
        raise DownloadFailed("live")

    if (info.get("duration") or 0) > MAX_AUDIO_DURATION:
        raise DownloadFailed("too_long")

    filepath = find_cached_file(name)

    if not filepath:
        raise DownloadFailed("not_found")

    if os.path.getsize(filepath) > MAX_UPLOAD_SIZE:
        remove_file(filepath)
        raise DownloadFailed("too_long")

    log.info("Musiqa tayyor: %s", filepath)
    return filepath, describe_media(info)


# =========================================================
# QIDIRUV
# =========================================================

def search_sync(query: str) -> list:
    """YouTube'dan qidiradi; faqat 30 daqiqagacha bo'lgan videolar qoladi."""
    opts = base_ydl_opts()
    opts.update({
        "extract_flat": True,
        "skip_download": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"ytsearch{SEARCH_MAX_RESULTS}:{query}",
                download=False,
            ) or {}

    except yt_dlp.utils.DownloadError as error:
        log.warning("Qidiruv xatosi (%s): %s", query, error)
        raise DownloadFailed("unknown") from error

    results = []

    for entry in info.get("entries") or []:
        video_id = str((entry or {}).get("id") or "")
        duration = (entry or {}).get("duration")

        # Davomiyligi yo'q natijalar — jonli efir, kanal yoki pleylist
        if not YOUTUBE_ID_PATTERN.match(video_id) or not duration:
            continue

        if duration > MAX_AUDIO_DURATION:
            continue

        results.append({
            "id": video_id,
            "title": entry.get("title") or "Nomsiz",
            "duration": int(duration),
        })

    return results


async def get_search_results(query: str) -> list:
    cache_key = query.lower()

    async with locks.hold("search:" + cache_key):
        cached = search_cache.get(cache_key)

        if cached and time.time() - cached[0] < SEARCH_CACHE_TIME:
            return cached[1]

        results = await asyncio.to_thread(search_sync, query)

        if len(search_cache) >= SEARCH_CACHE_LIMIT:
            search_cache.clear()

        search_cache[cache_key] = (time.time(), results)
        return results


def build_search_page(query: str, results: list, page: int):
    """Natijalar sahifasi uchun matn va tugmalarni tayyorlaydi."""
    pages = max(1, math.ceil(len(results) / SEARCH_PAGE_SIZE))
    page = min(max(page, 1), pages)

    start = (page - 1) * SEARCH_PAGE_SIZE
    items = results[start:start + SEARCH_PAGE_SIZE]

    # So'rov birinchi qatorda turadi: sahifa almashganda shu yerdan o'qiladi
    lines = [SEARCH_PREFIX + query, ""]

    for number, item in enumerate(items, 1):
        lines.append(
            f"{number}. {shorten(item['title'], 60)} — "
            f"{format_duration(item['duration'])}"
        )

    lines += ["", f"📄 Sahifa {page}/{pages}"]

    number_buttons = [
        InlineKeyboardButton(str(number), callback_data=f"s:{item['id']}")
        for number, item in enumerate(items, 1)
    ]

    rows = [number_buttons[:5], number_buttons[5:]]

    navigation = []

    if page > 1:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"pg:{page - 1}"))

    navigation.append(InlineKeyboardButton("❌", callback_data="x"))

    if page < pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"pg:{page + 1}"))

    rows.append(navigation)

    return "\n".join(lines), InlineKeyboardMarkup([row for row in rows if row])


def parse_search_query(text):
    """Natijalar xabarining birinchi qatoridan so'rovni qayta o'qiydi."""
    first_line = (text or "").split("\n", 1)[0]

    if not first_line.startswith(SEARCH_PREFIX):
        return None

    return clean_query(first_line[len(SEARCH_PREFIX):]) or None


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
# TELEGRAM'GA YUBORISH
# =========================================================

SEND_TIMEOUTS = {"read_timeout": 300, "write_timeout": 300}


def detect_kind(filepath: str) -> str:
    extension = os.path.splitext(filepath)[1].lower()

    if extension in VIDEO_EXTENSIONS:
        return "video"

    if extension in PHOTO_EXTENSIONS and os.path.getsize(filepath) <= MAX_PHOTO_SIZE:
        return "photo"

    return "document"


def music_markup(key: str, kind: str):
    """Video tagidagi «Musiqa» tugmasi."""
    if kind != "video":
        return None

    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 Musiqa", callback_data=f"mus:{key}"),
    ]])


async def send_media(bot, chat_id, kind: str, media, caption="✅ Tayyor!", markup=None):
    """Media yuboradi va Telegram bergan file_id ni qaytaradi.

    media — ochiq fayl yoki avval saqlangan file_id.
    """
    common = {
        "chat_id": chat_id,
        "caption": caption,
        "reply_markup": markup,
        **SEND_TIMEOUTS,
    }

    if kind == "video":
        sent = await bot.send_video(video=media, supports_streaming=True, **common)
        result = sent.video or sent.animation or sent.document

    elif kind == "photo":
        sent = await bot.send_photo(photo=media, **common)
        result = sent.photo[-1] if sent.photo else sent.document

    else:
        sent = await bot.send_document(document=media, **common)
        result = sent.document or sent.video or sent.animation

    return result.file_id if result else None


async def send_file(bot, chat_id, filepath: str, caption="✅ Tayyor!", markup=None):
    with open(filepath, "rb") as file:
        return await send_media(
            bot, chat_id, detect_kind(filepath), file, caption, markup,
        )


async def send_audio_file(bot, chat_id, filepath: str, meta: dict):
    with open(filepath, "rb") as file:
        sent = await bot.send_audio(
            chat_id=chat_id,
            audio=file,
            filename=audio_filename(meta, filepath),
            title=meta["title"],
            performer=meta.get("performer") or None,
            duration=meta.get("duration") or None,
            **SEND_TIMEOUTS,
        )

    result = sent.audio or sent.document or sent.voice
    return result.file_id if result else None


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


async def safe_answer(query, text=None, alert=False) -> None:
    """Tugma bosilganiga javob; eskirgan so'rovda xato bermaydi."""
    try:
        await query.answer(text=text, show_alert=alert)
    except Exception as error:
        log.warning("Tugmaga javob berib bo'lmadi: %s", error)


# =========================================================
# VIDEO HAVOLASI
# =========================================================

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str):
    message = update.effective_message
    bot, chat_id = context.bot, message.chat_id

    url = normalize_url(url, platform)

    if not url:
        await message.reply_text(NOT_A_VIDEO_TEXT)
        return

    key = get_cache_key(url)
    status = await message.reply_text("⏳")

    try:
        # Bir xil video bir vaqtda faqat bir marta yuklanadi
        async with locks.hold(key):
            entry = media_db.get(key) or {}

            # 1. Telegram file_id keshi — eng tez yo'l
            if entry.get("file_id"):
                kind = entry.get("kind") or "video"

                try:
                    await send_media(
                        bot, chat_id, kind, entry["file_id"],
                        markup=music_markup(key, kind),
                    )

                except Exception as error:
                    log.warning("file_id ishlamadi, qayta yuklanadi: %s", error)
                    update_media(key, file_id=None)

                else:
                    log.info("file_id keshidan yuborildi: %s", url)

                    if not entry.get("url"):
                        update_media(key, url=url)

                    await safe_delete(status)
                    return

            # 2. Disk keshi yoki manba sayt
            meta = {}
            filepath = find_cached_file(key)

            if filepath:
                log.info("Disk keshidan olindi: %s", url)
            else:
                filepath, meta = await asyncio.to_thread(
                    download_video_sync, url, key, platform,
                )

            kind = detect_kind(filepath)

            # Tugma ishlashi uchun havola yuborishdan oldin saqlanadi
            update_media(key, url=url, kind=kind, **meta)

            file_id = await send_file(
                bot, chat_id, filepath, markup=music_markup(key, kind),
            )
            update_media(key, file_id=file_id)

        await safe_delete(status)

    except DownloadFailed as error:
        await safe_edit(status, ERROR_TEXTS[error.reason])

    except Exception:
        log.exception("Havola so'rovida xatolik: %s", url)
        await safe_edit(status, GENERIC_ERROR_TEXT)


# =========================================================
# MUSIQA YUBORISH
# =========================================================

async def deliver_audio(bot, chat_id, key: str, url: str) -> None:
    """Musiqani eng tez yo'l bilan yuboradi.

    Tartib: Telegram file_id -> diskdagi MP3 -> keshdagi videodan ajratish
    -> manbadan yuklash.
    """
    status = None

    try:
        async with locks.hold("audio:" + key):
            entry = media_db.get(key) or {}

            if entry.get("audio_id"):
                try:
                    await bot.send_audio(chat_id=chat_id, audio=entry["audio_id"])

                except Exception as error:
                    log.warning("audio_id ishlamadi, qayta tayyorlanadi: %s", error)
                    update_media(key, audio_id=None)

                else:
                    log.info("Musiqa file_id keshidan yuborildi: %s", url)
                    return

            status = await bot.send_message(chat_id, "⏳")

            meta = None
            filepath = find_cached_file(key + "_audio")

            if filepath:
                log.info("Musiqa disk keshidan olindi: %s", url)

            else:
                video_path = find_cached_file(key) if FFMPEG_PATH else None

                if video_path and video_path.lower().endswith(VIDEO_EXTENSIONS):
                    log.info("Musiqa keshdagi videodan ajratilmoqda: %s", url)
                    filepath = await asyncio.to_thread(
                        extract_audio_sync, video_path, key,
                    )
                else:
                    filepath, meta = await asyncio.to_thread(
                        download_audio_sync, url, key,
                    )

            if not meta:
                meta = {
                    "title": entry.get("title") or "Audio",
                    "performer": entry.get("performer") or "",
                    "duration": entry.get("duration") or 0,
                }

            audio_id = await send_audio_file(bot, chat_id, filepath, meta)
            update_media(key, url=url, audio_id=audio_id, **meta)

        await safe_delete(status)

    except DownloadFailed as error:
        text = ERROR_TEXTS[error.reason]

        if status:
            await safe_edit(status, text)
        else:
            await bot.send_message(chat_id, text)

    except Exception:
        log.exception("Musiqa so'rovida xatolik: %s", url)

        if status:
            await safe_edit(status, GENERIC_ERROR_TEXT)


# =========================================================
# QIDIRUV SO'ROVI
# =========================================================

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    status = await update.effective_message.reply_text("🔎")

    try:
        results = await get_search_results(query)

    except DownloadFailed:
        await safe_edit(
            status,
            "❌ Qidiruvda xatolik yuz berdi.\n\nBirozdan keyin qayta urinib ko'ring.",
        )
        return

    if not results:
        await safe_edit(
            status,
            f"😕 «{query}» bo'yicha hech narsa topilmadi.\n\n"
            "Boshqacha yozib ko'ring.",
        )
        return

    log.info("Qidiruv: %s (%d ta natija)", query, len(results))

    text, markup = build_search_page(query, results, 1)

    try:
        await status.edit_text(
            text,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        log.exception("Qidiruv natijasini ko'rsatib bo'lmadi: %s", query)
        await safe_edit(status, GENERIC_ERROR_TEXT)


# =========================================================
# ODDIY RASM HAVOLASI
# =========================================================

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    message = update.effective_message
    status = await message.reply_text("⏳")
    filepath = None

    try:
        filepath = await asyncio.to_thread(download_image_sync, url)
        await send_file(context.bot, message.chat_id, filepath, "✅ Rasm tayyor!")
        await safe_delete(status)

    except DownloadFailed as error:
        text = (
            "📦 Rasm 20 MB dan katta."
            if error.reason == "too_big"
            else "❌ Rasmni yuklab bo'lmadi."
        )
        await safe_edit(status, text)

    except Exception:
        log.exception("Rasm so'rovida xatolik: %s", url)
        await safe_edit(status, "❌ Rasmni yuklashda xatolik.")

    finally:
        # Oddiy rasm keshlanmaydi, shuning uchun o'chiramiz
        if filepath and os.path.exists(filepath):
            remove_file(filepath)


# =========================================================
# HANDLERLAR
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(START_TEXT)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()
    url = extract_url(text)

    if url:
        platform = detect_platform(url)

        if platform:
            job = (handle_link, url, platform)

        elif is_image_link(url):
            job = (handle_image, url)

        else:
            await message.reply_text(UNSUPPORTED_SITE_TEXT)
            return

    else:
        # Guruhlarda har bir xabarni qidiruv deb qabul qilmaymiz
        if message.chat.type != "private":
            return

        query = clean_query(text)

        if len(query) < 2:
            await message.reply_text(
                "🔎 Qidirish uchun ijrochi ismi yoki qo'shiq nomini yozing."
            )
            return

        job = (handle_search, query)

    user_id = update.effective_user.id if update.effective_user else message.chat_id

    if not user_jobs.start(user_id):
        await message.reply_text(BUSY_TEXT)
        return

    try:
        handler, *args = job
        await handler(update, context, *args)
    finally:
        user_jobs.finish(user_id)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = update.effective_chat

    if not query or not chat:
        return

    data = query.data or ""
    user_id = update.effective_user.id if update.effective_user else chat.id

    # --- Natijalar xabarini yopish ---
    if data == "x":
        await safe_answer(query)

        try:
            await query.delete_message()
        except Exception as error:
            log.warning("Xabarni o'chirib bo'lmadi: %s", error)

        return

    # --- Qidiruv sahifasini almashtirish ---
    if data.startswith("pg:"):
        search_query = parse_search_query(getattr(query.message, "text", None))

        if not search_query or not data[3:].isdigit():
            await safe_answer(query, EXPIRED_TEXT, alert=True)
            return

        try:
            results = await get_search_results(search_query)
        except DownloadFailed:
            await safe_answer(query, "Qidiruvda xatolik. Qayta urinib ko'ring.", alert=True)
            return

        await safe_answer(query)
        text, markup = build_search_page(search_query, results, int(data[3:]))

        try:
            await query.edit_message_text(
                text,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as error:
            # Xuddi shu sahifa qayta so'ralsa, Telegram "not modified" deydi
            if "not modified" not in str(error).lower():
                log.warning("Sahifani yangilab bo'lmadi: %s", error)

        return

    # --- Musiqa: qidiruv natijasi yoki video tagidagi tugma ---
    if data.startswith("s:") and YOUTUBE_ID_PATTERN.match(data[2:]):
        url = youtube_watch_url(data[2:])
        key = get_cache_key(url)

    elif data.startswith("mus:") and CACHE_KEY_PATTERN.match(data[4:]):
        key = data[4:]
        url = (media_db.get(key) or {}).get("url")

        if not url:
            await safe_answer(query, EXPIRED_TEXT, alert=True)
            return

    else:
        await safe_answer(query)
        return

    if not user_jobs.start(user_id):
        await safe_answer(query, BUSY_TEXT, alert=True)
        return

    try:
        await safe_answer(query, "⏳ Musiqa tayyorlanmoqda...")
        await deliver_audio(context.bot, chat.id, key, url)
    finally:
        user_jobs.finish(user_id)


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

    if not FFMPEG_PATH:
        log.warning("ffmpeg topilmadi: YouTube sifati past bo'ladi, MP3 ishlamaydi")

    if not DENO_PATH:
        log.warning("deno topilmadi: YouTube'dan yuklash ishlamasligi mumkin")

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
            handle_message,
        )
    )
    application.add_error_handler(error_handler)

    log.info("Bot ishga tushdi")
    application.run_polling()


if __name__ == "__main__":
    main()
