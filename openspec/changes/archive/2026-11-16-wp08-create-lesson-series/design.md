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
lookup не нужны. Так внешний fan-out ограничен 3/2, а не 150 запросами.

В одной session_scope захватывается sorted union advisory resource keys всей серии,
группа читается FOR SHARE. Один SELECT загружает доменные snapshots активных занятий
для всего периода серии и локальных дней нагрузки. SQL и ORM не передаются потокам.
BR-04..07 для экземпляров выполняются через asyncio.to_thread с Semaphore(4),
не более четырёх проверок одновременно. Каждый worker получает общий immutable
снимок БД и prefix ранее сгенерированных PLANNED-экземпляров с заранее выделенными UUID.
Это сохраняет внутрисерийные конфликты и накопление нагрузки через локальную полночь.
Общий алгоритм assess_snapshot используется также одиночными командами.

Все workers завершаются до выбора первой ошибки по индексу; скорость завершения
не влияет на ответ. Только при успехе всех проверок выполняются INSERT/flush и commit.
Locks удерживаются до commit; конкурентные create/PATCH/reschedule/series используют
те же ключи. Отдельный AsyncSession на worker и конкурентные SQL-вызовы не нужны.
Потоки дают конкурентную обработку чистых функций; CPU-ускорение под GIL не обещается.

ApiError hard-оценки дополняется occurrence_index=i с сохранением исходных details
(capacity/group_size или conflicts). Общая отсутствующая ссылка/отказ people
помечается index=0. Структурные ошибки тела/параметров не имеют экземпляра и
возвращаются с обычными fields/errors. Early BR-02 у первого draft также index=0.

С одним общим набором ресурсов locks дедуплицируются до 2/3 ключей, но используется
тот же union API, что в PATCH/reschedule. Серии конкурируют безопасно с другими
сериями и одиночным create. При конфликте позднего экземпляра ни один INSERT серии не выполняется;
commit возможен только после всех проверок.
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
