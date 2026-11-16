## ADDED Requirements

### Requirement: Generate chronological UTC occurrences
The service SHALL implement FR-14 with count 1..50, unique nonempty MON..SUN weekdays,
offset first_starts_at and duration_minutes 30..480, applying BR-01/02 before I/O.

#### Scenario: Offset crosses UTC date
- **WHEN** the local input is Tuesday 01:00+05:00 and MON is selected
- **THEN** the first occurrence is Monday 20:00Z, using the UTC date and time.

#### Scenario: Next matching day
- **WHEN** the first UTC date is not selected
- **THEN** the nearest later selected weekday is used with the same UTC time.

#### Scenario: Invalid input
- **WHEN** count, duration, weekdays or date offset is invalid
- **THEN** 400 BAD_REQUEST for incorrect JSON types or 422 VALIDATION_ERROR for
  invalid values is returned before database and people-stub calls.

### Requirement: Reuse bounded common external validation
The service SHALL check the group before fetching common people data once, using
the existing gather batch bounded to 3 for IN_PERSON or 2 for ONLINE (BR-03).

#### Scenario: Multi-lesson lookup
- **WHEN** a valid five-occurrence series is requested
- **THEN** only one profile, availability and optional classroom lookup is performed.

#### Scenario: Missing group
- **WHEN** the group is absent
- **THEN** 422 GROUP_NOT_FOUND contains occurrence_index=0 and no people call runs.

### Requirement: Atomic bounded concurrent series assessment
The service SHALL read one snapshot under sorted resource locks and validate occurrences
concurrently with at most four workers. BR-04..07 SHALL consider existing active lessons
and earlier generated occurrences. SQL/ORM SHALL stay outside workers; the lowest failing
index SHALL determine the response regardless of completion order. Inserts SHALL begin
only after every check succeeds, and locks SHALL remain held through the single commit.

#### Scenario: Bounded occurrence validation
- **WHEN** a twelve-occurrence series is checked and checks are delayed
- **THEN** checks overlap with at most four in flight and all finish before persistence.

#### Scenario: Completion order differs from index order
- **WHEN** occurrence 2 fails before occurrence 1 finishes with a hard violation
- **THEN** the response contains the violation for occurrence_index=1 and no rows are inserted.

#### Scenario: Late hard violation
- **WHEN** occurrence 2 conflicts after two valid occurrences
- **THEN** 409 SCHEDULE_CONFLICT contains occurrence_index=2 and no series row commits.

#### Scenario: Capacity violation
- **WHEN** the classroom is too small
- **THEN** 409 CLASSROOM_TOO_SMALL preserves capacity/group_size and adds occurrence_index=0.

#### Scenario: Prefix workload across midnight
- **WHEN** previous and current UTC occurrences share an instructor-local date
- **THEN** BR-06 includes both contributions and returns WORKLOAD_SOFT_LIMIT for the
  current occurrence when the total exceeds the limit.

#### Scenario: Concurrent overlapping requests
- **WHEN** two series or a series and a single lesson target overlapping resources
- **THEN** exactly one operation commits and the other gets 409 without partial inserts.

### Requirement: Persist compatible series membership
The service SHALL return 201 LessonSeriesResult with a common UUID v4 series_id,
PLANNED lessons, per-item warnings and Clock timestamps, without publishing events.

#### Scenario: Existing and new single lessons
- **WHEN** seed or ordinary lessons are read
- **THEN** series_id is null and the response remains valid under the original Lesson schema.

#### Scenario: Series read and later edits
- **WHEN** a series member is read, patched or rescheduled
- **THEN** its series membership is preserved, including in the reschedule successor.

#### Scenario: Reversible migration
- **WHEN** 0001 is upgraded to 0002 and downgraded to 0001
- **THEN** existing lessons survive, new membership is initially null, and downgrade
  removes the membership column/index while retaining lesson records.
