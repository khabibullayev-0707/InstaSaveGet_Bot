import os
import re
import asyncio
import hashlib
import time
import requests
import yt_dlp

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)


# =========================================================
# KONFIGURATSIYA
# =========================================================

BOT_TOKEN = "7976473561:AAG_o09OrcpJcF5snBw1qiDNgMwJn4lQYjc"

DOWNLOAD_DIR = "downloads"
CACHE_DIR = "cache"

# Cache 6 soat saqlanadi
CACHE_TIME = 60 * 60 * 6

# Bir foydalanuvchi orasidagi minimal vaqt
USER_COOLDOWN = 5


# Papkalarni yaratish
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)


# =========================================================
# GLOBAL O'ZGARUVCHILAR
# =========================================================

# Foydalanuvchining oxirgi yuklash vaqti
user_last_download = {}

# Bir xil URL uchun lock
download_locks = {}


# =========================================================
# INSTAGRAM LINKNI TEKSHIRISH
# =========================================================

def is_instagram_link(text: str) -> bool:

    pattern = r"^https?://(www\.)?(instagram\.com|instagr\.am)/.+"

    return bool(
        re.match(
            pattern,
            text.strip(),
            re.IGNORECASE
        )
    )


# =========================================================
# RASM LINKINI TEKSHIRISH
# =========================================================

def is_image_link(text: str) -> bool:

    text = text.strip().lower()

    image_extensions = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp"
    )

    return (
        text.endswith(image_extensions)
        or "i.imgur.com" in text
        or "ibb.co" in text
    )


# =========================================================
# URL UCHUN CACHE NOMI
# =========================================================

def get_cache_key(url: str) -> str:

    return hashlib.md5(
        url.encode("utf-8")
    ).hexdigest()


# =========================================================
# CACHE'DAN FAYL QIDIRISH
# =========================================================

def get_cached_file(url: str):

    key = get_cache_key(url)

    try:

        for filename in os.listdir(CACHE_DIR):

            if not filename.startswith(key + "."):
                continue

            filepath = os.path.join(
                CACHE_DIR,
                filename
            )

            # Fayl mavjudligini tekshirish
            if not os.path.isfile(filepath):
                continue

            # Cache yoshi
            file_age = (
                time.time()
                - os.path.getmtime(filepath)
            )

            # Hali amal qilayotgan cache
            if file_age < CACHE_TIME:

                print(
                    "✅ Cache'dan olindi:",
                    filepath
                )

                return filepath

            # Eski cache'ni o'chirish
            try:

                os.remove(filepath)

                print(
                    "🗑 Eski cache o'chirildi:",
                    filepath
                )

            except Exception as e:

                print(
                    "Cache o'chirish xatosi:",
                    e
                )

    except Exception as e:

        print(
            "Cache tekshirish xatosi:",
            e
        )

    return None


# =========================================================
# ESKI CACHE'LARNI TOZALASH
# =========================================================

def clean_old_cache():

    try:

        current_time = time.time()

        for filename in os.listdir(CACHE_DIR):

            filepath = os.path.join(
                CACHE_DIR,
                filename
            )

            if not os.path.isfile(filepath):
                continue

            file_age = (
                current_time
                - os.path.getmtime(filepath)
            )

            if file_age > CACHE_TIME:

                try:

                    os.remove(filepath)

                    print(
                        "🗑 Cache tozalandi:",
                        filename
                    )

                except Exception as e:

                    print(
                        "Cache tozalash xatosi:",
                        e
                    )

    except Exception as e:

        print(
            "Cache tozalashda xato:",
            e
        )


# =========================================================
# INSTAGRAM MEDIA YUKLASH
# =========================================================

def download_instagram_media_sync(url: str):

    try:

        # -------------------------------------------------
        # 1. Avval cache tekshiriladi
        # -------------------------------------------------

        cached_file = get_cached_file(url)

        if cached_file:

            return cached_file


        # -------------------------------------------------
        # 2. Cache fayl nomi
        # -------------------------------------------------

        cache_key = get_cache_key(url)

        output_template = os.path.join(
            CACHE_DIR,
            cache_key + ".%(ext)s"
        )


        # -------------------------------------------------
        # 3. yt-dlp sozlamalari
        # -------------------------------------------------

        ydl_opts = {

            "quiet": True,

            "no_warnings": True,

            "noplaylist": True,

            "format": "best",

            "outtmpl": output_template,

            "restrictfilenames": True,

            "nocheckcertificate": True,

            # Tarmoq xatosida bir necha marta urinadi
            "retries": 2,

            "fragment_retries": 2,

            # Playlist yuklanmasin
            "extract_flat": False,
        }


        # -------------------------------------------------
        # 4. Yuklash
        # -------------------------------------------------

        print(
            "⬇️ Instagram'dan yuklanmoqda..."
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            filename = ydl.prepare_filename(
                info
            )


        # -------------------------------------------------
        # 5. Faylni tekshirish
        # -------------------------------------------------

        if os.path.exists(filename):

            print(
                "✅ Yuklandi:",
                filename
            )

            return filename


        # -------------------------------------------------
        # 6. Fayl extension o'zgargan bo'lishi mumkin
        # -------------------------------------------------

        for file in os.listdir(CACHE_DIR):

            if file.startswith(
                cache_key + "."
            ):

                filepath = os.path.join(
                    CACHE_DIR,
                    file
                )

                if os.path.isfile(filepath):

                    print(
                        "✅ Media topildi:",
                        filepath
                    )

                    return filepath


        return None


    except Exception as e:

        print(
            "❌ Instagram yuklash xatosi:",
            e
        )

        return None


# =========================================================
# ASYNC INSTAGRAM YUKLASH
# =========================================================

async def download_instagram_media(url: str):

    # URL uchun lock yaratish
    if url not in download_locks:

        download_locks[url] = asyncio.Lock()


    lock = download_locks[url]


    async with lock:

        # Lock ichida yana cache tekshiramiz
        cached_file = get_cached_file(url)

        if cached_file:

            return cached_file


        # Blocking yt-dlp ni alohida thread'da ishlatamiz
        return await asyncio.to_thread(
            download_instagram_media_sync,
            url
        )


# =========================================================
# RASM YUKLASH
# =========================================================

def download_image_sync(url: str):

    try:

        response = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        response.raise_for_status()


        # URL'dan nom olish
        filename = (
            url
            .split("/")[-1]
            .split("?")[0]
        )


        if (
            not filename
            or "." not in filename
        ):

            filename = "image.jpg"


        # Fayl nomini xavfsiz qilish
        filename = re.sub(
            r'[<>:"/\\|?*]',
            "_",
            filename
        )


        filepath = os.path.join(
            DOWNLOAD_DIR,
            filename
        )


        # Faylni saqlash
        with open(
            filepath,
            "wb"
        ) as file:

            file.write(
                response.content
            )


        return filepath


    except Exception as e:

        print(
            "❌ Rasm yuklash xatosi:",
            e
        )

        return None


# =========================================================
# ASYNC RASM YUKLASH
# =========================================================

async def download_image(url: str):

    return await asyncio.to_thread(
        download_image_sync,
        url
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "👋 Assalomu alaykum!\n\n"

        "📥 Instagram video va rasmlarini "
        "yuklab beruvchi bot.\n\n"

        "📌 Foydalanish:\n\n"

        "🎬 Instagram Reels havolasini yuboring\n"
        "🎥 Instagram video havolasini yuboring\n"
        "🖼 Instagram rasm havolasini yuboring\n\n"

        "⏳ Havolani yuboring, bot media'ni "
        "yuklab beradi."
    )


    await update.message.reply_text(
        text
    )


# =========================================================
# MEDIA YUBORISH
# =========================================================

async def send_media(
    update: Update,
    filepath: str
):

    try:

        if (
            not filepath
            or not os.path.exists(filepath)
        ):

            return False


        extension = os.path.splitext(
            filepath
        )[1].lower()


        # =================================================
        # VIDEO
        # =================================================

        if extension in (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".avi"
        ):

            with open(
                filepath,
                "rb"
            ) as video:

                await update.message.reply_video(
                    video=video,
                    caption="✅ Tayyor!",
                    supports_streaming=True
                )


        # =================================================
        # RASM
        # =================================================

        elif extension in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp"
        ):

            with open(
                filepath,
                "rb"
            ) as photo:

                await update.message.reply_photo(
                    photo=photo,
                    caption="✅ Tayyor!"
                )


        # =================================================
        # BOSHQA FORMAT
        # =================================================

        else:

            with open(
                filepath,
                "rb"
            ) as document:

                await update.message.reply_document(
                    document=document,
                    caption="✅ Media tayyor!"
                )


        return True


    except Exception as e:

        print(
            "❌ Media yuborish xatosi:",
            e
        )

        return False


# =========================================================
# ASOSIY MESSAGE HANDLER
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if (
        not update.message
        or not update.message.text
    ):

        return


    text = update.message.text.strip()

    user = update.message.from_user

    user_id = user.id

    # =====================================================
    # USER COOLDOWN
    # =====================================================

    current_time = time.time()

    last_download = user_last_download.get(
        user_id,
        0
    )


    if (
        current_time - last_download
        < USER_COOLDOWN
    ):

        await update.message.reply_text(
            "⏳ Biroz kuting...\n"
            "Oldingi so'rovingiz hali qayta ishlanmoqda."
        )

        return


    # =====================================================
    # INSTAGRAM LINK
    # =====================================================

    if is_instagram_link(text):

        user_last_download[user_id] = (
            current_time
        )


        # Qum soati
        loading_message = (
            await update.message.reply_text(
                "⏳"
            )
        )


        filepath = None


        try:

            # ---------------------------------------------
            # Cache yoki Instagram
            # ---------------------------------------------

            filepath = await download_instagram_media(
                text
            )


            # ---------------------------------------------
            # Yuklash amalga oshmadi
            # ---------------------------------------------

            if not filepath:

                await loading_message.edit_text(
                    "❌ Yuklab bo'lmadi.\n\n"

                    "Sabablari:\n"

                    "• Havola noto'g'ri\n"
                    "• Profil yopiq\n"
                    "• Media mavjud emas\n"
                    "• Instagram vaqtinchalik cheklov qo'ygan\n"
                    "• yt-dlp eski versiya bo'lishi mumkin"
                )

                return


            # ---------------------------------------------
            # Media yuborish
            # ---------------------------------------------

            success = await send_media(
                update,
                filepath
            )


            # ---------------------------------------------
            # ⏳ NI O'CHIRISH
            # ---------------------------------------------

            if success:

                try:

                    await loading_message.delete()

                except Exception:
                    pass


            else:

                try:

                    await loading_message.edit_text(
                        "❌ Media Telegram'ga yuborilmadi."
                    )

                except Exception:
                    pass


        except Exception as e:

            print(
                "❌ Asosiy xatolik:",
                e
            )


            try:

                await loading_message.edit_text(
                    "❌ Xatolik yuz berdi.\n\n"
                    "Iltimos, keyinroq qayta urinib ko'ring."
                )

            except Exception:
                pass


        finally:

            # Cache faylni O'CHIRMAYMIZ!
            #
            # Chunki keyingi so'rovda cache'dan
            # foydalanishimiz kerak.
            #
            # Faqat eski cache'lar keyinchalik
            # clean_old_cache() orqali o'chiriladi.

            pass


        return


    # =====================================================
    # RASM LINK
    # =====================================================

    if is_image_link(text):

        loading_message = (
            await update.message.reply_text(
                "⏳"
            )
        )


        filepath = None


        try:

            filepath = await download_image(
                text
            )


            if not filepath:

                await loading_message.edit_text(
                    "❌ Rasmni yuklab bo'lmadi."
                )

                return


            # Rasm yuborish
            with open(
                filepath,
                "rb"
            ) as photo:

                await update.message.reply_photo(
                    photo=photo,
                    caption="✅ Rasm tayyor!"
                )


            # ⏳ o'chirish
            try:

                await loading_message.delete()

            except Exception:
                pass


        except Exception as e:

            print(
                "❌ Rasm xatosi:",
                e
            )


            try:

                await loading_message.edit_text(
                    "❌ Rasmni yuklashda xatolik."
                )

            except Exception:
                pass


        finally:

            # Oddiy rasm cache emas,
            # shuning uchun o'chiramiz.

            if (
                filepath
                and os.path.exists(filepath)
            ):

                try:

                    os.remove(filepath)

                except Exception:
                    pass


        return


    # =====================================================
    # NOTO'G'RI LINK
    # =====================================================

    await update.message.reply_text(
        "❌ Havola noto'g'ri.\n\n"

        "📥 Instagram Reels yoki video "
        "havolasini yuboring."
    )


# =========================================================
# PERIODIC CACHE CLEANER
# =========================================================

async def cache_cleaner():

    while True:

        try:

            clean_old_cache()

        except Exception as e:

            print(
                "Cache cleaner xatosi:",
                e
            )

        # Har 30 daqiqada tekshiradi
        await asyncio.sleep(
            60 * 30
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "❌ Bot xatosi:",
        context.error
    )


# =========================================================
# BOTNI ISHGA TUSHIRISH
# =========================================================

async def post_init(
    application: Application
):

    # Cache cleaner ishga tushadi
    asyncio.create_task(
        cache_cleaner()
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "🤖 Bot ishga tushmoqda..."
    )


    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )


    # /start
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    # Oddiy text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )


    # Error handler
    application.add_error_handler(
        error_handler
    )


    print(
        "✅ Bot ishga tushdi!"
    )


    application.run_polling()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()