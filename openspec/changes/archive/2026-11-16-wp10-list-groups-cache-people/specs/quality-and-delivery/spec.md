## ADDED Requirements

### Requirement: Strict test clock input
PUT /__test/time SHALL accept only ISO 8601 datetime strings with an offset.
Invalid input SHALL preserve Clock and use the platform error envelope (NFR-03/04).

#### Scenario: Wrong JSON type
- **WHEN** now is a number, boolean, null, object or array
- **THEN** 400 BAD_REQUEST is returned and the frozen instant is unchanged.

#### Scenario: Invalid string value
- **WHEN** now is a Unix timestamp string, date-only value or malformed datetime
- **THEN** 422 VALIDATION_ERROR contains details.fields.now and Clock is unchanged.

#### Scenario: Explicit offset
- **WHEN** now is 2026-11-30T08:00:00+05:00
- **THEN** Clock freezes at 2026-11-30T03:00:00Z.

### Requirement: Reproducible integration and isolated unit checks
Both CI check jobs SHALL provide PostgreSQL 16 and WP01_TEST_DATABASE_URL (NFR-05).
Readiness unit checks SHALL replace database and broker probes with fakes.

#### Scenario: CI database configuration
- **WHEN** GitHub Actions or GitLab runs pytest
- **THEN** database tests execute in temporary schemas instead of skipping for missing DSN.

#### Scenario: Real local broker
- **WHEN** a real RabbitMQ is running while the readiness unit check executes
- **THEN** only fake probes run and the result does not depend on that broker.
