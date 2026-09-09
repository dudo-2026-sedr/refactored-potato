import os
import io
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
import pypdf

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
        # Создаем dudo с правами админа
        c.execute("""
            INSERT INTO users (username, password, role) 
            VALUES ('dudo', 'dudo_2026', 'admin')
            ON CONFLICT (username) DO NOTHING;

            INSERT INTO users (username, password, role) 
            VALUES ('артем', '1234', 'student')
            ON CONFLICT (username) DO NOTHING;

            INSERT INTO settings (key, value) VALUES 
            ('banner', 'Добро пожаловать в закрытую платформу 8 «Б»!'),
            ('system_prompt', 'Ты личный наставник 8 «Б» класса. Помогай решать задачи пошагово, объясняй формулы, не давай прямое списывание.'),
            ('facts', 'Учительница по физике снижает балл за отсутствие единиц СИ в Дано.')
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
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                task TEXT NOT NULL,
                deadline TEXT NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                content TEXT NOT NULL
            );
        """)
        c.execute("""
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
        c.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES ('артем', '1234', 'student');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('banner', 'Добро пожаловать в закрытую платформу 8 «Б»!');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('system_prompt', 'Ты наставник 8 «Б». Помогай пошагово.');")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('facts', 'По физике пишем единицы СИ.');")

    conn.commit()
    conn.close()

init_db()

def get_time_data():
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    now = datetime.now()
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
    return res[0] if res else default

# ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА (СТРОГО ТОЛЬКО ДЛЯ DUDO)
def verify_admin(token):
    if not token:
        return False
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"SELECT role, username FROM users WHERE auth_token={ph}", (token,))
    row = c.fetchone()
    conn.close()
    if row and row[0] == 'admin' and row[1] == 'dudo':
        return True
    return False

# --- МАРШРУТЫ ---

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
        return jsonify({"success": False, "error": "Неверное имя или пароль."}), 401

    user_id, uname, role, bound_token, avatar = user[0], user[1], user[2], user[3], user[4]

    # Генерация сессионного auth_token
    session_token = uuid.uuid4().hex

    # Device Lock: для dudo ограничений нет. Для учеников блокирует чужие устройства.
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

    # Защита: нельзя присвоить себе ник dudo
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
        "time": get_time_data(),
        "banner": get_setting("banner"),
        "homework": [{"id": h[0], "subject": h[1], "task": h[2], "deadline": h[3]} for h in hw]
    })

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_msg = data.get("message", "")
    username = data.get("username", "Ученик")
    attachment_name = data.get("attachment_name")
    t = get_time_data()

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT subject, task FROM homework")
    hw = c.fetchall()
    c.execute("SELECT filename, content FROM documents")
    docs = c.fetchall()
    conn.close()

    hw_text = "; ".join([f"{s[0]}: {s[1]}" for s in hw])
    docs_text = "\n".join([f"[{d[0]}]: {d[1][:500]}" for d in docs])

    system_prompt = f"""
    {get_setting('system_prompt')}
    Сегодня: {t['day']}, {t['date']}, {t['time']}.
    Ученик: {username}.
    Актуальные ДЗ: {hw_text}.
    Инсайды: {get_setting('facts')}.
    Конспекты: {docs_text}.
    """

    reply = f"Привет, {username.capitalize()}! Сейчас {t['day']} ({t['time']}). "
    if attachment_name:
        reply += f"Я изучил прикрепленный файл «{attachment_name}». "
    reply += f"Давай разберем задачу по шагам. Что тебе известно по условию?"

    return jsonify({"reply": reply})

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
    time_str = get_time_data()["time"]

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

# --- СТРОГО ЗАЩИЩЕННЫЕ МАРШРУТЫ АДМИНА (ТОЛЬКО DUDO) ---

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
        "facts": get_setting("facts")
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

@app.route("/api/admin/delete_hw", methods=["POST"])
def delete_hw():
    data = request.json or {}
    if not verify_admin(data.get("token")):
        return jsonify({"error": "Доступ запрещен"}), 403

    hid = data.get("id")
    conn = get_db()
    c = conn.cursor()
    ph = "%s" if IS_POSTGRES else "?"
    c.execute(f"DELETE FROM homework WHERE id={ph}", (hid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/admin/save_settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    if not verify_admin(data.get("token")):
        return jsonify({"error": "Доступ запрещен"}), 403

    banner = data.get("banner", "")
    prompt = data.get("system_prompt", "")
    facts = data.get("facts", "")

    conn = get_db()
    c = conn.cursor()
    if IS_POSTGRES:
        c.execute("INSERT INTO settings (key, value) VALUES ('banner', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (banner,))
        c.execute("INSERT INTO settings (key, value) VALUES ('system_prompt', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (prompt,))
        c.execute("INSERT INTO settings (key, value) VALUES ('facts', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (facts,))
    else:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('banner', ?)", (banner,))
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('system_prompt', ?)", (prompt,))
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('facts', ?)", (facts,))
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
