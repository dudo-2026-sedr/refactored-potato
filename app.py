import os
import io
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string
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
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                device_token TEXT,
                avatar TEXT,
                auth_token TEXT
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
        """)
        c.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES ('dudo', 'dudo_2026', 'admin');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('banner', 'Добро пожаловать в закрытую платформу 8 «Б»!');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('system_prompt', 'Ты личный наставник 8 «Б» класса. Помогай решать задачи пошагово.');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('facts', 'По физике пишем единицы СИ.');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_base_url', 'https://api.openai.com/v1');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_model_id', 'gpt-4o-mini');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_api_key', '');")

    conn.commit()
    conn.close()

init_db()

# ТОЧНОЕ МОСКОВСКОЕ ВРЕМЯ (МСК, UTC+3)
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

    # Device Lock (dudo входит без ограничений)
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

# --- ОСНОВНОЙ ВЫЗОВ ИИ (OPENAI COMPATIBLE) ---
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_msg = data.get("message", "")
    username = data.get("username", "Ученик")
    att_data = data.get("attachment_data")
    is_img = data.get("is_image", False)
    t = get_msk_time()

    # 1. Получаем базу заданий и конспектов
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT subject, task, deadline FROM homework")
    hw_rows = c.fetchall()
    c.execute("SELECT filename, content FROM documents")
    doc_rows = c.fetchall()
    conn.close()

    if hw_rows:
        hw_list_str = "\n".join([f"- {h[0]}: {h[1]} (Сдать до: {h[2]})" for h in hw_rows])
    else:
        hw_list_str = "Заданий пока нет."

    docs_str = "\n".join([f"[{d[0]}]: {d[1][:500]}" for d in doc_rows])

    # 2. Формируем подробный системный контекст
    system_instruction = f"""
{get_setting('system_prompt')}

РЕАЛЬНЫЕ ДАННЫЕ В РЕАЛЬНОМ ВРЕМЕНИ:
- Точное текущее время (МСК, Москва): {t['day']}, {t['date']}, время: {t['time']}.
- Имя ученика: {username}.
- Заметки и правила учителей: {get_setting('facts')}.

АКТУАЛЬНАЯ БАЗА ДОМАШНИХ ЗАДАНИЙ 8 «Б» КЛАССА:
{hw_list_str}

МАТЕРИАЛЫ И КОНСПЕКТЫ ИЗ УЧЕБНИКОВ:
{docs_str}

ИНСТРУКЦИИ ПО ОТВЕТАМ:
1. Если ученик спрашивает «какая домашка?», «что задали?», «что по физике/алгебре?» — бери информацию ТОЛЬКО из списка актуальной базы выше. Учитывай сегодняшний день недели ({t['day']}) и дедлайны!
2. Не придумывай домашку от себя, которой нет в базе. Если задания по предмету нет — так и скажи: «По этому предмету задания в базе пока нет».
3. Отвечай дружелюбно, понятно для ученика 8 класса, помогай разбирать задачи пошагово.
"""

    # 3. Настройки подключения к ИИ
    base_url = os.environ.get("AI_BASE_URL") or get_setting("ai_base_url", "https://api.openai.com/v1")
    api_key = os.environ.get("AI_API_KEY") or get_setting("ai_api_key", "")
    model_id = os.environ.get("AI_MODEL_ID") or get_setting("ai_model_id", "gpt-4o-mini")

    if not api_key:
        return jsonify({
            "reply": "⚠️ API-ключ для нейросети еще не настроен! Администратор (dudo) должен указать Base URL, Model ID и API Key во вкладке «АДМИНКА»."
        })

    try:
        client = OpenAI(base_url=base_url.rstrip("/"), api_key=api_key)

        # Формируем сообщение пользователя (с поддержкой Vision для фото задач)
        if att_data and is_img:
            user_content = [
                {"type": "text", "text": user_msg or "Помоги с этой задачей на фото."},
                {"type": "image_url", "image_url": {"url": att_data}}
            ]
        else:
            user_content = user_msg

        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content}
            ],
            temperature=0.7,
            max_tokens=1500
        )
        bot_reply = response.choices[0].message.content
        return jsonify({"reply": bot_reply})

    except Exception as e:
        return jsonify({
            "reply": f"❌ Ошибка вызова нейросети: {str(e)}\n\nПроверьте правильность Base URL, API Key и Model ID в панели администратора."
        })

# --- ГРУППОВОЙ ЧАТ ---
@app.route("/api/group/messages", methods=["GET"])
def group_messages():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT sender, avatar, message, time_str, attachment_name, attachment_data, is_image FROM group_chat ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()

    return jsonify([{
        "sender": r[0],
        "avatar": r[1],
        "text": r[2],
        "time": r[3],
        "attachment_name": r[4],
        "attachment_data": r[5],
        "is_image": bool(r[6])
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
        return jsonify({"error": "Доступ запрещен. Только для администратора dudo."}), 403

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
