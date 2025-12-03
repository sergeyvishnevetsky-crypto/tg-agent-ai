import os
import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, render_template_string, request, redirect, url_for, flash
import psycopg2

# --- базовая настройка ---

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-agent")

TG_API_ID_RAW = os.getenv("TG_API_ID", "0")
TG_API_ID = int(TG_API_ID_RAW) if TG_API_ID_RAW.isdigit() else 0
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

TARGET_IDS_RAW = os.getenv("TARGET_IDS", "")
START_MESSAGE = os.getenv("START_MESSAGE", "")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты дружелюбный Telegram-агент. Отвечай кратко, по делу, на русском языке."
)

DATABASE_URL = os.getenv("DATABASE_URL")

REQUIRED_OK = all([TG_API_ID, TG_API_HASH, TG_SESSION, OPENAI_API_KEY])

# --- OpenAI ---

oa_client = None
if OPENAI_API_KEY:
    oa_client = OpenAI(api_key=OPENAI_API_KEY)

# --- Telethon (user-аккаунт, не бот) ---

client = None
if TG_API_ID and TG_API_HASH and TG_SESSION:
    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

# Память диалогов в RAM: chat_id -> [messages]
dialogues = {}


# --- Работа с базой (тезисы для ИИ) ---

def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """
    Создаём таблицу ai_prompt и стартовую запись, если их ещё нет.
    """
    conn = get_db_conn()
    if conn is None:
        logger.warning("DATABASE_URL не задан, веб-редактор тезисов ИИ работать не будет.")
        return

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_prompt (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                cur.execute("SELECT id FROM ai_prompt LIMIT 1;")
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO ai_prompt (content) VALUES (%s);",
                        (SYSTEM_PROMPT,),
                    )
                    logger.info("Создана стартовая запись ai_prompt.")
    finally:
        conn.close()


def get_prompt_from_db():
    """
    Берём текущие тезисы из БД (если есть).
    """
    conn = get_db_conn()
    if conn is None:
        return None

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT content FROM ai_prompt ORDER BY id LIMIT 1;")
                row = cur.fetchone()
                if row:
                    return row[0]
                return None
    finally:
        conn.close()


def set_prompt_in_db(text: str):
    """
    Обновляем/создаём единственную запись с тезисами.
    """
    conn = get_db_conn()
    if conn is None:
        raise RuntimeError("DATABASE_URL не задан, некуда сохранить тезисы.")

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM ai_prompt ORDER BY id LIMIT 1;")
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE ai_prompt SET content=%s, updated_at=NOW() WHERE id=%s;",
                        (text, row[0]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO ai_prompt (content) VALUES (%s);",
                        (text,),
                    )
    finally:
        conn.close()


# Инициализируем таблицу при старте (если база есть)
init_db()


# --- LLM-логика ---

async def ask_llm(chat_id: int, user_text: str) -> str:
    """
    Берём актуальные тезисы из БД, собираем историю и спрашиваем OpenAI.
    """
    if oa_client is None:
        raise RuntimeError("OpenAI клиент не инициализирован (нет OPENAI_API_KEY)")

    # Берём текст тезисов из БД, если есть; иначе — SYSTEM_PROMPT из env
    system_prompt = get_prompt_from_db() or SYSTEM_PROMPT

    history = dialogues.setdefault(chat_id, [])

    messages = [{"role": "system", "content": system_prompt}]
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
    При старте воркера — разослать стартовое сообщение, если задано.
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

    await send_initial_messages()
    await client.run_until_disconnected()


# --- Flask веб-интерфейс ---

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-me")


INDEX_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Telegram AI Agent — статус</title>
</head>
<body style="font-family: system-ui, -apple-system; background:#111827; color:#e5e7eb;">
  <div style="max-width:720px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <h1 style="margin:0 0 4px 0;font-size:24px;">Telegram AI Agent</h1>
        <div style="color:#9ca3af;font-size:14px;">Статус приложения и настроек.</div>
      </div>
      <div>
        <a href="{{ url_for('edit_prompt') }}" style="padding:6px 12px;border-radius:999px;border:1px solid #374151;color:#e5e7eb;text-decoration:none;font-size:13px;">✏️ Тезисы для ИИ</a>
      </div>
    </div>

    <h3>Обязательные переменные</h3>
    <ul>
      <li>TG_API_ID: {{ 'ok' if has_tg_api_id else 'нет' }}</li>
      <li>TG_API_HASH: {{ 'ok' if has_tg_api_hash else 'нет' }}</li>
      <li>TG_SESSION: {{ 'ok' if has_tg_session else 'нет' }}</li>
      <li>OPENAI_API_KEY: {{ 'ok' if has_openai_key else 'нет' }}</li>
    </ul>

    <h3>Дополнительные</h3>
    <ul>
      <li>TARGET_IDS: {{ target_ids_raw or 'пусто' }}</li>
      <li>START_MESSAGE: {{ 'задано' if start_message else 'пусто' }}</li>
      <li>SYSTEM_PROMPT (env по умолчанию): {{ 'задан' if system_prompt else 'по умолчанию' }}</li>
    </ul>

    <p style="font-size:13px;color:#9ca3af;">
      Worker запускается командой <code>heroku ps:scale worker=1</code> (после настройки переменных).<br>
      Тезисы для ИИ можно редактировать на странице «Тезисы для ИИ».
    </p>
  </div>
</body>
</html>
"""


PROMPT_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Тезисы для ИИ — Telegram Agent</title>
</head>
<body style="font-family: system-ui, -apple-system; background:#020617; color:#e5e7eb;">
  <div style="max-width:840px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <h1 style="margin-top:0;font-size:22px;">Тезисы для ИИ</h1>
    <p style="color:#9ca3af;font-size:14px;">
      Здесь ты задаёшь, <b>о чём именно должен говорить агент</b> и как себя вести.<br>
      Этот текст попадает в системный промпт модели и влияет на все ответы.
    </p>

    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div style="margin:8px 0 12px 0;color:#bbf7d0;font-size:13px;">
          {% for m in messages %}
            {{ m }}
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <form method="post">
      <div style="margin-bottom:8px;font-size:13px;color:#9ca3af;">Основные тезисы и правила общения:</div>
      <textarea name="content" rows="16" style="width:100%;border-radius:12px;border:1px solid #374151;background:#020617;color:#e5e7eb;padding:10px;font-size:14px;resize:vertical;">{{ content or "" }}</textarea>
      <div style="margin-top:12px;display:flex;gap:12px;align-items:center;">
        <button type="submit" style="border:none;border-radius:999px;padding:8px 18px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer;">
          💾 Сохранить
        </button>
        <a href="{{ url_for('index') }}" style="font-size:13px;color:#9ca3af;text-decoration:none;">← Назад к статусу</a>
      </div>
    </form>
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        has_tg_api_id=bool(TG_API_ID),
        has_tg_api_hash=bool(TG_API_HASH),
        has_tg_session=bool(TG_SESSION),
        has_openai_key=bool(OPENAI_API_KEY),
        target_ids_raw=TARGET_IDS_RAW,
        start_message=START_MESSAGE,
        system_prompt=SYSTEM_PROMPT,
    )


@app.route("/prompt", methods=["GET", "POST"])
def edit_prompt():
    if request.method == "POST":
        text = request.form.get("content", "").strip()
        try:
            set_prompt_in_db(text or SYSTEM_PROMPT)
            flash("Тезисы обновлены.")
        except Exception as e:
            logger.exception("Ошибка при сохранении тезисов: %s", e)
            flash("Ошибка при сохранении тезисов, смотри логи.")
        return redirect(url_for("edit_prompt"))

    current = get_prompt_from_db() or SYSTEM_PROMPT
    return render_template_string(PROMPT_HTML, content=current)


if __name__ == "__main__":
    asyncio.run(main())
