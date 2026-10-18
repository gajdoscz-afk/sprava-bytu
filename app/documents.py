"""Čtení dokumentů: Claude přečte fotku nebo PDF a vrátí typ, adresu, rok a položky.

Hledání bytu je jen na straně aplikace (deterministicky), model nerozhoduje o přiřazení sám.
"""
import base64
import json
import os
import re
import unicodedata

import httpx

DOC_TYPES = {
    "smlouva": "Smlouva",
    "dodatek": "Dodatek",
    "vyuctovani_svj": "Vyúčtování SVJ",
    "vyuctovani_spravy": "Vyúčtování správy",
    "vyuctovani_najemnikovi": "Vyúčtování nájemníkovi",
    "faktura": "Faktura",
    "pojistka": "Pojistka",
    "fotky_stavu": "Fotky stavu bytu",
    "doklad_najemnika": "Doklad nájemníka",
    "jine": "Jiné",
}

SUPPORTED = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"}
AUTO_FILE_CONFIDENCE = 0.8

PROMPT = """Jsi asistentka správce bytů v ČR. Z přiloženého dokumentu urči typ a vytáhni údaje.
Odpověz POUZE jedním JSON objektem bez dalšího textu s klíči:
- doc_type: jeden z [smlouva, dodatek, vyuctovani_svj, vyuctovani_spravy, vyuctovani_najemnikovi, faktura, pojistka, fotky_stavu, doklad_najemnika, jine]
  vyuctovani_najemnikovi = vyúčtování služeb, které vystavil pronajímatel nájemníkovi (nájemník doplácí nebo má přeplatek).
  vyuctovani_spravy = vyúčtování od správy domu nebo SVJ pronajímateli.
- year: rok, ke kterému dokument patří (číslo), nebo null
- address: adresa bytu nebo domu přesně tak, jak je v dokumentu, nebo null
- tenant_name: jméno nájemníka nebo null
- summary: 1 až 2 věty česky o tom, co dokument je a co obsahuje
- confidence: číslo 0 až 1, jak jsi si jistá typem i adresou
- items: u vyúčtování seznam položek, každá {"name", "cost", "paid", "unit", "quantity", "category"}.
  cost a paid jsou částky v Kč jako čísla, unit je jednotka (GJ, m3, Kč...) nebo null, quantity číslo nebo null.
  category je "prenositelne", "neprenositelne" nebo "neurceno".
  Neprenositelné jsou náklady vlastníka, které nelze účtovat nájemníkovi: fond oprav, příspěvek na účetnictví a správu domu,
  správa vodoměrů a podobné. Ostatní označ "neurceno", pokud si nejsi jistá.
  U jiných typů dokumentů vrať prázdný seznam.
Nevymýšlej údaje, které v dokumentu nejsou. Rodná čísla a čísla OP nikdy nevypisuj."""


class ClassifyError(Exception):
    pass


def normalize(text: str) -> str:
    """Malá písmena, bez diakritiky, jen slova oddělená mezerou. 'Sibiřská 375/12' -> 'sibirska 375 12'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def classify(data: bytes, mime: str) -> dict:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ClassifyError("Čtení dokumentů zatím není zapnuté (chybí ANTHROPIC_API_KEY). Zařaďte ručně.")
    if mime not in SUPPORTED:
        raise ClassifyError("Nepodporovaný formát. Nahrajte JPG, PNG nebo PDF.")

    b64 = base64.standard_b64encode(data).decode("ascii")
    if mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}

    body = {
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=120)
        r.raise_for_status()
        text = "".join(c.get("text", "") for c in r.json().get("content", []) if c.get("type") == "text")
    except httpx.HTTPError as e:
        raise ClassifyError(f"Čtení selhalo: {e}")

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ClassifyError("Odpověď čtení nešla přečíst.")
    try:
        result = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ClassifyError("Odpověď čtení nešla přečíst.")

    if result.get("doc_type") not in DOC_TYPES:
        result["doc_type"] = "jine"
    try:
        result["confidence"] = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    return result


def _contains(needle: str, haystack: str) -> bool:
    """Hledá celá slova, takže '375 1' nesedí na '375 12'."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def suggest_flat(result: dict, flats, aliases, filename: str = "") -> int | None:
    """Vrátí id bytu, pokud je shoda jednoznačná. Jinak None.

    Hledá v adrese, shrnutí, názvu souboru a v příjmení nájemníka (příjmení stačí, protože se často píše jen v názvu).
    """
    text = normalize(" ".join([str(result.get(k) or "") for k in ("address", "tenant_name", "summary")] + [filename]))
    if not text:
        return None
    scores: dict[int, int] = {}
    for f in flats:
        if _contains(normalize(f.addr), text):
            scores[f.id] = scores.get(f.id, 0) + 3
        if _contains(normalize(f.tenant_name), text):
            scores[f.id] = scores.get(f.id, 0) + 2
        parts = normalize(f.tenant_name).split()
        surname = parts[-1] if parts else ""
        if len(surname) >= 4 and _contains(surname, text):
            scores[f.id] = scores.get(f.id, 0) + 2
    for al in aliases:
        if _contains(al.alias, text):
            scores[al.flat_id] = scores.get(al.flat_id, 0) + 3
    if not scores:
        return None
    best = max(scores.values())
    winners = [fid for fid, sc in scores.items() if sc == best]
    return winners[0] if len(winners) == 1 and best >= 3 else None
