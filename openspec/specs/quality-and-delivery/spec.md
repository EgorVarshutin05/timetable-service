# quality-and-delivery

## Purpose

Current Must quality and delivery requirements: NFR-01, NFR-02, NFR-03, NFR-04,
NFR-05, NFR-06, NFR-07. Functional specifications remain in the other capabilities.
Normative contracts are the original openapi.yaml and asyncapi.yaml.

## Requirements

### Requirement: One-command runtime and original readiness
The service SHALL implement NFR-01 with docker compose up --build and configured
database, selected broker and people-stub services. Readiness SHALL depend only
on database and broker, with top-level status, db and broker fields.

#### Scenario: Ready Docker stack
- **WHEN** the configured database and RabbitMQ RPC respond successfully
- **THEN** GET /health/ready returns 200 {status:ok, db:ok, broker:ok}.

#### Scenario: Failed readiness dependency
- **WHEN** the database or broker fails or times out
- **THEN** readiness returns 503 with status=degraded and individual top-level statuses;
  liveness remains 200 and people-stub is not probed.

### Requirement: Reproducible migrations
The service SHALL implement NFR-02 with Alembic migrations rather than schema
creation during HTTP startup. Constraints and indexes SHALL match ORM metadata.

#### Scenario: Empty database
- **WHEN** alembic upgrade head runs on an empty PostgreSQL 16 database
- **THEN** the schema is created and alembic check reports no pending upgrade.

### Requirement: Deterministic seed and controlled time
The service SHALL implement NFR-03 with fixed seed UUIDs/timestamps and Clock-only
current time. TEST_MODE SHALL expose time, reset and registered job endpoints.

#### Scenario: Reset under frozen Clock
- **WHEN** time is frozen and POST /__test/reset restores seed
- **THEN** data is restored atomically while Clock keeps the same frozen instant.

#### Scenario: Production mode
- **WHEN** TEST_MODE=0
- **THEN** test endpoints return 404 and registered jobs run periodically.

#### Scenario: Time input follows the HTTP contract
- **WHEN** PUT /__test/time receives a number, boolean, null, object or array as now
- **THEN** it returns 400 BAD_REQUEST without changing Clock.
- **WHEN** now is a Unix timestamp string or another invalid datetime string
- **THEN** it returns 422 VALIDATION_ERROR with details.fields.now, preserving Clock.
- **WHEN** now is an ISO 8601 datetime string with an offset
- **THEN** it freezes Clock at the corresponding UTC instant.

### Requirement: Uniform platform errors
The service SHALL implement NFR-04 through errors.py with error.code, error.message
and error.details. Syntactic JSON/type errors SHALL be 400; semantic rule errors,
path absence, conflicts and upstream errors SHALL follow openapi.yaml.

#### Scenario: Wrong JSON type
- **WHEN** a request uses an object where a datetime string is required
- **THEN** 400 BAD_REQUEST follows the Error schema, without a FastAPI detail envelope.

#### Scenario: Missing path resource
- **WHEN** a valid path UUID identifies no group or lesson
- **THEN** 404 NOT_FOUND follows the same Error schema.

### Requirement: Strict static checks and meaningful tests
The repository SHALL implement NFR-05 using the unchanged scaffold ruff and strict
mypy settings, isolated domain tests, API contract tests and PostgreSQL race tests.

#### Scenario: Quality gate
- **WHEN** ruff check, ruff format --check, mypy --strict and pytest run
- **THEN** static checks pass and the documented unit/PostgreSQL suite passes.

#### Scenario: CI executes database tests
- **WHEN** the GitHub Actions or GitLab check job runs
- **THEN** PostgreSQL 16 is provided with WP01_TEST_DATABASE_URL set, so database
  tests run in disposable schemas rather than being skipped for missing configuration.

#### Scenario: Unit readiness does not depend on local services
- **WHEN** the people-client readiness unit test runs with or without a local broker
- **THEN** database and broker probes are fakes and no real integration is contacted.

### Requirement: Reviewable documentation and package history
The repository SHALL implement NFR-06 with the six required README sections,
honest evidence and limitations, canonical Must specs and archived completed changes.
Every package SHALL retain its change branch and merge message with Change/Refs.

#### Scenario: Required README structure
- **WHEN** README is inspected
- **THEN** its second-level headings are exactly Запуск, Решения, Не сделано,
  Async-кейс, AI notes and Время, and Should/Could are identified separately.

#### Scenario: Documentation package merge
- **WHEN** wp07-finalize-docs is merged as a documentation package
- **THEN** spec/openspec, docs/readme and chore/openspec commits require no artificial
  feat or test commit, and lint_commits.py reports no FAIL or WARN.

### Requirement: Reproducible async evidence
The service SHALL implement NFR-07 with asyncio.gather for the independent people
requests and a fake-delay test requiring max_in_flight 3 for IN_PERSON and 2 for ONLINE.

#### Scenario: Async benchmark
- **WHEN** each fake transport request delays 300 ms
- **THEN** the documented test compares sequential and parallel elapsed time
  without network and verifies the exact peak concurrency.
