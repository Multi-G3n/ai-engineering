from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ProductInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(default="Uncategorized", min_length=1, max_length=100)
    unit_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    stock_quantity: int = Field(ge=0)
    minimum_stock: int = Field(ge=0)

    @field_validator("sku", "name", "category", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("unit_price", mode="before")
    @classmethod
    def parse_price(cls, value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a valid unit price.")
        if not result.is_finite():
            raise ValueError("Unit price must be a finite amount.")
        return result


class AdjustmentInput(BaseModel):
    quantity: int = Field(gt=0)
    direction: str
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("direction")
    @classmethod
    def valid_direction(cls, value):
        if value not in {"increase", "decrease"}:
            raise ValueError("Choose increase or decrease.")
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class SaleInput(BaseModel):
    idempotency_key: UUID
    product_id: int = Field(gt=0)
    quantity_sold: int = Field(gt=0)
    sale_date: date
    unit_selling_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    mark_day_complete: bool = False

    @field_validator("unit_selling_price", mode="before")
    @classmethod
    def parse_optional_price(cls, value):
        if value in (None, ""):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a valid unit selling price.")
        if not result.is_finite():
            raise ValueError("Unit selling price must be a finite amount.")
        return result


class SalesTrackingInput(BaseModel):
    product_id: int = Field(gt=0)
    sales_date: date
