from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ChargeRequest(BaseModel):
    order_id: UUID
    amount: float = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


class Payment(BaseModel):
    id: UUID
    order_id: UUID
    amount: float
    idempotency_key: str
    status: str
    created_at: datetime
