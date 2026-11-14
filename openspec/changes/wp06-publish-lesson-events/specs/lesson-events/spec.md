## ADDED Requirements

### Requirement: Lifecycle events follow AsyncAPI
The service SHALL implement FR-13 with LessonConfirmed, LessonCancelled and
LessonRescheduled envelopes and payloads exactly as defined in asyncapi.yaml.

#### Scenario: Confirmation
- **WHEN** PLANNED is successfully confirmed
- **THEN** one LessonConfirmed has UUID v4 event_id, operation Clock occurred_at,
  lesson-confirmed:original_id, producer timetable-service, version 1 and lesson payload.

#### Scenario: Cancellation
- **WHEN** PLANNED or CONFIRMED is successfully cancelled
- **THEN** LessonCancelled includes the locked previous_status and nullable reason.

#### Scenario: Rescheduling with a different instructor
- **WHEN** reschedule creates a successor with a different instructor
- **THEN** LessonRescheduled identifies the original lesson/instructor, successor id,
  and exact previous/new intervals with instructor_id and nullable classroom_id.

### Requirement: Publish only after successful commit
The service SHALL publish outside the successful write transaction and SHALL NOT
publish for create, validate, patch, complete, sweeper, seed or reset.

#### Scenario: Independent database observer
- **WHEN** the publisher is invoked
- **THEN** a separate SQL session can already read the committed transition and successor.

#### Scenario: Transaction or rule failure
- **WHEN** validation, status, flush or commit fails
- **THEN** no lifecycle event is published and transactional data is rolled back.

#### Scenario: Concurrent reschedules
- **WHEN** two reschedules race on one original
- **THEN** one succeeds and publishes once; the rejected request publishes nothing.

#### Scenario: Publisher fails after commit
- **WHEN** sending fails after a successful database commit
- **THEN** the standard HTTP 500 is returned without transaction replay;
  the already committed status remains changed and no delivery guarantee is claimed.

### Requirement: RabbitMQ metadata matches the consumer
The service SHALL use durable topic exchange wa.events and timetable.EventType keys,
JSON content_type, message_id=event_id and persistent delivery_mode=2.

#### Scenario: Bound e2e queue
- **WHEN** the e2e consumer has bound timetable.# before confirm
- **THEN** its expected routing key and envelope match the published message.

### Requirement: Kafka metadata matches the consumer
The service SHALL support configured Kafka via topic wa.timetable.events, UTF-8
original lesson id key and event_type header.

#### Scenario: Rescheduled aggregate key
- **WHEN** LessonRescheduled is sent to Kafka
- **THEN** the key is the original lesson id, not the successor id.

### Requirement: Runtime supports the configured broker
The service SHALL import both supported broker drivers without dev/e2e groups.

#### Scenario: Container dependency installation
- **WHEN** uv sync --locked --no-default-groups installs runtime
- **THEN** app.main, aio_pika and aiokafka import without version upgrades.

### Requirement: Real broker readiness and resource lifecycle
The service SHALL implement NFR-01 with concurrent database/broker probes and the
user-requested nested checks response, keeping the people stub out of readiness.

#### Scenario: Both dependencies available
- **WHEN** database and broker RPC succeed
- **THEN** 200 contains status=ok, checks.database=ok, checks.broker=ok.

#### Scenario: Dependency unavailable or timeout
- **WHEN** either dependency fails or times out
- **THEN** 503 contains status=degraded and each corresponding check status.

#### Scenario: Broker reconnect
- **WHEN** an existing transport fails and a later request runs
- **THEN** stale resources are discarded and a new lazy connection is attempted.

#### Scenario: Lifespan failure
- **WHEN** startup, seed or lifespan body fails
- **THEN** broker resources are closed alongside existing people-client and engine.
