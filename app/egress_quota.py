"""Conservative persistent listening quota with pre-reserved B2 credit.

Small blocks of outgoing bandwidth are reserved in B2, then consumed locally.
A crash discards unused credit, but can never make the persistent quota exceed
its cap. Single-Uvicorn-worker operation is required, as with the old counter.
"""
import asyncio
from datetime import datetime, timezone
from fastapi import HTTPException
from .storage import MissingObject


class MonthlyEgressQuota:
    def __init__(self, max_bytes: int, block_bytes: int = 256 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.block_bytes = max(1, block_bytes)
        self.lock = asyncio.Lock()
        self.month: str | None = None
        self.credit = 0

    async def reserve(self, amount: int, get_storage, s3_call):
        if amount <= 0:
            return
        async with self.lock:
            month = datetime.now(timezone.utc).strftime('%Y-%m')
            if month != self.month:
                self.month = month
                self.credit = 0
            if amount > self.max_bytes:
                raise HTTPException(429, 'The monthly listening bandwidth limit has been reached')
            if self.credit >= amount:
                self.credit -= amount
                return
            key = 'system/egress-' + month + '.json'
            b2 = get_storage()
            try:
                record = await s3_call(b2.get_json, key)
            except MissingObject:
                record = {'bytes': 0}
            used = int(record.get('bytes', 0))
            required = amount - self.credit
            available = self.max_bytes - used
            if required > available:
                raise HTTPException(429, 'The monthly listening bandwidth limit has been reached')
            # This is an advance reservation, not a record of delivered bytes.
            # Never reserve more than the configured global cap.
            grant = min(available, max(required, min(self.block_bytes, max(1, self.max_bytes // 4))))
            await s3_call(b2.put_json, key, {'bytes': used + grant})
            self.credit += grant - amount
