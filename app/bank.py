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


# Výchozí účty SVJ podle názvu ulice (naplní se jen u bytů, kde účet ještě není; pak se řídí kartou bytu)
SVJ_BUILDINGS = {
    "masarykova": "893261389/0800",
    "revolucni": "243441142/0300",
    "seifertova": "224383995/0300",
    "skolni": "2819095369/0800",
    "rozcesti": "268258846/0600",
}


def svj_vs_key(addr: str) -> str:
    """Rozcestí má VS platby SVJ ve tvaru 721 + číslo bytu na tři místa (Rozcestí 12 -> 721012)."""
    m = re.match(r"rozcesti (\d+)$", normalize(addr).split("  ")[0].strip())
    return f"721{int(m.group(1)):03d}" if m else ""


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
    ci_acc = _col(hdr, "číslo protiúčtu", "cislo protiuctu")
    ci_bank = _col(hdr, "kód banky protiúčtu", "kod banky protiuctu")
    ci_note = _col(hdr, "vlastní poznámka", "vlastni poznamka", "poznámka", "poznamka", "popis")
    ci_note2 = _col(hdr, "zpráva", "zprava")  # u CSOB je zpráva pro příjemce ve vlastním sloupci
    if ci_date is None:
        raise ValueError("V souboru nenajdu sloupec s datem.")

    out = []
    for r in rows[hdr_i + 1:]:
        def g(i):
            return r[i].strip() if i is not None and i < len(r) else ""
        d = _parse_date(g(ci_date))
        a = _parse_amount(g(ci_amt))
        if d is None or a is None or a == 0:
            continue  # řádky bez data nebo bez částky nás nezajímají
        acc = f"{g(ci_acc)}/{g(ci_bank)}" if g(ci_acc) else ""
        out.append({"date": d, "amount": abs(a), "dir": "out" if a < 0 else "in", "vs": digits(g(ci_vs)),
                    "counterparty": g(ci_cp), "account": acc,
                    "note": " ".join(x for x in (g(ci_note), g(ci_note2)) if x)})
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


def canon_vs(vs: str) -> str:
    """VS bez úvodních nul: PDF výpis píše '0000000000' tam, kde CSV píše '0' nebo nic."""
    return (vs or "").strip().lstrip("0")


def dup_identity(d: date, amount, vs: str, counterparty: str) -> tuple:
    """Podle čeho poznáme stejnou platbu. Částka se dvěma desetinnými místy, VS bez nul, poznámka se nepočítá
    (CSV a PDF výpis ji píšou jinak). Bez VS rozhoduje i plátce."""
    v = canon_vs(vs)
    cp = "" if v else normalize(counterparty)
    return (d.isoformat(), canon_amount(amount), v, cp)


def tx_key(r: dict) -> str:
    base = "|".join(dup_identity(r["date"], r["amount"], r["vs"], r["counterparty"]))
    if r.get("dir") == "out":
        base = "out|" + base  # odchozí platba nikdy nesplyne s příchozí
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def tx_key_v2(r: dict) -> str:
    """Klíč z importů verze 1.46–1.50 (VS se bralo doslova). Kvůli němu se opakovaný import nezdvojí."""
    cp = "" if r["vs"] else normalize(r["counterparty"])
    base = "|".join((r["date"].isoformat(), canon_amount(r["amount"]), r["vs"] or "", cp))
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


def _street(flat) -> str:
    """Ulice s číslem popisným (např. 'rozcesti 90'), aby se dva byty ve stejné ulici nepletly."""
    parts = normalize(flat.addr).split()
    return " ".join(parts[:2]) if len(parts) >= 2 and len(parts[0]) >= 5 and parts[1].isdigit() else ""


def suggest(amount, vs: str, counterparty: str, note: str, period: str, flats, aliases) -> tuple[int | None, str]:
    """Návrh bytu. Rozhoduje částka: musí se rovnat předpisu bytu v daném měsíci. Zelená = částka sedí
    a zároveň jméno, adresa, VS nebo známý plátce. Oranžová = sedí jen částka u jediného bytu.
    Jinak nic (ručně)."""
    vs = canon_vs(vs)
    text = normalize(f"{counterparty} {note}")
    cp = normalize(counterparty)
    first, last = month_bounds(period)
    amt = Decimal(str(amount))
    same = [f for f in flats if (e := expected_for(f, first, last)) > 0 and e == amt]
    if not same:
        return None, ""

    def named(f) -> bool:
        sn, st = _surname(f), _street(f)
        return bool((sn and _contains(sn, text)) or (st and _contains(st, text)))

    def known(f) -> bool:
        return any(al.flat_id == f.id and ((vs and al.alias == f"vs:{vs}") or (cp and al.alias == f"cp:{cp}"[:200]))
                   for al in aliases)

    if len(same) != 1:  # víc bytů se stejnou částkou: nerozhodujeme, nechte to na ručním přiřazení
        return None, ""
    f = same[0]
    return f.id, ("zelena" if named(f) or known(f) else "oranzova")
