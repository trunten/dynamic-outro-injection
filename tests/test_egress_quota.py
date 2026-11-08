"""Ensure durable monthly quota never overcommits after a restart."""
import asyncio
import pytest
from fastapi import HTTPException
from app.egress_quota import MonthlyEgressQuota
from app.storage import MissingObject


class FakeB2:
    def __init__(self):
        self.values = {}
        self.calls = 0

    def get_json(self, key):
        self.calls += 1
        if key not in self.values:
            raise MissingObject(key)
        return self.values[key]

    def put_json(self, key, value):
        self.calls += 1
        self.values[key] = value


async def async_s3(func, *args):
    return func(*args)


def test_credit_reused_locally_but_cap_persists():
    async def check():
        b2 = FakeB2()
        quota = MonthlyEgressQuota(1000, 256)
        await quota.reserve(100, lambda: b2, async_s3)
        assert b2.calls == 2
        await quota.reserve(100, lambda: b2, async_s3)
        assert b2.calls == 2
        for _ in range(6):
            await quota.reserve(100, lambda: b2, async_s3)
        used = next(iter(b2.values.values()))["bytes"]
        assert 800 <= used <= 1000
        with pytest.raises(HTTPException) as err:
            await quota.reserve(300, lambda: b2, async_s3)
        assert err.value.status_code == 429
    asyncio.run(check())


def test_crash_cannot_recover_unused_reserved_credit():
    async def check():
        b2 = FakeB2()
        await MonthlyEgressQuota(1000, 250).reserve(100, lambda: b2, async_s3)
        fresh = MonthlyEgressQuota(1000, 250)
        await fresh.reserve(700, lambda: b2, async_s3)
        with pytest.raises(HTTPException):
            await fresh.reserve(100, lambda: b2, async_s3)
    asyncio.run(check())
