import os
import sys
import json
import uuid
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template, Response
from openai import OpenAI

# Попытка подключения PostgreSQL (Railway), иначе используем встроенный SQLite
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

import sqlite3

app = Flask(__name__, template_folder='.')
app.config['JSON_AS_ASCII'] = False

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL and psycopg2)

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db():
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect("school.db", timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

def execute_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    conn = get_db()
    try:
        if IS_POSTGRES:
            query = query.replace("?", "%s")
            with conn.cursor() as cur:
                cur.execute(query, params)
                if commit:
                    conn.commit()
                if fetchone:
                    res = cur.fetchone()
                    return dict(res) if res else None
                if fetchall:
                    res = cur.fetchall()
                    return [dict(r) for r in res] if res else []
        else:
            cur = conn.cursor()
            cur.execute(query, params)
            if commit:
                conn.commit()
            if fetchone:
                res = cur.fetchone()
                return dict(res) if res else None
            if fetchall:
                res = cur.fetchall()
                return [dict(r) for r in res] if res else []
    finally:
        conn.close()

def init_db():
    auto_inc = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    # 1. Таблица настроек
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """, commit=True)

    # 2. Таблица пользователей
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {auto_inc},
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'student',
            device_token TEXT,
            avatar TEXT,
            token TEXT
        )
    """, commit=True)

    # 3. Таблица сессий чатов
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)

    # 4. Таблица сообщений ИИ-чата
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS chat_history (
            id {auto_inc},
            session_id TEXT,
            username TEXT,
            role TEXT,
            message TEXT,
            attachment_name TEXT,
            attachment_data TEXT,
            is_image BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)

    # 5. Таблица классного чата
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS group_messages (
            id {auto_inc},
            sender TEXT,
            avatar TEXT,
            text TEXT,
            attachment_name TEXT,
            attachment_data TEXT,
            is_image BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)

    # 6. Таблица домашнего задания
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS homework (
            id {auto_inc},
            subject TEXT,
            task TEXT,
            deadline TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)

    # 7. Таблица базы знаний
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS knowledge_files (
            id {auto_inc},
            filename TEXT,
            extracted_text TEXT,
            file_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """, commit=True)

    # Дефолтный админ dudo
    admin = execute_query("SELECT * FROM users WHERE LOWER(username) = 'dudo'", fetchone=True)
    if not admin:
        admin_token = str(uuid.uuid4())
        execute_query("""
            INSERT INTO users (username, password, role, token) 
            VALUES ('dudo', 'admin', 'admin', ?)
        """, (admin_token,), commit=True)

    # Настройки по умолчанию
    default_settings = {
        'system_prompt': 'Ты — персональный репетитор и наставник для учеников 8 «Б» класса. Помогай с домашними заданиями, объясняй школьные темы доступно и понятно, разбирай формулы по шагам. Отвечай всегда строго на русском языке, формулы оформляй в стандартном LaTeX формате ($...$ или $$...$$).',
        'facts': 'Класс: 8 «Б». Программа углубленная.',
        'banner': 'Добро пожаловать в рабочую платформу 8 «Б» класса!',
        'ai_base_url': 'https://api.openai.com/v1',
        'ai_model_id': 'glm-5.3-flash',
        'ai_api_key': ''
    }
    for k, v in default_settings.items():
        exists = execute_query("SELECT value FROM settings WHERE key = ?", (k,), fetchone=True)
        if not exists:
            execute_query("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v), commit=True)

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_setting(key, default=''):
    r = execute_query("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)
    return r['value'] if r and r['value'] else default

def get_msk_time():
    now_msk = datetime.now(timezone(timedelta(hours=3)))
    days = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
    return {
        'day': days[now_msk.weekday()],
        'time': now_msk.strftime('%H:%M')
    }

def verify_admin(token):
    if not token:
        return False
    u = execute_query("SELECT * FROM users WHERE token = ? AND role = 'admin' AND LOWER(username) = 'dudo'", (token,), fetchone=True)
    return bool(u)

# --- ГЛАВНАЯ СТРАНИЦА ---
@app.route('/')
def index():
    return render_template('index.html')

# --- АУТЕНТИФИКАЦИЯ И ПРОФИЛЬ ---
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    device_token = data.get('device_token', '').strip()

    user = execute_query("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if not user:
        return jsonify({'success': False, 'error': 'Пользователь не найден'})

    if user['password'] != password:
        return jsonify({'success': False, 'error': 'Неверный пароль'})

    if user['device_token'] and user['device_token'] != device_token:
        return jsonify({'success': False, 'error': 'Аккаунт уже привязан к другому телефону. Обратитесь к dudo для сброса.'})

    if not user['device_token'] and device_token:
        execute_query("UPDATE users SET device_token = ? WHERE id = ?", (device_token, user['id']), commit=True)

    if not user.get('token'):
        new_token = str(uuid.uuid4())
        execute_query("UPDATE users SET token = ? WHERE id = ?", (new_token, user['id']), commit=True)
        user['token'] = new_token

    return jsonify({
        'success': True,
        'username': user['username'],
        'role': user['role'],
        'avatar': user.get('avatar'),
        'token': user['token']
    })

@app.route('/api/profile/update', methods=['POST'])
def api_profile_update():
    data = request.get_json() or {}
    username = data.get('username')
    new_name = data.get('new_name', '').strip()
    avatar = data.get('avatar')

    if not username or not new_name:
        return jsonify({'success': False, 'error': 'Имя не может быть пустым'})

    execute_query("UPDATE users SET username = ?, avatar = ? WHERE LOWER(username) = LOWER(?)", 
                  (new_name, avatar, username), commit=True)
    return jsonify({'success': True})

@app.route('/api/dashboard', methods=['GET'])
def api_dashboard():
    time_info = get_msk_time()
    banner = get_setting('banner', '8-B CORE PLATFORM')
    hw = execute_query("SELECT id, subject, task, deadline FROM homework ORDER BY id DESC", fetchall=True)
    return jsonify({
        'time': time_info,
        'banner': banner,
        'homework': hw
    })

# --- УПРАВЛЕНИЕ СЕССИЯМИ ДИАЛОГОВ (DRAWER) ---
@app.route('/api/chats/list', methods=['POST'])
def api_chats_list():
    data = request.get_json() or {}
    username = data.get('username')
    chats = execute_query("""
        SELECT session_id, title, updated_at 
        FROM chat_sessions 
        WHERE LOWER(username) = LOWER(?) 
        ORDER BY updated_at DESC
    """, (username,), fetchall=True)
    return jsonify(chats)

@app.route('/api/chats/new', methods=['POST'])
def api_chats_new():
    data = request.get_json() or {}
    username = data.get('username')
    new_id = str(uuid.uuid4())
    title = "Новый чат"
    execute_query("""
        INSERT INTO chat_sessions (session_id, username, title) 
        VALUES (?, ?, ?)
    """, (new_id, username, title), commit=True)
    return jsonify({'session_id': new_id, 'title': title})

@app.route('/api/chats/rename', methods=['POST'])
def api_chats_rename():
    data = request.get_json() or {}
    session_id = data.get('session_id')
    title = data.get('title', '').strip()
    if session_id and title:
        execute_query("UPDATE chat_sessions SET title = ? WHERE session_id = ?", (title, session_id), commit=True)
    return jsonify({'success': True})

@app.route('/api/chats/delete', methods=['POST'])
def api_chats_delete():
    data = request.get_json() or {}
    session_id = data.get('session_id')
    if session_id:
        execute_query("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,), commit=True)
        execute_query("DELETE FROM chat_history WHERE session_id = ?", (session_id,), commit=True)
    return jsonify({'success': True})

@app.route('/api/chats/clear', methods=['POST'])
def api_chats_clear():
    data = request.get_json() or {}
    session_id = data.get('session_id')
    if session_id:
        execute_query("DELETE FROM chat_history WHERE session_id = ?", (session_id,), commit=True)
    return jsonify({'success': True})

@app.route('/api/chat/history', methods=['POST'])
def api_chat_history():
    data = request.get_json() or {}
    session_id = data.get('session_id')
    if not session_id:
        return jsonify([])
    history = execute_query("""
        SELECT role, message, attachment_name, attachment_data, is_image 
        FROM chat_history 
        WHERE session_id = ? 
        ORDER BY id ASC
    """, (session_id,), fetchall=True)
    return jsonify(history)

# --- ИИ ЧАТ (ИСПРАВЛЕНА СОВМЕСТИМОСТЬ РОЛЕЙ: 'bot' -> 'assistant') ---
@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.get_json() or {}
    user_msg = data.get('message', '').strip()
    username = data.get('username', 'Ученик')
    session_id = data.get('session_id')
    att_name = data.get('attachment_name')
    att_data = data.get('attachment_data')
    is_img = data.get('is_image', False)

    if not session_id:
        session_id = str(uuid.uuid4())
        execute_query("INSERT INTO chat_sessions (session_id, username, title) VALUES (?, ?, ?)",
                      (session_id, username, user_msg[:30] if user_msg else "Новый чат"), commit=True)

    # Сохраняем сообщение пользователя в базу данных
    execute_query("""
        INSERT INTO chat_history (session_id, username, role, message, attachment_name, attachment_data, is_image) 
        VALUES (?, ?, 'user', ?, ?, ?, ?)
    """, (session_id, username, user_msg, att_name, att_data, is_img), commit=True)

    sess = execute_query("SELECT title FROM chat_sessions WHERE session_id = ?", (session_id,), fetchone=True)
    if sess and sess.get('title') == "Новый чат" and user_msg:
        execute_query("UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", 
                      (user_msg[:35], session_id), commit=True)

    ai_url = get_setting('ai_base_url', 'https://api.openai.com/v1').strip()
    ai_model = get_setting('ai_model_id', 'glm-5.3-flash').strip()
    ai_key = get_setting('ai_api_key', '').strip()
    sys_prompt = get_setting('system_prompt', '')
    facts = get_setting('facts', '')

    kb_records = execute_query("SELECT filename, extracted_text FROM knowledge_files", fetchall=True)
    kb_context = ""
    if kb_records:
        kb_context = "\n\nМАТЕРИАЛЫ ИЗ БАЗЫ ЗНАНИЙ ШКОЛЫ:\n" + "\n---\n".join([f"Файл: {r['filename']}\n{r['extracted_text']}" for r in kb_records if r.get('extracted_text')])

    full_system = f"{sys_prompt}\n\nВАЖНЫЕ ФАКТЫ О КЛАССЕ:\n{facts}{kb_context}".strip()

    # Загружаем последние 8 сообщений из истории диалога
    raw_history = execute_query("""
        SELECT role, message, attachment_data, is_image 
        FROM chat_history 
        WHERE session_id = ? 
        ORDER BY id DESC LIMIT 8
    """, (session_id,), fetchall=True)
    raw_history.reverse()

    openai_messages = [{"role": "system", "content": full_system}]

    for idx, item in enumerate(raw_history):
        # ИСПРАВЛЕНИЕ: строго переводим 'bot' в 'assistant'
        raw_role = item['role']
        valid_role = "assistant" if raw_role in ['bot', 'assistant'] else "user"
        
        txt = item['message'] or ""
        img_data = item.get('attachment_data')
        is_i = item.get('is_image')

        # Если это последнее сообщение пользователя и прикреплено фото
        if idx == len(raw_history) - 1 and valid_role == 'user' and is_i and img_data:
            openai_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": txt if txt else "Посмотри на это изображение и помоги разобраться:"},
                    {"type": "image_url", "image_url": {"url": img_data}}
                ]
            })
        else:
            openai_messages.append({"role": valid_role, "content": txt})

    def generate():
        yield f"data: {json.dumps({'session_id': session_id})}\n\n"

        if not ai_key:
            yield f"data: {json.dumps({'error': 'В Админке не указан API-ключ для нейросети!'})}\n\n"
            return

        try:
            client = OpenAI(api_key=ai_key, base_url=ai_url)

            response = client.chat.completions.create(
                model=ai_model,
                messages=openai_messages,
                stream=True,
                max_tokens=8192,
                temperature=0.7
            )

            full_reply = ""
            in_think = False

            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Забираем только чистый ответ (content). Черновики reasoning отбрасываем
                text_chunk = getattr(delta, 'content', None)
                if not text_chunk:
                    continue

                if '<think>' in text_chunk:
                    in_think = True
                    text_chunk = text_chunk.split('<think>', 1)[0]
                if in_think:
                    if '</think>' in text_chunk:
                        in_think = False
                        text_chunk = text_chunk.split('</think>', 1)[1]
                    else:
                        continue

                if text_chunk:
                    full_reply += text_chunk
                    yield f"data: {json.dumps({'content': text_chunk})}\n\n"

            if full_reply:
                execute_query("""
                    INSERT INTO chat_history (session_id, username, role, message) 
                    VALUES (?, '8-B AI', 'bot', ?)
                """, (session_id, full_reply), commit=True)
                execute_query("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", 
                              (session_id,), commit=True)

            yield "data: [DONE]\n\n"

        except Exception as e:
            err_msg = str(e)
            print(f"[API CHAT ERROR]: {err_msg}", file=sys.stderr)
            yield f"data: {json.dumps({'error': f'Ошибка API нейросети: {err_msg}'})}\n\n"

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive'
    }
    return Response(generate(), headers=headers)

# --- ГРУППОВОЙ ЧАТ ---
@app.route('/api/group/messages', methods=['GET'])
def api_group_messages():
    msgs = execute_query("""
        SELECT sender, avatar, text, attachment_name, attachment_data, is_image, created_at 
        FROM group_messages 
        ORDER BY id ASC LIMIT 80
    """, fetchall=True)
    return jsonify(msgs)

@app.route('/api/group/send', methods=['POST'])
def api_group_send():
    data = request.get_json() or {}
    sender = data.get('sender', 'Аноним')
    avatar = data.get('avatar')
    text = data.get('message', '')
    att_name = data.get('attachment_name')
    att_data = data.get('attachment_data')
    is_img = data.get('is_image', False)

    execute_query("""
        INSERT INTO group_messages (sender, avatar, text, attachment_name, attachment_data, is_image) 
        VALUES (?, ?, ?, ?, ?, ?)
    """, (sender, avatar, text, att_name, att_data, is_img), commit=True)
    return jsonify({'success': True})

# --- АДМИН-ПАНЕЛЬ (ТОЛЬКО ДЛЯ DUDO) ---
@app.route('/api/admin/data', methods=['POST'])
def api_admin_data():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    users = execute_query("SELECT id, username, device_token FROM users ORDER BY id ASC", fetchall=True)
    user_list = [{'id': u['id'], 'username': u['username'], 'is_locked': bool(u['device_token'])} for u in users]
    kb_files = execute_query("SELECT id, filename FROM knowledge_files ORDER BY id DESC", fetchall=True)

    return jsonify({
        'system_prompt': get_setting('system_prompt'),
        'facts': get_setting('facts'),
        'banner': get_setting('banner'),
        'ai_base_url': get_setting('ai_base_url'),
        'ai_model_id': get_setting('ai_model_id'),
        'ai_api_key': get_setting('ai_api_key'),
        'users': user_list,
        'knowledge_files': kb_files
    })

# 1. Создание пользователя
@app.route('/api/admin/create_user', methods=['POST'])
def api_admin_create_user():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'success': False, 'error': 'Логин и пароль обязательны'})

    exists = execute_query("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if exists:
        return jsonify({'success': False, 'error': 'Такой пользователь уже существует'})

    execute_query("INSERT INTO users (username, password, role) VALUES (?, ?, 'student')", 
                  (username, password), commit=True)
    return jsonify({'success': True})

# 2. Загрузка файла в базу знаний
@app.route('/api/admin/upload_knowledge', methods=['POST'])
def api_admin_upload_knowledge():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    filename = data.get('filename', 'doc.txt')
    file_data = data.get('file_data', '')

    extracted_text = ""
    try:
        if "," in file_data:
            raw_b64 = file_data.split(",", 1)[1]
            decoded_bytes = base64.b64decode(raw_b64)
            extracted_text = decoded_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        extracted_text = f"[Ошибка чтения текста: {e}]"

    execute_query("""
        INSERT INTO knowledge_files (filename, extracted_text, file_data) 
        VALUES (?, ?, ?)
    """, (filename, extracted_text[:15000], file_data), commit=True)

    return jsonify({'success': True})

# 3. Удаление файла из базы знаний
@app.route('/api/admin/delete_knowledge', methods=['POST'])
def api_admin_delete_knowledge():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    file_id = data.get('file_id')
    if file_id:
        execute_query("DELETE FROM knowledge_files WHERE id = ?", (file_id,), commit=True)
    return jsonify({'success': True})

# 4. Просмотр сессий ученика
@app.route('/api/admin/user_chats', methods=['POST'])
def api_admin_user_chats():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    target_user = data.get('target_user')
    sessions = execute_query("""
        SELECT session_id, title 
        FROM chat_sessions 
        WHERE LOWER(username) = LOWER(?) 
        ORDER BY updated_at DESC
    """, (target_user,), fetchall=True)
    return jsonify(sessions)

# 5. Просмотр сообщений ученика
@app.route('/api/admin/user_chat_messages', methods=['POST'])
def api_admin_user_chat_messages():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    session_id = data.get('session_id')
    messages = execute_query("""
        SELECT role, message, attachment_name, attachment_data, is_image, created_at as time 
        FROM chat_history 
        WHERE session_id = ? 
        ORDER BY id ASC
    """, (session_id,), fetchall=True)
    return jsonify(messages)

# 6. Сброс привязки устройства
@app.route('/api/admin/reset_device', methods=['POST'])
def api_admin_reset_device():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    user_id = data.get('user_id')
    if user_id:
        execute_query("UPDATE users SET device_token = NULL WHERE id = ?", (user_id,), commit=True)
    return jsonify({'success': True})

# 7. Добавление домашнего задания
@app.route('/api/admin/save_hw', methods=['POST'])
def api_admin_save_hw():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    subj = data.get('subject', '').strip()
    task = data.get('task', '').strip()
    date = data.get('deadline', '').strip()
    if subj and task:
        execute_query("INSERT INTO homework (subject, task, deadline) VALUES (?, ?, ?)", 
                      (subj, task, date), commit=True)
    return jsonify({'success': True})

# 8. Сохранение настроек
@app.route('/api/admin/save_settings', methods=['POST'])
def api_admin_save_settings():
    data = request.get_json() or {}
    if not verify_admin(data.get('token')):
        return jsonify({'error': 'Доступ запрещен'}), 403

    keys = ['system_prompt', 'facts', 'banner', 'ai_base_url', 'ai_model_id', 'ai_api_key']
    for k in keys:
        if k in data:
            execute_query("""
                INSERT INTO settings (key, value) VALUES (?, ?) 
                ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
            """ if IS_POSTGRES else """
                INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
            """, (k, str(data[k])), commit=True)

    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
