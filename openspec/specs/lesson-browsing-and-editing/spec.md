# lesson-browsing-and-editing

## Purpose

Current implemented Must behavior: FR-03, FR-05, BR-01, BR-02, BR-03, BR-04, BR-05, BR-06, BR-07.
Normative contracts: openapi.yaml, asyncapi.yaml, stubs/people-stub/stub-openapi.yaml.

## Requirements

### Requirement: Filtered lesson page

GET /lessons SHALL apply filters with AND and return items and unpaginated total (FR-03).

#### Scenario: Period boundaries
- **WHEN** from/to are supplied with offsets
- **THEN** only lessons with from <= starts_at < to are included
- **AND** an overlapping lesson starting before from is not included

#### Scenario: Resource and status filters
- **WHEN** resource identifiers and status are supplied together
- **THEN** all filters apply and no implicit active-status filter is added

#### Scenario: Pagination and stable ordering
- **WHEN** limit/offset are omitted
- **THEN** defaults 50/0 apply with maximum limit 200
- **AND** order is starts_at then id ascending, including ties

#### Scenario: Empty page
- **WHEN** offset exceeds the filtered count
- **THEN** items is empty and total remains the filtered count

#### Scenario: Invalid query
- **WHEN** limit/offset, UUID, status or offset datetime is invalid, or from >= to
- **THEN** 422 VALIDATION_ERROR includes details.fields before SQL

#### Scenario: Concurrent insert
- **WHEN** a row is inserted after the list snapshot is established
- **THEN** total and items still describe the same snapshot

### Requirement: Status-first partial update

PATCH /lessons/{lesson_id} SHALL edit only PLANNED lessons and retain omitted fields (FR-05).

#### Scenario: Resource lookup
- **WHEN** the path UUID is malformed or no matching lesson exists
- **THEN** 400 BAD_REQUEST or 404 NOT_FOUND is returned respectively

#### Scenario: Noneditable lesson with invalid interval
- **WHEN** a non-PLANNED lesson receives an invalid interval or date value
- **THEN** 409 LESSON_NOT_EDITABLE contains current_status
- **AND** no BR checks or people-stub requests run

#### Scenario: Explicit classroom null
- **WHEN** format=ONLINE and classroom_id=null are explicitly supplied
- **THEN** the classroom is cleared while omitted fields are retained

#### Scenario: Online without clearing old classroom
- **WHEN** only format=ONLINE is supplied for an in-person lesson
- **THEN** 422 VALIDATION_ERROR identifies classroom_id

#### Scenario: Forbidden or empty update
- **WHEN** the body is empty or includes group_id/status
- **THEN** 422 VALIDATION_ERROR is returned

### Requirement: Revalidated atomic PATCH

PATCH SHALL reuse BR-01..BR-07 under union resource locks and row locking (FR-05).

#### Scenario: Successful update
- **WHEN** the merged state passes rules
- **THEN** 200 returns LessonWithWarnings after commit
- **AND** group_id and created_at remain unchanged while updated_at comes from Clock

#### Scenario: Self exclusion
- **WHEN** the lesson is revalidated
- **THEN** it contributes neither its own conflict nor duplicated workload

#### Scenario: Hard rule or upstream failure
- **WHEN** capacity, conflicts or upstream errors reject the update
- **THEN** the original lesson remains unchanged and the normal error code is returned

#### Scenario: Concurrent partial updates
- **WHEN** two requests merge from one initial snapshot
- **THEN** an outdated request repeats with fresh state and both changes are preserved
- **AND** equal frozen updated_at values do not hide the changed snapshot

#### Scenario: Two lessons target one slot
- **WHEN** concurrent PATCH requests move different lessons onto overlapping shared resources
- **THEN** one succeeds and one returns 409 SCHEDULE_CONFLICT

#### Scenario: Status or existence changes during HTTP
- **WHEN** the lesson is confirmed or removed before write locking
- **THEN** the locked reread returns 409 LESSON_NOT_EDITABLE or 404 NOT_FOUND

### Requirement: Public conflict scenario remains valid

The unchanged E2E-02 function SHALL run through conflict rejection and filtered listing.

#### Scenario: Public E2E-02
- **WHEN** the original test executes against PostgreSQL and ASGI API
- **THEN** its assertions pass without modifications
