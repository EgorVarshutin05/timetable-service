## MODIFIED Requirements

### Requirement: Atomic bounded concurrent series assessment
The service SHALL read one snapshot under sorted resource locks and validate occurrences
concurrently with at most four workers (FR-14). BR-04..07 SHALL consider existing active
lessons and earlier generated occurrences. SQL/ORM SHALL stay outside workers; the lowest
failing index SHALL determine the response regardless of completion order. Inserts SHALL
begin only after every check succeeds, with locks held through the single commit.

#### Scenario: Bounded occurrence validation
- **WHEN** twelve occurrences are checked with deliberately delayed checks
- **THEN** checks overlap with at most four workers and finish before persistence.

#### Scenario: Out-of-order failures
- **WHEN** occurrence 2 fails before occurrence 1 finishes with a hard violation
- **THEN** the error for occurrence_index=1 is returned without partial inserts.

#### Scenario: Prefix workload
- **WHEN** two occurrences contribute hours to the same instructor-local day
- **THEN** the later occurrence includes earlier generated hours in BR-06.

#### Scenario: Concurrent overlapping writers
- **WHEN** a series and another create operation target overlapping resources
- **THEN** one commits and the other receives 409 without a partial series.
