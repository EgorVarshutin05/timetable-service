# people-stub

Готовая заглушка сервиса преподавателей и аудиторий. Нормативный контракт — `stub-openapi.yaml`;
данные — `fixtures.json`; начальные режимы — `stub.env` (compose) или переменные `STUB_*`.
Порт 8100. Код заглушки менять не нужно и не следует: скрытый набор e2e поднимает её из этого каталога.

| Эндпоинт | Назначение |
| --- | --- |
| `GET /instructors/{instructor_id}` | профиль: `max_hours_per_day`, `timezone` |
| `GET /instructors/{instructor_id}/availability` | недельные окна доступности в местном времени преподавателя |
| `GET /classrooms/{classroom_id}` | аудитория: `capacity`, `online_capable` |
| `GET /health` | 200, в режиме `down` — 503 |
| `GET/PUT/DELETE /__stub/config` | режимы `latency_ms`, `fail_ids`, `timeout_ids`, `down`: чтение, изменение на лету, сброс к окружению |
| `GET/DELETE /__stub/stats` | счётчики `total`, `in_flight`, `max_in_flight`, `by_path`, `by_id`; сброс |

Режимы и порядок их применения описаны в `stub-openapi.yaml` (`info.description`) и в докстринге `app.py`.

Локальный запуск без Docker (из этого каталога): `pip install -r requirements.txt && uvicorn app:app --port 8100`.
