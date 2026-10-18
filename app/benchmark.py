"""Price Benchmark: nabídky nájmů z trhu, odhad tržního nájmu a návrh nového nájmu.

Nabídky hledá Claude s vyhledáváním na webu. Odhad počítáme sami z nabídek, aby byl vidět postup.
Spolehlivost se odvíjí od počtu srovnatelných nabídek a od jejich rozptylu.
"""
import json
import os
import re
import statistics
from decimal import ROUND_HALF_UP, Decimal

import httpx

# Návrh nového nájmu se ukáže, jen když je nájem o víc než tuto částku pod odhadem
MIN_DIFF_KC = Decimal("1000")
DEFAULT_LOCALITY = "Ústí nad Labem"

SEARCH_PROMPT = """Najdi aktuální nabídky ke PRONÁJMU bytů v lokalitě {locality}, dispozice {dispo}.
Hledej na veřejných realitních portálech (například sreality.cz, bezrealitky.cz, realitymix.cz, reality.idnes.cz).
Odpověz POUZE JSON polem objektů, každý s klíči:
- title: název nebo popis nabídky
- url: přímý odkaz na inzerát (povinné, jinak nabídku vynech)
- dispo: dispozice, např. "2+kk"
- area_m2: podlahová plocha v m² jako číslo, nebo null
- price: měsíční nájem v Kč bez záloh jako číslo (povinné)
- locality: obec nebo část města
Zahrň jen nabídky, které skutečně najdeš. Nejvíc 15. Nic nevymýšlej."""


class SearchError(Exception):
    pass


def norm_dispo(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def search_listings(locality: str, dispo: str) -> list[dict]:
    """Zavolá Claude s vyhledáváním na webu a vrátí očištěné nabídky."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise SearchError("Hledání nabídek zatím není zapnuté (chybí ANTHROPIC_API_KEY).")
    body = {
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        "messages": [{"role": "user", "content": SEARCH_PROMPT.format(locality=locality, dispo=dispo or "libovolná")}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=240)
        r.raise_for_status()
        text = "".join(c.get("text", "") for c in r.json().get("content", []) if c.get("type") == "text")
    except httpx.HTTPError as e:
        raise SearchError(f"Hledání selhalo: {e}")

    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()[:500]
        try:
            price = Decimal(str(row.get("price"))).quantize(Decimal("0.01"))
        except ArithmeticError:
            continue
        if not url.startswith("http") or price <= 0:
            continue
        area = None
        if row.get("area_m2") not in (None, ""):
            try:
                area = Decimal(str(row["area_m2"])).quantize(Decimal("0.01"))
            except ArithmeticError:
                area = None
        out.append({
            "url": url,
            "title": str(row.get("title") or "")[:300],
            "dispo": str(row.get("dispo") or "")[:20],
            "area_m2": area,
            "price": price,
            "locality": str(row.get("locality") or locality)[:120],
        })
    return out


def estimate(flat_dispo: str, flat_area: Decimal | None, listings) -> dict | None:
    """Odhad tržního nájmu z nabídek se stejnou dispozicí.

    Pokud je známa plocha bytu a nabídky mají plochu, počítá se medián ceny za m² a vynásobí plochou bytu.
    Jinak se bere medián celkových nájmů (méně přesné, spolehlivost nejvýš střední).
    """
    same = [l for l in listings if norm_dispo(l.dispo) == norm_dispo(flat_dispo)]
    if not same:
        return None

    per_m2 = [float(l.price / l.area) for l in same if l.area]
    if flat_area and len(per_m2) >= 3:
        values = per_m2
        basis = "cena za m² × plocha bytu"
        scale = float(flat_area)
        per_m2_basis = True
    else:
        values = [float(l.price) for l in same]
        basis = "medián celkových nájmů"
        scale = 1.0
        per_m2_basis = False

    if len(values) < 3:
        # Málo dat: jen orientační medián, bez spolehlivosti
        med = statistics.median(values) * scale
        return {"estimate": round(med), "n": len(values), "confidence": "nízká",
                "basis": basis, "low": round(min(values) * scale), "high": round(max(values) * scale)}

    med = statistics.median(values)
    q1, _, q3 = statistics.quantiles(values, n=4)
    spread = (q3 - q1) / med if med else 1.0
    n = len(values)
    if n >= 6 and spread <= 0.25:
        confidence = "vysoká"
    elif n >= 3:
        confidence = "střední"
    else:
        confidence = "nízká"
    if not per_m2_basis and confidence == "vysoká":
        confidence = "střední"  # bez přepočtu na m² nejde o vysokou spolehlivost

    return {
        "estimate": round(med * scale),
        "n": n,
        "confidence": confidence,
        "basis": basis,
        "low": round(q1 * scale),
        "high": round(q3 * scale),
    }


def suggest_rent(current: Decimal | None, est: int | None) -> Decimal | None:
    """Navrhne nový nájem, jen když je současný nájem o víc než MIN_DIFF_KC pod odhadem. Zaokrouhlí na stovky."""
    if current is None or est is None:
        return None
    if Decimal(est) - current <= MIN_DIFF_KC:
        return None
    return (Decimal(est) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 100
