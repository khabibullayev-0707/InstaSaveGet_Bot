# InstaSaveGet Bot

Instagram, YouTube va Facebook'dan video hamda musiqa yuklab beruvchi Telegram bot.

## Imkoniyatlar

- **Video yuklash**: Instagram, YouTube (Shorts ham) yoki Facebook havolasi
  yuborilsa, bot videoni yuklab beradi.
- **«🎵 Musiqa» tugmasi**: har bir video tagida turadi; bosilsa, o'sha
  videoning ovozi MP3 bo'lib keladi.
- **Qo'shiq qidirish**: ijrochi ismi, qo'shiq nomi yoki matnidan parcha
  yozilsa, bot 10 tadan natija ko'rsatadi (⬅️ ➡️ bilan sahifalanadi).
  Tanlangan qo'shiq MP3 bo'lib keladi.

## O'rnatish

1. Python 3.10 yoki undan yangisini o'rnating.
2. Kutubxonalarni o'rnating:

   ```
   pip install -r requirements.txt
   ```

   `ffmpeg` va `deno` ham shu buyruq bilan o'rnatiladi, ularni alohida
   yuklash shart emas.

3. `.env.example` faylidan nusxa olib, nomini `.env` qiling va ichiga
   [@BotFather](https://t.me/BotFather) bergan tokenni yozing:

   ```
   BOT_TOKEN=123456:ABC...
   ```

4. Botni ishga tushiring:

   ```
   python main.py
   ```

## Qanday ishlaydi

- Video va musiqa `yt-dlp` orqali yuklanadi, qidiruv YouTube'da bajariladi.
- Yuklangan fayllar `cache/` papkasida 6 soat saqlanadi.
- Telegram bergan `file_id` 30 kun eslab qolinadi, shuning uchun bir xil
  video yoki qo'shiq qayta so'ralsa, bot uni bir zumda yuboradi.
- YouTube videosi 50 MB ga sig'adigan eng yaxshi sifatda (720p gacha) olinadi.
- Bir foydalanuvchi bir vaqtda ko'pi bilan 2 ta so'rov bajara oladi.

## Cheklovlar

- Yopiq profil va yopiq videolar yuklanmaydi.
- 50 MB dan katta fayllar yuborilmaydi (Telegram Bot API cheklovi).
- Musiqa 30 daqiqadan uzun bo'lmasligi kerak.
- Jonli efirlar yuklanmaydi.
- Instagram'dagi bir nechta rasm yoki videodan iborat postlardan (karusel)
  hozircha faqat bittasi yuklanadi.

## Yuklash ishlamay qolsa

Saytlar tez-tez o'zgaradi. Yuklash to'xtab qolsa, avval `yt-dlp` ni yangilang:

```
pip install -U "yt-dlp[default]"
```
