# Слои

domain/series.py содержит только структуры, пределы и генерацию: count 1..50,
duration 30..480, непустой уникальный набор MON..SUN, aware first_starts_at.
Сначала astimezone(UTC), затем weekday и суточный шаг; локальная дата входного
offset не определяет weekday. Начальное время UTC сохраняется, первый подходящий
день включается. BR-01/02 проверяют все drafts до БД и HTTP; переполнение дат — 422.
api/schemas.py проверяет форму JSON, offset, enum/unique/ranges и строит SeriesPlan.
Неверные JSON-типы — 400, семантика — 422 через прежний errors.py.

# Транзакция

Короткая сессия проверяет группу до HTTP. Один people_client.get_lesson_people
параллельно получает profile/availability/classroom (3) или ONLINE без classroom (2).
Данные преподавателя/аудитории одинаковы для всей серии, поэтому 50 повторных
lookup не нужны. Так внешний fan-out ограничен 3/2, а не 150 запросами. TASK §4
упоминает bounded concurrent validation; межэкземплярный assess упорядочен по
явным требованиям WP-08 для первого нарушения и накопления нагрузки. AsyncSession
не является конкурентным task-safe объектом; gather над его запросами отвергнут.

В одной session_scope: sorted union advisory resource keys всей серии; группа
FOR SHARE; один Clock.now; затем индекс 0..count-1. Каждый assess(collect_all=False)
использует прежние BR-04..07, читая БД после захвата locks в READ COMMITTED.
Успешный INSERT/flush остаётся незакоммиченным, но виден следующему SELECT своей
транзакции. Поэтому self-series conflicts и дневная нагрузка включают предыдущие
экземпляры. Warnings относятся к префиксу серии в момент проверки, не пересчитывают
предыдущие ответы задним числом. При первом hard error вся транзакция откатывается.
Чистый workload/conflict алгоритм не продублирован в новом service.

ApiError hard-оценки дополняется occurrence_index=i с сохранением исходных details
(capacity/group_size или conflicts). Общая отсутствующая ссылка/отказ people
помечается index=0. Структурные ошибки тела/параметров не имеют экземпляра и
возвращаются с обычными fields/errors. Early BR-02 у первого draft также index=0.

С одним общим набором ресурсов locks дедуплицируются до 2/3 ключей, но используется
тот же union API, что в PATCH/reschedule. Серии конкурируют безопасно с другими
сериями и одиночным create. При конфликте позднего экземпляра предыдущие flush
не становятся частичной записью: commit только после последней проверки.
group_id/instructor_id/classroom_id в одной серии не меняются.

# Сохранение и совместимость

UUID v4 series_id один на запрос, у каждого Lesson отдельный UUID v4, PLANNED,
rescheduled_from_id=null, cancel_reason=null, одинаковые created_at/updated_at
из одного Clock. Return происходит после commit; publisher не вызывается.
Существующие одиночные/seed записи получают null. Поле допустимо исходным OpenAPI
и теперь всегда сериализуется. PATCH не редактирует series_id; reschedule
сохраняет membership. Решение для переноса контрактом отдельно не оговорено и
фиксируется в README; оно не меняет AsyncAPI payload.

0002 добавляет nullable UUID без server default и обычный индекс series_id.
Ограничение уникальности для series_id было бы ошибкой: члены обязаны делить UUID.
Upgrade сохраняет старые строки; downgrade удаляет индекс/колонку, сохраняя занятия,
но теряет информацию принадлежности. 0001 и существующие CHECK/FK/UNIQUE не меняются.

# Проверки

Pure UTC/date bounds, JSON shape/ranges/offset, error index и rollback API без сети;
PostgreSQL: late conflict index=2, capacity index=0, общий UUID, GET, 1/50 и 30/480,
BR-06 через локальную полночь, две серии с поздним пересечением, single/series race,
membership PATCH/reschedule, отсутствие create events. Старые 438 тестов сохраняются.
Отдельная disposable БД проверяет реальные Alembic upgrade/downgrade/check и old rows.
