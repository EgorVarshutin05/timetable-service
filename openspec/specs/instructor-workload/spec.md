# instructor-workload

## Purpose

Current Should behavior FR-15, consistent with BR-06, using the original Workload contract.

## Requirements

### Requirement: Validate path and real ISO weeks before I/O
The service SHALL implement FR-15 with required YYYY-Www, validated by the actual
ISO calendar, and SHALL reject invalid week parameters before people or database calls.

#### Scenario: Malformed path UUID
- **WHEN** instructor_id in the path is not a UUID
- **THEN** 400 BAD_REQUEST is returned without HTTP or SQL calls.

#### Scenario: Missing or impossible week
- **WHEN** week is absent, malformed, W00/W54, or W53 in a 52-week year
- **THEN** 422 VALIDATION_ERROR includes details.fields.week before I/O.

#### Scenario: Valid ISO year boundary
- **WHEN** 2020-W53 is requested
- **THEN** dates are 2020-12-28 through 2021-01-03 and the week label remains 2020-W53.

### Requirement: Fetch only the instructor profile
The service SHALL obtain max_hours_per_day and timezone via get_instructor,
without requesting availability or classroom data.

#### Scenario: Path resource absent
- **WHEN** people-stub returns 404 for the instructor profile
- **THEN** 404 NOT_FOUND is returned, not 422 INSTRUCTOR_NOT_FOUND.

#### Scenario: Upstream failure
- **WHEN** the profile lookup fails or times out
- **THEN** 502 UPSTREAM_UNAVAILABLE or 504 UPSTREAM_TIMEOUT is preserved without SQL.

#### Scenario: I6 profile available
- **WHEN** I6 has an available profile and failing availability
- **THEN** the workload response succeeds with exactly one profile request.

### Requirement: Aggregate seven local days using BR-06 semantics
The service SHALL select active lessons overlapping the local ISO week UTC bounds
and aggregate them through the same daily_hours function used by BR-06.

#### Scenario: Empty days and chronological order
- **WHEN** the instructor has no lessons on some days
- **THEN** 200 Workload still contains exactly seven Monday..Sunday entries with zero hours.

#### Scenario: Local midnight and week edges
- **WHEN** a lesson crosses local midnight or a week boundary
- **THEN** only elapsed hours within each of the seven requested local dates are included.

#### Scenario: Active statuses and series
- **WHEN** lessons include PLANNED, CONFIRMED, IN_PROGRESS, COMPLETED, CANCELLED,
  RESCHEDULED and series members
- **THEN** all first four statuses and active series members contribute, while the
  last two statuses do not.

#### Scenario: Strict workload limit
- **WHEN** a day equals max_hours_per_day or exceeds it
- **THEN** exceeded is false at equality and true strictly above the limit.

#### Scenario: DST transition
- **WHEN** a Europe/Berlin week contains a DST transition
- **THEN** local-midnight week bounds may span 167/169 hours and daily totals use
  actual UTC elapsed hours rather than naive wall-clock subtraction.

### Requirement: Keep reporting read-only and compatible
The service SHALL return the original Workload schema without commits, mutations
or broker events, preserving existing FR-01..FR-14 behavior and contracts.

#### Scenario: Single database snapshot
- **WHEN** a report is read
- **THEN** one SELECT supplies the week's active intervals and no data write or event occurs.
