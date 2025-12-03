import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH")

if not (TG_API_ID and TG_API_HASH):
    raise RuntimeError("Сначала в .env или окружении задай TG_API_ID и TG_API_HASH")

with TelegramClient(StringSession(), TG_API_ID, TG_API_HASH) as client:
    session_str = client.session.save()
    print("\nВставь это значение в переменную TG_SESSION (локально и на Heroku):\n")
    print(session_str)
    print("\nНЕ показывай это никому.\n")
