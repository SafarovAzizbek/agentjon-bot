# 🤖 Agentjon — AI Telegram Bot

Gemini AI va DuckDuckGo web-qidiruv asosida ishlaydigan aqlli Telegram bot.

## Imkoniyatlari

- 💬 **Jonli yozish (Streaming)** — javobni qismlab ko'rsatadi
- 🔍 **Internet Qidiruv** — yangiliklar va faktlarni internetdan qidiradi
- 🎤 **Ovozli xabar** — ovozni tinglab, javob beradi
- 🖼 **Rasm tahlili** — rasmdagi narsalarni aniqlaydi
- 📄 **Hujjat tahlili** — PDF, kod va boshqa fayllarni o'qiydi
- 📍 **Joylashuv** — yuborilgan joylashuvga mos javob beradi
- 👋 **Yangi a'zolarni kutib olish** — guruhga kirganlarni tabrik qiladi
- 🛡 **Moderatsiya** — nojo'ya xabarlarni avtomatik o'chiradi
- 😀 **Reaksiyalar** — xabarlarga emoji reaksiya qoldiradi

## Lokal Ishga Tushirish

```bash
# 1. Virtual muhit yaratish
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# 2. Kutubxonalarni o'rnatish
pip install -r requirements.txt

# 3. .env fayl yaratish
# .env faylga quyidagilarni yozing:
# TELEGRAM_BOT_TOKEN=your_token_here
# GEMINI_API_KEY=your_key_here

# 4. Botni ishga tushirish (polling rejimi)
python bot_core.py
```

## Vercel'ga Deploy Qilish

### 1. GitHub'ga Push Qilish

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/agentjon-bot.git
git push -u origin main
```

### 2. Vercel'da Loyiha Yaratish

1. [vercel.com](https://vercel.com) ga kiring
2. **"Add New Project"** bosing
3. GitHub reponi tanlang
4. **Framework Preset** → `Other` tanlang
5. **Deploy** bosing

### 3. Environment Variables Sozlash

Vercel dashboard → **Settings** → **Environment Variables**:

| Nomi | Qiymati |
|:---|:---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot tokeningiz |
| `GEMINI_API_KEY` | Google Gemini API kalitingiz |
| `WEBHOOK_SECRET` | (Ixtiyoriy) Xavfsizlik uchun maxfiy so'z |

### 4. Webhook O'rnatish

Deploy tugagandan keyin, brauzeringizda quyidagi URL ni oching:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://<YOUR_APP>.vercel.app/api/webhook
```

Agar `WEBHOOK_SECRET` o'rnatgan bo'lsangiz:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://<YOUR_APP>.vercel.app/api/webhook&secret_token=<YOUR_SECRET>
```

### 5. Tekshirish

Telegram'da botga `/start` yuboring. Agar javob kelsa — hammasi tayyor! 🎉

## Loyiha Strukturasi

```
├── api/
│   └── index.py         # Vercel serverless webhook endpoint
├── bot_core.py           # Bot yadro (handlers, tools, formatting)
├── requirements.txt      # Python kutubxonalari
├── vercel.json           # Vercel konfiguratsiya
├── .gitignore
├── .env                  # Lokal muhit o'zgaruvchilari (Git'ga yuklanmaydi!)
└── README.md
```

## Muhim Eslatmalar

- **Vercel Hobby plan**: Funksiya max 60 soniya ishlaydi
- **Suhbat tarixi**: Serverless muhitda har so'rov mustaqil (tarix tashqi bazasiz saqlanmaydi)
- **Lokal rejim**: `python bot_core.py` — polling rejimida ishlaydi, tarix saqlanadi
