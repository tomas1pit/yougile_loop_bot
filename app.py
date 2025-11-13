import os
import json
import time
import threading
import re
from datetime import datetime, timedelta, date
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from websocket import create_connection, WebSocketConnectionClosedException

# ---------- ENV ----------
MM_URL = os.getenv("MM_URL").rstrip("/")
MM_BOT_TOKEN = os.getenv("MM_BOT_TOKEN")
MM_BOT_USERNAME = os.getenv("MM_BOT_USERNAME", "yougile_bot").lower()  # без @
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL").rstrip("/")

YOUGILE_COMPANY_ID = os.getenv("YOUGILE_COMPANY_ID")
YOUGILE_API_KEY = os.getenv("YOUGILE_API_KEY")
YOUGILE_BASE_URL = os.getenv("YOUGILE_BASE_URL", "https://yougile.com/api-v2").rstrip("/")

if not (MM_URL and MM_BOT_TOKEN and YOUGILE_COMPANY_ID and YOUGILE_API_KEY and BOT_PUBLIC_URL):
    print("ERROR: some required env vars are missing")
    # не выходим, чтобы было видно в логах, но всё равно работать не будет

# ---------- HTTP HEADERS ----------
mm_headers = {
    "Authorization": f"Bearer {MM_BOT_TOKEN}",
    "Content-Type": "application/json",
}

yg_headers = {
    "X-Company-Id": YOUGILE_COMPANY_ID,
    "X-Api-Key": YOUGILE_API_KEY,
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
    return r.json()


def yg_get_boards(project_id):
    # пример: ?projectId
    r = requests.get(
        f"{YOUGILE_BASE_URL}/boards",
        headers=yg_headers,
        params={"projectId": project_id},
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def yg_get_columns(board_id):
    r = requests.get(
        f"{YOUGILE_BASE_URL}/columns",
        headers=yg_headers,
        params={"boardId": board_id},
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def yg_get_board_users(board_id):
    r = requests.get(
        f"{YOUGILE_BASE_URL}/users",
        headers=yg_headers,
        params={"boardId": board_id},
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def yg_create_task(title, column_id, description="", assignee_id=None, deadline=None):
    body = {
        "title": title,
        "columnId": column_id,
        "description": description,
    }
    if assignee_id:
        body["executorId"] = assignee_id
    if deadline:
        # допустим, API принимает ISO8601
        body["deadline"] = deadline.isoformat()

    r = requests.post(
        f"{YOUGILE_BASE_URL}/tasks",
        headers=yg_headers,
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def yg_add_comment(task_id, text):
    # зависит от реального API YouGile, проверить в доке
    r = requests.post(
        f"{YOUGILE_BASE_URL}/tasks/{task_id}/comments",
        headers=yg_headers,
        json={"text": text},
        timeout=10,
    )
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

def build_project_buttons(task_title, projects, user_id, root_post_id):
    actions = []
    for p in projects:
        actions.append({
            "id": f"project_{p['id']}",
            "name": p.get("name", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_PROJECT",
                    "task_title": task_title,
                    "project_id": p["id"],
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    return [{
        "text": f"Проекты:",
        "actions": actions
    }]


def build_board_buttons(task_title, project_id, boards, user_id, root_post_id):
    actions = []
    for b in boards:
        actions.append({
            "id": f"board_{b['id']}",
            "name": b.get("name", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_BOARD",
                    "task_title": task_title,
                    "project_id": project_id,
                    "board_id": b["id"],
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    return [{
        "text": "Доски:",
        "actions": actions
    }]


def build_column_buttons(task_title, project_id, board_id, columns, user_id, root_post_id):
    actions = []
    for c in columns:
        actions.append({
            "id": f"col_{c['id']}",
            "name": c.get("name", "Без имени"),
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": {
                    "step": "CHOOSE_COLUMN",
                    "task_title": task_title,
                    "project_id": project_id,
                    "board_id": board_id,
                    "column_id": c["id"],
                    "root_post_id": root_post_id,
                    "user_id": user_id,
                }
            }
        })
    return [{
        "text": "Колонки:",
        "actions": actions
    }]


def build_assignee_select(task_title, project_id, board_id, column_id, users, user_id, root_post_id):
    options = []
    for u in users:
        full_name = (u.get("firstName", "") + " " + u.get("lastName", "")).strip() or u.get("displayName", "Без имени")
        options.append({
            "text": full_name,
            "value": u["id"]
        })
    return [{
        "text": "Исполнитель:",
        "actions": [
            {
                "id": "assignee_select",
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
        ]
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
            "id": id_,
            "name": name,
            "type": "button",
            "integration": {
                "url": f"{BOT_PUBLIC_URL}/mattermost/actions",
                "context": ctx
            }
        }

    actions = [
        act("dl_today", "Сегодня", "today"),
        act("dl_tomorrow", "Завтра", "tomorrow"),
        act("dl_day_after", "Послезавтра", "day_after_tomorrow"),
        act("dl_custom", "Другая дата", "custom"),
    ]

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
                f"Можете прикрепить файлы или написать комментарий в этом треде, "
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
        if step == "CHOOSE_PROJECT":
            project_id = context["project_id"]
            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_PROJECT",
                "task_title": task_title,
                "project_id": project_id,
                "channel_id": channel_id,
            })
            boards = yg_get_boards(project_id)
            if len(boards) <= 1:
                # если одна доска – сразу к колонкам
                board = boards[0]
                board_id = board["id"]
                state = set_state(user_id, root_post_id, {"board_id": board_id})
                columns = yg_get_columns(board_id)
                attachments = build_column_buttons(task_title, project_id, board_id, columns, user_id, root_post_id)
                mm_patch_post(
                    post_id,
                    message=f'Выберите колонку для задачи "{task_title}"',
                    attachments=attachments
                )
            else:
                attachments = build_board_buttons(task_title, project_id, boards, user_id, root_post_id)
                mm_patch_post(
                    post_id,
                    message=f'Выберите доску для задачи "{task_title}"',
                    attachments=attachments
                )

        elif step == "CHOOSE_BOARD":
            project_id = context["project_id"]
            board_id = context["board_id"]
            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_BOARD",
                "project_id": project_id,
                "board_id": board_id,
            })
            columns = yg_get_columns(board_id)
            attachments = build_column_buttons(task_title, project_id, board_id, columns, user_id, root_post_id)
            mm_patch_post(
                post_id,
                message=f'Выберите колонку для задачи "{task_title}"',
                attachments=attachments
            )

        elif step == "CHOOSE_COLUMN":
            project_id = context["project_id"]
            board_id = context["board_id"]
            column_id = context["column_id"]
            state = set_state(user_id, root_post_id, {
                "step": "CHOOSE_COLUMN",
                "project_id": project_id,
                "board_id": board_id,
                "column_id": column_id,
            })
            users = yg_get_board_users(board_id)
            attachments = build_assignee_select(
                task_title, project_id, board_id, column_id, users, user_id, root_post_id
            )
            mm_patch_post(
                post_id,
                message=f'Кого назначить ответственным за задачу "{task_title}"?',
                attachments=attachments
            )

        elif step == "CHOOSE_ASSIGNEE":
            # выбранное значение лежит в data["data"]["selected_option"] / "value"
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

            meta = {
                "project_id": state.get("project_id"),
                "board_id": state.get("board_id"),
                "column_id": state.get("column_id"),
                "assignee_id": assignee_id,
            }
            attachments = build_deadline_buttons(task_title, meta, user_id, root_post_id)
            mm_patch_post(
                post_id,
                message=f'Какую дату дедлайна поставить для задачи "{task_title}"?',
                attachments=attachments
            )

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
            else:
                deadline_date = calc_deadline(deadline_choice)
                state = set_state(user_id, root_post_id, {"deadline": deadline_date})
                # создаём задачу сразу
                create_task_and_update_post(task_title, state, user_id, post_id)

        elif step == "FINISH":
            clear_state(user_id, root_post_id)
            mm_patch_post(
                post_id,
                message=f'Диалог по задаче "{task_title}" завершён.',
                attachments=[]
            )

    except Exception as e:
        print("Error in mm_actions:", e)
        try:
            mm_post(channel_id, f"💥 Ошибка обработки действия бота: {e}", root_id=root_post_id)
        except Exception:
            pass

    return "", 200


def create_task_and_update_post(task_title, state, user_id, post_id):
    # Автор в комментариях — FirstName LastName из Mattermost
    mm_user = mm_get_user(user_id)
    first_name = mm_user.get("first_name", "").strip()
    last_name = mm_user.get("last_name", "").strip()
    username = mm_user.get("username", "")
    full_name = (first_name + " " + last_name).strip() or username

    column_id = state.get("column_id")
    assignee_id = state.get("assignee_id")
    deadline = state.get("deadline")

    description = f"Создано из Mattermost пользователем {full_name} (@{username})"

    task = yg_create_task(task_title, column_id, description=description, assignee_id=assignee_id, deadline=deadline)
    task_id = task.get("id")
    task_url = task.get("url", f"https://yougile.com/tasks/{task_id}" if task_id else "")

    meta = {
        "yougile_task_id": task_id,
    }
    attachments = build_finish_buttons(task_title, task_url, user_id, state.get("root_post_id"), meta)

    mm_patch_post(
        post_id,
        message=f'✅ Задача "{task_title}" создана.\nСсылка: {task_url}\n'
                f'Можете прикрепить файлы или оставить комментарий в треде, затем нажмите "Завершить".',
        attachments=attachments
    )

    set_state(user_id, state.get("root_post_id"), {
        "step": "OPTIONAL_ATTACH",
        "yougile_task_id": task_id,
        "task_url": task_url,
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

                    # запускаем мастер: шаг выбор проекта
                    projects = yg_get_projects()
                    with STATE_LOCK:
                        STATE[(user_id, root_id)] = {
                            "step": "CHOOSE_PROJECT",
                            "task_title": title,
                            "root_post_id": root_id,
                            "channel_id": channel_id,
                        }

                    attachments = build_project_buttons(title, projects, user_id, root_id)
                    mm_post(
                        channel_id,
                        message=f'Выберите проект для задачи "{title}"',
                        attachments=attachments,
                        root_id=root_id
                    )
                    continue

                # 2) если мы на шаге OPTIONAL_ATTACH и пользователь пишет что-то в треде
                st = get_state(user_id, root_id)
                if st and st.get("step") == "OPTIONAL_ATTACH":
                    task_id = st.get("yougile_task_id")
                    if message.strip():
                        try:
                            yg_add_comment(task_id, message)
                        except Exception as e:
                            print("Error adding comment to YouGile:", e)
                    # TODO: прикрепление файлов можно реализовать отдельно через
                    # Mattermost /files и YouGile endpoint для файлов задач.
                    # Пока просто принимаем текст.
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