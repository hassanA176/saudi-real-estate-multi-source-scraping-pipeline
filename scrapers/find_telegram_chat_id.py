"""مساعدة اختيارية للحصول على chat_id من رسالة /start مرسلة لبوتك الخاص."""
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token or token.startswith("PASTE_"):
    raise SystemExit("ضع TELEGRAM_BOT_TOKEN الحقيقي في .env أولًا.")

try:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data={"offset": -20, "limit": 20, "timeout": 0},
        timeout=10,
    )
    body = response.json()
    if response.status_code != 200 or not body.get("ok"):
        raise RuntimeError()
except (requests.RequestException, RuntimeError, ValueError):
    raise SystemExit("تعذّر قراءة رسائل البوت. تحقق من التوكن وعدم وجود Webhook/قارئ تحديثات آخر.") from None

chats = {}
for update in body.get("result") or []:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") == "private" and str(message.get("text") or "").startswith("/start"):
        chats[chat.get("id")] = chat.get("first_name") or "محادثة خاصة"
if not chats:
    print("أرسل /start للبوت من حساب تليجرام الخاص بك، ثم أعد تشغيل هذا الملف.")
else:
    for chat_id, name in chats.items():
        print(f"محادثة خاصة: {name} | TELEGRAM_ALLOWED_CHAT_ID={chat_id}")
