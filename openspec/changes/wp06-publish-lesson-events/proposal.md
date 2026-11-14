# Зачем

FR-13 требует LessonConfirmed, LessonCancelled, LessonRescheduled после commit.
NFR-01 требует действительной готовности БД и выбранного брокера в контейнере.

# Что меняется

- Чистые снимки событий по неизменённому asyncapi.yaml, включая previous_status
  и старый/новый интервалы переноса.
- EventPublisher в LessonService, вызов только после успешного session_scope.
- Ленивые RabbitMQ/Kafka адаптеры, подтверждение публикации, RPC readiness и close.
- aio-pika и aiokafka входят в runtime dependencies; версии lock не обновляются.
- RabbitMQ остаётся выбранным брокером compose, его топология совпадает с e2e.
- Readiness возвращает вложенный checks по явному уточнению пользователя WP-06.

# Уточнения контракта

Исходные TASK/OpenAPI задавали status/db/broker. Пользователь WP-06 явно запросил
status/checks.database/checks.broker. Применено последнее уточнение с синхронным
изменением Readiness и examples в openapi.yaml; это несовместимое изменение,
поэтому соответствующий commit и merge имеют ! и описание в README.
AsyncAPI и публичные e2e assertions не меняются.

# Trade-offs

Выбран RabbitMQ: compose уже запускает его, routing keys и publisher confirms
достаточны трём событиям. Kafka адаптер оставлен рабочим, поскольку Settings уже
объявляет этот backend. Удаление поддержки Kafka упростило бы код, но меняло бы
существующую конфигурацию; обе runtime-зависимости уже зафиксированы в lock.
Ленивое соединение вместо обязательного startup позволяет процессу жить и
возвращать readiness=503 при отказе брокера. Общий lock соединения проще пула
каналов, но сериализует отправки одного процесса. Не добавлены outbox и скрытые
повторы отправки: неизвестный исход acknowledgement не означает отсутствия сообщения.

# Влияние

domain/events — данные; broker — транспорт/health; services — граница commit;
main/context/dependencies — DI и cleanup; health/OpenAPI — уточнённый response.
Данные БД, lifecycle, conflict locks, Clock, people-client и seed не меняются.
