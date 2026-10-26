"""Údaje pronajímatele, které se vždy doplní do smluv. Přepsat jde jen v konkrétní smlouvě."""

LANDLORD = {
    "name": "Michal Gajdoš",
    "address": "Západní stráň 346, 250 72 Předboj, okres Praha-východ",
    "phone": "+420 724 116 856",
    "email": "michal.gajdos@seznam.cz",
}


def merged(given: dict | None) -> dict:
    """Vyplněné údaje z formuláře mají přednost, prázdné se doplní z profilu."""
    out = dict(LANDLORD)
    for k, v in (given or {}).items():
        if str(v or "").strip():
            out[k] = str(v).strip()
    return out
