# Контракт и слои

domain/events.py строит UUID v4, конверт и payload из чистого DraftLesson.
occurred_at передаётся из Clock в момент операции, даты сериализуются to_iso_z.
Envelope имеет ровно семь обязательных полей. Ключ — lesson-<verb>:original_id.
cancel captures previous_status до изменения под row-lock. reschedule root
instructor_id — старый преподаватель; previous/new содержат оба интервала и
instructor_id/classroom_id, rescheduled_to_id ссылается на новый PLANNED.

# После commit

Снимок создаётся в той же write-транзакции, что и изменение, но publish вызывается
только после выхода из session_scope. Позднейший Clock/read DB не переопределяет
данные события. Ошибка flush/commit, запрет перехода и BR не достигают publish.
create/validate/patch/complete/sweeper/seed/reset не имеют вызовов publisher.
None у publisher допускается только для прежних прямых изолированных конструкторов;
build_context всегда передаёт настоящий клиент, тестовая API-фикстура — capture.

Публикация ожидает acknowledgement. Отказ publisher не переигрывает транзакцию
и не откатывает уже совершённый переход. HTTP получает стандартный 500 каркаса.
Без outbox возможны commit без сообщения и неизвестный исход при потере ack;
строгое «любой HTTP 5xx означает отсутствие события» нельзя гарантировать между
двумя независимыми системами. Тесты гарантируют отсутствие публикации до commit
и при достоверном отказе отправки. Для неизвестного исхода нужны дедупликация по
idempotency_key и, при усилении требований, durable outbox. Такой гарантии WP-06
не приписывает себе; TASK §3.4 разрешает Must-реализацию публикацией после commit.

# RabbitMQ

aio_pika.connect используется лениво под asyncio.Lock. Channel включает
publisher_confirms=True; durable topic exchange settings.rabbitmq_exchange
(wa.events). BROKER_URL остаётся env-источником settings.rabbitmq_url.
JSON UTF-8, content_type=application/json, message_id=event_id, delivery_mode=2,
routing key timetable.<EventType>. Дополнительные headers не требуются.
mandatory=False: отсутствие подписчиков не превращает корректный переход в 500;
publisher не создаёт бизнес-очередь за потребителей. Без bound queue сообщение
может быть отброшено RabbitMQ даже при persistent delivery. E2E открывает очередь
e2e.timetable с exclusive/auto_delete и binding timetable.# до HTTP-запроса.

Сбой/отмена отправки закрывает и сбрасывает соединение без replay. Следующая
операция/проверка откроет новое. ping выполняет passive exchange.declare RPC,
а не только проверяет is_closed. RPC-ошибка также сбрасывает ресурсы под lock.

# Kafka

Ленивый AIOKafkaProducer с acks=all, topic wa.timetable.events, key original_id
в UTF-8, header event_type. ping использует отдельный AdminClient describe_cluster
без публикации тестовых сообщений; admin закрывается даже при ошибке start.
Kafka профиль compose по-прежнему требует переключения defaults/профилей по
инструкции scaffold. Основной выбранный broker остаётся RabbitMQ.

# Lifespan и готовность

Один объект broker реализует EventPublisher, BrokerProbe и aclose; AppContext
хранит эти роли. AsyncExitStack закрывает people-client, брокер, engine, включая
ошибки startup/seed и тела lifespan. Конструкторы не открывают сеть, поэтому
изолированные тесты не требуют брокера. Production не подменяется no-op publisher.
Health запускает БД и брокер через gather с отдельными таймаутами каркаса; people
не участвует. Ответ по уточнению пользователя: status + checks.database/broker.
Только Readiness в OpenAPI изменён; это явное исключение из исходного формата.

# Runtime и проверка

Обе библиотеки добавлены в project.dependencies без upgrade package versions.
Отдельная venv с --locked --no-default-groups импортирует приложение/драйверы;
pytest в ней отсутствует. Dockerfile менять не нужно. Тесты подтверждают AMQP
properties, Kafka key/header, reconnect/close, схему всех трёх событий, commit
в независимой SQL-сессии и исходный E2E-17 с capture. Внешний брокер/Docker
не запускались и не подменяются в отчёте утверждением о сетевом e2e.
