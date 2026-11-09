"""Часы каркаса: фиксация, сброс, формат вывода."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.clock import AdjustableClock, Clock, to_iso_z


class _FixedSystemClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


def test_adjustable_clock_follows_system_until_frozen() -> None:
    system: Clock = _FixedSystemClock(datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
    clock = AdjustableClock(system)

    assert clock.now() == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert not clock.is_frozen


def test_frozen_clock_does_not_tick_and_normalizes_to_utc() -> None:
    clock = AdjustableClock(_FixedSystemClock(datetime(2026, 11, 9, 12, 0, tzinfo=UTC)))
    local = datetime(2026, 11, 30, 8, 0, tzinfo=timezone(timedelta(hours=5)))

    clock.freeze(local)

    assert clock.is_frozen
    first, second = clock.now(), clock.now()
    assert first == datetime(2026, 11, 30, 3, 0, tzinfo=UTC)
    assert first == second


def test_reset_returns_to_system_clock() -> None:
    system = _FixedSystemClock(datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
    clock = AdjustableClock(system)
    clock.freeze(datetime(2030, 1, 1, tzinfo=UTC))

    clock.reset()

    assert clock.now() == system.moment


def test_freeze_rejects_naive_datetime() -> None:
    clock = AdjustableClock()
    with pytest.raises(ValueError, match="смещение"):
        clock.freeze(datetime(2026, 11, 30, 3, 0))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 30, 3, 0, tzinfo=UTC), "2026-11-30T03:00:00Z"),
        (
            datetime(2026, 11, 30, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=5))),
            "2026-11-30T03:00:00Z",
        ),
    ],
)
def test_to_iso_z(moment: datetime, expected: str) -> None:
    assert to_iso_z(moment) == expected


def test_to_iso_z_rejects_naive() -> None:
    with pytest.raises(ValueError, match="aware"):
        to_iso_z(datetime(2026, 1, 1))
