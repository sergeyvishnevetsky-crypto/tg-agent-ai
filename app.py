import os
import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, render_template_string

# Локально подхватываем .env, на Heroku переменные будут из окружения
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-agent")

# === ENV ===
TG_API_ID_RAW = os.getenv("TG_API_ID", "0")
TG_API_ID = int(TG_API_ID_RAW) if TG_API_ID_RAW.isdigit() else 0
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

REQUIRED_OK = all([TG_API_ID, TG_API_HASH, TG_SESSION, OPENAI_API_KEY])
if not REQUIRED_OK:
    logger.warning("Не все обязательные переменные окружения заданы "
                   "(TG_API_ID, TG_API_HASH, TG_SESSION, OPENAI_API_KEY). "
                   "Worker упадёт до их заполнения.")

# === OpenAI клиент ===
oa_client = None
if OPENAI_API_KEY:
    oa_client = OpenAI(api_key=OPENAI_API_KEY)

# === Telethon клиент (user-аккаунт, не бот) ===
client = None
if TG_API_ID and TG_API_HASH and TG_SESSION:
    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

# Память диалогов в RAM: chat_id -> [messages]
dialogues = {}


async def ask_llm(chat_id: int, user_text: str) -> str:
    """
    Отправляем историю диалога + новый текст в OpenAI,
    получаем ответ.
    """
    if oa_client is None:
        raise RuntimeError("OpenAI клиент не инициализирован (нет OPENAI_API_KEY)")

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


if client is not None:
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
    if not START_MESSAGE or not TARGET_IDS or client is None:
        return

    logger.info("Шлю стартовое сообщение %d адресатам", len(TARGET_IDS))
    for uid in TARGET_IDS:
        try:
            await client.send_message(uid, START_MESSAGE)
            logger.info("Стартовое сообщение отправлено: %s", uid)
        except Exception as e:
            logger.exception("Не удалось отправить %s: %s", uid, e)


async def main():
    if not REQUIRED_OK:
        raise RuntimeError(
            "Не заданы TG_API_ID, TG_API_HASH, TG_SESSION или OPENAI_API_KEY. "
            "Заполни их в переменных окружения (локально или на Heroku)."
        )
    if client is None:
        raise RuntimeError("TelegramClient не инициализирован (проверь TG_* переменные).")

    await client.start()
    logger.info("Telegram-агент запущен (worker)")

    # один раз на старте
    await send_initial_messages()

    # дальше просто слушаем входящие
    await client.run_until_disconnected()


# === Flask веб-интерфейс ===

app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Telegram AI Agent — статус</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; background:#111827; color:#e5e7eb; padding:40px; }
    .card { max-width:720px; margin:0 auto; background:#020617; border-radius:24px; padding:28px 32px; box-shadow:0 20px 40px rgba(0,0,0,0.5); border:1px solid #1f2937; }
    h1 { font-size:26px; margin-top:0; margin-bottom:8px; }
    .subtitle { color:#9ca3af; margin-bottom:20px; }
    .badge { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; margin-left:8px; }
    .ok { background:#064e3b; color:#a7f3d0; }
    .bad { background:#7f1d1d; color:#fecaca; }
    .section-title { margin-top:20px; margin-bottom:8px; font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:#9ca3af; }
    ul { list-style:none; padding-left:0; margin:0; }
    li { padding:6px 0; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid #111827; font-size:14px; }
    code { background:#111827; padding:2px 6px; border-radius:6px; font-size:12px; }
    .hint { font-size:13px; color:#9ca3af; margin-top:12px; line-height:1.5; }
    .pill { font-size:12px; padding:2px 8px; border-radius:999px; background:#111827; color:#e5e7eb; margin-left:6px; }
    a { color:#60a5fa; text-decoration:none; }
    a:hover { text-decoration:underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Telegram AI Agent</h1>
    <div class="subtitle">
      Приложение развёрнуто на Heroku. Ниже — статус ключевых настроек.
    </div>

    <div>
      Статус worker:
      {% if required_ok %}
        <span class="badge ok">можно запускать</span>
      {% else %}
        <span class="badge bad">требуется настройка</span>
      {% endif %}
    </div>

    <div class="section-title">Обязательные переменные окружения</div>
    <ul>
      <li>
        <span>TG_API_ID</span>
        {% if has_tg_api_id %}<span class="badge ok">задано</span>{% else %}<span class="badge bad">нет</span>{% endif %}
      </li>
      <li>
        <span>TG_API_HASH</span>
        {% if has_tg_api_hash %}<span class="badge ok">задано</span>{% else %}<span class="badge bad">нет</span>{% endif %}
      </li>
      <li>
        <span>TG_SESSION</span>
        {% if has_tg_session %}<span class="badge ok">задано</span>{% else %}<span class="badge bad">нет</span>{% endif %}
      </li>
      <li>
        <span>OPENAI_API_KEY</span>
        {% if has_openai_key %}<span class="badge ok">задано</span>{% else %}<span class="badge bad">нет</span>{% endif %}
      </li>
    </ul>

    <div class="section-title">Дополнительные настройки</div>
    <ul>
      <li>
        <span>TARGET_IDS</span>
        <span>
          {% if target_ids_raw %}
            <code>{{ target_ids_raw }}</code>
          {% else %}
            <span class="pill">пусто</span>
          {% endif %}
        </span>
      </li>
      <li>
        <span>START_MESSAGE</span>
        <span>
          {% if start_message %}
            <span class="pill">задано</span>
          {% else %}
            <span class="pill">пусто</span>
          {% endif %}
        </span>
      </li>
      <li>
        <span>SYSTEM_PROMPT</span>
        <span>
          {% if system_prompt %}
            <span class="pill">задано</span>
          {% else %}
            <span class="pill">по умолчанию</span>
          {% endif %}
        </span>
      </li>
    </ul>

    <div class="hint">
      Чтобы изменить значения, зайди в Heroku → <b>Settings</b> → <b>Config Vars</b> и добавь/обнови переменные.<br>
      Worker запускается командой <code>heroku ps:scale worker=1</code> (после настройки переменных).
    </div>
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        required_ok=REQUIRED_OK,
        has_tg_api_id=bool(TG_API_ID),
        has_tg_api_hash=bool(TG_API_HASH),
        has_tg_session=bool(TG_SESSION),
        has_openai_key=bool(OPENAI_API_KEY),
        target_ids_raw=TARGET_IDS_RAW,
        start_message=START_MESSAGE,
        system_prompt=SYSTEM_PROMPT,
    )


if __name__ == "__main__":
    asyncio.run(main())
