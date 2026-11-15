## ADDED Requirements

### Requirement: Current Must documentation
The repository SHALL document implemented FR-01 through FR-13, BR-01 through BR-08
and NFR-01 through NFR-07 in canonical capability specs and the six README sections (NFR-06).

#### Scenario: README structure and scope
- **WHEN** README is inspected after WP-07
- **THEN** its six required second-level headings are present, Must is complete,
  and WP-08..10 are clearly identified as future Should/Could packages.

#### Scenario: Original readiness retained
- **WHEN** runtime behavior and documentation are compared to the source contract
- **THEN** readiness has top-level status/db/broker and no API contract is changed.

### Requirement: Honest verification evidence
The repository SHALL distinguish local static/PG verification from the author's
reported Docker/public E2E run and preserve reproducible commands (NFR-05, NFR-06).

#### Scenario: Completed verification
- **WHEN** the documented evidence is reviewed
- **THEN** 438 unit/PG tests and 3 Docker public E2E in 9.14s are recorded
  with their verification context, without claiming an unperformed assistant Docker run.

### Requirement: Canonical specs and completed archives
The repository SHALL keep current Requirements and WHEN/THEN scenarios in
openspec/specs and completed WP-01..06 changes in the archive (NFR-06).

#### Scenario: Archived change lookup
- **WHEN** lint_commits.py resolves an already merged Change identifier
- **THEN** its archived directory is found and its change branch is retained.

### Requirement: Accurate time and documentation history
The repository SHALL preserve supplied facts, leave the unknown WP-07 fact empty
and use valid docs/spec/chore history without artificial feature/test commits (NFR-06).

#### Scenario: Total calculation
- **WHEN** the author later records WP-07=0,30
- **THEN** total Must including WP-00 is 4,76; before that both actual cells remain empty.

#### Scenario: Merge and post-merge archive
- **WHEN** the documentation package is merged and archived with the prescribed messages
- **THEN** headers use allowed scopes and <=72 characters, Change/Refs resolve,
  and lint_commits.py reports zero FAIL and zero WARN.
