import os
import io
import uuid
import json
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
import pypdf
from openai import OpenAI

app = Flask(__name__, template_folder=".")
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 МБ макс. размер файлов

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg2
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    def get_db():
        return psycopg2.connect(DATABASE_URL)
else:
    import sqlite3
    def get_db():
        conn = sqlite3.connect("school_platform.db")
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    if IS_POSTGRES:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'student',
                device_token TEXT,
                avatar TEXT,
                auth_token TEXT
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100) UNIQUE NOT NULL,
                username VARCHAR(100) NOT NULL,
                title VARCHAR(255) NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100) NOT NULL,
                username VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL,
                message TEXT,
                attachment_name TEXT,
                attachment_data TEXT,
                is_image BOOLEAN DEFAULT FALSE,
                time_str VARCHAR(50)
            );
            CREATE TABLE IF NOT EXISTS homework (
                id SERIAL PRIMARY KEY,
                subject VARCHAR(100) NOT NULL,
                task TEXT NOT NULL,
                deadline VARCHAR(100) NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(255) NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_chat (
                id SERIAL PRIMARY KEY,
                sender VARCHAR(100) NOT NULL,
                avatar TEXT,
                message TEXT,
                time_str VARCHAR(50),
                attachment_name TEXT,
                attachment_data TEXT,
                is_image BOOLEAN DEFAULT FALSE
            );
        """)
        c.execute("""
            INSERT INTO users (username, password, role) 
            VALUES ('dudo', 'dudo_2026', 'admin')
            ON CONFLICT (username) DO NOTHING;

            INSERT INTO settings (key, value) VALUES 
            ('banner', 'Добро пожаловать в закрытую платформу 8 «Б»!'),
            ('system_prompt', 'Ты личный наставник 8 «Б» класса. Помогай решать задачи пошагово, объясняй формулы и логику, не давай прямое списывание.'),
            ('facts', 'По физике всегда обязательно писать единицы СИ в графе Дано.'),
            ('ai_base_url', 'https://api.openai.com/v1'),
            ('ai_model_id', 'gpt-4o-mini'),
            ('ai_api_key', '')
            ON CONFLICT (key) DO NOTHING;
        """)
        conn.commit()
    else:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                device_token TEXT,
                avatar TEXT,
                auth_token TEXT
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                message TEXT,
                attachment_name TEXT,
                attachment_data TEXT,
                is_image INTEGER DEFAULT 0,
                time_str TEXT
            );
            CREATE TABLE IF NOT EXISTS homework (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                task TEXT NOT NULL,
                deadline TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                avatar TEXT,
                message TEXT,
                time_str TEXT,
                attachment_name TEXT,
                attachment_data TEXT,
                is_image INTEGER DEFAULT 0
            );
            INSERT OR IGNORE INTO users (username, password, role) VALUES ('dudo', 'dudo_2026', 'admin');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('banner', 'Добро пожаловать в закрытую платформу 8 «Б»!');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('system_prompt', 'Ты личный наставник 8 «Б» класса.');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('facts', 'По физике пишем единицы СИ.');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_base_url', 'https://api.openai.com/v1');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_model_id', 'gpt-4o-mini');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_api_key', '');
        """)
        conn.commit()

    conn.close()

init_db()

def get_msk_time():
    msk_tz = timezone(timedelta(hours=3))
    now = datetime.now(msk_tz)
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return {
        "day": days[now.weekday()],
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M")
    }

def get_setting(key, default=""):
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT value FROM settings WHERE key={ph}", (key,))
    res = c.fetchone()
    conn.close()
    return res[0] if (res and res[0] is not None) else default

def verify_admin(token):
    if not token:
        return False
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT role, username FROM users WHERE auth_token={ph}", (token,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0] == 'admin' and row[1] == 'dudo')

# Функция извлечения текста из прикрепленного файла
def extract_text_from_attachment(att_data, att_name):
    if not att_data or not att_name:
        return ""
    try:
        if "," in att_data:
            base64_str = att_data.split(",", 1)[1]
        else:
            base64_str = att_data
        raw_bytes = base64.b64decode(base64_str)

        if att_name.lower().endswith(".pdf"):
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
            return text.strip()
        else:
            return raw_bytes.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        return f"[Не удалось прочитать содержимое файла: {str(e)}]"

# --- РОУТЫ ---

@app.route("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return render_template_string(f.read())

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    device_token = data.get("device_token", "")

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT id, username, role, device_token, avatar FROM users WHERE username={ph} AND password={ph}", 
              (username, password))
    user = c.fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "error": "Неверный логин или пароль."}), 401

    user_id, uname, role, bound_token, avatar = user[0], user[1], user[2], user[3], user[4]
    session_token = uuid.uuid4().hex

    if role != 'admin':
        if bound_token is None:
            c.execute(f"UPDATE users SET device_token={ph}, auth_token={ph} WHERE id={ph}", (device_token, session_token, user_id))
            conn.commit()
        elif bound_token != device_token:
            conn.close()
            return jsonify({
                "success": False, 
                "error": "❌ Доступ заблокирован! Этот аккаунт уже привязан к другому телефону. Обратись к dudo для сброса."
            }), 403
        else:
            c.execute(f"UPDATE users SET auth_token={ph} WHERE id={ph}", (session_token, user_id))
            conn.commit()
    else:
        c.execute(f"UPDATE users SET auth_token={ph} WHERE id={ph}", (session_token, user_id))
        conn.commit()

    conn.close()
    return jsonify({
        "success": True, 
        "username": uname, 
        "role": role,
        "avatar": avatar or "",
        "token": session_token
    })

@app.route("/api/profile/update", methods=["POST"])
def update_profile():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    new_name = data.get("new_name", "").strip().lower()
    avatar = data.get("avatar", "")

    if not new_name:
        return jsonify({"success": False, "error": "Имя не может быть пустым"}), 400

    if new_name == 'dudo' and username != 'dudo':
        return jsonify({"success": False, "error": "Имя dudo зарезервировано администратором"}), 403

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"UPDATE users SET username={ph}, avatar={ph} WHERE username={ph}", (new_name, avatar, username))
    c.execute(f"UPDATE chat_sessions SET username={ph} WHERE username={ph}", (new_name, username))
    c.execute(f"UPDATE ai_chat_history SET username={ph} WHERE username={ph}", (new_name, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "username": new_name, "avatar": avatar})

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, subject, task, deadline FROM homework ORDER BY id DESC")
    hw = c.fetchall()
    conn.close()

    return jsonify({
        "time": get_msk_time(),
        "banner": get_setting("banner"),
        "homework": [{"id": h[0], "subject": h[1], "task": h[2], "deadline": h[3]} for h in hw]
    })

# --- УПРАВЛЕНИЕ СЕССИЯМИ ЧАТОВ (КАК В CHATGPT/GEMINI) ---

@app.route("/api/chats/list", methods=["POST"])
def list_chats():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    if not username:
        return jsonify([])

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT session_id, title FROM chat_sessions WHERE username={ph} ORDER BY id DESC", (username,))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"session_id": r[0], "title": r[1]} for r in rows])

@app.route("/api/chats/new", methods=["POST"])
def create_new_chat():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    if not username:
        return jsonify({"error": "No user"}), 400

    new_session_id = uuid.uuid4().hex[:12]
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"INSERT INTO chat_sessions (session_id, username, title) VALUES ({ph}, {ph}, 'Новый чат')", 
              (new_session_id, username))
    conn.commit()
    conn.close()
    return jsonify({"session_id": new_session_id, "title": "Новый чат"})

@app.route("/api/chats/rename", methods=["POST"])
def rename_chat():
    data = request.json or {}
    session_id = data.get("session_id")
    title = data.get("title", "").strip() or "Без названия"
    username = data.get("username", "").strip().lower()

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"UPDATE chat_sessions SET title={ph} WHERE session_id={ph} AND username={ph}", 
              (title[:50], session_id, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/chats/delete", methods=["POST"])
def delete_chat():
    data = request.json or {}
    session_id = data.get("session_id")
    username = data.get("username", "").strip().lower()

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"DELETE FROM chat_sessions WHERE session_id={ph} AND username={ph}", (session_id, username))
    c.execute(f"DELETE FROM ai_chat_history WHERE session_id={ph}", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/chats/clear", methods=["POST"])
def clear_chat_history():
    data = request.json or {}
    session_id = data.get("session_id")
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"DELETE FROM ai_chat_history WHERE session_id={ph}", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/chat/history", methods=["POST"])
def get_chat_history():
    data = request.json or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify([])

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"""
        SELECT role, message, attachment_name, attachment_data, is_image, time_str 
        FROM ai_chat_history 
        WHERE session_id={ph} 
        ORDER BY id ASC
    """, (session_id,))
    rows = c.fetchall()
    conn.close()

    return jsonify([{
        "role": r[0],
        "message": r[1],
        "attachment_name": r[2],
        "attachment_data": r[3],
        "is_image": bool(r[4]),
        "time": r[5]
    } for r in rows])

# --- ПОТОКОВЫЙ ВЫЗОВ ИИ (С ПОДДЕРЖКОЙ ФОТО И ФАЙЛОВ) ---
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_msg = data.get("message", "").strip()
    username = data.get("username", "Ученик").strip().lower()
    session_id = data.get("session_id")
    att_name = data.get("attachment_name")
    att_data = data.get("attachment_data")
    is_img = bool(data.get("is_image", False))
    t = get_msk_time()

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"

    # Если сессии нет, создаем
    if not session_id:
        session_id = uuid.uuid4().hex[:12]
        c.execute(f"INSERT INTO chat_sessions (session_id, username, title) VALUES ({ph}, {ph}, 'Новый чат')", 
                  (session_id, username))
        conn.commit()

    # Проверяем, нужно ли дать автоматическое название чату
    c.execute(f"SELECT title FROM chat_sessions WHERE session_id={ph}", (session_id,))
    s_row = c.fetchone()
    if s_row and s_row[0] == "Новый чат":
        auto_title = (user_msg or att_name or "Диалог")[:28]
        c.execute(f"UPDATE chat_sessions SET title={ph} WHERE session_id={ph}", (auto_title, session_id))
        conn.commit()

    # Сохраняем сообщение пользователя в БД
    c.execute(f"""
        INSERT INTO ai_chat_history (session_id, username, role, message, attachment_name, attachment_data, is_image, time_str)
        VALUES ({ph}, {ph}, 'user', {ph}, {ph}, {ph}, {ph}, {ph})
    """, (session_id, username, user_msg, att_name, att_data, is_img, t["time"]))
    conn.commit()

    # Получаем актуальные ДЗ и документы
    c.execute("SELECT subject, task, deadline FROM homework")
    hw_rows = c.fetchall()
    c.execute("SELECT filename, content FROM documents")
    doc_rows = c.fetchall()
    conn.close()

    hw_list_str = "\n".join([f"- {h[0]}: {h[1]} (Сдать до: {h[2]})" for h in hw_rows]) if hw_rows else "Заданий в базе нет."
    docs_str = "\n".join([f"[{d[0]}]: {d[1][:500]}" for d in doc_rows])

    system_instruction = f"""
{get_setting('system_prompt')}

РЕАЛЬНЫЕ ДАННЫЕ В РЕАЛЬНОМ ВРЕМЕНИ:
- Точное текущее время (МСК, Москва): {t['day']}, {t['date']}, время: {t['time']}.
- Имя ученика: {username}.
- Заметки и подсказки об учителях: {get_setting('facts')}.

АКТУАЛЬНАЯ БАЗА ДОМАШНИХ ЗАДАНИЙ 8 «Б» КЛАССА:
{hw_list_str}

МАТЕРИАЛЫ ИЗ УЧЕБНИКОВ:
{docs_str}

ИНСТРУКЦИИ:
1. Если ученик спрашивает «какая домашка?», «что задали?» — бери информацию ТОЛЬКО из базы выше. Учитывай день недели ({t['day']}) и дедлайны.
2. Не давай сразу тупой готовый ответ, объясняй формулы и ход мыслей пошагово.
3. Если к сообщению прикреплен текст файла — внимательно изучи его и отвечай строго по его содержимому.
"""

    base_url = os.environ.get("AI_BASE_URL") or get_setting("ai_base_url", "https://api.openai.com/v1")
    api_key = os.environ.get("AI_API_KEY") or get_setting("ai_api_key", "")
    model_id = os.environ.get("AI_MODEL_ID") or get_setting("ai_model_id", "gpt-4o-mini")

    def event_stream():
        if not api_key:
            err = "⚠️ API-ключ не настроен! Администратор (dudo) должен указать Base URL, Model ID и API Key во вкладке «АДМИНКА»."
            yield f"data: {json.dumps({'error': err})}\n\n"
            yield "data: [DONE]\n\n"
            return

        full_bot_reply = ""
        try:
            client = OpenAI(base_url=base_url.rstrip("/"), api_key=api_key)

            # Обработка фото (Vision) или файлов (PDF/TXT)
            if att_data and is_img:
                # Для изображений: передаем URL с base64 (сжатым на фронте)
                user_content = [
                    {"type": "text", "text": user_msg or "Пожалуйста, посмотри на это фото и помоги с решением задачи."},
                    {"type": "image_url", "image_url": {"url": att_data}}
                ]
            elif att_data and not is_img:
                # Для документов: извлекаем текст и передаем модели
                file_text = extract_text_from_attachment(att_data, att_name)
                user_content = f"{user_msg}\n\n[СОДЕРЖИМОЕ ПРИКРЕПЛЕННОГО ФАЙЛА «{att_name}»]:\n{file_text}\n[КОНЕЦ ФАЙЛА]\n"
            else:
                user_content = user_msg

            stream = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content}
                ],
                stream=True,
                temperature=0.7,
                max_tokens=1500
            )

            for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        full_bot_reply += delta
                        yield f"data: {json.dumps({'content': delta, 'session_id': session_id})}\n\n"

            # Сохраняем ответ ИИ в историю
            if full_bot_reply:
                s_conn = get_db()
                sc = s_conn.cursor()
                sc.execute(f"""
                    INSERT INTO ai_chat_history (session_id, username, role, message, time_str)
                    VALUES ({ph}, {ph}, 'bot', {ph}, {ph})
                """, (session_id, username, full_bot_reply, t["time"]))
                s_conn.commit()
                s_conn.close()

            yield "data: [DONE]\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

# --- ГРУППОВОЙ ЧАТ ---
@app.route("/api/group/messages", methods=["GET"])
def group_messages():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT sender, avatar, message, time_str, attachment_name, attachment_data, is_image FROM group_chat ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()

    return jsonify([{
        "sender": r[0], "avatar": r[1], "text": r[2], "time": r[3],
        "attachment_name": r[4], "attachment_data": r[5], "is_image": bool(r[6])
    } for r in rows])

@app.route("/api/group/send", methods=["POST"])
def group_send():
    data = request.json or {}
    sender = data.get("sender", "Ученик")
    avatar = data.get("avatar", "")
    msg = data.get("message", "")
    att_name = data.get("attachment_name")
    att_data = data.get("attachment_data")
    is_img = bool(data.get("is_image", False))
    time_str = get_msk_time()["time"]

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"""
        INSERT INTO group_chat (sender, avatar, message, time_str, attachment_name, attachment_data, is_image)
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
    """, (sender, avatar, msg, time_str, att_name, att_data, is_img))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "time": time_str})

# --- АДМИНКА (СТРОГО ДЛЯ DUDO) ---
@app.route("/api/admin/data", methods=["POST"])
def admin_data():
    token = (request.json or {}).get("token")
    if not verify_admin(token):
        return jsonify({"error": "Доступ запрещен. Только для dudo."}), 403

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, device_token FROM users WHERE role='student'")
    users = c.fetchall()
    c.execute("SELECT id, filename FROM documents")
    docs = c.fetchall()
    conn.close()

    return jsonify({
        "users": [{"id": u[0], "username": u[1], "is_locked": bool(u[2])} for u in users],
        "documents": [{"id": d[0], "filename": d[1]} for d in docs],
        "banner": get_setting("banner"),
        "system_prompt": get_setting("system_prompt"),
        "facts": get_setting("facts"),
        "ai_base_url": get_setting("ai_base_url", "https://api.openai.com/v1"),
        "ai_model_id": get_setting("ai_model_id", "gpt-4o-mini"),
        "ai_api_key": get_setting("ai_api_key", "")
    })

@app.route("/api/admin/user_chats", methods=["POST"])
def admin_user_chats():
    data = request.json or {}
    token = data.get("token")
    target_user = data.get("target_user", "").strip().lower()
    if not verify_admin(token):
        return jsonify({"error": "Доступ запрещен"}), 403

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT session_id, title FROM chat_sessions WHERE username={ph} ORDER BY id DESC", (target_user,))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"session_id": r[0], "title": r[1]} for r in rows])

@app.route("/api/admin/user_chat_messages", methods=["POST"])
def admin_user_chat_messages():
    data = request.json or {}
    token = data.get("token")
    session_id = data.get("session_id")
    if not verify_admin(token):
        return jsonify({"error": "Доступ запрещен"}), 403

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"""
        SELECT role, message, attachment_name, attachment_data, is_image, time_str 
        FROM ai_chat_history 
        WHERE session_id={ph} 
        ORDER BY id ASC
    """, (session_id,))
    rows = c.fetchall()
    conn.close()

    return jsonify([{
        "role": r[0], "message": r[1], "attachment_name": r[2], 
        "attachment_data": r[3], "is_image": bool(r[4]), "time": r[5]
    } for r in rows])

@app.route("/api/admin/reset_device", methods=["POST"])
def reset_device():
    data = request.json or {}
    if not verify_admin(data.get("token")):
        return jsonify({"error": "Доступ запрещен"}), 403

    uid = data.get("user_id")
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"UPDATE users SET device_token=NULL, auth_token=NULL WHERE id={ph}", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/save_hw", methods=["POST"])
def save_hw():
    data = request.json or {}
    if not verify_admin(data.get("token")):
        return jsonify({"error": "Доступ запрещен"}), 403

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"INSERT INTO homework (subject, task, deadline) VALUES ({ph}, {ph}, {ph})", 
              (data['subject'], data['task'], data['deadline']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/save_settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    if not verify_admin(data.get("token")):
        return jsonify({"error": "Доступ запрещен"}), 403

    settings_map = {
        'banner': data.get("banner", ""),
        'system_prompt': data.get("system_prompt", ""),
        'facts': data.get("facts", ""),
        'ai_base_url': data.get("ai_base_url", "https://api.openai.com/v1"),
        'ai_model_id': data.get("ai_model_id", "gpt-4o-mini"),
        'ai_api_key': data.get("ai_api_key", "")
    }

    conn = get_db()
    c = conn.cursor()
    for k, v in settings_map.items():
        if IS_POSTGRES:
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (k, v))
        else:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/upload_doc", methods=["POST"])
def upload_doc():
    token = request.form.get("token")
    if not verify_admin(token):
        return jsonify({"error": "Доступ запрещен"}), 403

    if 'file' not in request.files:
        return jsonify({"success": False}), 400
    file = request.files['file']
    filename = file.filename
    content = ""
    if filename.endswith(".pdf"):
        reader = pypdf.PdfReader(io.BytesIO(file.read()))
        for p in reader.pages:
            content += (p.extract_text() or "") + "\n"
    else:
        content = file.read().decode("utf-8", errors="ignore")

    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"INSERT INTO documents (filename, content) VALUES ({ph}, {ph})", (filename, content[:25000]))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
