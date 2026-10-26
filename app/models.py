from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Flat(Base):
    __tablename__ = "flats"

    id: Mapped[int] = mapped_column(primary_key=True)
    addr: Mapped[str] = mapped_column(String(200))
    dispo: Mapped[str] = mapped_column(String(20), default="")
    floor: Mapped[str] = mapped_column(String(20), default="")
    zip: Mapped[str] = mapped_column(String(120), default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    bank_acc: Mapped[str] = mapped_column(String(60), default="")
    tenant_name: Mapped[str] = mapped_column(String(200), default="")
    tenant_email: Mapped[str] = mapped_column(String(200), default="")
    tenant_phone: Mapped[str] = mapped_column(String(60), default="")
    tenant_others: Mapped[str] = mapped_column(Text, default="")  # jména oddělená čárkou
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)  # podlahová plocha, z dokumentů nebo ručně
    field_dates: Mapped[dict] = mapped_column(JSON, default=dict)  # datum dokumentu (nebo úpravy), ze kterého je údaj: {"dispo": "2025-03-01"}
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)  # souřadnice adresy bytu
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    valuation_radius_km: Mapped[float] = mapped_column(Float, default=1.0)  # okruh pro hledání podobných nabídek

    leases: Mapped[list["Lease"]] = relationship(
        back_populates="flat", cascade="all, delete-orphan", order_by="Lease.valid_from"
    )
    statements: Mapped[list["Statement"]] = relationship(
        back_populates="flat", cascade="all, delete-orphan", order_by="Statement.year"
    )
    costs: Mapped[list["FlatCost"]] = relationship(cascade="all, delete-orphan", order_by="FlatCost.id")
    acquisition: Mapped["FlatAcquisition | None"] = relationship(cascade="all, delete-orphan", uselist=False)


class Lease(Base):
    """Předpis: nájem a zálohy platné v daném období."""

    __tablename__ = "leases"

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    rent: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    deposit: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # datum dokumentu (smlouvy), ze kterého předpis je
    persons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # počet osob, na který je předpis záloh na vodu

    flat: Mapped[Flat] = relationship(back_populates="leases")
    advances: Mapped[list["Advance"]] = relationship(cascade="all, delete-orphan", order_by="Advance.id")


class Advance(Base):
    __tablename__ = "advances"

    id: Mapped[int] = mapped_column(primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Statement(Base):
    """Vyúčtování služeb za jeden rok."""

    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("flat_id", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    doc_label: Mapped[str] = mapped_column(String(300), default="")

    flat: Mapped[Flat] = relationship(back_populates="statements")
    items: Mapped[list["StatementItem"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", order_by="StatementItem.position"
    )


class StatementItem(Base):
    __tablename__ = "statement_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)

    statement: Mapped[Statement] = relationship(back_populates="items")


class Version(Base):
    """Historie změn. Každá přepsaná hodnota zůstane uložená a dá se vrátit."""

    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), index=True)
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id", ondelete="SET NULL"), nullable=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("statement_items.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(40))  # nahrazeno, ponecháno, přidáno, vráceno, uložen
    old: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    """Nahraný dokument (fotka nebo PDF). Soubor je v databázi, takže není třeba další služba."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int | None] = mapped_column(
        ForeignKey("flats.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="ke_kontrole")  # ke_kontrole | zarazeno | rozpracováno (stránka skupiny)
    group_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # stránky jednoho dokumentu
    doc_type: Mapped[str] = mapped_column(String(40), default="jine")
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer, default=0)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    suggested_flat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # celý výstup čtení
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FlatAlias(Base):
    """Jiný zápis adresy, který aplikace pozná (např. 'Sibiřská 375' = byt 2). Vzniká potvrzením dokumentu."""

    __tablename__ = "flat_aliases"
    __table_args__ = (UniqueConstraint("flat_id", "alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(200))


class FlatCost(Base):
    """Měsíční platba, kterou platíte vy (např. záloha SVJ na teplo). Nájemníkovi se nepřeúčtuje."""

    __tablename__ = "flat_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)  # od kdy platí (None = odjakživa)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)  # do kdy platí (None = dosud)


class BankTx(Base):
    """Příchozí platba z výpisu účtu. Přiřazení k bytu je buď automatické, nebo ruční."""

    __tablename__ = "bank_txs"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)  # proti duplicitám při opakovaném importu
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    vs: Mapped[str] = mapped_column(String(20), default="")
    counterparty: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM, za který měsíc platba je
    flat_id: Mapped[int | None] = mapped_column(ForeignKey("flats.id", ondelete="SET NULL"), nullable=True, index=True)
    match: Mapped[str] = mapped_column(String(20), default="none")  # auto-vs, auto-částka, auto-jméno, ručně, none


class Listing(Base):
    """Srovnatelná nabídka na trhu (nájem nebo prodej). Vzniká ručně nebo hledáním na webu."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), default="rent")  # rent | sale
    locality: Mapped[str] = mapped_column(String(120), default="Ústí nad Labem")
    dispo: Mapped[str] = mapped_column(String(20), default="")
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    address: Mapped[str] = mapped_column(String(300), default="")  # ulice a číslo z inzerátu, pokud je
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(40), default="ručně")  # ručně | web
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketSnapshot(Base):
    """Týdenní snímek mediánu cen na trhu pro dispozici. Z něj se počítá růst za měsíc a za rok."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(10))  # rent | sale
    dispo: Mapped[str] = mapped_column(String(20), index=True)  # normalizovaná dispozice
    n: Mapped[int] = mapped_column(Integer, default=0)
    median_pm2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    median_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)


class FlatAcquisition(Base):
    """Pořízení bytu: datum koupě a cena. Vyplní se z kupní smlouvy, nebo ručně (ruční údaj má přednost)."""

    __tablename__ = "flat_acquisitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id", ondelete="CASCADE"), unique=True)
    acquired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="ručně")  # ručně | kupní smlouva
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class PortfolioSnapshot(Base):
    """Snímek hodnoty celého portfolia a hodnoty každého bytu. Zapisuje se při týdenním hledání, z něj jsou měsíční změny."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    per_flat: Mapped[dict] = mapped_column(JSON, default=dict)  # {"id bytu": hodnota v Kč}


class MarketNews(Base):
    """Zpráva z tržního přehledu (daně, zákony a lhůty, nájemní trh, sazby). Týdně se nahrazuje."""

    __tablename__ = "market_news"

    id: Mapped[int] = mapped_column(primary_key=True)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    week: Mapped[str] = mapped_column(String(10), index=True)  # pondělí přehledu, YYYY-MM-DD
    category: Mapped[str] = mapped_column(String(40), default="Ostatní")
    importance: Mapped[int] = mapped_column(Integer, default=2)  # 1 = nejdůležitější
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
