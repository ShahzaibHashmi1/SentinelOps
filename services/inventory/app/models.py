from pydantic import BaseModel, Field


class Item(BaseModel):
    sku: str
    name: str
    price: float
    stock: int


class QuantityRequest(BaseModel):
    quantity: int = Field(gt=0)


class StockChange(BaseModel):
    sku: str
    quantity: int  # how many units were reserved / released
    stock: int  # stock level after the change
