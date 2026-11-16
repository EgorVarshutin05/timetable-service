# Список групп

API GroupQuery валидирует limit/offset до SQL; dependency переводит все ошибки
пагинации в 422 VALIDATION_ERROR fields, как контракт GET /groups. Query metadata
содержит исходные defaults/min/max. Repository делает COUNT и SELECT с ORDER BY
name ASC, id ASC, limit/offset. Service открывает REPEATABLE READ снимок до COUNT,
поэтому items и total согласованы при конкурентных вставках. Offset за концом
списка возвращает [] и прежний total. Имена сортирует PostgreSQL в collation БД;
никакой сортировки только уже выбранной страницы в Python нет.

# Кэш интеграционного слоя

clients/cache.py — типизированный AsyncTTLCache[T], без HTTP и ORM. PeopleClient
имеет три независимых экземпляра для frozen domain values: profile, availability,
classroom, ключ каждого — UUID. Только полностью проверенные 200 с совпадающим ID
заселяются в кэш. Ошибки 404/5xx/timeout/network/malformed payload и отмена не
сохраняются. Общий маппинг ApiError и приоритет gather не меняются.

TTL измеряется time.monotonic, начиная с успешного окончания загрузки. Это время
жизни процесса, а не бизнес-время Clock: freeze/jump Clock не меняет TTL. Настройка
PEOPLE_CACHE_TTL_SECONDS по умолчанию 30, диапазон 0..3600, finite; 0 полностью
отключает хранение/coalescing. Значения действуют до deadline, на deadline истекают.
LRU ограничен 256 значениями на тип, максимум 768 на PeopleClient; expired values
удаляются при обращении. Перезапуск очищает всё; shared cache между workers нет.

# Конкуренция и инвалидация

RLock защищает короткие операции над metadata, никогда не удерживается при await.
Async lock на конкретный ключ с refcount объединяет одновременные успешные miss.
Разные UUID и разные типы запросов не блокируют друг друга. HTTPX и async locks
используются из одного owner event loop на lifespan; AsyncClient не делится между
event loops. Синхронный clear_cache безопасен также из другого потока.

Отмена владельца освобождает ключ; ожидающий caller повторяет fetch. Отмена waiter
не отменяет владельца. Ошибки не делятся как отрицательный результат: следующий
caller снова пробует upstream. Нет detached background tasks, ресурсы закрывает
прежний AsyncExitStack. В finally refcount удаляет ненужные key locks.

clear_cache очищает три типа и увеличивает generation. Уже выполняющийся запрос
может вернуть результат своему caller, но generation не позволит старому ответу
восстановить очищенную запись. Reset вызывает clear_cache после успешного DB reset,
не меняя Clock; aclose очищает кэш и закрывает HTTPX. После close cached hits также
запрещены. Изменение конфигурации/фикстур самого stub не отправляет push invalidation:
для немедленной свежести нужны clear_cache/reset или TTL=0.

# Совместимость и проверки

FR-12 всегда сохраняет gather над 3/2 методами; счётчик именно сетевых запросов
3/2 доказывается на cold cache или TTL=0. На warm cache меньше I/O — цель FR-17.
У старых SQL-race тестов HTTP GateTransport ждёт дублирующиеся запросы: только
gated_people fixture получает TTL=0. Assertions неизменны. Новый PG-тест с TTL=30
доказывает один 201 и один 409 при двух create с одним shared people lookup.

Pure/API cache tests используют fake monotonic timer без sleep для TTL; concurrency
имеет Event barriers/короткую transport delay. PG проверяет seed/name ties/pages,
снимок при INSERT между COUNT/items, reset/Clock, workload profile TTL и create race.
События на GET/reset/cache не публикуются. Новых domain rules, миграций и зависимостей нет.

# Исправления финального аудита

# Слои и инварианты

domain.scheduling.assess_snapshot проверяет BR-04..07 над immutable snapshots.
repository загружает общий набор кандидатов после захвата всех resource locks;
service добавляет prefix с UUID будущих занятий и ограничивает workers Semaphore(4).
Результаты gather читаются по индексу, запись начинается только после успеха всех
проверок. SQL/ORM остаются в event loop, locks — до единого commit.

TestTime валидирует now до pydantic datetime coercion через parse_iso_datetime.
Общий errors.py классифицирует datetime_type как 400 BAD_REQUEST, ValueError как
422 VALIDATION_ERROR. Отказ происходит до обработчика и не меняет Clock.

Readiness unit-test использует AsyncMock для database/broker probes. HTTPX transport
people-stub по-прежнему падает при любом обращении; calls остаётся пустым.
Рабочие readiness endpoints не меняются.

# CI

GitHub Actions использует PostgreSQL 16 service с healthcheck и host port 5432.
GitLab использует service alias postgres. Оба check job задают WP01_TEST_DATABASE_URL;
тесты создают/удаляют собственные схемы. Невозможность соединиться теперь проваливает
тесты БД вместо skip из-за отсутствующего DSN. Конфигурации mypy/ruff не ослабляются.

# Проверка

Края типов/строк времени, сохранение Clock при ошибке, нормализация offset;
readiness без реальных probes; bounded concurrency, первый индекс ошибки,
prefix workload и PostgreSQL races; полный pytest, ruff, mypy и Docker/public e2e.
