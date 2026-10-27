import io
import os
import secrets
import statistics
import threading
import time
from contextlib import asynccontextmanager
from decimal import Decimal

from pathlib import Path

import hashlib
import hmac
import mimetypes
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import bank
from . import benchmark as bm
from . import documents
from . import models as m
from . import news as nw
from . import geo as gx
from . import contracts
from . import landlord as landlord_profile
from . import protocols
from . import pdfgen
from . import docgen
from . import settlement as stl
from . import schemas as s
from .db import Base, engine, get_db

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

security = HTTPBasic(auto_error=False)
SESSION_COOKIE = "sb_session"
SESSION_DAYS = 90


def _session_token(user: str, pwd: str, expires: int) -> str:
    key = hashlib.sha256(("sb-session:" + pwd).encode()).digest()
    sig = hmac.new(key, f"{user}|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _valid_session(token: str, user: str, pwd: str) -> bool:
    try:
        expires = int(token.split(".", 1)[0])
    except (ValueError, AttributeError):
        return False
    if expires < time.time():
        return False
    return secrets.compare_digest(token, _session_token(user, pwd, expires))


PUBLIC_PATHS = {"/manifest.json", "/icon-180.png", "/icon-192.png", "/icon-512.png"}  # ikona na ploše, bez dat


def require_auth(request: Request, response: Response, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Jeden uživatel. Přihlášení přes BASIC_USER a BASIC_PASS, pak se nastaví přihlašovací cookie na 90 dní,
    takže se při každém obnovení stránky znovu neptá. Bez BASIC_USER a BASIC_PASS se server nespustí."""
    if request.url.path in PUBLIC_PATHS:
        return
    user, pwd = os.getenv("BASIC_USER"), os.getenv("BASIC_PASS")
    if not user or not pwd:
        if os.getenv("ALLOW_NO_AUTH") == "1":  # jen pro lokální vývoj a testy
            return
        raise HTTPException(500, "Na serveru chybí BASIC_USER a BASIC_PASS")
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and _valid_session(token, user, pwd):
        return
    ok = creds is not None and secrets.compare_digest(creds.username, user) and secrets.compare_digest(creds.password, pwd)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Neplatné přihlášení", {"WWW-Authenticate": "Basic"})
    expires = int(time.time()) + SESSION_DAYS * 86400
    response.set_cookie(SESSION_COOKIE, _session_token(user, pwd, expires), max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=True, samesite="lax")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # MVP: tabulky vytvoříme při startu. Až budou změny schématu, přejdeme na Alembic.
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # create_all nové sloupce do existujících tabulek nepřidá
        conn.execute(text("ALTER TABLE flats ADD COLUMN IF NOT EXISTS area_m2 NUMERIC(8,2)"))
        conn.execute(text("ALTER TABLE flats ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE flats ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE flats ADD COLUMN IF NOT EXISTS valuation_radius_km DOUBLE PRECISION NOT NULL DEFAULT 1.0"))
        conn.execute(text("ALTER TABLE listings ADD COLUMN IF NOT EXISTS address VARCHAR(300) NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE listings ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE listings ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS group_key VARCHAR(40)"))
        conn.execute(text("ALTER TABLE flats ADD COLUMN IF NOT EXISTS field_dates JSON NOT NULL DEFAULT '{}'"))
        conn.execute(text("ALTER TABLE leases ADD COLUMN IF NOT EXISTS source_date DATE"))
        conn.execute(text("ALTER TABLE leases ADD COLUMN IF NOT EXISTS persons INTEGER"))
        conn.execute(text("ALTER TABLE flat_costs ADD COLUMN IF NOT EXISTS valid_from DATE"))
        conn.execute(text("ALTER TABLE flat_costs ADD COLUMN IF NOT EXISTS valid_to DATE"))
        conn.execute(text("ALTER TABLE flat_costs ADD COLUMN IF NOT EXISTS source_doc_id INTEGER"))
        conn.execute(text("ALTER TABLE flat_costs ADD COLUMN IF NOT EXISTS ended_by_doc_id INTEGER"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS valid_from DATE"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS valid_to DATE"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS taken_at TIMESTAMPTZ"))
    try:
        with Session(engine) as db:
            migrate_snapshots_to_values(db)
    except Exception as e:
        print(f"[portfolio] migrace snímků selhala: {e}", flush=True)
    _fix_owners_from_documents()
    threading.Thread(target=_benchmark_scheduler, daemon=True).start()
    yield


def _fix_owners_from_documents() -> None:
    """Jednorázová oprava: vlastníka dříve doplnil dokument (prodávající z kupní smlouvy).
    Přepíše ho výchozím správcem, jen když je pořád stejný jako ta doplněná hodnota. Ruční úpravu nechá."""
    with Session(engine) as db:
        rows = db.scalars(select(m.Version).where(
            m.Version.name == "Vlastník", m.Version.action == "doplněno z dokumentu")).all()
        if not rows:
            return
        for v in rows:
            f = db.get(m.Flat, v.flat_id)
            if f is not None and f.owner == (v.new or {}).get("value"):
                f.owner = DEFAULT_OWNER
            db.delete(v)
        db.commit()


BENCH_WEEKDAY = 0  # pondělí: tržní přehled (zprávy)
BENCH_DAY = 1      # 1. den v měsíci: hledání nabídek, odhad hodnoty a snímek portfolia
BENCH_HOUR = 7     # od 7:00 pražského času


# Stav běžícího hledání u všech bytů, aby ho stránka mohla ukázat
BENCH_STATE: dict = {"running": False, "step": "", "done": 0, "total": 0, "result": None, "error": None}


def _run_refresh_bg() -> None:
    try:
        result = run_benchmark_refresh()
        BENCH_STATE.update(result=result, error=None)
    except Exception as e:
        BENCH_STATE.update(error=str(e))
    finally:
        BENCH_STATE.update(running=False, step="")


def run_benchmark_refresh() -> dict:
    """Najde nabídky nájmů i prodejů na webu pro každou dispozici bytů, uloží nové a zapíše týdenní snímek cen.
    Hledá se podle dispozice, ne po bytech."""
    done = {"searched": 0, "new": 0, "errors": 0}
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        dispos = {bm.norm_dispo(f.dispo) for f in db.scalars(select(m.Flat)).all() if f.dispo and f.dispo.strip()}
        known = set(db.scalars(select(m.Listing.url)).all())
        BENCH_STATE.update(total=len(dispos) * 2, done=0)
        for kind in ("rent", "sale"):
            for dispo in sorted(dispos):
                BENCH_STATE.update(step=f"{'nájmy' if kind == 'rent' else 'prodej'} · {dispo}")
                BENCH_STATE["done"] += 1
                try:
                    found, _note = bm.search_listings(bm.DEFAULT_LOCALITY, dispo, kind)
                except bm.SearchError as e:
                    done["errors"] += 1
                    print(f"[benchmark] {kind} {dispo}: {e}", flush=True)
                    continue
                done["searched"] += 1
                for it in found:
                    if it["url"] in known:
                        continue
                    known.add(it["url"])
                    db.add(m.Listing(kind=kind, locality=it["locality"], dispo=it["dispo"], area_m2=it["area_m2"],
                                     price=it["price"], url=it["url"], title=it["title"], source="web",
                                     address=it.get("address", "")))
                    done["new"] += 1
                db.commit()
                # Snímek mediánu z nabídek, které v databázi jsou
                same = [l for l in db.scalars(select(m.Listing).where(m.Listing.kind == kind)).all()
                        if bm.norm_dispo(l.dispo) == dispo]
                if same:
                    pm2 = [float(l.price / l.area_m2) for l in same if l.area_m2]
                    db.add(m.MarketSnapshot(
                        taken_at=now, kind=kind, dispo=dispo, n=len(same),
                        median_pm2=Decimal(str(round(statistics.median(pm2), 2))) if pm2 else None,
                        median_price=Decimal(str(round(statistics.median([float(l.price) for l in same]), 2))),
                    ))
                db.commit()
    try:
        ensure_geo()
    except Exception as e:
        print(f"[geo] doplnění souřadnic selhalo: {e}", flush=True)
    print(f"[benchmark] hotovo: {done}", flush=True)
    try:
        snapshot_portfolio_now()
    except Exception as e:
        print(f"[portfolio] snímek selhal: {e}", flush=True)
    return done


def _snapshot_growth(db: Session, kind: str, dispo: str) -> tuple[float | None, float | None]:
    """Růst mediánu ceny za m² (nebo celkové ceny, pokud m² chybí) za měsíc a za rok. Vrací procenta nebo None."""
    snaps = db.scalars(select(m.MarketSnapshot).where(
        m.MarketSnapshot.kind == kind, m.MarketSnapshot.dispo == bm.norm_dispo(dispo)
    ).order_by(m.MarketSnapshot.taken_at)).all()
    if not snaps:
        return None, None
    latest = snaps[-1]

    def value(s: m.MarketSnapshot) -> float | None:
        return float(s.median_pm2) if s.median_pm2 is not None else (float(s.median_price) if s.median_price else None)

    def base(days: int, tolerance: int) -> float | None:
        target = latest.taken_at - timedelta(days=days)
        cands = [s for s in snaps if s.taken_at < latest.taken_at and abs((s.taken_at - target).days) <= tolerance]
        if not cands:
            return None
        return value(min(cands, key=lambda s: abs((s.taken_at - target).total_seconds())))

    return bm.growth_pct(value(latest), base(30, 10)), bm.growth_pct(value(latest), base(365, 45))


def today_prague() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Prague")).date()
    except Exception:
        return date.today()


def latest_flat_values(db: Session, on: date | None = None) -> dict:
    """Pro každý byt poslední platná hodnota k danému dni. Při shodě data vyhrává ruční zápis."""
    on = on or today_prague()
    entries = db.scalars(select(m.FlatValue).where(m.FlatValue.on_date <= on)).all()
    latest: dict = {}
    for v in sorted(entries, key=lambda e: (e.on_date, e.source != "odhad")):
        latest[v.flat_id] = v
    return latest


def portfolio_values(db: Session) -> list[dict]:
    """Hodnota každého bytu = poslední zadaná nebo odhadnutá hodnota. Odhad z nabídek je uveden zvlášť pro porovnání."""
    sales = db.scalars(select(m.Listing).where(m.Listing.kind == "sale")).all()
    latest = latest_flat_values(db)
    pending = {sg.flat_id: sg for sg in db.scalars(
        select(m.ValueSuggestion).where(m.ValueSuggestion.status == "čeká")).all()}
    rows = []
    for f in db.scalars(select(m.Flat).order_by(m.Flat.addr)).all():
        est = gx.valuation(f.lat, f.lon, f.dispo, f.area_m2, f.valuation_radius_km or gx.DEFAULT_RADIUS_KM, sales)
        e = latest.get(f.id)
        sg = pending.get(f.id)
        rows.append({
            "suggestion": {"id": sg.id, "amount": float(sg.amount), "basis": sg.basis, "n": sg.n,
                           "created_at": sg.created_at.isoformat()} if sg else None,
            "id": f.id, "addr": f.addr,
            "value": float(e.amount) if e else None,
            "source": e.source if e else None,
            "value_date": e.on_date.isoformat() if e else None,
            "note": e.note if e else "",
            "estimate": est.get("value"),
            "basis": est.get("basis", ""), "confidence": est.get("confidence", ""),
            "n": est.get("n", 0), "n_in_radius": est.get("n_in_radius", 0),
            "radius_km": est.get("radius_km"), "reason": est.get("reason", ""), "comps": est.get("comps", []),
        })
    return rows


def portfolio_history(db: Session) -> list[dict]:
    """Celková hodnota k každému datu, kdy se něco zapsalo. Hodnota bytu se přenáší do dalšího zápisu.
    Poslední bod je vždy stejný jako aktuální součet na stránce Portfolio."""
    by_date: dict = defaultdict(list)
    for e in db.scalars(select(m.FlatValue)).all():
        by_date[e.on_date].append(e)
    state: dict = {}
    hist = []
    for d in sorted(by_date):
        for e in sorted(by_date[d], key=lambda e: e.source != "odhad"):
            state[e.flat_id] = e
        hist.append({"date": d.isoformat(), "total": round(sum(float(e.amount) for e in state.values()), 2)})
    return hist


def migrate_snapshots_to_values(db: Session) -> None:
    """Jednorázově přepíše dřívější snímky portfolia do hodnot bytů, aby graf neztratil historii."""
    if db.scalar(select(func.count()).select_from(m.FlatValue)):
        return
    flat_ids = set(db.scalars(select(m.Flat.id)).all())
    seen = set()
    for snap in db.scalars(select(m.PortfolioSnapshot).order_by(m.PortfolioSnapshot.taken_at)).all():
        d = snap.taken_at.astimezone(timezone.utc).date()
        for fid, val in (snap.per_flat or {}).items():
            fid = int(fid)
            if fid in flat_ids and val is not None and (fid, d) not in seen:
                seen.add((fid, d))
                db.add(m.FlatValue(flat_id=fid, on_date=d, amount=Decimal(str(val)), source="odhad"))
    db.commit()


def ensure_geo(limit: int = 200) -> None:
    """Doplní souřadnice bytům a nabídkám, které je ještě nemají. Nominatim chce max. 1 dotaz za sekundu (hlídá geo.geocode)."""
    with Session(engine) as db:
        for f in db.scalars(select(m.Flat).where(m.Flat.lat.is_(None)).limit(limit)).all():
            if f.addr:
                xy = gx.geocode(gx.flat_query(f.addr, f.zip))
                if xy:
                    f.lat, f.lon = xy
                    db.commit()
        for l in db.scalars(select(m.Listing).where(m.Listing.lat.is_(None), m.Listing.address != "").limit(limit)).all():
            xy = gx.geocode(gx.listing_query(l.address, l.locality))
            if xy:
                l.lat, l.lon = xy
                db.commit()


def geo_later() -> None:
    """Souřadnice doplní na pozadí, aby se uložení nezdrželo."""
    threading.Thread(target=lambda: ensure_geo(), daemon=True).start()


def snapshot_portfolio_now() -> None:
    """Porovná odhad z nabídek s potvrzenou hodnotou bytu. Při větší změně vytvoří návrh, hodnotu nemění."""
    with Session(engine) as db:
        latest = latest_flat_values(db)
        for r in portfolio_values(db):
            if r["estimate"] is None:
                continue
            est = Decimal(str(round(r["estimate"], 2)))
            cur = latest.get(r["id"])
            pending = db.scalar(select(m.ValueSuggestion).where(
                m.ValueSuggestion.flat_id == r["id"], m.ValueSuggestion.status == "čeká"))
            # změna do 3 % se nenavrhuje; bez potvrzené hodnoty se navrhne vždy
            small = cur is not None and cur.amount and abs(est - cur.amount) / cur.amount < Decimal("0.03")
            if small:
                if pending:
                    db.delete(pending)
                continue
            if pending:
                pending.amount, pending.basis, pending.n = est, r["basis"], r["n"]
                pending.created_at = datetime.now(timezone.utc)
            else:
                db.add(m.ValueSuggestion(flat_id=r["id"], amount=est, basis=r["basis"], n=r["n"]))
        db.commit()


def pick_snapshot(snaps: list, days: int, tolerance: int):
    """Snímek zhruba před daným počtem dní (tolerance ve dnech)."""
    target = datetime.now(timezone.utc) - timedelta(days=days)
    cands = [s for s in snaps if abs((s.taken_at - target).days) <= tolerance]
    return min(cands, key=lambda s: abs((s.taken_at - target).total_seconds())) if cands else None


NEWS_STATE: dict = {"running": False, "step": "", "result": None, "error": None}


def monday_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def run_news_scan() -> int:
    """Stáhne tržní přehled na tento týden a nahradí jím předchozí zprávy téhož týdne."""
    today = datetime.now(timezone.utc).date()
    week = monday_of(today)
    items = nw.search_news(today.isoformat())
    with Session(engine) as db:
        for old in db.scalars(select(m.MarketNews).where(m.MarketNews.week == week)).all():
            db.delete(old)
        for it in items:
            db.add(m.MarketNews(week=week, **it))
        db.commit()
    print(f"[news] přehled {week}: {len(items)} zpráv", flush=True)
    return len(items)


def _run_news_bg() -> None:
    try:
        NEWS_STATE.update(result=run_news_scan(), error=None)
    except Exception as e:
        NEWS_STATE.update(error=str(e))
    finally:
        NEWS_STATE.update(running=False, step="")


def _benchmark_scheduler() -> None:
    """Běží na pozadí: hned po startu a pak každé pondělí ráno (hledání nájmů a prodejů + tržní přehled)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Prague")
    except Exception:
        tz = None
    try:
        run_benchmark_refresh()  # při startu u všech bytů
    except Exception as e:
        print(f"[benchmark] start selhal: {e}", flush=True)
    try:
        with Session(engine) as db:
            has_news = db.scalar(select(m.MarketNews.id).where(
                m.MarketNews.week == monday_of(datetime.now(timezone.utc).date())).limit(1)) is not None
        if not has_news:
            run_news_scan()
    except Exception as e:
        print(f"[news] start selhal: {e}", flush=True)
    last_bench_day = last_news_day = None
    while True:
        time.sleep(1800)
        now = datetime.now(tz) if tz else datetime.now()
        if now.day == BENCH_DAY and now.hour >= BENCH_HOUR and last_bench_day != now.date():
            last_bench_day = now.date()
            try:
                run_benchmark_refresh()  # měsíční hledání nabídek u všech dispozic, odhad hodnoty
                snapshot_portfolio_now()  # měsíční snímek hodnoty všech bytů (kumulativně)
            except Exception as e:
                print(f"[benchmark] měsíční hledání selhalo: {e}", flush=True)
        if now.weekday() == BENCH_WEEKDAY and now.hour >= BENCH_HOUR and last_news_day != now.date():
            last_news_day = now.date()
            try:
                run_news_scan()
            except Exception as e:
                print(f"[news] pondělní přehled selhal: {e}", flush=True)


app = FastAPI(title="Správa bytů", version="0.3.0", lifespan=lifespan, dependencies=[Depends(require_auth)])

@app.put("/flats/{flat_id}/valuation")
def set_valuation_radius(flat_id: int, body: s.ValuationIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    f.valuation_radius_km = body.radius_km
    db.commit()
    return {"ok": True, "radius_km": body.radius_km}




# ---------- pomocné funkce ----------

def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def _cost_active(c, d: date) -> bool:
    return (c.valid_from is None or c.valid_from <= d) and (c.valid_to is None or c.valid_to >= d)


def _set_cost_from(db: Session, f: m.Flat, name: str, val, eff: date, old: dict, new: dict) -> None:
    """Nastaví částku platby od data eff. Starou hodnotu nechá v historii, pokud se mění až po jejím začátku."""
    cur = next((c for c in f.costs if c.name == name and _cost_active(c, eff)), None)
    if cur is None:
        db.add(m.FlatCost(flat_id=f.id, name=name, amount=val, valid_from=eff))
        new[name] = float(val)
    elif cur.amount != val:
        old[name] = float(cur.amount)
        new[name] = float(val)
        if cur.valid_from is None or eff > cur.valid_from:
            cur.valid_to = eff - timedelta(days=1)
            db.add(m.FlatCost(flat_id=f.id, name=name, amount=val, valid_from=eff))
        else:
            cur.amount = val  # oprava hned od začátku platnosti


def get_flat(db: Session, flat_id: int) -> m.Flat:
    f = db.get(m.Flat, flat_id)
    if f is None:
        raise HTTPException(404, "Byt nenalezen")
    return f


def get_statement(db: Session, flat_id: int, year: int) -> m.Statement | None:
    return db.scalar(select(m.Statement).where(m.Statement.flat_id == flat_id, m.Statement.year == year))


def same(item: m.StatementItem, new: s.ItemIn) -> bool:
    return money(item.cost) == money(new.cost) and money(item.paid) == money(new.paid)


def item_dict(item) -> dict:
    return {"cost": float(item.cost), "paid": float(item.paid)}


def lease_out(l: m.Lease) -> dict:
    return {
        "id": l.id,
        "valid_from": l.valid_from.isoformat(),
        "valid_to": l.valid_to.isoformat() if l.valid_to else None,
        "rent": float(l.rent),
        "deposit": float(l.deposit),
        "advances": {a.name: float(a.amount) for a in l.advances},
        "persons": l.persons,
    }


def statement_out(st: m.Statement) -> dict:
    cost = sum((i.cost for i in st.items), Decimal("0"))
    paid = sum((i.paid for i in st.items), Decimal("0"))
    return {
        "year": st.year,
        "doc_label": st.doc_label,
        "items": [{"id": i.id, "name": i.name, "cost": float(i.cost), "paid": float(i.paid)} for i in st.items],
        "balance": float(paid - cost),  # kladné = přeplatek, záporné = nedoplatek
    }


def flat_out(f: m.Flat) -> dict:
    return {
        "id": f.id,
        "addr": f.addr,
        "dispo": f.dispo,
        "floor": f.floor,
        "zip": f.zip,
        "area_m2": float(f.area_m2) if f.area_m2 is not None else None,
        "owner": f.owner,
        "bank_acc": f.bank_acc,
        "tenant": {
            "name": f.tenant_name,
            "email": f.tenant_email,
            "phone": f.tenant_phone,
            "others": [x.strip() for x in f.tenant_others.split(",") if x.strip()],
        },
        "leases": [lease_out(l) for l in f.leases],
        "statements": [statement_out(st) for st in f.statements],
        "svj_costs": [{"id": c.id, "name": c.name, "amount": float(c.amount), "valid_from": c.valid_from.isoformat() if c.valid_from else None} for c in f.costs if _cost_active(c, date.today())],
        "svj_all": [{"id": c.id, "name": c.name, "amount": float(c.amount), "valid_from": c.valid_from.isoformat() if c.valid_from else None, "valid_to": c.valid_to.isoformat() if c.valid_to else None} for c in f.costs],
        "acquisition": (
            {"acquired_on": f.acquisition.acquired_on.isoformat() if f.acquisition.acquired_on else None,
             "price": float(f.acquisition.price) if f.acquisition.price is not None else None,
             "source": f.acquisition.source, "note": f.acquisition.note}
            if f.acquisition else None
        ),
    }


DATED_ATTRS = ("dispo", "floor", "zip", "area_m2", "tenant_name", "tenant_email", "tenant_phone", "bank_acc")


def apply_flat(f: m.Flat, data: s.FlatIn) -> None:
    if (f.addr, f.zip) != (data.addr, data.zip):
        f.lat = f.lon = None  # změnila se adresa, souřadnice se zjistí znovu
    f.addr = data.addr
    before = {a: getattr(f, a) for a in DATED_ATTRS}
    f.dispo, f.floor, f.zip, f.owner, f.bank_acc = data.dispo, data.floor, data.zip, data.owner, data.bank_acc
    if "area_m2" in data.model_fields_set:
        f.area_m2 = data.area_m2
    f.tenant_name = data.tenant.name
    f.tenant_email = data.tenant.email
    f.tenant_phone = data.tenant.phone
    f.tenant_others = ",".join(x.strip() for x in data.tenant.others if x.strip())
    # Ruční změna je nejnovější údaj: starší dokument ji nepřepíše
    dates = dict(f.field_dates or {})
    for a in DATED_ATTRS:
        if getattr(f, a) != before[a]:
            dates[a] = date.today().isoformat()
    f.field_dates = dates


# ---------- byty ----------

@app.get("/flats")
def list_flats(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return [flat_out(f) for f in flats]


DEFAULT_OWNER = "Michal Gajdoš"  # vlastník bytů je vždy správce; prodávající z kupní smlouvy se nepřebírá


@app.post("/flats", status_code=201)
def create_flat(data: s.FlatIn, db: Session = Depends(get_db)):
    f = m.Flat()
    apply_flat(f, data)
    f.owner = f.owner or DEFAULT_OWNER
    db.add(f)
    db.flush()
    rematch_all(db)
    db.commit()
    geo_later()
    return flat_out(f)


@app.get("/flats/{flat_id}")
def read_flat(flat_id: int, db: Session = Depends(get_db)):
    return flat_out(get_flat(db, flat_id))


@app.put("/flats/{flat_id}")
def update_flat(flat_id: int, data: s.FlatIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    apply_flat(f, data)
    f.owner = f.owner or DEFAULT_OWNER
    db.flush()
    rematch_all(db)
    db.commit()
    geo_later()
    return flat_out(f)


@app.delete("/flats/{flat_id}", status_code=204)
def delete_flat(flat_id: int, db: Session = Depends(get_db)):
    db.delete(get_flat(db, flat_id))
    db.commit()
    return Response(status_code=204)


@app.post("/flats/{flat_id}/merge/{other_id}")
def merge_flats(flat_id: int, other_id: int, db: Session = Depends(get_db)):
    """Sloučí byt other_id do flat_id. Dokumenty, předpisy, vyúčtování a historie přejdou na cílový byt, druhý byt se smaže."""
    if flat_id == other_id:
        raise HTTPException(400, "Byt nelze sloučit sám se sebou")
    target = get_flat(db, flat_id)
    other = get_flat(db, other_id)
    target_years = set(db.scalars(select(m.Statement.year).where(m.Statement.flat_id == target.id)))
    other_years = set(db.scalars(select(m.Statement.year).where(m.Statement.flat_id == other.id)))
    clash = sorted(target_years & other_years)
    if clash:
        raise HTTPException(409, f"Oba byty mají vyúčtování za rok {', '.join(map(str, clash))}. Nejdřív jedno z nich smažte.")

    for model in (m.Lease, m.Statement, m.Document, m.Version):
        db.execute(update(model).where(model.flat_id == other.id).values(flat_id=target.id))

    target_aliases = set(db.scalars(select(m.FlatAlias.alias).where(m.FlatAlias.flat_id == target.id)))
    for al in db.scalars(select(m.FlatAlias).where(m.FlatAlias.flat_id == other.id)).all():
        if al.alias in target_aliases:
            db.delete(al)
        else:
            al.flat_id = target.id
            target_aliases.add(al.alias)
    other_addr = documents.normalize(other.addr)[:200]
    if other_addr and other_addr not in target_aliases and other_addr != documents.normalize(target.addr)[:200]:
        db.add(m.FlatAlias(flat_id=target.id, alias=other_addr))
    db.commit()

    # Po přesunu už druhý byt nemá žádné napojené záznamy, takže ho lze smazat
    db.delete(get_flat(db, other_id))
    db.commit()
    return flat_out(get_flat(db, flat_id))


# ---------- Price Benchmark ----------

def listing_out(l: m.Listing) -> dict:
    return {
        "id": l.id, "kind": l.kind, "locality": l.locality, "dispo": l.dispo,
        "area_m2": float(l.area_m2) if l.area_m2 is not None else None,
        "price": float(l.price), "url": l.url, "title": l.title, "source": l.source,
    }


@app.post("/benchmark/refresh")
def start_benchmark_refresh() -> dict:
    """Spustí hledání nájmů i prodejů u všech bytů na pozadí. Pokud už běží, jen vrátí stav."""
    if not BENCH_STATE["running"]:
        BENCH_STATE.update(running=True, step="začínám", done=0, total=0, result=None, error=None)
        threading.Thread(target=_run_refresh_bg, daemon=True).start()
    return dict(BENCH_STATE)


@app.get("/benchmark/refresh")
def benchmark_refresh_status() -> dict:
    return dict(BENCH_STATE)


@app.get("/portfolio")
def portfolio(db: Session = Depends(get_db)):
    """Hodnota portfolia: součet posledních hodnot bytů, změna za měsíc a historie (stejné číslo jako graf na přehledu)."""
    rows = portfolio_values(db)
    total = sum(r["value"] for r in rows if r["value"] is not None)
    hist = portfolio_history(db)
    target = today_prague() - timedelta(days=30)
    base = next((h for h in reversed(hist) if h["date"] <= target.isoformat()), None)
    base_total = base["total"] if base else None
    base_vals = latest_flat_values(db, target)
    for r in rows:
        e = base_vals.get(r["id"])
        r["change"] = (r["value"] - float(e.amount)) if (r["value"] is not None and e) else None
        # hodnota starší než 31 dní se má aktualizovat (ruční zadání nebo měsíční odhad)
        r["stale"] = r["value_date"] is None or (today_prague() - date.fromisoformat(r["value_date"])).days > 31
    return {
        "total": round(total),
        "flats_count": len(rows),
        "without_value": sum(1 for r in rows if r["value"] is None),
        "stale_count": sum(1 for r in rows if r.get("stale")),
        "suggestion_count": sum(1 for r in rows if r.get("suggestion")),
        "change_1m": round(total - base_total) if base_total else None,
        "change_1m_pct": bm.growth_pct(total, base_total),
        "base_date": base["date"] if base else None,
        "flats": rows,
        "history": hist[-52:],
    }


@app.get("/flats/{flat_id}/values")
def list_flat_values(flat_id: int, db: Session = Depends(get_db)):
    get_flat(db, flat_id)
    vs = db.scalars(select(m.FlatValue).where(m.FlatValue.flat_id == flat_id)
                    .order_by(m.FlatValue.on_date.desc(), m.FlatValue.source)).all()
    return [{"id": v.id, "on_date": v.on_date.isoformat(), "amount": float(v.amount),
             "source": v.source, "note": v.note} for v in vs]


@app.post("/flats/{flat_id}/values", status_code=201)
def add_flat_value(flat_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Ruční hodnota bytu k zadanému datu. Stejné datum a zdroj se přepíše."""
    get_flat(db, flat_id)
    try:
        on = date.fromisoformat(str(body.get("on_date") or ""))
    except ValueError:
        raise HTTPException(400, "Zadejte datum.")
    if on > today_prague():
        raise HTTPException(400, "Datum nesmí být v budoucnu.")
    try:
        amount = Decimal(str(body.get("amount") or "").replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except Exception:
        raise HTTPException(400, "Zadejte hodnotu v Kč.")
    if amount <= 0:
        raise HTTPException(400, "Hodnota musí být kladná.")
    note = str(body.get("note") or "").strip()
    row = db.scalar(select(m.FlatValue).where(
        m.FlatValue.flat_id == flat_id, m.FlatValue.on_date == on, m.FlatValue.source == "ručně"))
    if row:
        row.amount, row.note = amount, note
    else:
        db.add(m.FlatValue(flat_id=flat_id, on_date=on, amount=amount, source="ručně", note=note))
    db.commit()
    return {"ok": True}


@app.post("/value-suggestions/{sug_id}/accept")
def accept_value_suggestion(sug_id: int, db: Session = Depends(get_db)):
    """Potvrdí návrh z trhu: stane se z něj ruční hodnota bytu k dnešku."""
    sg = db.get(m.ValueSuggestion, sug_id)
    if not sg or sg.status != "čeká":
        raise HTTPException(404, "Návrh už neexistuje.")
    today = today_prague()
    row = db.scalar(select(m.FlatValue).where(
        m.FlatValue.flat_id == sg.flat_id, m.FlatValue.on_date == today, m.FlatValue.source == "ručně"))
    note = f"potvrzeno z trhu ({sg.n} nabídek)" if sg.n else "potvrzeno z trhu"
    if row:
        row.amount, row.note = sg.amount, note
    else:
        db.add(m.FlatValue(flat_id=sg.flat_id, on_date=today, amount=sg.amount, source="ručně", note=note))
    sg.status = "potvrzeno"
    db.commit()
    return {"ok": True}


@app.post("/value-suggestions/{sug_id}/dismiss")
def dismiss_value_suggestion(sug_id: int, db: Session = Depends(get_db)):
    sg = db.get(m.ValueSuggestion, sug_id)
    if not sg or sg.status != "čeká":
        raise HTTPException(404, "Návrh už neexistuje.")
    sg.status = "zamítnuto"
    db.commit()
    return {"ok": True}


@app.delete("/flat-values/{value_id}")
def delete_flat_value(value_id: int, db: Session = Depends(get_db)):
    v = db.get(m.FlatValue, value_id)
    if not v:
        raise HTTPException(404, "Hodnota neexistuje.")
    db.delete(v)
    db.commit()
    return {"ok": True}


@app.get("/news")
def news_list(db: Session = Depends(get_db)):
    week = db.scalar(select(m.MarketNews.week).order_by(m.MarketNews.week.desc()).limit(1))
    if not week:
        return {"week": None, "items": []}
    items = db.scalars(select(m.MarketNews).where(m.MarketNews.week == week)
                       .order_by(m.MarketNews.importance, m.MarketNews.id)).all()
    return {"week": week, "items": [
        {"id": i.id, "category": i.category, "importance": i.importance, "title": i.title,
         "summary": i.summary, "url": i.url} for i in items]}


@app.post("/news/refresh")
def start_news_refresh() -> dict:
    if not NEWS_STATE["running"]:
        NEWS_STATE.update(running=True, step="hledám", result=None, error=None)
        threading.Thread(target=_run_news_bg, daemon=True).start()
    return dict(NEWS_STATE)


@app.get("/news/refresh")
def news_refresh_status() -> dict:
    return dict(NEWS_STATE)


@app.get("/version")
def app_version() -> dict:
    """Identifikace verze nasazení. Stránka podle ní pozná, že vyšla nová verze, a sama se obnoví."""
    return {"version": os.getenv("RENDER_GIT_COMMIT", "dev")}


@app.get("/benchmark")
def benchmark_overview(db: Session = Depends(get_db)):
    all_listings = db.scalars(select(m.Listing)).all()
    rents = [l for l in all_listings if l.kind == "rent"]
    sales = [l for l in all_listings if l.kind == "sale"]
    flats = []
    for f in db.scalars(select(m.Flat).order_by(m.Flat.addr)).all():
        cur = f.leases[-1].rent if f.leases else None
        e = bm.estimate(f.dispo, f.area_m2, rents)
        sug = bm.suggest_rent(cur, e["estimate"]) if e else None
        # Prodejní odhad je stejný jako hodnota v portfoliu: podobné nabídky v okruhu kolem adresy
        v = gx.valuation(f.lat, f.lon, f.dispo, f.area_m2, f.valuation_radius_km or gx.DEFAULT_RADIUS_KM, sales)
        es = None
        if v["value"] is not None:
            es = {
                "estimate": v["value"],
                "per_m2": round(v["value"] / float(f.area_m2)) if f.area_m2 else None,
                "n": v["n"], "confidence": v["confidence"], "basis": v["basis"],
                "radius_km": v["radius_km"], "comps": v["comps"], "low": None, "high": None,
            }
        g1, g12 = _snapshot_growth(db, "sale", f.dispo) if f.dispo else (None, None)
        if es is not None:
            es["growth_1m"], es["growth_12m"] = g1, g12
        flats.append({
            "id": f.id, "addr": f.addr, "dispo": f.dispo,
            "area_m2": float(f.area_m2) if f.area_m2 is not None else None,
            "current_rent": float(cur) if cur is not None else None,
            "estimate": e,
            "diff": float(cur) - e["estimate"] if (cur is not None and e) else None,
            "suggested_rent": float(sug) if sug is not None else None,
            "sale": es,
            "sale_reason": v["reason"],
        })
    listings = sorted(all_listings, key=lambda l: l.found_at, reverse=True)[:300]
    return {"min_diff": float(bm.MIN_DIFF_KC), "flats": flats, "listings": [listing_out(l) for l in listings]}


@app.post("/flats/{flat_id}/benchmark/search")
def search_benchmark(flat_id: int, kind: str = "rent", db: Session = Depends(get_db)):
    if kind not in ("rent", "sale"):
        raise HTTPException(400, "Neznámý typ nabídek")
    f = get_flat(db, flat_id)
    try:
        found, note = bm.search_listings(bm.DEFAULT_LOCALITY, f.dispo, kind)
    except bm.SearchError as e:
        raise HTTPException(502, str(e))
    known = set(db.scalars(select(m.Listing.url)).all())
    new = 0
    for it in found:
        if it["url"] in known:
            continue
        known.add(it["url"])
        db.add(m.Listing(kind=kind, locality=it["locality"], dispo=it["dispo"], area_m2=it["area_m2"],
                         price=it["price"], url=it["url"], title=it["title"], source="web",
                         address=it.get("address", "")))
        new += 1
    db.commit()
    return {"found": len(found), "new": new, "note": note}


@app.post("/benchmark/listings", status_code=201)
def add_listing(body: s.ListingIn, db: Session = Depends(get_db)):
    l = m.Listing(kind=body.kind, locality=body.locality or bm.DEFAULT_LOCALITY, dispo=body.dispo,
                  area_m2=body.area_m2, price=money(body.price), url=body.url, title=body.title, source="ručně",
                  address=body.address)
    db.add(l)
    db.commit()
    geo_later()
    return listing_out(l)


@app.delete("/benchmark/listings/{listing_id}", status_code=204)
def delete_listing(listing_id: int, db: Session = Depends(get_db)):
    l = db.get(m.Listing, listing_id)
    if l is None:
        raise HTTPException(404, "Nabídka nenalezena")
    db.delete(l)
    db.commit()
    return Response(status_code=204)


# ---------- pořízení bytu ----------

@app.put("/flats/{flat_id}/acquisition")
def set_acquisition(flat_id: int, body: s.AcquisitionIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    acq = db.scalar(select(m.FlatAcquisition).where(m.FlatAcquisition.flat_id == f.id))
    if acq is None:
        acq = m.FlatAcquisition(flat_id=f.id)
        db.add(acq)
    acq.acquired_on = body.acquired_on
    acq.price = money(body.price) if body.price is not None else None
    acq.source = "ručně"
    db.commit()
    return flat_out(get_flat(db, flat_id))


# ---------- platby, které platíte vy (SVJ) ----------

@app.post("/flats/{flat_id}/costs", status_code=201)
def add_cost(flat_id: int, data: s.CostIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    db.add(m.FlatCost(flat_id=f.id, name=data.name, amount=money(data.amount)))
    db.commit()
    return flat_out(f)


@app.delete("/flats/{flat_id}/costs/{cost_id}")
def delete_cost(flat_id: int, cost_id: int, db: Session = Depends(get_db)):
    c = db.get(m.FlatCost, cost_id)
    if c is None or c.flat_id != flat_id:
        raise HTTPException(404, "Položka nenalezena")
    db.delete(c)
    db.commit()
    return flat_out(get_flat(db, flat_id))


@app.post("/flats/{flat_id}/predpis-zmena")
def change_predpis(flat_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Změna předpisu od data: nájem a zálohy nájemníka i moje platby SVJ. Starý předpis zůstane v historii.
    Když je datum změny stejné jako začátek předpisu, jde o opravu a přepíše se přímo."""
    f = get_flat(db, flat_id)
    try:
        eff = date.fromisoformat(str(body.get("effective") or date.today().isoformat())[:10])
    except ValueError:
        raise HTTPException(400, "Datum platnosti není platné")
    if body.get("rent") is not None or isinstance(body.get("advances"), dict):
        cur = sorted(f.leases, key=lambda l: l.valid_from)[-1] if f.leases else None
        if cur is None:
            raise HTTPException(400, "Nejdřív zadejte nájem v průvodci smlouvou")
        new_rent = money(body["rent"]) if body.get("rent") is not None else cur.rent
        adv_src = body["advances"] if isinstance(body.get("advances"), dict) else {a.name: a.amount for a in cur.advances}
        new_adv = [m.Advance(name=str(k).strip()[:200], amount=money(v)) for k, v in adv_src.items() if str(k).strip()]
        old = {"rent": float(cur.rent), "advances": {a.name: float(a.amount) for a in cur.advances}}
        new = {"rent": float(new_rent), "advances": {a.name: float(a.amount) for a in new_adv}}
        if old != new:
            if eff <= cur.valid_from:
                cur.rent, cur.advances = new_rent, new_adv
            else:
                old_to = cur.valid_to
                cur.valid_to = eff - timedelta(days=1)
                db.add(m.Lease(flat=f, valid_from=eff, valid_to=old_to, rent=new_rent, deposit=cur.deposit,
                               persons=cur.persons, advances=new_adv))
            db.add(m.Version(flat_id=f.id, name=f"Předpis nájmu a záloh od {eff.isoformat()}",
                             action=f"změna od {eff.isoformat()}", old=old, new=new))
    if isinstance(body.get("costs"), list):
        old, new = {}, {}
        items = []
        for w in body["costs"]:
            name = str(w.get("name") or "").strip()[:200]
            if name:
                items.append((name, money(w.get("amount") or 0)))
        replace_svj_set(db, f, items, eff, old, new, doc_id=int(body["from_doc"]) if body.get("from_doc") else None)
        if old or new:
            db.add(m.Version(flat_id=f.id, name=f"Platby SVJ od {eff.isoformat()}", action=f"změna od {eff.isoformat()}",
                             old=old or None, new=new or None))
    db.commit()
    return flat_out(get_flat(db, flat_id))


@app.patch("/flats/{flat_id}/costs/{cost_id}")
def update_cost(flat_id: int, cost_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Úprava měsíční platby, kterou platíte vy (název a částka)."""
    c = db.get(m.FlatCost, cost_id)
    if c is None or c.flat_id != flat_id:
        raise HTTPException(404, "Položka nenalezena")
    if str(body.get("name") or "").strip():
        c.name = str(body["name"]).strip()[:200]
    if body.get("amount") is not None:
        c.amount = money(body["amount"])
    db.commit()
    return flat_out(get_flat(db, flat_id))


@app.patch("/leases/{lease_id}")
def update_lease_items(lease_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Úprava nájmu a záloh, které platí nájemník: nájem a celý seznam záloh (název a částka)."""
    lease = db.get(m.Lease, lease_id)
    if lease is None:
        raise HTTPException(404, "Předpis nenalezen")
    if body.get("rent") is not None:
        lease.rent = money(body["rent"])
    if isinstance(body.get("advances"), dict):
        lease.advances = [m.Advance(name=str(k).strip()[:200], amount=money(v))
                          for k, v in body["advances"].items() if str(k).strip()]
    db.commit()
    return lease_out(lease)


@app.post("/flats/{flat_id}/generated-documents", status_code=201)
def save_generated_document(flat_id: int, body: s.GeneratedDocIn, db: Session = Depends(get_db)):
    """Uloží vygenerovanou smlouvu nebo dodatek jako Word dokument u bytu."""
    f = get_flat(db, flat_id)
    data = docgen.text_docx(body.title, body.text)
    safe = re.sub(r"[^\w .-]+", "_", body.title).strip()[:120] or "dokument"
    d = m.Document(flat_id=f.id, status="zarazeno", doc_type=body.doc_type, filename=f"{safe}.docx",
                   mime=docgen.DOCX_MIME, size=len(data), data=data, summary=body.title[:500],
                   year=date.today().year, note="")
    db.add(d)
    db.commit()
    return doc_out(d)


@app.get("/landlord")
def get_landlord():
    return landlord_profile.LANDLORD


def _upsert_lease_from_contract(db: Session, f: m.Flat, body: dict) -> None:
    """Nájemní smlouva je zdroj předpisu: nastaví nájem, zálohy a počet osob pro další smlouvy."""
    vf = date.fromisoformat(body["valid_from"]) if body.get("valid_from") else None
    if vf is None:
        return
    vt = date.fromisoformat(body["valid_to"]) if body.get("valid_to") and not body.get("indefinite") else None
    adv = [m.Advance(name=str(k)[:200], amount=money(v)) for k, v in (body.get("advances") or {}).items()]
    leases = sorted(f.leases, key=lambda l: l.valid_from)
    same = next((l for l in leases if l.valid_from == vf), None)
    if same is not None:
        same.rent, same.deposit, same.valid_to = money(body.get("rent") or 0), money(body.get("deposit") or 0), vt
        same.persons = body.get("npers") or None
        same.advances = adv
        return
    for p in leases:
        if p.valid_from < vf and (p.valid_to is None or p.valid_to >= vf):
            p.valid_to = vf - timedelta(days=1)
    db.add(m.Lease(flat=f, valid_from=vf, valid_to=vt, rent=money(body.get("rent") or 0),
                   deposit=money(body.get("deposit") or 0), persons=body.get("npers") or None, advances=adv))


@app.post("/flats/{flat_id}/meter-photo")
async def meter_photo(flat_id: int, file: UploadFile = File(...), meter: str = Form(...), room: str = Form(""),
                      date_str: str = Form(""), db: Session = Depends(get_db)):
    """Fotka měřidla: uloží se ke kartě bytu jako archiv (datum, měřidlo, místnost) a přečte se z ní stav."""
    f = get_flat(db, flat_id)
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Fotka je větší než 15 MB")
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "image/jpeg"
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise HTTPException(400, "Fotka musí být JPG nebo PNG")
    day = date_str or date.today().isoformat()
    safe = re.sub(r"[^\w-]+", "_", f"{day}_{meter}_{room or 'byt'}").strip("_")[:120]
    doc = m.Document(flat_id=f.id, status="zarazeno", doc_type="foto_meridla", filename=f"{safe}.jpg", mime=mime,
                     size=len(data), data=data, summary=f"Měřidlo: {meter}, místnost: {room or 'byt'}, {day}",
                     year=date.today().year, note="")
    db.add(doc)
    db.commit()
    try:
        res = await run_in_threadpool(documents.read_meter, data, mime)
        err = None
    except documents.ClassifyError as e:
        res, err = {"value": None, "unit": None, "type_guess": None}, str(e)
    return {"doc_id": doc.id, "filename": doc.filename, "value": res["value"], "unit": res["unit"],
            "type_guess": res["type_guess"], "error": err}


@app.post("/flats/{flat_id}/protocols", status_code=201)
def create_protocol(flat_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Předávací protokol ve Word formátu, uložený ke kartě bytu."""
    f = get_flat(db, flat_id)
    if body.get("attach_photos"):
        photos = []
        for mt in body.get("meters") or []:
            if mt.get("doc_id"):
                ph = db.get(m.Document, int(mt["doc_id"]))
                if ph and ph.flat_id == f.id and ph.data:
                    photos.append((ph.filename, ph.data))
        body["_photos"] = photos
    try:
        data = protocols.handover_docx(f, body)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Zkontrolujte údaje: {e}")
    title = f"Předávací protokol {f.addr} {body.get('date') or ''}"
    safe = re.sub(r"[^\w .-]+", "_", title).strip()[:120] or "protokol"
    dv = body.get("date") or None
    d = m.Document(flat_id=f.id, status="zarazeno", doc_type="protokol", filename=f"{safe}.docx",
                   mime=docgen.DOCX_MIME, size=len(data), data=data, summary=title[:500],
                   year=date.today().year, note="",
                   valid_from=date.fromisoformat(dv[:10]) if dv else None, valid_to=None)
    db.add(d)
    db.commit()
    return doc_out(d)


@app.get("/flats/{flat_id}/contract-defaults")
def contract_defaults(flat_id: int, db: Session = Depends(get_db)):
    """Všechno, co se dá natáhnout k bytu pro novou smlouvu: fond oprav a zálohy z dokumentů, poslední předpis."""
    f = get_flat(db, flat_id)
    preds = db.scalars(select(m.Document).where(m.Document.flat_id == f.id, m.Document.doc_type == "predpis_svj")
                       .order_by(m.Document.created_at.desc())).all()
    items = []
    for d in preds:
        its = (d.extracted or {}).get("svj_items") or []
        if its:
            items = its
            break
    fund_items = [i for i in items if re.search(r"fond", str(i.get("name", "")), re.I)]
    fund = round(sum(float(i.get("amount") or 0) for i in fund_items), 2) if fund_items else None
    # Co pronajímatel nese sám (fond oprav, účetnictví, správa SVJ…) se ve smlouvě tváří jako nájemné
    landlord_costs = [{"name": str(i.get("name", "")), "amount": round(float(i.get("amount") or 0), 2)}
                      for i in items if not stl.is_billable(str(i.get("name", "")))
                      and float(i.get("amount") or 0) > 0]
    last = sorted(f.leases, key=lambda l: l.valid_from)[-1] if f.leases else None
    if last and last.advances:
        advances = {a.name: float(a.amount) for a in last.advances}
        adv_source = "smlouva"
    else:
        advances = {str(i["name"]): round(float(i.get("amount") or 0), 2) for i in items
                    if stl.is_billable(str(i.get("name", ""))) and float(i.get("amount") or 0)}
        adv_source = "předpis SVJ" if advances else None
    return {
        "landlord": landlord_profile.LANDLORD,
        "addr": f.addr, "zip": f.zip, "floor": f.floor, "dispo": f.dispo,
        "area": float(f.area_m2) if f.area_m2 is not None else None,
        "bank_acc": f.bank_acc, "vs": getattr(f, "vs", None),
        "fund": fund, "fund_source": "předpis SVJ" if fund_items else None,
        "landlord_costs": landlord_costs, "landlord_costs_source": "předpis SVJ" if landlord_costs else None,
        "advances": advances, "advances_source": adv_source,
        "base_persons": last.persons if last else None,
        "rent": float(last.rent) if last else None,
        "deposit": float(last.deposit) if last else None,
        "tenant": {"name": f.tenant_name, "email": f.tenant_email, "phone": f.tenant_phone},
    }


@app.post("/flats/{flat_id}/contracts", status_code=201)
def create_contract(flat_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Nájemní smlouva nebo dodatek ve Word formátu, uložený ke kartě bytu."""
    f = get_flat(db, flat_id)
    kind = body.get("kind")
    body["landlord"] = landlord_profile.merged(body.get("landlord"))
    try:
        if kind == "najem":
            data, dtype = contracts.lease_docx(f, body), "smlouva"
            _upsert_lease_from_contract(db, f, body)
            title = f"Nájemní smlouva {f.addr} {body.get('contract_date') or ''}"
        elif kind == "dodatek":
            last = sorted(f.leases, key=lambda l: l.valid_from)[-1] if f.leases else None
            old = {"rent": float(last.rent) if last else 0.0, "deposit": float(last.deposit) if last else 0.0}
            data, dtype = contracts.amendment_docx(f, body, old), "dodatek"
            title = f"Dodatek č. {body.get('amendment_no') or ''} {f.addr}"
        else:
            raise HTTPException(400, "Neznámý typ dokumentu")
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Zkontrolujte údaje: {e}")
    safe = re.sub(r"[^\w .-]+", "_", title).strip()[:120] or "dokument"
    vf = body.get("valid_from") or body.get("eff_date") or None
    vt = None if (kind == "dodatek" or body.get("indefinite")) else (body.get("valid_to") or None)
    d = m.Document(flat_id=f.id, status="zarazeno", doc_type=dtype, filename=f"{safe}.docx",
                   mime=docgen.DOCX_MIME, size=len(data), data=data, summary=title[:500],
                   year=date.today().year, note="",
                   valid_from=date.fromisoformat(vf[:10]) if vf else None,
                   valid_to=date.fromisoformat(vt[:10]) if vt else None)
    db.add(d)
    db.commit()
    return doc_out(d)


@app.get("/documents/{doc_id}/pdf")
async def document_pdf(doc_id: int, db: Session = Depends(get_db)):
    """PDF z Word dokumentu (smlouva, dodatek, vyúčtování). Převod dělá LibreOffice na serveru."""
    from urllib.parse import quote
    d = get_document(db, doc_id)
    if d.mime != docgen.DOCX_MIME:
        raise HTTPException(400, "PDF jde vytvořit jen z Word dokumentu")
    try:
        pdf = await run_in_threadpool(pdfgen.docx_to_pdf, d.data)
    except pdfgen.PdfError as e:
        raise HTTPException(503, str(e))
    name = re.sub(r"\.docx$", "", d.filename, flags=re.I) + ".pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})


@app.post("/tenants/read-id")
async def read_tenant_id(files: list[UploadFile] = File(...), flat_id: int | None = Form(None),
                         db: Session = Depends(get_db)):
    """Přečte doklad nového nájemníka z fotek. Fotky se uloží jako doklad nájemníka."""
    if flat_id is not None:
        get_flat(db, flat_id)
    images, docs = [], []
    for up in files:
        data = await up.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{up.filename}: soubor je větší než 15 MB")
        mime = up.content_type or mimetypes.guess_type(up.filename or "")[0] or "image/jpeg"
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            raise HTTPException(400, "Doklad musí být fotka (JPG nebo PNG)")
        images.append((data, mime))
        d = m.Document(filename=(up.filename or "doklad.jpg")[:300], mime=mime, size=len(data), data=data,
                       doc_type="doklad_najemnika", flat_id=flat_id,
                       status="zarazeno" if flat_id is not None else "ke_kontrole",
                       summary="Doklad totožnosti nájemníka")
        db.add(d)
        docs.append(d)
    db.commit()
    try:
        fields = await run_in_threadpool(documents.read_id, images)
    except documents.ClassifyError as e:
        raise HTTPException(503, f"Fotky jsou uložené, ale doklad se nepřečetl: {e}")
    return {"fields": fields, "documents": [doc_out(d) for d in docs]}


@app.get("/flats/{flat_id}/settlement")
def settlement_preview(flat_id: int, year: int | None = None, db: Session = Depends(get_db)):
    """Náhled vyúčtování za kalendářní rok (výchozí: loni). Nic se neukládá."""
    f = get_flat(db, flat_id)
    y = year or date.today().year - 1
    return stl.to_json(stl.compute(db, f, y))


@app.post("/flats/{flat_id}/settlement", status_code=201)
def settlement_save(flat_id: int, year: int | None = None, db: Session = Depends(get_db)):
    """Vygeneruje Word vyúčtování pro nájemníka a přehled pro pronajímatele a uloží je ke kartě bytu."""
    f = get_flat(db, flat_id)
    y = year or date.today().year - 1
    res = stl.compute(db, f, y)
    out = []
    for doc_type, label, data in (
        ("vyuctovani_najemnikovi", "Vyúčtování nájemník", docgen.settlement_tenant_docx(f, res)),
        ("jine", "Přehled pro pronajímatele", docgen.settlement_landlord_docx(f, res)),
    ):
        d = m.Document(flat_id=f.id, status="zarazeno", doc_type=doc_type,
                       filename=f"{label} {y} {f.addr}.docx"[:300], mime=docgen.DOCX_MIME, size=len(data),
                       data=data, summary=f"{label} za rok {y}", year=y, note="",
                       valid_from=date(y, 1, 1), valid_to=date(y, 12, 31))
        db.add(d)
        out.append(d)
    db.commit()
    return {"preview": stl.to_json(res), "documents": [doc_out(d) for d in out]}


# ---------- předpisy ----------

@app.post("/flats/{flat_id}/leases", status_code=201)
def add_lease(flat_id: int, data: s.LeaseIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    lease = m.Lease(
        flat=f, valid_from=data.valid_from, valid_to=data.valid_to,
        rent=money(data.rent), deposit=money(data.deposit), persons=data.persons,
        advances=[m.Advance(name=k, amount=money(v)) for k, v in data.advances.items()],
    )
    db.add(lease)
    db.add(m.Version(
        flat_id=f.id, name=f"Předpis {data.valid_from.isoformat()} až {data.valid_to.isoformat() if data.valid_to else 'bez konce'}",
        action="uložen", new={"rent": float(lease.rent), "advances": {k: float(v) for k, v in data.advances.items()}},
    ))
    db.commit()
    return lease_out(lease)


# ---------- vyúčtování: porovnání a rozhodnutí ----------

@app.post("/flats/{flat_id}/statements/{year}/compare")
def compare_statement(flat_id: int, year: int, body: s.ExtractedIn, db: Session = Depends(get_db)):
    """Porovná položky z dokumentu s uloženými. Nic nezapisuje."""
    get_flat(db, flat_id)
    st = get_statement(db, flat_id, year)
    stored = {i.name: i for i in st.items} if st else {}
    diffs = []
    for it in body.items:
        old = stored.get(it.name)
        if old is None:
            diffs.append({"name": it.name, "status": "new", "stored": None, "extracted": item_dict(it)})
        elif not same(old, it):
            diffs.append({"name": it.name, "status": "changed", "stored": item_dict(old), "extracted": item_dict(it)})
    return {"year": year, "exists": st is not None, "diffs": diffs, "unchanged": len(body.items) - len(diffs)}


@app.post("/flats/{flat_id}/statements/{year}/resolve")
def resolve_statement(flat_id: int, year: int, body: s.ResolveIn, db: Session = Depends(get_db)):
    """Zapíše položky. U každé změněné položky musí být rozhodnutí: replace, nebo keep."""
    f = get_flat(db, flat_id)
    st = get_statement(db, flat_id, year)
    if st is None:
        st = m.Statement(flat=f, year=year, doc_label=body.doc_label)
        db.add(st)
        db.flush()
    stored = {i.name: i for i in st.items}

    undecided = [
        it.name for it in body.items
        if it.name in stored and not same(stored[it.name], it) and it.name not in body.decisions
    ]
    if undecided:
        raise HTTPException(409, {"message": "Některé rozdíly nemají rozhodnutí", "undecided": undecided})

    results = []
    for it in body.items:
        old = stored.get(it.name)
        if old is None:
            item = m.StatementItem(name=it.name, cost=money(it.cost), paid=money(it.paid), position=len(st.items))
            st.items.append(item)
            stored[it.name] = item
            db.add(m.Version(flat_id=f.id, statement_id=st.id, name=it.name, action="přidáno",
                             new=item_dict(item)))
            results.append({"name": it.name, "result": "přidáno"})
        elif same(old, it):
            continue
        elif body.decisions[it.name] == "replace":
            db.add(m.Version(flat_id=f.id, statement_id=st.id, item_id=old.id, name=it.name, action="nahrazeno",
                             old=item_dict(old), new=item_dict(it)))
            old.cost, old.paid = money(it.cost), money(it.paid)
            results.append({"name": it.name, "result": "nahrazeno"})
        else:
            db.add(m.Version(flat_id=f.id, statement_id=st.id, item_id=old.id, name=it.name, action="ponecháno",
                             old=item_dict(old), new=item_dict(it)))
            results.append({"name": it.name, "result": "ponecháno"})
    db.commit()
    return {"applied": results}


# ---------- historie ----------

@app.get("/versions")
def list_versions(flat_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.Version).order_by(m.Version.id.desc()).limit(300)
    if flat_id is not None:
        q = q.where(m.Version.flat_id == flat_id)
    return [
        {"id": v.id, "flat_id": v.flat_id, "name": v.name, "action": v.action,
         "old": v.old, "new": v.new, "created_at": v.created_at.isoformat()}
        for v in db.scalars(q)
    ]


@app.post("/versions/{version_id}/restore")
def restore_version(version_id: int, db: Session = Depends(get_db)):
    v = db.get(m.Version, version_id)
    if v is None or v.action != "nahrazeno" or v.item_id is None or v.old is None:
        raise HTTPException(400, "Tuto změnu nejde vrátit")
    item = db.get(m.StatementItem, v.item_id)
    if item is None:
        raise HTTPException(404, "Položka už neexistuje")
    current = item_dict(item)
    item.cost, item.paid = money(v.old["cost"]), money(v.old["paid"])
    db.add(m.Version(flat_id=v.flat_id, statement_id=v.statement_id, item_id=item.id, name=v.name,
                     action="vráceno", old=current, new=v.old))
    db.commit()
    return {"ok": True}


# ---------- dokumenty ----------

def get_document(db: Session, doc_id: int) -> m.Document:
    d = db.get(m.Document, doc_id)
    if d is None:
        raise HTTPException(404, "Dokument nenalezen")
    return d


def doc_out(d: m.Document) -> dict:
    return {
        "id": d.id, "flat_id": d.flat_id, "status": d.status, "doc_type": d.doc_type, "group_key": d.group_key,
        "filename": d.filename, "mime": d.mime, "size": d.size, "year": d.year,
        "summary": d.summary, "note": d.note, "suggested_flat_id": d.suggested_flat_id,
        "valid_from": d.valid_from.isoformat() if d.valid_from else None,
        "valid_to": d.valid_to.isoformat() if d.valid_to else None,
        "extracted": d.extracted, "created_at": d.created_at.isoformat(),
        "taken_at": d.taken_at.isoformat() if d.taken_at else None,
    }


# Údaje bytu, které se doplní z dokumentů: klíč v extracted -> atribut bytu
FLAT_FIELDS = {
    "dispo": "dispo",
    "floor": "floor",
    "zip_city": "zip",
    "area_m2": "area_m2",
    "tenant_name": "tenant_name",
    "tenant_email": "tenant_email",
    "tenant_phone": "tenant_phone",
    "tenant_bank_account": "bank_acc",
}
FLAT_FIELD_LABELS = {
    "dispo": "Dispozice", "floor": "Patro", "zip": "PSČ a město", "area_m2": "Plocha (m²)",
    "tenant_name": "Nájemník", "tenant_email": "E-mail nájemníka", "tenant_phone": "Telefon nájemníka",
    "bank_acc": "Účet pro platby",
}


def doc_date(d: m.Document) -> date:
    """Datum dokumentu: podle něj se pozná, který údaj je novější. Když není v dokumentu, bere se datum nahrání."""
    ex = d.extracted or {}
    for key in ("doc_date", "contract_date"):
        try:
            return date.fromisoformat(str(ex.get(key) or "")[:10])
        except ValueError:
            pass
    return (d.created_at or datetime.now(timezone.utc)).date()


def enrich_flat(db: Session, d: m.Document) -> None:
    """Údaje bytu z dokumentu. Novější dokument přepíše starší údaj, starší dokument nepřepíše novější.
    Přepsaná hodnota zůstane v historii a starší údaje zůstávají uložené v dokumentech."""
    ex = d.extracted or {}
    if d.flat_id is None or not ex:
        return
    f = db.get(m.Flat, d.flat_id)
    if f is None:
        return
    when = doc_date(d)
    dates = dict(f.field_dates or {})
    for key, attr in FLAT_FIELDS.items():
        raw = str(ex.get(key) or "").strip()
        if not raw or raw.lower() in ("null", "none"):
            continue
        if attr == "area_m2":
            try:
                new = Decimal(raw.replace(",", ".").replace(" ", "")).quantize(Decimal("0.01"))
            except ArithmeticError:
                continue
        else:
            new = raw[:60 if attr in ("bank_acc", "tenant_phone") else 120]
        old = getattr(f, attr)
        if old in (None, ""):
            setattr(f, attr, new)
            dates[attr] = when.isoformat()
            db.add(m.Version(flat_id=f.id, name=FLAT_FIELD_LABELS[attr], action="doplněno z dokumentu",
                             old=None, new={"value": str(new), "doc_id": d.id}))
            continue
        prev = dates.get(attr)
        if prev:
            try:
                if when <= date.fromisoformat(prev):
                    continue  # tento dokument není novější než údaj, který už je v bytu
            except ValueError:
                pass
        if old == new:
            dates[attr] = when.isoformat()
            continue
        db.add(m.Version(flat_id=f.id, name=FLAT_FIELD_LABELS[attr], action="přepsáno z dokumentu",
                         old={"value": str(old)}, new={"value": str(new), "doc_id": d.id, "date": when.isoformat()}))
        setattr(f, attr, new)
        dates[attr] = when.isoformat()
    f.field_dates = dates


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v).replace(" ", "").replace(",", ".")).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def sync_lease(db: Session, d: m.Document) -> None:
    """Nájem ze smlouvy nebo dodatku. Novější smlouva předchozí nájem ukončí. Starší smlouva nepřepíše novější nájem."""
    if d.flat_id is None or d.doc_type not in ("smlouva", "dodatek"):
        return
    ex = d.extracted or {}
    rent = _dec(ex.get("rent"))
    if rent is None or rent <= 0:
        return
    when = doc_date(d)
    vf = _day(ex.get("lease_from")) or when
    vt = _day(ex.get("lease_to"))
    deposit = _dec(ex.get("deposit")) or Decimal("0")
    raw_adv = ex.get("advances") if isinstance(ex.get("advances"), dict) else {}
    adv = {}
    for k, v in raw_adv.items():
        val = _dec(v)
        if val is not None:
            adv[str(k)[:200]] = val
    f = db.get(m.Flat, d.flat_id)
    leases = sorted(f.leases, key=lambda l: l.valid_from)
    same = next((l for l in leases if l.valid_from == vf), None)
    if same is not None:
        if same.source_date and when < same.source_date:
            return
        old_adv = {a.name: float(a.amount) for a in same.advances}
        new_adv_f = {k: float(v) for k, v in adv.items()}
        if (same.rent, same.deposit, same.valid_to) != (rent, deposit, vt) or old_adv != new_adv_f:
            db.add(m.Version(flat_id=f.id, name=f"Předpis od {vf.isoformat()}", action="přepsáno z dokumentu",
                             old={"rent": float(same.rent), "advances": old_adv},
                             new={"rent": float(rent), "advances": new_adv_f, "doc_id": d.id}))
        same.rent, same.deposit, same.valid_to, same.source_date = rent, deposit, vt, when
        same.advances = [m.Advance(name=k, amount=v) for k, v in adv.items()]
        return
    if any(l.valid_from > vf for l in leases):
        return  # tato smlouva je starší než předpis, který už je v bytu
    for p in leases:
        if p.valid_from < vf and (p.valid_to is None or p.valid_to >= vf):
            p.valid_to = vf - timedelta(days=1)  # předchozí nájem končí den před začátkem nového
    lease = m.Lease(flat=f, valid_from=vf, valid_to=vt, rent=rent, deposit=deposit, source_date=when,
                    advances=[m.Advance(name=k, amount=v) for k, v in adv.items()])
    db.add(lease)
    db.add(m.Version(flat_id=f.id, name=f"Předpis od {vf.isoformat()}", action="doplněno z dokumentu",
                     old=None, new={"rent": float(rent), "advances": {k: float(v) for k, v in adv.items()}, "doc_id": d.id}))


def rematch_all(db: Session) -> None:
    """Po založení nebo úpravě bytu znovu projde dokumenty. Nezařazené zkusí přiřadit (jen při jisté shodě),
    u zařazených doplní prázdné údaje bytu."""
    flats = db.scalars(select(m.Flat)).all()
    aliases = db.scalars(select(m.FlatAlias)).all()
    for d in db.scalars(select(m.Document).where(m.Document.extracted.is_not(None))).all():
        if d.flat_id is None:
            fid = documents.suggest_flat(d.extracted, flats, aliases, d.filename)
            d.suggested_flat_id = fid
            try:
                conf = float((d.extracted or {}).get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if fid and conf >= documents.AUTO_FILE_CONFIDENCE:
                d.flat_id = fid
                d.status = "zarazeno"
        enrich_flat(db, d)
        sync_svj_costs(db, d)
        sync_acquisition(db, d)
        sync_lease(db, d)


SVJ_COSTS = {"svj_services": "Služby SVJ (měsíční záloha)", "svj_admin": "Příspěvek SVJ na správu domu a pozemku"}


def _ckey(name: str) -> str:
    """Klíč položky pro porovnání: bez mezer a velkých písmen (různá čtení se liší v mezerách)."""
    return re.sub(r"\s+", "", name).upper()


def replace_svj_set(db: Session, f: m.Flat, items: list, eff: date, old: dict, new: dict, doc_id: int | None = None) -> None:
    """Celý předpis SVJ nahradí novým od data eff. Položky, které v novém nejsou, se ukončí; starý předpis
    zůstane v historii (valid_to), aby šel použít pro vyúčtování dřívějšího období."""
    wanted = {}
    for name, amt in items:
        wanted[_ckey(name)] = (name, amt)
    seen = set()
    for c in [c for c in f.costs if _cost_active(c, eff)]:
        k = _ckey(c.name)
        if k in wanted:
            seen.add(k)
            name, amt = wanted[k]
            if c.amount == amt and c.name == name:
                continue
            if c.valid_from is None or eff > c.valid_from:
                old[c.name] = float(c.amount)
                c.valid_to = eff - timedelta(days=1)
                c.ended_by_doc_id = doc_id
                db.add(m.FlatCost(flat_id=f.id, name=name, amount=amt, valid_from=eff, source_doc_id=doc_id))
                new[name] = float(amt)
            else:  # oprava hned od začátku platnosti
                old[c.name] = float(c.amount)
                c.name, c.amount = name, amt
                new[name] = float(amt)
        else:
            old[c.name] = float(c.amount)
            if c.valid_from is None or eff > c.valid_from:
                c.valid_to = eff - timedelta(days=1)
                c.ended_by_doc_id = doc_id
            else:
                db.delete(c)
    for k, (name, amt) in wanted.items():
        if k not in seen:
            db.add(m.FlatCost(flat_id=f.id, name=name, amount=amt, valid_from=eff, source_doc_id=doc_id))
            new[name] = float(amt)
    if doc_id is not None:  # položky z tohoto dokladu, které už na něm nejsou (chyba čtení), se smažou; ostatní předpisy zůstávají
        for c in list(f.costs):
            if c.source_doc_id == doc_id and _ckey(c.name) not in wanted:
                old.setdefault(c.name, float(c.amount))
                db.delete(c)


def sync_svj_costs(db: Session, d: m.Document) -> None:
    """Z předpisu SVJ nahradí celý rozpis „Co platím SVJ“ od data dokumentu (nebo dnes). Opakované čtení nic nezdvojí."""
    if d.flat_id is None or d.doc_type != "predpis_svj":
        return
    ex = d.extracted or {}
    f = db.get(m.Flat, d.flat_id)
    items: list = []
    for it in ex.get("svj_items") or []:
        if not isinstance(it, dict):
            continue
        val = _dec(it.get("amount"))
        name = str(it.get("name") or "").strip()[:200]
        if name and val is not None and val > 0:
            items.append((name, val))
    if not items:  # starší čtení: jen dvě souhrnné částky
        for key, name in SVJ_COSTS.items():
            val = _dec(ex.get(key))
            if val is not None and val > 0:
                items.append((name, val))
    if not items:
        return
    eff = d.valid_from or date.today()
    old, new = {}, {}
    replace_svj_set(db, f, items, eff, old, new, doc_id=d.id)
    if new or old:
        db.add(m.Version(flat_id=f.id, name=f"Platby SVJ od {eff.isoformat()}", action=f"změna od {eff.isoformat()}",
                         old=old or None, new={**new, "doc_id": d.id}))


def run_classification(d: m.Document, db: Session) -> None:
    """Přečte dokument a nastaví typ, rok, shrnutí a navržený byt. Automaticky zařadí jen při jisté shodě."""
    if d.status == "rozpracováno":
        return  # stránka, která čeká na zbytek dokumentu; čte se až celý dokument
    flats = db.scalars(select(m.Flat)).all()
    aliases = db.scalars(select(m.FlatAlias)).all()
    try:
        result = documents.classify(d.data, d.mime)
    except documents.ClassifyError as e:
        d.note = str(e)
        d.status = "ke_kontrole" if d.flat_id is None else d.status
        return

    d.extracted = result
    d.doc_type = result.get("doc_type", "jine")
    try:
        d.year = int(result["year"]) if result.get("year") else None
    except (TypeError, ValueError):
        d.year = None
    d.summary = str(result.get("summary") or "")[:500]
    d.note = ""
    d.suggested_flat_id = documents.suggest_flat(result, flats, aliases, d.filename)

    if d.flat_id is None and d.suggested_flat_id and result["confidence"] >= documents.AUTO_FILE_CONFIDENCE:
        d.flat_id = d.suggested_flat_id
        d.status = "zarazeno"
    elif d.flat_id is None:
        d.status = "ke_kontrole"
    sync_acquisition(db, d)
    sync_lease(db, d)
    enrich_flat(db, d)
    sync_svj_costs(db, d)


def sync_acquisition(db: Session, d: m.Document) -> None:
    """Z kupní smlouvy doplní datum a cenu pořízení bytu. Ruční údaj má přednost."""
    ex = d.extracted or {}
    if d.doc_type != "kupni_smlouva" or d.flat_id is None:
        return
    price_raw, date_raw = ex.get("price"), ex.get("contract_date")
    if price_raw is None and not date_raw:
        return
    acq = db.scalar(select(m.FlatAcquisition).where(m.FlatAcquisition.flat_id == d.flat_id))
    if acq is None:
        acq = m.FlatAcquisition(flat_id=d.flat_id)
        db.add(acq)
    if acq.source == "ručně" and acq.price is not None:
        return
    try:
        acq.price = money(price_raw) if price_raw is not None else acq.price
    except (ArithmeticError, ValueError, TypeError):
        pass
    try:
        acq.acquired_on = date.fromisoformat(str(date_raw)[:10]) if date_raw else acq.acquired_on
    except ValueError:
        pass
    acq.source, acq.doc_id = "kupní smlouva", d.id


def exif_taken(raw: bytes):
    """Datum pořízení fotky z EXIF (původní čas exponování, jinak čas uložení)."""
    try:
        from PIL import Image
        from zoneinfo import ZoneInfo
        with Image.open(io.BytesIO(raw)) as im:
            ex = im.getexif()
            sub = ex.get_ifd(0x8769) if ex else {}
            val = sub.get(36867) or sub.get(36868) or (ex.get(306) if ex else None)
        if not val:
            return None
        return datetime.strptime(str(val)[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Prague"))
    except Exception:
        return None


@app.post("/documents", status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),
    flat_id: int | None = Form(None),
    group_key: str | None = Form(None),
    db: Session = Depends(get_db),
):
    # flat_id posílá karta bytu: dokumenty jdou rovnou k tomuto bytu, bez dotazu
    if flat_id is not None:
        get_flat(db, flat_id)
    out = []
    for up in files:
        data = await up.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{up.filename}: soubor je větší než 15 MB")
        mime = up.content_type or mimetypes.guess_type(up.filename or "")[0] or "application/octet-stream"
        d = m.Document(filename=(up.filename or "dokument")[:300], mime=mime, size=len(data), data=data)
        if mime.startswith("image/"):
            d.taken_at = exif_taken(data)
        if group_key:
            # Stránka jednoho dokumentu: čeká, až přijde celý dokument, a teprve pak se spojí do PDF
            if not mime.startswith("image/"):
                raise HTTPException(400, f"{up.filename}: do dokumentu po stránkách jdou jen fotky. PDF nahrajte zvlášť.")
            d.group_key = group_key[:40]
            d.status = "rozpracováno"
            d.flat_id = flat_id
            db.add(d)
            db.commit()
            out.append(doc_out(d))
            continue
        if flat_id is not None:
            d.flat_id = flat_id
            d.status = "zarazeno"
        db.add(d)
        db.flush()
        await run_in_threadpool(run_classification, d, db)
        db.commit()
        out.append(doc_out(d))
    return out


@app.get("/documents")
def list_documents(flat_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.Document).order_by(m.Document.id.desc())
    if flat_id is not None:
        q = q.where(m.Document.flat_id == flat_id)
    return [doc_out(d) for d in db.scalars(q)]


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: int, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    return Response(content=d.data, media_type=d.mime, headers={"Content-Disposition": "inline"})


@app.patch("/documents/{doc_id}")
def update_document(doc_id: int, body: s.DocumentUpdate, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    if body.doc_type is not None:
        if body.doc_type not in documents.DOC_TYPES:
            raise HTTPException(400, "Neznámý typ dokumentu")
        d.doc_type = body.doc_type
    if "flat_id" in body.model_fields_set:
        if body.flat_id is not None:
            get_flat(db, body.flat_id)
        d.flat_id = body.flat_id
        d.status = "zarazeno" if body.flat_id is not None else "ke_kontrole"
        # Zapamatuje si, jak se adresa na dokumentu píše, aby příště byl byt poznán sám
        addr = documents.normalize((d.extracted or {}).get("address") or "")
        if body.flat_id is not None and addr:
            exists = db.scalar(select(m.FlatAlias).where(
                m.FlatAlias.flat_id == body.flat_id, m.FlatAlias.alias == addr[:200]))
            if exists is None:
                db.add(m.FlatAlias(flat_id=body.flat_id, alias=addr[:200]))
    sync_acquisition(db, d)
    sync_lease(db, d)
    enrich_flat(db, d)
    sync_svj_costs(db, d)
    db.commit()
    return doc_out(d)


@app.post("/documents/{doc_id}/classify")
async def reclassify_document(doc_id: int, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    await run_in_threadpool(run_classification, d, db)
    db.commit()
    return doc_out(d)


def _images_to_pdf(images: list[bytes]) -> bytes:
    from PIL import Image
    pages = []
    for raw in images:
        pages.append(Image.open(io.BytesIO(raw)).convert("RGB"))
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return out.getvalue()


def _group_pages(db: Session, key: str) -> list[m.Document]:
    return list(db.scalars(select(m.Document).where(
        m.Document.group_key == key, m.Document.status == "rozpracováno").order_by(m.Document.id)).all())


@app.post("/documents/{doc_id}/undo-svj")
def undo_svj_schedule(doc_id: int, db: Session = Depends(get_db)):
    """Odstraní rozpis SVJ nahraný z tohoto dokladu a vrátí předchozí předpis, který ten doklad ukončil."""
    d = get_document(db, doc_id)
    if d.flat_id is None:
        raise HTTPException(400, "Dokument není přiřazen k bytu.")
    f = get_flat(db, d.flat_id)
    created = [c for c in f.costs if c.source_doc_id == doc_id]
    ended = [c for c in f.costs if c.ended_by_doc_id == doc_id]
    if not created and not ended:
        raise HTTPException(404, "Z tohoto dokladu není nahraný žádný rozpis.")
    old = {c.name: float(c.amount) for c in created}
    for c in ended:
        c.valid_to, c.ended_by_doc_id = None, None
    for c in created:
        db.delete(c)
    db.add(m.Version(flat_id=f.id, name=f"Rozpis SVJ z dokladu {d.filename}", action="odstraněn rozpis z dokladu",
                     old=old or None, new={"doc_id": doc_id}))
    db.commit()
    return flat_out(get_flat(db, f.id))


@app.post("/documents/{doc_id}/merge-batch")
async def merge_upload_batch(doc_id: int, db: Session = Depends(get_db)):
    """Spojí fotky stejného bytu a typu nahrané do 15 minut od tohoto dokumentu do jednoho PDF."""
    base = db.get(m.Document, doc_id)
    if not base or not base.mime.startswith("image/"):
        raise HTTPException(400, "Spojit se dají jen fotky.")
    sibs = [x for x in db.scalars(select(m.Document).where(
                m.Document.flat_id.is_(None) if base.flat_id is None else m.Document.flat_id == base.flat_id,
                m.Document.doc_type == base.doc_type, m.Document.status == base.status)).all()
            if x.mime.startswith("image/") and abs(x.created_at - base.created_at) <= timedelta(minutes=15)]
    if len(sibs) < 2:
        raise HTTPException(400, "Nenašel jsem další stránky nahrané se stejným dokumentem.")
    sibs.sort(key=lambda x: (x.created_at, x.id))
    pdf_bytes = await run_in_threadpool(_images_to_pdf, [x.data for x in sibs])
    taken = [x.taken_at for x in sibs if x.taken_at]
    stem = re.sub(r"\.\w+$", "", sibs[0].filename)[:200] or "dokument"
    d = m.Document(filename=f"{stem} ({len(sibs)} str.).pdf"[:300], mime="application/pdf", size=len(pdf_bytes),
                   data=pdf_bytes, flat_id=base.flat_id, status=base.status, doc_type=base.doc_type,
                   year=base.year, valid_from=base.valid_from, valid_to=base.valid_to,
                   taken_at=min(taken) if taken else None)
    for x in sibs:
        db.delete(x)
    db.add(d)
    db.commit()
    return doc_out(d)


@app.post("/document-groups/{key}/finish")
async def finish_document_group(key: str, db: Session = Depends(get_db)):
    """Spojí stránky do jednoho PDF, zařadí ho a přečte celý najednou (první stránka i ty další)."""
    docs = _group_pages(db, key)
    if not docs:
        raise HTTPException(404, "Rozpracovaný dokument nenalezen")
    if len(docs) == 1 and docs[0].mime == "application/pdf":
        pdf_bytes = docs[0].data
    elif any(x.mime == "application/pdf" for x in docs):
        raise HTTPException(400, "V dokumentu je PDF i fotky. Spojit se dají jen fotky.")
    else:
        pdf_bytes = await run_in_threadpool(_images_to_pdf, [x.data for x in docs])
    first = docs[0]
    stem = re.sub(r"\.\w+$", "", first.filename)[:200] or "dokument"
    name = first.filename if len(docs) == 1 else f"{stem} ({len(docs)} str.).pdf"
    taken = [x.taken_at for x in docs if x.taken_at]
    d = m.Document(filename=name[:300], mime="application/pdf", size=len(pdf_bytes), data=pdf_bytes,
                   flat_id=first.flat_id, status="zarazeno" if first.flat_id else "ke_kontrole",
                   taken_at=min(taken) if taken else None)
    for x in docs:
        db.delete(x)
    db.add(d)
    db.flush()
    await run_in_threadpool(run_classification, d, db)
    db.commit()
    return doc_out(d)


@app.delete("/document-groups/{key}", status_code=204)
def cancel_document_group(key: str, db: Session = Depends(get_db)):
    for x in _group_pages(db, key):
        db.delete(x)
    db.commit()


@app.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    db.delete(get_document(db, doc_id))
    db.commit()
    return Response(status_code=204)


# ---------- výpisy z účtu a ekonomika ----------

@app.post("/bank/import", status_code=201)
async def import_bank(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Výpis je větší než 15 MB")
    name = (file.filename or "").lower()
    try:
        if name.endswith(".pdf") or file.content_type == "application/pdf":
            rows = await run_in_threadpool(bank.parse_pdf, data)
        else:
            rows = bank.parse_csv(data)
    except (ValueError, documents.ClassifyError) as e:
        raise HTTPException(400, str(e))

    flats = db.scalars(select(m.Flat)).all()
    aliases = db.scalars(select(m.FlatAlias)).all()
    seen = set(db.scalars(select(m.BankTx.key)))
    counts: dict[str, int] = {}
    added = skipped = 0
    for r in rows:
        base = bank.tx_key(r)
        n = counts.get(base, 0)
        counts[base] = n + 1
        key = f"{base}#{n}"  # stejná platba dvakrát v jednom výpisu zůstane dvakrát
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        period = r["date"].strftime("%Y-%m")
        fid, how = bank.auto_match(r, period, flats, aliases)
        db.add(m.BankTx(
            key=key, date=r["date"], amount=money(r["amount"]), vs=r["vs"][:20],
            counterparty=r["counterparty"][:200], note=r["note"], period=period,
            flat_id=fid, match=how,
        ))
        added += 1
    db.commit()
    return {"added": added, "skipped": skipped}


def _forget_payer(db: Session, t: m.BankTx, flat_id: int) -> None:
    """Při opravě odebere zapamatovaného plátce u původního bytu, pokud k němu nezbývá žádná jiná platba od něj."""
    cp = bank.normalize(t.counterparty)
    if not cp:
        return
    others = db.scalars(select(m.BankTx).where(m.BankTx.flat_id == flat_id, m.BankTx.id != t.id)).all()
    if any(bank.normalize(o.counterparty) == cp for o in others):
        return
    alias = f"cp:{cp}"[:200]
    for a in db.scalars(select(m.FlatAlias).where(m.FlatAlias.flat_id == flat_id, m.FlatAlias.alias == alias)).all():
        db.delete(a)


@app.patch("/bank/{tx_id}")
def assign_tx(tx_id: int, body: s.BankAssignIn, db: Session = Depends(get_db)):
    t = db.get(m.BankTx, tx_id)
    if t is None:
        raise HTTPException(404, "Platba nenalezena")
    if "flat_id" in body.model_fields_set:
        if t.flat_id is not None and t.flat_id != body.flat_id:
            _forget_payer(db, t, t.flat_id)  # oprava: původní byt si už plátce nepamatuje
        if body.flat_id is not None:
            f = get_flat(db, body.flat_id)
            cp = bank.normalize(t.counterparty)
            if cp:  # zapamatuje si plátce, aby příště přiřadila sama
                alias = f"cp:{cp}"[:200]
                if db.scalar(select(m.FlatAlias).where(m.FlatAlias.flat_id == f.id, m.FlatAlias.alias == alias)) is None:
                    db.add(m.FlatAlias(flat_id=f.id, alias=alias))
        t.flat_id = body.flat_id
        t.match = "ručně" if body.flat_id is not None else "none"
    db.commit()
    return {"ok": True}


@app.put("/flats/{flat_id}/vs")
def set_vs(flat_id: int, body: s.VsIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    vs = re.sub(r"\D", "", body.vs)
    if not vs:
        raise HTTPException(400, "Zadejte variabilní symbol (jen číslice)")
    alias = f"vs:{vs}"
    if db.scalar(select(m.FlatAlias).where(m.FlatAlias.alias == alias, m.FlatAlias.flat_id != f.id)) is not None:
        raise HTTPException(409, "Tento variabilní symbol už má jiný byt")
    old = db.scalars(select(m.FlatAlias).where(m.FlatAlias.flat_id == f.id, m.FlatAlias.alias.like("vs:%"))).all()
    same = any(o.alias == alias for o in old)
    for o in old:
        if o.alias != alias:
            db.delete(o)
    if not same:
        db.add(m.FlatAlias(flat_id=f.id, alias=alias))
    db.commit()
    return {"vs": vs}


@app.get("/overview")
def overview(year: int | None = None, db: Session = Depends(get_db)):
    """Přehled za kalendářní rok: příjmy (přijaté platby přiřazené bytům), výdaje (náklady SVJ) a zisk, po bytech."""
    y = year or date.today().year
    years = {d.year for d in db.scalars(select(m.BankTx.date).distinct()).all()}
    years |= {d for d in db.scalars(select(m.Document.year).where(m.Document.year.is_not(None)).distinct()).all()}
    years.add(y)
    today = date.today()
    # Měsíce, za které se počítá: celý minulý rok, u letošního roku jen do aktuálního měsíce
    months_in_year = 12 if y < today.year else (today.month if y == today.year else 0)
    rows, inc, exp = [], 0.0, 0.0
    for f in db.scalars(select(m.Flat).order_by(m.Flat.addr)).all():
        res = stl.compute(db, f, y)
        r = float(res["received"])
        stmt_cost = float(res["landlord_cost"])
        # Výdaje: vyúčtování SVJ za rok, když existuje; jinak měsíční platby SVJ z karty bytu krát počet měsíců
        monthly = sum(float(c.amount) for c in f.costs if _cost_active(c, date(y, today.month if y == today.year else 12, 15)))
        by_month = sum(float(c.amount) for mo in range(1, months_in_year + 1) for c in f.costs if _cost_active(c, date(y, mo, 15)))
        c = stmt_cost if stmt_cost else round(by_month, 2)
        n_pay = db.scalar(select(func.count(m.BankTx.id)).where(
            m.BankTx.flat_id == f.id, m.BankTx.period.like(f"{y}-%"))) or 0
        inc += r
        exp += c
        rows.append({"id": f.id, "addr": f.addr, "tenant": f.tenant_name, "received": r,
                     "landlord_cost": c, "monthly_cost": monthly, "payments": n_pay,
                     "cost_source": "vyúčtování" if stmt_cost else "měsíční platby SVJ",
                     "profit": round(r - c, 2)})
    return {"year": y, "years": sorted(years, reverse=True), "income": round(inc, 2),
            "expenses": round(exp, 2), "profit": round(inc - exp, 2), "flats": rows}


@app.get("/economics")
def economics(months: int = 6, db: Session = Depends(get_db)):
    """Očekávané a přijaté platby po měsících, za každý byt zvlášť, a platby, které zatím nejsou přiřazené."""
    months = max(1, min(months, 24))
    today = date.today()
    periods = []
    y, mo = today.year, today.month
    for _ in range(months):
        periods.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
    periods.reverse()

    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    txs = db.scalars(select(m.BankTx)).all()
    received: dict[tuple[int, str], Decimal] = defaultdict(lambda: Decimal("0"))
    unmatched_by: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for t in txs:
        if t.flat_id is None:
            unmatched_by[t.period] += t.amount
        else:
            received[(t.flat_id, t.period)] += t.amount

    out = []
    for p in periods:
        first, last = bank.month_bounds(p)
        rows = []
        exp_total = rec_total = Decimal("0")
        for f in flats:
            e = bank.expected_for(f, first, last)
            r = received.get((f.id, p), Decimal("0"))
            if e == 0 and r == 0:
                continue
            if e == 0:
                st = "extra"  # platba bez smlouvy, která to pokrývá
            elif r >= e:
                st = "paid"
            elif r > 0:
                st = "partial"
            else:
                st = "unpaid"
            rows.append({
                "flat_id": f.id, "addr": f.addr, "tenant": f.tenant_name, "email": f.tenant_email,
                "expected": float(e), "received": float(r),
                "missing": float(max(e - r, Decimal("0"))), "status": st,
            })
            exp_total += e
            rec_total += r
        out.append({
            "period": p, "expected": float(exp_total), "received": float(rec_total),
            "unmatched": float(unmatched_by.get(p, Decimal("0"))), "rows": rows,
        })

    unmatched = sorted((t for t in txs if t.flat_id is None), key=lambda t: t.date, reverse=True)
    addr_of = {f.id: f.addr for f in flats}
    shown = set(periods)
    assigned = sorted((t for t in txs if t.flat_id is not None and t.period in shown),
                      key=lambda t: t.date, reverse=True)
    return {
        "months": out,
        "unmatched": [
            {"id": t.id, "date": t.date.isoformat(), "amount": float(t.amount), "vs": t.vs,
             "counterparty": t.counterparty, "note": t.note, "period": t.period}
            for t in unmatched[:200]
        ],
        "assigned": [
            {"id": t.id, "date": t.date.isoformat(), "amount": float(t.amount), "vs": t.vs,
             "counterparty": t.counterparty, "period": t.period, "flat_id": t.flat_id,
             "flat_addr": addr_of.get(t.flat_id, "?"), "match": t.match}
            for t in assigned[:300]
        ],
    }


# ---------- záloha ----------

@app.get("/export")
def export_all(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return {"flats": [flat_out(f) for f in flats]}


# ---------- jednoduchá stránka pro správu (chráněná stejným přihlášením) ----------

ICON_DIR = Path(__file__).parent / "icons"


@app.get("/icon-{size}.png")
def app_icon(size: int):
    if size not in (180, 192, 512):
        raise HTTPException(404, "Ikona neexistuje")
    return FileResponse(ICON_DIR / f"icon-{size}.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.json")
def web_manifest():
    return JSONResponse({
        "name": "MG Property", "short_name": "MG Property", "lang": "cs",
        "start_url": "/", "display": "standalone",
        "background_color": "#EEF4F0", "theme_color": "#1F5C4A",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, media_type="application/manifest+json")


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)):
    return HTMLResponse((Path(__file__).parent / "ui.html").read_text(encoding="utf-8"))
