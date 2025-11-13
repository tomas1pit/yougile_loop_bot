# Loop (Mattermost) → YouGile Task Bot

Бот для создания задач в [YouGile](https://ru.yougile.com/) прямо из чатов Loop (Mattermost).

Пользователь пишет в треде:

> `@yougile_bot создай задачу Сделать отчёт по продажам`

И бот пошагово ведёт пользователя:

---

## 🚶 Пошаговый мастер

1. Выбор проекта  
2. Выбор доски  
3. Выбор колонки  
4. Выбор ответственного  
5. Выбор дедлайна:

   - Сегодня  
   - Завтра  
   - Послезавтра  
   - Через неделю  
   - Через месяц  
   - **Без дедлайна**  
   - **Другая дата (YYYY-MM-DD)**  

6. Создание задачи в YouGile  
7. Приём комментариев в треде → добавление их в описание задачи  
8. Завершение через кнопку «Завершить» или автозавершение

---

## ✨ Возможности

- Создание задач одной командой:

  ```text
  @yougile_bot создай задачу <название>
  ```

- При выборе неправильной команды бот отвечает подсказкой.
- Кнопка **«Отменить»** доступна на каждом шаге.
- Автозавершение диалога по таймауту.
- Ссылки составляются в человекочитаемом виде:

  ```
  https://ru.yougile.com/team/<teamId>/<projectSlug>#<taskProjectId>
  ```

- Каждый комментарий в треде дописывается в описание задачи:

  ```html
  Дополнено 14.11.2025 23:15:<br>Комментарий пользователя...
  ```

- Итоговое сообщение дублируется в тред и в канал.

---

## 🧩 Архитектура

- **Flask** — обработка интерактивных кнопок  
- **WebSocket-клиент Loop** — слушает события в реальном времени  
- **YouGile API** — получение проектов, досок, колонок, создание задач  
- **In-memory state** — хранится в Python (без внешней БД)  

---

## 🔧 Требования

- Python 3.10+ или Docker
- Loop (Mattermost) доступный по HTTPS
- Bot Token Loop
- YouGile API Token
- Публичный URL для бота

---

## ⚙️ Переменные окружения

### Обязательные

| ENV | Описание |
|-----|----------|
| `MM_URL` | URL Loop, например `https://loop.example.com` |
| `MM_BOT_TOKEN` | Token Loop-бота |
| `MM_BOT_USERNAME` | Имя бота (по умолчанию `yougile_bot`) |
| `BOT_PUBLIC_URL` | Публичный HTTPS URL бота |
| `YOUGILE_COMPANY_ID` | Company ID в YouGile |
| `YOUGILE_API_KEY` | API ключ YouGile |

### Опциональные

| ENV | Описание |
|-----|----------|
| `YOUGILE_BASE_URL` | URL API, по умолчанию `https://yougile.com/api-v2` |
| `YOUGILE_TEAM_ID` | teamId из YouGile (если не задан — вычисляется автоматически) |
| `AUTO_FINISH_TIMEOUT_MINUTES` | Таймаут автозавершения (по умолчанию 5 мин.) |
| `TZ` | Таймзона (рекомендуется `Europe/Moscow`) |

---

## 🐳 Docker Compose

```yaml
version: "3.9"

services:
  yougile-bot:
    image: python:3.12-slim
    container_name: yougile-bot
    working_dir: /app
    command: ["python", "app.py"]
    volumes:
      - ./app.py:/app/app.py:ro
    environment:
      TZ: Europe/Moscow

      MM_URL: https://loop.example.com
      MM_BOT_TOKEN: "<loop_bot_token>"
      MM_BOT_USERNAME: "yougile_bot"
      BOT_PUBLIC_URL: "https://bot.example.com"

      YOUGILE_COMPANY_ID: "<yougile_company_id>"
      YOUGILE_API_KEY: "<yougile_api_key>"

      AUTO_FINISH_TIMEOUT_MINUTES: "5"

    restart: unless-stopped
```

> ⚠️ Не рекомендуется хранить токены в Docker Compose.  
> Используйте `.env` или Docker Secrets.

---

## 🗣️ Как работает в чате

1. Пользователь пишет:

   ```
   @yougile_bot создай задачу Подготовить отчёт
   ```

2. Бот задаёт вопросы через интерактивные карточки.

3. Создаёт задачу в YouGile:

   ```
   https://ru.yougile.com/team/<teamId>/<projectSlug>#DEL-42
   ```

4. Ждёт сообщений в треде — дописывает их в описание задачи.

5. После завершения:

   - пишет итог в тред  
   - дублирует итог в канал  

6. Если пользователь ничего не пишет и не нажимает кнопку — сработает авто-таймер.

---

## 🔒 Безопасность

- Не храните токены в GitHub.
- Используйте `.env` или Docker Secrets.
- Ограничьте доступ к серверу/Portainer.
- Всегда используйте HTTPS.

---

## ❗ Обработка ошибок

Если что-то пошло не так, бот пишет:

```
💥 Ошибка обработки действия бота: <ошибка>
```

А в логах контейнера будет traceback.

---

## 📄 Лицензия — MIT

MIT License

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights   
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell      
copies of the Software, and to permit persons to whom the Software is          
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all  
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR      
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,        
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE     
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER          
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,   
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE   
SOFTWARE.
