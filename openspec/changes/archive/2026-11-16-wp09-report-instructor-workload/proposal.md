# Зачем

FR-15 (Should) добавляет недельную нагрузку преподавателя. Расчёт должен совпадать
с дневными часами BR-06 и учитывать активные занятия, включая созданные WP-08 серии.

# Что меняется

- Чистый parser ISO-недели и weekly summary, повторно использующий daily_hours.
- Один read-only SQL-запрос по преподавателю, активным статусам и пересечению недели.
- GET /instructors/{instructor_id}/workload с оригинальным Workload response.
- Один get_instructor, без availability/classroom. Его BR-03 422 при отсутствии
  превращается в 404 NOT_FOUND только в контексте этого path resource.
- Canonical Should spec, README, isolated/API/PostgreSQL tests.

# Trade-offs и альтернативы

Выбран один SELECT вместо семи запросов по дням или группировки по UTC-дате в SQL:
он даёт единый MVCC-снимок, а ZoneInfo/daily_hours сохраняют общую семантику BR-06.
Профиль читается перед открытием SQL-сессии. Внешний профиль и БД не образуют
распределённую транзакцию; профиль берётся один раз на отчёт без нового кэша WP-10.
Результат не округляется перед суммой/сравнением; float нужен только в JSON.
Изменение общего people-client на path-specific 404 отвергнуто: create/PATCH/series
по-прежнему требуют 422 INSTRUCTOR_NOT_FOUND для ссылки в теле.

# Влияние

Новые domain/repository/service/router модули; расширены DI и API schemas, main
подключает router. ORM, migrations, Clock, BR-06, broker, people-client, readiness,
конфигурация и нормативные контракты остаются неизменными.
