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
YOUGILE_BASE_URL = os.getenv("YOUGILE_BASE_URL", "https://yougile.com/api-v2").rstrip("/")
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


def yg_get_project_users(project_id):
    """GET /users?projectId=... — список пользователей проекта."""
    r = requests.get(
        f"{YOUGILE_BASE_URL}/users",
        headers=yg_headers,
        params={"projectId": project_id},
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


# ---------------------------------------------------------------------------
#  Построение интерактивных сообщений (attachments) для шагов мастера
# ---------------------------------------------------------------------------

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


def build_project_buttons(task_title, projects, user_id, root_post_id):
    """Кнопки выбора проекта."""
    actions = []
    for idx, p in enumerate(projects):
        actions.append({
            "id": f"project{idx}",  # важно: без дефисов и подчёркиваний
            "name": p.get("title", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_PROJECT",
                    "task_title": task_title,
                    "project_id": p["id"],
                    "project_title": p.get("title", "Без имени"),
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    add_cancel_action(actions, task_title, root_post_id, user_id)
    return [{
        "text": "Проекты:",
        "actions": actions
    }]


def build_board_buttons(task_title, project_id, boards, user_id, root_post_id):
    """Кнопки выбора доски."""
    actions = []
    for idx, b in enumerate(boards):
        actions.append({
            "id": f"board{idx}",
            "name": b.get("title", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_BOARD",
                    "task_title": task_title,
                    "project_id": project_id,
                    "board_id": b["id"],
                    "board_title": b.get("title", "Без имени"),
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    add_cancel_action(actions, task_title, root_post_id, user_id)
    return [{
        "text": "Доски:",
        "actions": actions
    }]


def build_column_buttons(task_title, project_id, board_id, columns, user_id, root_post_id):
    """Кнопки выбора колонки."""
    actions = []
    for idx, c in enumerate(columns):
        actions.append({
            "id": f"column{idx}",
            "name": c.get("title", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_COLUMN",
                    "task_title": task_title,
                    "project_id": project_id,
                    "board_id": board_id,
                    "column_id": c["id"],
                    "column_title": c.get("title", "Без имени"),
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    add_cancel_action(actions, task_title, root_post_id, user_id)
    return [{
        "text": "Колонки:",
        "actions": actions
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
            f"Задача создана. Ссылка: {task_url}\n"
            f"Можете написать дополнительный комментарий в этом треде, "
            f'а затем нажать "Завершить".'
        ),
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
    context = data.get("context", {})
    step = context.get("step")
    user_id = context.get("user_id")
    root_post_id = context.get("root_post_id")
    post_id = data.get("post_id")
    channel_id = data.get("channel_id")

    if not (step and user_id and root_post_id and post_id and channel_id):
        return "", 200

    state = get_state(user_id, root_post_id) or {}
    task_title = context.get("task_title") or state.get("task_title", "Без названия")

    try:
        # ---------- ВЫБОР ПРОЕКТА ----------
        if step == "CHOOSE_PROJECT":
            project_id = context["project_id"]
            project_title = context.get("project_title", "без названия")

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

            if len(boards) <= 1:
                board = boards[0]
                board_id = board["id"]
                board_title = board.get("title", "без названия")

                state = set_state(user_id, root_post_id, {
                    "board_id": board_id,
                    "board_title": board_title,
                })

                columns = yg_get_columns(board_id)
                attachments = build_column_buttons(
                    task_title, project_id, board_id, columns, user_id, root_post_id
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
            else:
                attachments = build_board_buttons(
                    task_title, project_id, boards, user_id, root_post_id
                )
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
            project_id = context["project_id"]
            board_id = context["board_id"]
            board_title = context.get("board_title", "без названия")

            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_BOARD",
                "project_id": project_id,
                "board_id": board_id,
                "board_title": board_title,
            })

            columns = yg_get_columns(board_id)
            attachments = build_column_buttons(
                task_title, project_id, board_id, columns, user_id, root_post_id
            )

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
            project_id = context["project_id"]
            board_id = context["board_id"]
            column_id = context["column_id"]
            column_title = context.get("column_title", "без названия")

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
            selected = (data.get("context") or {}).get("selected_option") or (data.get("data") or {}).get("selected_option")
            if isinstance(selected, dict):
                assignee_id = selected.get("value")
            else:
                assignee_id = selected
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
                state = set_state(user_id, root_post_id, {"deadline": None})
                create_task_and_update_post(task_title, state, user_id, post_id)
            else:
                deadline_date = calc_deadline(deadline_choice)
                state = set_state(user_id, root_post_id, {"deadline": deadline_date})
                create_task_and_update_post(task_title, state, user_id, post_id)

        # ---------- ОТМЕНА ----------
        elif step == "CANCEL":
            state = get_state(user_id, root_post_id) or {}
            channel_id_state = state.get("channel_id", channel_id)
            post_ids = state.get("post_ids", [])

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

            mm_post(
                channel_id_state,
                message=(
                    f'Хорошо, создание задачи "{task_title}" отменено. '
                    f'Если передумаете — обратитесь ко мне снова.'
                ),
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

            summary = (
                f'Задача "{task_title}" создана в проекте: {project_title}, '
                f'доска: {board_title}, ответственный: {assignee_name}, '
                f'дедлайн: {deadline_str}.'
            )
            if task_url:
                summary += f"\nСсылка: {task_url}"

            mm_post(
                channel_id_state,
                message=summary,
                root_id=root_post_id
            )

            mm_post(
                channel_id_state,
                message=summary
            )

            mm_patch_post(
                post_id,
                message=f'Диалог по задаче "{task_title}" завершён.',
                attachments=[]
            )

            clear_state(user_id, root_post_id)

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
    Создаёт задачу в YouGile на основе state и обновляет сообщение
    в Loop (там, где были кнопки дедлайна) на "✅ Задача создана".
    Также записывает всё нужное для финального резюме (FINISH / автозавершение).
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

    if deadline:
        deadline_str = deadline.strftime("%d.%m.%Y")
    else:
        deadline_str = "без дедлайна"

    meta = {
        "yougile_task_id": task_id,
    }
    attachments = build_finish_buttons(task_title, task_url, user_id, state.get("root_post_id"), meta)

    mm_patch_post(
        post_id,
        message=(
            f'✅ Задача "{task_title}" создана.\n'
            f"Ссылка: {task_url}\n"
            f'Можете оставить дополнительный комментарий в треде, затем нажмите "Завершить".'
        ),
        attachments=attachments
    )

    set_state(user_id, state.get("root_post_id"), {
        "step": "OPTIONAL_ATTACH",
        "yougile_task_id": task_id,
        "task_url": task_url,
        "deadline_str": deadline_str,
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

    summary = (
        f'Задача "{task_title}" создана в проекте: {project_title}, '
        f'доска: {board_title}, ответственный: {assignee_name}, '
        f'дедлайн: {deadline_str}.'
    )
    if task_url:
        summary += f"\nСсылка: {task_url}"

    mm_post(
        channel_id_state,
        message=summary,
        root_id=root_post_id
    )

    mm_post(
        channel_id_state,
        message=f"(Автозавершение) {summary}"
    )

    clear_state(user_id, root_post_id)


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
                data = json.loads(msg)

                if data.get("event") != "posted":
                    continue

                post = decode_mm_post_from_event(data)
                if not post:
                    continue

                channel_id = post.get("channel_id")
                user_id = post.get("user_id")
                message = post.get("message", "")
                root_id = post.get("root_id") or post.get("id")

                # ---------- 1) Старт диалога: упоминание бота ----------
                if f"@{MM_BOT_USERNAME}" in message.lower():
                    title = parse_create_command(message, MM_BOT_USERNAME)

                    # Если команда непонятна — показываем хелп
                    if not title:
                        help_text = (
                            ":huh: Привет!\n"
                            "Я пока глупенький и умею работать только со следующей командой:\n"
                            "- `создай задачу <название задачи>`\n\n"
                            "Попробуйте ещё раз, пожалуйста, используя команду выше.\n"
                            "Спасибо! :thanks:"
                        )
                        mm_post(
                            channel_id,
                            message=help_text,
                            root_id=root_id
                        )
                        continue

                    # 1.1. Получаем email пользователя из Loop
                    try:
                        mm_user = mm_get_user(user_id)
                        mm_email = (mm_user.get("email") or "").strip().lower()
                    except Exception as e:
                        print("Error fetching MM user for project filter:", e)
                        mm_email = ""

                    # 1.2. Получаем все проекты из YouGile
                    try:
                        all_projects = yg_get_projects()
                    except Exception as e:
                        print("Error fetching YouGile projects:", e)
                        all_projects = []

                    # 1.3. Фильтруем проекты по участию пользователя (по email)
                    allowed_projects = []

                    if mm_email:
                        for p in all_projects:
                            project_id = p.get("id")
                            if not project_id:
                                continue
                            try:
                                users = yg_get_project_users(project_id)
                            except Exception as e:
                                print(f"Error fetching users for project {project_id}:", e)
                                continue

                            for u in users:
                                u_email = (u.get("email") or "").strip().lower()
                                if u_email and u_email == mm_email:
                                    allowed_projects.append(p)
                                    break
                    else:
                        # У пользователя нет email в Loop — считаем, что нет доступа ни к одному проекту
                        allowed_projects = []

                    # 1.4. Если нет ни одного доступного проекта — завершаем диалог
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
                        continue

                    # 1.5. Если проекты есть — запускаем мастер, как раньше
                    with STATE_LOCK:
                        STATE[(user_id, root_id)] = {
                            "step": "CHOOSE_PROJECT",
                            "task_title": title,
                            "root_post_id": root_id,
                            "channel_id": channel_id,
                            "post_ids": [],
                        }

                    attachments = build_project_buttons(title, allowed_projects, user_id, root_id)
                    resp = mm_post(
                        channel_id,
                        message=f'Выберите проект для задачи "{title}"',
                        attachments=attachments,
                        root_id=root_id
                    )
                    set_state(user_id, root_id, {
                        "post_ids": [resp["id"]]
                    })
                    continue

                # ---------- 2) Ожидание кастомной даты дедлайна ----------
                st = get_state(user_id, root_id)
                if st and st.get("step") == "CHOOSE_DEADLINE" and st.get("deadline_choice") == "custom":
                    text = message.strip()
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

                        st = set_state(user_id, root_id, {"deadline": d})
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

                    # 3.1. Отправляем прикреплённые файлы (если есть)
                    file_ids = post.get("file_ids") or []
                    for fid in file_ids:
                        try:
                            file_bytes = mm_get_file(fid)
                            info = mm_get_file_info(fid)
                            filename = info.get("name") or info.get("id") or "file"
                            mimetype = info.get("mime_type") or "application/octet-stream"

                            # загружаем файл в YouGile и получаем относительный url
                            yg_file_url = yg_upload_file(file_bytes, filename, mimetype)

                            # формируем текст для чата: /root/#file:/user-data/...
                            file_cmd = f"/root/#file:{yg_file_url}"
                            chat_text = prefix_text(file_cmd)

                            yg_send_chat_message(task_id, chat_text)
                            sent_anything = True
                        except Exception as e:
                            print("Error sending file to YouGile chat:", e)

                    # 3.2. Отправляем обычный текст, если он есть
                    text = (message or "").strip()
                    if text:
                        try:
                            chat_text = prefix_text(text)
                            yg_send_chat_message(task_id, chat_text)
                            sent_anything = True
                        except Exception as e:
                            print("Error sending text comment to YouGile chat:", e)

                    # 3.3. Если хоть что-то удалось отправить — ставим реакцию ✅ в Loop
                    if sent_anything:
                        try:
                            mm_add_reaction(user_id, post.get("id"), "white_check_mark")
                        except Exception as e:
                            print("Error adding MM reaction:", e)

                        # обновляем updated_at, чтобы авто-завершение шло от последнего действия
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
    start_ws_thread()
    start_cleanup_thread()
    app.run(host="0.0.0.0", port=8000)