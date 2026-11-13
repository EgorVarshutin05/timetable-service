# Design: WP-02

## Scope and Layers

FR-12, NFR-07, BR-03. domain/people.py содержит только стандартные dataclass,
UUID, Decimal и time. Преподаватель, доступность и аудитория не сохраняются в БД.
clients/people.py содержит приватные Pydantic DTO, HTTPX и перевод в ApiError.
services/ будущего WP-03 вызывают клиент после проверки статуса и BR-01/BR-02,
проверки существования группы; доменные правила не получают HTTPX-клиент.

## Domain Data

InstructorProfile: id, full_name, max_hours_per_day (Decimal), timezone (IANA string).
InstructorAvailability: instructor_id, timezone, tuple недельных AvailabilityWindow.
Окно: ISO weekday 1..7, локальные time начала/окончания без смещения. Classroom:
id, name, capacity, online_capable. LessonPeople объединяет три результата, при
ONLINE classroom=None. Преобразование в ZoneInfo для BR-06/BR-07 — WP-03.

## HTTP and Validation

AsyncClient использует base_url=settings.stub_base_url,
timeout=settings.stub_timeout_ms/1000, trust_env=False. HTTPX timeout применяется
к connect/read/write/pool; это не отдельный общий deadline HTTP-операции.
Ответ 200 валидируется приватной DTO; проверяются UUID, IANA timezone, положительные
capacity/max_hours, weekday и локальное окно HH:MM. Несовпадение идентификатора,
невалидный JSON/схема и неожиданный статус преобразуются в 502 UPSTREAM_UNAVAILABLE.
Клиент не возвращает наружу тело upstream-ошибки или полный URL.

## Concurrency and Error Precedence

После проверки предусловия BR-02 все независимые запросы запускаются gather.
Для IN_PERSON: профиль, окна, аудитория. Для ONLINE: профиль, окна; аудитория
не запрашивается. Некорректная комбинация format/classroom в прямом внутреннем
вызове даёт ValueError до сети: бизнес-ответ BR-02 обязан формировать domain/service
в WP-03, а не этот адаптер.

return_exceptions=True собирает все исходы. Затем unwrap выполняется в порядке
профиль → окна → аудитория. Ошибка не зависит от скорости ответов. Все запросы
завершены до возврата ApiError. Отмена вызывающей корутины передаётся gather и
отменяет дочерние запросы; CancelledError не превращается в upstream-ошибку.

| Отказ | ApiError |
| --- | --- |
| 404 профиля или окон | 422 INSTRUCTOR_NOT_FOUND |
| 404 аудитории | 422 CLASSROOM_NOT_FOUND |
| httpx.TimeoutException | 504 UPSTREAM_TIMEOUT |
| 5xx или httpx.RequestError | 502 UPSTREAM_UNAVAILABLE |
| Неверный 200 / неожиданный статус | 502 UPSTREAM_UNAVAILABLE |

Профиль и окна обязательны даже для soft-правил: I6 с профилем 200 и окнами 500
должен отклонить операцию. Конкурентные нарушения предметных правил будут
упорядочиваться в service/domain WP-03; здесь определён только порядок upstream.

## Lifecycle and WP-01 Compatibility

build_context всегда создаёт people_client. AppContext допускает None только для
ручных изолированных тестовых контекстов. Lifespan регистрирует engine.dispose и
people_client.aclose в AsyncExitStack: клиент закрывается до engine, включая
неуспешный seed/startup и исключение во время работы. Сохраняются SEED_ON_START,
reset_state, Clock, оба GET-роутера и реестр задач WP-01.
api/health.py не меняется: БД + брокер, без people-stub. Клиент не имеет миграций,
не вызывает commit и не публикует событий. Повторный seed/reset не затрагивает клиента.

## Exact Stub Behavior

Служебные пути: /__stub/config и /__stub/stats (два подчёркивания).
I4/C4 отвечают 500 при fail_ids из stub.env; I5/C5 задерживаются 10 секунд при
timeout_ids. Это режимы окружения, не свойства самих записей fixtures.json.
I6 имеет fail_availability=true: профиль 200, доступность 500 независимо от fail_ids.
Stats.enter вызывается до ожидания; leave выполняется в finally. Служебные вызовы
не учитываются. DELETE stats сохраняет текущий in_flight, поэтому в тестах сброс
делается только после завершения предметных запросов.

## Tests and Measurement

Fake CountingTransport с задержкой 0,3 с измеряет последовательный и параллельный
режимы без сети. max_in_flight == 3/2 — непосредственная проверка параллелизма;
временная проверка относительная с запасом для CI. Дополнительно исходный stub
запускается без изменений через ASGITransport: проверяется настоящий /__stub/stats
и I4/I6/C4. I5/C5 моделируются ReadTimeout: кастомные HTTPX-транспорты сами
должны возбуждать таймаут и не превращают asyncio.sleep в сетевой timeout.

Фактические измерения и команда воспроизведения — README Async-кейс. Тесты
lifecycle проверяют закрытие на успехе/ошибке и отсутствие upstream в readiness.
Весь набор WP-01 с PostgreSQL проверяется вместе с WP-02.
