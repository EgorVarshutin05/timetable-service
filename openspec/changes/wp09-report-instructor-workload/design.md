# Порядок

Path UUID разбирается прежним parse_resource_id: 400 BAD_REQUEST до I/O. Query week
обязателен и документирован исходным pattern. Missing/pattern errors дают 422
fields.week через errors.py. Чистый parse_week дополнительно вызывает
date.fromisocalendar(year, week, 1), отклоняя W00/W54 и невозможную W53.
Неподдерживаемая граница Python date/datetime также возвращает 422, не overflow 500.
Period валидируется до people и SQL. Календарная верхняя граница вычисляется как
локальная полночь следующего понедельника, не как start_utc + 168 часов.

# Слои и семантика

domain/workload.py содержит IsoWeek, WorkloadDay, WeeklyWorkload и Decimal-суммы.
Нет FastAPI, HTTPX, SQLAlchemy или получения текущего времени. Активные статусы и
daily_hours импортируются из существующего domain/scheduling.py без изменения BR-06.
Repository выбирает одного преподавателя и ACTIVE_STATUSES, starts_at < week_end
AND ends_at > week_start; сортировка starts_at/id обеспечивает воспроизводимость.
Отбираются пересекающие интервалы, а не только начинающиеся в неделе.

Service получает только профиль get_instructor; availability/classroom не нужны,
поэтому I6 с недоступной availability остаётся доступен для отчёта. Только
422 INSTRUCTOR_NOT_FOUND превращается в 404 NOT_FOUND с instructor_id в details.
502/504 сохраняются. До успешного профиля сессия БД не открывается. Клиент не меняется.

Интервалы клиппируются до UTC-границ локальной недели и передаются в daily_hours
с timezone преподавателя. Суммы собираются по семи локальным датам Monday..Sunday,
включая нули; bool exceeded вычисляется Decimal-операцией строго > max_hours.
На границе лимита false. В API Decimal переводится в float, date — ISO YYYY-MM-DD.
Отчёт не зависит от Clock.now, статусы COMPLETED включены независимо от давности.

# Транзакции и совместимость

Один SELECT даёт согласованный снимок всех дней; дополнительных advisory/row locks,
commit, мутаций и событий нет. Существующие индексы instructor/time достаточны;
новых миграций/зависимостей/настроек нет. Profile и DB читаются последовательно,
не атомарно между системами — это обычный отчёт, а не резервирование расписания.
OpenAPI уже содержит FR-15; отдельная операция не перечисляет 400, но общий §3.3
TASK и явное уточнение WP-09 задают 400 для malformed path UUID. Контракт не правится.

# Проверки

IsoWeek включает реальные 52/53-недельные годы и пересечение ISO/calendar year.
Pure/API проверяют отказ до I/O, profile error mapping, семь дней, полночь, clipping,
DST-недели 167/169 часов и физические UTC-длительности. PostgreSQL — seed I1 W49,
все шесть статусов, series WP-08, сравнение отчёта с warning BR-06 при 6/6,5 ч,
границы недели, DST и настоящий people-client с одним fake-transport запросом.
