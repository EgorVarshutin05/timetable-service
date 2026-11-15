# Design: WP-04

## Contract Decisions

FR-03: group_id, instructor_id, classroom_id, status, from, to объединяются по И.
Период относится к starts_at: from включительно, to исключительно. Входные даты
со смещением обязательны по общему соглашению TASK/OpenAPI. limit default=50,
1..200; offset default=0 и >=0. Ответ строго {items,total}; total до limit/offset.
Сортировка starts_at ASC, id ASC. Нет фильтра по активности без явного status:
список включает также CANCELLED, RESCHEDULED и COMPLETED.

Случай from>=to контрактом отдельно не определён. Выбрано 422 VALIDATION_ERROR
с details.fields.to. Невалидные query-значения, включая UUID/int/enum, дают 422
VALIDATION_ERROR, как указано в операции списка; неизвестные query-имена игнорируются.
Несуществующий валидный UUID фильтра даёт пустую страницу, а не GROUP_NOT_FOUND.

FR-05: любое непустое подмножество starts_at, ends_at, format, instructor_id,
classroom_id; лишние поля, group_id/status запрещены. Только classroom_id допускает
явный null. Пропущенные значения сохраняются. Новые ORM-строки не создаются.

## Layers and Validation Timing

- domain/listing.py: чистый LessonFilters и проверки диапазона/offset.
- domain/scheduling.py: добавлена чистая проверка ensure_editable; остальные BR переиспользуются.
- repositories/listing.py: общий filtered_query для count и страницы.
- repositories/scheduling.py: union locks и чтение занятия с FOR UPDATE.
- services/lessons.py: чтение списка, статус, HTTP, транзакция и повтор снимка.
- api/schemas.py: LessonQuery и LessonPatch с model_fields_set; публичная JSON Schema
  PATCH выводится из LessonCreate, сохраняя нормативные типы при отложенной проверке.
- api/dependencies.py: query-ошибки преобразуются в 422 fields; метаданные параметров
  для автогенерируемого OpenAPI выводятся из LessonQuery.

Сервис принимает типизированный Callable[[DraftLesson], DraftLesson], а не API-модель.
Это позволяет API валидировать значения по запросу сервиса, не импортируя API в service.
Неверный JSON/форма запроса отклоняются до lookup; значения ISO/UUID/enum и BR — после
проверки статуса. Поэтому CONFIRMED/CANCELLED с неверным интервалом, в том числе
некорректной строкой даты, дают 409 до people-stub. Путь с неверным UUID даёт 400,
отсутствующий ресурс — 404, details LESSON_NOT_EDITABLE содержит current_status.

## PATCH Transaction Protocol

1. Короткая сессия читает занятие без write-lock; проверяет существование и PLANNED.
2. Применяется только переданное подмножество PATCH; validate_early проверяет BR-01/02.
3. Вне write-транзакции people-client выполняет параллельные вызовы для нового состояния.
4. Write-транзакция берёт sorted(set(old_keys union new_keys)), затем SELECT FOR UPDATE.
5. Повторяются существование и PLANNED. Если полный текущий DraftLesson отличается
   от снимка, транзакция завершается без записи, освобождает locks, и шаги повторяются.
6. Группа читается FOR SHARE; assess(...exclude_lesson_id=id, collect_all=False)
   проверяет все правила в исходном порядке. Ошибка откатывает транзакцию.
7. Меняются только пять разрешённых полей и updated_at=Clock.now(); created_at,
   group_id, status и поля отмены/переноса сохраняются. Ответ после commit.

Повтор нужен даже при неизменном updated_at: тест специально замораживает время.
Не захватываются дополнительные ресурсы после row-lock: это нарушило бы общий
порядок и могло создать deadlock. Повтор не является retry upstream-ошибок —
422/502/504 возвращаются сразу. При непрерывном изменении горячего занятия возможны
дополнительные циклы; операция отменяема и не вводит выдуманного кода конфликта.

## List Snapshot and Isolation

Сессия списка устанавливает REPEATABLE READ до первого SELECT. Count и items
видят один снимок даже при параллельной вставке. При закрытии сессии SQLAlchemy
возвращает соединение с исходным уровнем изоляции в пул. Write-сессии по-прежнему
READ COMMITTED: после ожидания advisory lock они видят чужой commit.

## Invariant Protection

| Инвариант | Защита |
| --- | --- |
| PATCH только PLANNED | Чистый ensure_editable до HTTP и после FOR UPDATE |
| Частичное обновление | model_fields_set / exclude_unset, без exclude_none |
| Нет конфликтов с собой | assess exclude_lesson_id в SQL и domain |
| Нет конкурентной коллизии ресурсов | Sorted union advisory xact locks |
| Нет потерянных частичных обновлений | Полное сравнение снимка и повтор слияния |
| Нет частичной записи | session_scope commit/rollback |
| created_at/group_id неизменны | Запрещены в body, не присваиваются в service |
| Все BR соблюдены | validate_early + прежний assess; ограничения WP-01 |
| Стабильная страница | from<=starts_at<to, ORDER BY starts_at,id, общий снимок |

## Tests and Limits

Изолированные проверки параметров, model_fields_set, SQL-предикатов/сортировки,
статусного приоритета, 400/404/422/409/502/504. PostgreSQL проверяет все фильтры,
большую страницу с default=50, UUID tie-break, снимок списка, PATCH/rollback,
явный null, concurrent PATCH и смену статуса/удаление во время HTTP.

Публичные функции E2E-01 и E2E-02 теперь выполняются целиком без изменения assertions
в PostgreSQL + ASGI окружении. Полный Docker/e2e-runner и брокер здесь не запускались;
жизненный цикл/брокер/серии остаются следующими пакетами.
