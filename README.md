# InstaSaveGet Bot

Instagram'dagi ochiq video, Reels va rasmlarni yuklab beruvchi Telegram bot.

## O'rnatish

1. Python 3.10 yoki undan yangisini o'rnating.
2. Kutubxonalarni o'rnating:

   ```
   pip install -r requirements.txt
   ```

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

- Foydalanuvchi Instagram havolasini yuboradi, bot uni `yt-dlp` orqali yuklaydi.
- Yuklangan fayl `cache/` papkasida 6 soat saqlanadi.
- Telegram bergan `file_id` 30 kun eslab qolinadi, shuning uchun bir xil
  post qayta so'ralsa, bot uni qayta yuklamasdan bir zumda yuboradi.
- Bir foydalanuvchi 5 soniyada bitta so'rov yubora oladi.

## Cheklovlar

- Yopiq profillardagi postlar yuklanmaydi.
- 50 MB dan katta fayllar yuborilmaydi (Telegram Bot API cheklovi).
- Bir nechta rasm yoki videodan iborat postlardan (karusel) hozircha
  faqat bittasi yuklanadi.

## Instagram ishlamay qolsa

Instagram tez-tez o'zgaradi. Yuklash to'xtab qolsa, avval `yt-dlp` ni yangilang:

```
pip install -U yt-dlp
```
