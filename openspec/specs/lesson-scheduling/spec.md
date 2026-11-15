# lesson-scheduling

## Purpose

Current implemented Must behavior: FR-01, FR-02, BR-01, BR-02, BR-03, BR-04, BR-05, BR-06, BR-07.
Normative contracts: openapi.yaml, asyncapi.yaml, stubs/people-stub/stub-openapi.yaml.

## Requirements

### Requirement: Ordered early validation

The service SHALL require offset datetimes and ends_at > starts_at with duration
30 minutes..8 hours inclusive (BR-01), then apply classroom/format BR-02
before database and upstream calls (FR-01, FR-02).

#### Scenario: Invalid duration and missing classroom
- **WHEN** both duration and classroom violate rules
- **THEN** 422 VALIDATION_ERROR identifies ends_at
- **AND** neither database nor people-stub is called

#### Scenario: Date without offset
- **WHEN** an input timestamp omits its offset
- **THEN** 422 VALIDATION_ERROR includes details.fields

#### Scenario: Wrong JSON type
- **WHEN** a timestamp is a number or format is an object
- **THEN** 400 BAD_REQUEST is returned

#### Scenario: Format/classroom mismatch
- **WHEN** IN_PERSON lacks a classroom
- **THEN** 422 CLASSROOM_REQUIRED is returned
- **AND** ONLINE with a classroom yields 422 VALIDATION_ERROR

### Requirement: Group existence precedes upstream

The service SHALL check the group before fetching people data (BR-03).

#### Scenario: Group missing
- **WHEN** the group does not exist
- **THEN** 422 GROUP_NOT_FOUND is returned without calling people-stub

#### Scenario: Upstream error
- **WHEN** the people client raises missing entity, failure or timeout
- **THEN** its 422, 502 or 504 platform error is preserved without inserting a lesson

### Requirement: Capacity and complete conflict detection

The domain SHALL apply BR-04 before BR-05 and report every overlapping resource.
Overlap SHALL mean existing.starts_at < new.ends_at AND existing.ends_at > new.starts_at.
PLANNED, CONFIRMED, IN_PROGRESS and COMPLETED SHALL be active;
CANCELLED and RESCHEDULED SHALL be excluded.

#### Scenario: Capacity and overlap violated
- **WHEN** the classroom is too small and an overlap exists
- **THEN** create returns 409 CLASSROOM_TOO_SMALL with capacity and group_size
- **AND** validate returns both violations in table order

#### Scenario: All resource conflicts
- **WHEN** active lessons overlap by instructor, classroom or group
- **THEN** all matches are returned in stable resource/UUID order with UTC intersection

#### Scenario: Touching or inactive lesson
- **WHEN** intervals only touch or a record is CANCELLED/RESCHEDULED
- **THEN** that record does not cause a conflict

#### Scenario: Existing lesson excluded
- **WHEN** exclude_lesson_id is supplied internally
- **THEN** that lesson contributes neither conflicts nor workload

### Requirement: Calendar-aware soft rules

The domain SHALL compute BR-06 and BR-07 using the instructor IANA timezone.

#### Scenario: Midnight crossing
- **WHEN** a lesson spans two local dates
- **THEN** elapsed hours are split at local midnight and each date is checked separately

#### Scenario: DST and exact midnight
- **WHEN** the clock offset changes or the lesson ends at midnight
- **THEN** UTC elapsed hours are used without adding an empty next-day contribution

#### Scenario: Exact limit
- **WHEN** workload equals max_hours_per_day
- **THEN** WORKLOAD_SOFT_LIMIT is absent

#### Scenario: Availability
- **WHEN** the full lesson does not fit one weekly window
- **THEN** INSTRUCTOR_UNAVAILABLE is returned without blocking the operation

### Requirement: Transactionally safe creation

POST /lessons SHALL create one PLANNED lesson only after successful hard checks (FR-01).

#### Scenario: Valid creation
- **WHEN** no hard rules are violated
- **THEN** 201 returns LessonWithWarnings with UUID v4 and timestamps from Clock
- **AND** created_at equals updated_at, with null cancellation/rescheduling fields

#### Scenario: Parallel conflicting creates
- **WHEN** two requests share an instructor, group or classroom and overlap
- **THEN** one succeeds with 201 and the other returns 409 SCHEDULE_CONFLICT
- **AND** exactly one row is inserted

#### Scenario: Rejected transaction
- **WHEN** a hard rule rejects a request
- **THEN** no row is inserted and transaction locks are released

### Requirement: Side-effect-free validation

POST /lessons/validate SHALL use the same rules without persistence (FR-02).

#### Scenario: Violations
- **WHEN** BR-04 or BR-05 is violated
- **THEN** 200 returns ok=false with violations and warnings

#### Scenario: Valid preliminary check
- **WHEN** no hard rules are violated
- **THEN** 200 returns ok=true and no lesson is created or slot reserved
