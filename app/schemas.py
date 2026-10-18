from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class TenantIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    others: list[str] = []


class FlatIn(BaseModel):
    addr: str = Field(min_length=1, max_length=200)
    dispo: str = ""
    floor: str = ""
    zip: str = ""
    owner: str = ""
    bank_acc: str = ""
    area_m2: Decimal | None = Field(default=None, ge=0)
    tenant: TenantIn = TenantIn()


class LeaseIn(BaseModel):
    valid_from: date
    valid_to: date | None = None
    rent: Decimal = Field(ge=0)
    deposit: Decimal = Field(default=Decimal("0"), ge=0)
    advances: dict[str, Decimal] = {}


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cost: Decimal
    paid: Decimal = Decimal("0")


class ExtractedIn(BaseModel):
    """Položky, které přišly z dokumentu (fotka, PDF, později OCR)."""

    doc_label: str = ""
    items: list[ItemIn]


class ResolveIn(ExtractedIn):
    # Rozhodnutí u každé změněné položky: nahradit novým, nebo ponechat starý údaj
    decisions: dict[str, Literal["replace", "keep"]] = {}


class BankAssignIn(BaseModel):
    flat_id: int | None = None


class ListingIn(BaseModel):
    """Srovnatelná nabídka zadaná ručně."""

    kind: str = Field(default="rent", pattern="^(rent|sale)$")
    dispo: str = Field(default="", max_length=20)
    area_m2: Decimal | None = Field(default=None, ge=0)
    price: Decimal = Field(gt=0)
    url: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=300)
    locality: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=300)


class AcquisitionIn(BaseModel):
    acquired_on: date | None = None
    price: Decimal | None = Field(default=None, ge=0)


class VsIn(BaseModel):
    vs: str = Field(min_length=1, max_length=20)


class CostIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(ge=0)


class DocumentUpdate(BaseModel):
    # flat_id se bere jen, když ho klient poslal (null znamená odebrat byt)
    flat_id: int | None = None
    doc_type: str | None = None


class ValuationIn(BaseModel):
    radius_km: float = Field(ge=0.1, le=20)
