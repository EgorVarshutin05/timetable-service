## ADDED Requirements

### Requirement: Paginated ordered groups

GET /groups SHALL return GroupPage with items and total using limit=50 (1..200),
offset=0 (>=0) and PostgreSQL ordering name ASC, id ASC (FR-16).
The service SHALL read total and items in one consistent database snapshot.

#### Scenario: Seed listing
- **WHEN** the three seed groups are requested without query parameters
- **THEN** total is 3 and items are ordered ВЕБ-24-2, ДАН-24-3, ПРГ-24-1
- **AND** each item matches the original Group schema

#### Scenario: Equal names
- **WHEN** multiple groups have the same name
- **THEN** their UUIDs sort ascending before pagination is applied

#### Scenario: Page beyond end
- **WHEN** offset is at or beyond total
- **THEN** items is empty and total still counts every group

#### Scenario: Invalid pagination
- **WHEN** limit is outside 1..200 or offset is negative or a value is not an integer
- **THEN** the API returns 422 VALIDATION_ERROR with details.fields before SQL

#### Scenario: Concurrent insert
- **WHEN** another transaction commits a group between COUNT and page SELECT
- **THEN** the current response sees one consistent snapshot
- **AND** the following request sees the newly committed group
