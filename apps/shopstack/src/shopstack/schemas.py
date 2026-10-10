"""ShopStack API request and response models."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str


class OrderRequest(BaseModel):
    user_id: UUID
    sku: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=20)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    sku: str
    quantity: int
    amount: Decimal
    status: str
    payment_id: UUID | None


class ReservationRequest(BaseModel):
    order_id: UUID
    sku: str
    quantity: int = Field(ge=1, le=20)


class ReservationResponse(BaseModel):
    order_id: UUID
    sku: str
    quantity: int
    status: str
    available: int


class PaymentRequest(BaseModel):
    order_id: UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    amount: Decimal
    status: str
