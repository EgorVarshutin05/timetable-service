# lesson-lifecycle

## Purpose

Current implemented Must behavior: FR-06, FR-07, FR-08, FR-09, FR-10, BR-08.
Normative contracts: openapi.yaml, asyncapi.yaml, stubs/people-stub/stub-openapi.yaml.

## Requirements

### Requirement: Status transitions are checked first
The service SHALL implement FR-06..FR-09 according to the state diagram and reject
forbidden transitions with 409 TRANSITION_FORBIDDEN and current_status/target_status.

#### Scenario: Repeated confirmation
- **WHEN** confirm is requested for a CONFIRMED lesson
- **THEN** the response is 409 with current_status=CONFIRMED, target_status=CONFIRMED.

#### Scenario: Invalid reschedule interval on a closed lesson
- **WHEN** a CANCELLED lesson is rescheduled with invalid date strings
- **THEN** the response is 409 TRANSITION_FORBIDDEN targeting RESCHEDULED before BR/HTTP.

#### Scenario: Path resource errors
- **WHEN** a lifecycle path has an invalid UUID or its valid UUID is absent
- **THEN** the response is 400 BAD_REQUEST or 404 NOT_FOUND respectively.

### Requirement: Confirm and cancel persist only permitted transitions
The service SHALL confirm only PLANNED and cancel only PLANNED or CONFIRMED.
Both operations SHALL return 200 Lesson, preserving created_at and schedule.

#### Scenario: Optional cancellation reason
- **WHEN** cancel has no body, {}, reason=null, or a string of at most 500 characters
- **THEN** CANCELLED and the nullable reason are persisted and returned as Lesson.

#### Scenario: Incorrect cancellation body
- **WHEN** reason has an incorrect JSON type or exceeds 500 characters
- **THEN** the response is 400 BAD_REQUEST or 422 VALIDATION_ERROR respectively.

### Requirement: Completion obeys BR-08 after status
The service SHALL complete only IN_PROGRESS when Clock.now() >= ends_at.

#### Scenario: Completion before the end
- **WHEN** an IN_PROGRESS lesson is completed before ends_at
- **THEN** the response is 409 LESSON_NOT_FINISHED with ends_at and now as UTC Z.

#### Scenario: Exact end boundary
- **WHEN** an IN_PROGRESS lesson is completed exactly at ends_at
- **THEN** the response is 200 COMPLETED and updated_at equals Clock.now().

### Requirement: Reschedule is atomic and unique
The service SHALL implement FR-08 with required starts_at/ends_at and inherited
optional instructor_id/classroom_id/format; group_id SHALL remain the original's.

#### Scenario: Explicit online classroom null
- **WHEN** a permitted lesson is rescheduled with ONLINE and classroom_id=null
- **THEN** 201 LessonWithWarnings contains a new PLANNED lesson with no classroom.

#### Scenario: Original excluded from rules
- **WHEN** a lesson is moved to an overlapping interval of its own
- **THEN** BR-05 and BR-06 exclude the original and evaluate other active lessons.

#### Scenario: Failure rolls back both records
- **WHEN** a hard rule or the write transaction fails
- **THEN** the original status is unchanged and no successor is persisted.

#### Scenario: Concurrent reschedules
- **WHEN** two valid reschedules target the same original concurrently
- **THEN** exactly one returns 201 and one returns 409 TRANSITION_FORBIDDEN;
  exactly one successor refers to the original.

#### Scenario: PATCH changes an inherited resource during HTTP
- **WHEN** an original's editable fields change before reschedule obtains its locks
- **THEN** reschedule retries from a fresh snapshot and locks the new sorted union.

### Requirement: Sweeper starts all due confirmed lessons
The service SHALL implement FR-10 via lesson-status-sweeper in the existing registry.

#### Scenario: End time has already passed
- **WHEN** CONFIRMED starts_at <= Clock.now(), including ends_at < Clock.now()
- **THEN** the job persists IN_PROGRESS and returns the exact processed count.

#### Scenario: Concurrent or repeated sweeps
- **WHEN** the job runs twice at the same Clock or concurrently
- **THEN** each due lesson is counted once, others are unchanged, and a repeat returns 0.

#### Scenario: Test-mode and periodic invocation
- **WHEN** TEST_MODE=1
- **THEN** the manual endpoint returns processed and no periodic loop runs.
- **WHEN** TEST_MODE=0
- **THEN** the registered job runs periodically and test endpoints return 404.

### Requirement: Commit precedes lifecycle publication
The service SHALL commit successful confirm/cancel/reschedule changes before
publishing their events (FR-13; see lesson-events).

#### Scenario: Commit fails
- **WHEN** a lifecycle commit fails
- **THEN** rollback occurs and no event publisher is invoked.
