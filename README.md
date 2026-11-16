# timetable-service

Сервис расписания учебного центра: создание и проверка занятий, поиск конфликтов,
редактирование и жизненный цикл. Стек: Python 3.12, FastAPI, SQLAlchemy 2 async,
Alembic, PostgreSQL 16 и RabbitMQ. Контракты: `openapi.yaml`, `asyncapi.yaml`,
`stubs/people-stub/stub-openapi.yaml`. Обязательная часть реализована пакетами WP-01..WP-07.
WP-08 добавляет Should FR-14 — атомарные серии занятий; WP-09 — Should FR-15,
нагрузку преподавателя за локальную ISO-неделю.
WP-10 завершает Could FR-16/17 — список групп и TTL-кэш people-client.

## Запуск

Нужны Docker Compose v2.20+, buildx 0.17+, uv 0.13+ и just. Установка описана в
`SCAFFOLD.md`; `just doctor` проверяет окружение. `.env` необязателен: compose
содержит defaults, а `.env.example` перечисляет переопределения.

```bash
just doctor
just up
just seed
curl -i http://localhost:8000/health/live
curl -i http://localhost:8000/health/ready
```

`just up` собирает и поднимает app, postgres, people-stub и rabbitmq с ожиданием
healthcheck. Эквивалент запуска проверки задания — `docker compose up --build`.
Entrypoint применяет Alembic; `just migrate` повторяет `alembic upgrade head`.
`just seed` идемпотентен; альтернатива при старте — `SEED_ON_START=1`.

Выбрал RabbitMQ: он включён в compose по умолчанию, а topic routing достаточно
трём событиям сервиса. Используется durable topic exchange `wa.events`, routing
key `timetable.<EventType>`, persistent JSON-сообщения (`delivery_mode=2`),
`content_type=application/json`, `message_id=event_id` и publisher confirms.
Readiness выполняет настоящий passive exchange-declare RPC, а не только проверяет
локальный флаг соединения. Подключение ленивое, повреждённое соединение сбрасывается.

Готовый сервис отвечает по **оригинальному контракту**:

```json
{"status":"ok","db":"ok","broker":"ok"}
```

Если БД или брокер недоступны — 503 со `status=degraded` и их отдельными статусами.
People-stub в readiness не входит. `/health/live` остаётся 200, пока жив процесс;
именно его использует healthcheck контейнера app.

Основные настройки: `DATABASE_URL`, `BROKER`, `BROKER_URL`, `STUB_BASE_URL`,
`STUB_TIMEOUT_MS`, `TEST_MODE`, `SEED_ON_START`, `JOBS_INTERVAL_SECONDS`, `LOG_LEVEL`.
Дополнительные defaults: `RABBITMQ_EXCHANGE=wa.events`,
`KAFKA_TOPIC=wa.timetable.events`, `BROKER_TIMEOUT_MS=2000`.
`PEOPLE_CACHE_TTL_SECONDS=30` задаёт TTL people-client (0 отключает, максимум 3600).
Настройка передаётся в app через compose; новые зависимости не требуются.
`settings.rabbitmq_url` читает канонический `BROKER_URL`. Kafka-адаптер также есть;
для него переключаются defaults и профили по инструкции в `docker-compose.yml`.
Основной и проверенный сетевым e2e брокер — RabbitMQ.

По умолчанию compose использует `TEST_MODE=1`: доступны `/__test/time`,
`/__test/reset` и ручной запуск `lesson-status-sweeper`, периодический цикл отключён.
`POST /__test/reset` удаляет текущие группы/занятия и восстанавливает seed, не меняя
Clock; после успешного reset очищается также кэш people-client. При `TEST_MODE=0`
тестовые маршруты возвращают 404, а job запускается каждые
`JOBS_INTERVAL_SECONDS` (по умолчанию 30, допустимо 1..60).

Проверки с хоста:

```bash
uv sync --locked
just check
just e2e-public
uv run pytest tests/unit/test_people_client.py -k test_async_case -q -s
```

`just check` запускает ruff, strict mypy и unit/API-тесты. Для PostgreSQL-тестов
нужна отдельная БД и роль с правом CREATE SCHEMA. Если `wp01_tests` ещё нет,
создайте её один раз; тесты создают и удаляют только свои случайные схемы:

```bash
docker compose exec postgres createdb -U wa wp01_tests
export WP01_TEST_DATABASE_URL=postgresql+asyncpg://wa:wa@localhost:5432/wp01_tests
uv run pytest -q
just check
```

Без `WP01_TEST_DATABASE_URL` PostgreSQL-проверки пропускаются: это не полный прогон.
Публичные e2e работают через настоящие HTTP и RabbitMQ, сбрасывают seed/Clock/очередь
перед сценарием и сохраняют `e2e-report.json`. Для диагностики: `just ps`,
`just logs app`, `just logs rabbitmq`. `just down` удаляет также тома БД.

Проверено при завершении WP-06: `just up`, readiness **200 OK** с исходным
`status/db/broker`, затем `just e2e-public` — **3 passed in 9.14s**: E2E-01,
E2E-02, E2E-17. Это реальный прогон автора на полном Docker-стеке.
На этапе WP-10 полный unit/PostgreSQL-набор — **635 passed in 14.31s** на PostgreSQL
16.2, ruff и strict mypy чисты (87 файлов). Это исходный замер WP-10;
результаты последующего Docker-аудита приведены в «Не сделано» и «AI notes».
При обновлении существующей БД
entrypoint/`just migrate` применяет новую additive revision `0002`.

## Решения

**Слои.** `domain/` содержит чистые BR и статусную машину без FastAPI/HTTPX/ORM;
`api/` — схемы и HTTP, `repositories/` — SQL, `services/` — порядок I/O и транзакции,
`clients/` и `broker.py` — интеграционные адаптеры. Ошибки проходят через `errors.py`
в формате `{"error":{"code":"...","message":"...","details":{}}}`. Неверная форма JSON/тип
даёт 400, семантическое нарушение — 422, отсутствующий ресурс пути — 404.
Текущее время берётся только через `Clock`; даты входа требуют offset, БД/ответы — UTC,
выход через `to_iso_z()`. Наивные даты дают 422 с `details.fields`.

**Данные и seed.** Группы и занятия хранятся локально; преподаватели/аудитории —
в people-stub. Миграция `0001` содержит CHECK для длительности 30 минут..8 часов,
порядка дат, формата/аудитории и статусов, FK и UNIQUE для одного преемника переноса.
Статусы/формат — VARCHAR + CHECK вместо native enum ради простого отката миграций.
Индексы поддерживают resource/time, status/time и стабильные выборки.
Seed использует INSERT ON CONFLICT DO NOTHING, сохраняя пользовательские правки.
Reset — атомарные TRUNCATE + seed без CASCADE и без изменения Clock.

**Порядок проверок.** Для изменения/жизненного цикла статус проверяется раньше BR.
BR-01/02 выполняются до БД и HTTP. При создании группа проверяется до people-client;
на время HTTP SQL-сессия освобождается. Затем write-транзакция повторно читает
группу FOR SHARE и оценивает BR-04..07. POST create останавливается на первом
hard-нарушении; validate возвращает все 409-нарушения в `violations` при HTTP 200,
но не превращает 422/502/504 в нарушения. Soft warnings не блокируют запись.

**Конкуренция.** BR-05 защищён `pg_advisory_xact_lock` по преподавателю, группе и
непустой аудитории. Стабильные BLAKE2b-64 ключи берутся в числовом порядке; только
row-lock найденных конфликтов недостаточен при пустом расписании. Выбрана блокировка
ресурса целиком вместо дробления по датам: проще доказательство, меньше параллелизм.
Write isolation — READ COMMITTED: SELECT после ожидания lock видит commit конкурента.
Advisory locks кооперативны и не защищают от произвольного внешнего SQL.
Пересечение строгое, касание границ допустимо. Активны PLANNED, CONFIRMED,
IN_PROGRESS, COMPLETED; CANCELLED/RESCHEDULED исключены. Конфликты перечисляются
instructor → classroom → group, внутри по UUID, с UTC-интервалом пересечения.

**Локальные дни.** BR-06 использует `ZoneInfo` преподавателя, разрезает интервал
по локальным полуночам и считает фактическую UTC-длительность через Decimal.
Это учитывает сутки 23/25 часов при DST; окончание в полночь не добавляет пустой день.
BR-07 требует одного полного окна, соседние окна не объединяются. Для неоднозначной
границы окна выбран `fold=0`, окно с несуществующей DST-границей в этот день пропускается.

**Список и PATCH.** Фильтры объединяются по И, период `from <= starts_at < to`,
сортировка `starts_at ASC, id ASC`, limit=50 (1..200), offset=0. Total считается до
пагинации в одном REPEATABLE READ снимке с items; записи остаются READ COMMITTED.
Случай `from>=to` отдельно не задан — выбрал 422 VALIDATION_ERROR с `fields.to`.
PATCH принимает непустое подмножество разрешённых полей; `model_fields_set` отличает
непереданную аудиторию от явного null. Семантика значений проверяется после PLANNED.
Берётся sorted union старых/новых ресурсов, затем FOR UPDATE; устаревший DraftLesson
заставляет повторить merge/HTTP со свежим снимком. `updated_at` не служит версией
при frozen Clock, новые locks не добавляются после row-lock. При постоянных правках
возможны новые циклы; upstream-ошибки автоматически не повторяются.

**Жизненный цикл.** Confirm/cancel/complete используют row-lock и текущий статус.
Complete сначала требует IN_PROGRESS, затем BR-08 `now>=ends_at`.
Reschedule одной транзакцией меняет исходное на RESCHEDULED и создаёт новое PLANNED
с той же группой и `rescheduled_from_id`. Исходное исключается из BR-05 и BR-06;
второй конкурентный перенос получает 409 TRANSITION_FORBIDDEN. Sweeper берёт
CONFIRMED с `starts_at<=now` FOR UPDATE и переводит в IN_PROGRESS, даже если ends_at
уже прошёл. Выбранное ожидание вместо SKIP LOCKED не пропускает занятые строки
при ручном запуске. Повторный запуск не считает строку дважды.

**People-client.** Для IN_PERSON три запроса идут через `asyncio.gather`, для ONLINE
два. Результаты/ошибки выбираются в фиксированном порядке profile → availability →
classroom, не по скорости ответа. 404 превращается в 422 ENTITY_NOT_FOUND,
таймаут — 504 UPSTREAM_TIMEOUT, сеть/5xx — 502 UPSTREAM_UNAVAILABLE.
I6 с успешным профилем и ошибкой availability отклоняется. Невалидный upstream 200
также даёт 502. `trust_env=False` исключает случайный proxy для внутренней заглушки;
все ресурсы закрывает lifespan, включая ошибку startup.

**События.** Payload снимается под блокировкой; publish выполняется после commit
только для confirm/cancel/reschedule. У cancel сохраняется предыдущий статус;
у переноса root lesson_id/instructor_id относятся к исходному, previous/new —
к соответствующим интервалам. Event ID — UUID v4, occurred_at — Clock момента
операции. Idempotency key стабилен для исходного занятия. Ни rollback, ни create,
validate, PATCH, complete, sweeper, seed/reset событий не отправляют.
Без outbox остаётся окно сбоя между commit и отправкой: HTTP 500 не откатывает БД,
а потерянный ack не доказывает отсутствие события. Не обещаю exactly-once;
потребитель дедуплицирует по ключу. Outbox не требуется Must и не добавлен.
Persistent message без bound queue может быть отброшен; очередь заранее создаёт
и привязывает потребитель. E2E использует `e2e.timetable` с `timetable.#` до HTTP.

**WP-07.** Канонические specs синхронизированы с текущей обязательной частью,
WP-01..WP-06 перенесены в `openspec/changes/archive/2026-11-15-<id>`.
Ветки и Change идентификаторы сохраняются. WP-07 также влит и архивирован.
Нормативные HTTP/AsyncAPI контракты и исходный readiness сохранены. Пакет меняет
документацию; искусственные feat/test-коммиты для него не создаются.

**WP-08 (FR-14).** POST `/lesson-series` принимает count=1..50, duration_minutes=30..480,
непустой уникальный список MON..SUN и first_starts_at со смещением. Сначала время
переводится в UTC; weekday и повторения определяются по UTC-дате, а время суток UTC
остаётся тем же. Если исходный день подходит, он включается, иначе выбирается
следующий подходящий. Порядок weekdays во входном массиве не меняет хронологию.

Чистый генератор проверяет BR-01/02 до БД/HTTP. Группа проверяется короткой сессией
до единственного общего people lookup: 3 параллельных запроса для IN_PERSON и 2 для
ONLINE. В write-транзакции захватывается sorted union advisory locks всей серии,
группа читается FOR SHARE, а один SELECT загружает снимок активных занятий для
BR-05/06. Межэкземплярные проверки BR-04..07 выполняются конкурентно через
asyncio.to_thread с Semaphore(4): одновременно работают не более четырёх проверок.
Workers получают immutable domain snapshots; ORM и AsyncSession остаются в event loop.

Каждая проверка видит общий снимок БД и префикс ранее сгенерированных занятий с
заранее выделенными UUID. Поэтому сохраняются внутрисерийные конфликты и BR-06
через локальную полночь; предупреждения относятся к проверяемому префиксу.
Сервис дожидается всех проверок и выбирает первую ошибку по индексу экземпляра,
независимо от порядка завершения workers. Только после успеха всей серии идут
INSERT/flush и единый commit. Resource locks удерживаются от чтения снимка до
commit, поэтому одиночный create/PATCH/reschedule и другие серии не обходят BR-05.
Пул потоков использует стандартный executor asyncio; отдельного процесса нет.
Это конкурентная обработка, а не обещание CPU-ускорения Python-кода под GIL.

Hard error возвращает исходные details и occurrence_index, начиная с 0; любой
отказ не допускает INSERT серии. Общие reference/upstream ошибки получают
index=0; структурные ошибки входного тела остаются обычными fields/errors.

У всех новых занятий PLANNED, разные UUID v4 и общий series_id=UUID v4; один
Clock.now задаёт created_at=updated_at всей серии. Возвращается 201
`{series_id,items:[{lesson,warnings}]}` после commit. Создание серии событий не
публикует. Проверки используют те же locks, что одиночный create/PATCH/reschedule.
По текущему контракту одинаковое UTC-время и duration<=8h не дают двум экземплярам
серии пересечься между собой, но прежний conflict алгоритм всё равно видит
предыдущие сгенерированные экземпляры; дневная нагрузка может накапливаться на общей локальной дате.

Миграция `0002` добавляет nullable UUID series_id и индекс, без отдельной таблицы
или уникальности по series_id. У старых/одиночных/seed занятий JSON содержит null,
что разрешено исходным OpenAPI. PATCH сохраняет membership; для reschedule выбрал
наследование series_id новым преемником, поскольку перенос не меняет принадлежность.
Этот случай отдельно не задан контрактом; решение покрыто PG-тестом. Downgrade
удаляет только индекс/колонку: занятия сохраняются, membership теряется.
Нормативные OpenAPI/AsyncAPI, 0001, readiness, broker и конфигурация не менялись.

**WP-09 (FR-15).** GET `/instructors/{instructor_id}/workload?week=2026-W49`
требует UUID пути и реально существующую ISO-неделю через `date.fromisocalendar`.
Ошибки UUID дают 400 BAD_REQUEST, отсутствующая/невалидная week — 422
VALIDATION_ERROR с fields.week; эти проверки выполняются до HTTP и SQL.
Нужен только профиль преподавателя: availability и classroom не запрашиваются,
поэтому отказ availability у I6 не мешает отчёту. Сервис переводит только профильный
INSTRUCTOR_NOT_FOUND в 404 NOT_FOUND, поскольку преподаватель — ресурс пути.
Общий people-client сохраняет BR-03 и 422 для ссылок тела; 502/504 не меняются.

Границы недели — две независимо построенные локальные полуночи понедельников,
переведённые в UTC. Нельзя прибавлять 168 UTC-часов: неделя DST может длиться
167 или 169 часов. Один SELECT выбирает активные занятия преподавателя по строгому
пересечению `starts_at < end AND ends_at > start`, включая COMPLETED и занятия
серий. CANCELLED/RESCHEDULED исключены; касание границы не даёт часов.
Чистый расчёт обрезает интервалы границами недели и вызывает ту же `daily_hours`,
что BR-06. Возвращаются ровно 7 локальных дат, включая нули. Суммы и строгое
`exceeded = hours > max_hours_per_day` считаются в Decimal до преобразования в float.

Один SELECT даёт согласованный снимок строк PostgreSQL; write locks и commit
отчёту не нужны. Профиль и БД не образуют распределённый снимок: используются
профиль текущего запроса и затем снимок занятий. Clock, брокер и события не
задействованы. Миграций и новых зависимостей нет; исходные контракты сохранены.

**WP-10 (FR-16).** GET `/groups` возвращает `{items,total}`, limit=50 (1..200),
offset=0 (>=0). SQL сортирует всю выборку по `name ASC, id ASC` до LIMIT/OFFSET;
используется collation PostgreSQL. Имена-дубликаты разрешены, UUID задаёт устойчивый
порядок. Offset за концом даёт пустые items и полный total. Пагинация валидируется
до SQL и даёт 422 VALIDATION_ERROR с fields, включая нецелые query values.
COUNT и page SELECT читаются в одной REPEATABLE READ транзакции без commit/записей.
GET группы по ID сохраняется без изменений; I/O people-stub/брокера здесь нет.

**WP-10 (FR-17).** Три типизированных TTL/LRU-кэша на PeopleClient хранят только
успешные валидированные immutable domain values: профиль, окна и аудиторию.
Ключ — UUID внутри типа; разные клиенты/workers не делят значения. TTL по умолчанию
30 секунд, `PEOPLE_CACHE_TTL_SECONDS=0` отключает хранение и объединение запросов.
Диапазон настройки 0..3600, NaN/Infinity отклоняются. Срок измеряется монотонным
таймером **от успешного завершения загрузки**, действует строго до deadline.
Clock отвечает за предметное время; его freeze/reset не управляет TTL.
На тип хранится максимум 256 LRU values (768 на клиент); истёкшие записи удаляются
при обращениях. Ошибки 404/5xx/сеть/таймаут/некорректный payload и отмена не кэшируются.
Expired value не используется как fallback при отказе upstream.

Метаданные защищены коротким RLock, HTTP никогда не выполняется под этим mutex.
Async lock на ключ объединяет одновременные успешные miss. Разные UUID и типы
загружаются параллельно, прежний gather 3/2 сохраняется. Отмена waiter не отменяет
owner; отмена owner освобождает ключ, и waiter повторяет загрузку. Ошибки не
становятся отрицательным shared cache. Key locks удаляются после последнего caller.
AsyncClient и async locks принадлежат одному event loop lifespan; общий клиент
между event loops не используется. Синхронный clear_cache безопасен и из другого
потока. Нет фоновых задач загрузки, переживающих запрос.

Инвалидация — TTL, LRU eviction, явный `PeopleClient.clear_cache()`, успешный
`POST /__test/reset`, shutdown/restart. Generation не позволяет старой in-flight
загрузке восстановить очищенную запись; уже ожидавший caller может получить её
старый результат. aclose очищает кэш, закрывает HTTPX и запрещает последующие hits.
Изменение fixtures/config самого people-stub не очищает кэш автоматически: до
expiry допустимы устаревшие capacity/max_hours/timezone/windows. Для немедленной
свежести нужен clear/reset или TTL=0. Это локальный TTL-кэш, без distributed
invalidation и общей атомарной версии profile/availability; выбран явный компромисс
свежести и количества HTTP. Прежние ошибки/приоритет проверок и BR-05 locks не меняются.
@lru_cache над async def отвергнут, поскольку сохраняет coroutine вместо результата;
глобальный lock вокруг HTTP сломал бы параллелизм независимых запросов.

| Требования | Каноническая спецификация | Основная проверка |
| --- | --- | --- |
| FR-04, FR-11, NFR-02/03 | catalog-bootstrap | catalog API, WP-01 PostgreSQL |
| FR-12, BR-03, NFR-07 | people-client | client/async и lifespan tests |
| FR-01/02, BR-01..07 | lesson-scheduling | pure domain, commands API, WP-03 PostgreSQL |
| FR-03/05 | lesson-browsing-and-editing | list/PATCH, WP-04 PostgreSQL, E2E-02 |
| FR-06..10, BR-08 | lesson-lifecycle | lifecycle matrix, WP-05 PostgreSQL |
| FR-13, NFR-01 | lesson-events | events/transport, WP-06 PostgreSQL, E2E-17 |
| FR-14 (Should) | lesson-series | UTC generator/API, WP-08 PostgreSQL и Alembic cycle |
| FR-15 (Should), BR-06 | instructor-workload | ISO-недели/DST, API и WP-09 PostgreSQL |
| FR-16 (Could) | catalog-bootstrap | group page API и WP-10 PostgreSQL |
| FR-17 (Could), FR-12/NFR-07 | people-client | TTL/concurrency/cancellation, reset и cached create race |
| NFR-01..07 | quality-and-delivery | static checks, Docker, README/OpenSpec/history |

Спецификации лежат в `openspec/specs/<capability>/spec.md`; сценарии WHEN/THEN
описывают текущее поведение, а архив хранит дельты и решения соответствующих пакетов.

## Не сделано

Вся обязательная часть **Must, WP-01..WP-07**, реализована и проверена:
Все исходные 438 тестов Must сохраняются; текущий полный набор с WP-08..10 —
647 unit/API/PostgreSQL-тестов, включая регрессии аудита.
При WP-06 автор проверил 3 public E2E на полном Docker-стеке
app + postgres + people-stub + rabbitmq, **3 passed in 9.14s**.
Should FR-14/15 (WP-08/09) и Could FR-16/17 (WP-10) реализованы и проверены
PostgreSQL/API-тестами. Незавершённых функциональных пакетов WP-01..10 нет.
После аудита Docker-стек проверен на отдельном чистом томе: readiness 200,
3 public E2E passed in 9.38s. Сетевые smoke-проверки FR-14..16 прошли:
50 экземпляров серии, поздний конфликт без частичных записей, отчёт и пагинация групп.

Не реализован необязательный transactional outbox. Сетевой Kafka-прогон не выполнялся:
для сдачи выбран и проверен RabbitMQ. У TTL-кэша допустима описанная выше задержка
свежести; общей распределённой инвалидации нет. Это ограничение выбранного решения.
Факты WP-08/09/10 — 0,66/0,60/0,50 ч; итог Must — 4,76 ч,
Should/Could — 1,76 ч, общий итог — 6,52 ч. Завершённые changes WP-08..10
архивированы; для сдачи используется тег `submission` на финальном коммите master.

## Async-кейс

Независимые запросы people-stub выполняются параллельно: профиль преподавателя,
окна доступности и, для IN_PERSON, аудитория. Тест с искусственной задержкой
300 мс на каждый вызов работает без сети, БД и брокера:

```bash
uv run pytest tests/unit/test_people_client.py -k test_async_case -q -s
```

Замер WP-02, сохранённый без подмены новыми числами:

| Формат | Последовательно, мс | Параллельно, мс | Ускорение | max_in_flight |
| --- | ---: | ---: | ---: | ---: |
| IN_PERSON | 904,49 | 302,19 | 2,99× | 3 |
| ONLINE | 602,27 | 302,09 | 1,99× | 2 |

CountingTransport охватывает весь запрос; проверяются не только относительные
времена с запасом для CI, но и точные пики 3/2. Это воспроизводимый fake benchmark,
а не нагрузочное обещание production. Дополнительный тест запускает исходный stub
через ASGI и проверяет его реальный счётчик, in_flight=0 и сброс статистики:

```bash
uv run pytest tests/unit/test_people_client.py -k original_stub_stats -q
```

У заглушки путь `/__stub/stats` с двумя подчёркиваниями. Admin-запросы в счётчик
не входят. Timeout custom transport моделируется `httpx.ReadTimeout`, поскольку
один asyncio.sleep внутри пользовательского транспорта не включает сетевой timeout
HTTPX. В рабочем клиенте ограничены connect/read/write/pool таймауты.

После WP-10 замеры выше относятся к **холодному кэшу** (или TTL=0). Тест создаёт
отдельный PeopleClient для каждой стратегии и по-прежнему достигает max_in_flight
3/2. Тёплый кэш уменьшает количество реальных HTTP-запросов. Отдельные cache-тесты
проверяют 20 одновременных callers: один сетевой запрос на ключ и пик 3/2.

## AI notes

Использовал ChatGPT/Codex для чтения реальных файлов архива через Python,
проектирования слоёв, подготовки реализации, OpenSpec и тестов. Работа шла по пакетам:
WP-01 — модели/миграция/seed; WP-02 — people-client и async-замер;
WP-03 — BR и advisory locks; WP-04 — список/PATCH; WP-05 — lifecycle/sweeper;
WP-06 — post-commit события и брокеры; WP-07 — текущая документация и канонические specs.

Предложения проверялись по исходным контрактам, strict mypy, ruff, API-схемам и
настоящему PostgreSQL. Исправлялись типизация SQL/HTTPX, runtime-зависимости брокера,
выбор ошибки I6, сохранение previous_status под lock и свежего снимка PATCH.
Отвергнуты UPSERT с перезаписью seed, row-lock только найденных конфликтов,
Python hash() для ключей, updated_at как версия, exclude_none для PATCH,
обращение к people-stub в readiness и отправка события внутри транзакции.
Оригинальные openapi.yaml и health.py сохранены; черновое изменение формы readiness
не включено в принятый WP-06. Изоляция domain и отсутствие подавлений типизации сохранены.

WP-07: перепроверены шесть заголовков, факты времени, все Must-идентификаторы,
scenarios и архивные Change пути. Из канона удалены обещания «будущего» publisher.
Линтер допускает docs-only пакет: no-code — статистика, обязательного feat/test нет.
Предложенные сообщения и отдельная модель истории проверяются настоящим
`tools/lint_commits.py`; это не выдаётся за доступ к удалённой истории пользователя.

Проверки разделены по контексту: ассистент повторяет static/unit/PostgreSQL;
автор подтвердил настоящий `just up`, ready=200 по исходному контракту и
`just e2e-public` — **3 passed in 9.14s**. Эти сетевые результаты не приписываются
ASGI/capture-тестам. Предыдущие пакеты влиты; реальные факты времени сохранены ниже.

WP-08: ChatGPT/Codex прочитал исходные LessonSeriesCreate/Result/Lesson и TASK через
Python, добавил чистый UTC-генератор, nullable membership, миграцию и транзакционный
endpoint поверх WP-07. Отвергнуты commit на каждый экземпляр, дни недели в локальном
offset, повторный people lookup на каждую дату и gather над одним AsyncSession.
Два старых JSON-ожидания seed расширены только полем series_id=null; исходные e2e
assertions и нормативные контракты не менялись. Все факты Must и сумма 4,76 сохранены.

Результат WP-08: 51 новых изолированных и 11 PostgreSQL-тестов, полный прогон
**500 passed** на PostgreSQL 16.2; без БД — 383 passed, 117 skipped. Ruff и strict
mypy чисты (77 файлов). Проверены count/duration boundaries, UTC/leap dates,
late conflict rollback index=2, cap index=0, BR-06 через полночь, две серии и
single/series race, GET/membership/null и отсутствие create events. На отдельной
БД прошли 0001 с данными → 0002 → 0001 → 0002, base → head и alembic check.
Предлагаемые commit headers проверяются настоящим lint_commits.py, scopes и <=72.

WP-09: исходные FR-15 и Workload прочитаны через Python. Добавлены чистый парсер
ISO-недели и отчёт на общей с BR-06 функции daily_hours, один SQL SELECT и один
profile-only HTTP lookup. Отвергнуты группировка по UTC-дате, фиксированные 168 часов,
семь отдельных SELECT, запрос availability и глобальная замена 422 на 404 в клиенте.
Дневные суммы и exceeded вычисляются до float-сериализации. Старые тесты и код BR-06
сохранены; нормативный readiness по-прежнему status/db/broker.

Результат WP-09: 58 новых изолированных unit/API и 20 PostgreSQL-тестов;
**578 passed in 14.17s** на PostgreSQL 16.2, без БД — 441 passed, 137 skipped.
Ruff и strict mypy чисты (83 файла). Проверены 52/53 недели, ошибки до I/O,
seed I1/2026-W49, все статусы, серия WP-08, локальная полночь и границы недели,
весенний/осенний DST, равенство/превышение лимита, профиль I6 и 404/502/504.
Публикаций нет. Новых миграций нет; Docker/e2e-прогон WP-09 не заявляется.
Факты WP-00..WP-08 и Must 4,76 сохранены; принятый позднее факт WP-09 — 0,60 ч.

WP-10: через Python прочитаны реальные GroupPage/Limit/Offset и FR-16/17.
Добавлены страница групп со snapshot COUNT/items и bounded typed TTL/LRU cache.
Отвергнуты @lru_cache на coroutine, global HTTP lock, кэширование ошибок и связь TTL
с adjustable Clock. Кэш помещён в clients, SQL — в repositories, snapshot — в
services; чистые BR не менялись. Clear generation защищает от stale repopulation.
Конфигурация TTL проведена через Settings, .env.example и Docker Compose.

Результат WP-10: 44 новых isolated/API и 13 PostgreSQL-тестов, полный прогон
**635 passed in 14.31s** на PostgreSQL 16.2. Ruff и strict mypy чисты (87 файлов).
Проверены seed/name ties/pages/empty total, единый снимок при конкурентной вставке,
TTL hit/exact expiry/disable/LRU, три типа, ошибки без negative cache, clear during
flight, отмена owner/waiter, cross-thread clear, reset без изменения Clock,
workload с обновлением профиля после expiry и cached concurrent create (201/409).
Старый gated_people test helper получает TTL=0, поскольку его барьер ждёт
повторяющиеся HTTP-запросы для проверки SQL-гонок; assertions не ослаблены.
Нормативные контракты, readiness status/db/broker, модели, миграции и старые BR
сохранены. Предлагаемые commit/merge headers проверяются lint_commits.py и <=72.
Принятые факты до WP-09=0,60 сохранены; Must=4,76, до WP-10 всего 6,11 ч.
Принятый факт WP-10 — 0,50 ч; итог Should/Could — 1,76 ч, общий — 6,52 ч
(текущие значения из таблицы «Время»).

После аудита FR-14: общий снимок БД под resource locks и конкурентные проверки
экземпляров через asyncio.to_thread с Semaphore(4). Из workers исключены ORM/SQL;
prefix snapshots сохраняют BR-06 и внутрисерийные конфликты. Результаты выбираются
по индексу, запись начинается после успешной проверки всей серии. Добавлены тесты
ограничения concurrency и первого индекса при обратном порядке завершения ошибок.

Docker-аудит 2026-11-16: изолированный compose project timetable-audit-gate,
отдельный пустой volume; единственный override отключает публикацию PostgreSQL
на хост, где порт 5432 уже занят рабочей БД. Сборка, healthchecks и entrypoint
alembic upgrade head (0001, 0002) прошли; /health/ready вернул 200 с db/broker=ok.
Public E2E: 3 passed in 9.38s; сетевые smoke FR-14..16 прошли. Временный стек и
его volume удалены; рабочие таблицы исходного PostgreSQL не затрагивались. Проверка клона по тегу
submission отложена до подготовки сдачи. Результаты исходных замеров выше сохранены.
На первом этапе аудита обнаружено, что старый readiness unit-test зависел от
отсутствия RabbitMQ на localhost. В финальной доработке WP-10 обе probes заменены фейками; окружение
больше не определяет результат этого unit-теста.
После удаления временного стека полный прогон с PostgreSQL: **637 passed in 34.42s**.
Ruff, форматирование, strict mypy (87 файлов) и шесть заголовков README проверены.

Финальная доработка WP-10 закрывает аудит FR-14 и NFR-03/04/05/06. TestTime использует общий строгий
ISO-парсер: число now даёт 400 BAD_REQUEST, Unix timestamp строкой — 422
VALIDATION_ERROR; Clock не меняется при отказе. Дробные секунды в сериализации
Clock оставлены как в scaffold по решению автора. В GitHub Actions добавлен
PostgreSQL 16 service с healthcheck, в GitLab — такой же service с alias postgres;
оба check job задают WP01_TEST_DATABASE_URL. Workflow на хостинге будет проверен
после push; локально проверены YAML/DSN и полный PostgreSQL-набор.

Проверка финального WP-10: **647 passed in 31.71s**, без skips, при реально работающем RabbitMQ;
ruff, форматирование и strict mypy чисты (87 файлов). Повторная сборка на чистом
Docker-томе и readiness 200 прошли; public E2E — **3 passed in 9.51s**.
Сетевые запросы подтвердили 400/422 и сохранение Clock. Временный Docker-стек
после проверки удалён. Записи о предыдущих прогонах выше сохранены как история.

## Время

| Пакет | Оценка задания, ч | Моя оценка до старта, ч | Факт, ч | Куда ушло время (при факте ≥ 2× и ≥ +1 ч) |
| --- | --- | --- | --- | --- |
| WP-00 Процесс | 0,75 | 0,75 | 0,50 | — |
| wp01-load-seed-data | 0,80 | 1,00 | 0,75 | — |
| wp02-add-people-client | 0,75 | 0,75 | 0,70 | — |
| wp03-detect-lesson-conflicts | 1,75 | 2,00 | 0,83 | — |
| wp04-list-and-edit-lessons | 0,80 | 1,00 | 0,6 | — |
| wp05-manage-lesson-lifecycle | 1,50 | 1,75 | 0,58 | — |
| wp06-publish-lesson-events | 0,70 | 0,75 | 0,50 | — |
| wp07-finalize-docs | 0,40 | 0,50 | 0,30 | — |
| Итого Must, включая WP-00 | 7,45 | 8,50 | 4,76 | — |
| wp08-create-lesson-series | 1,00 | 1,25 | 0,66 | — |
| wp09-report-instructor-workload | 0,50 | 0,75 | 0,60 | — |
| wp10-list-groups-cache-people | 0,50 | 0,75 | 0,50 | — |
| Итого Should/Could | 2,00 | 2,75 | 1,76 | — |
| Итого | 9,45 | 11,25 | 6,52 | — |

WP-10 остаётся последним пакетом и включает исправления финального аудита.
Исходные факты времени сохранены; автор уточняет факт WP-10 и итог с учётом доработок перед сдачей.
