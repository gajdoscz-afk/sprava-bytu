"""Týdenní přehled trhu: daně, zákony a lhůty, nájemní trh, úrokové sazby, podnikání.

Zprávy hledá Claude s vyhledáváním na webu. Každá položka musí mít odkaz na zdroj, který si uživatel může ověřit.
Jde o informace, ne o právní nebo daňové poradenství.
"""
import json

from . import benchmark as bm

NEWS_PROMPT = """Dnes je {today}. Jsi asistent pro drobného pronajímatele bytů v Česku (Ústí nad Labem).
Najdi nejdůležitější aktuální zprávy z posledních 7 dnů a z nejbližších 90 dnů (plánované změny, lhůty), které se ho týkají:
- daně (daň z příjmu z pronájmu, paušální výdaje, lhůty pro podání přiznání, zálohy, DPH)
- zákony a vyhlášky (nájemní bydlení, ochrana nájemníků, SVJ, katastr, stavební a energetické předpisy, zvýšení poplatků)
- lhůty a termíny (podání, splatnosti, změny sazeb)
- úrokové sazby a hypotéky, inflace (ČNB, ČSÚ)
- trh s nájmy a prodeji bytů v Česku a v Ústí nad Labem
- podnikání v pronájmu (živnost, registrace, pojištění)
Hledej hlavně u zdrojů: financnisprava.gov.cz, mfcr.cz, zakonyprolidi.cz, cnb.cz, czso.cz, mpo.gov.cz, mmr.gov.cz, ceskenoviny.cz, sreality.cz a bezrealitky.cz (blog nebo tisková zpráva).
Odpověz POUZE JSON polem objektů, nejvíc 8, nejdůležitější první. Každý objekt má klíče:
- category: jedna z "Daně", "Zákony a lhůty", "Nájemní trh", "Sazby a inflace", "Podnikání", "Ostatní"
- importance: 1 (zásadní, hrozí sankce nebo je třeba jednat), 2 (důležité), 3 (na vědomí)
- title: krátký nadpis
- summary: 1 až 2 věty česky, co se stalo a co z toho plyne pro pronajímatele, včetně data účinnosti nebo lhůty, pokud je známá
- url: přímý odkaz na zdroj (povinné, jinak zprávu vynech)
Nic nevymýšlej. Pokud nic relevantního nenajdeš, vrať []. Odpověz jen JSON."""

FORMAT_PROMPT = """Z následujícího textu vypiš zprávy jako JSON pole objektů s klíči:
category, importance (1 až 3), title, summary, url (přímý odkaz na zdroj, pokud je v textu).
Zahrň jen zprávy s odkazem. Pokud žádné nejsou, vrať []. Nic nevymýšlej. Odpověz jen JSON."""

CATEGORIES = {"Daně", "Zákony a lhůty", "Nájemní trh", "Sazby a inflace", "Podnikání", "Ostatní"}


def _clean(rows: list) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()[:500]
        title = str(r.get("title") or "").strip()[:300]
        if not url.startswith("http") or not title:
            continue
        try:
            imp = min(3, max(1, int(r.get("importance") or 2)))
        except (TypeError, ValueError):
            imp = 2
        cat = str(r.get("category") or "Ostatní")
        out.append({
            "category": cat if cat in CATEGORIES else "Ostatní",
            "importance": imp,
            "title": title,
            "summary": str(r.get("summary") or "")[:1000],
            "url": url,
        })
    out.sort(key=lambda x: x["importance"])
    return out[:8]


def search_news(today: str) -> list[dict]:
    """Vrátí seznam zpráv s odkazem na zdroj. Při chybě vyhodí bm.SearchError."""
    import os
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise bm.SearchError("Přehled trhu zatím není zapnutý (chybí ANTHROPIC_API_KEY).")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    body = {
        "model": model,
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        "messages": [{"role": "user", "content": NEWS_PROMPT.format(today=today)}],
    }
    text = bm._claude_text(key, body, 300)
    rows = bm._extract_rows(text)
    if not rows and text.strip():
        fmt = {
            "model": model,
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": FORMAT_PROMPT + "\n\nTEXT:\n" + text[:20000]}],
        }
        rows = bm._extract_rows(bm._claude_text(key, fmt, 120))
    return _clean(rows)
