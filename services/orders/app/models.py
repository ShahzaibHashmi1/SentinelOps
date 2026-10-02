from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateOrder(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    customer_id: str = Field(min_length=1)


class Order(BaseModel):
    id: UUID
    sku: str
    quantity: int
    customer_id: str
    amount: float
    status: str  # PENDING | PAID | FAILED
    payment_id: UUID | None = None
    created_at: datetime


class Product(BaseModel):
    sku: str
    name: str
    price: float
    stock: int
