## ADDED Requirements

### Requirement: Pure people data structures

The domain SHALL expose immutable people data without FastAPI, HTTPX, Pydantic
or SQLAlchemy dependencies (FR-12).

#### Scenario: Convert valid stub data
- **WHEN** valid profile, availability and classroom responses are received
- **THEN** the client returns pure InstructorProfile, InstructorAvailability and Classroom
- **AND** max_hours_per_day is Decimal and availability windows contain local time values

### Requirement: Parallel lesson lookups

The client SHALL fetch three independent resources for IN_PERSON and two for
ONLINE using asyncio.gather (FR-12, NFR-07).

#### Scenario: In-person async case
- **WHEN** each fake request takes 300 milliseconds
- **THEN** IN_PERSON reaches max_in_flight equal to 3 and takes approximately 300 ms
- **AND** a sequential baseline takes approximately 900 ms

#### Scenario: Online async case
- **WHEN** an ONLINE lookup is performed
- **THEN** only profile and availability are fetched with max_in_flight equal to 2
- **AND** no classroom request is sent

#### Scenario: Real stub counters without network
- **WHEN** the unmodified stub is exercised through ASGITransport
- **THEN** /__stub/stats confirms total and max_in_flight equal to 3 or 2
- **AND** in_flight returns to zero and admin calls do not increase total

### Requirement: Deterministic upstream errors

The client SHALL convert absent entities and integration failures into platform
ApiError values and choose concurrent errors in profile, availability, classroom
order, independent of completion speed (FR-12, BR-03).

#### Scenario: Entity absent
- **WHEN** profile or availability returns 404
- **THEN** the client raises 422 INSTRUCTOR_NOT_FOUND

#### Scenario: Classroom absent
- **WHEN** the classroom request returns 404
- **THEN** the client raises 422 CLASSROOM_NOT_FOUND

#### Scenario: Upstream failure
- **WHEN** an upstream endpoint returns 5xx or raises HTTPX RequestError
- **THEN** the client raises 502 UPSTREAM_UNAVAILABLE

#### Scenario: I6 partially available
- **WHEN** I6 profile returns 200 and availability returns 500
- **THEN** the client raises 502 UPSTREAM_UNAVAILABLE

#### Scenario: Timeout
- **WHEN** HTTPX raises TimeoutException for I5 or C5
- **THEN** the client raises 504 UPSTREAM_TIMEOUT

#### Scenario: Response order changes
- **WHEN** profile 404 and other failures complete in different orders
- **THEN** the selected error remains INSTRUCTOR_NOT_FOUND
- **AND** no request remains in flight after the error is returned

#### Scenario: Invalid upstream data
- **WHEN** a 200 response has invalid fields or a mismatched entity identifier
- **THEN** the client raises 502 UPSTREAM_UNAVAILABLE

#### Scenario: Caller cancelled
- **WHEN** the operation is cancelled with all requests in flight
- **THEN** child requests are cancelled and in_flight becomes zero

### Requirement: Configured lifecycle-owned client

The client SHALL use the configured base URL and timeout and SHALL close during
lifespan shutdown without joining readiness dependencies (FR-12).

#### Scenario: Configured transport
- **WHEN** the client is configured with STUB_TIMEOUT_MS=1750
- **THEN** HTTPX connect/read/write/pool timeouts are 1.75 seconds
- **AND** requests use STUB_BASE_URL

#### Scenario: Resource cleanup
- **WHEN** lifespan ends normally or exits through a runtime or startup exception
- **THEN** the HTTP client is closed and the database engine is disposed
- **AND** WP-01 seed/reset and GET routes remain connected

#### Scenario: Readiness
- **WHEN** the readiness endpoint is called while people-stub is unavailable
- **THEN** only database and broker checks determine the response
- **AND** no people-stub request is sent
