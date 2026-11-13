"""Порядок advisory locks одинаков для всех процессов и наборов ресурсов."""

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.lesson import LessonFormat
from app.repositories.scheduling import lock_resources, resource_lock_keys
from tests.unit.scheduling_data import DRAFT, G2


async def test_locks_are_stable_sorted_signed_bigints() -> None:
    keys = resource_lock_keys(DRAFT)
    assert keys == tuple(sorted(set(keys)))
    assert len(keys) == 3
    assert all(-(2**63) <= key < 2**63 for key in keys)
    assert keys == resource_lock_keys(replace(DRAFT))
    session = AsyncMock(spec=AsyncSession)
    await lock_resources(cast(AsyncSession, session), DRAFT)
    assert [call.args[1]["key"] for call in session.execute.await_args_list] == list(keys)
    assert all(
        "pg_advisory_xact_lock" in str(call.args[0]) for call in session.execute.await_args_list
    )


def test_online_uses_two_keys_and_preserves_shared_resource_keys() -> None:
    online = replace(DRAFT, classroom_id=None, format=LessonFormat.ONLINE)
    assert len(resource_lock_keys(online)) == 2
    other_group = replace(online, group_id=G2)
    assert len(set(resource_lock_keys(online)) & set(resource_lock_keys(other_group))) == 1
