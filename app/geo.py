"""Hodnota bytu z podobných nabídek v okolí adresy.

Adresy převádíme na souřadnice (OpenStreetMap Nominatim). Hodnota vychází jen z nabídek prodeje,
které leží v zadaném okruhu (výchozí 1 km) a mají stejnou dispozici. Nejpodobnější nabídky
(plocha, vzdálenost) mají přednost. Pořizovací cena se nepoužívá.
"""
import math
import re
import statistics
import threading
import time

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "MG-Property-sprava-bytu/1.0"
DEFAULT_RADIUS_KM = 1.0
MIN_COMPS = 2
MAX_COMPS = 8
_last_call = [0.0]
_lock = threading.Lock()


def norm_dispo(s) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode(query: str) -> tuple[float, float] | None:
    """Souřadnice pro text adresy. Nominatim chce nejvýš jeden dotaz za sekundu. Při chybě vrací None."""
    import httpx  # importuje se až při použití, aby šel modul otestovat bez síťových knihoven
    with _lock:
        wait = 1.1 - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    try:
        r = httpx.get(NOMINATIM, params={"q": query, "format": "json", "limit": 1, "countrycodes": "cz"},
                      headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def flat_query(addr: str, zip_: str) -> str:
    parts = [addr, zip_ or "Ústí nad Labem", "Česká republika"]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def listing_query(address: str, locality: str) -> str:
    parts = [address, locality or "Ústí nad Labem", "Česká republika"]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def valuation(lat, lon, dispo: str, area, radius_km: float, listings) -> dict:
    """Odhad hodnoty bytu z nejpodobnějších nabídek prodeje v okruhu. `listings` jsou objekty s lat, lon, dispo, area_m2, price, url, title."""
    base = {"value": None, "basis": "", "confidence": "", "n": 0, "n_in_radius": 0, "radius_km": radius_km, "reason": "", "comps": []}
    if lat is None or lon is None:
        return {**base, "reason": "Byt nemá souřadnice. Zkontrolujte adresu."}
    nd = norm_dispo(dispo)
    if not nd:
        return {**base, "reason": "Chybí dispozice bytu."}

    cands = []
    for l in listings:
        if l.lat is None or l.lon is None or norm_dispo(l.dispo) != nd:
            continue
        d = haversine_km(lat, lon, l.lat, l.lon)
        if d <= radius_km:
            cands.append((l, d))
    base["n_in_radius"] = len(cands)
    if len(cands) < MIN_COMPS:
        return {**base, "reason": f"V okruhu {radius_km:g} km je {len(cands)} srovnatelných nabídek ({nd}). Zvětšete okruh."}

    def score(item):
        l, d = item
        size = abs(float(l.area_m2) - float(area)) / float(area) if (area and l.area_m2) else 0.5
        return size + d / radius_km

    cands.sort(key=score)
    chosen = cands[:MAX_COMPS]
    pm2 = [float(l.price) / float(l.area_m2) for l, _ in chosen if l.area_m2]
    if area and len(pm2) >= MIN_COMPS:
        med = statistics.median(pm2) * float(area)
        basis = "cena za m² podobných nabídek × plocha bytu"
        values = pm2
        area_based = True
    else:
        values = [float(l.price) for l, _ in chosen]
        med = statistics.median(values)
        basis = "medián cen podobných nabídek (plocha bytu nebo nabídek chybí)"
        area_based = False

    n = len(chosen)
    if len(values) >= 4:
        q1, _, q3 = statistics.quantiles(values, n=4)
        spread = (q3 - q1) / statistics.median(values) if statistics.median(values) else 1.0
    else:
        spread = 1.0
    if n >= 5 and spread <= 0.25:
        confidence = "vysoká"
    elif n >= 3:
        confidence = "střední"
    else:
        confidence = "nízká"
    if not area_based and confidence == "vysoká":
        confidence = "střední"

    comps = [{
        "url": l.url, "title": l.title, "price": float(l.price),
        "area_m2": float(l.area_m2) if l.area_m2 is not None else None,
        "dist_km": round(d, 2),
    } for l, d in chosen]
    return {**base, "value": round(med), "basis": basis, "confidence": confidence, "n": n, "comps": comps}
