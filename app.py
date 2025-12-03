import os
import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient as SyncTelegramClient
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

# env по умолчанию (на случай, если БД нет)
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


# --- Работа с базой (тезисы, настройки рассылки, история рассылок) ---

def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """
    Создаём таблицы:
      - ai_prompt      (тезисы для ИИ)
      - agent_settings (настройки рассылки)
      - broadcast_log  (история рассылок)
    """
    conn = get_db_conn()
    if conn is None:
        logger.warning("DATABASE_URL не задан, БД-функции (тезисы/рассылка/лог) работать не будут.")
        return

    try:
        with conn:
            with conn.cursor() as cur:
                # Тезисы для ИИ
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_prompt (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                # Настройки рассылки
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_settings (
                        id SERIAL PRIMARY KEY,
                        target_ids TEXT,
                        start_message TEXT,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                # История рассылок
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS broadcast_log (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT,
                        chat_type TEXT,
                        chat_name TEXT,
                        message TEXT,
                        success BOOLEAN,
                        error TEXT,
                        sent_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                # Стартовая запись для ai_prompt
                cur.execute("SELECT id FROM ai_prompt LIMIT 1;")
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO ai_prompt (content) VALUES (%s);",
                        (SYSTEM_PROMPT,),
                    )
                    logger.info("Создана стартовая запись ai_prompt.")

                # Стартовая запись для agent_settings
                cur.execute("SELECT id FROM agent_settings LIMIT 1;")
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO agent_settings (target_ids, start_message) VALUES (%s, %s);",
                        (TARGET_IDS_RAW, START_MESSAGE),
                    )
                    logger.info("Создана стартовая запись agent_settings.")
    finally:
        conn.close()


def get_prompt_from_db():
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


def get_agent_settings():
    """
    Берём текущие TARGET_IDS и START_MESSAGE из БД.
    Если БД нет или записи нет — возвращаем значения из env.
    """
    conn = get_db_conn()
    if conn is None:
        return TARGET_IDS_RAW, START_MESSAGE

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT target_ids, start_message FROM agent_settings ORDER BY id LIMIT 1;"
                )
                row = cur.fetchone()
                if row:
                    return row[0] or "", row[1] or ""
                else:
                    return TARGET_IDS_RAW, START_MESSAGE
    finally:
        conn.close()


def set_agent_settings(target_ids: str, start_message: str):
    conn = get_db_conn()
    if conn is None:
        raise RuntimeError("DATABASE_URL не задан, некуда сохранить настройки рассылки.")

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM agent_settings ORDER BY id LIMIT 1;")
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE agent_settings
                           SET target_ids=%s,
                               start_message=%s,
                               updated_at=NOW()
                         WHERE id=%s;
                        """,
                        (target_ids, start_message, row[0]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO agent_settings (target_ids, start_message) VALUES (%s, %s);",
                        (target_ids, start_message),
                    )
    finally:
        conn.close()


def log_broadcast(chat_id, chat_type, chat_name, message, success, error_text=None):
    """
    Пишем одну запись в историю рассылки.
    """
    conn = get_db_conn()
    if conn is None:
        logger.warning("DATABASE_URL не задан — лог рассылки не сохраняется.")
        return

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO broadcast_log
                        (chat_id, chat_type, chat_name, message, success, error)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (chat_id, chat_type, chat_name, message, success, error_text),
                )
    finally:
        conn.close()


def get_broadcast_log(limit: int = 50):
    """
    Возвращаем последние записи истории рассылок.
    """
    conn = get_db_conn()
    if conn is None:
        return []

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chat_id, chat_type, chat_name, message, success, error, sent_at
                      FROM broadcast_log
                  ORDER BY sent_at DESC
                     LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                result = []
                for r in rows:
                    result.append(
                        {
                            "chat_id": r[0],
                            "chat_type": r[1],
                            "chat_name": r[2],
                            "message": r[3],
                            "success": r[4],
                            "error": r[5],
                            "sent_at": r[6],
                        }
                    )
                return result
    finally:
        conn.close()


# Инициализация таблиц при старте
init_db()


# --- LLM-логика ---

async def ask_llm(chat_id: int, user_text: str) -> str:
    """
    Берём актуальные тезисы из БД, собираем историю и спрашиваем OpenAI.
    """
    if oa_client is None:
        raise RuntimeError("OpenAI клиент не инициализирован (нет OPENAI_API_KEY)")

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
    Раньше тут была автозапуск рассылки при старте воркера.
    Сейчас отключено — рассылка запускается вручную через /broadcast.
    """
    logger.info("Авторассылка при старте воркера отключена. Используй веб-кнопку /broadcast.")
    return


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


# --- Вспомогательное: чтение диалогов и рассылка синхронно через Telethon ---

def fetch_dialogs(limit: int = 50):
    """
    Получаем список диалогов (название + id) через синхронный клиент Telethon.
    """
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return []

    dialogs_data = []
    try:
        with SyncTelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH) as sync_client:
            for d in sync_client.iter_dialogs(limit=limit):
                if d.is_user:
                    d_type = "user"
                elif d.is_group:
                    d_type = "group"
                elif d.is_channel:
                    d_type = "channel"
                else:
                    d_type = "other"

                name = d.name or "(без названия)"
                dialogs_data.append({
                    "id": d.id,
                    "name": name,
                    "type": d_type,
                })
    except Exception as e:
        logger.exception("Ошибка при получении диалогов: %s", e)
    return dialogs_data


def run_broadcast_now():
    """
    Запускаем рассылку из веб-интерфейса:
      - читаем настройки из БД;
      - шлём сообщения через SyncTelegramClient;
      - пишем историю в broadcast_log;
      - возвращаем (total, ok, fail).
    """
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        raise RuntimeError("Нет Telegram-кредов, рассылка невозможна.")

    target_ids_str, start_msg = get_agent_settings()
    ids = parse_target_ids(target_ids_str)

    if not start_msg:
        raise RuntimeError("START_MESSAGE пустой — нечего рассылать.")
    if not ids:
        raise RuntimeError("TARGET_IDS пустой — не указано, кому слать.")

    total = len(ids)
    ok = 0
    fail = 0

    with SyncTelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH) as sync_client:
        for chat_id in ids:
            chat_name = ""
            chat_type = ""
            try:
                entity = sync_client.get_entity(chat_id)
                # определяем тип и имя
                try:
                    chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "") or "(без названия)"
                except Exception:
                    chat_name = "(без названия)"

                if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
                    chat_type = "group"
                elif getattr(entity, "broadcast", False):
                    chat_type = "channel"
                else:
                    chat_type = "user"

                sync_client.send_message(chat_id, start_msg)
                ok += 1
                log_broadcast(chat_id, chat_type, chat_name, start_msg, True, None)
                logger.info("Рассылка: успешно отправлено в %s (%s)", chat_id, chat_name)
            except Exception as e:
                fail += 1
                err_text = str(e)
                log_broadcast(chat_id, chat_type or "unknown", chat_name or "", start_msg, False, err_text)
                logger.exception("Рассылка: ошибка отправки в %s: %s", chat_id, e)

    return total, ok, fail


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
  <div style="max-width:860px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
      <div>
        <h1 style="margin:0 0 4px 0;font-size:24px;">Telegram AI Agent</h1>
        <div style="color:#9ca3af;font-size:14px;">Статус приложения и быстрые ссылки.</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <a href="{{ url_for('edit_prompt') }}" style="padding:6px 12px;border-radius:999px;border:1px solid #374151;color:#e5e7eb;text-decoration:none;font-size:13px;">✏️ Тезисы для ИИ</a>
        <a href="{{ url_for('settings_page') }}" style="padding:6px 12px;border-radius:999px;border:1px solid #374151;color:#e5e7eb;text-decoration:none;font-size:13px;">🎯 Цели рассылки</a>
        <a href="{{ url_for('dialogs_page') }}" style="padding:6px 12px;border-radius:999px;border:1px solid #374151;color:#e5e7eb;text-decoration:none;font-size:13px;">📚 Диалоги Telegram</a>
        <a href="{{ url_for('broadcast_page') }}" style="padding:6px 12px;border-radius:999px;border:1px solid #22c55e;color:#bbf7d0;text-decoration:none;font-size:13px;">▶️ Рассылка</a>
      </div>
    </div>

    <h3>Обязательные переменные</h3>
    <ul>
      <li>TG_API_ID: {{ 'ok' if has_tg_api_id else 'нет' }}</li>
      <li>TG_API_HASH: {{ 'ok' if has_tg_api_hash else 'нет' }}</li>
      <li>TG_SESSION: {{ 'ok' if has_tg_session else 'нет' }}</li>
      <li>OPENAI_API_KEY: {{ 'ok' if has_openai_key else 'нет' }}</li>
    </ul>

    <h3>Дополнительные (env по умолчанию)</h3>
    <ul>
      <li>TARGET_IDS (env): {{ target_ids_raw or 'пусто' }}</li>
      <li>START_MESSAGE (env): {{ 'задано' if start_message else 'пусто' }}</li>
      <li>SYSTEM_PROMPT (env): {{ 'задан' if system_prompt else 'по умолчанию' }}</li>
    </ul>

    <p style="font-size:13px;color:#9ca3af;">
      Реальные значения для стартовой рассылки и тезисов берутся из базы (страницы «Тезисы для ИИ» и «Цели рассылки»).<br>
      Worker обрабатывает входящие сообщения, а рассылка стартует вручную на странице «Рассылка».
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


SETTINGS_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Цели рассылки — Telegram Agent</title>
</head>
<body style="font-family: system-ui, -apple-system; background:#020617; color:#e5e7eb;">
  <div style="max-width:840px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <h1 style="margin-top:0;font-size:22px;">Цели рассылки и первое сообщение</h1>
    <p style="color:#9ca3af;font-size:14px;">
      Здесь ты задаёшь, <b>кому агент пишет первым</b> и какой текст отправляет при запуске рассылки.<br>
      Формат списка ID: <code>123456789,-1002222333444</code> (через запятую).
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
      <div style="margin-bottom:6px;font-size:13px;color:#9ca3af;">Список chat_id (юзеры, группы, каналы) через запятую:</div>
      <textarea name="target_ids" rows="3" style="width:100%;border-radius:12px;border:1px solid #374151;background:#020617;color:#e5e7eb;padding:10px;font-size:14px;resize:vertical;">{{ target_ids or "" }}</textarea>

      <div style="margin:12px 0 6px 0;font-size:13px;color:#9ca3af;">Текст первого сообщения (START_MESSAGE):</div>
      <textarea name="start_message" rows="5" style="width:100%;border-radius:12px;border:1px solid #374151;background:#020617;color:#e5e7eb;padding:10px;font-size:14px;resize:vertical;">{{ start_message or "" }}</textarea>

      <div style="margin-top:12px;display:flex;gap:12px;align-items:center;">
        <button type="submit" style="border:none;border-radius:999px;padding:8px 18px;background:#16a34a;color:#fff;font-size:14px;cursor:pointer;">
          💾 Сохранить настройки
        </button>
        <a href="{{ url_for('index') }}" style="font-size:13px;color:#9ca3af;text-decoration:none;">← Назад к статусу</a>
      </div>
    </form>

    <p style="margin-top:18px;font-size:13px;color:#9ca3af;">
      Чтобы увидеть названия групп и их ID, открой страницу «Диалоги Telegram».
    </p>
  </div>
</body>
</html>
"""


DIALOGS_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Диалоги Telegram — Telegram Agent</title>
</head>
<body style="font-family: system-ui, -apple-system; background:#020617; color:#e5e7eb;">
  <div style="max-width:880px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <h1 style="margin-top:0;font-size:22px;">Диалоги Telegram</h1>
    <p style="color:#9ca3af;font-size:14px;">
      Список последних диалогов аккаунта агента. Отсюда можно копировать <code>chat_id</code> и вставлять в «Цели рассылки».
    </p>

    {% if not has_creds %}
      <p style="color:#fecaca;font-size:14px;">
        TG_API_ID / TG_API_HASH / TG_SESSION не заданы — получить диалоги невозможно.
      </p>
    {% else %}
      {% if not dialogs %}
        <p style="color:#9ca3af;font-size:14px;">
          Диалоги не найдены или произошла ошибка при запросе. Попробуй позже или проверь логи.
        </p>
      {% else %}
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px;">
          <thead>
            <tr>
              <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Тип</th>
              <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Название</th>
              <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">chat_id</th>
            </tr>
          </thead>
          <tbody>
            {% for d in dialogs %}
              <tr>
                <td style="padding:6px;border-bottom:1px solid #111827;">{{ d.type }}</td>
                <td style="padding:6px;border-bottom:1px solid #111827;">{{ d.name }}</td>
                <td style="padding:6px;border-bottom:1px solid #111827;"><code>{{ d.id }}</code></td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% endif %}
    {% endif %}

    <p style="margin-top:18px;font-size:13px;color:#9ca3af;">
      После обновления целей рассылки используй страницу «Рассылка», чтобы отправить сообщения.
    </p>

    <p style="font-size:13px;">
      <a href="{{ url_for('index') }}" style="color:#9ca3af;text-decoration:none;">← Назад к статусу</a>
    </p>
  </div>
</body>
</html>
"""


BROADCAST_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Рассылка — Telegram Agent</title>
</head>
<body style="font-family: system-ui, -apple-system; background:#020617; color:#e5e7eb;">
  <div style="max-width:900px;margin:40px auto;padding:24px;border-radius:16px;background:#020617;border:1px solid #1f2937;">
    <h1 style="margin-top:0;font-size:22px;">Рассылка</h1>
    <p style="color:#9ca3af;font-size:14px;">
      Эта страница запускает рассылку по текущим настройкам (страница «Цели рассылки»).
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
      <p style="font-size:13px;color:#fbbf24;">
        Перед запуском убедись, что правильно заполнены <a href="{{ url_for('settings_page') }}" style="color:#93c5fd;">цели рассылки</a>.
      </p>
      <button type="submit" style="border:none;border-radius:999px;padding:10px 22px;background:#22c55e;color:#022c22;font-size:15px;cursor:pointer;">
        ▶️ Запустить рассылку сейчас
      </button>
    </form>

    <h2 style="margin-top:24px;font-size:18px;">История рассылок (последние {{ logs|length }})</h2>

    {% if not logs %}
      <p style="color:#9ca3af;font-size:14px;">Пока нет записей. Запусти первую рассылку.</p>
    {% else %}
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px;">
        <thead>
          <tr>
            <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Время</th>
            <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Тип</th>
            <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Чат</th>
            <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">chat_id</th>
            <th style="text-align:left;border-bottom:1px solid #1f2937;padding:6px;">Статус</th>
          </tr>
        </thead>
        <tbody>
          {% for r in logs %}
            <tr>
              <td style="padding:6px;border-bottom:1px solid #111827;">{{ r.sent_at }}</td>
              <td style="padding:6px;border-bottom:1px solid #111827;">{{ r.chat_type }}</td>
              <td style="padding:6px;border-bottom:1px solid #111827;">{{ r.chat_name }}</td>
              <td style="padding:6px;border-bottom:1px solid #111827;"><code>{{ r.chat_id }}</code></td>
              <td style="padding:6px;border-bottom:1px solid #111827;">
                {% if r.success %}
                  <span style="color:#4ade80;">успех</span>
                {% else %}
                  <span style="color:#fecaca;" title="{{ r.error or '' }}">ошибка</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}

    <p style="margin-top:18px;font-size:13px;color:#9ca3af;">
      Если какие-то отправки не прошли (ошибка), наведи курсор на «ошибка» чтобы увидеть текст.
    </p>

    <p style="font-size:13px;">
      <a href="{{ url_for('index') }}" style="color:#9ca3af;text-decoration:none;">← Назад к статусу</a>
    </p>
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


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        target_ids = request.form.get("target_ids", "").strip()
        start_message = request.form.get("start_message", "").strip()
        try:
            set_agent_settings(target_ids, start_message)
            flash("Настройки рассылки сохранены.")
        except Exception as e:
            logger.exception("Ошибка при сохранении настроек: %s", e)
            flash("Ошибка при сохранении настроек, смотри логи.")
        return redirect(url_for("settings_page"))

    ids, msg = get_agent_settings()
    return render_template_string(
        SETTINGS_HTML,
        target_ids=ids,
        start_message=msg,
    )


@app.route("/dialogs")
def dialogs_page():
    has_creds = bool(TG_API_ID and TG_API_HASH and TG_SESSION)
    dialogs = fetch_dialogs(limit=50) if has_creds else []
    return render_template_string(
        DIALOGS_HTML,
        dialogs=dialogs,
        has_creds=has_creds,
    )


@app.route("/broadcast", methods=["GET", "POST"])
def broadcast_page():
    if request.method == "POST":
        try:
            total, ok, fail = run_broadcast_now()
            flash(f"Рассылка запущена. Всего: {total}, успешно: {ok}, ошибок: {fail}.")
        except Exception as e:
            logger.exception("Ошибка при запуске рассылки: %s", e)
            flash(f"Ошибка при запуске рассылки: {e}")
        return redirect(url_for("broadcast_page"))

    logs = get_broadcast_log(limit=50)
    return render_template_string(BROADCAST_HTML, logs=logs)


if __name__ == "__main__":
    asyncio.run(main())
