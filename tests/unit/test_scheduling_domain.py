"""BR-01/02/04..07 отдельно от HTTP, ORM, PostgreSQL и people-stub."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.lesson import LessonFormat, LessonStatus
from app.domain.people import AvailabilityWindow
from app.domain.scheduling import (
    InvalidLesson,
    availability_warning,
    capacity_violation,
    conflict_violation,
    daily_hours,
    validate_early,
    workload_warnings,
)
from tests.unit.scheduling_data import C2, DRAFT, G2, I2, L1, PEOPLE, existing


@pytest.mark.parametrize("minutes", [-1, 0, 29, 481])
def test_br01_rejects_invalid_duration_before_br02(minutes: int) -> None:
    draft = replace(DRAFT, ends_at=DRAFT.starts_at + timedelta(minutes=minutes), classroom_id=None)
    with pytest.raises(InvalidLesson) as error:
        validate_early(draft)
    assert error.value.issue.code == "VALIDATION_ERROR"
    assert "ends_at" in error.value.issue.details["fields"]


@pytest.mark.parametrize("minutes", [30, 480])
def test_duration_boundaries(minutes: int) -> None:
    result = validate_early(replace(DRAFT, ends_at=DRAFT.starts_at + timedelta(minutes=minutes)))
    assert result.ends_at - result.starts_at == timedelta(minutes=minutes)


def test_aware_input_normalized_and_naive_rejected() -> None:
    offset = timezone(timedelta(hours=5))
    draft = replace(DRAFT, starts_at=DRAFT.starts_at.astimezone(offset))
    assert validate_early(draft).starts_at == DRAFT.starts_at
    with pytest.raises(InvalidLesson):
        validate_early(replace(DRAFT, starts_at=DRAFT.starts_at.replace(tzinfo=None)))


@pytest.mark.parametrize(
    ("format_", "classroom", "code"),
    [
        (LessonFormat.IN_PERSON, None, "CLASSROOM_REQUIRED"),
        (LessonFormat.ONLINE, C2, "VALIDATION_ERROR"),
    ],
)
def test_br02(format_: LessonFormat, classroom: UUID | None, code: str) -> None:
    with pytest.raises(InvalidLesson) as error:
        validate_early(replace(DRAFT, format=format_, classroom_id=classroom))
    assert error.value.issue.code == code


def test_capacity_details_and_online() -> None:
    issue = capacity_violation(DRAFT, 31, PEOPLE)
    assert issue is not None
    assert issue.code == "CLASSROOM_TOO_SMALL"
    assert issue.details == {"capacity": 30, "group_size": 31}
    assert capacity_violation(DRAFT, 30, PEOPLE) is None
    assert (
        capacity_violation(
            replace(DRAFT, format=LessonFormat.ONLINE, classroom_id=None),
            99,
            replace(PEOPLE, classroom=None),
        )
        is None
    )


@pytest.mark.parametrize("status", list(LessonStatus))
def test_active_statuses_include_completed(status: LessonStatus) -> None:
    issue = conflict_violation(DRAFT, [replace(existing(), status=status)])
    assert (issue is None) == (status in {LessonStatus.CANCELLED, LessonStatus.RESCHEDULED})


def test_conflicts_all_resources_sorted_and_excluded() -> None:
    a = existing()
    b = replace(a, id=UUID("3d000000-0000-4000-8000-000000000002"))
    issue = conflict_violation(DRAFT, [b, a])
    assert issue is not None
    assert [(row["resource"], row["lesson_id"]) for row in issue.details["conflicts"]] == [
        (resource, str(row.id))
        for resource in ("instructor", "classroom", "group")
        for row in (a, b)
    ]
    assert issue == conflict_violation(DRAFT, [a, b])
    assert conflict_violation(DRAFT, [a], exclude_lesson_id=L1) is None


def test_touching_intervals_and_unrelated_resources_do_not_conflict() -> None:
    row = existing()
    assert (
        conflict_violation(
            DRAFT,
            [replace(row, starts_at=DRAFT.ends_at, ends_at=DRAFT.ends_at + timedelta(hours=1))],
        )
        is None
    )
    assert (
        conflict_violation(
            DRAFT,
            [replace(row, ends_at=DRAFT.starts_at, starts_at=DRAFT.starts_at - timedelta(hours=1))],
        )
        is None
    )
    assert (
        conflict_violation(DRAFT, [replace(row, instructor_id=I2, group_id=G2, classroom_id=C2)])
        is None
    )


def test_overlap_is_intersection_not_whole_interval() -> None:
    row = replace(
        existing(),
        starts_at=DRAFT.starts_at - timedelta(minutes=30),
        ends_at=DRAFT.starts_at + timedelta(minutes=30),
    )
    issue = conflict_violation(DRAFT, [row])
    assert issue is not None
    assert issue.details["conflicts"][0]["overlap"] == {
        "starts_at": "2026-12-02T07:00:00Z",
        "ends_at": "2026-12-02T07:30:00Z",
    }


def test_daily_hours_midnight_split_and_exact_midnight() -> None:
    start = datetime(2026, 12, 1, 18, 30, tzinfo=UTC)  # 23:30 в Екатеринбурге
    end = start + timedelta(hours=2)
    assert daily_hours(start, end, PEOPLE.instructor.timezone) == {
        date(2026, 12, 1): Decimal("0.5"),
        date(2026, 12, 2): Decimal("1.5"),
    }
    assert daily_hours(start, start + timedelta(minutes=30), PEOPLE.instructor.timezone) == {
        date(2026, 12, 1): Decimal("0.5")
    }


@pytest.mark.parametrize(
    ("start", "end", "hours"),
    [
        ("2026-03-08T06:30:00+00:00", "2026-03-08T07:30:00+00:00", "1"),
        ("2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00", "1"),
    ],
)
def test_dst_uses_elapsed_utc_time(start: str, end: str, hours: str) -> None:
    result = daily_hours(
        datetime.fromisoformat(start), datetime.fromisoformat(end), "America/New_York"
    )
    assert sum(result.values(), Decimal(0)) == Decimal(hours)


def test_workload_two_days_and_exact_limit() -> None:
    draft = replace(
        DRAFT,
        starts_at=datetime(2026, 12, 1, 18, 30, tzinfo=UTC),
        ends_at=datetime(2026, 12, 1, 20, 30, tzinfo=UTC),
    )
    people = replace(PEOPLE, instructor=replace(PEOPLE.instructor, max_hours_per_day=Decimal(2)))
    previous = replace(
        existing(),
        starts_at=draft.starts_at - timedelta(hours=2, minutes=30),
        ends_at=draft.starts_at - timedelta(minutes=30),
    )
    following = replace(
        existing(),
        starts_at=draft.ends_at,
        ends_at=draft.ends_at + timedelta(hours=1, minutes=30),
        id=UUID("3d000000-0000-4000-8000-000000000002"),
    )
    warnings = workload_warnings(draft, [following, previous], people)
    assert [warning.details for warning in warnings] == [
        {"date": "2026-12-01", "max_hours": 2.0, "planned_hours": 2.5},
        {"date": "2026-12-02", "max_hours": 2.0, "planned_hours": 3.0},
    ]
    assert (
        workload_warnings(
            DRAFT,
            [],
            replace(
                PEOPLE, instructor=replace(PEOPLE.instructor, max_hours_per_day=Decimal("1.5"))
            ),
        )
        == ()
    )


def test_workload_filters_status_instructor_and_excluded_id() -> None:
    people = replace(PEOPLE, instructor=replace(PEOPLE.instructor, max_hours_per_day=Decimal(2)))
    row = existing()
    assert workload_warnings(DRAFT, [row], people)[0].details["planned_hours"] == 3.0
    assert workload_warnings(DRAFT, [row], people, exclude_lesson_id=L1) == ()
    assert (
        workload_warnings(
            DRAFT,
            [
                replace(row, instructor_id=I2),
                replace(row, status=LessonStatus.CANCELLED),
                replace(row, status=LessonStatus.RESCHEDULED),
            ],
            people,
        )
        == ()
    )


def test_availability_whole_window_end_boundary_and_midnight() -> None:
    assert availability_warning(DRAFT, PEOPLE) is None
    assert (
        availability_warning(
            replace(
                DRAFT,
                starts_at=datetime(2026, 12, 2, 12, tzinfo=UTC),
                ends_at=datetime(2026, 12, 2, 13, tzinfo=UTC),
            ),
            PEOPLE,
        )
        is None
    )
    midnight = replace(
        DRAFT,
        starts_at=datetime(2026, 12, 2, 18, tzinfo=UTC),
        ends_at=datetime(2026, 12, 2, 20, tzinfo=UTC),
    )
    warning = availability_warning(midnight, PEOPLE)
    assert warning is not None
    assert warning.details == {"instructor_id": str(DRAFT.instructor_id)}


def test_adjacent_windows_not_combined_and_wrong_weekday_warns() -> None:
    availability = replace(
        PEOPLE.availability,
        windows=(
            AvailabilityWindow(3, time(9), time(12)),
            AvailabilityWindow(3, time(12), time(18)),
        ),
    )
    draft = replace(DRAFT, starts_at=datetime(2026, 12, 2, 6, tzinfo=UTC))  # 11:00–13:30
    assert availability_warning(draft, replace(PEOPLE, availability=availability)) is not None
    assert (
        availability_warning(
            DRAFT,
            replace(
                PEOPLE,
                availability=replace(
                    availability, windows=(AvailabilityWindow(2, time(9), time(18)),)
                ),
            ),
        )
        is not None
    )


def test_dst_availability_missing_boundary_and_valid_window() -> None:
    profile = replace(PEOPLE.instructor, timezone="America/New_York")
    draft = replace(
        DRAFT,
        starts_at=datetime(2026, 3, 8, 7, tzinfo=UTC),
        ends_at=datetime(2026, 3, 8, 8, tzinfo=UTC),
    )
    unavailable = replace(PEOPLE.availability, windows=(AvailabilityWindow(7, time(2), time(4)),))
    people = replace(PEOPLE, instructor=profile, availability=unavailable)
    assert availability_warning(draft, people) is not None
    available = replace(unavailable, windows=(AvailabilityWindow(7, time(3), time(4)),))
    assert availability_warning(draft, replace(people, availability=available)) is None
