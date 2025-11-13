import os
import json
import time
import threading
import re
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from websocket import create_connection, WebSocketConnectionClosedException

from urllib.parse import quote

def slugify_title(title: str) -> str:
    s = (title or "").strip()
    # пробелы → дефисы
    s = re.sub(r"\s+", "-", s)
    # схлопываем повторяющиеся дефисы
    s = re.sub(r"-+", "-", s)
    return quote(s)

# ---------- ENV ----------
MM_URL = os.getenv("MM_URL").rstrip("/")
MM_BOT_TOKEN = os.getenv("MM_BOT_TOKEN")
MM_BOT_USERNAME = os.getenv("MM_BOT_USERNAME", "yougile_bot").lower()  # без @
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL").rstrip("/")

YOUGILE_COMPANY_ID = os.getenv("YOUGILE_COMPANY_ID")
YOUGILE_API_KEY = os.getenv("YOUGILE_API_KEY")
YOUGILE_BASE_URL = os.getenv("YOUGILE_BASE_URL", "https://yougile.com/api-v2").rstrip("/")
YOUGILE_TEAM_ID = os.getenv("YOUGILE_TEAM_ID")
if not YOUGILE_TEAM_ID and YOUGILE_COMPANY_ID:
    # Берём последний сегмент UUID как team-id (как в твоём URL)
    YOUGILE_TEAM_ID = YOUGILE_COMPANY_ID.split("-")[-1]

if not (MM_URL and MM_BOT_TOKEN and YOUGILE_COMPANY_ID and YOUGILE_API_KEY and BOT_PUBLIC_URL):
    print("ERROR: some required env vars are missing")
    # не выходим, чтобы было видно в логах, но всё равно работать не будет

# ---------- HTTP HEADERS ----------
mm_headers = {
    "Authorization": f"Bearer {MM_BOT_TOKEN}",
    "Content-Type": "application/json",
}

yg_headers = {
#    "X-Company-Id": YOUGILE_COMPANY_ID,
#    "X-Api-Key": YOUGILE_API_KEY,
    "Authorization": f"Bearer {YOUGILE_API_KEY}",
    "Content-Type": "application/json",
}

# ---------- STATE ----------
# key: (user_id, root_post_id) -> dict
STATE = defaultdict(dict)
STATE_LOCK = threading.Lock()

# ---------- UTILS ----------

def set_state(user_id, root_post_id, data: dict):
    with STATE_LOCK:
        s = STATE[(user_id, root_post_id)]
        s.update(data)
        return s


def get_state(user_id, root_post_id):
    with STATE_LOCK:
        return STATE.get((user_id, root_post_id))


def clear_state(user_id, root_post_id):
    with STATE_LOCK:
        STATE.pop((user_id, root_post_id), None)


def mm_get_user(user_id):
    url = f"{MM_URL}/api/v4/users/{user_id}"
    r = requests.get(url, headers=mm_headers, timeout=10)
    r.raise_for_status()
    return r.json()


def mm_post(channel_id, message, attachments=None, root_id=None):
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
    payload = {"id": post_id}
    if message is not None:
        payload["message"] = message
    if attachments is not None:
        payload.setdefault("props", {})
        payload["props"]["attachments"] = attachments

    r = requests.put(f"{MM_URL}/api/v4/posts/{post_id}", headers=mm_headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def decode_mm_post_from_event(data):
    post_raw = data.get("data", {}).get("post")
    if not post_raw:
        return None
    return json.loads(post_raw)


def parse_create_command(message: str, bot_username: str):
    # ожидаем: @yougile_bot создай задачу <название>
    text = message.strip()
    # убираем упоминание
    mention_pattern = rf"@{re.escape(bot_username)}"
    text = re.sub(mention_pattern, "", text, flags=re.IGNORECASE).strip()

    # ищем "создай задачу"
    pattern = r"^создай\s+задачу\s+(.+)$"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None
    title = m.group(1).strip()
    return title


# ---------- YOUGILE API WRAPPERS ----------
# NB: эндпоинты для boards/columns/users могут отличаться у тебя в доке –
# их удобно вынести в константы и поправить при необходимости.

def yg_get_projects():
    r = requests.get(f"{YOUGILE_BASE_URL}/projects", headers=yg_headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data.get("content", [])


def yg_get_boards(project_id):
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

def yg_get_task(task_id):
    r = requests.get(
        f"{YOUGILE_BASE_URL}/tasks/{task_id}",
        headers=yg_headers,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def yg_update_task_description(task_id, new_description):
    # YouGile обычно обновляет задачу через PUT /tasks/{id}
    r = requests.put(
        f"{YOUGILE_BASE_URL}/tasks/{task_id}",
        headers=yg_headers,
        json={"description": new_description},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def yg_create_task(title, column_id, description="", assignee_id=None, deadline=None):
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
        # deadline у нас date, превращаем в 00:00 локального дня и далее в миллисекунды
        # Полдень по UTC, чтобы дата не сдвигалась
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
            # "startDate": ms,          <-- Убрал, так как не нужно
            "withTime": False,
        }

    r = requests.post(
        f"{YOUGILE_BASE_URL}/tasks",
        headers=yg_headers,
        json=body,
        timeout=10,
    )

    # на время отладки можно раскомментировать:
    # print("YG create task status:", r.status_code, "body:", r.text)

    r.raise_for_status()
    return r.json()

# ---------- DUE DATE UTILS ----------

def calc_deadline(choice: str) -> date:
    today = date.today()
    c = choice.lower()
    if c == "today":
        return today
    if c == "tomorrow":
        return today + timedelta(days=1)
    if c == "day_after_tomorrow":
        return today + timedelta(days=2)
    # fallback: сегодня
    return today


# ---------- BUILD ATTACHMENTS FOR STEPS ----------
def add_cancel_action(actions, task_title, root_post_id, user_id):
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
    actions = []
    for idx, p in enumerate(projects):
        actions.append({
            "id": f"project{idx}",  # БЕЗ _ и -
            "name": p.get("title", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_PROJECT",
                    "task_title": task_title,
                    "project_id": p["id"],
                    "project_title": p.get("title", "Без имени"),  # понадобится для текста
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
    actions = []
    for idx, b in enumerate(boards):
        actions.append({
            "id": f"board{idx}",  # вместо board_{b['id']}
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
    actions = []
    for idx, c in enumerate(columns):
        actions.append({
            "id": f"column{idx}",  # вместо column_{c['id']}
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
    options = []
    for u in users:
        full_name = u.get("realName", "") or u.get("email", "Без имени")
        options.append({
            "text": full_name,
            "value": u["id"]
        })
    return [{
        "text": "Исполнитель:",
        "actions": add_cancel_action([
            {
                "id": "assigneeSelect",  # раньше assignee_select
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
        ], task_title, root_post_id, user_id)
    }]


def build_deadline_buttons(task_title, meta, user_id, root_post_id):
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
            "id": id_,      # здесь ids будут без _
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
        act("dlCustom", "Другая дата", "custom"),
    ]

    add_cancel_action(actions, task_title, root_post_id, user_id)

    return [{
        "text": "Выберите дедлайн:",
        "actions": actions
    }]


def build_finish_buttons(task_title, task_url, user_id, root_post_id, meta):
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
        "text": f"Задача создана. Ссылка: {task_url}\n"
                f"Можете написать дополнительный комментарий в этом треде, "
                f"а затем нажать \"Завершить\".",
        "actions": actions
    }]


# ---------- FLASK APP ----------

app = Flask(__name__)


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/mattermost/actions", methods=["POST"])
def mm_actions():
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
                "project_title": project_title,   # <-- ДОБАВИЛИ
                "channel_id": channel_id,
            })

            boards = yg_get_boards(project_id)

            # превращаем старое сообщение в "фикс выбора" без кнопок
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
                # одна доска — сразу идём к колонкам
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

            # фиксируем выбор доски в текущем сообщении
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

            # сохраняем исполнителя
            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_ASSIGNEE",
                "assignee_id": assignee_id,
                "assignee_name": assignee_name,
            })

            # вытаскиваем project_id из state
            project_id = state.get("project_id") or context.get("project_id")

            # берём список пользователей проекта, чтобы найти имя
            assignee_name = assignee_id
            try:
                users = yg_get_project_users(project_id)
                for u in users:
                    if u.get("id") == assignee_id:
                        assignee_name = u.get("realName") or u.get("email") or assignee_id
                        break
            except Exception as e:
                print("Error fetching project users for assignee name:", e)

            meta = {
                "project_id": state.get("project_id"),
                "board_id": state.get("board_id"),
                "column_id": state.get("column_id"),
                "assignee_id": assignee_id,
            }
            attachments = build_deadline_buttons(task_title, meta, user_id, root_post_id)

            # затираем select с исполнителем и показываем, кто выбран
            mm_patch_post(
                post_id,
                message=f'Ответственный для задачи "{task_title}": {assignee_name}',
                attachments=[]
            )

            # новый пост с выбором дедлайна
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
                # Без дедлайна
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

            # удаляем все посты бота в этом треде
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

        # ---------- ЗАВЕРШЕНИЕ ДИАЛОГА ----------
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

            # сообщение в треде
            mm_post(
                channel_id_state,
                message=summary,
                root_id=root_post_id
            )

            # сообщение в сам канал (без треда)
            mm_post(
                channel_id_state,
                message=summary
            )

            # optionally – подчистим сообщение с кнопкой
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

def create_task_and_update_post(task_title, state, user_id, post_id):
    # Автор в описании — FirstName LastName из Mattermost
    mm_user = mm_get_user(user_id)
    first_name = mm_user.get("first_name", "").strip()
    last_name = mm_user.get("last_name", "").strip()
    username = mm_user.get("username", "")
    full_name = (first_name + " " + last_name).strip() or username

    column_id = state.get("column_id")
    assignee_id = state.get("assignee_id")
    deadline = state.get("deadline")

    description = f"Создано из Loop пользователем {full_name} (@{username})"

    # создаём задачу
    task = yg_create_task(
        task_title,
        column_id,
        description=description,
        assignee_id=assignee_id,
        deadline=deadline
    )
    task_id = task.get("id")

    # подтягиваем полную задачу, чтобы получить idTaskProject
    task_project_id = task.get("idTaskProject") or task.get("idTaskCommon")
    try:
        if task_id and not task_project_id:
            full_task = yg_get_task(task_id)
            task_project_id = full_task.get("idTaskProject") or full_task.get("idTaskCommon")
    except Exception as e:
        print("Error fetching full YouGile task:", e)

    # красивый URL
    project_title = state.get("project_title")
    project_slug = slugify_title(project_title) if project_title else ""
    team_id = YOUGILE_TEAM_ID or YOUGILE_COMPANY_ID

    if team_id and project_slug and task_project_id:
        task_url = f"https://ru.yougile.com/team/{team_id}/{project_slug}#{task_project_id}"
    elif team_id:
        task_url = f"https://ru.yougile.com/team/{team_id}"
    else:
        task_url = "https://ru.yougile.com/"

    # человекочитаемый дедлайн
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
            f'Ссылка: {task_url}\n'
            f'Можете оставить дополнительный комментарий в треде, затем нажмите "Завершить".'
        ),
        attachments=attachments
    )

    # сохраняем всё нужное для финального резюме
    set_state(user_id, state.get("root_post_id"), {
        "step": "OPTIONAL_ATTACH",
        "yougile_task_id": task_id,
        "task_url": task_url,
        "deadline_str": deadline_str,
    })


# ---------- WEBSOCKET BOT ----------

def run_ws_bot():
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

                # 1) если это новый старт: @yougile_bot создай задачу ...
                if f"@{MM_BOT_USERNAME}" in message.lower():
                    title = parse_create_command(message, MM_BOT_USERNAME)
                    if not title:
                        continue

                    projects = yg_get_projects()
                    with STATE_LOCK:
                        STATE[(user_id, root_id)] = {
                            "step": "CHOOSE_PROJECT",
                            "task_title": title,
                            "root_post_id": root_id,
                            "channel_id": channel_id,
                            "post_ids": [],   # список постов бота
                        }

                    attachments = build_project_buttons(title, projects, user_id, root_id)
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

                # 2) если мы ждём кастомную дату дедлайна
                st = get_state(user_id, root_id)
                if st and st.get("step") == "CHOOSE_DEADLINE" and st.get("deadline_choice") == "custom":
                    text = message.strip()
                    if text:
                        try:
                            # Ждём формат YYYY-MM-DD
                            d = datetime.strptime(text, "%Y-%m-%d").date()
                        except ValueError:
                            # не похоже на дату — скажем об этом и ждём дальше
                            mm_post(
                                channel_id,
                                message=(
                                    f'Не удалось разобрать дату "{text}". '
                                    f'Используйте формат YYYY-MM-DD, например 2025-11-13.'
                                ),
                                root_id=root_id
                            )
                            continue

                        # дата распарсилась — сохраняем дедлайн
                        st = set_state(user_id, root_id, {"deadline": d})
                        task_title = st.get("task_title", "Без названия")

                        # берём последний пост бота в этом диалоге
                        post_ids = st.get("post_ids") or []
                        target_post_id = post_ids[-1] if post_ids else None

                        if target_post_id:
                            # обновляем последний бот-пост (не пост пользователя!)
                            create_task_and_update_post(task_title, st, user_id, target_post_id)
                        else:
                            # на всякий случай — если по какой-то причине нет post_ids,
                            # просто создаём новый пост с результатом
                            mm_post(
                                channel_id,
                                message=f'✅ Задача "{task_title}" создана (кастомный дедлайн).',
                                root_id=root_id
                            )
                        continue

                # 3) если мы на шаге OPTIONAL_ATTACH и пользователь пишет что-то в треде
                st = get_state(user_id, root_id)
                if st and st.get("step") == "OPTIONAL_ATTACH":
                    task_id = st.get("yougile_task_id")
                    if message.strip():
                        try:
                            task = yg_get_task(task_id)
                            old_desc = task.get("description") or ""
                            ts = datetime.now().strftime("%d.%m.%Y %H:%M")
                            # если описания нет — просто пишем текст;
                            # если есть — вставляем два <br> перед блоком
                            if old_desc:
                                new_desc = (
                                    f"{old_desc}<br><br>"
                                    f"Дополнено {ts}:<br>{message}"
                                )
                            else:
                                new_desc = f"Дополнено {ts}:<br>{message}"
                            yg_update_task_description(task_id, new_desc)
                        except Exception as e:
                            print("Error updating description in YouGile:", e)
        except WebSocketConnectionClosedException:
            print("WS closed, reconnecting in 3s...")
            time.sleep(3)
        except Exception as e:
            print("WS error:", e)
            time.sleep(5)


def start_ws_thread():
    t = threading.Thread(target=run_ws_bot, daemon=True)
    t.start()


# ---------- MAIN ----------

if __name__ == "__main__":
    start_ws_thread()
    app.run(host="0.0.0.0", port=8000)