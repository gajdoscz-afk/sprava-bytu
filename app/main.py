import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from pathlib import Path

import mimetypes
import re
from collections import defaultdict
from datetime import date

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import bank
from . import documents
from . import models as m
from . import schemas as s
from .db import Base, engine, get_db

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

security = HTTPBasic()


def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    """Jeden uživatel, přihlášení přes BASIC_USER a BASIC_PASS. Bez nich se server nespustí."""
    user, pwd = os.getenv("BASIC_USER"), os.getenv("BASIC_PASS")
    if not user or not pwd:
        if os.getenv("ALLOW_NO_AUTH") == "1":  # jen pro lokální vývoj a testy
            return
        raise HTTPException(500, "Na serveru chybí BASIC_USER a BASIC_PASS")
    ok = secrets.compare_digest(creds.username, user) and secrets.compare_digest(creds.password, pwd)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Neplatné přihlášení", {"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # MVP: tabulky vytvoříme při startu. Až budou změny schématu, přejdeme na Alembic.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Správa bytů", version="0.3.0", lifespan=lifespan, dependencies=[Depends(require_auth)])


# ---------- pomocné funkce ----------

def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


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
        "svj_costs": [{"id": c.id, "name": c.name, "amount": float(c.amount)} for c in f.costs],
        "acquisition": (
            {"acquired_on": f.acquisition.acquired_on.isoformat() if f.acquisition.acquired_on else None,
             "price": float(f.acquisition.price) if f.acquisition.price is not None else None,
             "source": f.acquisition.source, "note": f.acquisition.note}
            if f.acquisition else None
        ),
    }


def apply_flat(f: m.Flat, data: s.FlatIn) -> None:
    f.addr = data.addr
    f.dispo, f.floor, f.zip, f.owner, f.bank_acc = data.dispo, data.floor, data.zip, data.owner, data.bank_acc
    f.tenant_name = data.tenant.name
    f.tenant_email = data.tenant.email
    f.tenant_phone = data.tenant.phone
    f.tenant_others = ",".join(x.strip() for x in data.tenant.others if x.strip())


# ---------- byty ----------

@app.get("/flats")
def list_flats(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return [flat_out(f) for f in flats]


@app.post("/flats", status_code=201)
def create_flat(data: s.FlatIn, db: Session = Depends(get_db)):
    f = m.Flat()
    apply_flat(f, data)
    db.add(f)
    db.flush()
    rematch_all(db)
    db.commit()
    return flat_out(f)


@app.get("/flats/{flat_id}")
def read_flat(flat_id: int, db: Session = Depends(get_db)):
    return flat_out(get_flat(db, flat_id))


@app.put("/flats/{flat_id}")
def update_flat(flat_id: int, data: s.FlatIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    apply_flat(f, data)
    db.flush()
    rematch_all(db)
    db.commit()
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


# ---------- předpisy ----------

@app.post("/flats/{flat_id}/leases", status_code=201)
def add_lease(flat_id: int, data: s.LeaseIn, db: Session = Depends(get_db)):
    f = get_flat(db, flat_id)
    lease = m.Lease(
        flat=f, valid_from=data.valid_from, valid_to=data.valid_to,
        rent=money(data.rent), deposit=money(data.deposit),
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
        "id": d.id, "flat_id": d.flat_id, "status": d.status, "doc_type": d.doc_type,
        "filename": d.filename, "mime": d.mime, "size": d.size, "year": d.year,
        "summary": d.summary, "note": d.note, "suggested_flat_id": d.suggested_flat_id,
        "extracted": d.extracted, "created_at": d.created_at.isoformat(),
    }


# Údaje bytu, které se doplní z dokumentů: klíč v extracted -> atribut bytu
FLAT_FIELDS = {
    "dispo": "dispo",
    "floor": "floor",
    "zip_city": "zip",
    "owner": "owner",
    "tenant_name": "tenant_name",
    "tenant_email": "tenant_email",
    "tenant_phone": "tenant_phone",
    "tenant_bank_account": "bank_acc",
}
FLAT_FIELD_LABELS = {
    "dispo": "Dispozice", "floor": "Patro", "zip": "PSČ a město", "owner": "Vlastník",
    "tenant_name": "Nájemník", "tenant_email": "E-mail nájemníka", "tenant_phone": "Telefon nájemníka",
    "bank_acc": "Účet pro platby",
}


def enrich_flat(db: Session, d: m.Document) -> None:
    """Doplní prázdné údaje bytu z dokumentu. Vyplněné hodnoty nepřepisuje a každé doplnění zapíše do historie."""
    ex = d.extracted or {}
    if d.flat_id is None or not ex:
        return
    f = db.get(m.Flat, d.flat_id)
    if f is None:
        return
    for key, attr in FLAT_FIELDS.items():
        val = str(ex.get(key) or "").strip()
        if not val or val.lower() in ("null", "none") or getattr(f, attr):
            continue
        limit = 60 if attr in ("bank_acc", "tenant_phone") else 120
        val = val[:limit]
        setattr(f, attr, val)
        db.add(m.Version(flat_id=f.id, name=FLAT_FIELD_LABELS[attr], action="doplněno z dokumentu",
                         old=None, new={"value": val, "doc_id": d.id}))


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
        sync_acquisition(db, d)


def run_classification(d: m.Document, db: Session) -> None:
    """Přečte dokument a nastaví typ, rok, shrnutí a navržený byt. Automaticky zařadí jen při jisté shodě."""
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
    enrich_flat(db, d)


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


@app.post("/documents", status_code=201)
async def upload_documents(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    out = []
    for up in files:
        data = await up.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{up.filename}: soubor je větší než 15 MB")
        mime = up.content_type or mimetypes.guess_type(up.filename or "")[0] or "application/octet-stream"
        d = m.Document(filename=(up.filename or "dokument")[:300], mime=mime, size=len(data), data=data)
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
    enrich_flat(db, d)
    db.commit()
    return doc_out(d)


@app.post("/documents/{doc_id}/classify")
async def reclassify_document(doc_id: int, db: Session = Depends(get_db)):
    d = get_document(db, doc_id)
    await run_in_threadpool(run_classification, d, db)
    db.commit()
    return doc_out(d)


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


@app.patch("/bank/{tx_id}")
def assign_tx(tx_id: int, body: s.BankAssignIn, db: Session = Depends(get_db)):
    t = db.get(m.BankTx, tx_id)
    if t is None:
        raise HTTPException(404, "Platba nenalezena")
    if "flat_id" in body.model_fields_set:
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
    return {
        "months": out,
        "unmatched": [
            {"id": t.id, "date": t.date.isoformat(), "amount": float(t.amount), "vs": t.vs,
             "counterparty": t.counterparty, "note": t.note, "period": t.period}
            for t in unmatched[:200]
        ],
    }


# ---------- záloha ----------

@app.get("/export")
def export_all(db: Session = Depends(get_db)):
    flats = db.scalars(select(m.Flat).order_by(m.Flat.addr)).all()
    return {"flats": [flat_out(f) for f in flats]}


# ---------- jednoduchá stránka pro správu (chráněná stejným přihlášením) ----------

@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)):
    return HTMLResponse((Path(__file__).parent / "ui.html").read_text(encoding="utf-8"))
