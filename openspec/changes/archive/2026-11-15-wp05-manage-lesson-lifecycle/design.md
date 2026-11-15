# Слои и инварианты

domain/lifecycle.py не импортирует FastAPI, HTTPX или SQLAlchemy. ensure_transition
задаёт разрешённые рёбра рисунка 1 TASK; ensure_complete сначала проверяет статус,
затем BR-08, используя переданный now. is_due не учитывает ends_at намеренно.
api/schema наследует механизм отложенных значений WP-04 для RescheduleRequest:
обязательны обе даты; instructor/classroom/format наследуются; явный null аудитории
сохраняется через model_fields_set/exclude_unset. CancelRequest необязателен,
reason nullable, максимум 500. Неверная форма JSON проверяется при входе.

# Статусные команды

confirm/cancel/complete берут SELECT FOR UPDATE и повторно читают существование
и статус в READ COMMITTED. Сначала 404/TRANSITION_FORBIDDEN; complete затем BR-08.
Одного Clock.now достаточно для проверки и updated_at. created_at и расписание
не меняются. Эти команды не запрашивают people-stub и не берут advisory locks:
не добавляют активных интервалов, поэтому не могут создать новый конфликт BR-05.
Отмена может снять конфликт; параллельное создание вправе линеаризоваться до неё.
Они никогда не ожидают resource locks, удерживая строку.

# Перенос

1. Короткое чтение исходного занятия, 404 и статус PLANNED|CONFIRMED.
2. Применение тела к снимку, validate_early BR-01/02, параллельный people-client.
3. Write-транзакция: sorted(set(old_keys | new_keys)), затем FOR UPDATE исходного.
4. Повторные 404/статус; изменившийся DraftLesson приводит к освобождению locks
   и повтору шага 1. updated_at не является версией при замороженном Clock.
5. Группа FOR SHARE; assess(exclude_lesson_id=id, collect_all=False). Исходное
   занятие не участвует ни в BR-05, ни в BR-06. Первое нарушение откатывает всё.
6. Один now: исходное RESCHEDULED, updated_at=now; новое PLANNED, та же группа,
   rescheduled_from_id=исходное, cancel_reason=null, created_at=updated_at=now.
7. Flush и commit вместе. Второй перенос после ожидания видит RESCHEDULED и 409.

Уникальный rescheduled_from_id и FK миграции WP-01 — последняя защита целостности.
При постоянных PATCH возможны повторные циклы; upstream-ошибки не ретраятся.

# Sweeper и конкуренция

Job зарегистрирован в build_context через замыкание над session_factory и Clock.
Существующие testing router и периодический цикл используются без дублирования.
TEST_MODE=1 отключает цикл; ручной запуск возвращает processed после commit.
При TEST_MODE=0 job запускает штатный run_periodically, интервал из Settings.
now фиксируется один раз на запуск. SELECT CONFIRMED AND starts_at<=now ORDER BY id
FOR UPDATE удерживает строки до commit. PostgreSQL READ COMMITTED после ожидания
перепроверяет WHERE; повторный sweeper/отмена/перенос сериализуются. Нет skip_locked:
запуск ждёт строку, затем обрабатывает актуальный набор. Все переведённые строки
получают IN_PROGRESS и один updated_at. Другие статусы/будущие строки не меняются.

# Граница событий WP-06

confirm/cancel вызывают транзакционный _transition, затем имеют отдельную точку
перед return. reschedule также выходит из session_scope перед return.
Здесь WP-06 подключит publisher с committed Lesson/исходным id. При ошибке flush,
commit или отказе статуса исполнение до этой точки не дойдёт. Сейчас реальных
событий нет, поэтому полный E2E-17 не объявляется пройденным. Окно сбоя между
commit и будущей публикацией остаётся известным trade-off без outbox из TASK §3.4.
