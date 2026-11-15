# Design: WP-01

## Context and Goals

FR-04, FR-11, NFR-02, NFR-03. Сервис владеет группами и занятиями;
преподаватели и аудитории принадлежат people-stub. Clock и обработчики ошибок
каркаса переиспользуются.

## Layers

- domain/lesson.py: чистые StrEnum; статусная машина и полные правила появятся позже.
- models.py: SQLAlchemy-модели без HTTP и запросов к people-stub.
- repositories/: SELECT по PK, прямые INSERT и TRUNCATE.
- services/catalog.py: отсутствие ресурса преобразуется в ApiError NOT_FOUND.
- seed.py: оркестрация загрузки; reset_data владеет транзакцией через session_scope.
- api/: параметры пути, схемы, сериализация через to_iso_z и FastAPI-роутеры.
- main.py: seed при старте и подключение callback reset_state.
- docker/entrypoint.sh: только миграции и запуск процесса; seed принадлежит lifespan.

## Invariant Protection

| Инвариант | Защита |
| --- | --- |
| Размер группы не меньше 1 | CHECK БД и проверка формы seed |
| BR-01: конец позже начала, 30 минут–8 часов | CHECK БД; доменные ошибки — WP-03 |
| BR-02: аудитория соответствует формату | CHECK БД; доменные ошибки — WP-03 |
| Допустимые format/status | VARCHAR + CHECK БД, чистые перечисления domain |
| Существующая группа занятия | FK lessons.group_id, ON DELETE RESTRICT |
| Существующий источник переноса | Отложенный FK на lessons.id |
| Не более одного потомка переноса | UNIQUE rescheduled_from_id; NULL не ограничивается |
| Занятие не ссылается на себя как на источник | CHECK БД |
| Seed не дублирует и не перезаписывает записи | INSERT ON CONFLICT по PK |
| Reset выполняется целиком или откатывается | TRUNCATE + INSERT в одной транзакции |
| Reset сохраняет текущее время | Не получает Clock; callback не вызывает freeze/reset |
| Даты ответа в UTC с Z | Сериализатор вызывает существующий to_iso_z |

Ссылки на instructor_id/classroom_id не имеют локальных FK: их таблиц в сервисе нет.
created_at/updated_at обязательны, без datetime.now() и server_default=now(): seed
содержит фиксированные значения, будущие команды получают время из Clock.

## Indexes and Concurrency

Индексы (group_id/instructor_id/classroom_id, starts_at, ends_at) поддерживают
будущий поиск пересечений BR-05. Индексы (starts_at, id) и (status, starts_at, id)
поддерживают выборки и sweeper. B-tree индексы сами не запрещают пересечения.
Транзакционные блокировки BR-05 и SELECT FOR UPDATE жизненного цикла относятся
к WP-03/WP-05 и не считаются реализованными этим пакетом.

## API and Errors

Успех: 200 с Group/Lesson непосредственно, без обёртки. Пустой результат: 404
NOT_FOUND. Неверный UUID: 400 BAD_REQUEST до SQL. У GET в нормативном YAML явно
перечислены 200/404; 400 выбран по общему соглашению о неверном формате и прямому
уточнению пользователя. Ошибки всегда формирует app.errors; нормативный YAML
не меняется. Валидный UUID другой версии не переопределяется: схемы пути используют
format: uuid, а новые идентификаторы будущих операций будут UUID v4.

## Seed and Reset

Seed проверяется на форму и aware datetime, приводится к UUID/datetime UTC и
загружается напрямую, без предметной валидации и people-stub. load_seed не делает
commit: это обязанность CLI, lifespan или session_scope. Связь L7 → L6 работает
даже при обратном порядке seed благодаря отложенному FK. Reset очищает только
lessons/groups. Чтение файлов из async-кода вынесено в asyncio.to_thread.

## Migration Plan

На новой БД: alembic upgrade head; затем alembic check. Downgrade удаляет lessons,
затем groups, вместе с их индексами и ограничениями. Для исходной уже применённой
пустой 0001 разрешён stamp base только после проверки отсутствия предметных таблиц.

## Verification and Limits

Изолированные API-тесты используют ASGITransport и fake AsyncSession, проверяют
все seed-записи через нормативный ContractChecker. Тесты PostgreSQL включаются
WP01_TEST_DATABASE_URL и создают отдельную временную схему на тест, не трогая public.
Проверяются настоящие CHECK/FK/UNIQUE, границы длительности, повторный seed,
reset/rollback, неизменность часов и оба значения SEED_ON_START.

E2E-01 целиком требует POST /lessons из WP-03. Readiness станет 200 после подключения
брокера в WP-06; в WP-01 ожидаемо остаётся 503.
