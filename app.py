#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loop (Mattermost) → YouGile бот.

Функции:
- При сообщении вида `@yougile_bot создай задачу <название>`
  запускает интерактивный мастер:
  проект → доска → колонка → исполнитель → дедлайн → создание задачи в YouGile.
- Поддерживает:
  - выбор стандартного дедлайна (сегодня / завтра / послезавтра / неделя / месяц),
  - кастомную дату YYYY-MM-DD,
  - необязательный дедлайн ("Без дедлайна"),
  - отправку сообщений и файлов в чат задачи (chatId = taskId) из треда,
  - отмену создания задачи на любом шаге,
  - автозавершение диалога, если пользователь забыл нажать "Завершить".
"""

import os
import json
import time
import threading
import re
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict
from urllib.parse import quote

import requests
from flask import Flask, request
from websocket import create_connection, WebSocketConnectionClosedException


# ---------------------------------------------------------------------------
#  Вспомогательное: преобразование названия проекта в slug для URL
# ---------------------------------------------------------------------------

def slugify_title(title: str) -> str:
    """
    Превращает название проекта в slug для URL YouGile:
    - пробелы → дефисы
    - несколько дефисов подряд схлопываются
    - строка URL-энкодится
    """
    s = (title or "").strip()
    s = re.sub(r"\s+", "-", s)   # пробелы → дефисы
    s = re.sub(r"-+", "-", s)    # схлопываем повторяющиеся дефисы
    return quote(s)


# ---------------------------------------------------------------------------
#  ENV / конфиг
# ---------------------------------------------------------------------------

# Через сколько минут после неактивности автозавершать диалог (OPTIONAL_ATTACH)
AUTO_FINISH_TIMEOUT_MINUTES = int(os.getenv("AUTO_FINISH_TIMEOUT_MINUTES", "5"))

MM_URL = os.getenv("MM_URL", "").rstrip("/")
MM_BOT_TOKEN = os.getenv("MM_BOT_TOKEN")
MM_BOT_USERNAME = os.getenv("MM_BOT_USERNAME", "yougile_bot").lower()  # без @
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "").rstrip("/")

YOUGILE_COMPANY_ID = os.getenv("YOUGILE_COMPANY_ID")
YOUGILE_API_KEY = os.getenv("YOUGILE_API_KEY")
YOUGILE_BASE_URL = os.getenv("YOUGILE_BASE_URL", "https://ru.yougile.com/api-v2").rstrip("/")
YOUGILE_TEAM_ID = os.getenv("YOUGILE_TEAM_ID")

# Если явно не указан TEAM_ID, пытаемся получить его из COMPANY_ID
if not YOUGILE_TEAM_ID and YOUGILE_COMPANY_ID:
    YOUGILE_TEAM_ID = YOUGILE_COMPANY_ID.split("-")[-1]

if not (MM_URL and MM_BOT_TOKEN and YOUGILE_COMPANY_ID and YOUGILE_API_KEY and BOT_PUBLIC_URL):
    print("ERROR: some required env vars are missing (MM_URL / MM_BOT_TOKEN / YOUGILE_* / BOT_PUBLIC_URL)")
    # Не выходим, чтобы это было видно в логах, но бот работать не будет.

BOT_USER_ID = None

# ---------------------------------------------------------------------------
#  HTTP-заголовки
# ---------------------------------------------------------------------------

mm_headers = {
    "Authorization": f"Bearer {MM_BOT_TOKEN}",
    "Content-Type": "application/json",
}

yg_headers = {
    # "X-Company-Id": YOUGILE_COMPANY_ID,
    # "X-Api-Key": YOUGILE_API_KEY,
    "Authorization": f"Bearer {YOUGILE_API_KEY}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
#  Состояние диалогов (по пользователю и корневому посту)
# ---------------------------------------------------------------------------

# ключ: (user_id, root_post_id) → dict со всеми шагами мастера
STATE = defaultdict(dict)
STATE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
#  Мэппинг: канал Loop (Mattermost) → проект YouGile по умолчанию
# ---------------------------------------------------------------------------

CHANNEL_MAP_FILE = os.getenv("CHANNEL_MAP_FILE", "/data/channel_project_map.json")
CHANNEL_MAP_LOCK = threading.Lock()
CHANNEL_PROJECT_MAP = {}  # channel_id -> {"project_id": ..., "project_title": ...}


def load_channel_map():
    global CHANNEL_PROJECT_MAP
    if not CHANNEL_MAP_FILE:
        CHANNEL_PROJECT_MAP = {}
        return
    try:
        with open(CHANNEL_MAP_FILE, "r", encoding="utf-8") as f:
            CHANNEL_PROJECT_MAP = json.load(f)
    except FileNotFoundError:
        CHANNEL_PROJECT_MAP = {}
    except Exception as e:
        print("Error loading channel map:", e)
        CHANNEL_PROJECT_MAP = {}


def save_channel_map():
    if not CHANNEL_MAP_FILE:
        return
    tmp_path = CHANNEL_MAP_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(CHANNEL_PROJECT_MAP, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CHANNEL_MAP_FILE)
    except Exception as e:
        print("Error saving channel map:", e)


def get_default_project_for_channel(channel_id):
    with CHANNEL_MAP_LOCK:
        entry = CHANNEL_PROJECT_MAP.get(channel_id)
        if not entry:
            return None
        return entry  # {"project_id": ..., "project_title": ...}

def set_default_project_for_channel(channel_id, project_id, project_title):
    with CHANNEL_MAP_LOCK:
        CHANNEL_PROJECT_MAP[channel_id] = {
            "project_id": project_id,
            "project_title": project_title or "без названия",
        }
        save_channel_map()

def delete_default_project_for_channel(channel_id):
    with CHANNEL_MAP_LOCK:
        if channel_id in CHANNEL_PROJECT_MAP:
            CHANNEL_PROJECT_MAP.pop(channel_id, None)
            save_channel_map()

def set_state(user_id, root_post_id, data: dict):
    """
    Обновляет состояние мастера для пары (user_id, root_post_id).
    Заодно проставляет created_at / updated_at.
    """
    now = time.time()
    with STATE_LOCK:
        s = STATE[(user_id, root_post_id)]
        if "created_at" not in s:
            s["created_at"] = now
        s.update(data or {})
        s["updated_at"] = now
        return s


def get_state(user_id, root_post_id):
    """Возвращает состояние мастера, если есть."""
    with STATE_LOCK:
        return STATE.get((user_id, root_post_id))


def clear_state(user_id, root_post_id):
    """Удаляет состояние мастера для пары (user_id, root_post_id)."""
    with STATE_LOCK:
        STATE.pop((user_id, root_post_id), None)


# ---------------------------------------------------------------------------
#  Помощники для Loop (Mattermost)
# ---------------------------------------------------------------------------
def mm_get_me():
    """Получить данные текущего пользователя (бота) по токену."""
    r = requests.get(f"{MM_URL}/api/v4/users/me", headers=mm_headers, timeout=10)
    r.raise_for_status()
    return r.json()

def get_bot_user_id():
    """Лениво получить и закешировать user_id бота."""
    global BOT_USER_ID
    if BOT_USER_ID:
        return BOT_USER_ID
    try:
        me = mm_get_me()
        BOT_USER_ID = me.get("id")
    except Exception as e:
        print("Error getting bot user id:", e)
        BOT_USER_ID = None
    return BOT_USER_ID

def mm_get_user(user_id):
    """Получить данные пользователя по user_id."""
    url = f"{MM_URL}/api/v4/users/{user_id}"
    r = requests.get(url, headers=mm_headers, timeout=10)
    r.raise_for_status()
    return r.json()

def mm_get_channel(channel_id):
    """Получить данные канала (нужно для красивого имени чата)."""
    url = f"{MM_URL}/api/v4/channels/{channel_id}"
    r = requests.get(url, headers=mm_headers, timeout=10)
    r.raise_for_status()
    return r.json()

def mm_post(channel_id, message, attachments=None, root_id=None):
    """
    Создаёт новый пост в Loop.
    Если указан root_id — пост будет в треде.
    Если переданы attachments — это интерактивные сообщения (кнопки/селекты).
    """
    payload = {
        "channel_id": channel_id,
        "message": message,
    }
    if root_id:
        payload["root_id"] = root_id
    if attachments:
        payload.setdefault("props", {})
        payload["props"]["attachments"] = attachments

    r = requests.post(f"{MM_URL}/api/v4/posts", headers=mm_headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def mm_patch_post(post_id, message=None, attachments=None):
    """
    Обновляет существующий пост:
    - можно поменять текст,
    - можно убрать/заменить attachments.
    """
    payload = {"id": post_id}
    if message is not None:
        payload["message"] = message
    if attachments is not None:
        payload.setdefault("props", {})
        payload["props"]["attachments"] = attachments

    r = requests.put(f"{MM_URL}/api/v4/posts/{post_id}", headers=mm_headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def mm_post_ephemeral(user_id, channel_id, message, attachments=None, root_id=None):
    """
    Отправить ephemeral-сообщение (видно только одному пользователю).
    """
    post = {
        "channel_id": channel_id,
        "message": message,
    }
    if root_id:
        post["root_id"] = root_id
    if attachments:
        post.setdefault("props", {})
        post["props"]["attachments"] = attachments

    payload = {
        "user_id": user_id,
        "post": post,
    }

    r = requests.post(
        f"{MM_URL}/api/v4/posts/ephemeral",
        headers=mm_headers,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def mm_add_reaction(user_id, post_id, emoji_name):
    """Поставить реакцию на сообщение в Loop (Mattermost) от имени бота."""
    bot_id = get_bot_user_id()

    payload = {
        # даже если нам передали user_id автора,
        # реакция должна ставиться именно ботом
        "user_id": bot_id or user_id,
        "post_id": post_id,
        "emoji_name": emoji_name,
    }
    r = requests.post(
        f"{MM_URL}/api/v4/reactions",
        headers=mm_headers,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def mm_get_file(file_id):
    """Скачать файл из Loop по file_id."""
    r = requests.get(
        f"{MM_URL}/api/v4/files/{file_id}",
        headers=mm_headers,
        timeout=30,
    )
    r.raise_for_status()
    return r.content


def mm_get_file_info(file_id):
    """Получить метаданные файла из Loop (имя, mime и т.п.)."""
    r = requests.get(
        f"{MM_URL}/api/v4/files/{file_id}/info",
        headers=mm_headers,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def decode_mm_post_from_event(data):
    """Парсит JSON-представление поста из события websocket."""
    post_raw = data.get("data", {}).get("post")
    if not post_raw:
        return None
    return json.loads(post_raw)


def parse_create_command(message: str, bot_username: str):
    """
    Парсит команду вида:
        @yougile_bot создай задачу <название>

    Возвращает title задачи или None, если формат не подходит.
    """
    text = message.strip()
    # Убираем упоминание бота
    mention_pattern = rf"@{re.escape(bot_username)}"
    text = re.sub(mention_pattern, "", text, flags=re.IGNORECASE).strip()

    # Ищем "создай задачу ..."
    pattern = r"^создай\s+задачу\s+(.+)$"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()

def extract_selected_value(data):
    """
    Унифицированно достаёт value выбранной опции из payload интерактивного экшена Mattermost.

    Возвращает строку (value) или None, если ничего не выбрано.
    """
    ctx = data.get("context") or {}
    raw = ctx.get("selected_option") or (data.get("data") or {}).get("selected_option")

    # Mattermost может присылать selected_option как dict {"text": "...", "value": "..."}
    # или сразу строкой.
    if isinstance(raw, dict):
        return raw.get("value")
    return raw

# ---------------------------------------------------------------------------
#  Обёртки над YouGile API (проекты / доски / задачи / чат / файлы)
# ---------------------------------------------------------------------------

def yg_get_projects():
    """GET /projects — список проектов."""
    r = requests.get(f"{YOUGILE_BASE_URL}/projects", headers=yg_headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data.get("content", [])


def yg_get_boards(project_id):
    """GET /boards?projectId=... — список досок проекта."""
    r = requests.get(
        f"{YOUGILE_BASE_URL}/boards",
        headers=yg_headers,
        params={"projectId": project_id},
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    return data.get("content", [])


def yg_get_columns(board_id):
    """GET /columns?boardId=... — список колонок доски."""
    r = requests.get(
        f"{YOUGILE_BASE_URL}/columns",
        headers=yg_headers,
        params={"boardId": board_id},
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    return data.get("content", [])


def yg_get_project_users(project_id=None):
    """
    GET /users?projectId=... — список пользователей проекта
    или GET /users — список всех пользователей компании (если project_id не задан).
    """
    params = {"projectId": project_id} if project_id else None

    r = requests.get(
        f"{YOUGILE_BASE_URL}/users",
        headers=yg_headers,
        params=params,
        timeout=10
    )
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict):
        return data.get("content", [])
    if isinstance(data, list):
        return data

    print("DEBUG yg_get_project_users unexpected type:", type(data), data)
    return []


def yg_create_task(title, column_id, description="", assignee_id=None, deadline=None):
    """
    POST /tasks — создать задачу в YouGile.
    - title: название
    - column_id: колонка
    - description: описание
    - assignee_id: id исполнителя (опционально)
    - deadline: date или None
    """
    body = {
        "title": title,
        "columnId": column_id,
        "description": description,
    }

    # назначенный исполнитель
    if assignee_id:
        body["assigned"] = [assignee_id]

    # дедлайн в формате YouGile
    if deadline:
        # deadline у нас date, превращаем в полдень по UTC, чтобы дата не сдвигалась
        dt_utc_noon = datetime(
            deadline.year,
            deadline.month,
            deadline.day,
            12, 0, 0,
            tzinfo=timezone.utc,
        )
        ms = int(dt_utc_noon.timestamp() * 1000)
        body["deadline"] = {
            "deadline": ms,
            "withTime": False,
        }

    r = requests.post(
        f"{YOUGILE_BASE_URL}/tasks",
        headers=yg_headers,
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def yg_get_task(task_id):
    """GET /tasks/{id} — полная карточка задачи (используем только для idTaskProject/idTaskCommon)."""
    r = requests.get(
        f"{YOUGILE_BASE_URL}/tasks/{task_id}",
        headers=yg_headers,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def yg_send_chat_message(chat_id, text):
    """
    Отправить сообщение в чат задачи.
    В YouGile chatId = taskId.

    Согласно доке:
    POST /api-v2/chats/{chatId}/messages
    """
    payload = {
        "text": text,
    }

    # YOUGILE_BASE_URL уже вида https://ru.yougile.com/api-v2
    url = f"{YOUGILE_BASE_URL}/chats/{chat_id}/messages"

    r = requests.post(
        url,
        headers=yg_headers,
        json=payload,
        timeout=10,
    )

    if "application/json" not in r.headers.get("Content-Type", ""):
        print("YG chat send non-JSON response:", r.status_code, r.text[:500])

    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {}


def yg_upload_file(file_bytes, filename, mimetype="application/octet-stream"):
    """
    Загрузить файл в YouGile и вернуть относительный URL вида
    /user-data/.../file.ext

    Согласно доке используется:
    POST /api-v2/upload-file
    с multipart/form-data.
    """
    files = {
        "file": (filename, file_bytes, mimetype),
    }

    # Для multipart заголовок Content-Type ставит сам requests,
    # поэтому убираем его из yg_headers
    headers = dict(yg_headers)
    headers.pop("Content-Type", None)

    r = requests.post(
        f"{YOUGILE_BASE_URL}/upload-file",
        headers=headers,
        files=files,
        timeout=30,
    )

    if "application/json" not in r.headers.get("Content-Type", ""):
        print("YG upload non-JSON response:", r.status_code, r.text[:500])
    r.raise_for_status()

    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"YouGile file upload returned non-JSON response (status {r.status_code})"
        )

    file_url = (
        data.get("url")
        or data.get("path")
        or data.get("fileUrl")
    )
    if not file_url:
        print("YG upload unexpected JSON:", data)
        raise RuntimeError("YouGile file upload JSON has no 'url' field")

    return file_url

def get_allowed_projects_for_mm_user(user_id):
    """
    Возвращает список проектов YouGile, к которым у пользователя Loop есть доступ (по email).

    Логика:
    1) Берём email пользователя из Loop.
    2) Один запрос к /users (все пользователи компании) -> строим map user_id -> email.
    3) Один запрос к /projects -> в каждом проекте поле "users" (userId -> role).
    4) Для каждого проекта проверяем, есть ли среди users тот, у кого email = email из Loop.
    """
    try:
        mm_user = mm_get_user(user_id)
        mm_email = (mm_user.get("email") or "").strip().lower()
    except Exception as e:
        print("Error fetching MM user for project filter:", e)
        mm_email = ""

    if not mm_email:
        return []

    # 1) Все пользователи компании: id -> email
    try:
        all_users = yg_get_project_users(None)  # вызов без projectId -> /users (все пользователи)
    except Exception as e:
        print("Error fetching YouGile users:", e)
        all_users = []

    user_email_by_id = {}
    for u in all_users:
        uid = u.get("id")
        email = (u.get("email") or "").strip().lower()
        if uid and email:
            user_email_by_id[uid] = email

    # 2) Все проекты
    try:
        all_projects = yg_get_projects()
    except Exception as e:
        print("Error fetching YouGile projects:", e)
        all_projects = []

    allowed_projects = []

    for p in all_projects:
        # опционально можно сразу отфильтровать удалённые проекты
        if p.get("deleted"):
            continue

        users_map = p.get("users") or {}
        if not isinstance(users_map, dict):
            continue

        # users_map: { userId: "roleId" / "worker" / ... }
        for user_id_in_project in users_map.keys():
            email = user_email_by_id.get(user_id_in_project)
            if email and email == mm_email:
                allowed_projects.append(p)
                break  # этот проект уже добавлен, дальше юзеров не смотрим

    return allowed_projects


# ---------------------------------------------------------------------------
#  Дедлайны
# ---------------------------------------------------------------------------

def calc_deadline(choice: str) -> date:
    """
    Преобразует строковый выбор дедлайна в дату:
    today / tomorrow / day_after_tomorrow / week / month.
    """
    today = date.today()
    c = (choice or "").lower()
    if c == "today":
        return today
    if c == "tomorrow":
        return today + timedelta(days=1)
    if c == "day_after_tomorrow":
        return today + timedelta(days=2)
    if c == "week":
        return today + timedelta(days=7)
    if c == "month":
        return today + timedelta(days=30)
    # fallback: сегодня
    return today

def format_deadline(choice: str, deadline: date | None, raw_display: str | None = None):
    """
    Преобразует внутренние значения дедлайна в:
    - deadline_human: человекочитаемое значение ("Сегодня", "Через неделю", "Без дедлайна", "2025-11-13", ...)
    - deadline_str: финальная строка для summary, вида "<human> (DD.MM.YYYY)" или "без дедлайна"
    """
    choice = (choice or "").lower()

    if choice == "none":
        deadline_human = "Без дедлайна"
    elif choice == "today":
        deadline_human = "Сегодня"
    elif choice == "tomorrow":
        deadline_human = "Завтра"
    elif choice == "day_after_tomorrow":
        deadline_human = "Послезавтра"
    elif choice == "week":
        deadline_human = "Через неделю"
    elif choice == "month":
        deadline_human = "Через месяц"
    elif choice == "custom" and raw_display:
        # Показываем ровно то, что ввёл пользователь
        deadline_human = raw_display
    elif deadline:
        deadline_human = deadline.strftime("%d.%m.%Y")
    else:
        deadline_human = "без дедлайна"

    if deadline:
        date_str = deadline.strftime("%d.%m.%Y")
        deadline_str = f"{deadline_human} ({date_str})"
    else:
        deadline_str = deadline_human or "без дедлайна"

    return deadline_human, deadline_str


# ---------------------------------------------------------------------------
#  Построение интерактивных сообщений (attachments) для шагов мастера
# ---------------------------------------------------------------------------

def build_task_summary(task_title, project_title, board_title, assignee_name, deadline_str, task_url=None):
    """
    Строит единообразное summary задачи:
    Задача "<title>" создана в проекте: <project>, доска: <board>, 
    ответственный: <assignee>, дедлайн: <deadline>.
    + при наличии добавляет ссылку.
    """
    summary = (
        f'Задача "{task_title}" создана в проекте: {project_title}, '
        f'доска: {board_title}, ответственный: {assignee_name}, '
        f'дедлайн: {deadline_str}.'
    )
    if task_url:
        summary += f"\nСсылка: {task_url}"
    return summary

def send_task_summary(channel_id, root_post_id, summary, *, to_channel=False, auto=False):
    """
    Унифицированная отправка summary:
    - всегда пишет summary в тред (root_post_id),
    - опционально дублирует summary в общий канал (to_channel=True),
    - для автозавершения добавляет тех. сообщение "(Автозавершение диалога)" (auto=True).
    """
    # 1) В тред
    mm_post(
        channel_id,
        message=summary,
        root_id=root_post_id
    )

    # 2) В общий чат (дополнительный пост без root_id)
    if to_channel:
        mm_post(
            channel_id,
            message=summary
        )

    # 3) Техническое сообщение при автозавершении
    if auto:
        mm_post(
            channel_id,
            message="(Автозавершение диалога)",
            root_id=root_post_id
        )

def add_cancel_action(actions, task_title, root_post_id, user_id):
    """
    Добавляет красную кнопку "Отменить" в конец списка actions.
    """
    actions.append({
        "id": "cancel",
        "name": "Отменить",
        "type": "button",
        "style": "danger",
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
            "context": {
                "step": "CANCEL",
                "task_title": task_title,
                "root_post_id": root_post_id,
                "user_id": user_id,
            }
        }
    })
    return actions


def build_project_select_for_task(task_title, projects, user_id, root_post_id):
    """
    Выпадающий список проектов для создания задачи.
    """
    options = []
    for p in projects:
        pid = p.get("id")
        title = p.get("title", "Без имени")
        if not pid:
            continue
        options.append({
            "text": title,
            "value": pid
        })

    select_action = {
        "id": "projectSelect",
        "name": "Выберите проект",
        "type": "select",
        "options": options,
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
            "context": {
                "step": "CHOOSE_PROJECT",
                "task_title": task_title,
                "root_post_id": root_post_id,
                "user_id": user_id,
            }
        }
    }

    return [{
        "text": "Проект:",
        "actions": add_cancel_action([select_action], task_title, root_post_id, user_id)
    }]
    
def build_board_select_for_task(task_title, project_id, boards, user_id, root_post_id):
    """
    Выпадающий список досок для выбранного проекта.
    """
    options = []
    for b in boards:
        bid = b.get("id")
        title = b.get("title", "Без имени")
        if not bid:
            continue
        options.append({
            "text": title,
            "value": bid
        })

    select_action = {
        "id": "boardSelect",
        "name": "Выберите доску",
        "type": "select",
        "options": options,
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
            "context": {
                "step": "CHOOSE_BOARD",
                "task_title": task_title,
                "project_id": project_id,
                "root_post_id": root_post_id,
                "user_id": user_id,
            }
        }
    }

    return [{
        "text": "Доска:",
        "actions": add_cancel_action([select_action], task_title, root_post_id, user_id)
    }]
    
def build_column_select_for_task(task_title, project_id, board_id, columns, user_id, root_post_id):
    """
    Выпадающий список колонок для выбранной доски.
    """
    options = []
    for c in columns:
        cid = c.get("id")
        title = c.get("title", "Без имени")
        if not cid:
            continue
        options.append({
            "text": title,
            "value": cid
        })

    select_action = {
        "id": "columnSelect",
        "name": "Выберите колонку",
        "type": "select",
        "options": options,
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
            "context": {
                "step": "CHOOSE_COLUMN",
                "task_title": task_title,
                "project_id": project_id,
                "board_id": board_id,
                "root_post_id": root_post_id,
                "user_id": user_id,
            }
        }
    }

    return [{
        "text": "Колонка:",
        "actions": add_cancel_action([select_action], task_title, root_post_id, user_id)
    }]


def build_assignee_select(task_title, project_id, board_id, column_id, users, user_id, root_post_id):
    """Селект выбора исполнителя + кнопка отмены."""
    options = []
    for u in users:
        full_name = u.get("realName", "") or u.get("email", "Без имени")
        options.append({
            "text": full_name,
            "value": u["id"]
        })

    base_action = {
        "id": "assigneeSelect",
        "name": "Выберите исполнителя",
        "type": "select",
        "options": options,
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
            "context": {
                "step": "CHOOSE_ASSIGNEE",
                "task_title": task_title,
                "project_id": project_id,
                "board_id": board_id,
                "column_id": column_id,
                "root_post_id": root_post_id,
                "user_id": user_id,
            }
        }
    }

    return [{
        "text": "Исполнитель:",
        "actions": add_cancel_action([base_action], task_title, root_post_id, user_id)
    }]


def build_deadline_buttons(task_title, meta, user_id, root_post_id):
    """Кнопки выбора дедлайна."""

    def act(id_, name, key):
        ctx = {
            "step": "CHOOSE_DEADLINE",
            "task_title": task_title,
            "root_post_id": root_post_id,
            "user_id": user_id,
            "deadline_choice": key,
        }
        ctx.update(meta)
        return {
            "id": id_,
            "name": name,
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": ctx
            }
        }

    actions = [
        act("dlNone", "Без дедлайна", "none"),
        act("dlToday", "Сегодня", "today"),
        act("dlTomorrow", "Завтра", "tomorrow"),
        act("dlDayAfter", "Послезавтра", "day_after_tomorrow"),
        act("dlWeek", "Через неделю", "week"),
        act("dlMonth", "Через месяц", "month"),
        act("dlCustom", "Другая дата", "custom"),
    ]

    add_cancel_action(actions, task_title, root_post_id, user_id)

    return [{
        "text": "Выберите дедлайн:",
        "actions": actions
    }]


def build_finish_buttons(task_title, task_url, user_id, root_post_id, meta):
    """Кнопка 'Завершить' после создания задачи."""
    actions = [
        {
            "id": "finish",
            "name": "Завершить",
            "type": "button",
            "style": "primary",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "FINISH",
                    "task_title": task_title,
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                    **meta
                }
            }
        }
    ]
    return [{
        "text": (
            "Можете написать дополнительный комментарий и приложить файлы в этом треде, "
            'а затем нажать "Завершить".'
        ),
        "actions": actions
    }]
    
def build_main_menu_attachments(user_id, root_post_id):
    """
    Главное меню бота:
    - Создать задачу
    - Показать быстрые команды
    - Сменить проект по умолчанию для чата
    - Завершить диалог
    """
    actions = [
        {
            "id": "menuCreateTask",
            "name": "Создать задачу",
            "type": "button",
            "style": "primary",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "MENU_CREATE_TASK",
                    "user_id": user_id,
                    "root_post_id": root_post_id,
                }
            }
        },
        {
            "id": "menuShowShortcuts",
            "name": "Показать быстрые команды",
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "MENU_SHOW_SHORTCUTS",
                    "user_id": user_id,
                    "root_post_id": root_post_id,
                }
            }
        },
        {
            "id": "menuChangeDefaultProject",
            "name": "Сменить проект по умолчанию",
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "MENU_CHANGE_DEFAULT_PROJECT",
                    "user_id": user_id,
                    "root_post_id": root_post_id,
                }
            }
        },
        {
            "id": "menuCancel",
            "name": "Завершить диалог",
            "type": "button",
            "style": "danger",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "MENU_CANCEL",
                    "user_id": user_id,
                    "root_post_id": root_post_id,
                }
            }
        },
    ]

    return [{
        "text": "",  # раньше было "Привет! Вот что я умею:"
        "actions": actions
    }]


# ---------------------------------------------------------------------------
#  Flask-приложение (webhook для интерактивных действий)
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/healthz", methods=["GET"])
def healthz():
    """Простой healthcheck."""
    return "ok", 200


@app.route("/mattermost/actions", methods=["POST"])
def mm_actions():
    """
    Обработчик интерактивных действий:
    - выбор проекта / доски / колонки / исполнителя / дедлайна
    - отмена
    - ручное завершение диалога
    """
    data = request.get_json(force=True, silent=True) or {}
    context = data.get("context", {}) or {}
    step = context.get("step")

    # кто реально нажал кнопку — берём из запроса, если в контексте нет
    user_id = context.get("user_id") or data.get("user_id")
    post_id = data.get("post_id")
    channel_id = data.get("channel_id")
    # если root_post_id не передан в контексте, берём сам post_id
    root_post_id = context.get("root_post_id") or data.get("root_id") or post_id

    if not (step and user_id and root_post_id and post_id and channel_id):
        return "", 200

    state = get_state(user_id, root_post_id) or {}
    task_title = context.get("task_title") or state.get("task_title", "Без названия")

    try:
        # ---------- ГЛАВНОЕ МЕНЮ ----------
        if step == "MENU_CREATE_TASK":
            # Начинаем диалог: ждём название задачи
            set_state(user_id, root_post_id, {
                "step": "ASK_TASK_TITLE",
                "channel_id": channel_id,
                "task_title": None,
                # запоминаем сообщение, где просили ввести название
                "ask_title_post_id": post_id,
                # и сразу считаем его служебным, чтобы CANCEL мог удалить
                "post_ids": [post_id],
            })

            # Кнопка "Отменить" для этого шага
            attachments = [{
                "text": "Назовите задачу:",
                "actions": add_cancel_action([], "Без названия", root_post_id, user_id)
            }]

            mm_patch_post(
                post_id,
                message='Пожалуйста, введите название задачи в этом треде.',
                attachments=attachments
            )

            return "", 200

        elif step == "MENU_SHOW_SHORTCUTS":
            shortcuts_text = (
                "Быстрые команды:\n"
                "- `создай задачу <название задачи>` — сразу запустить мастер создания задачи.\n\n"
                "Можно также воспользоваться главным меню, упомянув бота."
            )
            mm_post(
                channel_id,
                message=shortcuts_text,
                root_id=root_post_id
            )
            return "", 200

        elif step == "MENU_CHANGE_DEFAULT_PROJECT":
            # Показываем текущий проект по умолчанию и даём выбрать новый
            allowed_projects = get_allowed_projects_for_mm_user(user_id)
            if not allowed_projects:
                mm_post(
                    channel_id,
                    message="Не нашёл для вас доступных проектов в YouGile. Обратитесь к администратору.",
                    root_id=root_post_id
                )
                return "", 200

            project_options = {
                p["id"]: p.get("title", "Без имени")
                for p in allowed_projects
                if p.get("id")
            }

            channel = mm_get_channel(channel_id)
            channel_name = channel.get("display_name") or channel.get("name") or channel_id

            current = get_default_project_for_channel(channel_id)
            if current:
                current_title = current.get("project_title", "не установлен")
            else:
                current_title = "не установлен"

            set_state(user_id, root_post_id, {
                "step": "MENU_CHANGE_DEFAULT_PROJECT",
                "channel_id": channel_id,
                "project_options": project_options,
            })

            options = [
                {"text": title, "value": pid}
                for pid, title in project_options.items()
            ] + [
                {"text": "Не выбирать проект автоматически", "value": "__none__"},
            ]

            select_action = {
                "id": "defaultProjectSelectChange",
                "name": "Выберите новый проект",
                "type": "select",
                "options": options,
                "integration": {
                    "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                    "context": {
                        "step": "MENU_DEFAULT_PROJECT_SET",
                        "root_post_id": root_post_id,
                    }
                }
            }

            cancel_action = {
                "id": "cancelDefaultProjectChange",
                "name": "Не менять",
                "type": "button",
                "integration": {
                    "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                    "context": {
                        "step": "MENU_DEFAULT_PROJECT_CANCEL",
                        "root_post_id": root_post_id,
                    }
                }
            }

            text = (
                f'Сейчас проект по умолчанию для чата "{channel_name}": {current_title}\n'
                f"Выберите новый проект:"
            )

            attachments = [{
                "text": text,
                "actions": [select_action, cancel_action]
            }]

            mm_patch_post(
                post_id,
                message=text,
                attachments=attachments
            )

            return "", 200

        elif step == "MENU_DEFAULT_PROJECT_CANCEL":
            mm_patch_post(
                post_id,
                message="Смена проекта по умолчанию отменена. Старое значение сохранено.",
                attachments=[]
            )
            clear_state(user_id, root_post_id)
            return "", 200

        elif step == "MENU_DEFAULT_PROJECT_SET":
            project_id = extract_selected_value(data)
            if not project_id:
                return "", 200

            if project_id == "__none__":
                # очищаем маппинг
                delete_default_project_for_channel(channel_id)
                mm_patch_post(
                    post_id,
                    message="Проект по умолчанию для этого чата отключён. Буду спрашивать проект при создании задач.",
                    attachments=[]
                )
                clear_state(user_id, root_post_id)
                return "", 200

            st = get_state(user_id, root_post_id) or {}
            project_options = st.get("project_options", {})
            project_title = project_options.get(project_id, "без названия")

            set_default_project_for_channel(channel_id, project_id, project_title)

            mm_patch_post(
                post_id,
                message=f'Проект по умолчанию для этого чата изменён на: {project_title}',
                attachments=[]
            )

            clear_state(user_id, root_post_id)
            return "", 200

        elif step == "MENU_CANCEL":
            mm_patch_post(
                post_id,
                message="Диалог с ботом завершён. Если что — зовите ещё! :wink:",
                attachments=[]
            )
            clear_state(user_id, root_post_id)
            return "", 200
        
        # ---------- ВОПРОС ПРО ПРОЕКТ ПО УМОЛЧАНИЮ (ПРИ ДОБАВЛЕНИИ В КАНАЛ) ----------
        elif step == "DEFAULT_PROJECT_PROMPT_NO":
            # Пользователь явно отказался от проекта по умолчанию → чистим мэппинг
            delete_default_project_for_channel(channel_id)

            # Обновляем текущее сообщение (где были кнопки) и выключаем их
            mm_patch_post(
                post_id,
                message="Ок, проект по умолчанию для этого чата не установлен. Буду спрашивать проект при создании задач.",
                attachments=[]
            )

            clear_state(user_id, root_post_id)
            return "", 200

        elif step == "DEFAULT_PROJECT_PROMPT_YES":
            # user_id здесь - тот, кто нажал кнопку
            allowed_projects = get_allowed_projects_for_mm_user(user_id)
            if not allowed_projects:
                mm_post(
                    channel_id,
                    message="Не нашёл для вас доступных проектов в YouGile. Обратитесь к администратору.",
                    root_id=root_post_id
                )
                return "", 200

            project_options = {
                p["id"]: p.get("title", "Без имени")
                for p in allowed_projects
                if p.get("id")
            }

            set_state(user_id, root_post_id, {
                "step": "DEFAULT_PROJECT_SELECT",
                "channel_id": channel_id,
                "project_options": project_options,
            })

            # --- ВСТАВЛЯЕМ опцию "Не выбирать проект автоматически" ПЕРВОЙ ---
            options = [
                {"text": "Не выбирать проект автоматически", "value": "__none__"},
            ] + [
                {"text": title, "value": pid}
                for pid, title in project_options.items()
            ]

            select_action = {
                "id": "defaultProjectSelect",
                "name": "Выберите проект",
                "type": "select",
                "options": options,
                "integration": {
                    "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                    "context": {
                        "step": "DEFAULT_PROJECT_SET",
                        "root_post_id": root_post_id,
                    }
                }
            }

            attachments = [{
                "text": "Выберите проект по умолчанию для этого чата:",
                "actions": [select_action]
            }]

            # ВАЖНО: обновляем ИСХОДНОЕ ephemeral, НЕ создаём новое сообщение
            mm_patch_post(
                post_id,
                message="Проект по умолчанию:",
                attachments=attachments
            )

            return "", 200

        elif step == "DEFAULT_PROJECT_SET":
            project_id = extract_selected_value(data)
            if not project_id:
                return "", 200

            if project_id == "__none__":
                delete_default_project_for_channel(channel_id)
                mm_patch_post(
                    post_id,
                    message="Проект по умолчанию для этого чата не будет выбран автоматически. Буду спрашивать проект при создании задач.",
                    attachments=[]
                )
                clear_state(user_id, root_post_id)
                return "", 200

            st = get_state(user_id, root_post_id) or {}
            project_options = st.get("project_options", {})
            project_title = project_options.get(project_id, "без названия")

            set_default_project_for_channel(channel_id, project_id, project_title)

            mm_patch_post(
                post_id,
                message=f'Проект по умолчанию для этого чата установлен: {project_title}',
                attachments=[]
            )

            clear_state(user_id, root_post_id)
            return "", 200
        
        # ---------- ВЫБОР ПРОЕКТА (через select) ----------
        elif step == "CHOOSE_PROJECT":
            project_id = extract_selected_value(data)
            if not project_id:
                return "", 200

            state = get_state(user_id, root_post_id) or {}
            project_options = state.get("project_options", {})
            project_title = project_options.get(project_id, "без названия")

            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_PROJECT",
                "task_title": task_title,
                "project_id": project_id,
                "project_title": project_title,
                "channel_id": channel_id,
            })

            boards = yg_get_boards(project_id)

            mm_patch_post(
                post_id,
                message=f'Проект для задачи "{task_title}": {project_title}',
                attachments=[]
            )

            if not boards:
                mm_post(
                    channel_id,
                    message=f'В проекте "{project_title}" нет досок, задачу создать нельзя.',
                    root_id=root_post_id
                )
                return "", 200

            if len(boards) == 1:
                board = boards[0]
                board_id = board["id"]
                board_title = board.get("title", "без названия")

                state = set_state(user_id, root_post_id, {
                    "step": "CHOOSE_BOARD",
                    "board_id": board_id,
                    "board_title": board_title,
                })

                columns = yg_get_columns(board_id)
                if not columns:
                    mm_post(
                        channel_id,
                        message=f'На доске "{board_title}" нет колонок, задачу создать нельзя.',
                        root_id=root_post_id
                    )
                    return "", 200

                column_options = {c["id"]: c.get("title", "Без имени") for c in columns if c.get("id")}
                set_state(user_id, root_post_id, {"column_options": column_options})

                attachments = build_column_select_for_task(task_title, project_id, board_id, columns, user_id, root_post_id)
                resp = mm_post(
                    channel_id,
                    message=f'Выберите колонку для задачи "{task_title}"',
                    attachments=attachments,
                    root_id=root_post_id
                )
                set_state(user_id, root_post_id, {
                    "post_ids": state.get("post_ids", []) + [resp["id"]]
                })
            else:
                board_options = {b["id"]: b.get("title", "Без имени") for b in boards if b.get("id")}
                set_state(user_id, root_post_id, {"board_options": board_options})

                attachments = build_board_select_for_task(task_title, project_id, boards, user_id, root_post_id)
                resp = mm_post(
                    channel_id,
                    message=f'Выберите доску для задачи "{task_title}"',
                    attachments=attachments,
                    root_id=root_post_id
                )
                set_state(user_id, root_post_id, {
                    "post_ids": state.get("post_ids", []) + [resp["id"]]
                })

        # ---------- ВЫБОР ДОСКИ ----------
        elif step == "CHOOSE_BOARD":
            board_id = extract_selected_value(data)
            if not board_id:
                return "", 200

            state = get_state(user_id, root_post_id) or {}
            project_id = state.get("project_id") or context.get("project_id")
            board_options = state.get("board_options", {})
            board_title = board_options.get(board_id, "без названия")

            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_BOARD",
                "project_id": project_id,
                "board_id": board_id,
                "board_title": board_title,
            })

            columns = yg_get_columns(board_id)
            if not columns:
                mm_patch_post(
                    post_id,
                    message=f'На доске "{board_title}" нет колонок, задачу создать нельзя.',
                    attachments=[]
                )
                return "", 200

            column_options = {c["id"]: c.get("title", "Без имени") for c in columns if c.get("id")}
            set_state(user_id, root_post_id, {"column_options": column_options})

            attachments = build_column_select_for_task(task_title, project_id, board_id, columns, user_id, root_post_id)

            mm_patch_post(
                post_id,
                message=f'Доска для задачи "{task_title}": {board_title}',
                attachments=[]
            )

            resp = mm_post(
                channel_id,
                message=f'Выберите колонку для задачи "{task_title}"',
                attachments=attachments,
                root_id=root_post_id
            )
            set_state(user_id, root_post_id, {
                "post_ids": state.get("post_ids", []) + [resp["id"]]
            })

        # ---------- ВЫБОР КОЛОНКИ ----------
        elif step == "CHOOSE_COLUMN":
            column_id = extract_selected_value(data)
            if not column_id:
                return "", 200

            state = get_state(user_id, root_post_id) or {}
            project_id = state.get("project_id") or context.get("project_id")
            board_id = state.get("board_id") or context.get("board_id")
            column_options = state.get("column_options", {})
            column_title = column_options.get(column_id, "без названия")

            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_COLUMN",
                "project_id": project_id,
                "board_id": board_id,
                "column_id": column_id,
            })

            users = yg_get_project_users(project_id)
            attachments = build_assignee_select(
                task_title, project_id, board_id, column_id, users, user_id, root_post_id
            )

            mm_patch_post(
                post_id,
                message=f'Колонка для задачи "{task_title}": {column_title}',
                attachments=[]
            )

            resp = mm_post(
                channel_id,
                message=f'Кого назначить ответственным за задачу "{task_title}"?',
                attachments=attachments,
                root_id=root_post_id
            )
            set_state(user_id, root_post_id, {
                "post_ids": state.get("post_ids", []) + [resp["id"]]
            })

        # ---------- ВЫБОР ИСПОЛНИТЕЛЯ ----------
        elif step == "CHOOSE_ASSIGNEE":
            assignee_id = extract_selected_value(data)
            if not assignee_id:
                return "", 200

            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_ASSIGNEE",
                "assignee_id": assignee_id,
            })

            assignee_name = assignee_id
            project_id = state.get("project_id") or context.get("project_id")

            try:
                users = yg_get_project_users(project_id)
                for u in users:
                    if u.get("id") == assignee_id:
                        assignee_name = u.get("realName") or u.get("email") or assignee_id
                        break
            except Exception as e:
                print("Error fetching project users:", e)

            state = set_state(user_id, root_post_id, {
                "assignee_name": assignee_name,
            })

            meta = {
                "project_id": state.get("project_id"),
                "board_id": state.get("board_id"),
                "column_id": state.get("column_id"),
                "assignee_id": assignee_id,
            }
            attachments = build_deadline_buttons(task_title, meta, user_id, root_post_id)

            mm_patch_post(
                post_id,
                message=f'Ответственный для задачи "{task_title}": {assignee_name}',
                attachments=[]
            )

            resp = mm_post(
                channel_id,
                message=f'Какую дату дедлайна поставить для задачи "{task_title}"?',
                attachments=attachments,
                root_id=root_post_id
            )

            set_state(user_id, root_post_id, {
                "post_ids": state.get("post_ids", []) + [resp["id"]]
            })

        # ---------- ВЫБОР ДЕДЛАЙНА ----------
        elif step == "CHOOSE_DEADLINE":
            deadline_choice = context.get("deadline_choice")
            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_DEADLINE",
                "deadline_choice": deadline_choice,
            })

            if deadline_choice == "custom":
                mm_patch_post(
                    post_id,
                    message=(
                        f'Введите дату дедлайна для задачи "{task_title}" '
                        f'в этом треде в формате YYYY-MM-DD, например 2025-11-13.'
                    ),
                    attachments=[]
                )
            elif deadline_choice == "none":
                state = set_state(user_id, root_post_id, {
                    "deadline": None,
                })
                create_task_and_update_post(task_title, state, user_id, post_id)
            else:
                deadline_date = calc_deadline(deadline_choice)
                state = set_state(user_id, root_post_id, {
                    "deadline": deadline_date,
                })
                create_task_and_update_post(task_title, state, user_id, post_id)

        # ---------- ОТМЕНА ----------
        elif step == "CANCEL":
            state = get_state(user_id, root_post_id) or {}
            channel_id_state = state.get("channel_id", channel_id)
            post_ids = state.get("post_ids", [])
            task_title = context.get("task_title") or state.get("task_title") or "Без названия"

            for pid in post_ids:
                try:
                    requests.delete(
                        f"{MM_URL}/api/v4/posts/{pid}",
                        headers=mm_headers,
                        timeout=5
                    )
                except Exception as del_e:
                    print("Error deleting post", pid, del_e)

            clear_state(user_id, root_post_id)

            if task_title == "Без названия" or not task_title:
                msg = "Хорошо, создание задачи отменено."
            else:
                msg = f'Хорошо, создание задачи "{task_title}" отменено.'

            mm_post(
                channel_id_state,
                message=msg,
                root_id=root_post_id
            )

            return "", 200

        # ---------- РУЧНОЕ ЗАВЕРШЕНИЕ ДИАЛОГА ----------
        elif step == "FINISH":
            st = get_state(user_id, root_post_id) or {}

            project_title = st.get("project_title", "без названия")
            board_title = st.get("board_title", "без названия")
            assignee_name = st.get("assignee_name", "не указан")
            deadline_str = st.get("deadline_str", "без дедлайна")
            task_url = st.get("task_url", "")
            channel_id_state = st.get("channel_id", channel_id)

            summary = build_task_summary(
                task_title=task_title,
                project_title=project_title,
                board_title=board_title,
                assignee_name=assignee_name,
                deadline_str=deadline_str,
                task_url=task_url,
            )

            # summary → в тред + в общий канал
            send_task_summary(
                channel_id_state,
                root_post_id,
                summary,
                to_channel=True,
                auto=False,
            )

            # 3) Обновляем интерактивный пост
            mm_patch_post(
                post_id,
                message=f'Диалог по задаче "{task_title}" завершён.',
                attachments=[]
            )

            clear_state(user_id, root_post_id)
            return "", 200

    except Exception as e:
        print("Error in mm_actions:", e)
        try:
            mm_post(channel_id, f"💥 Ошибка обработки действия бота: {e}", root_id=root_post_id)
        except Exception:
            pass

    return "", 200


# ---------------------------------------------------------------------------
#  Создание задачи в YouGile + обновление поста с кнопкой "Завершить"
# ---------------------------------------------------------------------------

def create_task_and_update_post(task_title, state, user_id, post_id):
    """
    Создаёт задачу в YouGile на основе state и:

    1) Обновляет исходное сообщение с кнопками дедлайна на:
       "Дедлайн для задачи "<название>": <выбор> (<дата DD.MM.YYYY>)"
       (без attachments).
    2) Отправляет отдельное сообщение в тред:
       "✅ Задача "<название>" создана.\nСсылка: ..."
       с action "Завершить" и приглашением добавить комментарии/файлы.
    3) Обновляет state для FINISH и автозавершения.
    """
    mm_user = mm_get_user(user_id)
    first_name = mm_user.get("first_name", "").strip()
    last_name = mm_user.get("last_name", "").strip()
    username = mm_user.get("username", "")
    full_name = (first_name + " " + last_name).strip() or username

    column_id = state.get("column_id")
    assignee_id = state.get("assignee_id")
    deadline = state.get("deadline")

    description = f"Создано из Loop пользователем {full_name} (@{username})"

    task = yg_create_task(
        task_title,
        column_id,
        description=description,
        assignee_id=assignee_id,
        deadline=deadline
    )
    task_id = task.get("id")

    task_project_id = task.get("idTaskProject") or task.get("idTaskCommon")
    try:
        if task_id and not task_project_id:
            full_task = yg_get_task(task_id)
            task_project_id = full_task.get("idTaskProject") or full_task.get("idTaskCommon")
    except Exception as e:
        print("Error fetching full YouGile task:", e)

    project_title = state.get("project_title")
    project_slug = slugify_title(project_title) if project_title else ""
    team_id = YOUGILE_TEAM_ID or YOUGILE_COMPANY_ID

    if team_id and project_slug and task_project_id:
        task_url = f"https://ru.yougile.com/team/{team_id}/{project_slug}#{task_project_id}"
    elif team_id:
        task_url = f"https://ru.yougile.com/team/{team_id}"
    else:
        task_url = "https://ru.yougile.com/"

    deadline_choice = state.get("deadline_choice")
    deadline_display = state.get("deadline_display")

    deadline_human, deadline_str = format_deadline(
        deadline_choice,
        deadline,
        deadline_display,
    )

    meta = {
        "yougile_task_id": task_id,
    }

    root_post_id = state.get("root_post_id")
    channel_id_state = state.get("channel_id")
    post_ids = state.get("post_ids") or []

    # 1) Обновляем исходное сообщение (где были кнопки дедлайна).
    #    ВАЖНО: attachments убираем — на этом сообщении больше ничего интерактивного не нужно.
    mm_patch_post(
        post_id,
        message=f'Дедлайн для задачи "{task_title}": {deadline_str}.',
        attachments=[]
    )

    # 2) Отдельное сообщение "✅ Задача создана" в тред — здесь будет action "Завершить".
    if channel_id_state:
        success_msg = (
            f'✅ Задача "{task_title}" создана.\n'
            f"Ссылка: {task_url}"
        )

        attachments = build_finish_buttons(task_title, task_url, user_id, root_post_id, meta)

        resp = mm_post(
            channel_id_state,
            message=success_msg,
            attachments=attachments,
            root_id=root_post_id
        )
        # Добавим этот пост в список служебных, чтобы CANCEL мог их удалить при необходимости
        post_ids = post_ids + [resp["id"]]

    # 3) Обновляем state для FINISH / автозавершения
    set_state(user_id, root_post_id, {
        "step": "OPTIONAL_ATTACH",
        "yougile_task_id": task_id,
        "task_url": task_url,
        "deadline_str": deadline_str,
        "post_ids": post_ids,
    })

def auto_finish_dialog(user_id, root_post_id):
    """
    Автозавершение диалога, если пользователь не нажал "Завершить"
    в течение AUTO_FINISH_TIMEOUT_MINUTES.
    """
    st = get_state(user_id, root_post_id) or {}
    if not st:
        return

    task_title = st.get("task_title", "Без названия")
    project_title = st.get("project_title", "без названия")
    board_title = st.get("board_title", "без названия")
    assignee_name = st.get("assignee_name", "не указан")
    deadline_str = st.get("deadline_str", "без дедлайна")
    task_url = st.get("task_url", "")
    channel_id_state = st.get("channel_id")

    if not channel_id_state:
        return

    summary = build_task_summary(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        assignee_name=assignee_name,
        deadline_str=deadline_str,
        task_url=task_url,
    )

    # summary → только в тред + "(Автозавершение диалога)"
    send_task_summary(
        channel_id_state,
        root_post_id,
        summary,
        to_channel=False,
        auto=True,
    )

    clear_state(user_id, root_post_id)
    
def start_task_creation(user_id, channel_id, root_id, title):
    """
    Запуск мастера создания задачи:
    - определяем проекты YouGile, к которым пользователь имеет доступ
    - учитываем проект по умолчанию для канала (если задан)
    - дальше: выбор доски, колонки, исполнителя, дедлайна
    """
    allowed_projects = get_allowed_projects_for_mm_user(user_id)

    # 4) нет доступных проектов
    if not allowed_projects:
        no_access_msg = (
            "Извините, но кажется у вас нет доступа к проектам в нашей доске YouGile.\n"
            "Обратитесь за помощью к администратору.\n"
            "Мне очень жаль :cry:"
        )
        mm_post(
            channel_id,
            message=no_access_msg,
            root_id=root_id
        )
        return

    # 5) проверяем проект по умолчанию для этого канала
    default_entry = get_default_project_for_channel(channel_id)
    default_project = None
    if default_entry:
        for p in allowed_projects:
            if p.get("id") == default_entry.get("project_id"):
                default_project = p
                break

    if default_project:
        project_id = default_project["id"]
        project_title = default_project.get("title", "без названия")

        state = set_state(user_id, root_id, {
            "step": "CHOOSE_PROJECT",
            "task_title": title,
            "root_post_id": root_id,
            "channel_id": channel_id,
            "project_id": project_id,
            "project_title": project_title,
            "post_ids": [],
        })

        boards = yg_get_boards(project_id)

        if not boards:
            mm_post(
                channel_id,
                message=f'В проекте "{project_title}" нет досок, задачу создать нельзя.',
                root_id=root_id
            )
            return

        if len(boards) == 1:
            board = boards[0]
            board_id = board["id"]
            board_title = board.get("title", "без названия")

            state = set_state(user_id, root_id, {
                "step": "CHOOSE_BOARD",
                "board_id": board_id,
                "board_title": board_title,
            })

            columns = yg_get_columns(board_id)
            if not columns:
                mm_post(
                    channel_id,
                    message=f'На доске "{board_title}" нет колонок, задачу создать нельзя.',
                    root_id=root_id
                )
                return

            column_options = {c["id"]: c.get("title", "Без имени") for c in columns if c.get("id")}
            set_state(user_id, root_id, {"column_options": column_options})

            attachments = build_column_select_for_task(title, project_id, board_id, columns, user_id, root_id)
            resp = mm_post(
                channel_id,
                message=f'Выберите колонку для задачи "{title}"',
                attachments=attachments,
                root_id=root_id
            )
            set_state(user_id, root_id, {
                "post_ids": state.get("post_ids", []) + [resp["id"]]
            })
        else:
            board_options = {b["id"]: b.get("title", "Без имени") for b in boards if b.get("id")}
            set_state(user_id, root_id, {"board_options": board_options})

            attachments = build_board_select_for_task(title, project_id, boards, user_id, root_id)
            resp = mm_post(
                channel_id,
                message=f'Выберите доску для задачи "{title}"',
                attachments=attachments,
                root_id=root_id
            )
            set_state(user_id, root_id, {
                "post_ids": state.get("post_ids", []) + [resp["id"]]
            })

        return

    # нет проекта по умолчанию — выбираем проект select'ом
    project_options = {
        p["id"]: p.get("title", "Без имени")
        for p in allowed_projects
        if p.get("id")
    }

    with STATE_LOCK:
        STATE[(user_id, root_id)] = {
            "step": "CHOOSE_PROJECT",
            "task_title": title,
            "root_post_id": root_id,
            "channel_id": channel_id,
            "post_ids": [],
            "project_options": project_options,
        }

    attachments = build_project_select_for_task(title, allowed_projects, user_id, root_id)
    resp = mm_post(
        channel_id,
        message=f'Выберите проект для задачи "{title}"',
        attachments=attachments,
        root_id=root_id
    )
    set_state(user_id, root_id, {
        "post_ids": [resp["id"]]
    })


# ---------------------------------------------------------------------------
#  WebSocket-бот Loop (Mattermost)
# ---------------------------------------------------------------------------

def run_ws_bot():
    """Подключение к WebSocket и обработка событий posted."""
    ws_url = MM_URL.replace("https", "wss").replace("http", "ws") + "/api/v4/websocket"
    seq = 1

    while True:
        try:
            print(f"Connecting to Mattermost WS {ws_url}")
            ws = create_connection(ws_url)

            auth_msg = {
                "seq": seq,
                "action": "authentication_challenge",
                "data": {
                    "token": MM_BOT_TOKEN
                }
            }
            seq += 1
            ws.send(json.dumps(auth_msg))
            print("Authenticated to Mattermost WS")

            while True:
                msg = ws.recv()
                if not msg:
                    continue

                try:
                    data = json.loads(msg)
                except Exception as e:
                    print("WS json error:", e, msg[:200])
                    continue

                event = data.get("event")
                if event:
                    print("WS event:", event, "data:", data.get("data", {}))

                # --- Обрабатываем только posted ---
                if event != "posted":
                    continue

                post = decode_mm_post_from_event(data)
                if not post:
                    continue

                post_type = post.get("type") or ""
                props = post.get("props") or {}
                channel_id = post.get("channel_id")
                user_id = post.get("user_id")
                message = post.get("message", "")
                root_id = post.get("root_id") or post.get("id")
                bot_id = get_bot_user_id()

                # ---------- 0) Системные события добавления/удаления бота из канала ----------
                # Бота ДОБАВИЛИ в канал → спросить про проект по умолчанию
                if post_type == "system_add_to_channel":
                    added_user_id = props.get("addedUserId")
                    # кто добавил бота
                    adder_user_id = props.get("userId") or props.get("user_id")
                    if bot_id and added_user_id == bot_id and channel_id:
                        prompt = (
                            "Я только что добавлен в этот канал.\n"
                            "Хотите установить проект по умолчанию для этого чата?"
                        )
                        attachments = [{
                            "text": "Выберите действие:",
                            "actions": [
                                {
                                    "id": "defaultProjectYes",
                                    "name": "Да, выбрать проект",
                                    "type": "button",
                                    "style": "primary",
                                    "integration": {
                                        "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                                        "context": {
                                            "step": "DEFAULT_PROJECT_PROMPT_YES",
                                        }
                                    }
                                },
                                {
                                    "id": "defaultProjectNo",
                                    "name": "Нет, буду задавать проект отдельно",
                                    "type": "button",
                                    "integration": {
                                        "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                                        "context": {
                                            "step": "DEFAULT_PROJECT_PROMPT_NO",
                                        }
                                    }
                                },
                            ]
                        }]

                        mm_post(
                            channel_id,
                            message=prompt,
                            attachments=attachments,
                            root_id=None
                        )
                    continue

                # Бота УДАЛИЛИ из канала → чистим мэппинг
                if post_type == "system_remove_from_channel":
                    removed_user_id = props.get("removedUserId")
                    if bot_id and removed_user_id == bot_id and channel_id:
                        print(f"Bot removed from channel {channel_id}, deleting default project mapping")
                        delete_default_project_for_channel(channel_id)
                    continue

                # ---------- 1) Старт диалога: упоминание бота ----------
                if f"@{MM_BOT_USERNAME}" in (message or "").lower():
                    title = parse_create_command(message, MM_BOT_USERNAME)

                    if title:
                        # быстрая команда: сразу запускаем мастер
                        start_task_creation(user_id, channel_id, root_id, title)
                        continue

                    # иначе показываем главное меню
                    attachments = build_main_menu_attachments(user_id, root_id)
                    mm_post(
                        channel_id,
                        message="Привет! Вот что я умею:",
                        attachments=attachments,
                        root_id=root_id
                    )
                    continue

                # ---------- 2) Ожидание кастомной даты дедлайна ----------
                st = get_state(user_id, root_id)
                if st and st.get("step") == "CHOOSE_DEADLINE" and st.get("deadline_choice") == "custom":
                    text = (message or "").strip()
                    if text:
                        try:
                            d = datetime.strptime(text, "%Y-%m-%d").date()
                        except ValueError:
                            mm_post(
                                channel_id,
                                message=(
                                    f'Не удалось разобрать дату "{text}". '
                                    f'Используйте формат YYYY-MM-DD, например 2025-11-13.'
                                ),
                                root_id=root_id
                            )
                            continue

                        st = set_state(user_id, root_id, {
                            "deadline": d,
                            "deadline_display": text,  # запоминаем, что ввёл пользователь
                        })
                        task_title = st.get("task_title", "Без названия")

                        post_ids = st.get("post_ids") or []
                        target_post_id = post_ids[-1] if post_ids else None

                        if target_post_id:
                            create_task_and_update_post(task_title, st, user_id, target_post_id)
                        else:
                            mm_post(
                                channel_id,
                                message=f'✅ Задача "{task_title}" создана (кастомный дедлайн).',
                                root_id=root_id
                            )
                        continue

                # ---------- 2b) Ожидание названия задачи после MENU_CREATE_TASK ----------
                st = get_state(user_id, root_id)
                if st and st.get("step") == "ASK_TASK_TITLE":
                    title_text = (message or "").strip()
                    if not title_text:
                        continue

                    st = set_state(user_id, root_id, {
                        "task_title": title_text
                    })

                    ask_post_id = st.get("ask_title_post_id")
                    if ask_post_id:
                        try:
                            mm_patch_post(
                                ask_post_id,
                                message=f'Создаём задачу "{title_text}"',
                                attachments=[]
                            )
                        except Exception as e:
                            print("Error patching ask_title_post:", e)

                    start_task_creation(user_id, channel_id, root_id, title_text)
                    continue

                # ---------- 3) Дополнительные комментарии / файлы после создания задачи ----------
                st = get_state(user_id, root_id)
                if st and st.get("step") == "OPTIONAL_ATTACH":
                    task_id = st.get("yougile_task_id")
                    if not task_id:
                        continue

                    sent_anything = False

                    # данные пользователя Loop для префикса
                    try:
                        mm_user = mm_get_user(user_id)
                    except Exception as e:
                        print("Error fetching MM user for comment prefix:", e)
                        mm_user = {}

                    first_name = (mm_user.get("first_name") or "").strip()
                    last_name = (mm_user.get("last_name") or "").strip()
                    username = mm_user.get("username") or ""
                    full_name = (first_name + " " + last_name).strip() or username or "неизвестный пользователь"

                    def prefix_text(text: str) -> str:
                        return f"Пользователь {full_name} (@{username}) написал: {text}"

                    # 3.1. Файлы
                    file_ids = post.get("file_ids") or []
                    for fid in file_ids:
                        try:
                            file_bytes = mm_get_file(fid)
                            info = mm_get_file_info(fid)
                            filename = info.get("name") or info.get("id") or "file"
                            mimetype = info.get("mime_type") or "application/octet-stream"

                            yg_file_url = yg_upload_file(file_bytes, filename, mimetype)
                            file_cmd = f"/root/#file:{yg_file_url}"
                            chat_text = prefix_text(file_cmd)

                            yg_send_chat_message(task_id, chat_text)
                            sent_anything = True
                        except Exception as e:
                            print("Error sending file to YouGile chat:", e)

                    # 3.2. Текст
                    text = (message or "").strip()
                    if text:
                        try:
                            chat_text = prefix_text(text)
                            yg_send_chat_message(task_id, chat_text)
                            sent_anything = True
                        except Exception as e:
                            print("Error sending text comment to YouGile chat:", e)

                    # 3.3. Ставим реакцию
                    if sent_anything:
                        try:
                            mm_add_reaction(user_id, post.get("id"), "white_check_mark")
                        except Exception as e:
                            print("Error adding MM reaction:", e)

                        set_state(user_id, root_id, {})

        except WebSocketConnectionClosedException:
            print("WS closed, reconnecting in 3s...")
            time.sleep(3)
        except Exception as e:
            print("WS error:", e)
            time.sleep(5)

def start_ws_thread():
    """Стартуем отдельный поток для WebSocket-бота."""
    t = threading.Thread(target=run_ws_bot, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
#  Авто-уборка зависших диалогов (автозавершение)
# ---------------------------------------------------------------------------

def auto_cleanup_loop():
    """
    Раз в минуту просматривает STATE и автозавершает диалоги,
    которые давно в состоянии OPTIONAL_ATTACH.
    """
    while True:
        try:
            now = time.time()
            with STATE_LOCK:
                items = list(STATE.items())
            for (user_id, root_post_id), st in items:
                if st.get("step") != "OPTIONAL_ATTACH":
                    continue
                updated_at = st.get("updated_at") or st.get("created_at")
                if not updated_at:
                    continue
                if now - updated_at > AUTO_FINISH_TIMEOUT_MINUTES * 60:
                    print(f"Auto-finishing dialog for user={user_id}, root={root_post_id}")
                    try:
                        auto_finish_dialog(user_id, root_post_id)
                    except Exception as e:
                        print("Error in auto_finish_dialog:", e)
        except Exception as e:
            print("Error in auto_cleanup_loop:", e)
        time.sleep(60)


def start_cleanup_thread():
    """Стартуем отдельный поток авто-уборки."""
    t = threading.Thread(target=auto_cleanup_loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
#  MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_channel_map()
    start_ws_thread()
    start_cleanup_thread()
    app.run(host="0.0.0.0", port=8000)