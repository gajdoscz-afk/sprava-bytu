"""Vyúčtování služeb za kalendářní rok: co se přeúčtuje nájemníkovi a jak to vychází."""
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bank
from . import models as m

# Přeúčtovávají se jen tyto služby
BILLABLE = ("teplo", "ohřev", "voda", "stočné", "osvětlen", "elektřin", "výtah")
# Tyto položky se nikdy nepřeúčtují (správa vodoměrů, účetnictví, odměna SVJ, správa)
NOT_BILLABLE = ("vodoměr", "odečt", "účetn", "odměn", "správ")
SOURCE_TYPES = ("vyuctovani_svj", "vyuctovani_spravy")


def is_billable(name: str) -> bool:
    n = (name or "").lower()
    if any(k in n for k in NOT_BILLABLE):
        return False
    return any(k in n for k in BILLABLE)


def _money(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def svj_items(db: Session, flat_id: int, year: int) -> list[dict]:
    docs = db.scalars(select(m.Document).where(
        m.Document.flat_id == flat_id, m.Document.doc_type.in_(SOURCE_TYPES), m.Document.year == year)).all()
    items = []
    for d in docs:
        for it in (d.extracted or {}).get("items") or []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()[:200]
            cost = _money(it.get("cost"))
            if not name or cost is None:
                continue
            items.append({"name": name, "cost": cost, "paid": _money(it.get("paid")) or Decimal("0"),
                          "billable": is_billable(name), "doc_id": d.id})
    return items


def tenant_advances(flat: m.Flat, year: int) -> dict[str, Decimal]:
    """Zálohy na přeúčtovatelné služby předepsané nájemníkovi za každý měsíc roku podle platného předpisu."""
    out: dict[str, Decimal] = {}
    for mo in range(1, 13):
        _, last = bank.month_bounds(f"{year}-{mo:02d}")
        first = date(year, mo, 1)
        lease = None
        for l in flat.leases:
            if l.valid_from <= last and (l.valid_to is None or l.valid_to >= first):
                if lease is None or l.valid_from > lease.valid_from:
                    lease = l
        if lease is None:
            continue
        for a in lease.advances:
            if is_billable(a.name):
                out[a.name] = out.get(a.name, Decimal("0")) + Decimal(a.amount)
    return out


def compute(db: Session, flat: m.Flat, year: int) -> dict:
    items = svj_items(db, flat.id, year)
    billable = [i for i in items if i["billable"]]
    adv = tenant_advances(flat, year)
    bill_cost = sum((i["cost"] for i in billable), Decimal("0"))
    adv_total = sum(adv.values(), Decimal("0"))
    received = sum((t.amount for t in db.scalars(select(m.BankTx).where(
        m.BankTx.flat_id == flat.id, m.BankTx.period.like(f"{year}-%"))).all()), Decimal("0"))
    l_cost = sum((i["cost"] for i in items), Decimal("0"))
    l_paid = sum((i["paid"] for i in items), Decimal("0"))
    return {
        "year": year, "items": items, "billable": billable, "advances": adv,
        "billable_cost": bill_cost, "advances_total": adv_total,
        "tenant_balance": bill_cost - adv_total,  # kladné = nedoplatek nájemníka
        "received": received,
        "landlord_cost": l_cost, "landlord_paid": l_paid, "landlord_balance": l_paid - l_cost,
    }


def to_json(res: dict) -> dict:
    f = float
    return {
        "year": res["year"],
        "items": [{"name": i["name"], "cost": f(i["cost"]), "paid": f(i["paid"]), "billable": i["billable"]}
                  for i in res["items"]],
        "advances": {k: f(v) for k, v in res["advances"].items()},
        "billable_cost": f(res["billable_cost"]), "advances_total": f(res["advances_total"]),
        "tenant_balance": f(res["tenant_balance"]), "received": f(res["received"]),
        "landlord_cost": f(res["landlord_cost"]), "landlord_paid": f(res["landlord_paid"]),
        "landlord_balance": f(res["landlord_balance"]),
    }
