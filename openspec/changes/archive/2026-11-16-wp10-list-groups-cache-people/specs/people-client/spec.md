## MODIFIED Requirements

### Requirement: Parallel lesson lookups

The client SHALL call three independent resource methods for IN_PERSON and two
for ONLINE using asyncio.gather (FR-12, NFR-07). On cold cache or with caching
disabled the corresponding HTTP requests SHALL run concurrently (FR-17).

#### Scenario: In-person async case
- **WHEN** each cold-cache fake request takes 300 milliseconds
- **THEN** IN_PERSON reaches max_in_flight equal to 3 and takes approximately 300 ms
- **AND** a sequential baseline takes approximately 900 ms

#### Scenario: Online async case
- **WHEN** an ONLINE lookup uses a cold cache
- **THEN** only profile and availability are fetched with max_in_flight equal to 2
- **AND** no classroom request is sent

#### Scenario: Real stub counters without network
- **WHEN** the unmodified stub is exercised through ASGITransport on a cold cache
- **THEN** /__stub/stats confirms total and max_in_flight equal to 3 or 2
- **AND** in_flight returns to zero and admin calls do not increase total

## ADDED Requirements

### Requirement: Bounded successful response TTL cache

PeopleClient SHALL cache validated immutable profile, availability and classroom
values by type/UUID for PEOPLE_CACHE_TTL_SECONDS monotonic seconds from successful
load completion (FR-17). The default SHALL be 30 seconds, 0 SHALL disable caching,
and each type SHALL store at most 256 LRU entries. Errors SHALL NOT be cached.

#### Scenario: Hit and expiry
- **WHEN** the same key is requested before its deadline and again at its deadline
- **THEN** the first request uses the cached value and the second reloads upstream

#### Scenario: Disabled cache
- **WHEN** PEOPLE_CACHE_TTL_SECONDS is 0
- **THEN** every lookup performs its normal upstream request

#### Scenario: Failed lookup recovery
- **WHEN** a lookup fails with 404, 5xx, timeout, network error or invalid payload
- **THEN** another lookup retries upstream and preserves the original error mapping

#### Scenario: Concurrent successful misses
- **WHEN** twenty callers request identical cold IN_PERSON or ONLINE data
- **THEN** each distinct key is fetched once with network max_in_flight 3 or 2
- **AND** different keys remain independent

#### Scenario: Bounded LRU
- **WHEN** storing another successful value would exceed the configured capacity
- **THEN** the least recently used value is evicted

### Requirement: Cache invalidation and cancellation

PeopleClient SHALL support clear_cache, successful test reset and shutdown
invalidation (FR-17). Metadata SHALL be protected from concurrent access;
async callers SHALL use the lifespan owner's event loop. Invalidation SHALL
prevent older in-flight responses from repopulating the cleared cache.

#### Scenario: Reset
- **WHEN** POST /__test/reset succeeds after profile/availability/classroom are cached
- **THEN** all three values are fetched again on their next lookup
- **AND** the current Clock value remains unchanged

#### Scenario: Clear while loading
- **WHEN** clear_cache runs while an old request is in flight
- **THEN** that result may reach its caller but cannot populate the cleared cache

#### Scenario: Cancel owner or waiter
- **WHEN** either the owner of a fetch or a waiting caller is cancelled
- **THEN** other callers are not cancelled and the key remains usable

#### Scenario: Cross-thread clear and shutdown
- **WHEN** clear_cache is called from another thread and later aclose is called
- **THEN** clear is safe, shutdown closes HTTPX and later cache hits are rejected

#### Scenario: Cached create race
- **WHEN** two overlapping creates share successful cached people data
- **THEN** existing database resource locks still allow exactly one 201 and one 409
