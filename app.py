import os
import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv
from openai import OpenAI

# Локально подхватываем .env, на Heroku переменные будут из окружения
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-agent")

# === ENV ===
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")  # строка сессии Telethon

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# список ID (юзеры/чаты/группы), через запятую: "12345,-1002222"
TARGET_IDS_RAW = os.getenv("TARGET_IDS", "")
START_MESSAGE = os.getenv("START_MESSAGE", "")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты дружелюбный Telegram-агент. Отвечай кратко, по делу, на русском языке."
)

if not (TG_API_ID and TG_API_HASH and TG_SESSION and OPENAI_API_KEY):
    raise RuntimeError("Не заданы TG_API_ID, TG_API_HASH, TG_SESSION или OPENAI_API_KEY")

# === OpenAI клиент ===
oa_client = OpenAI(api_key=OPENAI_API_KEY)

# === Telethon клиент (user-аккаунт, не бот) ===
client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

# Память диалогов в RAM: chat_id -> [messages]
dialogues = {}


async def ask_llm(chat_id: int, user_text: str) -> str:
    """
    Отправляем историю диалога + новый текст в OpenAI,
    получаем ответ.
    """
    history = dialogues.setdefault(chat_id, [])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # последние 10 сообщений (user+assistant)
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    resp = oa_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
    )

    reply = resp.choices[0].message.content

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})

    return reply


def parse_target_ids(raw: str):
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Не удалось распарсить ID: %r", part)
    return ids


TARGET_IDS = parse_target_ids(TARGET_IDS_RAW)


@client.on(events.NewMessage(incoming=True))
async def on_new_message(event):
    """
    Обработчик входящих сообщений.
    """
    # Игнорируем собственные исходящие
    if event.out:
        return

    chat_id = event.chat_id
    text = event.raw_text

    logger.info("Сообщение от %s: %s", chat_id, text)

    try:
        reply = await ask_llm(chat_id, text)
        await event.respond(reply)
        logger.info("Ответ отправлен в %s", chat_id)
    except Exception as e:
        logger.exception("Ошибка при обработке сообщения: %s", e)


async def send_initial_messages():
    """
    По желанию: при старте разослать первое сообщение TARGET_IDS.
    Если START_MESSAGE пустой или список пуст — ничего не делает.
    """
    if not START_MESSAGE or not TARGET_IDS:
        return

    logger.info("Шлю стартовое сообщение %d адресатам", len(TARGET_IDS))
    for uid in TARGET_IDS:
        try:
            await client.send_message(uid, START_MESSAGE)
            logger.info("Стартовое сообщение отправлено: %s", uid)
        except Exception as e:
            logger.exception("Не удалось отправить %s: %s", uid, e)


async def main():
    await client.start()
    logger.info("Telegram-агент запущен")

    # один раз на старте
    await send_initial_messages()

    # дальше просто слушаем входящие
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
