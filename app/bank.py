"""Výpisy z účtu: čtení CSV a PDF, párování plateb s byty a výpočet očekávaného a přijatého."""
import base64
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import documents

BANK_PROMPT = """Z přiloženého výpisu z účtu vypiš všechny PŘÍCHOZÍ platby (kladné částky).
Odpověz POUZE JSON polem objektů, každý s klíči:
- date: datum platby ve formátu YYYY-MM-DD
- amount: částka v Kč jako číslo
- vs: variabilní symbol jako text, nebo null
- counterparty: jméno plátce nebo název protiúčtu, nebo null
- note: zpráva pro příjemce, nebo null
Nevymýšlej údaje. Čísla účtů a rodná čísla nikdy nevypisuj."""


def normalize(text: str) -> str:
    return documents.normalize(text)


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _contains(needle: str, haystack: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    s = s.split()[0].rstrip(".") if s else ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s) -> Decimal | None:
    s = str(s or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _col(hdr: list[str], *keys: str) -> int | None:
    for i, h in enumerate(hdr):
        if any(k in h for k in keys):
            return i
    return None


def parse_csv(data: bytes) -> list[dict]:
    text = None
    for enc in ("utf-8-sig", "cp1250"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Soubor nejde přečíst. Zkuste export z banky stáhnout znovu.")

    head = text[:2000]
    delim = ";" if head.count(";") >= head.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))

    hdr_i = None
    for i, r in enumerate(rows[:40]):
        low = [c.lower() for c in r]
        if any(("částk" in c or "castk" in c or "amount" in c) for c in low):
            hdr_i = i
            break
    if hdr_i is None:
        raise ValueError("V souboru nenajdu sloupec s částkou.")

    hdr = [c.strip().lower() for c in rows[hdr_i]]
    ci_date = _col(hdr, "datum", "date")
    ci_amt = _col(hdr, "částk", "castk", "amount")
    ci_vs = _col(hdr, "variabil", "vs")
    ci_cp = _col(hdr, "protiúčet", "protiucet", "protistrana", "název", "nazev", "jméno", "jmeno", "plátce", "platce")
    ci_note = _col(hdr, "zpráva", "zprava", "poznámka", "poznamka", "popis")
    if ci_date is None:
        raise ValueError("V souboru nenajdu sloupec s datem.")

    out = []
    for r in rows[hdr_i + 1:]:
        def g(i):
            return r[i].strip() if i is not None and i < len(r) else ""
        d = _parse_date(g(ci_date))
        a = _parse_amount(g(ci_amt))
        if d is None or a is None or a <= 0:
            continue  # odchozí platby a řádky bez data nás nezajímají
        out.append({"date": d, "amount": a, "vs": digits(g(ci_vs)), "counterparty": g(ci_cp), "note": g(ci_note)})
    return out


def parse_pdf(data: bytes) -> list[dict]:
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.standard_b64encode(data).decode("ascii")}}
    text = documents.call_claude([block], BANK_PROMPT, max_tokens=8000)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError("Výpis se nepodařilo přečíst.")
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ValueError("Výpis se nepodařilo přečíst.")

    out = []
    for it in items:
        d = _parse_date(str(it.get("date") or ""))
        a = _parse_amount(it.get("amount"))
        if d is None or a is None or a <= 0:
            continue
        out.append({"date": d, "amount": a, "vs": digits(str(it.get("vs") or "")),
                    "counterparty": str(it.get("counterparty") or ""), "note": str(it.get("note") or "")})
    return out


def canon_amount(a) -> str:
    return str(Decimal(str(a)).quantize(Decimal("0.01")))


def dup_identity(d: date, amount, vs: str, counterparty: str) -> tuple:
    """Podle čeho poznáme stejnou platbu. Částka vždy se dvěma desetinnými místy, poznámka se nepočítá
    (CSV a PDF výpis ji píšou jinak). Bez VS rozhoduje i plátce."""
    cp = "" if vs else normalize(counterparty)
    return (d.isoformat(), canon_amount(amount), vs or "", cp)


def tx_key(r: dict) -> str:
    base = "|".join(dup_identity(r["date"], r["amount"], r["vs"], r["counterparty"]))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def tx_key_legacy(r: dict) -> str:
    """Klíč z dřívějších importů. Kvůli němu se opakovaný import staré platby nezdvojí."""
    base = f"{r['date'].isoformat()}|{r['amount']}|{r['vs']}|{normalize(r['counterparty'])}|{normalize(r['note'])}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def month_bounds(period: str) -> tuple[date, date]:
    y, m = map(int, period.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def expected_for(flat, first: date, last: date) -> Decimal:
    """Měsíční předpis (nájem + zálohy) platný v daném měsíci podle smlouvy. Nula, když žádná smlouva neplatí."""
    best = None
    for lease in flat.leases:
        if lease.valid_from <= last and (lease.valid_to is None or lease.valid_to >= first):
            if best is None or lease.valid_from > best.valid_from:
                best = lease
    if best is None:
        return Decimal("0")
    return Decimal(best.rent) + sum((Decimal(a.amount) for a in best.advances), Decimal("0"))


def _surname(flat) -> str:
    parts = normalize(flat.tenant_name).split()
    return parts[-1] if parts and len(parts[-1]) >= 4 else ""


def auto_match(tx: dict, period: str, flats, aliases) -> tuple[int | None, str]:
    """Najde byt pro platbu. Pořadí: variabilní symbol, dřív potvrzený plátce, jednoznačná částka, příjmení."""
    vs = tx["vs"]
    cp = normalize(tx["counterparty"])
    if not vs:
        return None, "none"  # bez VS (složenka, hotovost) se přiřazuje ručně, aby se nic nespárovalo špatně
    if vs:
        for al in aliases:
            if al.alias == f"vs:{vs}":
                return al.flat_id, "auto-vs"
    if cp:
        for al in aliases:
            if al.alias == f"cp:{cp}"[:200]:
                return al.flat_id, "auto-plátce"
    first, last = month_bounds(period)
    cands = [f.id for f in flats if (e := expected_for(f, first, last)) > 0 and e == tx["amount"]]
    if len(cands) == 1:
        return cands[0], "auto-částka"
    hits = [f.id for f in flats if _surname(f) and _contains(_surname(f), cp)]
    if len(hits) == 1:
        return hits[0], "auto-jméno"
    return None, "none"
