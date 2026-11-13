# Design: WP-03

## Scope and Layers

FR-01, FR-02, BR-01..BR-07. API занимается типами/ISO 8601 и сериализацией.
domain/scheduling.py содержит чистые функции и снимки без FastAPI, HTTPX и ORM.
repositories/scheduling.py содержит SQL и advisory locks. services/lessons.py
упорядочивает I/O, правила, транзакции и создание. Clock, ApiError, session_scope,
PeopleClient и ORM-модели переиспользуются.

## Ordering

1. API отклоняет неверный JSON/тип (400) и невалидное значение/offset (422 fields).
2. validate_early проверяет aware, UTC, BR-01 и BR-02 до любых БД/HTTP.
3. Короткая сессия проверяет группу: отсутствие → 422 GROUP_NOT_FOUND.
4. После закрытия сессии people-client получает данные параллельно; его ошибки сохраняются.
5. Новая транзакция берёт advisory locks, повторно читает группу FOR SHARE.
6. BR-04 → BR-05 → BR-06 → BR-07. Create останавливается на первом hard-нарушении;
   validate собирает обе 409-проблемы и предупреждения.
7. Create добавляет PLANNED, UUID v4 и фиксирует commit до формирования ответа.
   Validate не вставляет строки и не публикует событий.

Статус существующего занятия в этом пакете отсутствует: команды PATCH/reschedule
следующих пакетов должны сначала проверить статус, затем validate_early, захватить
объединение старых/новых ресурсов в том же порядке и вызвать assess с exclude_lesson_id.

## Invariant Protection

| Инвариант | Защита |
| --- | --- |
| BR-01 длительность и порядок | Чистый domain; существующие CHECK WP-01 |
| BR-02 формат/аудитория | Чистый domain; существующий CHECK WP-01 |
| BR-03 группа | До HTTP; повторное чтение FOR SHARE; существующий FK |
| BR-03 преподаватель/аудитория | Existing people-client и его маппинг ошибок |
| BR-04 вместимость | Чистый domain, размер группы защищён FOR SHARE |
| BR-05 пересечения | Чистый domain + общие xact locks до SELECT и до commit |
| BR-06 дневная нагрузка | Чистый domain, тот же lock преподавателя |
| BR-07 окно | Чистый domain и IANA ZoneInfo преподавателя |
| Нет частичной записи | session_scope commit/rollback, flush внутри транзакции |
| Даты now | Только Clock; created_at и updated_at из одного вызова |

## Concurrency

Ключ = signed bigint от BLAKE2b-64 строки timetable:<resource>:<UUID>.
Ресурсы instructor/group и непустая classroom блокируются в sorted(set(keys)).
Используется pg_advisory_xact_lock, а не session lock: commit/rollback освобождают
его автоматически. SQL-параметр имеет тип BigInteger.

Изоляция engine явно READ COMMITTED. SELECT с ожиданием lock и последующий SELECT
кандидатов — разные SQL statements: второй видит commit предыдущего владельца.
В REPEATABLE READ снимок мог бы устареть до ожидания, поэтому этот уровень здесь
не используется. Advisory locks кооперативны: будущие записи/изменения расписания
обязаны использовать тот же протокол. Произвольный внешний SQL его не соблюдает.

Кандидаты отбираются по активным статусам и пересечению ресурса/интервала либо
по преподавателю и UTC-границам затронутых локальных дат. Domain повторяет фильтры
для самостоятельной тестируемости. exclude_lesson_id учитывается в SQL, BR-05 и BR-06.

## Domain Decisions

Активны PLANNED, CONFIRMED, IN_PROGRESS, COMPLETED; CANCELLED/RESCHEDULED исключены.
Интервалы полуоткрытые: касание не конфликт. Для одной записи могут быть три
конфликта. Порядок details.conflicts: instructor → classroom → group, внутри UUID
лексикографически. overlap — фактическое пересечение в UTC через to_iso_z.

Нагрузка: интервалы разрезаются по полуночам ZoneInfo(instructor.timezone), каждая
часть считается как UTC elapsed time с Decimal. Ровно в полночь следующий день
не получает нулевую часть. Предупреждение формируется по каждой затронутой дате
при строгом превышении нормы. Даты сортируются, затем идёт INSTRUCTOR_UNAVAILABLE.

BR-07 требует одного целого окна; соседние окна не объединяются. Границы окна
конструируются в локальной дате и переводятся в UTC. Допускается ends_at == to.
Для неоднозначной DST-границы выбран fold=0; окно с несуществующей локальной
границей в этот день не используется. Это явная трактовка неописанной ситуации.

## Contract

LessonCreate: лишние поля запрещены. Даты принимаются только строками ISO 8601
с offset/Z; unix timestamp и числовые строки не принимаются. Неверный JSON/тип,
включая формат enum не строкой и неправильный UUID, → 400 BAD_REQUEST; неверное
значение/отсутствие offset → 422 VALIDATION_ERROR с details.fields.

Create: 201 {lesson,warnings}; BR-04/05: 409 Error, первое нарушение.
Validate: 200 {ok,violations,warnings}; violations содержит только code/message/details,
без http_status и обёртки error. 400/422/502/504 остаются ошибками тех же кодов.
Нормативный openapi.yaml не меняется. Все исходящие timestamps используют to_iso_z.

## Verification and Limits

Изолированные domain/API-тесты; PostgreSQL с отдельной временной схемой на тест;
исходная функция E2E-01 вызывается без изменения assertions. Для E2E-02 проверены
точный конфликт и отсутствие вставки через SQL: финальный GET /lessons принадлежит
WP-04. Полный исходный E2E-02 до WP-04 не объявляется зелёным. Docker Compose
и внешние брокерные e2e здесь не запускались; readiness брокера относится к WP-06.
