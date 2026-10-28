"""Vyúčtování služeb za kalendářní rok: co se přeúčtuje nájemníkovi a jak to vychází."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bank
from . import models as m

# Přeúčtovávají se jen tyto služby
BILLABLE = ("teplo", "ohřev", "voda", "stočné", "osvětlen", "elektřin", "výtah", "úklid", "tuv", "suv")
# Zkratky, které se musí shodovat celé slovo (TUV, SUV), aby se nechytly jiné názvy
BILLABLE_WORDS = ("tuv", "suv")
# Tyto položky se nikdy nepřeúčtují (správa vodoměrů, účetnictví, odměna SVJ, správa)
NOT_BILLABLE = ("vodoměr", "odečt", "účetn", "odměn", "správ")
SOURCE_TYPES = ("vyuctovani_svj", "vyuctovani_spravy")


def is_billable(name: str) -> bool:
    n = (name or "").lower()
    if any(k in n for k in NOT_BILLABLE):
        return False
    if any(re.search(rf"\b{w}\b", n) for w in BILLABLE_WORDS):
        return True
    return any(k in n for k in BILLABLE if k not in BILLABLE_WORDS)


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
    excluded = {str(k.get("adv")) for k in (flat.cmp_links or []) if k.get("adv") and k.get("bill") is False}
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
            if is_billable(a.name) and a.name not in excluded:
                out[a.name] = out.get(a.name, Decimal("0")) + Decimal(a.amount)
    return out


def svj_predpis_months(flat: m.Flat, year: int) -> dict[int, Decimal]:
    """Měsíční zálohy na SVJ podle předpisů platných v daném měsíci. Při překryvu se počítá jen nejnovější
    platný řádek pro každou položku, takže se nic nezdvojí."""
    out: dict[int, Decimal] = {}
    for mo in range(1, 13):
        d = date(year, mo, 15)
        best: dict[str, m.FlatCost] = {}
        for c in flat.costs:
            if (c.valid_from is None or c.valid_from <= d) and (c.valid_to is None or c.valid_to >= d):
                k = "".join(c.name.split()).upper()
                prev = best.get(k)
                if prev is None or (c.valid_from or date.min) > (prev.valid_from or date.min):
                    best[k] = c
        out[mo] = sum((Decimal(c.amount) for c in best.values()), Decimal("0"))
    return out


def compute(db: Session, flat: m.Flat, year: int) -> dict:
    items = svj_items(db, flat.id, year)
    by_month = svj_predpis_months(flat, year)
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
        "svj_predpis_by_month": by_month, "svj_predpis_total": sum(by_month.values(), Decimal("0")),
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
        "svj_predpis_by_month": {str(k): f(v) for k, v in res["svj_predpis_by_month"].items()},
        "svj_predpis_total": f(res["svj_predpis_total"]),
    }
