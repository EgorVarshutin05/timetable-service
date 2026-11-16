# Зачем

Завершить Could FR-16/17: просматривать группы с пагинацией и снижать повторные
обращения к people-stub, сохраняя семантику ошибок и async-кейс FR-12/NFR-07.

# Что меняется

- GET /groups: limit=50 (1..200), offset=0 (>=0), name ASC/id ASC, GroupPage.
- TTL/LRU для успешных профилей, availability и classroom в PeopleClient.
- PEOPLE_CACHE_TTL_SECONDS=30, значение 0 отключает кэш; настройка доступна в compose.
- Single-flight по ключу, bounded storage, явная очистка и reset/lifespan invalidation.
- Canonical specs, README, isolated/API/PostgreSQL tests.
- Конкурентная проверка экземпляров серии (max 4), общий снимок и единый commit.
- Строгий ISO-ввод /__test/time, изолированные readiness probes в unit-тесте.
- PostgreSQL 16 и тестовый DSN в GitHub Actions и GitLab.

# Trade-offs и альтернативы

Ограниченный локальный кэш не требует Redis/cachetools или новых зависимостей.
@lru_cache над async def отвергнут: он сохраняет coroutine, а не её результат,
не реализует TTL и не решает отмену/одновременные miss. Глобальный async lock на
всё HTTP I/O отвергнут: он сериализовал бы независимые 3/2 запроса FR-12.
Выбран TTL 30 секунд вместо неограниченной свежести. Изменения upstream могут
быть невидимы до expiry; нет stale-on-error, negative caching или shared-worker cache.

# Влияние

Group API/schemas/DI, catalog service/repository, cache/people-client, Settings,
reset callback, env/compose. Нормативные HTTP/AsyncAPI, Clock, ORM и миграции,
рабочий readiness, lifecycle и publisher не меняются. Scheduling получает
конкурентную проверку серии; тестовый API и CI обновляются по результатам аудита. Одна старая gated test
fixture отключает кэш, сохраняя assertions о SQL-гонках; новая PG-гонка проверяет
create при включённом кэше.
