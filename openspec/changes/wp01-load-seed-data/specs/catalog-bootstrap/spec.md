## ADDED Requirements

### Requirement: Reproducible catalog schema

The service SHALL create groups and lessons using Alembic revision 0001 (NFR-02).
The database SHALL enforce the structural invariants listed in design.md.

#### Scenario: Fresh database upgrade
- **WHEN** alembic upgrade head runs against an empty PostgreSQL 16 database
- **THEN** both tables, foreign keys, CHECK constraints, unique constraint and indexes exist
- **AND** alembic check reports no new upgrade operations

#### Scenario: Invalid lesson persistence
- **WHEN** SQL writes an invalid interval, classroom/format combination, status or reference
- **THEN** PostgreSQL rejects the transaction

#### Scenario: Duration boundaries
- **WHEN** a lesson lasts exactly 30 minutes or exactly 8 hours
- **THEN** the duration CHECK accepts the row

#### Scenario: Duplicate rescheduling source
- **WHEN** two lessons reference the same non-null rescheduled_from_id
- **THEN** the unique constraint rejects the second row

### Requirement: Idempotent deterministic seed

The service SHALL load fixed seed UUIDs and timestamps without people-stub (NFR-03).
Repeated seed SHALL preserve existing rows and add missing rows without duplicates.

#### Scenario: Repeated seed
- **WHEN** the supplied seed runs twice
- **THEN** there are three groups and eight lessons
- **AND** existing edits remain unchanged

#### Scenario: Referenced row follows its child
- **WHEN** lessons.json records are inserted in reverse order
- **THEN** the L7 to L6 reference remains valid at commit

#### Scenario: Seed on startup
- **WHEN** SEED_ON_START=1 and lifespan starts
- **THEN** seed is committed before requests are served
- **AND** with SEED_ON_START=0 no automatic seed insertion occurs

### Requirement: Atomic reset preserves Clock

POST /__test/reset SHALL restore seed atomically without changing Clock (NFR-03).

#### Scenario: Successful repeated reset
- **WHEN** TEST_MODE=1, Clock is frozen and reset is called twice after data changes
- **THEN** both responses are 204 without a body
- **AND** seed is restored, extra rows disappear and Clock stays frozen at the same instant

#### Scenario: Failed reset
- **WHEN** seed insertion fails after TRUNCATE
- **THEN** the transaction restores all pre-reset data

#### Scenario: Test mode disabled
- **WHEN** TEST_MODE=0 and a test endpoint is requested
- **THEN** the response is 404 NOT_FOUND

### Requirement: Read group by path identifier

GET /groups/{group_id} SHALL return the Group schema directly (FR-11).

#### Scenario: Existing group
- **WHEN** a seed group identifier is requested
- **THEN** the response is 200 and matches Group in openapi.yaml

#### Scenario: Missing group
- **WHEN** the identifier is a valid UUID but the group does not exist
- **THEN** the response is 404 NOT_FOUND in the platform Error format

#### Scenario: Malformed group identifier
- **WHEN** group_id is not a UUID
- **THEN** the response is 400 BAD_REQUEST before querying the database

### Requirement: Read lesson by path identifier

GET /lessons/{lesson_id} SHALL return the Lesson schema directly (FR-04).
All timestamps SHALL be serialized through to_iso_z.

#### Scenario: Existing lesson
- **WHEN** any seed lesson identifier is requested
- **THEN** the response is 200 and matches Lesson in openapi.yaml
- **AND** nullable fields are included, with timestamps in UTC ending with Z

#### Scenario: Missing lesson
- **WHEN** the identifier is a valid UUID but the lesson does not exist
- **THEN** the response is 404 NOT_FOUND in the platform Error format

#### Scenario: Malformed lesson identifier
- **WHEN** lesson_id is not a UUID
- **THEN** the response is 400 BAD_REQUEST before querying the database

#### Scenario: Datetime with another offset
- **WHEN** the response model receives a timestamp with an offset and microseconds
- **THEN** the timestamp is converted to UTC with Z and microseconds are removed
